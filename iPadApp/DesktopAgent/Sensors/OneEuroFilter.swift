import Foundation
import QuartzCore

/// 1-Euro adaptive low-pass filter (Casiez et al., 2012).
///
/// Provides jitter reduction at low speeds and minimal latency at high speeds.
/// Applied per-axis on iPad before sending sensor deltas (e.g. tilt) over WebSocket.
///
/// Reference: Géry Casiez, Nicolas Roussel, Daniel Vogel. 1€ Filter: A Simple
/// Speed-based Low-pass Filter for Noisy Input in Interactive Systems. CHI 2012.
///
/// Thread safety: This class is NOT thread-safe. Use from a single serial context
/// (e.g., the ARKit delegate OperationQueue or a dedicated serial queue).
final class OneEuroFilter {

    // MARK: - Configuration

    /// Minimum cutoff frequency in Hz. Lower = more smoothing at rest.
    let minCutoff: Double

    /// Speed coefficient. Higher = faster response to speed changes.
    let beta: Double

    /// Derivative cutoff frequency in Hz. Controls smoothing of speed estimate.
    let dCutoff: Double

    // MARK: - State

    private var xPrev: Double?
    private var dxPrev: Double = 0.0
    private var tPrev: CFTimeInterval?

    // MARK: - Init

    /// Create a 1-Euro filter with the given parameters.
    ///
    /// Parameters are clamped to valid ranges:
    /// - minCutoff: [0.1, 10.0] Hz
    /// - beta: [0.0, 1.0]
    /// - dCutoff: [0.1, 10.0] Hz
    init(minCutoff: Double = 1.0, beta: Double = 0.007, dCutoff: Double = 1.0) {
        self.minCutoff = max(0.1, min(10.0, minCutoff))
        self.beta = max(0.0, min(1.0, beta))
        self.dCutoff = max(0.1, min(10.0, dCutoff))
    }

    // MARK: - Public API

    /// Filter a single sample. Returns the filtered value.
    ///
    /// - Parameters:
    ///   - x: Raw input value.
    ///   - timestamp: Monotonic time of this sample (e.g., CACurrentMediaTime()).
    /// - Returns: Filtered output value.
    func filter(_ x: Double, timestamp: CFTimeInterval = CACurrentMediaTime()) -> Double {
        guard let prevT = tPrev, let prevX = xPrev else {
            // First sample — initialize without jump
            xPrev = x
            dxPrev = 0.0
            tPrev = timestamp
            return x
        }

        let dt = timestamp - prevT
        guard dt > 0 else { return prevX }
        tPrev = timestamp

        // 1. Compute raw derivative
        let dx = (x - prevX) / dt

        // 2. Filter derivative with fixed cutoff
        let alphaD = Self.alpha(cutoff: dCutoff, dt: dt)
        dxPrev = alphaD * dx + (1 - alphaD) * dxPrev

        // 3. Compute adaptive cutoff frequency
        let fc = minCutoff + beta * abs(dxPrev)

        // 4. Filter signal with adaptive cutoff
        let a = Self.alpha(cutoff: fc, dt: dt)
        let filtered = a * x + (1 - a) * prevX
        xPrev = filtered

        return filtered
    }

    /// Reset filter state. Call when sensor restarts, after blinks, or on mode change.
    ///
    /// - Parameter initialValue: Optional seed value. If nil, next `filter()` call
    ///   will initialize from its input (no jump).
    func reset(initialValue: Double? = nil) {
        xPrev = initialValue
        dxPrev = 0.0
        tPrev = nil
    }

    /// Whether the filter has received at least one sample.
    var isInitialized: Bool {
        xPrev != nil
    }

    // MARK: - Private

    /// Compute smoothing factor alpha from cutoff frequency and time step.
    private static func alpha(cutoff: Double, dt: Double) -> Double {
        let tau = 1.0 / (2.0 * .pi * cutoff)
        return 1.0 / (1.0 + tau / dt)
    }
}
