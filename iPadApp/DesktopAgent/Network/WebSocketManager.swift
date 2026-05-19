import Foundation
import Network
import Combine
import QuartzCore

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

    // Injected at runtime from SettingsStore
    var settings: SettingsStore?

    // mDNS service discovery — injected at runtime
    var serviceDiscovery: ServiceDiscovery?

    private var task: URLSessionWebSocketTask?
    private var reconnectAttempt = 0
    private var reconnectWorkItem: DispatchWorkItem?
    private var receiveTask: Task<Void, Never>?
    private var pingTask: Task<Void, Never>?

    private let maxBackoffSeconds: Double = 5
    private var msgCounter: Int = 0

    // Fix #9: Serial send queue preserves message ordering for delta-based messages.
    private let sendQueue = DispatchQueue(label: "ws.send.serial", qos: .userInitiated)

    // MARK: — Public API

    func connect() {
        guard state == .disconnected else { return }
        state = .connecting
        _connect()
    }

    func disconnect() {
        reconnectWorkItem?.cancel()
        reconnectWorkItem = nil
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
        // Fix #9: Serialize on a dedicated serial queue to guarantee ordering.
        let capturedTask = task
        sendQueue.async {
            guard let data = try? JSONSerialization.data(withJSONObject: payload),
                  let text = String(data: data, encoding: .utf8) else { return }
            capturedTask.send(.string(text)) { error in
                if let error {
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

        receiveTask = Task { [weak self] in
            guard let self else { return }
            do {
                let firstMessage = try await wsTask.receive()
                await MainActor.run {
                    self.state = .connected
                    self.reconnectAttempt = 0
                    self._startPingTimer()
                }
                self._handleReceived(message: firstMessage)
                try await self._receiveLoop(task: wsTask)
            } catch {
                print("[WebSocketManager] Connection error: \(error.localizedDescription)")
                print("[WebSocketManager] URL was: \(url)")
                await MainActor.run {
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
}
