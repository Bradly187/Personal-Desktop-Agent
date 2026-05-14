import UIKit
import os

/// Manages screenshot display state. Decodes base64 image data received
/// from the PC bridge and drives the overlay presentation.
@MainActor
final class ScreenshotStore: ObservableObject {
    @Published var latestScreenshot: UIImage?
    @Published var showScreenshot: Bool = false

    private let logger = Logger(subsystem: "com.desktopagent", category: "ScreenshotStore")

    /// Decode a base64-encoded image and present it in the overlay.
    /// If decoding fails, logs a warning and leaves the UI unchanged (no crash).
    func handleScreenshot(base64: String, mime: String) {
        guard let data = Data(base64Encoded: base64, options: .ignoreUnknownCharacters) else {
            logger.warning("Failed to decode base64 screenshot data (length: \(base64.count))")
            return
        }
        guard let image = UIImage(data: data) else {
            logger.warning("Failed to create UIImage from decoded data (mime: \(mime), bytes: \(data.count))")
            return
        }
        latestScreenshot = image
        showScreenshot = true
    }

    /// Dismiss the screenshot overlay.
    func dismiss() {
        showScreenshot = false
    }
}
