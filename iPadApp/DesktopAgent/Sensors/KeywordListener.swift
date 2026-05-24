import Speech
import AVFoundation
import Foundation

/// On-device continuous speech recognition.
/// Matches detected phrases against the keyword list and sends `keyword` messages.
/// PCM audio that does not match a keyword is NOT streamed (Whisper transcription
/// happens on the PC side via the audio_stream pipeline — future task).
///
/// Uses SharedAudioSession for audio input — does NOT own its own AVAudioEngine.
@MainActor
final class KeywordListener: NSObject, ObservableObject {

    private let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var recognitionTask: SFSpeechRecognitionTask?

    private let sharedAudioSession: SharedAudioSession
    private weak var ws: WebSocketManager?
    private var settings: SettingsStore?

    private static let consumerID = "KeywordListener"

    @Published var isListening = false
    @Published var lastKeyword: String?

    /// Tracks the length of transcript already scanned to avoid re-firing on partial results
    private var lastScannedLength: Int = 0
    /// Per-keyword cooldown to prevent rapid re-fires
    private var keywordCooldowns: [String: Date] = [:]
    private let keywordCooldownDuration: TimeInterval = 0.5

    /// Fix #18: Backoff for restart loop prevention
    private var consecutiveRestarts: Int = 0
    private let maxConsecutiveRestarts = 5
    private var lastRestartTime: Date?

    init(ws: WebSocketManager, settings: SettingsStore, sharedAudioSession: SharedAudioSession) {
        self.ws = ws
        self.settings = settings
        self.sharedAudioSession = sharedAudioSession
        super.init()
    }

    // MARK: — Lifecycle

    func start() {
        SFSpeechRecognizer.requestAuthorization { [weak self] status in
            Task { @MainActor [weak self] in
                guard status == .authorized else {
                    AppLogger.shared.error("KeywordListener", "Speech recognition not authorized by user")
                    return
                }
                self?._startRecognition()
            }
        }
    }

    func stop() {
        recognitionTask?.cancel()
        recognitionTask = nil
        request = nil
        isListening = false
        sharedAudioSession.removeConsumer(Self.consumerID)
    }

    // MARK: — Recognition

    private func _startRecognition() {
        guard let recognizer, recognizer.isAvailable else {
            AppLogger.shared.warning("KeywordListener", "Recognizer unavailable")
            return
        }

        request = SFSpeechAudioBufferRecognitionRequest()
        guard let request else { return }
        request.shouldReportPartialResults = true
        request.requiresOnDeviceRecognition = true   // force on-device for speed + privacy

        lastScannedLength = 0

        // Register with shared audio session and receive buffers via fan-out
        sharedAudioSession.addConsumer(Self.consumerID) { [weak self] buffer, _ in
            self?.request?.append(buffer)
        }

        isListening = true

        recognitionTask = recognizer.recognitionTask(with: request) { [weak self] result, error in
            Task { @MainActor [weak self] in
                guard let self else { return }
                if let result {
                    self._checkKeywords(in: result.bestTranscription.formattedString)
                }
                if error != nil || (result?.isFinal ?? false) {
                    if let error {
                        AppLogger.shared.error("KeywordListener",
                            "Recognition ended with error: \(error.localizedDescription)")
                    }
                    self.stop()
                    self._restartWithBackoff()
                }
            }
        }
    }

    /// Restart with exponential backoff to prevent infinite loop on persistent errors.
    /// Resets counter only after 10s of stable operation, so the delay actually escalates.
    private func _restartWithBackoff() {
        let now = Date()

        // Reset counter only when last restart was >10s ago (stable operation window)
        if let last = lastRestartTime, now.timeIntervalSince(last) > 10.0 {
            consecutiveRestarts = 0
        }

        consecutiveRestarts += 1
        lastRestartTime = now

        if consecutiveRestarts > maxConsecutiveRestarts {
            // De-duplicate: SFSpeechRecognitionTask can deliver several terminal
            // callbacks in rapid succession (isFinal + error). Only arm one backoff
            // Task per cycle.
            guard !isBackingOff else { return }
            isBackingOff = true

            // Exponential backoff: 5 → 10 → 20 → 40 → 60s cap
            let exponent = consecutiveRestarts - maxConsecutiveRestarts - 1
            let delay = min(60.0, 5.0 * pow(2.0, Double(max(0, exponent))))
            AppLogger.shared.warning("KeywordListener",
                "Too many consecutive restarts (\(consecutiveRestarts)), backing off \(Int(delay))s")

            Task { @MainActor [weak self] in
                try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
                guard let self else { return }
                self.isBackingOff = false
                // Do NOT reset consecutiveRestarts here — only the 10s stable check
                // above should reset it, so delays continue escalating on a broken mic.
                self._startRecognition()
            }
            return
        }

        _startRecognition()
    }

    /// True while a backoff Task is pending — prevents duplicate log spam and
    /// duplicate restart attempts when several handler callbacks fire at once.
    private var isBackingOff = false

    private func _checkKeywords(in transcript: String) {
        guard let settings else { return }

        // Only scan new content since last check to avoid re-firing
        let lower = transcript.lowercased()
        guard lower.count > lastScannedLength else { return }

        let newContent = String(lower.dropFirst(lastScannedLength))
        lastScannedLength = lower.count

        let now = Date()
        for keyword in settings.keywordList {
            let kw = keyword.lowercased().trimmingCharacters(in: .whitespaces)
            guard !kw.isEmpty else { continue }
            if newContent.contains(kw) {
                // Check per-keyword cooldown
                if let lastFire = keywordCooldowns[kw],
                   now.timeIntervalSince(lastFire) < keywordCooldownDuration {
                    continue
                }
                keywordCooldowns[kw] = now
                ws?.sendKeyword(word: keyword, confidence: 0.9)
                lastKeyword = keyword
            }
        }
    }
}
