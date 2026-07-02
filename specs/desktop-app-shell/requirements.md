# Spec: Desktop App Shell (Electron)

---

## 1. Background — the "Why"

Brad drives the agent through the browser chat UI (`:8770/`) and dashboard (`:8770/dashboard`), but file browsing, text editing, and terminal work still require juggling separate apps — extra window management that is costly with rheumatoid arthritis. This spec unifies them in one Electron desktop shell: the existing web UIs embedded unchanged, plus a file tree, a Monaco editor, and a pty terminal, with RA-friendly targets and keyboard shortcuts throughout. The shell also becomes the app-like entry point for the backend itself (owns `main.py --chat` when nothing else started it).

Related: `../chat-workbench-parity/` (the chat UI this shell embeds), `../dashboard-observability-gaps/` (the dashboard it embeds).

**Status:** Shipped (PR #160)
**Approved:** Brad, 2026-07-02 (spec + tasks approved in-session via plan approval; layout, whole-filesystem scope, and backend-lifecycle-owner decisions confirmed by Brad in the same session)
**Owner / author session:** Claude Code

---

## 2. Glossary

- **Shell**: the Electron application in `desktop_app/` — one `BrowserWindow`, no bundler, no build step.
- **ChatServer**: the aiohttp server in `core/chat_server.py` on `127.0.0.1:8770`; serves `/` (chat), `/dashboard`, `/chat` (WebSocket), `/api/*`, `/static/*`; token-gated on every route except `/health`.
- **Agent token**: the secret at `~/.claude/chat_server/token`; accepted as `X-Agent-Token` header, `?token=` query, or `da_chat_token` cookie (HttpOnly, SameSite=Strict).
- **Header injection**: Electron main-process `session.webRequest.onBeforeSendHeaders` adding `X-Agent-Token` to every request to `127.0.0.1:8770` (HTTP and WS handshake).
- **Backend mode**: the Shell's view of the Python backend — `down` | `starting` | `owned` (Shell spawned it, Shell kills it) | `attached` (already running, e.g. via watchdog; Shell never kills it).
- **Watchdog**: `scripts/agent_watchdog.ps1`, run by the logon scheduled task; starts `main.py --chat` and restarts it on non-zero exit; its already-running guard (port 8765 listen check) runs once at watchdog start.
- **Preload API**: the `window.agent` surface exposed via `contextBridge` — the only privileged access the renderer has (`nodeIntegration: false`, `contextIsolation: true`).

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: Embedded chat + dashboard with transparent auth

**User Story:** As Brad, I want the existing chat and dashboard inside the shell, so that I keep one window without re-authenticating.

#### Acceptance Criteria
1. THE Shell SHALL embed `http://127.0.0.1:8770/` (right sidebar) and `http://127.0.0.1:8770/dashboard` (center Dashboard tab) as iframes, unmodified.
2. THE Shell SHALL inject `X-Agent-Token` on every request to `127.0.0.1:8770` (including the `/chat` WebSocket handshake) in the main process; the token SHALL never be exposed to the renderer or iframes.
3. WHEN the token file changes on disk, THE Shell SHALL reload it without restart.
4. IF the token file is missing, THEN THE Shell SHALL show a "token missing" banner and keep running (iframes may 401; no crash).
5. WHEN the center tab switches away from Dashboard, THE Shell SHALL hide (not unmount) the iframes, so the chat WebSocket survives.
6. THE Shell SHALL set iframe `src` only after `/health` reports the backend up.

### Requirement 2: Backend lifecycle ownership

**User Story:** As Brad, I want the shell to start and stop the agent like a normal app, so that launching one icon brings everything up.

#### Acceptance Criteria
1. WHEN the Shell starts and `GET /health` succeeds, THE Shell SHALL enter `attached` mode and SHALL NOT terminate that backend on quit.
2. WHEN the Shell starts and `/health` fails, THE Shell SHALL spawn `.venv\Scripts\python.exe main.py --chat --chat-no-browser` (cwd = repo root), stream stdout/stderr to `logs/desktop_app_backend.log` and a renderer log tail, and enter `owned` mode once `/health` succeeds (poll up to 120 s).
3. WHEN the Shell quits in `owned` mode, THE Shell SHALL terminate the backend process tree (`taskkill /PID <pid> /T /F` — the venv launcher's child worker holds the ports).
4. IF the owned child exits unexpectedly, THEN THE Shell SHALL show `down` state with the log tail visible; it SHALL NOT auto-restart (the watchdog owns restart policy).
5. THE backend Stop control SHALL be disabled in `attached` mode.
6. THE `main.py` `--chat-no-browser` flag SHALL suppress `_open_chat_shell` (no browser tab when the Shell is the launcher); default behavior without the flag is unchanged.

### Requirement 3: File tree + editor

**User Story:** As Brad, I want to browse any drive and edit files with large, forgiving targets, so that I don't need a separate editor for quick changes.

#### Acceptance Criteria
1. THE file tree SHALL list all Windows drive roots and lazy-load directory children on expand; row targets SHALL be ≥ 28 px tall.
2. WHEN a file is clicked, THE Shell SHALL open it in a Monaco tab (models per file, language from extension).
3. IF a file exceeds 5 MB or contains a NUL byte in its first 8 KB, THEN THE Shell SHALL refuse or warn instead of opening it.
4. WHEN Ctrl+S is pressed on a dirty tab, THE Shell SHALL write the file; IF the file's mtime changed since open, THEN it SHALL prompt before overwriting.
5. IF a directory read fails (access denied), THEN THE tree SHALL render an inline error row, not crash.
6. WHEN a dirty tab is closed, THE Shell SHALL prompt before discarding changes.

### Requirement 4: Terminal

**User Story:** As Brad, I want a terminal in the bottom panel, so that shell work happens in the same window.

#### Acceptance Criteria
1. THE terminal SHALL run PowerShell via node-pty (ConPTY) wired to xterm.js, font ≥ 15 px, default cwd = repo root.
2. WHEN the panel resizes, THE terminal SHALL refit and propagate cols/rows to the pty.
3. WHEN the pty exits, THE terminal SHALL print an exit banner and respawn on Enter.
4. WHEN the window closes, THE Shell SHALL kill all pty sessions.

### Requirement 5: Layout + accessibility

**User Story:** As Brad, I want resizable panels I can operate without precise mouse control, so that the shell works on flare days.

#### Acceptance Criteria
1. THE Shell SHALL use the layout: left file tree, center tabbed Dashboard/editor, bottom terminal, right chat.
2. THE splitters SHALL have a total hit area ≥ 20 px wide (8 px visible + ±6 px extension), support pointer-capture drag with panel minimums, double-click reset, and arrow-key resize (24 px steps) when focused.
3. THE Shell SHALL persist splitter sizes and open-tab state across restarts (localStorage).
4. THE Shell SHALL provide keyboard shortcuts: `Ctrl+1..9` (tabs), `Ctrl+PgUp/PgDn` (cycle), `Ctrl+W` (close tab), `Ctrl+S` (save), ``Ctrl+` `` (focus terminal), `Ctrl+B` (toggle tree); tab-switch shortcuts SHALL also be registered as Menu accelerators so they work while an iframe has focus.
5. FOR ALL clickable controls (tabs, tree rows, buttons), targets SHALL be ≥ 28 px in the smaller dimension.

---

## 4. Technical Design

- **Entry point:** standalone Electron app `desktop_app/` (npm project, precedent: `tts_service/`, `desktop-agent-bridge/`). Not part of the Python pipeline; no `Command` DTO involvement; no 60 Hz loop contact (AGENTS.md #2).
- **Security baseline:** `nodeIntegration: false`, `contextIsolation: true`, `sandbox: false` (Monaco file:// workers); all privileged operations via preload `window.agent` IPC.
- **Auth:** header injection in main process (see Glossary). Zero ChatServer changes — its middleware already prefers the header (`core/chat_server.py:462-489`).
- **Backend change (the only one):** `--chat-no-browser` argparse flag in `main.py` guarding the `_open_chat_shell` call.
- **No bundler:** Monaco pinned to `0.52.2` (last release shipping the AMD `min/vs` build; 0.53+ is ESM-only), loaded via its AMD loader with a data-URL worker proxy; xterm pinned to 5.x (UMD builds). All assets from local `node_modules` — no CDN (repo convention).
- **node-pty:** rebuilt against Electron ABI via `@electron/rebuild` postinstall; Win11 ConPTY, no winpty.
- **Persistence:** none in `agent.db`; renderer state in localStorage; backend log at `logs/desktop_app_backend.log`.
- **Watchdog interplay (documented, not automated):** watchdog-first → Shell attaches; Shell-first → watchdog's guard sees port 8765 and exits 0; simultaneous logon start → the losing python fails to bind, watchdog self-limits (3 crashes → clean give-up). Recommendation when adopting the Shell as entry point: `Disable-ScheduledTask` on the logon task. Orphan after a Shell crash: next launch attaches via `/health`.

### Configuration (flat YAML — v1 constants, in-code)

```yaml
desktop_app_shell:
  backend_url: http://127.0.0.1:8770
  health_poll_max_s: 120
  file_open_warn_bytes: 5242880   # 5 MB
  binary_sniff_bytes: 8192
  terminal_shell: powershell.exe
  log_tail_lines: 500
```

### Non-goals (v1)
- Packaging/installer (electron-builder) — run via `npm start`.
- Graceful backend shutdown endpoint (taskkill /F is crash-safe: SQLite WAL).
- Pidfile re-adopt of an orphaned owned backend (attach covers it).
- Agent-driven control of the shell (it is a user-driven surface; approval gates unaffected).

---

## 5. Behavior Verification

The Shell is a GUI npm subproject outside the Python eval harness; verification is the manual checklist in `tasks.md` plus:

- **Unit test (Python):** `tests/test_chat_no_browser_flag.py` — argparse accepts `--chat-no-browser`; flag suppresses `_open_chat_shell` (R2.6).
- **Live checks:** zero 401s in devtools Network with both iframes active (R1.2); attached-mode quit leaves `/health` at 200 (R2.1); owned-mode spawn/kill verified post-merge when the backend is stopped (R2.2–R2.3).

---

## 6. Tasks

Promoted to [`tasks.md`](tasks.md).
