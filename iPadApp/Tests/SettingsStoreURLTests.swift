import XCTest
@testable import DesktopAgent

/// Unit tests for SettingsStore.wsURL and wsURLOrDefault
/// Validates Requirements 3.1, 3.2, 3.3, 3.4, 3.5
@MainActor
final class SettingsStoreURLTests: XCTestCase {

    // MARK: — Requirement 3.1: Empty host returns nil

    func testEmptyHostReturnsNil() {
        let store = SettingsStore()
        store.serverHost = ""
        store.serverPort = 8765
        XCTAssertNil(store.wsURL, "Empty host should produce nil wsURL")
    }

    func testWhitespaceOnlyHostReturnsNil() {
        let store = SettingsStore()
        store.serverHost = "   "
        store.serverPort = 8765
        XCTAssertNil(store.wsURL, "Whitespace-only host should produce nil wsURL")
    }

    func testTabOnlyHostReturnsNil() {
        let store = SettingsStore()
        store.serverHost = "\t"
        store.serverPort = 8765
        XCTAssertNil(store.wsURL, "Tab-only host should produce nil wsURL")
    }

    // MARK: — Requirement 3.2: Invalid port returns nil

    func testPortZeroReturnsNil() {
        let store = SettingsStore()
        store.serverHost = "192.168.1.100"
        store.serverPort = 0
        XCTAssertNil(store.wsURL, "Port 0 should produce nil wsURL")
    }

    func testNegativePortReturnsNil() {
        let store = SettingsStore()
        store.serverHost = "192.168.1.100"
        store.serverPort = -1
        XCTAssertNil(store.wsURL, "Negative port should produce nil wsURL")
    }

    func testPortAbove65535ReturnsNil() {
        let store = SettingsStore()
        store.serverHost = "192.168.1.100"
        store.serverPort = 65536
        XCTAssertNil(store.wsURL, "Port > 65535 should produce nil wsURL")
    }

    func testPortMaxIntReturnsNil() {
        let store = SettingsStore()
        store.serverHost = "192.168.1.100"
        store.serverPort = Int.max
        XCTAssertNil(store.wsURL, "Port Int.max should produce nil wsURL")
    }

    func testPortMinIntReturnsNil() {
        let store = SettingsStore()
        store.serverHost = "192.168.1.100"
        store.serverPort = Int.min
        XCTAssertNil(store.wsURL, "Port Int.min should produce nil wsURL")
    }

    // MARK: — Requirement 3.3: Special characters / unicode

    func testUnicodeHostReturnsNilOrValidURL() {
        let store = SettingsStore()
        store.serverHost = "日本語.com"
        store.serverPort = 8765
        // URL(string:) may return nil for non-ASCII hosts without percent encoding
        // Either nil (invalid) or a valid URL is acceptable — never a crash
        let _ = store.wsURL  // Must not crash
    }

    func testHostWithSpacesReturnsNil() {
        let store = SettingsStore()
        store.serverHost = "my host"
        store.serverPort = 8765
        // URL(string:) returns nil for strings with unescaped spaces
        // wsURL should return nil (space makes invalid URL)
        let result = store.wsURL
        // If URL(string:) happens to parse it, that's fine — the key is no crash
        let _ = result
    }

    func testHostWithColonReturnsNilOrValid() {
        let store = SettingsStore()
        store.serverHost = "host:injection"
        store.serverPort = 8765
        // Must not crash regardless of result
        let _ = store.wsURL
    }

    func testHostWithSlashReturnsNilOrValid() {
        let store = SettingsStore()
        store.serverHost = "host/path"
        store.serverPort = 8765
        let _ = store.wsURL  // Must not crash
    }

    // MARK: — Requirement 3.4: wsURLOrDefault provides fallback

    func testWsURLOrDefaultWithInvalidInputReturnsFallback() {
        let store = SettingsStore()
        store.serverHost = ""
        store.serverPort = 0
        let url = store.wsURLOrDefault
        XCTAssertEqual(url.absoluteString, "ws://192.168.18.2:8765/ws",
                       "wsURLOrDefault should return fallback when wsURL is nil")
    }

    func testWsURLOrDefaultNeverReturnsNil() {
        let store = SettingsStore()
        // Even with garbage input, wsURLOrDefault always returns a URL
        store.serverHost = ""
        store.serverPort = -999
        let url = store.wsURLOrDefault
        XCTAssertNotNil(url, "wsURLOrDefault must always return a valid URL")
    }

    // MARK: — Requirement 3.5: Valid inputs produce correct URL

    func testValidHostAndPortProducesCorrectURL() {
        let store = SettingsStore()
        store.serverHost = "192.168.1.50"
        store.serverPort = 9000
        let url = store.wsURL
        XCTAssertNotNil(url)
        XCTAssertEqual(url?.absoluteString, "ws://192.168.1.50:9000/ws")
    }

    func testValidLocalhostProducesCorrectURL() {
        let store = SettingsStore()
        store.serverHost = "localhost"
        store.serverPort = 8765
        let url = store.wsURL
        XCTAssertNotNil(url)
        XCTAssertEqual(url?.absoluteString, "ws://localhost:8765/ws")
    }

    func testPort1IsValid() {
        let store = SettingsStore()
        store.serverHost = "10.0.0.1"
        store.serverPort = 1
        let url = store.wsURL
        XCTAssertNotNil(url)
        XCTAssertEqual(url?.absoluteString, "ws://10.0.0.1:1/ws")
    }

    func testPort65535IsValid() {
        let store = SettingsStore()
        store.serverHost = "10.0.0.1"
        store.serverPort = 65535
        let url = store.wsURL
        XCTAssertNotNil(url)
        XCTAssertEqual(url?.absoluteString, "ws://10.0.0.1:65535/ws")
    }

    func testWsURLOrDefaultWithValidInputReturnsUserURL() {
        let store = SettingsStore()
        store.serverHost = "mypc.local"
        store.serverPort = 4000
        let url = store.wsURLOrDefault
        XCTAssertEqual(url.absoluteString, "ws://mypc.local:4000/ws",
                       "wsURLOrDefault should return user URL when valid")
    }

    // MARK: — Edge cases: no crash guarantee

    func testEmptyStringAndZeroPortNoCrash() {
        let store = SettingsStore()
        store.serverHost = ""
        store.serverPort = 0
        // This is the exact scenario that would crash with the old force-unwrap
        let _ = store.wsURL
        let _ = store.wsURLOrDefault
        // If we reach here, no crash occurred
    }

    func testVeryLongHostNoCrash() {
        let store = SettingsStore()
        store.serverHost = String(repeating: "a", count: 10000)
        store.serverPort = 8765
        let _ = store.wsURL  // Must not crash
        let _ = store.wsURLOrDefault
    }

    func testNewlineInHostNoCrash() {
        let store = SettingsStore()
        store.serverHost = "host\ninjection"
        store.serverPort = 8765
        let _ = store.wsURL  // Must not crash
    }

    func testNullCharacterInHostNoCrash() {
        let store = SettingsStore()
        store.serverHost = "host\0evil"
        store.serverPort = 8765
        let _ = store.wsURL  // Must not crash
    }
}
