import Speech
import AVFoundation
import Foundation

/// On-device continuous speech recognition.
/// Matches detected phrases against the keyword list and sends `keyword` messages.
/// PCM audio that does not match a keyword is NOT streamed (Whisper transcription
/// happens on the PC side via the audio_stream pipeline — future task).
@MainActor
final class KeywordListener: NSObject, ObservableObject {

    private let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var recognitionTask: SFSpeechRecognitionTask?
    private let engine = AVAudioEngine()

    private weak var ws: WebSocketManager?
    private var settings: SettingsStore?

    @Published var isListening = false
    @Published var lastKeyword: String?

    init(ws: WebSocketManager, settings: SettingsStore) {
        self.ws = ws
        self.settings = settings
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
        engine.stop()
        recognitionTask?.cancel()
        recognitionTask = nil
        request = nil
        isListening = false
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

        do {
            let inputNode = engine.inputNode
            let format = inputNode.outputFormat(forBus: 0)
            inputNode.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
                self?.request?.append(buffer)
            }
            try engine.start()
            isListening = true
        } catch {
            print("KeywordListener: engine start failed: \(error)")
            return
        }

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
