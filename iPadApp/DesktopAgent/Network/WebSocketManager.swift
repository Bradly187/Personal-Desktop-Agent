import Foundation
import Network
import Combine

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
    @Published private(set) var lastMessage: BridgeMessage?

    // Injected at runtime from SettingsStore
    var settings: SettingsStore?

    private var task: URLSessionWebSocketTask?
    private var reconnectAttempt = 0
    private var reconnectWorkItem: DispatchWorkItem?
    private var receiveTask: Task<Void, Never>?

    private let maxBackoffSeconds: Double = 30
    private var msgCounter: Int = 0

    // MARK: — Public API

    func connect() {
        guard state == .disconnected else { return }
        state = .connecting
        _connect()
    }

    func disconnect() {
        reconnectWorkItem?.cancel()
        reconnectWorkItem = nil
        receiveTask?.cancel()
        task?.cancel(with: .normalClosure, reason: nil)
        task = nil
        state = .disconnected
    }

    func send(_ payload: [String: Any]) {
        guard let task, state == .connected else { return }
        guard let data = try? JSONSerialization.data(withJSONObject: payload),
              let text = String(data: data, encoding: .utf8) else { return }
        task.send(.string(text)) { [weak self] error in
            if let error {
                Task { @MainActor [weak self] in
                    self?._handleDisconnect(error: error)
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
        return id
    }

    // MARK: — Connection internals

    private func _connect() {
        let url: URL
        if let settings {
            url = settings.wsURL
        } else {
            url = URL(string: "ws://192.168.1.100:8765/ws")!
        }

        let session = URLSession(configuration: .default)
        let wsTask = session.webSocketTask(with: url)
        self.task = wsTask
        wsTask.resume()

        receiveTask = Task { [weak self] in
            guard let self else { return }
            do {
                // URLSessionWebSocketTask doesn't have a connected callback,
                // so we optimistically mark connected and let errors roll back.
                await MainActor.run {
                    self.state = .connected
                    self.reconnectAttempt = 0
                }
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
    }

    private func _handle(text: String) {
        guard let data = text.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return }

        let type = json["type"] as? String ?? ""
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

        lastMessage = parsed
    }

    private func _handleDisconnect(error: Error?) {
        task = nil
        receiveTask?.cancel()
        receiveTask = nil

        let delay = min(pow(2.0, Double(reconnectAttempt)), maxBackoffSeconds)
        reconnectAttempt += 1
        state = reconnectAttempt == 1 ? .disconnected : .reconnecting(attempt: reconnectAttempt)

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
}

// MARK: — Sensor message helpers

extension WebSocketManager {
    func sendTilt(rx: Double, ry: Double) {
        send(["type": "tilt", "rx": rx, "ry": ry])
    }

    func sendGaze(x: Double, y: Double, confidence: Double) {
        send(["type": "gaze", "x": x, "y": y, "confidence": confidence])
    }

    func sendGazeDwell(x: Double, y: Double) {
        msgCounter += 1
        send(["type": "gaze_dwell", "id": "gd-\(msgCounter)", "x": x, "y": y])
    }

    func sendHeadPose(pitch: Double, yaw: Double) {
        send(["type": "head_pose", "pitch": pitch, "yaw": yaw])
    }

    func sendKeyword(word: String, confidence: Double) {
        msgCounter += 1
        send(["type": "keyword", "id": "kw-\(msgCounter)", "word": word, "confidence": confidence])
    }

    func sendSoundAction(sound: String, confidence: Double) {
        msgCounter += 1
        send(["type": "sound_action", "id": "sa-\(msgCounter)", "sound": sound, "confidence": confidence])
    }

    func sendTrackpadMove(dx: Int, dy: Int) {
        send(["type": "trackpad", "event": "move", "dx": dx, "dy": dy])
    }

    func sendTrackpadTap(button: String = "left") {
        msgCounter += 1
        send(["type": "trackpad", "id": "tp-\(msgCounter)", "event": "tap", "button": button])
    }

    func sendTrackpadScroll(direction: String, clicks: Int = 3) {
        send(["type": "trackpad", "event": "scroll", "direction": direction, "clicks": clicks])
    }

    func sendTiltTap() {
        msgCounter += 1
        send(["type": "tilt_tap", "id": "tt-\(msgCounter)"])
    }

    func sendHandwritingImage(base64PNG: String) {
        msgCounter += 1
        send(["type": "handwriting_image", "id": "hw-\(msgCounter)", "image": base64PNG])
    }

    func sendAudioStream(samplesBase64: String, frames: Int) {
        send(["type": "audio_stream", "samples": samplesBase64, "frames": frames])
    }
}
