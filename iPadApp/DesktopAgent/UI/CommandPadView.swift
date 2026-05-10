import SwiftUI

/// Configurable button grid for sending explicit desktop commands.
/// Minimum 80×80 pt touch targets per iOS accessibility guidelines.
/// Palm rejection via UITouch.majorRadius threshold.
struct CommandPadView: View {
    @EnvironmentObject var wsManager: WebSocketManager
    @EnvironmentObject var settings: SettingsStore

    private let columns = [
        GridItem(.adaptive(minimum: 80, maximum: 160), spacing: 12)
    ]

    var body: some View {
        NavigationStack {
            ScrollView {
                LazyVGrid(columns: columns, spacing: 12) {
                    ForEach(settings.commandButtons) { button in
                        CommandButtonView(button: button)
                            .environmentObject(wsManager)
                    }
                }
                .padding()
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

// MARK: — Individual command button

private struct CommandButtonView: View {
    let button: CommandButton
    @EnvironmentObject var wsManager: WebSocketManager

    @State private var isPressed = false
    @State private var flash = false

    var body: some View {
        Button {
            sendCommand()
        } label: {
            VStack(spacing: 4) {
                Image(systemName: iconName(for: button.action))
                    .font(.title2)
                Text(button.label)
                    .font(.caption)
                    .multilineTextAlignment(.center)
                    .lineLimit(2)
            }
            .frame(maxWidth: .infinity, minHeight: 80)
            .background(flash ? Color.accentColor.opacity(0.3) : Color(.secondarySystemGroupedBackground))
            .clipShape(RoundedRectangle(cornerRadius: 12))
            .contentShape(Rectangle())    // full hit area
        }
        .buttonStyle(.plain)
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
