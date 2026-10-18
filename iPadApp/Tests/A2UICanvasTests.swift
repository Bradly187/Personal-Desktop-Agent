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

    func testApprovalSurfaceDecodesAndPreservesCorrelation() throws {
        let digest = String(repeating: "d", count: 64)
        let requestId = String(repeating: "r", count: 32)
        let surface = try decode("""
        {"type":"a2ui_surface","surface_id":"approval-1","request_id":"\(requestId)",
         "action_digest":"\(digest)","root":"b","dismissible":false,"timeout_s":30,
         "components":[{"id":"b","component":"Button","label":"Approve",
         "action":{"event":"approval","value":"approve"}}]}
        """)
        XCTAssertEqual(surface.requestId, requestId)
        XCTAssertEqual(surface.actionDigest, digest)
        XCTAssertEqual(surface.merging([]).requestId, requestId)
        XCTAssertEqual(surface.merging([]).actionDigest, digest)
    }

    func testApprovalEventPayloadEchoesExactResponseContract() throws {
        let digest = String(repeating: "d", count: 64)
        let requestId = String(repeating: "r", count: 32)
        let payload = try XCTUnwrap(WebSocketManager.a2UIEventPayload(
            surfaceId: "approval-1",
            event: "approval",
            value: "approve",
            values: [:],
            requestId: requestId,
            actionDigest: digest,
            messageId: "a2ui-7",
            respondedAt: 123.5
        ))
        XCTAssertEqual(payload["request_id"] as? String, requestId)
        XCTAssertEqual(payload["id"] as? String, "a2ui-7")
        XCTAssertEqual(payload["action_digest"] as? String, digest)
        XCTAssertEqual(payload["decision"] as? String, "approve")
        XCTAssertEqual(payload["responded_at"] as? Double, 123.5)
        XCTAssertNil(payload["value"])
    }

    func testApprovalEventPayloadRejectsMissingBindingOrInvalidDecision() {
        XCTAssertNil(WebSocketManager.a2UIEventPayload(
            surfaceId: "approval-1", event: "approval", value: "approve", values: [:]
        ))
        XCTAssertNil(WebSocketManager.a2UIEventPayload(
            surfaceId: "approval-1", event: "approval", value: "yes", values: [:],
            requestId: String(repeating: "r", count: 32),
            actionDigest: String(repeating: "d", count: 64)
        ))
    }

    func testApprovalAckRequiresExactMessageCorrelationAndStatus() throws {
        let ok = try XCTUnwrap(WebSocketManager.a2UIApprovalAck(from: [
            "type": "ack", "id": "a2ui-7", "status": "ok"
        ]))
        XCTAssertEqual(ok.id, "a2ui-7")
        XCTAssertTrue(ok.accepted)
        XCTAssertNil(WebSocketManager.a2UIApprovalAck(from: [
            "type": "ack", "id": "msg-7", "status": "ok"
        ]))
        XCTAssertNil(WebSocketManager.a2UIApprovalAck(from: [
            "type": "ack", "id": "a2ui-7", "status": true
        ]))
    }
}
