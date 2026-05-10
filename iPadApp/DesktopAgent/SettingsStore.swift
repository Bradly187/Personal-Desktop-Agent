import Foundation
import Combine

/// Persists all user-configurable sensor preferences to UserDefaults.
final class SettingsStore: ObservableObject {

    // MARK: — Connection
    @Published var serverHost: String {
        didSet { defaults.set(serverHost, forKey: "serverHost") }
    }
    @Published var serverPort: Int {
        didSet { defaults.set(serverPort, forKey: "serverPort") }
    }

    var wsURL: URL {
        URL(string: "ws://\(serverHost):\(serverPort)/ws")!
    }

    // MARK: — Tilt
    @Published var tiltSensitivity: Double {
        didSet { defaults.set(tiltSensitivity, forKey: "tiltSensitivity") }
    }
    @Published var tiltDeadZone: Double {
        didSet { defaults.set(tiltDeadZone, forKey: "tiltDeadZone") }
    }
    @Published var tiltEnabled: Bool {
        didSet { defaults.set(tiltEnabled, forKey: "tiltEnabled") }
    }

    // MARK: — Gaze / Dwell
    @Published var gazeEnabled: Bool {
        didSet { defaults.set(gazeEnabled, forKey: "gazeEnabled") }
    }
    @Published var dwellTimeout: Double {
        didSet { defaults.set(dwellTimeout, forKey: "dwellTimeout") }
    }

    // MARK: — Head tracking
    @Published var headEnabled: Bool {
        didSet { defaults.set(headEnabled, forKey: "headEnabled") }
    }
    @Published var headSmoothingFactor: Double {
        didSet { defaults.set(headSmoothingFactor, forKey: "headSmoothingFactor") }
    }

    // MARK: — Trackpad
    @Published var trackpadSpeed: Double {
        didSet { defaults.set(trackpadSpeed, forKey: "trackpadSpeed") }
    }
    @Published var palmRejectRadius: Double {
        didSet { defaults.set(palmRejectRadius, forKey: "palmRejectRadius") }
    }

    // MARK: — Voice / Keywords
    @Published var keywordList: [String] {
        didSet { defaults.set(keywordList, forKey: "keywordList") }
    }

    // MARK: — Sound mappings  {"cluck": "CLICK", "pop": "SCROLL down", ...}
    @Published var soundMappings: [String: String] {
        didSet {
            if let data = try? JSONEncoder().encode(soundMappings) {
                defaults.set(data, forKey: "soundMappings")
            }
        }
    }

    // MARK: — CommandPad buttons  [{label, action, params}]
    @Published var commandButtons: [CommandButton] {
        didSet {
            if let data = try? JSONEncoder().encode(commandButtons) {
                defaults.set(data, forKey: "commandButtons")
            }
        }
    }

    // MARK: — Init

    private let defaults = UserDefaults.standard

    init() {
        serverHost = defaults.string(forKey: "serverHost") ?? "192.168.1.100"
        serverPort = defaults.integer(forKey: "serverPort").nonZero ?? 8765
        tiltSensitivity = defaults.double(forKey: "tiltSensitivity").nonZero ?? 1.0
        tiltDeadZone = defaults.double(forKey: "tiltDeadZone").nonZero ?? 0.02
        tiltEnabled = defaults.object(forKey: "tiltEnabled") as? Bool ?? true
        gazeEnabled = defaults.object(forKey: "gazeEnabled") as? Bool ?? true
        dwellTimeout = defaults.double(forKey: "dwellTimeout").nonZero ?? 1.0
        headEnabled = defaults.object(forKey: "headEnabled") as? Bool ?? false
        headSmoothingFactor = defaults.double(forKey: "headSmoothingFactor").nonZero ?? 0.3
        trackpadSpeed = defaults.double(forKey: "trackpadSpeed").nonZero ?? 1.0
        palmRejectRadius = defaults.double(forKey: "palmRejectRadius").nonZero ?? 25.0
        keywordList = defaults.stringArray(forKey: "keywordList") ?? ["click", "scroll", "open"]

        if let data = defaults.data(forKey: "soundMappings"),
           let decoded = try? JSONDecoder().decode([String: String].self, from: data) {
            soundMappings = decoded
        } else {
            soundMappings = ["cluck": "CLICK", "pop": "SCROLL down", "hiss": "SCROLL up"]
        }

        if let data = defaults.data(forKey: "commandButtons"),
           let decoded = try? JSONDecoder().decode([CommandButton].self, from: data) {
            commandButtons = decoded
        } else {
            commandButtons = CommandButton.defaults
        }
    }
}

// MARK: — Supporting types

struct CommandButton: Identifiable, Codable {
    let id: UUID
    var label: String
    var action: String          // CLICK | SCROLL | TYPE | OPEN | CLOSE | HOTKEY | DICTATE | SCREENSHOT
    var params: [String: String]

    init(id: UUID = UUID(), label: String, action: String, params: [String: String] = [:]) {
        self.id = id
        self.label = label
        self.action = action
        self.params = params
    }

    static let defaults: [CommandButton] = [
        CommandButton(label: "Click", action: "CLICK"),
        CommandButton(label: "Right Click", action: "CLICK", params: ["button": "right"]),
        CommandButton(label: "Scroll ↓", action: "SCROLL", params: ["direction": "down"]),
        CommandButton(label: "Scroll ↑", action: "SCROLL", params: ["direction": "up"]),
        CommandButton(label: "Copy", action: "HOTKEY", params: ["keys": "ctrl,c"]),
        CommandButton(label: "Paste", action: "HOTKEY", params: ["keys": "ctrl,v"]),
        CommandButton(label: "Undo", action: "HOTKEY", params: ["keys": "ctrl,z"]),
        CommandButton(label: "Screenshot", action: "SCREENSHOT"),
        CommandButton(label: "Tab", action: "HOTKEY", params: ["keys": "tab"]),
        CommandButton(label: "Enter", action: "HOTKEY", params: ["keys": "enter"]),
        CommandButton(label: "Escape", action: "HOTKEY", params: ["keys": "escape"]),
        CommandButton(label: "Space", action: "HOTKEY", params: ["keys": "space"]),
    ]
}

private extension Int {
    var nonZero: Int? { self == 0 ? nil : self }
}
private extension Double {
    var nonZero: Double? { self == 0.0 ? nil : self }
}
