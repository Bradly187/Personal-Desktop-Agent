# Gap Analysis, Security Review & Next-Sprint Directions
**Personal Desktop Agent — 2026-06-12**

Scope: a functionality gap analysis (against the "personal operating system" goal), a security analysis (practical single-user home-LAN threat model), a stale/dead-code-and-reference audit, and a prioritized set of next-sprint directions. Findings are evidence-based from a read of the actual source, not from CLAUDE.md alone — where CLAUDE.md is itself stale, that is flagged.

---

## 1. Executive summary

The **OS-style plumbing is genuinely mature.** Scheduler, memory-syscall façade, event bus, resource governor, supervisor, circuit breakers, VRAM arbiter, per-domain SLOs, a durable goal queue, cross-layer tracing, and crash recovery all exist with real implementations and a passing test suite. The `ipad-sensor-focus` spec is essentially 100% delivered. As a low-strain **control surface and developer agent**, the system is strong.

The gap is in **user space, not kernel space.** As a *personal* operating system the agent is missing the application layer that delivers daily-life value: there is no calendar/email/messaging integration, no proactive or time/event-triggered automation, no user-level cross-application workflows, no extensible skill model, and the knowledge base is scoped to the codebase rather than the user's own information.

The dominant **security** risk is **network exposure**, not application logic. The application-layer authorization (goal-session Bash allowlist, fail-safe-to-deny approval gate, cloud content-scrubbing, DevAgent destructive-op confirmation) is well built and hard to bypass. But the primary control plane (`ipad_bridge` on `0.0.0.0:8765`) and the remote indexer (`0.0.0.0:9000`) bind all interfaces with **no authentication**. On a trusted home LAN these are the documented accepted risks; they become disqualifying the moment the system leaves a trusted network or goes multi-user.

The codebase is **clean of most dead code** (the gaze/head/sound removals were thorough), but **documentation and git state have drifted badly**: the real `agent.db` table count is **40** (docs say 38/39, CLAUDE.md says 29/30), CLAUDE.md's entire git "Current Status" header describes a branch that no longer exists, and the working tree shows 330 "modified" files that are pure CRLF/LF line-ending churn masking all real diffs.

**Highest-leverage next sprint:** not more kernel primitives. It is (a) close the bridge auth hole, (b) ship the first user-space integration (calendar/email read-out + voice reply), and (c) a documentation/git-hygiene cleanup pass so the repo's own status is trustworthy again.

---

## 2. Functionality gap analysis — the "personal OS" lens

### 2.1 OS-subsystem maturity

| Subsystem | Maturity | Evidence |
|---|---|---|
| Process/task scheduling & prioritization | **Present** | `core/scheduler.py` — priority queue, 5 tiers (accessibility/voice/gesture uncapped; dev/background semaphore-gated), `fan_out()` on a separate sub-agent semaphore, bounded queue (256) with priority-aware load-shedding. |
| Memory management (working/long-term/semantic) | **Present** | `storage/memory_manager.py` syscall façade (`read_context`/`write_state`/`search_semantic`, `_VALID_KEYS` validation); `storage/db.py` (AgentDB, versioned via `PRAGMA user_version`); `storage/semantic_memory.py` (ChromaDB cosine + Jaccard fallback); `adaptive/behavioral_twin_state.py`. |
| IPC / message bus | **Present** | `core/events.py` — durable replayable SQLite `event_log` + in-process async fan-out, dotted-topic namespace. `Command` dataclass is the universal DTO. |
| Resource governance (VRAM/CPU/pain) | **Present** (no thermal) | `core/resource_governor.py` (flare → relax thresholds, pause indexer, evict heavy models, pause dev admission; hysteresis 0.6/0.4); `core/vram.py`, `core/vram_arbiter.py`, `core/slo.py`. GPU thermals are **not** monitored — "pain" is the only thermal analog. |
| Fault tolerance / supervision / self-healing | **Present** | `core/supervisor.py` (one-for-one watchdog, bounded restarts → latch FAILED → degrade), `core/circuit_breaker.py` (wired into `OllamaInference`), DevAgent closed-loop replan + saga rollback. |
| Persistence & crash recovery | **Present** | `goal_queue` (`db.py`, enqueue/claim/complete/requeue-stale, idempotency key); `agent_runs` status lifecycle + `mark_interrupted_runs`; voice-gated `resume_pending_plan()`; append-only `audit.db`. |
| Observability / tracing / metrics | **Present** | `monitoring/metrics.py` (`/metrics`), `monitoring/trace.py` (opt-in `DA_TRACE`, `trace_id` on `Command`, `GET /trace`), `storage/session_analyzer.py` (DuckDB analytics). |
| Security / permissions / sandboxing | **Partial** | Strong app-layer authz (see §3). Known gaps: no audit hash-chaining; remote indexer no-auth; WSL sandbox is a no-op on the actual Windows host. |
| I/O & device abstraction (sensors) | **Present** | `sensors/*` + Swift iPad app (40 files); graceful `ImportError` degradation; 25-type WebSocket protocol. Narrowed (gaze/head/sound removed for hardware reasons). |
| User space — apps / skills / extensibility | **Partial** | Action vocabulary is fixed and closed (11 accessibility + 5 dev + git/plan verbs). **No skill/plugin/extension registry** — new capability = editing `command_executor` + LLM prompt. MCP server (14 tools) is the only real extensibility seam. |
| Networking / distributed compute | **Present** (no auth) | `core/cluster_health.py` + `core/cluster_config.py` (laptop RTX 4070 offload, fail-safe to local). Home-LAN only, unauthenticated. |
| Update / deployment / lifecycle | **Partial / Missing** | Graceful shutdown, startup status table, launcher scripts. **No self-update, no versioned release/rollback of the PC service, no end-user config migration.** iPad ships via TestFlight CI; PC runs from source. |

### 2.2 Top functional gaps (severity-ranked)

1. **[HIGH] No comms/PIM integration — calendar, email, messaging.** A grep across all `.py` for `calendar|email|gmail|outlook|reminder|smtp|imap` returns **zero** integration modules. For an RA user, "read my next meeting" / "reply to this email by voice" is exactly the high-value, low-strain workflow the system exists to enable — and it is absent.
2. **[HIGH] No proactive / scheduled / event-triggered automation.** No cron-of-user-intents, no "when X, do Y," no reminders. `goal_queue` is durable but *pull-on-demand*. Everything the user gets is reactive — they must issue every command.
3. **[MEDIUM-HIGH] No user-level cross-application workflows.** Multi-step orchestration (plan→DAG→saga) exists only in the *dev* domain. There is no daily-life equivalent ("summarize this PDF, paste into the email I'm drafting"). The accessibility path is one-verb-per-command.
4. **[MEDIUM] No extensibility/skill model for user space.** The 16-verb vocabulary is hard-coded; new capabilities need source edits. No manifest-driven skill/plugin system caps how "personal" the agent can become.
5. **[MEDIUM] Knowledge base is code/docs-only.** `codebase_indexer.py` (~1937 code chunks, 128 doc pages) + `semantic_memory.py` index the *codebase and project PDFs* — a developer KB, not a personal one. The agent can't answer life questions from the user's notes/files.
6. **[MEDIUM] Vision/grounding fallback chain is partly cloud-dependent.** CLICK resolution chains UIAutomation → vision (qwen3-vl local, Sonnet 4.6 cloud fallback) → OCR → CLARIFY. ~92% CLICK success means ~8% of named-target clicks still fail — meaningful friction for a hands-limited user — and the vision fallback can leave the device.
7. **[LOW-MEDIUM] No thermal/hardware-health governance.** The governor is pain-aware but not GPU-thermal-aware; a sustained-load thermal event has no kernel response.
8. **[LOW] Manual deployment/update lifecycle.** No self-update, release versioning/rollback, or config migration for the PC service — fine single-user-from-source, a gap for the commercial roadmap.

### 2.3 Open work (from `tasks.md` and trajectory)

- `tasks.md` (419 lines) is essentially fully `[x]`. The only open item — `6.4 AgentCore Tier 1 deployment` — is a **closed decision** (source deleted; raw cloud via Anthropic API is the permanent tier), not a real gap.
- `ROADMAP.md` Phase 7 `#9 Speculative decoding ⏳` — flag plumbing exists, production path not yet validated/enabled.
- Spec-vs-impl divergence: `.kiro/specs/behavioral-twin-state/` still documents `command_count_today` (renamed `command_count_session` in code).

---

## 3. Security analysis (single-user / home-LAN threat model)

**Overall:** application-layer authorization is genuinely well-built; the risk concentration is unauthenticated network exposure. On the current trusted-LAN deployment, C1/C2/M1 are the *documented accepted* risks. Everything in HIGH/MEDIUM becomes mandatory the moment this leaves a trusted LAN or goes multi-user.

### CRITICAL

**C1 — iPad WebSocket bridge `0.0.0.0:8765`: no auth, token, origin check, or TLS.**
`core/ipad_bridge.py` (`ws_handler`, default `host="0.0.0.0"`); `main.py` default `--host 0.0.0.0`. The handler accepts any connection and dispatches every message with no credential check. A `touch_command` flows straight to `CommandExecutor.execute()` — any of the 16 verbs including `RUN_TERMINAL` (arbitrary shell) and `WRITE_FILE` (arbitrary path). **Any LAN device gets full keyboard/mouse/terminal/file control.**
*Fix:* require a pairing token on WS connect (reject the upgrade otherwise); bind the WireGuard interface rather than `0.0.0.0` by default; add `wss://` for a commercial product. **Single highest-leverage fix — it is the unauthenticated root of full desktop control and amplifies H2/M2.**

**C2 — `remote_indexer_service.py` `0.0.0.0:9000`: no auth; results flow unvalidated into the LLM prompt.**
`inference/remote_indexer_service.py` (no auth on any route); `inference/remote_indexer_client.py` returns `data["results"]` unconditionally into `DevAgent._rag_context()`. Anyone who can reach `:9000` can read indexed source/docs **and inject attacker-controlled text into the dev-agent's LLM context** — a prompt-injection channel that can steer plans toward destructive verbs.
*Fix:* bind WireGuard/loopback, add a shared bearer token both ends, treat results as untrusted data (length-cap, clearly delimit as data not instructions).

### HIGH

**H1 — `/metrics` and `/trace` bind `0.0.0.0` and leak command text.** `main.py` (`TCPSite(..., "0.0.0.0", args.metrics_port)`). `/trace` returns the 50 most recent traces (command text, route decisions, inference detail), no auth. Off by default (`metrics_port=0`), which bounds exposure. *Fix:* bind `127.0.0.1`/WireGuard; add a token for remote dashboards.

**H2 — RUN_TERMINAL runs `shell=True` UNSANDBOXED on the actual Windows host.** `inference/sandbox.py` returns `None` on non-POSIX, so the bubblewrap/firejail jail never engages on Windows — the advertised cwd-jail, net isolation, and memory caps are all absent. The goal-session allowlist is the *only* real boundary there, and verbs arriving via the no-auth bridge (C1) bypass goal-session entirely. *Fix:* a Windows containment path (Job Object limits / restricted token / forced WSL2 round-trip), or make `sandboxed=False` on Windows a hard blocker for any command not on a tighter exec allowlist.

### MEDIUM

**M1 — `audit.db` is append-only by trigger only; no DROP-TABLE/file protection, no hash-chaining.** `storage/audit_log.py` enforces append-only with `BEFORE UPDATE`/`BEFORE DELETE` triggers, which do **not** fire on `DROP TABLE` or file deletion, and there is no `prev_hash` chaining for tamper-evidence. (Explicitly deferred in CLAUDE.md.) *Fix:* per-row hash-chaining; restrict file ACLs / forward to append-only external storage for any commercial/compliance posture.

**M2 — `WRITE_FILE` has no path scoping at the executor level.** `core/command_executor.py` writes to *any* path. The `cwd_scope` guard in `goal_session.allows_action` only governs Claude Code's `Write`/`Edit`, not this executor verb, which is reachable directly from the (unauth) bridge. *Fix:* enforce a writable-root allowlist inside `CommandExecutor` for WRITE_FILE/RUN_TERMINAL cwd, independent of the goal-session layer.

**M3 — Windows action proxy executes arbitrary verbs over HTTP, no auth (loopback only).** `windows_action_proxy.py` `POST /execute` on `127.0.0.1:8768` runs the full verb set. Loopback-bound, so a local-process / DNS-rebinding concern rather than LAN-remote. *Fix:* per-launch shared token on the WSL→proxy handshake.

**M4 — Gate 0 privacy filter is naive substring matching.** `core/hybrid_coordinator.py` forces-local only on literal substrings (`"password"`, `"api key"`, `"ssn"`). A secret without a trigger keyword passes to cloud. The real backstop is `adaptive/content_filter.py` (well-built: AWS/Anthropic/OpenAI/GitHub keys, private keys, DB URLs, SSN/CC; scrubs both `text` and `session_context`), so actual leak risk is bounded by ContentFilter's regex coverage — but unframed high-entropy secrets won't match. *Fix:* treat Gate 0 as best-effort routing; rely on ContentFilter as authoritative; consider an entropy-based detector.

### LOW / Positive notes

- **Secrets handling is clean.** No hardcoded keys/tokens in any `.py`; Anthropic SDK and AWS use standard credential chains.
- **Localhost services correctly scoped:** TTS sidecar (`:8766`), kiro (`:8767`), action proxy (`:8768`) all loopback. Only bridge (8765), indexer (9000), and the metrics endpoint default/expose `0.0.0.0`.
- **Dependencies fully pinned** (`requirements.txt`), even patching a known mDNS CVE (`zeroconf==0.149.16`).
- **Worth preserving:** the goal-session Bash allowlist is deny-by-default and defeats compound injection (`pytest && rm -rf`), `python -c` inline code, redirection, and command substitution, with `..`-traversal-safe path scoping; the approval gate fails safe to DENY on ambiguity/silence/timeout and guards the TTS echo from self-approving; DevAgent recomputes destructiveness after replans; the cloud path scrubs command text and session context. These are the things less careful projects get wrong, and they're right here.

### Remediation order
1. **C1** — pairing token + WireGuard/loopback bind on the bridge.
2. **C2** — token-auth + bind the remote indexer; treat its results as untrusted.
3. **H1** — bind `/metrics` and `/trace` to loopback.
4. **H2 / M2 / M3** — Windows RUN_TERMINAL containment + WRITE_FILE writable-root allowlist + proxy token.
5. **M1** — audit hash-chaining + file ACLs.

---

## 4. Stale / dead code & reference audit

### 4.1 Dead / orphaned code

- **Dead schema columns (removed-feature residue), never populated with real data:** `sensor_telemetry.{gaze_dx,gaze_dy,gaze_conf,head_pitch,head_yaw}` — written hardcoded `None` from the only writer (`fusion_engine.py`); `flare_profile.gaze_degrades` (no-op passthrough); `flare_profile.sound_degrades` (mouth-sound control removed). *Note:* `commands.gaze_x/gaze_y` ← `cmd.gaze_coords` is **legitimately kept** (generic explicit-click-coord field) — not dead.
- **Orphaned modules:** `quick_check.py` (referenced nowhere but the index-state file — dead scratch script); `sensors/hand_pointer.py` and `sensors/realsense_publisher.py` (imported only by `scripts/validate_realsense.py`, never by the live pipeline — in-progress RealSense L515 work, dead weight on this branch).
- **Not dead (standalone daemons, 0 importers expected):** `sensors/remote_whisper_service.py`, `inference/remote_indexer_service.py` (run on the laptop node).
- **Clean removals confirmed:** the Swift app has zero references to `GazeTracker`/`HeadTracker`/`SharedFaceSession`/`SoundDetector`/`MonitorCalibration`/`CursorConflict`; the `gaze_monitor_calibration` table is gone from `db.py` (CLAUDE.md's "existing DBs keep the orphan table" is stale — the schema is clean); `desktop/magnetic_overlay.py` deleted cleanly; no tests reference the removed pipelines; **zero TODO/FIXME/XXX/HACK/DEPRECATED markers** in project Python.

### 4.2 Stale documentation / counts

- **Real `agent.db` table count is 40** (verified by enumerating every `CREATE TABLE` in the agent.db range, excluding the 3 DuckDB `benchmark_*` tables). Stale claims: `docs/architecture/database-design.md` says "39 tables" (lines ~192, 194, 860) and even "38-table schema" (line ~104) — three different numbers in one doc, all wrong. CLAUDE.md says "30 tables" / "29 tables" — both badly stale. A recent commit bumped the doc to 39, but the schema has since grown to 40.
- The ER diagrams in `database-design.md` enumerate only ~14 entities — far short of the prose count; diagrams are incomplete.
- **Stale removed-feature spec/diagram artifacts:** `.kiro/specs/enhanced-gaze-dwell/` (entire spec dir for the removed feature); a duplicate `kiro/specs/accessibility-agent/` tree (non-dotted `kiro/`, older copy riddled with gaze/head/sound refs); numerous `.kiro/.../diagrams/*` and `docs/diagrams/*.svg` still depict gaze/head/sound nodes despite the priority list dropping to 6 levels.

### 4.3 Git hygiene

- **CLAUDE.md's git "Current Status" header is several sprints stale.** It says the branch is `feat/rag-kb-remediation`, tip `633164d`, "not yet pushed / no PR." Reality: that branch no longer exists, `633164d` was merged, the actual checked-out branch is `feat/dev-escalation-queue`, and PRs #39–#44 are already merged past where CLAUDE.md describes.
- **Working tree shows 330 "modified" files that are pure CRLF↔LF line-ending churn.** `git diff --ignore-all-space --stat` is empty — zero semantic change. Likely a Windows checkout without `.gitattributes` normalization. This masks all real diffs (including the uncommitted gravity/snap tuning CLAUDE.md mentions). *Fix:* add `.gitattributes` (`* text=auto eol=lf`), run `git add --renormalize .`, remove the stale `.git/index.lock`.
- Local `master` is behind `origin/master` by 6 commits; ~10 local branches, several already-merged, not yet pruned.

---

## 5. Next-sprint directions (prioritized)

A "personal OS" is bottlenecked on **user space**, not kernel space. Recommended ordering:

**Sprint N (security + hygiene foundation — small, high-value, do first)**
1. **Close C1**: pairing token on the iPad bridge + bind WireGuard/loopback by default. Single highest-leverage change.
2. **Close C2 + H1**: token-auth and bind the remote indexer and metrics/trace endpoints; treat indexer results as untrusted prompt data.
3. **Repo hygiene**: `.gitattributes` + renormalize (clears the 330-file phantom diff), remove `.git/index.lock`, prune merged branches, sync `master`.
4. **Doc truth-up**: fix the table count to 40 everywhere, rewrite CLAUDE.md's git status header, delete `.kiro/specs/enhanced-gaze-dwell/` and the duplicate `kiro/specs/` tree, prune gaze/head/sound from diagrams, drop the dead schema columns (or tombstone them).

**Sprint N+1 (first user-space integration — the biggest functional gap)**
5. **Calendar + email read-out and voice reply.** Wire a comms/PIM connector (Gmail/Calendar or Outlook) into the action vocabulary: "read my next meeting," "summarize unread," "reply by voice." This is the single largest category of daily personal-assistant value and maps directly to the low-strain RA use case. Reuse the existing approval-gate + content-filter for any send action.

**Sprint N+2 (proactivity)**
6. **Time/event-triggered automation.** A user-intent scheduler ("every morning brief me," "when an email from X arrives, notify me") on top of `goal_queue` + `events.py`. Turns the agent from reactive to proactive.

**Sprint N+3 (extensibility)**
7. **Manifest-driven skill/plugin model for user space**, so new capabilities stop requiring `command_executor` + prompt edits. This is what lets the system become genuinely *personal* per-user and unblocks the commercial roadmap.

**Backlog / opportunistic**
8. WRITE_FILE writable-root allowlist + Windows RUN_TERMINAL containment (H2/M2/M3) — pairs naturally with the C1 fix.
9. Personal knowledge base (user notes/files) alongside the existing code KB.
10. GPU-thermal governance in the resource governor.
11. Validate/enable speculative decoding (ROADMAP #9); audit hash-chaining (M1) when a commercial posture is on the table.
12. Self-update / versioned release + config migration for the PC service.

---

*Prepared 2026-06-12. Findings verified against source; where CLAUDE.md diverged from the code, the code was treated as ground truth.*
