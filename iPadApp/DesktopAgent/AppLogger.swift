import Foundation
import os

/// Centralised logging for the DesktopAgent iPad app.
///
/// Writes to the unified os.Logger (visible in Console.app / Instruments) AND
/// batches warning+ entries to the PC bridge over WebSocket for persistent
/// capture during soak testing.
///
/// Usage (from any thread or actor):
///   AppLogger.shared.warning("LiDARStreamer", "ARSession error — \(error)")
///   AppLogger.shared.info("WebSocketManager",  "Connected to \(url)")
///
/// Replaces all bare print() calls in sensor and network classes.
final class AppLogger: @unchecked Sendable {

    static let shared = AppLogger()

    // MARK: — Log level

    enum Level: String {
        case debug   = "debug"
        case info    = "info"
        case warning = "warning"
        case error   = "error"
        case fault   = "fault"

        var osLogType: OSLogType {
            switch self {
            case .debug:   return .debug
            case .info:    return .info
            case .warning: return .default   // os_log has no explicit "warning"; .default is appropriate
            case .error:   return .error
            case .fault:   return .fault
            }
        }

        var intValue: Int {
            switch self {
            case .debug:   return 0
            case .info:    return 1
            case .warning: return 2
            case .error:   return 3
            case .fault:   return 4
            }
        }
    }

    // MARK: — Internal entry type

    private struct Entry {
        let ts: Double
        let level: Level
        let subsystem: String
        let msg: String
    }

    // MARK: — Configuration

    /// Minimum level forwarded to the PC over WebSocket.
    /// .debug entries are written to os.Logger only (not sent over the wire).
    var minForwardLevel: Level = .info

    /// Maximum entries buffered before oldest are dropped (advisory guard).
    private let maxPending = 40

    // MARK: — Transport

    /// Weak to avoid a retain cycle with WebSocketManager.
    /// Set once at app startup via setTransport(_:).
    private weak var wsManager: WebSocketManager?

    func setTransport(_ manager: WebSocketManager) {
        wsManager = manager
    }

    // MARK: — Internal state

    private let batchQueue = DispatchQueue(label: "AppLogger.batch", qos: .utility)
    private var pending: [Entry] = []

    private var loggerCache: [String: Logger] = [:]
    private let loggerLock = NSLock()

    // MARK: — Init

    private init() {
        scheduleFlusher()
    }

    // MARK: — Public log entry points

    func debug(_ subsystem: String, _ msg: String) {
        log(.debug, subsystem, msg)
    }

    func info(_ subsystem: String, _ msg: String) {
        log(.info, subsystem, msg)
    }

    func warning(_ subsystem: String, _ msg: String) {
        log(.warning, subsystem, msg)
    }

    func error(_ subsystem: String, _ msg: String) {
        log(.error, subsystem, msg)
    }

    func fault(_ subsystem: String, _ msg: String) {
        log(.fault, subsystem, msg)
    }

    // MARK: — Core dispatch

    private func log(_ level: Level, _ subsystem: String, _ msg: String) {
        // Always write to unified logging (Console.app, Instruments, crash reports)
        let logger = cachedLogger(for: subsystem)
        logger.log(level: level.osLogType, "\(msg, privacy: .public)")

        // Queue for WebSocket forwarding if level meets the threshold
        guard level.intValue >= minForwardLevel.intValue else { return }

        let entry = Entry(ts: Date().timeIntervalSince1970, level: level,
                          subsystem: subsystem, msg: msg)
        batchQueue.async { [weak self] in
            guard let self else { return }
            if self.pending.count >= self.maxPending {
                self.pending.removeFirst()  // drop oldest; logs are advisory
            }
            self.pending.append(entry)
        }
    }

    // MARK: — os.Logger cache

    private func cachedLogger(for subsystem: String) -> Logger {
        loggerLock.lock()
        defer { loggerLock.unlock() }
        if let existing = loggerCache[subsystem] { return existing }
        let l = Logger(subsystem: "com.desktopagent", category: subsystem)
        loggerCache[subsystem] = l
        return l
    }

    // MARK: — Batch flush

    private func scheduleFlusher() {
        batchQueue.asyncAfter(deadline: .now() + 0.5) { [weak self] in
            self?.flush()
            self?.scheduleFlusher()
        }
    }

    private func flush() {
        guard !pending.isEmpty, let ws = wsManager else { return }

        // G6: Only flush when connected. If not connected, keep pending entries
        // so startup logs (permissions, sensor init) are not silently dropped.
        // The next 500 ms flush cycle will retry.
        //
        // Concurrency Bug 1 fix: snapshot the count and remove ONLY that many
        // when delivery completes. New entries that arrived on batchQueue between
        // the snapshot and the clear must survive — they go out in the next flush.
        let snapshotCount = pending.count
        let entries: [[String: Any]] = pending.prefix(snapshotCount).map { e in
            ["ts": e.ts, "level": e.level.rawValue, "subsystem": e.subsystem, "msg": e.msg]
        }
        Task { @MainActor [weak self, weak ws] in
            guard let ws else { return }
            guard case .connected = ws.state else { return }
            ws.sendLogBatch(entries)
            self?.batchQueue.async { [weak self] in
                guard let self else { return }
                let removeCount = min(snapshotCount, self.pending.count)
                self.pending.removeFirst(removeCount)
            }
        }
    }
}
