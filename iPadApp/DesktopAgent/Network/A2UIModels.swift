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
struct A2UINode: Codable, Identifiable {
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
struct A2UIAction: Codable {
    let event: String
    let value: String?
}

/// A single ChoicePicker option (label shown, value reported).
struct A2UIOption: Codable {
    let label: String
    let value: String
}

/// A complete surface to render. Decoded from the raw `a2ui_surface` (transient
/// overlay) or `a2ui_canvas` (persistent dashboard) message. The canvas form
/// omits `dismissible`/`timeout_s`, so both default when absent. Codable so the
/// canvas store can cache the last dashboard to disk and restore it on launch.
struct A2UISurface: Codable {
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

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        surfaceId = try c.decode(String.self, forKey: .surfaceId)
        version = try c.decodeIfPresent(String.self, forKey: .version)
        root = try c.decode(String.self, forKey: .root)
        // Transient-only fields — absent on a canvas payload.
        dismissible = try c.decodeIfPresent(Bool.self, forKey: .dismissible) ?? false
        timeoutS = try c.decodeIfPresent(Double.self, forKey: .timeoutS) ?? 0
        components = try c.decode([A2UINode].self, forKey: .components)
    }

    /// Memberwise init for building a patched copy in the canvas store.
    init(surfaceId: String, version: String?, root: String,
         dismissible: Bool, timeoutS: Double, components: [A2UINode]) {
        self.surfaceId = surfaceId
        self.version = version
        self.root = root
        self.dismissible = dismissible
        self.timeoutS = timeoutS
        self.components = components
    }

    /// id → node lookup for the recursive renderer.
    var nodesById: [String: A2UINode] {
        Dictionary(components.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
    }

    /// Return a copy with `patch` nodes merged in by id (replace existing,
    /// append new) — for `a2ui_canvas_update`.
    func merging(_ patch: [A2UINode]) -> A2UISurface {
        var byId = nodesById
        for node in patch { byId[node.id] = node }
        // Preserve original order where possible, then append any new ids.
        var ordered: [A2UINode] = components.map { byId[$0.id] ?? $0 }
        let existing = Set(components.map { $0.id })
        for node in patch where !existing.contains(node.id) { ordered.append(node) }
        return A2UISurface(surfaceId: surfaceId, version: version, root: root,
                           dismissible: dismissible, timeoutS: timeoutS, components: ordered)
    }
}
