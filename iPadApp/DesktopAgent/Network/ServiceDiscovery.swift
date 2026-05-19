import Foundation
import Network

/// Discovers the PC bridge on the local network via Bonjour/mDNS.
/// Browses for `_desktop-agent._tcp` and resolves the endpoint to host:port.
@MainActor
final class ServiceDiscovery: ObservableObject {

    @Published var discoveredHost: String?
    @Published var discoveredPort: Int?
    @Published var isSearching: Bool = false

    private var browser: NWBrowser?
    private var resolveConnection: NWConnection?
    private var timeoutTask: Task<Void, Never>?

    /// Whether the user has manually configured a host (non-default).
    /// When true, mDNS discovery results are informational only.
    var hasManualOverride: Bool = false

    // MARK: — Public API

    /// Start browsing for `_desktop-agent._tcp` services on the local network.
    func startBrowsing() {
        // Don't restart if already browsing
        guard browser == nil else { return }

        let params = NWParameters()
        params.includePeerToPeer = true

        browser = NWBrowser(for: .bonjour(type: "_desktop-agent._tcp", domain: nil), using: params)

        browser?.stateUpdateHandler = { [weak self] state in
            Task { @MainActor in
                guard let self else { return }
                switch state {
                case .ready:
                    self.isSearching = true
                case .failed, .cancelled:
                    self.isSearching = false
                default:
                    break
                }
            }
        }

        browser?.browseResultsChangedHandler = { [weak self] results, _ in
            Task { @MainActor in
                guard let self else { return }
                guard let result = results.first else { return }
                if case .service(let name, let type, let domain, _) = result.endpoint {
                    self.resolve(name: name, type: type, domain: domain)
                }
            }
        }

        browser?.start(queue: .main)

        // Start 5-second timeout
        startTimeout()
    }

    /// Stop browsing and release all resources.
    func stopBrowsing() {
        timeoutTask?.cancel()
        timeoutTask = nil

        browser?.cancel()
        browser = nil

        resolveConnection?.cancel()
        resolveConnection = nil

        isSearching = false
    }

    // MARK: — Resolution

    /// Resolve a discovered service name to a concrete host:port via NWConnection.
    private func resolve(name: String, type: String, domain: String) {
        // Cancel any previous resolution attempt
        resolveConnection?.cancel()

        let connection = NWConnection(
            to: .service(name: name, type: type, domain: domain, interface: nil),
            using: .tcp
        )

        resolveConnection = connection

        connection.stateUpdateHandler = { [weak self] state in
            switch state {
            case .ready:
                if let endpoint = connection.currentPath?.remoteEndpoint,
                   case .hostPort(let host, let port) = endpoint {
                    Task { @MainActor in
                        guard let self else { return }
                        self.discoveredHost = "\(host)"
                        self.discoveredPort = Int(port.rawValue)
                        // Cancel timeout since we found a service
                        self.timeoutTask?.cancel()
                        self.timeoutTask = nil
                    }
                }
                connection.cancel()
            case .failed:
                connection.cancel()
            default:
                break
            }
        }

        connection.start(queue: .global())
    }

    // MARK: — Timeout

    /// After 5 seconds with no discovery, stop searching.
    /// The caller (WebSocketManager) will fall back to manual host/port.
    private func startTimeout() {
        timeoutTask?.cancel()
        timeoutTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 5_000_000_000) // 5 seconds
            guard !Task.isCancelled else { return }
            guard let self else { return }
            // If we still haven't found anything, stop searching
            if self.discoveredHost == nil {
                self.isSearching = false
            }
        }
    }
}
