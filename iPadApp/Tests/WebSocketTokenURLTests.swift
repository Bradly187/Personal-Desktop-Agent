import XCTest
@testable import DesktopAgent

/// Unit tests for `WebSocketManager.appendingToken(_:to:)` — the pairing-token
/// (security fix C1) query-param builder. Verifies the iPad sends `?token=…` on
/// the WebSocket URL so the bridge does not reject the connection with HTTP 401.
@MainActor
final class WebSocketTokenURLTests: XCTestCase {

    private let base = URL(string: "ws://192.168.1.50:8765/ws")!

    // MARK: — Empty / whitespace tokens leave the URL untouched

    func testEmptyTokenLeavesURLUnchanged() {
        let result = WebSocketManager.appendingToken("", to: base)
        XCTAssertEqual(result.absoluteString, base.absoluteString)
    }

    func testWhitespaceTokenLeavesURLUnchanged() {
        let result = WebSocketManager.appendingToken("   \n\t ", to: base)
        XCTAssertEqual(result.absoluteString, base.absoluteString,
                       "A token that trims to empty should not add a query param")
    }

    // MARK: — Normal token is appended as a query item

    func testTokenAppendedAsQueryParam() {
        let token = "abc123XYZ-_"
        let result = WebSocketManager.appendingToken(token, to: base)
        XCTAssertEqual(result.scheme, "ws")
        XCTAssertEqual(result.host, "192.168.1.50")
        XCTAssertEqual(result.port, 8765)
        XCTAssertEqual(result.path, "/ws")
        XCTAssertEqual(_queryValue("token", in: result), token)
    }

    func testTokenIsTrimmedBeforeSending() {
        let result = WebSocketManager.appendingToken("  paste-me  ", to: base)
        XCTAssertEqual(_queryValue("token", in: result), "paste-me",
                       "Leading/trailing whitespace from a paste should be stripped")
    }

    // MARK: — Replace / preserve existing query

    func testReplacesExistingToken() {
        let url = URL(string: "ws://host:8765/ws?token=STALE")!
        let result = WebSocketManager.appendingToken("FRESH", to: url)
        XCTAssertEqual(_queryValue("token", in: result), "FRESH")
        // Exactly one token param — no duplicates.
        let comps = URLComponents(url: result, resolvingAgainstBaseURL: false)
        XCTAssertEqual(comps?.queryItems?.filter { $0.name == "token" }.count, 1)
    }

    func testPreservesExistingNonTokenQuery() {
        let url = URL(string: "ws://host:8765/ws?foo=bar")!
        let result = WebSocketManager.appendingToken("T", to: url)
        XCTAssertEqual(_queryValue("foo", in: result), "bar")
        XCTAssertEqual(_queryValue("token", in: result), "T")
    }

    // MARK: — Special characters are percent-encoded and round-trip cleanly

    func testSpecialCharsPercentEncoded() {
        let token = "a b/c+d=e&f"
        let result = WebSocketManager.appendingToken(token, to: base)
        // The raw URL string must not contain an unescaped space.
        XCTAssertFalse(result.absoluteString.contains(" "),
                       "Spaces in the token must be percent-encoded")
        // Parsed back out, the value must equal the original input exactly.
        XCTAssertEqual(_queryValue("token", in: result), token,
                       "Token must round-trip through URL encoding unchanged")
    }

    // MARK: — Helpers

    /// Extracts a query value the way a server would parse it (decoded).
    private func _queryValue(_ name: String, in url: URL) -> String? {
        URLComponents(url: url, resolvingAgainstBaseURL: false)?
            .queryItems?
            .first { $0.name == name }?
            .value
    }
}
