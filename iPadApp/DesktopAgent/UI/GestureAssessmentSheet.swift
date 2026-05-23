import SwiftUI

/// Self-reported gesture capability assessment.
///
/// The user tries each gesture and reports whether they can perform it
/// comfortably.  Results are stored in SettingsStore and sent to the PC
/// so GestureProcessor can skip unsupported gestures and relax confidence
/// floors for difficult-but-possible ones.
///
/// For RA users: PINCH is often the hardest on flare days — the user can
/// mark it as "Hard on bad days" rather than binary yes/no.
struct GestureAssessmentSheet: View {

    @ObservedObject var settings: SettingsStore
    @Environment(\.appTheme) private var theme
    @Environment(\.dismiss)  private var dismiss

    // MARK: - Gesture definitions

    private struct GestureDef: Identifiable {
        let id = UUID()
        let name:        String   // matches PC gesture classifier label
        let icon:        String   // SF Symbol
        let title:       String
        let instruction: String
        let whyMatters:  String
    }

    private let gestures: [GestureDef] = [
        GestureDef(
            name: "POINT",
            icon: "hand.point.up.left.fill",
            title: "Point",
            instruction: "Extend your index finger and hold it toward the camera for 2 seconds.",
            whyMatters: "Used for gaze+gesture click — aim and point."
        ),
        GestureDef(
            name: "PINCH",
            icon: "hand.pinch",
            title: "Pinch",
            instruction: "Touch your thumb and index finger together, then release.",
            whyMatters: "Used for precise selections and depth-confirmed clicks via LiDAR."
        ),
        GestureDef(
            name: "OPEN_PALM",
            icon: "hand.raised.fill",
            title: "Open Palm",
            instruction: "Spread all fingers wide and face your palm toward the camera.",
            whyMatters: "Used for scroll up and pause commands."
        ),
        GestureDef(
            name: "FIST",
            icon: "hand.raised.brakesignal",
            title: "Fist",
            instruction: "Close all fingers into a relaxed fist.",
            whyMatters: "Used for close window and stop commands."
        ),
    ]

    // MARK: - State

    @State private var phase: Phase = .intro
    @State private var currentIndex: Int = 0
    @State private var ratings: [String: Rating] = [:]

    private enum Phase { case intro, assessing, done }

    enum Rating: String, CaseIterable {
        case easy      = "Easy"
        case hard      = "Hard on bad days"
        case unable    = "Can't do this"

        var icon: String {
            switch self {
            case .easy:   return "checkmark.circle.fill"
            case .hard:   return "exclamationmark.triangle.fill"
            case .unable: return "xmark.circle.fill"
            }
        }
        var color: String { // used as key, actual color applied in view
            switch self {
            case .easy:   return "success"
            case .hard:   return "warning"
            case .unable: return "error"
            }
        }
    }

    // MARK: - Body

    var body: some View {
        NavigationStack {
            VStack(spacing: DesignTokens.Spacing.xl) {
                Spacer()
                switch phase {
                case .intro:     introContent
                case .assessing: assessingContent
                case .done:      doneContent
                }
                Spacer()
                actionButton
            }
            .padding(DesignTokens.Spacing.xl)
            .navigationTitle("Gesture Assessment")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Skip") { saveAndDismiss() }
                }
            }
        }
    }

    // MARK: - Intro

    private var introContent: some View {
        VStack(spacing: DesignTokens.Spacing.xl) {
            Image(systemName: "hand.wave.fill")
                .font(.system(size: 72))
                .foregroundStyle(theme.accent)

            Text("Gesture Assessment")
                .font(DesignTokens.Typography.headline)
                .foregroundStyle(theme.textPrimary)

            Text("We'll show you 4 hand gestures. For each one, try it and tell us how easy it is. Be honest — especially about harder days. The system adjusts confidence thresholds based on your assessment.")
                .font(DesignTokens.Typography.body)
                .foregroundStyle(theme.textSecondary)
                .multilineTextAlignment(.center)

            HStack(spacing: DesignTokens.Spacing.xl) {
                legendItem("checkmark.circle.fill", "Easy", theme.success)
                legendItem("exclamationmark.triangle.fill", "Hard on bad days", theme.warning)
                legendItem("xmark.circle.fill", "Can't do this", theme.destructive)
            }
        }
    }

    private func legendItem(_ icon: String, _ label: String, _ color: Color) -> some View {
        VStack(spacing: DesignTokens.Spacing.sm) {
            Image(systemName: icon).foregroundStyle(color).font(.title2)
            Text(label)
                .font(.system(size: 11))
                .foregroundStyle(theme.textSecondary)
                .multilineTextAlignment(.center)
        }
    }

    // MARK: - Assessing

    private var assessingContent: some View {
        let gesture = gestures[currentIndex]

        return VStack(spacing: DesignTokens.Spacing.xl) {

            // Step counter
            Text("Gesture \(currentIndex + 1) of \(gestures.count)")
                .font(DesignTokens.Typography.caption)
                .foregroundStyle(theme.textSecondary)

            // Gesture icon + title
            Image(systemName: gesture.icon)
                .font(.system(size: 72))
                .foregroundStyle(theme.accent)

            Text(gesture.title)
                .font(DesignTokens.Typography.headline)
                .foregroundStyle(theme.textPrimary)

            // Instruction card
            VStack(alignment: .leading, spacing: DesignTokens.Spacing.sm) {
                Text("How to do it")
                    .font(DesignTokens.Typography.caption.weight(.semibold))
                    .foregroundStyle(theme.textSecondary)
                Text(gesture.instruction)
                    .font(DesignTokens.Typography.body)
                    .foregroundStyle(theme.textPrimary)
            }
            .padding(DesignTokens.Spacing.lg)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(theme.surfaceSecondary)
            .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))

            Text("Why it matters: \(gesture.whyMatters)")
                .font(DesignTokens.Typography.caption)
                .foregroundStyle(theme.textSecondary)
                .multilineTextAlignment(.center)

            // Rating buttons
            VStack(spacing: DesignTokens.Spacing.md) {
                ForEach(Rating.allCases, id: \.self) { rating in
                    ratingButton(rating, for: gesture.name)
                }
            }
        }
    }

    private func ratingButton(_ rating: Rating, for gestureName: String) -> some View {
        let isSelected = ratings[gestureName] == rating
        let color: Color = {
            switch rating {
            case .easy:   return theme.success
            case .hard:   return theme.warning
            case .unable: return theme.destructive
            }
        }()

        return Button {
            ratings[gestureName] = rating
            // Auto-advance after brief delay
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) {
                advance()
            }
        } label: {
            HStack(spacing: DesignTokens.Spacing.md) {
                Image(systemName: rating.icon)
                    .foregroundStyle(isSelected ? .white : color)
                Text(rating.rawValue)
                    .font(DesignTokens.Typography.body.weight(isSelected ? .semibold : .regular))
                    .foregroundStyle(isSelected ? .white : theme.textPrimary)
                Spacer()
            }
            .padding(DesignTokens.Spacing.lg)
            .frame(minHeight: DesignTokens.Size.touchTargetCompact)
            .background(isSelected ? color : theme.surfaceSecondary)
            .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
            .animation(.easeInOut(duration: 0.15), value: isSelected)
        }
    }

    // MARK: - Done

    private var doneContent: some View {
        VStack(spacing: DesignTokens.Spacing.xl) {
            Image(systemName: "checkmark.seal.fill")
                .font(.system(size: 72))
                .foregroundStyle(theme.success)

            Text("Assessment Complete")
                .font(DesignTokens.Typography.headline)
                .foregroundStyle(theme.textPrimary)

            VStack(alignment: .leading, spacing: DesignTokens.Spacing.md) {
                ForEach(gestures) { gesture in
                    HStack(spacing: DesignTokens.Spacing.md) {
                        Image(systemName: gesture.icon)
                            .foregroundStyle(theme.accent)
                            .frame(width: 28)
                        Text(gesture.title)
                            .font(DesignTokens.Typography.body)
                            .foregroundStyle(theme.textPrimary)
                        Spacer()
                        if let r = ratings[gesture.name] {
                            Image(systemName: r.icon)
                                .foregroundStyle(r == .easy ? theme.success : r == .hard ? theme.warning : theme.destructive)
                            Text(r.rawValue)
                                .font(DesignTokens.Typography.caption)
                                .foregroundStyle(theme.textSecondary)
                        } else {
                            Text("Skipped")
                                .font(DesignTokens.Typography.caption)
                                .foregroundStyle(theme.textSecondary)
                        }
                    }
                }
            }
            .padding(DesignTokens.Spacing.lg)
            .background(theme.surfaceSecondary)
            .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))

            Text("Gestures marked 'Hard on bad days' get relaxed confidence thresholds on flare days.")
                .font(DesignTokens.Typography.caption)
                .foregroundStyle(theme.textSecondary)
                .multilineTextAlignment(.center)
        }
    }

    // MARK: - Action Button

    @ViewBuilder
    private var actionButton: some View {
        switch phase {
        case .intro:
            Button { phase = .assessing } label: {
                Text("Start Assessment")
                    .font(DesignTokens.Typography.body.weight(.semibold))
                    .foregroundStyle(.white)
                    .frame(maxWidth: .infinity)
                    .frame(minHeight: DesignTokens.Size.touchTargetCompact)
                    .background(theme.accent)
                    .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
            }

        case .assessing:
            Button { advance() } label: {
                Text("Skip this gesture")
                    .font(DesignTokens.Typography.body)
                    .foregroundStyle(theme.textSecondary)
            }

        case .done:
            Button { saveAndDismiss() } label: {
                Text("Save Assessment")
                    .font(DesignTokens.Typography.body.weight(.semibold))
                    .foregroundStyle(.white)
                    .frame(maxWidth: .infinity)
                    .frame(minHeight: DesignTokens.Size.touchTargetCompact)
                    .background(theme.success)
                    .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
            }
        }
    }

    // MARK: - Logic

    private func advance() {
        if currentIndex < gestures.count - 1 {
            currentIndex += 1
        } else {
            phase = .done
        }
    }

    private func saveAndDismiss() {
        // Persist gesture capability to SettingsStore
        // Maps gesture name → rating string
        let gestureRatings = ratings.mapValues { $0.rawValue }
        settings.gestureAssessment = gestureRatings

        // Disable gestures rated "Can't do this"
        let unable = ratings.filter { $0.value == .unable }.map { $0.key }
        for name in unable {
            settings.disabledGestures.insert(name)
        }
        dismiss()
    }
}
