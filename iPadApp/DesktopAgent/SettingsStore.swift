import Foundation
import Combine
import CoreGraphics

// MARK: — Toolbar Position

/// Supported positions for the dwell action toolbar.
/// Persisted to UserDefaults; defaults to `.bottom` on fresh install.
enum ToolbarPosition: String, Codable, CaseIterable {
    case top = "top"
    case bottom = "bottom"
    case floating = "floating"
}

// MARK: — Dwell Action Types

/// Supported dwell action types. Raw values match the WebSocket protocol strings.
enum DwellActionType: String, Codable, CaseIterable {
    case leftClick = "left_click"
    case rightClick = "right_click"
    case doubleClick = "double_click"
    case dragStart = "drag_start"
    case dragEnd = "drag_end"
}

/// Persists all user-configurable sensor preferences to UserDefaults.
/// Fix #23: @MainActor ensures all @Published mutations happen on the main thread.
@MainActor
final class SettingsStore: ObservableObject {

    // MARK: — Connection
    @Published var serverHost: String {
        didSet { defaults.set(serverHost, forKey: "serverHost") }
    }
    @Published var serverPort: Int {
        didSet { defaults.set(serverPort, forKey: "serverPort") }
    }

    /// Returns a valid WebSocket URL or nil if host/port are invalid.
    /// Never force-unwraps user input.
    var wsURL: URL? {
        let trimmedHost = serverHost.trimmingCharacters(in: .whitespaces)
        guard !trimmedHost.isEmpty,
              serverPort > 0, serverPort <= 65535 else {
            return nil
        }
        return URL(string: "ws://\(trimmedHost):\(serverPort)/ws")
    }

    /// Always returns a valid URL — falls back to a compile-time safe literal
    /// when user input is invalid.
    var wsURLOrDefault: URL {
        wsURL ?? URL(string: "ws://192.168.18.2:8765/ws")!
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
    @Published var tiltInverted: Bool {
        didSet { defaults.set(tiltInverted, forKey: "tiltInverted") }
    }

    /// Maximum angular displacement (degrees) from neutral that maps to screen edge.
    /// Clamped to [5, 60]. Default 25°.
    @Published var tiltRange: Double {
        didSet {
            let clamped = max(5.0, min(60.0, tiltRange))
            if clamped != tiltRange { tiltRange = clamped }
            defaults.set(clamped, forKey: "tiltRange")
        }
    }

    /// When true, tilt uses position-mapped mode (absolute screen coordinates).
    /// When false, uses legacy velocity-based mode (rotation-rate deltas).
    @Published var tiltPositionMode: Bool {
        didSet { defaults.set(tiltPositionMode, forKey: "tiltPositionMode") }
    }
    /// Tilt dwell-to-click: hold the cursor still to fire the active dwell
    /// action. Opt-in (off by default) to avoid accidental "Midas touch" clicks.
    @Published var tiltDwellClickEnabled: Bool {
        didSet { defaults.set(tiltDwellClickEnabled, forKey: "tiltDwellClickEnabled") }
    }
    /// Seconds the cursor must be held still before a dwell click fires.
    @Published var tiltDwellDuration: Double {
        didSet { defaults.set(tiltDwellDuration, forKey: "tiltDwellDuration") }
    }

    /// Accelerometer tap-to-click threshold (g-force delta). LOWER = a lighter
    /// tap registers (more sensitive). The Settings "Tap Sensitivity" slider maps
    /// to this inversely. Clamped to [0.4, 3.0]; default 0.6. The 0.4 floor sits
    /// just above the ~0.3 g accel spikes that tilting-to-move-the-cursor produces,
    /// so lighter taps register without cursor motion firing false clicks.
    @Published var tapThreshold: Double {
        didSet {
            let clamped = max(0.4, min(3.0, tapThreshold))
            if clamped != tapThreshold { tapThreshold = clamped }
            defaults.set(clamped, forKey: "tapThreshold")
        }
    }

    // MARK: — Neutral Gravity Persistence

    /// Persisted neutral gravity X component. Used to restore calibration across app restarts.
    var neutralGravityX: Double {
        get { defaults.double(forKey: "neutralGravityX") }
        set { defaults.set(newValue, forKey: "neutralGravityX") }
    }

    /// Persisted neutral gravity Y component.
    var neutralGravityY: Double {
        get { defaults.double(forKey: "neutralGravityY") }
        set { defaults.set(newValue, forKey: "neutralGravityY") }
    }

    /// Persisted neutral gravity Z component.
    var neutralGravityZ: Double {
        get { defaults.double(forKey: "neutralGravityZ") }
        set { defaults.set(newValue, forKey: "neutralGravityZ") }
    }

    /// True when a neutral gravity calibration has been persisted (key exists in UserDefaults).
    var hasPersistedNeutral: Bool {
        defaults.object(forKey: "neutralGravityX") != nil
    }

    // MARK: — Dwell
    @Published var dwellTimeout: Double {
        didSet { defaults.set(dwellTimeout, forKey: "dwellTimeout") }
    }

    /// The currently active dwell action type. Persisted to UserDefaults.
    @Published var activeDwellAction: DwellActionType {
        didSet { defaults.set(activeDwellAction.rawValue, forKey: "activeDwellAction") }
    }

    /// When true, non-default actions (rightClick, doubleClick) reset to leftClick after one fire.
    @Published var oneShotEnabled: Bool {
        didSet { defaults.set(oneShotEnabled, forKey: "oneShotEnabled") }
    }

    /// Transient drag state — true between drag-start and drag-end. Not persisted.
    @Published var isDragging: Bool = false

    // MARK: — Toolbar Position

    /// The user's selected toolbar position. Persisted to UserDefaults.
    /// Defaults to `.bottom` on fresh install (Requirement 6.8).
    @Published var toolbarPosition: ToolbarPosition {
        didSet { defaults.set(toolbarPosition.rawValue, forKey: "toolbarPosition") }
    }

    /// Floating toolbar offset from center, persisted so the toolbar stays where the user left it.
    @Published var toolbarFloatingOffset: CGSize {
        didSet {
            defaults.set(Double(toolbarFloatingOffset.width), forKey: "toolbarFloatingOffsetX")
            defaults.set(Double(toolbarFloatingOffset.height), forKey: "toolbarFloatingOffsetY")
        }
    }

    // MARK: — Dwell Action State Transitions

    /// Applies one-shot reset after a non-default dwell action fires.
    /// When `oneShotEnabled` is true and the fired action is rightClick or doubleClick,
    /// resets `activeDwellAction` to `.leftClick`.
    ///
    /// - Returns: `true` if a reset occurred, `false` otherwise.
    @discardableResult
    func applyOneShotResetIfNeeded() -> Bool {
        guard oneShotEnabled else { return false }

        switch activeDwellAction {
        case .rightClick, .doubleClick:
            // Setting the property triggers DwellActionSyncer for WebSocket sync.
            activeDwellAction = .leftClick
            return true
        case .leftClick, .dragStart, .dragEnd:
            // One-shot does not apply to leftClick (already default) or drag (has its own state machine)
            return false
        }
    }

    // MARK: — Drag State Machine

    /// Applies dwell action state transitions after a dwell fires.
    /// - dragStart fires → auto-transition to dragEnd, isDragging = true
    /// - dragEnd fires → reset to leftClick, isDragging = false
    /// Setting `activeDwellAction` triggers DwellActionSyncer for WebSocket sync.
    func applyDwellTransition(firedAction: DwellActionType) {
        switch firedAction {
        case .dragStart:
            activeDwellAction = .dragEnd
            isDragging = true

        case .dragEnd:
            activeDwellAction = .leftClick
            isDragging = false

        default:
            break
        }
    }

    // MARK: — Cursor Sensor Selection
    /// Which cursor-driving sensor is currently active. Currently always "tilt".
    @Published var activeCursorSensor: String {
        didSet { defaults.set(activeCursorSensor, forKey: "activeCursorSensor") }
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

    // MARK: — Flare / Accessibility Profile (Sprint B)
    @Published var flareVoiceDegrades: Bool {
        didSet { defaults.set(flareVoiceDegrades, forKey: "flareVoiceDegrades") }
    }
    @Published var flareGestureDegrades: Bool {
        didSet { defaults.set(flareGestureDegrades, forKey: "flareGestureDegrades") }
    }
    @Published var flareTiltDegrades: Bool {
        didSet { defaults.set(flareTiltDegrades, forKey: "flareTiltDegrades") }
    }
    /// Mouth-sound triggers get weaker/slower on flare days → relax the
    /// sound cooldown. Independent of tilt.
    @Published var flareSoundDegrades: Bool {
        didSet { defaults.set(flareSoundDegrades, forKey: "flareSoundDegrades") }
    }
    /// Fraction of baseline voice volume on flare days (0.25–0.75).
    @Published var flareVadScale: Double {
        didSet { defaults.set(flareVadScale, forKey: "flareVadScale") }
    }
    /// User-initiated flare day override — synced to PC via WebSocket.
    @Published var manualPainDay: Bool {
        didSet {
            defaults.set(manualPainDay, forKey: "manualPainDay")
        }
    }
    /// Gesture capability assessment results: gesture name → "Easy" / "Hard on bad days" / "Can't do this"
    @Published var gestureAssessment: [String: String] {
        didSet {
            if let data = try? JSONEncoder().encode(gestureAssessment) {
                defaults.set(data, forKey: "gestureAssessment")
            }
        }
    }
    /// Gesture names the user has explicitly disabled.
    @Published var disabledGestures: Set<String> {
        didSet {
            if let data = try? JSONEncoder().encode(Array(disabledGestures)) {
                defaults.set(data, forKey: "disabledGestures")
            }
        }
    }
    /// Date of last voice calibration — used for seasonal re-cal prompts.
    @Published var lastCalibrationDate: Date? {
        didSet { defaults.set(lastCalibrationDate, forKey: "lastCalibrationDate") }
    }

    // MARK: — Feature Toggles
    @Published var edgeScrollEnabled: Bool {
        didSet { defaults.set(edgeScrollEnabled, forKey: "edgeScrollEnabled") }
    }

    // MARK: — Voice condition (G5: persist last-declared condition so it survives reconnects)
    /// Last condition the user declared: "good_day" | "flare_day" | "allergy_day" | "svt_attack"
    @Published var voiceCondition: String {
        didSet { defaults.set(voiceCondition, forKey: "voiceCondition") }
    }

    // MARK: — Audio Streaming (iPad mic → PC Whisper)
    @Published var audioStreamEnabled: Bool {
        didSet { defaults.set(audioStreamEnabled, forKey: "audioStreamEnabled") }
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

    private let defaults: UserDefaults

    /// Creates a SettingsStore backed by the given UserDefaults instance.
    /// Defaults to `.standard` for production use. Pass a custom suite for test isolation.
    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        serverHost = defaults.string(forKey: "serverHost") ?? "192.168.18.2"
        serverPort = defaults.integer(forKey: "serverPort").nonZero ?? 8765
        tiltSensitivity = defaults.double(forKey: "tiltSensitivity").nonZero ?? 1.0
        tiltDeadZone = defaults.double(forKey: "tiltDeadZone").nonZero ?? 1.5
        tiltEnabled = defaults.object(forKey: "tiltEnabled") as? Bool ?? true
        tiltInverted = defaults.object(forKey: "tiltInverted") as? Bool ?? false

        // Position-mapping settings
        let savedRange = defaults.double(forKey: "tiltRange")
        tiltRange = savedRange > 0 ? max(5.0, min(60.0, savedRange)) : 25.0
        tiltPositionMode = defaults.object(forKey: "tiltPositionMode") as? Bool ?? true
        tiltDwellClickEnabled = defaults.object(forKey: "tiltDwellClickEnabled") as? Bool ?? false
        tiltDwellDuration = defaults.double(forKey: "tiltDwellDuration").nonZero ?? 1.0
        let savedTap = defaults.double(forKey: "tapThreshold")
        tapThreshold = savedTap > 0 ? max(0.4, min(3.0, savedTap)) : 0.6
        dwellTimeout = defaults.double(forKey: "dwellTimeout").nonZero ?? 1.0

        if let savedAction = defaults.string(forKey: "activeDwellAction"),
           let action = DwellActionType(rawValue: savedAction) {
            activeDwellAction = action
        } else {
            activeDwellAction = .leftClick
        }
        oneShotEnabled = defaults.object(forKey: "oneShotEnabled") as? Bool ?? false

        // Toolbar position — default to .bottom on fresh install (Requirement 6.8)
        if let savedPosition = defaults.string(forKey: "toolbarPosition"),
           let position = ToolbarPosition(rawValue: savedPosition) {
            toolbarPosition = position
        } else {
            toolbarPosition = .bottom
        }

        // Floating offset — default to center (zero offset)
        let offsetX = defaults.double(forKey: "toolbarFloatingOffsetX")
        let offsetY = defaults.double(forKey: "toolbarFloatingOffsetY")
        toolbarFloatingOffset = CGSize(width: offsetX, height: offsetY)

        activeCursorSensor = defaults.string(forKey: "activeCursorSensor") ?? "tilt"
        trackpadSpeed = defaults.double(forKey: "trackpadSpeed").nonZero ?? 2.0
        palmRejectRadius = defaults.double(forKey: "palmRejectRadius").nonZero ?? 25.0
        // Empty default: KeywordListener is off until the user adds words in Settings.
        // WhisperStream handles all voice input more naturally — keywords are opt-in.
        keywordList = defaults.stringArray(forKey: "keywordList") ?? []
        flareVoiceDegrades  = defaults.object(forKey: "flareVoiceDegrades")  as? Bool ?? true
        flareGestureDegrades = defaults.object(forKey: "flareGestureDegrades") as? Bool ?? false
        flareTiltDegrades   = defaults.object(forKey: "flareTiltDegrades")   as? Bool ?? false
        flareSoundDegrades  = defaults.object(forKey: "flareSoundDegrades")  as? Bool ?? true
        flareVadScale       = defaults.double(forKey: "flareVadScale").nonZero ?? 0.5
        manualPainDay       = defaults.object(forKey: "manualPainDay")       as? Bool ?? false
        if let data = defaults.data(forKey: "gestureAssessment"),
           let decoded = try? JSONDecoder().decode([String: String].self, from: data) {
            gestureAssessment = decoded
        } else {
            gestureAssessment = [:]
        }
        if let data = defaults.data(forKey: "disabledGestures"),
           let decoded = try? JSONDecoder().decode([String].self, from: data) {
            disabledGestures = Set(decoded)
        } else {
            disabledGestures = []
        }
        lastCalibrationDate = defaults.object(forKey: "lastCalibrationDate") as? Date
        edgeScrollEnabled = defaults.object(forKey: "edgeScrollEnabled") as? Bool ?? true

        voiceCondition = defaults.string(forKey: "voiceCondition") ?? "good_day"
        audioStreamEnabled = defaults.object(forKey: "audioStreamEnabled") as? Bool ?? false

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

// MARK: — CommandButton export/import (G7: survive device reset without iCloud entitlement)

extension SettingsStore {

    /// Export command buttons as JSON to the app's Documents directory.
    /// Returns the file URL on success so the caller can present a share sheet.
    @discardableResult
    func exportCommandButtons() -> URL? {
        guard let data = try? JSONEncoder().encode(commandButtons) else { return nil }
        let url = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("command_buttons.json")
        try? data.write(to: url, options: .atomic)
        return url
    }

    /// Import command buttons from a JSON file URL. Returns true on success.
    @discardableResult
    func importCommandButtons(from url: URL) -> Bool {
        guard url.startAccessingSecurityScopedResource() else { return false }
        defer { url.stopAccessingSecurityScopedResource() }
        guard let data = try? Data(contentsOf: url),
              let buttons = try? JSONDecoder().decode([CommandButton].self, from: data)
        else { return false }
        commandButtons = buttons
        return true
    }
}

private extension Int {
    var nonZero: Int? { self == 0 ? nil : self }
}
private extension Double {
    var nonZero: Double? { self == 0.0 ? nil : self }
}
