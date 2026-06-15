# Regression Test — 2026-05-27

> **⚠️ Historical snapshot — superseded.** Point-in-time review of the 2026-05-25 commits.
> Several premises are already stale (e.g. `websockets` is now pinned `16.0`, not `14.2`).
> Do not treat findings here as current. For live state see `CLAUDE.md` and the later
> audits in this folder (`2026-06-12-gap-security-analysis.md`, `2026-06-14-audit-and-sprint-plan.md`).
> Archived from the repo root to `docs/audits/` on 2026-06-15.

Automated review of the most recent day of commits (2026-05-25; there were no
commits dated 2026-05-26). Scope: regressions, logic errors, concurrency,
dependency problems, and security. Logs reviewed: `logs/agent_startup.log`.

## Commits reviewed

| Hash | Summary |
|------|---------|
| `9bc6605` | chore: .gitignore, raise VAD floor, docs/diagrams |
| `f0ed95b` | docs: Azure AI Foundry handoff |
| `edae1c8` | feat: ML observability + RAG pipeline (metrics, dashboard, analytics, codebase index) |
| `729afaa` | docs: ML pipeline diagrams |
| `52dfe58` | feat: hardware + IDE integration (Kiro bridge, llama.cpp, git verbs, file watcher) |
| `790ad1f` | refactor(model_router): plan→qwen3-coder:30b+thinking, general→gemma3:27b |
| `570d805` | VisionGrounder→local qwen3-vl:30b; Chatterbox TTS; Continue.dev |

Verification performed: `py_compile` of all 14 changed Python modules (pass),
runtime import of the 5 new modules (`metrics`, `session_analyzer`, `dashboard`,
`codebase_indexer`, `kiro_client`) (pass), targeted API checks against the
installed `websockets` and `concurrent.futures` libraries, and a scan of the
running agent's startup log.

No fixes were applied — this is a findings report, per the scheduled-task spec.

---

## HIGH — functional bugs

### H1. KiroClient reconnects + leaks a socket on every request (`kiro_client.py`, 52dfe58)
`_do_request` guards connection reuse with:
```python
if self._ws is None or getattr(self._ws, "closed", True):
```
`requirements.txt` pins `websockets==14.2`, but the installed runtime is
**websockets 16.0**, where `websockets.connect()` returns a
`websockets.asyncio.client.ClientConnection` that has **no `.closed` attribute**
(verified: `hasattr(ClientConnection, "closed") == False`; it exposes `.state`
and `.close_code` instead). `getattr(..., "closed", True)` therefore always
returns `True`, so:
- a brand-new WebSocket is opened on *every* `_request`, and
- the previous connection is dropped without `close()` → socket/task leak.

Only manifests when the Kiro/VS Code bridge extension is actually running on
:8767 (otherwise `_available` short-circuits). **Fix:** test
`self._ws is None or self._ws.state is not State.OPEN` (or wrap send/recv and
reconnect on `ConnectionClosed`), and align the pinned `websockets` version with
reality.

### H2. Two metrics gauges never populate (`hybrid_coordinator.py`, edae1c8)
At command-outcome time the coordinator records:
```python
whisper_logprob=cmd.params.get("whisper_logprob") if cmd.params else None,
gesture_conf=cmd.params.get("gesture_conf")   if cmd.params else None,
```
But these values live on the `Command` **dataclass fields**
(`cmd.whisper_logprob`, `cmd.gesture_confidence`), not inside `cmd.params`.
Nothing ever writes the keys `"whisper_logprob"`/`"gesture_conf"` into `params`.
Result: the `whisper_logprob_ema` and `gesture_conf_ema` gauges (and the
dashboard "SENSORS" panel that reads them) are permanently `N/A`.
**Fix:** `getattr(cmd, "whisper_logprob", None)` and
`getattr(cmd, "gesture_confidence", None)`.

---

## MEDIUM

### M1. Blocking `pyautogui.position()` inside the 60 Hz tick loop (`fusion_engine.py:1277`, edae1c8)
The new ~1 Hz telemetry sampler reads the cursor with:
```python
# Cursor position — read pyautogui off the event loop to avoid latency
import pyautogui as _pag
_pos = _pag.position()
```
The comment says "off the event loop," but the call runs **synchronously inside
the tick loop**. `pyautogui.position()` is a Win32 round-trip and the loop's
budget is 16.7 ms. The startup log shows two `FusionEngine slow tick`
warnings (116.6 ms, 231.8 ms) since this landed. **Fix:** move the read into
`asyncio.to_thread`, or source the cursor position from an already-cached value
rather than calling Win32 on the hottest loop in the system.

### M2. `rms_ambient` telemetry is always NULL — dead wiring (`main.py` + `fusion_engine.py`, edae1c8)
`FusionEngine.set_acoustic_profiler()` was added and the telemetry row carries
an `rms_ambient` column, but `main.py` never calls
`fusion.set_acoustic_profiler(profiler)` (only `whisper.` and `twin_state.` get
it). `self._acoustic_profiler` stays `None`, so `rms_ambient` is never written.
Not a crash; a silent data gap in the new `sensor_telemetry` table that
`SessionAnalyzer` also reports on (`_mean_rms` → None). **Fix:** add the one
wiring line in `main.py` next to the other `fusion.set_*` calls.

### M3. `--dashboard` never shows the curses TUI; can crash-spam in plain mode (`dashboard.py`, edae1c8)
`Dashboard.start()` (the in-process path used by `--dashboard`) only ever
launches `_log_loop()` → `PlainTextRenderer`. The `CursesRenderer` is reachable
only from the standalone `python dashboard.py` CLI; the `_use_curses` flag is set
to `None` and never consulted. Separately, `PlainTextRenderer.render` formats
`f"{g.get('latency_ema_ms', 0):.0f}ms"`, but the gauge is initialised to `None`
(not absent), so before the first command it raises
`TypeError: unsupported format string`. It is swallowed by the loop's
`try/except`, but it logs a warning every tick until a command routes.
**Fix:** coalesce `None`→0 in the formatters; decide whether `--dashboard` should
attempt curses.

### M4. New model lineup is a hard runtime dependency that isn't satisfied (790ad1f, 570d805)
`model_router` now points `code`/`plan`→`qwen3-coder:30b`, `general`→`gemma3:27b`,
`vision`→`qwen3-vl:30b`, and `VisionGrounder` defaults to local
`qwen3-vl:30b` over Ollama. The startup log shows **Ollama unreachable**
(`WinError 10061`, connection refused) repeatedly. If Ollama isn't running or
those tags aren't pulled, every dev-domain inference and local vision grounding
fails (vision grounder falls back to Anthropic; the rest return CLARIFY/error).
Also note VRAM headroom: baseline 8.3 GB + Whisper 4.2 GB leaves ~19 GB, so
`qwen3-coder:30b` (17.3 GB) or `gemma3:27b` (16.2 GB) fit individually, but
`code`/`plan` (17.3 GB) and `vision` (18.2 GB) **cannot co-reside** — expect a
model swap whenever the domain alternates between them. Action: confirm
`ollama pull` of all four tags and that the Ollama service auto-starts.

---

## LOW / observations

- **L1. VisionGrounder cold-start latency (570d805).** The Ollama path uses a
  10 s `urllib` timeout (correctly wrapped in `asyncio.to_thread`, so the loop
  isn't blocked). On a cold `qwen3-vl:30b` load, a CLICK target resolution can
  stall up to 10 s before falling back. Warm latency (~0.4 s) is fine.

- **L2. Autonomous git/GitHub verbs bypass the approval gate (`dev_agent.py`, 52dfe58).**
  `GIT_COMMIT` runs `git add -u` + `git commit`, `GITHUB_PR` runs `gh pr create`,
  driven directly by planner output via `subprocess`. These do **not** pass
  through `approval_hook.py` (which only gates MCP tool calls). Args are passed
  as argv lists (no `shell=True`) so there's **no shell-injection risk**, but a
  voice-misheard plan could commit or open a PR with no confirmation. Consider
  routing these through the existing voice-approval gate.

- **L3. Kiro bridge has no auth (`kiro-extension/src/extension.ts`, 52dfe58).**
  The WebSocket server binds to `127.0.0.1:8767` (loopback only — good) but has
  no token. Any local process can `apply_edit` or `run_terminal`
  (`terminal.sendText(command)`). Acceptable for a single-user machine; flagged
  for awareness if the bind host ever changes.

- **L4. `websockets` version drift.** `requirements.txt` pins `14.2`; installed
  is `16.0`. Beyond H1, pin-vs-reality drift can mask other API changes.

- **L5. KiroClient response correlation.** `_do_request` assumes the first
  `recv()` is the reply to the request it just sent (no `id` matching). Safe
  today because an `asyncio.Lock` serialises requests, but fragile if the bridge
  ever pushes unsolicited messages.

- **L6. `SessionAnalyzer` reads agent.db via DuckDB ATTACH while the aiosqlite
  connection is still open** (run before `shutdown()`). agent.db is WAL +
  `busy_timeout=5000`, and the call is wrapped in try/except (non-fatal), so this
  is tolerable, but it's a known fragility of the DuckDB SQLite reader against a
  live WAL database.

- **L7. Process pool is never explicitly shut down (`codebase_indexer.py`,
  52dfe58).** `_CPU_POOL` is a module global reclaimed only at interpreter exit
  (atexit). Intentional reuse; noted for completeness. The `_broken` private
  attribute it checks does exist on `ProcessPoolExecutor` (verified), so the
  guard is safe.

---

## Items checked and found OK

- All 14 changed Python modules compile and the 5 new modules import cleanly.
- `LlamaCppInference` (new) reuses the existing `_SYSTEM_PROMPT` and few-shot key
  contract (`command_text`/`action_text`) consistently — no KeyError risk.
- `dev_agent` git/PR/URL verbs use argv-list `subprocess` calls — no shell
  injection. `FETCH_URL` strips scripts/styles before returning text.
- `codebase_indexer` chunking now correctly accumulates real chunk counts
  (`total_chunks += n`); process-pool chunkers pass picklable `str` args and
  return picklable `Chunk` dataclasses.
- `_check_kiro` HTTP-probe logic correctly treats a non-"connection refused"
  response as "server up."
- `Metrics` write paths hold a lock; all pipeline call sites wrap metrics calls
  in try/except (non-fatal).
- VisionGrounder Ollama→Anthropic fallback chain is correct; blocking call is
  off-loop via `asyncio.to_thread`.

---

## Recommended fixes (priority order)
1. **H1** KiroClient `.closed` → `.state`/`ConnectionClosed`; reconcile websockets pin.
2. **H2** `getattr(cmd, "whisper_logprob"/"gesture_confidence", None)` in `hybrid_coordinator.record_command_outcome`.
3. **M1** Wrap the tick-loop `pyautogui.position()` in `asyncio.to_thread` (or cache it).
4. **M2** Add `fusion.set_acoustic_profiler(profiler)` in `main.py`.
5. **M3** None-coalesce dashboard formatters.
6. **M4** Verify Ollama is running with all four model tags pulled.
