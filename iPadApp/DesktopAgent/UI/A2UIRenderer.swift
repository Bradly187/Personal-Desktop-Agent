import SwiftUI

// MARK: - A2UIRenderer
//
// Renders an A2UISurface natively from the closed v1 catalog, mapping each
// component type onto an existing DesignSystem view. The switch is exhaustive:
// an unknown component type renders nothing (and is logged), so a malformed or
// future-versioned payload can never execute code or crash the app.
//
// Form field state (TextField / future Slider / Checkbox) lives in A2UIState,
// keyed by field_id, and is sent back with every event under `values`.

final class A2UIState: ObservableObject {
    @Published var fieldValues: [String: String] = [:]
    let onEvent: (_ event: String, _ value: String?, _ values: [String: String]) -> Void

    init(onEvent: @escaping (_ event: String, _ value: String?, _ values: [String: String]) -> Void) {
        self.onEvent = onEvent
    }

    func fire(event: String, value: String?) {
        onEvent(event, value, fieldValues)
    }
}

struct A2UIRenderer: View {
    let surface: A2UISurface
    @ObservedObject var state: A2UIState

    var body: some View {
        let nodes = surface.nodesById
        if let rootNode = nodes[surface.root] {
            render(rootNode, nodes)
        } else {
            // Root didn't resolve — render nothing rather than guess.
            EmptyView()
        }
    }

    // Returns AnyView (not opaque `some View`) because the component tree is
    // recursive — an opaque return type cannot be inferred in terms of itself.
    private func render(_ node: A2UINode, _ nodes: [String: A2UINode]) -> AnyView {
        switch node.component {
        case "Column":
            return AnyView(VStack(alignment: .leading, spacing: DesignTokens.Spacing.md) {
                childViews(node, nodes)
            })
        case "Row":
            return AnyView(HStack(spacing: DesignTokens.Spacing.md) {
                childViews(node, nodes)
            })
        case "Card":
            return AnyView(DACard {
                VStack(alignment: .leading, spacing: DesignTokens.Spacing.md) {
                    childViews(node, nodes)
                }
            })
        case "Text":
            return AnyView(textView(node))
        case "Button":
            return AnyView(buttonView(node))
        case "ChoicePicker":
            return AnyView(choicePickerView(node))
        case "TextField":
            return AnyView(textFieldView(node))
        case "Divider":
            return AnyView(Divider())
        default:
            // Unknown / future component — skip safely.
            return AnyView(EmptyView()
                .onAppear { AppLogger.shared.warning("a2ui", "unknown component: \(node.component)") })
        }
    }

    private func childViews(_ node: A2UINode, _ nodes: [String: A2UINode]) -> AnyView {
        AnyView(ForEach(node.children ?? [], id: \.self) { childId in
            if let child = nodes[childId] {
                render(child, nodes)
            }
        })
    }

    @ViewBuilder
    private func textView(_ node: A2UINode) -> some View {
        let font: Font = {
            switch node.variant {
            case "headline": return DesignTokens.Typography.headline
            case "caption":  return DesignTokens.Typography.caption
            default:         return DesignTokens.Typography.body
            }
        }()
        Text(node.text ?? "")
            .font(font)
    }

    @ViewBuilder
    private func buttonView(_ node: A2UINode) -> some View {
        // Reuses DAButton's 80pt touch target + accessibility annotations.
        DAButton(label: node.label ?? "",
                 icon: node.icon ?? "circle") {
            if let action = node.action {
                state.fire(event: action.event, value: action.value)
            }
        }
    }

    @ViewBuilder
    private func choicePickerView(_ node: A2UINode) -> some View {
        VStack(spacing: DesignTokens.Spacing.sm) {
            ForEach(node.options ?? [], id: \.value) { opt in
                DAButton(label: opt.label, icon: "circle") {
                    state.fire(event: node.action?.event ?? "choice", value: opt.value)
                }
            }
        }
    }

    @ViewBuilder
    private func textFieldView(_ node: A2UINode) -> some View {
        let fieldId = node.fieldId ?? node.id
        TextField(node.placeholder ?? "",
                  text: Binding(
                    get: { state.fieldValues[fieldId] ?? "" },
                    set: { state.fieldValues[fieldId] = $0 }
                  ))
            .textFieldStyle(.roundedBorder)
            .font(DesignTokens.Typography.body)
            .frame(minHeight: DesignTokens.Size.touchTargetCompact)
    }
}
