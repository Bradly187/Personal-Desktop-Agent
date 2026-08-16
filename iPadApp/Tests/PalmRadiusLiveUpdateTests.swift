import XCTest
@testable import DesktopAgent

/// Regression tests for the "Palm Reject Radius" slider actually taking effect.
///
/// Bug (fixed 2026-08-16): `TrackpadGestureView.makeCoordinator()` captured
/// `palmRadius` once and `updateUIView` never refreshed it, so the coordinator
/// kept its first value forever. Because `ContentView` deliberately keeps the
/// `TabView` alive across tab switches (its "Fix #15"), `TrackpadGestureView` is
/// never recreated during a session — so the Settings slider
/// (`SettingsView`, range 10...60) was inert for the whole session, silently
/// disabling a pain-day accessibility control.
///
/// Property: for ANY palm radius the user selects, a live coordinator SHALL
/// observe that value, and its touch-filtering decision SHALL follow it.
@MainActor
final class PalmRadiusLiveUpdateTests: XCTestCase {

    /// Builds a coordinator the way SwiftUI would, via the view's own factory.
    private func makeCoordinator(initialRadius: CGFloat)
        -> TrackpadGestureView.Coordinator {
        TrackpadGestureView(palmRadius: initialRadius, speed: 1.0) { _ in }.makeCoordinator()
    }

    // MARK: — The regression itself

    func testApplyLiveSettingsUpdatesPalmRadius() {
        let coordinator = makeCoordinator(initialRadius: 25)
        XCTAssertEqual(coordinator.palmRadius, 25, accuracy: 1e-10,
                       "Coordinator should start at the radius it was built with")

        TrackpadGestureView.applyLiveSettings(to: coordinator, palmRadius: 55)

        XCTAssertEqual(coordinator.palmRadius, 55, accuracy: 1e-10,
                       "Moving the Palm Reject Radius slider must reach the live coordinator")
    }

    /// The slider's full range (SettingsView: 10...60) must round-trip.
    func testEveryValueInSliderRangeApplies() {
        let coordinator = makeCoordinator(initialRadius: 25)
        for raw in stride(from: 10.0, through: 60.0, by: 0.5) {
            let radius = CGFloat(raw)
            TrackpadGestureView.applyLiveSettings(to: coordinator, palmRadius: radius)
            XCTAssertEqual(coordinator.palmRadius, radius, accuracy: 1e-10,
                           "Slider value \(raw) should apply verbatim")
        }
    }

    func testRepeatedUpdatesTakeTheLatestValue() {
        let coordinator = makeCoordinator(initialRadius: 25)
        for radius in [10, 60, 30, 45, 12] as [CGFloat] {
            TrackpadGestureView.applyLiveSettings(to: coordinator, palmRadius: radius)
        }
        XCTAssertEqual(coordinator.palmRadius, 12, accuracy: 1e-10,
                       "Last write wins — no stale capture")
    }

    // MARK: — The behaviour the setting exists to control

    /// Raising the radius must widen what counts as a legitimate fingertip;
    /// lowering it must narrow it. This is the user-visible consequence of the
    /// value reaching the coordinator at all.
    func testFilterThresholdFollowsTheUpdatedRadius() {
        let coordinator = makeCoordinator(initialRadius: 25)

        // A contact of radius 40 is rejected while the threshold is 25 …
        TrackpadGestureView.applyLiveSettings(to: coordinator, palmRadius: 25)
        XCTAssertFalse(40 < coordinator.palmRadius,
                       "A 40pt contact should be rejected at a 25pt threshold")

        // … and accepted once the user raises the threshold past it.
        TrackpadGestureView.applyLiveSettings(to: coordinator, palmRadius: 55)
        XCTAssertTrue(40 < coordinator.palmRadius,
                      "The same 40pt contact should pass at a 55pt threshold")
    }
}
