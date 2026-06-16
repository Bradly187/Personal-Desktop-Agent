import Foundation
import Network
import Combine
import QuartzCore

// MARK: — Recalibration request (PC → iPad)

struct RecalibrationRequest {
    let reason: String          // "voice_clarity" | "seasonal"
    let degradationPct: Double  // 0–100
}

// MARK: — Proactive notification (PC → iPad)

/// A proactive notification pushed by the PC (N+2): a morning brief, a reminder,
/// or an event-rule alert ("Email from … arrived"). Surfaced as a banner.
struct ProactiveNotification {
    let title: String
    let body: String
}

// MARK: — Connection state

enum ConnectionState: Equatable {
    case disconnected
    case connecting
    case reconnecting(attempt: Int)
    case connected
}

// MARK: — Incoming message types

enum BridgeMessage {
    case ack(id: String?, status: String, error: String?)
    case status(activeWindow: String?, cursorX: Int, cursorY: Int)
    case screenshot(id: String?, imageBase64: String, mime: String)
    case handwritingResult(id: String?, latex: String?, unicode: String?, error: String?)
    case unknown(type: String, raw: [String: Any])
}

// MARK: — WebSocketManager

/// Persistent WebSocket connection to the PC bridge.
/// Provides automatic reconnection with exponential backoff and mDNS discovery.
@MainActor
final class WebSocketManager: ObservableObject {

    @Published private(set) var state: ConnectionState = .disconnected
    @Published private(set) var latencyMs: Double = 0

    /// Human-readable reason the last connection attempt failed, surfaced in
    /// Settings → Connection. Set on an auth rejection (HTTP 401 — missing or
    /// wrong pairing token); cleared on a successful connect. `nil` when the
    /// last attempt did not fail on authentication.
    @Published private(set) var lastConnectionError: String?

    /// Current microphone mute state, mirrored from the PC. Updated by the
    /// `mic_state` push (voice- or iPad-initiated) and the welcome `status`.
    /// Safety-critical: surfaced as a persistent toolbar indicator.
    @Published private(set) var micMuted: Bool = false

    /// Whether the connected PC advertised support for raw binary audio frames
    /// (welcome `status` field `binary_audio`). Until a status frame confirms it,
    /// AudioStreamer uses the legacy base64 `audio_stream` path — so an old PC
    /// (no flag) keeps working unchanged.
    @Published private(set) var peerSupportsBinaryAudio: Bool = false

    // Fix #3: PassthroughSubject delivers every message to every subscriber (no single-slot loss).
    // Keep @Published lastMessage for backward compat but add a subject that never drops.
    @Published private(set) var lastMessage: BridgeMessage?
    let messageStream = PassthroughSubject<BridgeMessage, Never>()

    /// Feed of outgoing command descriptions for the activity toast.
    /// Emits a human-readable string each time a notable command is sent.
    let commandFeed = PassthroughSubject<String, Never>()

    /// Feed of error messages from the PC bridge (ack errors, inference failures).
    /// Emits a human-readable error string for display in the UI.
    let errorFeed = PassthroughSubject<String, Never>()

    /// Feed of re-calibration requests from the PC (voice drift or seasonal prompt).
    let recalibrationFeed = PassthroughSubject<RecalibrationRequest, Never>()

    /// Feed of proactive notifications from the PC (N+2): morning briefs,
    /// reminders, and event-rule alerts. Rendered as a banner.
    let proactiveFeed = PassthroughSubject<ProactiveNotification, Never>()

    /// Feed of voice calibration events from the PC (phrase prompts, results, completion).
    let calibrationEventPublisher = PassthroughSubject<CalibrationEvent, Never>()

    /// Feed of agent-pushed A2UI surfaces (approval prompts, enumerable CLARIFY).
    /// Rendered by A2UIOverlay; taps reply via `sendA2UIEvent`.
    let a2uiFeed = PassthroughSubject<A2UISurface, Never>()

    /// Feed of A2UI dismiss requests, keyed by surface_id.
    let a2uiClearFeed = PassthroughSubject<String, Never>()

    /// Feed of persistent dashboard canvases (Agent tab). Full set/replace.
    let a2uiCanvasFeed = PassthroughSubject<A2UISurface, Never>()

    /// Feed of canvas patches — components to merge by id (no full re-render).
    let a2uiCanvasUpdateFeed = PassthroughSubject<[A2UINode], Never>()

    /// Feed of canvas clear requests (reset the dashboard to empty).
    let a2uiCanvasClearFeed = PassthroughSubject<Void, Never>()

    // Injected at runtime from SettingsStore
    var settings: SettingsStore?

    // mDNS service discovery — injected at runtime
    var serviceDiscovery: ServiceDiscovery?

    private var task: URLSessionWebSocketTask?
    private var reconnectAttempt = 0
    private var reconnectWorkItem: DispatchWorkItem?
    private var receiveTask: Task<Void, Never>?
    private var pingTask: Task<Void, Never>?
    private var connectionTimeoutTask: Task<Void, Never>?

    private let maxBackoffSeconds: Double = 5
    private let connectionTimeoutSeconds: Double = 10
    private var msgCounter: Int = 0

    // Fix #9: Serial send queue preserves message ordering for delta-based messages.
    private let sendQueue = DispatchQueue(label: "ws.send.serial", qos: .userInitiated)

    // G3: Backpressure — track approximate queue depth to drop stale sensor frames.
    // Sensor streams (tilt) are sampled, not queued: only the
    // latest value matters. Non-sensor messages always pass through.
    private var _sendQueueDepth: Int = 0
    private let _maxSensorQueueDepth = 15
    // M3: audio_stream gets a higher threshold (~3s of 50ms chunks) because
    // dropping audio mid-utterance corrupts Whisper transcription. We tolerate
    // brief slowness but cap the buildup to prevent unbounded memory growth.
    private let _maxAudioQueueDepth = 60
    private static let _sensorFrameTypes: Set<String> = [
        "tilt", "tilt_position"
    ]
    /// Binary audio frame tag (byte 0). Must match the PC bridge's
    /// `_BIN_TAG_AUDIO_PCM16` (0x01) — payload is little-endian int16 PCM.
    static let binTagAudioPCM16: UInt8 = 0x01

    /// Builds a binary audio frame: the 1-byte tag prefix followed by the raw
    /// PCM payload. `nonisolated` + `static` so it is a pure function testable
    /// without a live WebSocketManager (mirrors `appendingToken`).
    nonisolated static func frameBinaryAudio(_ pcm: Data) -> Data {
        var framed = Data([binTagAudioPCM16])
        framed.append(pcm)
        return framed
    }

    // G2: Pending gesture assessment — queued when not connected, flushed on reconnect.
    private var _pendingGestureAssessment: [String]? = nil

    // MARK: — Public API

    func connect() {
        guard state == .disconnected else { return }
        lastConnectionError = nil  // clear any prior auth-failure banner
        state = .connecting
        _connect()
    }

    func disconnect() {
        reconnectWorkItem?.cancel()
        reconnectWorkItem = nil
        connectionTimeoutTask?.cancel()
        connectionTimeoutTask = nil
        pingTask?.cancel()
        pingTask = nil
        receiveTask?.cancel()
        task?.cancel(with: .normalClosure, reason: nil)
        task = nil
        reconnectAttempt = 0  // Fix #21: Reset so next connect() starts fresh
        state = .disconnected
    }

    func send(_ payload: [String: Any]) {
        guard let task, state == .connected else { return }

        // G3: Drop stale sensor frames when the send queue is backed up.
        // Only the latest reading matters for sensor streams.
        let msgType = payload["type"] as? String ?? ""
        if WebSocketManager._sensorFrameTypes.contains(msgType) && _sendQueueDepth > _maxSensorQueueDepth {
            return
        }
        // M3: bound audio_stream depth so a slow WebSocket can't OOM the iPad.
        if msgType == "audio_stream" && _sendQueueDepth > _maxAudioQueueDepth {
            AppLogger.shared.warning("WebSocketManager", "Audio stream backpressure — dropping chunk (depth=\(_sendQueueDepth))")
            return
        }

        // Fix #9: Serialize on a dedicated serial queue to guarantee ordering.
        _sendQueueDepth += 1
        let capturedTask = task
        sendQueue.async { [weak self] in
            defer { Task { @MainActor [weak self] in self?._sendQueueDepth -= 1 } }
            // E2: surface non-encodable payloads instead of silently dropping
            let data: Data
            do {
                data = try JSONSerialization.data(withJSONObject: payload)
            } catch {
                AppLogger.shared.warning("WebSocketManager",
                    "JSON encode failed for type=\(msgType): \(error.localizedDescription)")
                return
            }
            guard let text = String(data: data, encoding: .utf8) else {
                AppLogger.shared.warning("WebSocketManager", "Non-UTF8 payload dropped for type=\(msgType)")
                return
            }
            capturedTask.send(.string(text)) { [weak self] error in
                self?._onSendComplete(error, label: msgType)
            }
        }
    }

    /// Shared send-completion handler for text and binary sends. Logs rich error
    /// info and triggers reconnect on failure. `nonisolated` — called from the
    /// URLSession completion thread.
    nonisolated private func _onSendComplete(_ error: Error?, label: String) {
        guard let error else { return }
        // E5: capture rich error info before triggering reconnect
        let nsErr = error as NSError
        AppLogger.shared.warning(
            "WebSocketManager",
            "send failed (\(label)) (domain=\(nsErr.domain) code=\(nsErr.code)): \(error.localizedDescription)"
        )
        Task { @MainActor [weak self] in
            self?._handleDisconnect(error: error)
        }
    }

    /// Send a raw binary audio frame: a 1-byte `binTagAudioPCM16` prefix
    /// followed by little-endian int16 PCM (no base64, no JSON). Reuses the
    /// serial send queue and the same `_maxAudioQueueDepth` backpressure bound
    /// as the base64 `audio_stream` path.
    func sendAudioBinary(_ pcm: Data) {
        guard let task, state == .connected else { return }
        // M3: same backpressure cap as the base64 audio path.
        if _sendQueueDepth > _maxAudioQueueDepth {
            AppLogger.shared.warning("WebSocketManager",
                "Audio stream backpressure — dropping binary chunk (depth=\(_sendQueueDepth))")
            return
        }
        let framed = Self.frameBinaryAudio(pcm)

        _sendQueueDepth += 1
        let capturedTask = task
        sendQueue.async { [weak self] in
            defer { Task { @MainActor [weak self] in self?._sendQueueDepth -= 1 } }
            capturedTask.send(.data(framed)) { [weak self] error in
                self?._onSendComplete(error, label: "audio_binary")
            }
        }
    }

    /// Send a message and return the next id for correlation.
    @discardableResult
    func sendCommand(action: String, text: String? = nil, params: [String: Any] = [:]) -> String {
        msgCounter += 1
        let id = "msg-\(msgCounter)"
        var payload: [String: Any] = [
            "type": "touch_command",
            "id": id,
            "action": action,
        ]
        if let text { payload["text"] = text }
        if !params.isEmpty { payload["params"] = params }
        send(payload)
        commandFeed.send(text ?? action)
        return id
    }

    /// Reply to an agent-pushed A2UI surface. `value` is the activated control's
    /// value; `values` carries form-field state keyed by field_id.
    func sendA2UIEvent(surfaceId: String,
                       event: String,
                       value: String?,
                       values: [String: String]) {
        var payload: [String: Any] = [
            "type": "a2ui_event",
            "surface_id": surfaceId,
            "event": event,
            "ts": CACurrentMediaTime(),
        ]
        if let value { payload["value"] = value }
        if !values.isEmpty { payload["values"] = values }
        send(payload)
    }

    // MARK: — Connection internals

    /// Determines the WebSocket URL to connect to.
    /// Priority: mDNS discovered endpoint (if no manual override) > manual settings > fallback default.
    /// The pairing token (C1) is appended as a `?token=` query param on whichever
    /// base URL is chosen — the bridge rejects tokenless connections with HTTP 401.
    private func resolveConnectionURL() -> URL {
        let base: URL
        // If mDNS discovered a host and user hasn't manually overridden, use discovered endpoint
        if let discovery = serviceDiscovery,
           !discovery.hasManualOverride,
           let host = discovery.discoveredHost,
           let port = discovery.discoveredPort,
           let url = URL(string: "ws://\(host):\(port)/ws") {
            base = url
        } else {
            // Fall back to manual settings or default
            base = settings?.wsURLOrDefault ?? URL(string: "ws://192.168.18.2:8765/ws")!
        }
        return WebSocketManager.appendingToken(settings?.pairingToken ?? "", to: base)
    }

    /// Returns `url` with the pairing `token` added as a `?token=` query item.
    /// Percent-encodes the value, replaces any existing `token` param, and
    /// returns the URL unchanged when the token is empty. `nonisolated` so it
    /// is a pure function testable without a live WebSocketManager.
    nonisolated static func appendingToken(_ token: String, to url: URL) -> URL {
        let trimmed = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty,
              var comps = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
            return url
        }
        var items = comps.queryItems ?? []
        items.removeAll { $0.name == "token" }
        items.append(URLQueryItem(name: "token", value: trimmed))
        comps.queryItems = items
        return comps.url ?? url
    }

    private func _connect() {
        let url = resolveConnectionURL()

        let session = URLSession(configuration: .default)
        let wsTask = session.webSocketTask(with: url)
        self.task = wsTask
        wsTask.resume()

        state = .connecting

        // Connection timeout: if no message received within 10s, treat as failed
        connectionTimeoutTask?.cancel()
        connectionTimeoutTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(10_000_000_000))
            guard !Task.isCancelled else { return }
            guard let self else { return }
            // If still connecting (no message received), trigger disconnect/reconnect
            if self.state == .connecting {
                AppLogger.shared.warning("WebSocketManager", "Connection timeout after \(self.connectionTimeoutSeconds)s")
                wsTask.cancel(with: .abnormalClosure, reason: nil)
                self._handleDisconnect(error: URLError(.timedOut))
            }
        }

        receiveTask = Task { [weak self] in
            guard let self else { return }
            do {
                let firstMessage = try await wsTask.receive()
                await MainActor.run {
                    self.connectionTimeoutTask?.cancel()
                    self.connectionTimeoutTask = nil
                    self.lastConnectionError = nil  // handshake succeeded
                    self.state = .connected
                    self.reconnectAttempt = 0
                    self._startPingTimer()
                    self._flushPendingGestureAssessment()  // G2
                }
                self._handleReceived(message: firstMessage)
                try await self._receiveLoop(task: wsTask)
            } catch {
                // The token rides in the query string — never log it verbatim.
                let safeURL = WebSocketManager._redactedURLString(url)
                let httpStatus = (wsTask.response as? HTTPURLResponse)?.statusCode
                AppLogger.shared.error("WebSocketManager", "Connection error: \(error.localizedDescription) — URL: \(safeURL) status: \(httpStatus.map(String.init) ?? "n/a")")
                await MainActor.run {
                    self.connectionTimeoutTask?.cancel()
                    self.connectionTimeoutTask = nil
                    if httpStatus == 401 {
                        self._handleAuthFailure()
                    } else {
                        self._handleDisconnect(error: error)
                    }
                }
            }
        }
    }

    private func _receiveLoop(task: URLSessionWebSocketTask) async throws {
        while !Task.isCancelled {
            let message = try await task.receive()
            _handleReceived(message: message)
        }
    }

    private func _handleReceived(message: URLSessionWebSocketTask.Message) {
        switch message {
        case .string(let text):
            _handle(text: text)
        case .data(let data):
            if let text = String(data: data, encoding: .utf8) {
                _handle(text: text)
            }
        @unknown default:
            break
        }
    }

    private func _handle(text: String) {
        guard let data = text.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return }

        let type = json["type"] as? String ?? ""

        // Handle pong for latency measurement — don't propagate to lastMessage
        if type == "pong" {
            let sentMs = json["t"] as? Double ?? 0
            let nowMs = CACurrentMediaTime() * 1000
            Task { @MainActor in
                self.latencyMs = nowMs - sentMs
            }
            return
        }

        let id = json["id"] as? String

        let parsed: BridgeMessage
        switch type {
        case "ack":
            parsed = .ack(
                id: id,
                status: json["status"] as? String ?? "ok",
                error: json["error"] as? String
            )
            // Surface inference/execution errors to the UI via errorFeed
            if let errMsg = json["error"] as? String, !errMsg.isEmpty {
                errorFeed.send(errMsg)
            }
        case "status":
            let cursor = json["cursor"] as? [String: Int] ?? [:]
            // Welcome/status frames carry the current mute state so the
            // indicator is correct immediately on (re)connect.
            if let muted = json["muted"] as? Bool {
                micMuted = muted
            }
            // Capability negotiation: the PC advertises binary audio support in
            // its welcome frame. Absent (old PC) → stays false → base64 path.
            if let binaryAudio = json["binary_audio"] as? Bool {
                peerSupportsBinaryAudio = binaryAudio
            }
            parsed = .status(
                activeWindow: json["active_window"] as? String,
                cursorX: cursor["x"] ?? 0,
                cursorY: cursor["y"] ?? 0
            )
        case "mic_state":
            micMuted = json["muted"] as? Bool ?? false
            parsed = .unknown(type: type, raw: json)
        case "screenshot":
            parsed = .screenshot(
                id: id,
                imageBase64: json["image"] as? String ?? "",
                mime: json["mime"] as? String ?? "image/png"
            )
        case "handwriting_result":
            parsed = .handwritingResult(
                id: id,
                latex: json["latex"] as? String,
                unicode: json["unicode"] as? String,
                error: json["error"] as? String
            )
        case "recalibration_request":
            let reason  = json["reason"]          as? String ?? "voice_clarity"
            let pct     = json["degradation_pct"] as? Double ?? 0.0
            recalibrationFeed.send(RecalibrationRequest(reason: reason, degradationPct: pct))
            parsed = .unknown(type: type, raw: json)

        case "proactive_notification":
            let title = json["title"] as? String ?? "Notification"
            let body  = json["body"]  as? String ?? ""
            proactiveFeed.send(ProactiveNotification(title: title, body: body))
            parsed = .unknown(type: type, raw: json)

        case "a2ui_surface":
            // Re-serialize the raw dict and decode into the typed surface model;
            // a decode failure is logged and dropped (renderer never sees junk).
            if let data = try? JSONSerialization.data(withJSONObject: json),
               let surface = try? JSONDecoder().decode(A2UISurface.self, from: data) {
                a2uiFeed.send(surface)
            } else {
                AppLogger.shared.warning("a2ui", "failed to decode a2ui_surface")
            }
            parsed = .unknown(type: type, raw: json)

        case "a2ui_clear":
            if let surfaceId = json["surface_id"] as? String {
                a2uiClearFeed.send(surfaceId)
            }
            parsed = .unknown(type: type, raw: json)

        case "a2ui_canvas":
            // Persistent dashboard surface for the Agent tab. Decoded like
            // a2ui_surface (same shape); the canvas store caches it.
            if let data = try? JSONSerialization.data(withJSONObject: json),
               let surface = try? JSONDecoder().decode(A2UISurface.self, from: data) {
                a2uiCanvasFeed.send(surface)
            } else {
                AppLogger.shared.warning("a2ui", "failed to decode a2ui_canvas")
            }
            parsed = .unknown(type: type, raw: json)

        case "a2ui_canvas_update":
            // Patch: merge these components into the live canvas by id.
            if let comps = json["components"],
               let data = try? JSONSerialization.data(withJSONObject: comps),
               let nodes = try? JSONDecoder().decode([A2UINode].self, from: data) {
                a2uiCanvasUpdateFeed.send(nodes)
            } else {
                AppLogger.shared.warning("a2ui", "failed to decode a2ui_canvas_update")
            }
            parsed = .unknown(type: type, raw: json)

        case "a2ui_canvas_clear":
            a2uiCanvasClearFeed.send(())
            parsed = .unknown(type: type, raw: json)

        case "calibration_phrase":
            // PC → iPad: present the next phrase to the user
            let phrase = json["phrase"]  as? String ?? ""
            let index  = json["index"]   as? Int    ?? 0
            let total  = json["total"]   as? Int    ?? 0
            calibrationEventPublisher.send(.phrasePrompt(phrase: phrase, index: index, total: total))
            parsed = .unknown(type: type, raw: json)

        case "calibration_result":
            // PC → iPad: result for the phrase just spoken
            let expected = json["expected"] as? String ?? ""
            let heard    = json["heard"]    as? String ?? ""
            let matched  = json["matched"]  as? Bool   ?? false
            calibrationEventPublisher.send(.phraseResult(expected: expected, heard: heard, matched: matched))
            parsed = .unknown(type: type, raw: json)

        case "calibration_complete":
            let accuracy    = json["accuracy"]     as? Double ?? 0.0
            let corrections = json["corrections"]  as? Int    ?? 0
            calibrationEventPublisher.send(.complete(accuracy: accuracy, correctionsAdded: corrections))
            parsed = .unknown(type: type, raw: json)

        case "calibration_error":
            let msg = json["message"] as? String ?? "Unknown calibration error"
            calibrationEventPublisher.send(.error(msg))
            parsed = .unknown(type: type, raw: json)

        default:
            parsed = .unknown(type: type, raw: json)
        }

        // Fix #3: Emit on both the subject (guaranteed delivery) and @Published (backward compat)
        lastMessage = parsed
        messageStream.send(parsed)
    }

    private func _handleDisconnect(error: Error?) {
        task = nil
        receiveTask?.cancel()
        receiveTask = nil
        pingTask?.cancel()
        pingTask = nil

        // Fix #21: Cancel any pending reconnect to prevent multiple timers firing
        reconnectWorkItem?.cancel()

        // Reset drag state on disconnect — prevents isDragging stuck true
        if settings?.isDragging == true {
            settings?.isDragging = false
            settings?.activeDwellAction = .leftClick
        }

        reconnectAttempt += 1
        state = .reconnecting(attempt: reconnectAttempt)

        let delay = min(pow(2.0, Double(reconnectAttempt - 1)), maxBackoffSeconds)

        let workItem = DispatchWorkItem { [weak self] in
            Task { @MainActor [weak self] in
                guard let self else { return }
                self.state = .connecting
                self._connect()
            }
        }
        reconnectWorkItem = workItem
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: workItem)
    }

    /// Handle a pairing-token rejection (HTTP 401). Unlike a transport drop we
    /// do NOT auto-reconnect: retrying with the same missing/wrong token just
    /// fails identically and spins a hot loop. Surface a clear message, settle
    /// to `.disconnected`, and let the user paste the token and tap Reconnect.
    private func _handleAuthFailure() {
        task = nil
        receiveTask?.cancel()
        receiveTask = nil
        pingTask?.cancel()
        pingTask = nil
        reconnectWorkItem?.cancel()
        reconnectWorkItem = nil
        reconnectAttempt = 0

        let hasToken = !(settings?.pairingToken.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ?? true)
        let message = hasToken
            ? "Connection refused (401): the pairing token is incorrect. Check the token printed in the PC startup log (“iPad pairing token: …”) and re-enter it in Settings → Connection, then tap Reconnect."
            : "Connection refused (401): a pairing token is required. Enter the token printed in the PC startup log (“iPad pairing token: …”) in Settings → Connection, then tap Reconnect."
        lastConnectionError = message
        errorFeed.send(message)
        AppLogger.shared.warning("WebSocketManager", "Auth failure (401) — token \(hasToken ? "wrong" : "missing")")

        state = .disconnected
    }

    /// Returns the URL's string with the query (which carries the pairing token)
    /// stripped, for safe logging.
    nonisolated static func _redactedURLString(_ url: URL) -> String {
        var comps = URLComponents(url: url, resolvingAgainstBaseURL: false)
        comps?.queryItems = nil
        return comps?.url?.absoluteString ?? "ws://\(url.host ?? "?"):\(url.port.map(String.init) ?? "?")\(url.path)"
    }

    // MARK: — Latency Ping

    private func _startPingTimer() {
        pingTask?.cancel()
        pingTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                guard !Task.isCancelled else { break }
                guard let self else { break }
                let nowMs = CACurrentMediaTime() * 1000
                self.send(["type": "ping", "t": nowMs])
            }
        }
    }
}

// MARK: — Dwell action helpers

extension WebSocketManager {
    /// Sends a `set_dwell_action` message to the PC bridge, informing it of the new active action type.
    func sendSetDwellAction(_ action: DwellActionType) {
        send(["type": "set_dwell_action", "action_type": action.rawValue])
    }
}

// MARK: — Sensor message helpers

extension WebSocketManager {
    func sendTilt(rx: Double, ry: Double) {
        send(["type": "tilt", "rx": rx, "ry": ry])
    }

    func sendTiltPosition(x: Double, y: Double) {
        send(["type": "tilt_position", "x": x, "y": y])
    }

    func sendRatchet() {
        send(["type": "tilt_ratchet", "ts": CACurrentMediaTime()])
    }

    func sendSensorSwitch(from fromSensor: String?, to toSensor: String) {
        var msg: [String: Any] = ["type": "sensor_switch", "to": toSensor, "ts": CACurrentMediaTime()]
        if let from = fromSensor {
            msg["from"] = from
        }
        send(msg)
    }

    func sendCursorPause() {
        send(["type": "cursor_pause", "ts": CACurrentMediaTime()])
    }

    func sendCursorResume() {
        send(["type": "cursor_resume", "ts": CACurrentMediaTime()])
    }

    func sendKeyword(word: String, confidence: Double) {
        msgCounter += 1
        send(["type": "keyword", "id": "kw-\(msgCounter)", "word": word, "confidence": confidence])
        commandFeed.send("Keyword: \(word)")
    }


    func sendTrackpadMove(dx: Int, dy: Int) {
        send(["type": "trackpad", "event": "move", "dx": dx, "dy": dy])
    }

    func sendTrackpadTap(button: String = "left") {
        msgCounter += 1
        send(["type": "trackpad", "id": "tp-\(msgCounter)", "event": "tap", "button": button])
        commandFeed.send("\(button == "left" ? "Left" : "Right") Click")
    }

    func sendTrackpadScroll(direction: String, clicks: Int = 3) {
        send(["type": "trackpad", "event": "scroll", "direction": direction, "clicks": clicks])
        commandFeed.send("Scroll \(direction)")
    }

    /// Tilt dwell-to-click: the cursor was held still long enough. The PC
    /// fires whatever DwellActionToolbar selected at the current cursor.
    func sendDwellClick() {
        send(["type": "dwell_click"])
    }

    func sendTiltTap() {
        msgCounter += 1
        send(["type": "tilt_tap", "id": "tt-\(msgCounter)"])
        commandFeed.send("Tilt Tap")
    }

    func sendHandwritingImage(base64PNG: String) {
        msgCounter += 1
        send(["type": "handwriting_image", "id": "hw-\(msgCounter)", "image": base64PNG])
    }

    func sendAudioStream(samplesBase64: String, frames: Int) {
        send(["type": "audio_stream", "samples": samplesBase64, "frames": frames])
    }

    // MARK: — Voice calibration

    /// Tell the PC to start a guided calibration session.
    func sendCalibrationStart(condition: String, quick: Bool) {
        send(["type": "calibration_start", "condition": condition, "quick": quick])
    }

    /// Cancel an in-progress calibration session.
    func sendCalibrationCancel() {
        send(["type": "calibration_cancel"])
    }

    /// Notify the PC of a manual pain-day override so it immediately
    /// adapts VAD thresholds and PainDayEngine state.
    func sendPainDayOverride(active: Bool) {
        send(["type": "pain_day_override", "active": active])
    }

    /// Sync the user's flare degrade profile (which sensors get harder on a
    /// flare) so BehavioralTwinState gates pain-day relaxation to match.
    func sendFlareProfile(voiceDegrades: Bool, gestureDegrades: Bool,
                          tiltDegrades: Bool, soundDegrades: Bool,
                          vadScale: Double) {
        send([
            "type": "flare_profile",
            "voice_degrades": voiceDegrades,
            "gesture_degrades": gestureDegrades,
            "tilt_degrades": tiltDegrades,
            "sound_degrades": soundDegrades,
            "flare_vad_scale": vadScale,
        ])
    }

    /// Send the user's gesture capability assessment so GestureProcessor
    /// can skip gestures marked "Can't do this" in onboarding.
    /// G2: queued and retried on reconnect so offline assessments are not lost.
    func sendGestureAssessment(disabled: [String]) {
        if state == .connected {
            send(["type": "gesture_assessment", "disabled": disabled])
            _pendingGestureAssessment = nil
        } else {
            _pendingGestureAssessment = disabled   // last-write-wins
        }
    }

    /// G2: Flush pending gesture assessment on reconnect.
    private func _flushPendingGestureAssessment() {
        guard let pending = _pendingGestureAssessment else { return }
        send(["type": "gesture_assessment", "disabled": pending])
        _pendingGestureAssessment = nil
    }

    /// Mute or unmute the PC microphone. The PC echoes a `mic_state` push that
    /// updates `micMuted`; we also set it optimistically for instant UI feedback.
    /// Unlike voice ("mute mic" is one-way), this works in both directions — it
    /// is the only way to UNmute once the mic is deaf.
    func sendMicMute(_ muted: Bool) {
        micMuted = muted
        send(["type": "mic_mute", "muted": muted])
        commandFeed.send(muted ? "Mic muted" : "Mic unmuted")
    }

    /// Forward a batch of structured log entries to the PC bridge.
    /// Called by AppLogger on a 500ms timer; no ack expected.
    func sendLogBatch(_ entries: [[String: Any]]) {
        guard state == .connected, !entries.isEmpty else { return }
        send(["type": "ipad_log", "entries": entries])
    }
}
