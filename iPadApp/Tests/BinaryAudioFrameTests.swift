import XCTest
@testable import DesktopAgent

/// Unit tests for `WebSocketManager.frameBinaryAudio(_:)` — the binary audio
/// frame builder (PR-3). The PC bridge parses byte 0 as the message tag and the
/// remainder as little-endian int16 PCM, so the framing must put the tag first
/// and leave the payload byte-for-byte intact.
@MainActor
final class BinaryAudioFrameTests: XCTestCase {

    func testFramePrependsAudioTag() {
        let pcm = Data([0x10, 0x20, 0x30, 0x40])
        let framed = WebSocketManager.frameBinaryAudio(pcm)
        XCTAssertEqual(framed.first, WebSocketManager.binTagAudioPCM16)
        XCTAssertEqual(framed.first, 0x01, "Tag must match the bridge's _BIN_TAG_AUDIO_PCM16")
    }

    func testFramePreservesPayloadBytes() {
        let pcm = Data([0x10, 0x20, 0x30, 0x40])
        let framed = WebSocketManager.frameBinaryAudio(pcm)
        XCTAssertEqual(framed.count, pcm.count + 1, "Exactly one tag byte is added")
        XCTAssertEqual(framed.dropFirst(), pcm, "Payload must follow the tag unchanged")
    }

    func testFrameEmptyPayloadIsJustTag() {
        let framed = WebSocketManager.frameBinaryAudio(Data())
        XCTAssertEqual(framed, Data([WebSocketManager.binTagAudioPCM16]))
    }

    func testFrameLengthGivesWholeInt16FrameCount() {
        // 800 frames * 2 bytes = 1600-byte payload (a 50 ms chunk at 16 kHz).
        let pcm = Data(count: 1600)
        let framed = WebSocketManager.frameBinaryAudio(pcm)
        // The bridge computes frames = (len - 1) / 2 from the framed bytes.
        XCTAssertEqual((framed.count - 1) / 2, 800)
    }
}
