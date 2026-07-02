# Spec: DevAgent Egress Controls (CG-1)

---

## 1. Background — the "Why"

DevAgent's `_fetch_url` verb currently has no outbound restrictions, meaning it can hit any scheme and any IP. This introduces an SSRF vulnerability where a malicious or hallucinated prompt could exfiltrate data or interact with local services like the chat server (`:8770`), Ollama (`:11434`), or the bridge (`:8765`). While we have inbound taint screening (MCPTrustClassifier), lacking an outbound allowlist is a significant security gap compared to leading agents (Codex, Claude Code, Antigravity). This small, fail-closed fix implements those outbound restrictions.

**Status:** In Progress
**Approved:** Brad, 2026-07-02
**Owner / author session:** Antigravity

---

## 2. Glossary

- **EgressController**: A lightweight security component that intercepts network requests from DevAgent verbs, enforcing scheme allowlists and IP blocklists (private/loopback).

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: Scheme Allowlist

**User Story:** As Brad, I want DevAgent's web verbs to only use safe protocols so that local OS behaviors (like `file://`) cannot be invoked remotely.

#### Acceptance Criteria
1. THE `EgressController` SHALL reject any `_fetch_url` request with a scheme other than `http` or `https`.
2. WHEN an invalid scheme is requested, THE `EgressController` SHALL return a fail-closed error message without executing the request.

### Requirement 2: Private-IP and Loopback Blocking

**User Story:** As Brad, I want DevAgent to be blocked from accessing local network services, so that it cannot manipulate the chat server, bridge, or Ollama via SSRF.

#### Acceptance Criteria
1. THE `EgressController` SHALL resolve the target hostname before fetching.
2. THE `EgressController` SHALL reject the request if the resolved IP is in the RFC-1918 private space or the loopback block (`127.0.0.0/8`, `::1`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`).
3. IF DNS resolution fails or times out, THEN THE `EgressController` SHALL fail-safe to DENY the request.

### Requirement 3: Terminal Network Heuristic Review

**User Story:** As Brad, I want terminal commands to respect the same outbound network restrictions when `allow_network` is heuristically granted.

#### Acceptance Criteria
1. THE `CommandExecutor` SHALL apply the same private-IP deny-list to its `command_needs_network` auto-grant heuristic (if practical at the sandbox level) or explicitly document why the terminal sandbox handles this separately.

---

## 4. Technical Design

- **Entry point / pipeline boundary:** `DevAgent._fetch_url` (and any other HTTP-fetching verbs).
- **New `Command` fields (if any):** None.
- **Models / VRAM:** N/A (rule-based security gate).
- **Persistence:** N/A.

### Configuration (flat YAML)

```yaml
dev_agent_egress:
  enabled: true          # Security feature, default ON once shipped
  allowed_schemes:
    - http
    - https
  block_private_ips: true
```

---

## 5. Behavior Verification (executable, not prose)

- **Unit/integration tests:** 
  - `tests/test_dev_agent_egress.py`: 
    - Assert R1.1/R1.2: `file:///etc/passwd` fails.
    - Assert R2.2: `http://localhost:8770/` and `http://192.168.1.1/` fail.
    - Assert R2.3: Unresolvable host fails safely.
- **Eval suite:** No new model behaviors, but should run the baseline harness to ensure standard `FETCH_URL` tasks for public sites still pass.

---

## 6. Tasks

- [ ] 1. Implement `EgressController` in `core/` or `utils/` with IP resolution and checking (R1, R2).
- [ ] 2. Wire `EgressController` into `DevAgent._fetch_url`.
- [ ] 3. Audit `CommandExecutor` sandbox proxy/firewall for equivalent outbound rules (R3).
- [ ] 4. Add unit tests for localhost, private IP, and invalid schemes.
