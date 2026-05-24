import UIKit
import os

/// Manages screenshot display state. Decodes base64 image data received
/// from the PC bridge and drives the overlay presentation.
@MainActor
final class ScreenshotStore: ObservableObject {
    @Published var latestScreenshot: UIImage?
    @Published var showScreenshot: Bool = false

    private let logger = Logger(subsystem: "com.desktopagent", category: "ScreenshotStore")

    // M1: Release decoded UIImage under memory pressure (full-screen screenshots
    // can be 4–8 MB each and stay in RAM until the next replaces them).
    private var memoryWarningObserver: NSObjectProtocol?

    init() {
        memoryWarningObserver = NotificationCenter.default.addObserver(
            forName: UIApplication.didReceiveMemoryWarningNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            guard let self else { return }
            if !self.showScreenshot {
                AppLogger.shared.info("ScreenshotStore", "Released cached screenshot on memory warning")
                self.latestScreenshot = nil
            }
        }
    }

    deinit {
        if let observer = memoryWarningObserver {
            NotificationCenter.default.removeObserver(observer)
        }
    }

    /// Decode a base64-encoded image and present it in the overlay.
    /// Decoding runs off the main thread to avoid blocking UI during large screenshots.
    func handleScreenshot(base64: String, mime: String) {
        Task.detached(priority: .userInitiated) {
            guard let data = Data(base64Encoded: base64, options: .ignoreUnknownCharacters) else {
                await MainActor.run {
                    self.logger.warning("Failed to decode base64 screenshot data (length: \(base64.count))")
                }
                return
            }
            guard let image = UIImage(data: data) else {
                await MainActor.run {
                    self.logger.warning("Failed to create UIImage from decoded data (mime: \(mime), bytes: \(data.count))")
                }
                return
            }
            await MainActor.run {
                self.latestScreenshot = image
                self.showScreenshot = true
            }
        }
    }

    /// Dismiss the screenshot overlay and release the cached image.
    /// C1: Don't hold the decoded UIImage in RAM after the user dismisses it.
    func dismiss() {
        showScreenshot = false
        latestScreenshot = nil
    }
}
