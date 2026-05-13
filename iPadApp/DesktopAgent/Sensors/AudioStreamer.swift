import AVFoundation
import Foundation

/// Streams raw PCM audio from the iPad microphone to the PC via WebSocket.
/// The PC-side WhisperStream handles VAD and transcription.
///
/// Audio format: 16-bit signed integer PCM, 16 kHz mono.
/// Messages sent as `audio_stream` type with base64-encoded samples.
///
/// This runs independently of KeywordListener — both can use the mic
/// simultaneously via AVAudioEngine's tap mechanism (separate taps on
/// different buses or shared engine with multiple taps).
@MainActor
final class AudioStreamer: ObservableObject {

    @Published var isStreaming = false

    private let engine = AVAudioEngine()
    private weak var ws: WebSocketManager?
    private var settings: SettingsStore?

    /// Target sample rate for Whisper (16 kHz)
    private let targetSampleRate: Double = 16000
    /// Buffer size in frames (50ms chunks at 16kHz = 800 frames)
    private let bufferFrames: AVAudioFrameCount = 800
    /// Converter for resampling to 16kHz mono int16
    private var converter: AVAudioConverter?

    init(ws: WebSocketManager, settings: SettingsStore) {
        self.ws = ws
        self.settings = settings
    }

    // MARK: — Lifecycle

    func start() {
        guard !isStreaming else { return }

        do {
            let inputNode = engine.inputNode
            let inputFormat = inputNode.outputFormat(forBus: 0)

            // Target format: 16kHz mono Int16
            guard let outputFormat = AVAudioFormat(
                commonFormat: .pcmFormatInt16,
                sampleRate: targetSampleRate,
                channels: 1,
                interleaved: true
            ) else {
                print("AudioStreamer: failed to create output format")
                return
            }

            // Create converter if sample rates differ
            if inputFormat.sampleRate != targetSampleRate || inputFormat.channelCount != 1 {
                converter = AVAudioConverter(from: inputFormat, to: outputFormat)
            }

            inputNode.installTap(onBus: 0, bufferSize: bufferFrames, format: inputFormat) {
                [weak self] buffer, _ in
                self?.processBuffer(buffer, outputFormat: outputFormat)
            }

            try engine.start()
            isStreaming = true
        } catch {
            print("AudioStreamer: engine start failed: \(error)")
        }
    }

    func stop() {
        guard isStreaming else { return }
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        isStreaming = false
        converter = nil
    }

    // MARK: — Processing

    private func processBuffer(_ buffer: AVAudioPCMBuffer, outputFormat: AVAudioFormat) {
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
                print("AudioStreamer: conversion error: \(error)")
                return
            }

            int16Data = dataFromInt16Buffer(outputBuffer)
        } else {
            // Already in correct format
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
