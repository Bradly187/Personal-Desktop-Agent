import AVFoundation
import Accelerate
import Foundation

/// Classifies mouth sounds (cluck, pop, hiss) from the microphone.
/// Uses onset detection + spectral shape heuristics on AVAudioEngine FFT data.
/// 500 ms debounce prevents duplicate fires.
///
/// Uses SharedAudioSession for audio input — does NOT own its own AVAudioEngine.
@MainActor
final class SoundDetector {

    private let sharedAudioSession: SharedAudioSession
    private weak var ws: WebSocketManager?
    private var settings: SettingsStore?

    private static let consumerID = "SoundDetector"

    private var lastFireTime: Date?
    private let debounceDuration: TimeInterval = 0.2

    // FFT setup
    private let fftSize = 1024
    private var fftSetup: FFTSetup?
    private var prevMag: Float = 0

    init(ws: WebSocketManager, settings: SettingsStore, sharedAudioSession: SharedAudioSession) {
        self.ws = ws
        self.settings = settings
        self.sharedAudioSession = sharedAudioSession
        fftSetup = vDSP_create_fftsetup(vDSP_Length(log2(Float(fftSize))), FFTRadix(FFT_RADIX2))
    }

    deinit {
        if let setup = fftSetup { vDSP_destroy_fftsetup(setup) }
    }

    // MARK: — Lifecycle

    func start() {
        sharedAudioSession.addConsumer(Self.consumerID) { [weak self] buffer, _ in
            self?.analyze(buffer: buffer)
        }
    }

    func stop() {
        sharedAudioSession.removeConsumer(Self.consumerID)
    }

    // MARK: — Sound classification

    private func analyze(buffer: AVAudioPCMBuffer) {
        guard let channelData = buffer.floatChannelData?[0] else { return }
        let frameCount = Int(buffer.frameLength)
        guard frameCount >= fftSize else { return }

        // Compute RMS for onset detection
        var rms: Float = 0
        vDSP_rmsqv(channelData, 1, &rms, vDSP_Length(fftSize))

        // Simple threshold-based onset
        let onset = rms - prevMag
        prevMag = rms

        guard onset > 0.02 else { return }

        // Compute magnitude spectrum
        var real = [Float](repeating: 0, count: fftSize / 2)
        var imag = [Float](repeating: 0, count: fftSize / 2)
        var magnitudes = [Float](repeating: 0, count: fftSize / 2)

        real.withUnsafeMutableBufferPointer { realBuf in
            imag.withUnsafeMutableBufferPointer { imagBuf in
                var split = DSPSplitComplex(realp: realBuf.baseAddress!, imagp: imagBuf.baseAddress!)

                channelData.withMemoryRebound(to: DSPComplex.self, capacity: fftSize / 2) { ptr in
                    vDSP_ctoz(ptr, 2, &split, 1, vDSP_Length(fftSize / 2))
                }
                vDSP_fft_zrip(fftSetup!, &split, 1, vDSP_Length(log2(Float(fftSize))), FFTDirection(FFT_FORWARD))

                magnitudes.withUnsafeMutableBufferPointer { magBuf in
                    vDSP_zvmags(&split, 1, magBuf.baseAddress!, 1, vDSP_Length(fftSize / 2))
                }
            }
        }

        let sound = classify(magnitudes: magnitudes)
        guard let sound else { return }

        let now = Date()
        if let last = lastFireTime, now.timeIntervalSince(last) < debounceDuration { return }
        lastFireTime = now

        Task { @MainActor [weak self] in
            guard let self, let settings = self.settings else { return }
            // Only fire if user has a mapping for this sound
            if settings.soundMappings[sound] != nil {
                self.ws?.sendSoundAction(sound: sound, confidence: 0.8)
            }
        }
    }

    /// Classify a magnitude spectrum into a sound label.
    /// Heuristic: cluck = mid-frequency burst, pop = broad burst, hiss = high-frequency sustained.
    private func classify(magnitudes: [Float]) -> String? {
        let n = magnitudes.count
        let low   = magnitudes[0..<(n/4)].reduce(0, +)
        let mid   = magnitudes[(n/4)..<(n/2)].reduce(0, +)
        let high  = magnitudes[(n/2)...].reduce(0, +)
        let total = low + mid + high
        guard total > 0.001 else { return nil }

        let lowR  = low  / total
        let midR  = mid  / total
        let highR = high / total

        if midR > 0.5 && lowR < 0.3 { return "cluck" }
        if highR > 0.4              { return "hiss"  }
        if lowR > 0.4 && midR > 0.3 { return "pop"   }
        return nil
    }
}
