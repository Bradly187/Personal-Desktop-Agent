import Foundation

// MARK: - A2UI message models
//
// Declarative UI surfaces pushed by the PC agent and rendered natively from a
// closed catalog (see A2UIRenderer). The payload carries INTENT only — no code,
// no markup — which is what makes the channel safe: the renderer instantiates
// only known component types and ignores anything it doesn't recognize.
//
// Mirror of core/a2ui.py (PC side). v1 catalog:
//   Column, Row, Card, Text, Button, ChoicePicker, TextField, Divider

/// One node in the flat, id-referenced component adjacency list.
struct A2UINode: Decodable, Identifiable {
    let id: String
    let component: String
    let children: [String]?
    let text: String?
    let variant: String?
    let label: String?
    let icon: String?
    let placeholder: String?
    let fieldId: String?
    let action: A2UIAction?
    let options: [A2UIOption]?

    enum CodingKeys: String, CodingKey {
        case id, component, children, text, variant, label, icon, placeholder
        case fieldId = "field_id"
        case action, options
    }
}

/// The event a control fires when activated.
struct A2UIAction: Decodable {
    let event: String
    let value: String?
}

/// A single ChoicePicker option (label shown, value reported).
struct A2UIOption: Decodable {
    let label: String
    let value: String
}

/// A complete surface to render. Decoded from the raw `a2ui_surface` message.
struct A2UISurface: Decodable {
    let surfaceId: String
    let version: String?
    let root: String
    let dismissible: Bool
    let timeoutS: Double
    let components: [A2UINode]

    enum CodingKeys: String, CodingKey {
        case surfaceId = "surface_id"
        case version, root, dismissible, components
        case timeoutS = "timeout_s"
    }

    /// id → node lookup for the recursive renderer.
    var nodesById: [String: A2UINode] {
        Dictionary(components.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
    }
}
