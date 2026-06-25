# Spec: Browser / UI Testing Primitives

> One feature → one folder. This spec is the source of truth; the code is the
> build artifact. Keep this updated as the design evolves.

---

## 1. Background — the "Why"

Phase 3 of the Claude-Code capability-gap closure (plan approved 2026-06-25).
Claude Code ships `preview_*` tools (start a dev server, screenshot, click,
fill, inspect the DOM, read console + network) — a full loop for verifying web
UIs. This agent had only `screenshot` (mss, pixel grab) — no DOM inspection, no
click-and-verify. The agent's own chat UI (`core/chat_server.py` :8770 +
`web_client_chat/`, including the live execution-DAG pane) had **zero** automated
coverage. This was the only genuinely greenfield gap of the four (confirmed: no
playwright/selenium/puppeteer anywhere in the tree).

**Status:** Done. `preview_*` MCP tools added via `mcp_server/tools/browser.py`,
Playwright-backed, **optional dependency with graceful degradation**.
**Owner / author session:** Claude Code (Opus 4.8).
**Related:** `../first-class-search-tools/` (sibling Phase-2 MCP-surface work).
Honors AGENTS.md #4 (fail-safe / SAFE_MODE), and the CLAUDE.md degrade-gracefully
convention (optional dep wrapped in `try/except ImportError`).

---

## 2. Glossary

- **preview_\*** tools: `preview_start`, `preview_screenshot`, `preview_snapshot`
  (url/title/text), `preview_click`, `preview_fill`, `preview_console_logs`,
  `preview_network`, `preview_stop`.
- **Worker thread:** the Playwright *sync* API refuses to run inside a running
  asyncio loop, and the MCP server calls `_dispatch` synchronously from inside
  its loop. So the browser session lives in a dedicated daemon thread
  (`_BrowserWorker`) that owns the Playwright objects and serves commands off a
  queue — giving both event-loop isolation and a persistent page across calls.

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: preview_* MCP tool surface
1. THE MCP server SHALL register the 8 `preview_*` tools in BOTH `list_tools()`
   and `_dispatch()`. Each SHALL return a dict; `preview_screenshot` SHALL return
   `image_base64` so `call_tool` renders it as MCP image content.

### Requirement 2: Graceful degradation (optional dependency)
1. Playwright SHALL be optional. WHEN it (or its Chromium build) is absent, every
   `preview_*` tool SHALL return `{"ok": False, "disabled": True, "error": …}`
   with the install hint and SHALL NOT crash the server (CLAUDE.md convention).
2. Importing `mcp_server/tools/browser.py` SHALL never raise when Playwright is
   absent.

### Requirement 3: Localhost scoping + SAFE_MODE
1. `preview_start` SHALL refuse a non-localhost URL unless `allow_external=true`
   (the UI under test is local; arbitrary external navigation is out of scope and
   matches the link-safety posture). Loopback = `localhost`/`127.0.0.1`/`::1`/
   `0.0.0.0`, http(s) only.
2. `preview_click` and `preview_fill` mutate page state and SHALL be blocked in
   `SAFE_MODE` (like `keyboard_type`/`mouse_drag`). Read-only preview tools SHALL
   NOT be gated.

### Requirement 4: Event-loop isolation
1. Playwright operations SHALL run in a dedicated worker thread, never on the MCP
   server's asyncio thread; the worker SHALL survive an operation error (one bad
   call returns an error dict, the thread keeps serving).

---

## 4. Behavior Verification (executable)

- `tests/test_browser_tools.py` — registry (all 8), degradation sweep
  (Playwright-absent → disabled), import-never-crashes, `is_local_url`
  accept/reject, external-URL refusal (forced-available branch), SAFE_MODE
  gating of click/fill + non-gating of read-only tools.
- Live smoke (`test_live_preview_against_static_page`, `skipif` no Playwright):
  serve a static page on loopback, `preview_start` → `preview_snapshot` asserts
  title + body text. Runs only where Playwright + Chromium are installed.

---

## 5. Tasks

- [x] 1. `mcp_server/tools/browser.py` — `_BrowserWorker` (thread-isolated sync
      Playwright) + 8 `preview_*` functions + `is_local_url`; graceful degradation.
- [x] 2. Register the 8 tools in `desktop_mcp_server.py` (`list_tools` +
      `_dispatch`); SAFE_MODE-gate click/fill; docstring. Import shim so the
      server is importable both as a script and as a module.
- [x] 3. `requirements.txt` — Playwright as a commented optional dep + install hint.
- [x] 4. `tests/test_browser_tools.py` (23 pass + 1 skip live smoke).
