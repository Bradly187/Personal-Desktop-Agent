# Spec: First-Class Search + Web Tools

> One feature → one folder. This spec is the source of truth; the code is the
> build artifact. Keep this updated as the design evolves.

---

## 1. Background — the "Why"

Phase 2 of the Claude-Code capability-gap closure (gap analysis + plan approved
2026-06-25). Code search and web fetch existed only as DevAgent *plan verbs*
(`GREP`, `FETCH_URL`, `SEARCH_WEB` in `inference/dev_agent.py`): reachable only
by emitting a plan step inside the dev-agent loop, never as a direct, composable
tool. Claude Code exposes `Grep`/`Glob`/`WebFetch` as first-class primitives
callable at any time. This closes that gap on the agent's own MCP surface so
`grep`/`glob_files`/`fetch_url` are directly callable, and adds the missing
`glob` capability.

**Status:** Done (core). `grep` + `glob_files` + `fetch_url` exposed as MCP tools
backed by shared modules; `DevAgent._grep` refactored to delegate to the same
implementation (parity). **Deferred (noted, not built):** command-domain web
*enrichment* in `HybridCoordinator` (touches the routing path near the 60 Hz
loop and Privacy Gate 0 — held for a focused follow-up), and sharing the
*async* `DevAgent._fetch_url` (aiohttp, in-loop trust scan) with the sync
tool-layer `fetch_url` (the two stay separate impls, both trust-screened).
**Owner / author session:** Claude Code (Opus 4.8).
**Related:** `../accessibility-agent/` (DevAgent), `../edit-format-aci/` (sibling
Phase-1 gap work). Honors AGENTS.md #4 (fail-safe), #7 (path boundaries).

---

## 2. Glossary

- **Shared search module** (`mcp_server/tools/search.py`): `search_text` (grep)
  + `glob_paths` (glob) + `format_grep_result`. Single source of truth for both
  the `GREP` plan-verb and the `grep` MCP tool.
- **Web tool** (`mcp_server/tools/web.py`): stdlib-only sync `fetch_url`.
- **Scopes**: the writable-root allowlist (`_load_writable_roots`). When passed,
  a target outside it is refused via `core.goal_session._path_in_scope`.

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: First-class grep + glob MCP tools
1. THE MCP server SHALL expose `grep` (regex over text files) and `glob_files`
   (pathlib glob, `*` and `**`) registered in BOTH `list_tools()` and `_dispatch()`.
2. Both tools SHALL prune the shared skip-dir set (`__pycache__`, `.git`,
   `node_modules`, `.venv`, …) and return a dict (`ok`, matches/paths, `count`,
   `truncated`, `error`).
3. A bad regex, bad glob, or missing path SHALL return `ok=False` with an
   `error` and SHALL NOT raise.

### Requirement 2: Scope enforcement (deny-by-default)
1. WHEN a scope allowlist is supplied, `search_text`/`glob_paths` SHALL refuse a
   target outside it via the hardened realpath check (AGENTS.md #7), returning
   `ok=False`.
2. THE MCP `grep`/`glob_files` tools SHALL pass the writable-root allowlist, so a
   direct call cannot read outside it; resolution failure SHALL fail closed
   (empty allowlist → deny all).
3. THE in-process `DevAgent._grep` SHALL pass `scopes=None` (unrestricted),
   preserving its existing repo-wide read.

### Requirement 3: Shared implementation (no drift)
1. `DevAgent._grep` SHALL delegate to `search_text` + `format_grep_result` and
   SHALL return a byte-identical legacy string (`Found N match(es)…` /
   `No matches…` / `Path does not exist:…`).

### Requirement 4: First-class web fetch
1. THE MCP server SHALL expose `fetch_url` (http(s) only — other schemes refused,
   fail-closed) returning extracted text (HTML stripped) capped at `max_chars`.
2. A network/HTTP failure SHALL return `ok=False` gracefully (never raise).
3. THE fetched text SHALL be screened by the server's existing
   `MCPTrustClassifier` in `call_tool` (no second copy of that logic).

---

## 4. Behavior Verification (executable)

- `tests/test_search_tools.py` — grep hit/miss/regex-error/truncation, glob
  single+recursive+skip-dirs+sorted, scope refusal (both tools) + allow + None,
  DevAgent parity.
- `tests/test_web_tools.py` — scheme gate, HTML strip, non-HTML raw, truncation,
  graceful network failure (urllib mocked, no network).

---

## 5. Tasks

- [x] 1. `mcp_server/tools/search.py` — `search_text` + `glob_paths` +
      `format_grep_result`, scope-aware (R1, R2, R3).
- [x] 2. `mcp_server/tools/web.py` — sync `fetch_url`, scheme-gated (R4).
- [x] 3. Register `grep`/`glob_files`/`fetch_url` in `desktop_mcp_server.py`
      (`list_tools` + `_dispatch`); lazy writable-root scope getter; docstring.
- [x] 4. Refactor `DevAgent._grep` to delegate (parity, R3.1).
- [x] 5. `tests/test_search_tools.py` + `tests/test_web_tools.py` (24 tests).
- [ ] 6. (Deferred) command-domain web enrichment in `HybridCoordinator`
      honoring Privacy Gate 0; share async `_fetch_url`. Separate follow-up.
