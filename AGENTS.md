# AI Agent Behavior Rules

These rules apply to **every** AI assistant working in this repository — Google
Antigravity (reads this file natively) and Claude Code (pulls it in via the
`@AGENTS.md` import at the top of `CLAUDE.md`). This is the single source of
truth for cross-tool behavior; do not duplicate these rules elsewhere.

- **`AGENTS.md`** (this file) — concise behavioral rules, obeyed by both IDEs.
- **`CLAUDE.md`** — deep project context (architecture, file map, status, gotchas).
  It imports this file, so the rules below apply in Claude Code too.

## 1. 🗄️ Database Schema Source of Truth
- **Rule:** Always read `storage/schema/agent.py` to determine the current `agent.db` SQLite schema, table structures, and `PRAGMA user_version`.
- **Context:** Historical documentation (including older sections of `CLAUDE.md`) frequently has stale table counts. The Python schema definition in `storage/schema/agent.py` is the ONLY source of truth. All migrations must be explicitly defined and backwards compatible.

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
- **Context:** `specs/` is the consolidated home for all checked-in technical designs (migrated from the retired spec trees). It does **not** override the narrower, code-level sources of truth that already exist and remain authoritative for their domain: `storage/db.py` for the DB schema (#1), and the executable `evals/` suites for runtime behavior. Specs describe; `evals/` verifies — add eval cases rather than static Gherkin prose.

## 10. ♻️ Spec-Bounded Regeneration ("disposable code", narrowly scoped)
- **Rule:** When a spec fully defines a unit's behavior **and** tests/evals cover it, an agent may regenerate **that single function** from the spec rather than debugging it line-by-line. **Never** regenerate an entire module, class, or file wholesale without first confirming no untested invariant would be lost.
- **Context:** This is a mature, security-hardened codebase (~1,400+ tests), not a greenfield prototype — "treat code as disposable" applies at function granularity only. Whole-file regeneration silently drops hard-won invariants that no spec captures in full: the fail-safe-DENY approval gate, the 60 Hz tick-loop guarantees (#2), VRAM eviction lifecycle (#6), `goal_session` path/Bash allowlists (#7), and pain-day threshold wiring (#5). If those invariants aren't both specced and test-covered, edit surgically instead.

## 11. 🚦 Two-Gate Feature Approval
- **Rule:** A spec at `Status: Draft` MUST NOT be built. An agent drafts the spec, presents it for review, and waits for explicit human approval before changing the status to `In Progress` and writing any code. `tasks.md` (the phase plan) follows the same gate — draft it, present it, wait for explicit approval before executing any task. Silence or ambiguity = NOT approved. **This gate also applies to AGENTS.md rule additions and modifications** — draft the change in conversation, present it, and wait for explicit approval before committing it to the file.
- **Context:** This prevents a session from spec-ing and immediately building a feature in one unreviewed sweep, and prevents rule creep in AGENTS.md from accumulating unchecked. The two gates are: (1) spec/rule approved → `Status: In Progress` (or rule committed); (2) phase plan (`tasks.md`) approved → tasks may execute. Both gates require an explicit "yes" in the conversation or a written sign-off on the file. The spec `Status:` field is the approval signal — don't promote it yourself.

## 12. 📋 Decision Log
- **Rule:** When making a non-obvious architectural or behavioral decision with a meaningful rejected alternative, add an entry to `docs/decisions.md`. Use the format: **Date, Chose, Rejected, Why, Ref**. Log mid-session when the decision is made, not at session end.
- **Context:** This repo has multiple AI sessions and two AI assistants. Without a log, the same trade-off gets re-derived in every new session — wasting tokens and occasionally arriving at the wrong answer. The bar is "meaningful rejected alternative": if there was only one reasonable option, skip the log. If you chose X over Y and the reasoning isn't obvious from the code, log it. Keep the index at the top of `docs/decisions.md` under 30 lines.

## 13. 📄 Documentation Hygiene
- **Rule:** Route new documentation to the correct destination using this matrix. Before writing to CLAUDE.md or AGENTS.md, identify which row applies and go there instead:

  | Content type | Correct destination |
  |---|---|
  | Non-obvious trade-off with a rejected alternative | `docs/decisions.md` (D-entry) |
  | Feature behavior, API contract, EARS criteria | `specs/<feature>/` |
  | New `DA_*` flag | One row in CLAUDE.md Feature Flags table |
  | True gotcha: invariant not obvious from the spec (≤3 sentences) | CLAUDE.md Known Gotchas |
  | Cross-tool behavioral rule (MUST/MUST NOT, stable, cross-tool) | `AGENTS.md` (Rule 11 gate applies) |
  | Status update | CLAUDE.md Current Status — date + PR# only |

  **Never append inline amendments** to existing statements — replace the full statement and update the Current Status date. History belongs in `docs/CHANGELOG.md`, not inside section prose.

  **Pruning:** A Gotcha entry MUST be removed or compressed to one sentence + refs when both conditions hold: (1) its behavioral content is fully described in the feature's spec, AND (2) any trade-off it documents is captured in `docs/decisions.md`. Run `/doc-update` at the end of every session that ships code. Run the pruning pass (Step 5 of `/doc-update`) after every 5 merged PRs.

  **AGENTS.md rule bar:** A rule belongs in AGENTS.md only when it passes all three: (a) phrased as MUST or MUST NOT, (b) applies cross-tool, (c) is stable across features. Feature-specific content that fails any bar belongs in CLAUDE.md or a spec. Proposing a new rule requires two-gate approval per Rule 11.

- **Context:** CLAUDE.md is loaded at the start of every session. Redundant feature summaries and stale gotchas directly cost context budget and mislead new sessions. The destination matrix is the single check; `/doc-update` is the enforcement mechanism that fires at session end.

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
