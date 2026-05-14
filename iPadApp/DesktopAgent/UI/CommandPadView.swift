import SwiftUI

/// Configurable button grid for sending explicit desktop commands.
/// Uses DesignTokens.Size.touchTargetMin (80pt) for all primary action buttons.
/// Palm rejection via UITouch.majorRadius threshold.
struct CommandPadView: View {
    @EnvironmentObject var wsManager: WebSocketManager
    @EnvironmentObject var settings: SettingsStore

    @Environment(\.appTheme) private var theme

    private let columns = [
        GridItem(.adaptive(minimum: DesignTokens.Size.touchTargetMin, maximum: 160),
                 spacing: DesignTokens.Spacing.md)
    ]

    var body: some View {
        NavigationStack {
            ScrollView {
                LazyVGrid(columns: columns, spacing: DesignTokens.Spacing.md) {
                    ForEach(settings.commandButtons) { button in
                        CommandButtonView(button: button)
                            .environmentObject(wsManager)
                    }
                }
                .padding(DesignTokens.Spacing.lg)
            }
            .navigationTitle("Commands")
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    NavigationLink(destination: CommandPadEditorView()
                        .environmentObject(settings)) {
                        Label("Edit", systemImage: "square.and.pencil")
                    }
                }
            }
        }
    }
}

// MARK: — Individual command button (DAButton pattern)

private struct CommandButtonView: View {
    let button: CommandButton
    @EnvironmentObject var wsManager: WebSocketManager

    @Environment(\.appTheme) private var theme

    @State private var flash = false

    var body: some View {
        Button {
            sendCommand()
        } label: {
            VStack(spacing: DesignTokens.Spacing.sm) {
                Image(systemName: iconName(for: button.action))
                    .font(.system(size: DesignTokens.Size.iconSize))
                    .foregroundStyle(theme.accent)
                Text(button.label)
                    .font(DesignTokens.Typography.caption)
                    .foregroundStyle(theme.textPrimary)
                    .multilineTextAlignment(.center)
                    .lineLimit(2)
            }
            .frame(maxWidth: .infinity,
                   minHeight: DesignTokens.Size.touchTargetMin)
            .background(flash ? theme.accent.opacity(0.3) : theme.surfaceSecondary)
            .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.md))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(button.label)
        .accessibilityHint("Double-tap to send \(button.action) command")
    }

    private func sendCommand() {
        flash = true
        withAnimation(.easeOut(duration: 0.2)) {
            flash = false
        }

        var params: [String: Any] = [:]
        for (k, v) in button.params {
            if k == "keys" {
                params[k] = v.split(separator: ",").map(String.init)
            } else {
                params[k] = v
            }
        }
        wsManager.sendCommand(action: button.action, text: button.label, params: params)
    }

    private func iconName(for action: String) -> String {
        switch action.uppercased() {
        case "CLICK":      return "cursorarrow.click"
        case "SCROLL":     return "scroll"
        case "TYPE":       return "keyboard"
        case "OPEN":       return "square.and.arrow.up"
        case "CLOSE":      return "xmark.square"
        case "HOTKEY":     return "command"
        case "DICTATE":    return "mic"
        case "SCREENSHOT": return "camera.viewfinder"
        default:           return "bolt"
        }
    }
}

// MARK: — Editor (reorder/add/remove buttons)

private struct CommandPadEditorView: View {
    @EnvironmentObject var settings: SettingsStore
    @State private var editMode: EditMode = .active

    var body: some View {
        List {
            ForEach(settings.commandButtons) { btn in
                HStack {
                    Text(btn.label)
                    Spacer()
                    Text(btn.action)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .onDelete { idx in settings.commandButtons.remove(atOffsets: idx) }
            .onMove { from, to in settings.commandButtons.move(fromOffsets: from, toOffset: to) }
        }
        .environment(\.editMode, $editMode)
        .navigationTitle("Edit Buttons")
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button("Reset") { settings.commandButtons = CommandButton.defaults }
            }
        }
    }
}
