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
    /// Guards against the several terminal callbacks SFSpeechRecognitionTask can
    /// deliver per cycle (isFinal + error) scheduling more than one restart.
    private var restartScheduled = false

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
        restartScheduled = false

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
                    // Dedup the multiple terminal callbacks (isFinal + error) that
                    // a single recognition cycle can deliver.
                    guard !self.restartScheduled else { return }
                    self.restartScheduled = true

                    // "No speech detected" is the normal end of a silent cycle, not a
                    // failure. Log it at debug and restart quietly so silence doesn't
                    // spam ERROR or churn the backoff machinery.
                    let benign = Self.isBenignNoSpeech(error)
                    if let error, !benign {
                        AppLogger.shared.error("KeywordListener",
                            "Recognition ended with error: \(error.localizedDescription)")
                    } else if benign {
                        AppLogger.shared.debug("KeywordListener",
                            "Recognition cycle ended (no speech) — restarting quietly")
                    }
                    self.stop()
                    self._restartWithBackoff(benign: benign)
                }
            }
        }
    }

    /// SFSpeechRecognizer reports "no speech detected" (and a couple of related
    /// no-input codes) at the end of a silent cycle, and a cancellation when we
    /// tear the task down ourselves. These are expected, not failures — we restart
    /// quietly instead of logging an error and escalating the backoff.
    private static func isBenignNoSpeech(_ error: Error?) -> Bool {
        guard let error else { return true }   // clean isFinal end → benign
        let ns = error as NSError
        // kAFAssistantErrorDomain: 1110 = no speech; 203 = retry; 216/301 = cancelled.
        if ns.domain == "kAFAssistantErrorDomain"
            && (ns.code == 1110 || ns.code == 203 || ns.code == 216 || ns.code == 301) {
            return true
        }
        // Fallback on the localized text in case the domain/code shifts across iOS versions.
        return error.localizedDescription.lowercased().contains("no speech")
    }

    /// Restart with exponential backoff to prevent infinite loop on persistent errors.
    /// Resets counter only after 10s of stable operation, so the delay actually escalates.
    /// `benign` cycles (normal end-of-silence) skip the backoff machinery entirely and
    /// just relisten after a short quiet delay.
    private func _restartWithBackoff(benign: Bool) {
        if benign {
            // Not a failure — relisten after a short delay so a silent room doesn't
            // spin a tight restart loop, without touching the failure counter.
            Task { @MainActor [weak self] in
                try? await Task.sleep(nanoseconds: UInt64(1.0 * 1_000_000_000))
                self?._startRecognition()
            }
            return
        }

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
