import AVFoundation
import Foundation

/// Streams raw PCM audio from the iPad microphone to the PC via WebSocket.
/// The PC-side WhisperStream handles VAD and transcription.
///
/// Audio format: 16-bit signed integer PCM, 16 kHz mono.
/// Messages sent as `audio_stream` type with base64-encoded samples.
///
/// Uses SharedAudioSession for audio input — does NOT own its own AVAudioEngine.
/// All three audio sensors (KeywordListener, SoundDetector, AudioStreamer) share
/// the same engine via the fan-out tap pattern in SharedAudioSession.
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

    /// Converter for resampling to 16kHz mono int16 (accessed only from processQueue)
    private var converter: AVAudioConverter?
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
            print("AudioStreamer: failed to create output format")
            return
        }
        outputFormat = outFmt
        lastError = nil
        consecutiveErrors = 0

        // Create converter if sample rates differ from the shared engine's input format
        let inputFormat = sharedAudioSession.inputFormat
        if inputFormat.sampleRate != targetSampleRate || inputFormat.channelCount != 1 {
            converter = AVAudioConverter(from: inputFormat, to: outFmt)
        }

        // Capture converter and format for the closure (avoids accessing self on render thread)
        let capturedConverter = converter
        let capturedFormat = outFmt

        // Register with shared audio session and receive buffers via fan-out
        sharedAudioSession.addConsumer(Self.consumerID) { [weak self] buffer, _ in
            guard let self else { return }
            // Dispatch resampling off the audio render thread
            self.processQueue.async { [weak self] in
                self?.processBuffer(buffer, converter: capturedConverter, outputFormat: capturedFormat)
            }
        }

        isStreaming = true
    }

    func stop() {
        guard isStreaming else { return }
        sharedAudioSession.removeConsumer(Self.consumerID)
        isStreaming = false
        converter = nil
        outputFormat = nil
    }

    // MARK: — Processing (runs on processQueue)

    private func processBuffer(_ buffer: AVAudioPCMBuffer, converter: AVAudioConverter?, outputFormat: AVAudioFormat) {
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
                    print("AudioStreamer: \(msg)")
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
