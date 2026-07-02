# Desktop Agent Shell

Electron shell unifying the agent's chat UI and dashboard (embedded from
`http://127.0.0.1:8770`) with a whole-filesystem file tree, a Monaco editor,
and a PowerShell terminal (node-pty/ConPTY). Spec: `../specs/desktop-app-shell/`.

```
npm install    # also rebuilds node-pty against Electron's ABI (scripts/rebuild-pty.js)
npm start
```

No bundler, no build step: Monaco is pinned to 0.52.2 (last AMD build) and
xterm to 5.x (last UMD build) so everything loads from `node_modules` via
plain script tags, offline, per repo convention.

## Auth

Every ChatServer route except `/health` needs the token from
`~/.claude/chat_server/token`. Its cookie is `SameSite=Strict`, which never
crosses an iframe boundary — so the main process injects `X-Agent-Token` on
every request to `127.0.0.1:8770` (including WebSocket handshakes) via
`session.webRequest.onBeforeSendHeaders`. The token never reaches the
renderer or the embedded pages.

## Backend lifecycle

- Backend already healthy at launch → **attached**: the shell never kills it.
- Backend down → the shell spawns `.venv\Scripts\python.exe main.py --chat
  --chat-no-browser` and **owns** it: quit tears down the process tree
  (`taskkill /T` — the venv launcher's child holds the ports).
- An owned child that dies is reported as **down** with the log tail
  (`logs/desktop_app_backend.log`); restart policy stays with the watchdog.

### Watchdog interplay

The logon scheduled task (`scripts/agent_watchdog.ps1`) also starts
`main.py --chat`. Both orders are safe: watchdog-first → the shell attaches;
shell-first → the watchdog's port-8765 guard exits cleanly. A simultaneous
logon race self-limits (the losing python fails to bind; the watchdog gives up
after 3 crashes). **If you adopt the shell as your entry point, disable the
logon task** (`Disable-ScheduledTask`) — otherwise the shell will nearly always
run in attached mode and owned-mode log capture never engages.

## Smoke test

```
SHELL_SMOKE=1 npx electron .
```

Loads the shell, waits ~25 s, prints a `SMOKE_RESULT` JSON line (iframe srcs,
Monaco/xterm/tree/pty state, fs round-trip, HTTP status tally — the 401 check),
saves a screenshot to `SHELL_SMOKE_OUT`, and quits.

Known cosmetic noise on quit: node-pty's `conpty_console_list_agent.js` may
print `AttachConsole failed` during teardown — harmless.
