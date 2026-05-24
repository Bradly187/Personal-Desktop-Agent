import Foundation
import Network
import Combine
import QuartzCore

// MARK: — Recalibration request (PC → iPad)

struct RecalibrationRequest {
    let reason: String          // "voice_clarity" | "seasonal"
    let degradationPct: Double  // 0–100
}

// MARK: — Gaze monitor calibration events (PC → iPad)

enum GazeCalibrationEvent {
    case next(dotIndex: Int, pxX: Int, pxY: Int, total: Int, label: String)
    case complete(residualPx: Double, success: Bool, message: String?)
    case error(String)
    case cancelled
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

    /// Feed of voice calibration events from the PC (phrase prompts, results, completion).
    let calibrationEventPublisher = PassthroughSubject<CalibrationEvent, Never>()

    /// Feed of gaze monitor calibration events from the PC (next dot, complete, error).
    let gazeCalibrationFeed = PassthroughSubject<GazeCalibrationEvent, Never>()

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
    // Sensor streams (tilt, gaze_delta, head) are sampled, not queued: only the
    // latest value matters. Non-sensor messages always pass through.
    private var _sendQueueDepth: Int = 0
    private let _maxSensorQueueDepth = 15
    // M3: audio_stream gets a higher threshold (~3s of 50ms chunks) because
    // dropping audio mid-utterance corrupts Whisper transcription. We tolerate
    // brief slowness but cap the buildup to prevent unbounded memory growth.
    private let _maxAudioQueueDepth = 60
    private static let _sensorFrameTypes: Set<String> = [
        "tilt", "tilt_position", "gaze_delta", "gaze", "gaze_ray", "head_pose"
    ]

    // G2: Pending gesture assessment — queued when not connected, flushed on reconnect.
    private var _pendingGestureAssessment: [String]? = nil

    // MARK: — Public API

    func connect() {
        guard state == .disconnected else { return }
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
            capturedTask.send(.string(text)) { error in
                if let error {
                    // E5: capture rich error info before triggering reconnect
                    let nsErr = error as NSError
                    AppLogger.shared.warning(
                        "WebSocketManager",
                        "send failed (domain=\(nsErr.domain) code=\(nsErr.code)): \(error.localizedDescription)"
                    )
                    Task { @MainActor [weak self] in
                        self?._handleDisconnect(error: error)
                    }
                }
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

    // MARK: — Connection internals

    /// Determines the WebSocket URL to connect to.
    /// Priority: mDNS discovered endpoint (if no manual override) > manual settings > fallback default.
    private func resolveConnectionURL() -> URL {
        // If mDNS discovered a host and user hasn't manually overridden, use discovered endpoint
        if let discovery = serviceDiscovery,
           !discovery.hasManualOverride,
           let host = discovery.discoveredHost,
           let port = discovery.discoveredPort {
            if let url = URL(string: "ws://\(host):\(port)/ws") {
                return url
            }
        }
        // Fall back to manual settings or default
        return settings?.wsURLOrDefault ?? URL(string: "ws://192.168.18.2:8765/ws")!
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
                    self.state = .connected
                    self.reconnectAttempt = 0
                    self._startPingTimer()
                    self._flushPendingGestureAssessment()  // G2
                }
                self._handleReceived(message: firstMessage)
                try await self._receiveLoop(task: wsTask)
            } catch {
                AppLogger.shared.error("WebSocketManager", "Connection error: \(error.localizedDescription) — URL: \(url)")
                await MainActor.run {
                    self.connectionTimeoutTask?.cancel()
                    self.connectionTimeoutTask = nil
                    self._handleDisconnect(error: error)
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
            parsed = .status(
                activeWindow: json["active_window"] as? String,
                cursorX: cursor["x"] ?? 0,
                cursorY: cursor["y"] ?? 0
            )
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

        case "gaze_calibration_next":
            let dotIndex = json["dot_index"] as? Int    ?? 0
            let pxX      = json["px_x"]      as? Int    ?? 0
            let pxY      = json["px_y"]      as? Int    ?? 0
            let total    = json["total"]     as? Int    ?? 5
            let label    = json["label"]     as? String ?? ""
            gazeCalibrationFeed.send(.next(dotIndex: dotIndex, pxX: pxX, pxY: pxY,
                                           total: total, label: label))
            parsed = .unknown(type: type, raw: json)

        case "gaze_calibration_complete":
            let residual = json["residual_px"] as? Double ?? 0.0
            let success  = json["success"]     as? Bool   ?? false
            let message  = json["message"]     as? String
            gazeCalibrationFeed.send(.complete(residualPx: residual,
                                               success: success, message: message))
            parsed = .unknown(type: type, raw: json)

        case "gaze_calibration_error":
            let msg = json["message"] as? String ?? "Unknown error"
            gazeCalibrationFeed.send(.error(msg))
            parsed = .unknown(type: type, raw: json)

        case "gaze_calibration_cancelled":
            gazeCalibrationFeed.send(.cancelled)
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

    func sendGaze(x: Double, y: Double, confidence: Double) {
        send(["type": "gaze", "x": x, "y": y, "confidence": confidence])
    }

    func sendGazeDelta(dx: Double, dy: Double, confidence: Double = 1.0, saccade: Bool = false) {
        send(["type": "gaze_delta", "dx": dx, "dy": dy, "conf": confidence, "saccade": saccade])
    }

    /// Send world-space gaze ray direction at ~10 Hz for monitor calibration.
    /// Called from GazeTracker.processQueue — WebSocketManager.send() is queue-safe.
    func sendGazeRay(dx: Double, dy: Double, dz: Double, confidence: Double = 1.0) {
        send(["type": "gaze_ray", "dx": dx, "dy": dy, "dz": dz, "conf": confidence])
    }

    func sendGazeDwell(x: Double, y: Double, actionType: DwellActionType) {
        msgCounter += 1
        send(["type": "gaze_dwell", "id": "gd-\(msgCounter)", "x": x, "y": y, "action_type": actionType.rawValue])
    }

    func sendHeadPose(pitch: Double, yaw: Double) {
        send(["type": "head_pose", "pitch": pitch, "yaw": yaw])
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

    func sendSoundAction(sound: String, confidence: Double) {
        msgCounter += 1
        send(["type": "sound_action", "id": "sa-\(msgCounter)", "sound": sound, "confidence": confidence])
        commandFeed.send("Sound: \(sound)")
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

    /// Forward a batch of structured log entries to the PC bridge.
    /// Called by AppLogger on a 500ms timer; no ack expected.
    func sendLogBatch(_ entries: [[String: Any]]) {
        guard state == .connected, !entries.isEmpty else { return }
        send(["type": "ipad_log", "entries": entries])
    }

    func sendDepthFrame(width: Int, height: Int, depthB64: String, confB64: String, ts: Double) {
        send([
            "type": "depth_frame",
            "ts": ts,
            "width": width,
            "height": height,
            "depth_b64": depthB64,
            "conf_b64": confB64,
        ])
    }

    func sendCameraFrame(width: Int, height: Int, imageB64: String, ts: Double) {
        send([
            "type": "camera_frame",
            "ts": ts,
            "width": width,
            "height": height,
            "image_b64": imageB64,
        ])
    }

    /// Trigger a 5-dot monitor calibration session on the PC.
    func sendGazeCalibrationStart() {
        msgCounter += 1
        send(["type": "gaze_calibration_start", "id": "gcal-\(msgCounter)"])
    }

    /// Send a captured calibration sample for one dot to the PC.
    /// pxX/pxY are the known pixel coords the PC displayed; ray is the gaze world vector.
    func sendGazeCalibrationSample(dotIndex: Int, pxX: Int, pxY: Int,
                                   rayDx: Double, rayDy: Double, rayDz: Double) {
        msgCounter += 1
        send([
            "type":      "gaze_calibration_sample",
            "id":        "gs-\(msgCounter)",
            "dot_index": dotIndex,
            "px_x":      pxX,
            "px_y":      pxY,
            "ray_dx":    rayDx,
            "ray_dy":    rayDy,
            "ray_dz":    rayDz,
        ])
    }
}
