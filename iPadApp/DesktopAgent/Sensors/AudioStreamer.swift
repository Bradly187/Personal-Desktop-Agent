import AVFoundation
import Foundation

/// Streams raw PCM audio from the iPad microphone to the PC via WebSocket.
/// The PC-side WhisperStream handles VAD and transcription.
///
/// Audio format: 16-bit signed integer PCM, 16 kHz mono.
/// Messages sent as `audio_stream` type with base64-encoded samples.
///
/// Uses SharedAudioSession for audio input — does NOT own its own AVAudioEngine.
/// The audio sensors (KeywordListener, AudioStreamer) share the same engine via
/// the fan-out tap pattern in SharedAudioSession.
///
/// Resampling runs on a dedicated serial queue to avoid blocking the audio render
/// thread and to prevent data races on converter state.
@MainActor
final class AudioStreamer: ObservableObject {

    @Published var isStreaming = false
    /// Surfaces persistent conversion failures to SensorManager/UI.
    @Published var lastError: String?

    private let sharedAudioSession: SharedAudioSession
    private weak var ws: WebSocketManager?
    private var settings: SettingsStore?

    private static let consumerID = "AudioStreamer"

    /// Target sample rate for Whisper (16 kHz)
    private let targetSampleRate: Double = 16000
    /// Buffer size in frames (50ms chunks at 16kHz = 800 frames)
    private let bufferFrames: AVAudioFrameCount = 800

    /// Serial queue for resampling — keeps converter state off the render thread.
    private let processQueue = DispatchQueue(label: "audio.streamer.process", qos: .userInteractive)

    /// Converter for resampling to 16kHz mono int16. Built lazily inside
    /// `processBuffer` and rebuilt whenever the input format changes (e.g. AEC
    /// switching the I/O unit to 48 kHz Float32, or a post-interruption format
    /// change). Accessed only from processQueue.
    nonisolated(unsafe) private var converter: AVAudioConverter?
    /// Input format the current `converter` was built from — used to detect a
    /// format change. Accessed only from processQueue.
    nonisolated(unsafe) private var lastInputFormat: AVAudioFormat?
    /// Output format for the converter (16kHz mono Int16, immutable after start)
    private var outputFormat: AVAudioFormat?

    /// Consecutive conversion errors — tracked on processQueue.
    /// After 10 consecutive failures, stops streaming and surfaces error.
    nonisolated(unsafe) private var consecutiveErrors: Int = 0
    private let maxConsecutiveErrors = 10

    init(ws: WebSocketManager, settings: SettingsStore, sharedAudioSession: SharedAudioSession) {
        self.ws = ws
        self.settings = settings
        self.sharedAudioSession = sharedAudioSession
    }

    // MARK: — Lifecycle

    func start() {
        guard !isStreaming else { return }

        // Target format: 16kHz mono Int16
        guard let outFmt = AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: targetSampleRate,
            channels: 1,
            interleaved: true
        ) else {
            AppLogger.shared.error("AudioStreamer", "Failed to create output format")
            return
        }
        outputFormat = outFmt
        lastError = nil
        consecutiveErrors = 0
        // Converter is built lazily on the first buffer (and rebuilt on any input
        // format change) inside processBuffer — never captured here, because the
        // input format can change after start() once AEC adjusts the I/O unit.
        converter = nil
        lastInputFormat = nil

        let capturedFormat = outFmt

        // Register with shared audio session and receive buffers via fan-out
        sharedAudioSession.addConsumer(Self.consumerID) { [weak self] buffer, _ in
            guard let self else { return }
            // Dispatch resampling off the audio render thread
            self.processQueue.async { [weak self] in
                self?.processBuffer(buffer, outputFormat: capturedFormat)
            }
        }

        isStreaming = true
    }

    func stop() {
        guard isStreaming else { return }
        sharedAudioSession.removeConsumer(Self.consumerID)
        isStreaming = false
        converter = nil
        lastInputFormat = nil
        outputFormat = nil
    }

    // MARK: — Processing (runs on processQueue)

    private func processBuffer(_ buffer: AVAudioPCMBuffer, outputFormat: AVAudioFormat) {
        // (Re)build the converter when the input format first appears or changes.
        // The shared engine's input format can change after start() — most notably
        // when AEC/Voice-Processing switches the I/O unit (typically to 48 kHz
        // Float32), and again after an interruption resume. Keying the rebuild on
        // the live buffer.format is more robust than capturing it once at start().
        let inputFormat = buffer.format
        let needsConvert = !inputFormat.isEqual(outputFormat)
        if needsConvert {
            if converter == nil || lastInputFormat == nil || !(lastInputFormat!.isEqual(inputFormat)) {
                converter = AVAudioConverter(from: inputFormat, to: outputFormat)
                lastInputFormat = inputFormat
                if converter == nil {
                    AppLogger.shared.error("AudioStreamer",
                        "Could not build converter for input format \(inputFormat)")
                }
            }
        } else if converter != nil {
            // Input already matches the target format — drop any stale converter.
            converter = nil
            lastInputFormat = inputFormat
        }

        // A conversion is required but no converter could be built — skip this
        // buffer rather than reinterpret non-Int16 samples as Int16 (garbage).
        if needsConvert && converter == nil { return }

        let int16Data: Data

        if let converter {
            // Resample to 16kHz mono int16
            let frameCapacity = AVAudioFrameCount(
                Double(buffer.frameLength) * targetSampleRate / buffer.format.sampleRate
            )
            guard let outputBuffer = AVAudioPCMBuffer(
                pcmFormat: outputFormat,
                frameCapacity: frameCapacity
            ) else { return }

            var error: NSError?
            var consumed = false
            converter.convert(to: outputBuffer, error: &error) { _, outStatus in
                if consumed {
                    outStatus.pointee = .noDataNow
                    return nil
                }
                consumed = true
                outStatus.pointee = .haveData
                return buffer
            }

            if let error {
                consecutiveErrors += 1
                if consecutiveErrors >= maxConsecutiveErrors {
                    let msg = "Audio conversion failed \(consecutiveErrors) times: \(error.localizedDescription)"
                    AppLogger.shared.error("AudioStreamer", msg)
                    Task { @MainActor [weak self] in
                        guard let self else { return }
                        self.lastError = msg
                        self.stop()
                    }
                }
                return
            }

            consecutiveErrors = 0
            int16Data = dataFromInt16Buffer(outputBuffer)
        } else {
            // Already in correct format
            consecutiveErrors = 0
            int16Data = dataFromInt16Buffer(buffer)
        }

        guard !int16Data.isEmpty else { return }

        // Send as base64 via WebSocket
        let base64 = int16Data.base64EncodedString()
        let frames = int16Data.count / 2  // 2 bytes per int16 sample

        Task { @MainActor [weak self] in
            self?.ws?.sendAudioStream(samplesBase64: base64, frames: frames)
        }
    }

    private func dataFromInt16Buffer(_ buffer: AVAudioPCMBuffer) -> Data {
        guard let int16Ptr = buffer.int16ChannelData else { return Data() }
        let frameCount = Int(buffer.frameLength)
        return Data(bytes: int16Ptr[0], count: frameCount * 2)
    }
}
