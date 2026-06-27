# Spec: Interactive Terminal MCP (PTY)

---

## 1. Background — the "Why"

Currently, the `RUN_TERMINAL` verb executes commands synchronously (blocking until completion or timeout) and captures `stdout`/`stderr`. This fails for long-running processes (e.g., test runners, dev servers) or interactive prompts (e.g., REPLs, interactive CLI tools). Advanced coding agents can spawn background pseudoterminal (PTY) sessions, send input, and read streaming output. This capability bridges the gap, allowing the DevAgent to interact with standard developer workflows in real-time.

**Status:** Draft
**Owner / author session:** Antigravity

---

## 2. Glossary

- **PTY Session**: A spawned pseudoterminal process running in the background.
- **Interactive Terminal Tools**: A suite of MCP tools (`spawn_process`, `send_input`, `read_stream`) that interface with the PTY Session.

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: Spawning Background Processes
**User Story:** As the DevAgent, I want to start a long-running process in the background, so that I can interact with it over time.

#### Acceptance Criteria
1. THE `Interactive Terminal Tools` SHALL expose a `spawn_process(command)` tool.
2. WHEN `spawn_process` is called, THE system SHALL spawn the command in a PTY, assign it a unique session ID, and return immediately.
3. THE spawned process SHALL respect the same WSL-routing and bubblewrap sandboxing constraints as `RUN_TERMINAL`.

### Requirement 2: Reading Process Output
**User Story:** As the DevAgent, I want to read the latest output from a running process, so that I can see logs or prompt responses.

#### Acceptance Criteria
1. THE `Interactive Terminal Tools` SHALL expose a `read_stream(session_id, max_lines)` tool.
2. WHEN `read_stream` is called, THE system SHALL return the buffered output from the PTY since the last read.

### Requirement 3: Sending Process Input
**User Story:** As the DevAgent, I want to send text input to a running process, so that I can answer interactive prompts or type in a REPL.

#### Acceptance Criteria
1. THE `Interactive Terminal Tools` SHALL expose a `send_input(session_id, text)` tool.
2. WHEN `send_input` is called, THE system SHALL write the text to the standard input of the PTY session.

---

## 4. Technical Design

- **Entry point / pipeline boundary:** `mcp_server/desktop_mcp_server.py` and `mcp_server/tools/terminal.py`.
- **New `Command` fields (if any):** None.
- **Models / VRAM:** No new models required.
- **Persistence:** None.

### Configuration (flat YAML)

```yaml
interactive_terminal:
  enabled: true
  max_concurrent_sessions: 3
  timeout_idle_s: 600
```

---

## 5. Behavior Verification (executable, not prose)

- **Eval suite:** Add cases to `evals/suites/interactive_pty.jsonl` testing a python REPL session (spawn -> read prompt -> send 1+1 -> read 2).
- **Unit/integration tests:** `tests/test_interactive_pty.py` validating PTY lifecycle, WSL sandbox wrapper, and read/write buffering.

---

## 6. Tasks

- [ ] 1. Implement PTY session manager (`core/pty_manager.py`) (R1.1, R1.2)
- [ ] 2. Integrate existing WSL/bubblewrap sandboxing into the PTY spawner (R1.3)
- [ ] 3. Implement `spawn_process`, `read_stream`, and `send_input` MCP tools (R1, R2, R3)
- [ ] 4. Add unit tests and eval cases
- [ ] 5. Update `CLAUDE.md` Action Vocabulary
