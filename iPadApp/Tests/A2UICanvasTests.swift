import XCTest
@testable import DesktopAgent

/// Unit tests for the persistent dashboard canvas: decoding an `a2ui_canvas`
/// payload (which omits the transient `dismissible`/`timeout_s` fields) and the
/// `A2UISurface.merging(_:)` patch logic used by `a2ui_canvas_update`.
@MainActor
final class A2UICanvasTests: XCTestCase {

    private func decode(_ json: String) throws -> A2UISurface {
        try JSONDecoder().decode(A2UISurface.self, from: Data(json.utf8))
    }

    // MARK: — Canvas decodes without the transient fields

    func testCanvasDecodesWithoutTimeoutOrDismissible() throws {
        let json = """
        {"type":"a2ui_canvas","surface_id":"dashboard","version":"v0.9","root":"root",
         "components":[
           {"id":"root","component":"Column","children":["t"]},
           {"id":"t","component":"Text","text":"Agent","variant":"headline"}
         ]}
        """
        let s = try decode(json)
        XCTAssertEqual(s.surfaceId, "dashboard")
        XCTAssertEqual(s.root, "root")
        XCTAssertFalse(s.dismissible)      // defaulted
        XCTAssertEqual(s.timeoutS, 0)      // defaulted
        XCTAssertEqual(s.components.count, 2)
    }

    func testSurfaceStillDecodesWithTransientFields() throws {
        let json = """
        {"type":"a2ui_surface","surface_id":"approval-1","root":"b","dismissible":false,
         "timeout_s":30,"components":[{"id":"b","component":"Button","label":"OK",
         "action":{"event":"approval","value":"approve"}}]}
        """
        let s = try decode(json)
        XCTAssertFalse(s.dismissible)
        XCTAssertEqual(s.timeoutS, 30)
    }

    // MARK: — merging() replaces by id and appends new nodes

    func testMergingReplacesExistingNodeById() throws {
        let base = try decode("""
        {"type":"a2ui_canvas","surface_id":"dashboard","root":"root","components":[
          {"id":"root","component":"Column","children":["row0"]},
          {"id":"row0","component":"Text","text":"Connection: offline","variant":"body"}
        ]}
        """)
        let patch = [
            A2UINode(id: "row0", component: "Text", children: nil,
                     text: "Connection: online", variant: "body", label: nil,
                     icon: nil, placeholder: nil, fieldId: nil, action: nil, options: nil)
        ]
        let merged = base.merging(patch)
        XCTAssertEqual(merged.components.count, 2, "replace, not append")
        XCTAssertEqual(merged.nodesById["row0"]?.text, "Connection: online")
    }

    func testMergingAppendsNewNode() throws {
        let base = try decode("""
        {"type":"a2ui_canvas","surface_id":"dashboard","root":"root","components":[
          {"id":"root","component":"Column","children":["row0"]},
          {"id":"row0","component":"Text","text":"A","variant":"body"}
        ]}
        """)
        let patch = [
            A2UINode(id: "row1", component: "Text", children: nil, text: "B",
                     variant: "body", label: nil, icon: nil, placeholder: nil,
                     fieldId: nil, action: nil, options: nil)
        ]
        let merged = base.merging(patch)
        XCTAssertEqual(merged.components.count, 3)
        XCTAssertEqual(merged.nodesById["row1"]?.text, "B")
    }

    // MARK: — Encodable round-trip (the canvas store caches via JSONEncoder)

    func testCanvasRoundTripsThroughCache() throws {
        let original = try decode("""
        {"type":"a2ui_canvas","surface_id":"dashboard","root":"root","components":[
          {"id":"root","component":"Column","children":["t"]},
          {"id":"t","component":"Text","text":"Agent","variant":"headline"}
        ]}
        """)
        let data = try JSONEncoder().encode(original)
        let restored = try JSONDecoder().decode(A2UISurface.self, from: data)
        XCTAssertEqual(restored.surfaceId, original.surfaceId)
        XCTAssertEqual(restored.components.count, original.components.count)
        XCTAssertEqual(restored.nodesById["t"]?.text, "Agent")
    }
}
