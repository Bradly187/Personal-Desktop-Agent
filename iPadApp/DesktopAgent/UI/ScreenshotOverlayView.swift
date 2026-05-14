import SwiftUI

/// Full-screen overlay that displays a screenshot received from the PC bridge.
/// Dismisses on tap. Accessibility: labeled as modal with descriptive label.
struct ScreenshotOverlayView: View {
    @EnvironmentObject var screenshotStore: ScreenshotStore

    var body: some View {
        if screenshotStore.showScreenshot, let image = screenshotStore.latestScreenshot {
            ZStack {
                Color.black.opacity(0.6)
                    .ignoresSafeArea()

                Image(uiImage: image)
                    .resizable()
                    .scaledToFit()
                    .clipShape(RoundedRectangle(cornerRadius: 16))
                    .padding(24)
                    .accessibilityLabel("Screenshot from PC")
            }
            .onTapGesture {
                screenshotStore.dismiss()
            }
            .accessibilityAddTraits(.isModal)
        }
    }
}
