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
                    print("KeywordListener: speech recognition not authorized")
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
            print("KeywordListener: recognizer unavailable")
            return
        }

        request = SFSpeechAudioBufferRecognitionRequest()
        guard let request else { return }
        request.shouldReportPartialResults = true
        request.requiresOnDeviceRecognition = true   // force on-device for speed + privacy

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
                    // Restart recognition loop
                    self.stop()
                    self._startRecognition()
                }
            }
        }
    }

    private func _checkKeywords(in transcript: String) {
        guard let settings else { return }
        let lower = transcript.lowercased()
        for keyword in settings.keywordList {
            if lower.contains(keyword.lowercased()) {
                let conf: Double = 0.9  // SFSpeech on-device doesn't expose confidence
                ws?.sendKeyword(word: keyword, confidence: conf)
                lastKeyword = keyword
                // Reset to avoid repeated fires on the same partial result
                request?.endAudio()
                break
            }
        }
    }
}
