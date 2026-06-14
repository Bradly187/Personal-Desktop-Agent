import XCTest
import AVFoundation
@testable import DesktopAgent

/// Unit tests for `SharedAudioSession.audioMode(aecEnabled:)` and the
/// `DA_AEC_ENABLED` kill-switch (PR-2, hardware echo cancellation).
///
/// `.voiceChat` engages the Voice-Processing I/O unit (AEC/AGC/noise
/// suppression); `.default` leaves the mic input unprocessed. The mapping is a
/// pure function so it can be verified without an audio session on the host.
final class AudioSessionModeTests: XCTestCase {

    // MARK: — Pure mode mapping

    func testAECEnabledSelectsVoiceChat() {
        XCTAssertEqual(SharedAudioSession.audioMode(aecEnabled: true), .voiceChat)
    }

    func testAECDisabledSelectsDefault() {
        XCTAssertEqual(SharedAudioSession.audioMode(aecEnabled: false), .default)
    }

    func testModeIsNeverMeasurement() {
        // .measurement disables system signal processing (incl. AEC) — must never
        // be chosen by the AEC path regardless of the flag.
        XCTAssertNotEqual(SharedAudioSession.audioMode(aecEnabled: true), .measurement)
        XCTAssertNotEqual(SharedAudioSession.audioMode(aecEnabled: false), .measurement)
    }

    // MARK: — Kill-switch default + override

    func testAECDefaultsOnWhenKeyUnset() {
        let defaults = UserDefaults.standard
        defaults.removeObject(forKey: SharedAudioSession.aecDefaultsKey)
        XCTAssertTrue(SharedAudioSession.isAECEnabled,
                      "AEC must default ON when the key has never been set")
    }

    func testAECRespectsDisableOverride() {
        let defaults = UserDefaults.standard
        defer { defaults.removeObject(forKey: SharedAudioSession.aecDefaultsKey) }
        defaults.set(false, forKey: SharedAudioSession.aecDefaultsKey)
        XCTAssertFalse(SharedAudioSession.isAECEnabled)
        XCTAssertEqual(
            SharedAudioSession.audioMode(aecEnabled: SharedAudioSession.isAECEnabled),
            .default,
            "With the kill-switch off the session must use the unprocessed .default mode"
        )
    }

    func testAECRespectsEnableOverride() {
        let defaults = UserDefaults.standard
        defer { defaults.removeObject(forKey: SharedAudioSession.aecDefaultsKey) }
        defaults.set(true, forKey: SharedAudioSession.aecDefaultsKey)
        XCTAssertTrue(SharedAudioSession.isAECEnabled)
        XCTAssertEqual(
            SharedAudioSession.audioMode(aecEnabled: SharedAudioSession.isAECEnabled),
            .voiceChat
        )
    }
}
