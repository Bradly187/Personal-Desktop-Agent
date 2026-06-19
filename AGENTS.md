# AI Agent Behavior Rules

These rules apply to **every** AI assistant working in this repository — Google
Antigravity (reads this file natively) and Claude Code (pulls it in via the
`@AGENTS.md` import at the top of `CLAUDE.md`). This is the single source of
truth for cross-tool behavior; do not duplicate these rules elsewhere.

- **`AGENTS.md`** (this file) — concise behavioral rules, obeyed by both IDEs.
- **`CLAUDE.md`** — deep project context (architecture, file map, status, gotchas).
  It imports this file, so the rules below apply in Claude Code too.

## 1. 🗄️ Database Schema Source of Truth
- **Rule:** Always read `storage/db.py` to determine the current `agent.db` SQLite schema, table structures, and `PRAGMA user_version`.
- **Context:** Historical documentation (including older sections of `CLAUDE.md`) frequently has stale table counts. The Python schema definition is the ONLY source of truth. All migrations must be explicitly defined and backwards compatible.

## 2. ⚡ 60Hz Tick Loop Protection
- **Rule:** Never introduce synchronous I/O, heavy computation, or blocking LLM inference inside `core/fusion_engine.py` or the `AccessibilityScheduler`.
- **Context:** The sensor pipeline must maintain a strict 60 Hz loop for smooth cursor gravity, swipe recognition, and tilt responsiveness. Offload DB writes to `async_utils.fire_and_log` and keep model routing asynchronous.

## 3. 🌉 Cross-Platform Protocol Synchronization
- **Rule:** Any changes to JSON payloads or message types in `core/ipad_bridge.py` MUST be mirrored in the Swift codebase (e.g., `iPadApp/DesktopAgent/WebSocketManager.swift` and related files).
- **Context:** The iPad and Windows PC communicate strictly over WebSocket. Do not break serialization/deserialization logic by modifying one side without the other.

## 4. 🛡️ Safe-by-Default Fallbacks
- **Rule:** When modifying `command_executor.py` or adding dev-agent verbs, ensure destructive actions fail safely on ambiguity.
- **Context:** The project uses a "fail-safe to DENY on silence" policy. Any new UI automation paths, shell executions, or file edits must route through voice-approved or explicitly gated pathways (like `goal_session.py`).

## 5. 🤕 Pain-Day Awareness
- **Rule:** When modifying sensor input thresholds, computer vision grounding, or voice processing (e.g., `whisper_stream.py`), you must account for `PainDayEngine` signals.
- **Context:** This is a core accessibility feature. Never hardcode interaction thresholds. Always wire threshold logic through `BehavioralTwinState.apply_pain_day()` so the system adapts during an RA flare-up.

## 6. 🧹 LLM Model VRAM Hygiene
- **Rule:** Strictly respect the `ResourceGovernor` when modifying `inference/model_router.py` or adding new specialist domains.
- **Context:** VRAM on the RTX 5090 is carefully orchestrated. Models are evicted (`keep_alive=0`) on flares to prioritize Whisper. Do not load new large models into the VLLM/Ollama pool without hooking into this eviction lifecycle.

## 7. 🔒 Strict Path Boundaries
- **Rule:** File editing or terminal execution logic must respect the `writable_roots` allowlist (e.g., via `goal_session._path_in_scope`).
- **Context:** Do not use absolute paths or behaviors that circumvent the Windows PC sandbox. Maintain the security posture of the dev-escalation queue.

## 8. 📝 Collaboration & History Check
- **Rule:** At the start of a session, check the most recent work in the project's git history (and open PRs) to ensure your work does not conflict with or duplicate effort from other LLM sessions. Provide a brief summary of previous work.
- **Context:** This repo is worked by multiple AI assistants (Antigravity + Claude Code) and across git worktrees. A quick `git log`/`gh pr list` scan up front prevents duplicated or conflicting changes.

## 9. 📐 Spec-Driven Source of Truth
- **Rule:** Before generating or substantially changing a feature, check `specs/` for its blueprint. `specs/<feature>/` holds the requirements (EARS acceptance criteria), design, and tasks; `specs/TEMPLATE.md` is the starting point for a new one. Treat the spec as authoritative for *intent and behavior*, and update it in the same change when the design moves.
- **Context:** `specs/` is the consolidated home for all checked-in technical designs (migrated from the retired `.kiro/`/`kiro/` trees). It does **not** override the narrower, code-level sources of truth that already exist and remain authoritative for their domain: `storage/db.py` for the DB schema (#1), and the executable `evals/` suites for runtime behavior. Specs describe; `evals/` verifies — add eval cases rather than static Gherkin prose.

## 10. ♻️ Spec-Bounded Regeneration ("disposable code", narrowly scoped)
- **Rule:** When a spec fully defines a unit's behavior **and** tests/evals cover it, an agent may regenerate **that single function** from the spec rather than debugging it line-by-line. **Never** regenerate an entire module, class, or file wholesale without first confirming no untested invariant would be lost.
- **Context:** This is a mature, security-hardened codebase (~1,400+ tests), not a greenfield prototype — "treat code as disposable" applies at function granularity only. Whole-file regeneration silently drops hard-won invariants that no spec captures in full: the fail-safe-DENY approval gate, the 60 Hz tick-loop guarantees (#2), VRAM eviction lifecycle (#6), `goal_session` path/Bash allowlists (#7), and pain-day threshold wiring (#5). If those invariants aren't both specced and test-covered, edit surgically instead.

## Skills catalog (dev/meta procedural memory)

Real [agentskills.io](https://agentskills.io) `SKILL.md` skills live in `.agents/skills/`
(see `.agents/skills/README.md`). They load on demand — consult the matching one
*before* the task instead of re-deriving the procedure:

| Skill | Load it when you're about to… |
|-------|-------------------------------|
| `adding-a-connector-skill` | add an MCP-connector skill (manifest + FastMCP server) |
| `changing-the-db-schema`   | add/alter a table, column, or migration in `agent.db` |
| `running-the-eval-harness` | run, lock, or extend the `evals/` behavioral gates |

These are **not** the runtime accessibility agent's MCP-connector skills
(`skills/manifests/*.json`) — different primitive, different location.
