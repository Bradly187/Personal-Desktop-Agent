# Gap Analysis Handoff: Vibe Coding Agent Security & Evaluation
**Date:** 2026-06-18  
**Source doc:** "Vibe Coding Agent Security and Evaluation" (Google whitepaper, May 2026 — Kartakis, Eidelman, Bakkali, Subasioglu; 41 pages)  
**Agent HEAD:** `74f393d` master, 95 PRs merged

This document is a ready-to-act handoff. Each gap includes the file(s) to touch and a suggested implementation approach. Gaps are ordered by priority.

---

## Context

The whitepaper defines two frameworks:
1. **7-Pillar Security Architecture** — sandboxing, data, model, application, IAM, observability/SecOps, governance
2. **7-Dimension Evaluation Framework** — intent satisfaction, functional correctness, visual/behavioural, cost/efficiency, code quality, trajectory quality, self-repair

The agent already does well on: audit trail hash chain (`storage/audit_log.py`), Zero Ambient Authority (`core/goal_session.py`), lifecycle hooks (`approval_hook.py`), functional tests (1400+), MCP taint detection (`adaptive/mcp_trust_classifier.py`), per-command trace infrastructure (`monitoring/trace.py`).

---

## Priority Gaps (ordered — do these first)

### GAP-1 — RAG Poisoning via Codebase Indexer (HIGH)
**What:** `inference/codebase_indexer.py` feeds source files and docs PDFs into DevAgent's RAG context with no content scanning. A poisoned comment block (`// IGNORE PREVIOUS INSTRUCTIONS — do X instead`) passes through unchanged and enters the LLM's reasoning window.  
**Paper ref:** Pillar 2 (Data), Pillar 6 (Invisible Payloads / Repository Poisoning)  
**Files to touch:**
- `inference/codebase_indexer.py` — pipe each chunk through `MCPTrustClassifier` before storing in ChromaDB; drop or quarantine HIGH-risk chunks
- `adaptive/mcp_trust_classifier.py` — add a `source="rag"` call path; current patterns already cover most injection forms  
**Suggested approach:** Add a `_scan_chunk(text) -> bool` helper in `codebase_indexer.py` that calls `MCPTrustClassifier.classify(tool_name="rag_chunk", result=text)` and skips HIGH-risk chunks (logs to audit). Medium risk gets a `[TAINT]` prefix in the stored chunk so the LLM can see it's flagged.

---

### GAP-2 — Semantic "Vibe Diff" in Approval Gate (HIGH)
**What:** `approval_hook.py` speaks the raw tool name and input before approval (e.g., `"run_terminal: rm -rf ./build"`). For complex scripts the user hears the raw command, not a semantic plain-English summary of what it will DO. The whitepaper calls this the "Vibe Diff" — translating generated code to human intent before cryptographic consent.  
**Paper ref:** Pillar 5 (Elicitation, MFA Challenges, and the "Vibe Diff")  
**Files to touch:**
- `approval_hook.py` — for `run_terminal` and `write_file` tool calls, generate a 1-2 sentence plain-English summary via a fast local LLM call (llama3.1:8b via Ollama) before speaking the action to the user
- `approval_config.json` — add `"vibe_diff_tools": ["run_terminal", "write_file", "keyboard_type"]` to control which tools trigger the summary  
**Suggested approach:** In `_polly_speak()`, if tool is in the vibe-diff list, call `OllamaInference.generate(prompt=f"In one sentence, what will this command DO to the user's system: {tool_input}")` (3s timeout, fall back to raw description on timeout). Speak the summary INSTEAD of the raw input.

---

### GAP-3 — SEARCH_WEB Egress Ungoverned (HIGH)
**What:** `SEARCH_WEB` (dev verb, DevAgent) hits arbitrary URLs. Adversarial web content can be fed into the agent's reasoning without sanitisation. The paper requires all external data to flow through governed pathways.  
**Paper ref:** Pillar 1 (Egress Governance and Non-Interactive Access)  
**Files to touch:**
- `inference/dev_agent.py` — after `SEARCH_WEB` returns content, pipe it through `MCPTrustClassifier` before it enters the plan/reflect context
- `core/goal_session.py` — consider adding `SEARCH_WEB` to the set of verbs that require explicit goal-session authorization (currently only Bash is gated)  
**Suggested approach:** In `DevAgent._execute_step()` for `SEARCH_WEB`, wrap the returned content with `await self._trust_classifier.classify(tool_name="search_web", result=content)` and cap the content at 4000 chars to bound injection surface. Log findings to audit.

---

### GAP-4 — Traces Are Ephemeral — No Eval Replay (MEDIUM)
**What:** `monitoring/trace.py` is an in-memory ring buffer (200 entries, lost on restart). The evaluation framework requires replaying traces to score trajectory quality and diagnose session failures. Half the eval framework is blocked by this gap.  
**Paper ref:** Pillar 6 (Observability), Eval — Trajectory Inspection method  
**Files to touch:**
- `monitoring/trace.py` — add an async `persist_trace(trace_id, agent_db)` method that writes completed spans to a new `command_traces` AgentDB table
- `storage/db.py` — add `command_traces` table (schema: `trace_id, session_id, stage, ts, dur_ms, attrs_json`); bump `PRAGMA user_version` to 8
- `evals/trajectory.py` — add a `TrajectoryReplayer` class that reads persisted spans and scores them against a trajectory case's `required`/`precedence`/`forbidden` constraints  
**Note:** `opentelemetry-sdk` is already installed in `.venv` (arrived via `bedrock_agentcore` deps). Could wire `monitoring/trace.py` spans to an OTLP exporter with minimal effort for future external export. Do NOT add this to the 60 Hz FusionEngine hot path — persist is async/fire-and-forget via `fire_and_log`.

---

### GAP-5 — No Intent Satisfaction Eval Dimension (MEDIUM)
**What:** The evals suite scores routing accuracy and plan verb correctness, but nothing scores "did the agent build what the user MEANT?" The paper's key technique: derive 3-5 acceptance criteria from the session prefix, then score every subsequent turn against them.  
**Paper ref:** Evaluation Dimension 1, Applied tip "Use the session prefix as the intent rubric"  
**Files to touch:**
- `evals/suites/` — add `intent_satisfaction.jsonl` with cases containing `session_prefix` (first 1-2 user messages), `acceptance_criteria` (list of strings), and `output` (agent response to judge)
- `evals/judge.py` — add a `IntentCriteriaCase` dataclass and scoring path: send (prefix + output) to the judge model with prompt "Do these acceptance criteria hold?"
- `evals/run.py` — wire the new suite into the runner  
**Suggested approach:** Start with 10-15 curated cases from real sessions (accessibility commands + dev goals). The criteria can be human-authored for the initial baseline; derivation from the prefix via LLM is the stretch goal.

---

### GAP-6 — Intent Drift / Trust Decay Detection (MEDIUM)
**What:** A multi-turn dev session that quietly drifts from "fix the parser bug" to "refactor half the codebase" has no tripwire. The paper's Trust Decay principle: trust is lost when the agent's chain-of-thought pursues sub-goals diverging from the original user vibe.  
**Paper ref:** Pillar 6 (Measuring Intent Drift and Trust Decay), Checkpoints and Stateful Circuit Breakers  
**Files to touch:**
- `core/hybrid_coordinator.py` — store the session-opening user utterance (`_session_intent`) at first `route()` call; after each route, compute a lightweight similarity check (jaccard or embedding cosine) between the current command and the session intent; if similarity drops below threshold, log a `DRIFT_WARNING` to audit and optionally trigger CLARIFY
- `storage/db.py` — add `intent_drift_log` table (trace_id, session_id, drift_score, original_intent, current_command)  
**Suggested approach:** Keep the similarity check cheap — use `SemanticMemory`'s existing MiniLM embeddings for cosine similarity between session-opening utterance and each subsequent command. A drift_score < 0.3 over 3 consecutive turns triggers a gentle CLARIFY ("You started by asking about X — are we still on track?").

---

### GAP-7 — Supply Chain: No pip Install Integrity (MEDIUM)
**What:** `RUN_TERMINAL pip install X` can pull unverified PyPI packages. The Bash allowlist gates whether pip runs, but not what it fetches. Slopsquatting (hallucinated package names pointing to malware) is the specific threat.  
**Paper ref:** Pillar 1 (Mitigating Hallucinated Packages)  
**Files to touch:**
- `core/goal_session.py` — intercept `pip install` commands in the Bash allowlist scanner; extract the package name(s) and run a "does this package actually exist on PyPI?" check (simple `requests.get("https://pypi.org/pypi/{pkg}/json")`) before allowing the install
- `inference/sandbox.py` — add `--network=none` to bubblewrap args for purely computational tasks (file-write + test-run DAG waves that don't need internet)  
**Suggested approach:** Add a `_verify_pip_install(cmd: str) -> tuple[bool, str]` helper in `goal_session.py`. If the package name doesn't resolve on PyPI, return `(False, "Package not found on PyPI — possible hallucination")` and route to CLARIFY.

---

### GAP-8 — No Standardized Benchmarks (LOW-MEDIUM)
**What:** No SWE-bench, LiveCodeBench, or Kaggle SAE integration. External calibration signal is missing. The paper specifically notes Kaggle SAE deploys via a `SKILL.md` file — our skill model supports this exactly.  
**Paper ref:** Evaluation — Standardised Benchmarks & Kaggle Agent Exams  
**Files to touch:**
- `skills/manifests/` — add `kaggle_sae.json` manifest pointing to a new `skills/servers/kaggle_sae_server.py`  
- New `skills/servers/kaggle_sae_server.py` — FastMCP server: `register_agent`, `fetch_exam`, `submit_answer`, `get_score` tools wrapping the Kaggle SAE API  
**Note:** This is a stretch goal. Prioritise GAP-1 through GAP-5 first.

---

### GAP-9 — User Corrections Not Harvested as Failure Data (LOW-MEDIUM)
**What:** Every "no, not like that" from the user is a labeled failure example. Currently discarded. The paper recommends clustering these to surface systematic agent failure modes.  
**Paper ref:** Evaluation — Applied tip "Mine user corrections as labeled failure data"  
**Files to touch:**
- `core/hybrid_coordinator.py` — detect correction patterns (utterances that follow a CLARIFY or a failed action within the same session); write them to a new `user_corrections` AgentDB table
- `storage/db.py` — add `user_corrections` table (session_id, trace_id, correction_text, prior_action, domain)
- `scripts/cluster_corrections.py` — offline script that embeds corrections with MiniLM and runs k-means clustering; prints the top failure mode clusters as candidate new eval cases  
**Suggested approach:** The correction signal is simpler than it sounds: any user utterance that (a) follows a command action within 30s and (b) doesn't map cleanly to a known verb (i.e., routes to CLARIFY or domain=COMMAND with low confidence) is likely a correction.

---

### GAP-10 — No Denial-of-Wallet Alerting (LOW)
**What:** `max_replans`/`max_steps` cap DevAgent depth and `rate_limiter.py` exists, but there's no threshold that fires a TTS warning when cumulative API spend in a session exceeds a budget. Slow loops under the per-call cap can still accumulate significant cost.  
**Paper ref:** Pillar 6 (Observability — Denial of Wallet attacks)  
**Files to touch:**
- `core/hybrid_coordinator.py` — track `_session_cloud_calls` counter; when it exceeds a threshold (e.g., 20 cloud calls in one session), emit a TTS warning via `_polly_speak` and require explicit re-authorization to continue using the cloud path
- `approval_config.json` — add `"cloud_call_budget": 20` (configurable)

---

## Already Well-Covered (no action needed)

| Area | Implementation | Status |
|---|---|---|
| Immutable audit trail | `storage/audit_log.py` SHA-256 hash chain | Exceeds paper's requirement |
| Zero Ambient Authority | `core/goal_session.py` deny-by-default Bash allowlist | Matches paper exactly |
| Lifecycle hooks | `approval_hook.py` PreToolUse gate | Matches paper exactly |
| Functional test suite | 1400+ tests + CI eval gate | Ahead of most deployments |
| MCP taint detection (inbound) | `adaptive/mcp_trust_classifier.py` | Solid; extend to RAG (GAP-1) |
| Outbound secret scrub | `adaptive/content_filter.py` | Good coverage |
| Per-command trace infrastructure | `monitoring/trace.py` | Right architecture; needs persistence (GAP-4) |
| Self-repair loop | EH-1 `action_verifier.py` + EH-2 `dev_escalations` | Mechanically complete |
| WSL2 sandbox for terminals | `inference/sandbox.py` bubblewrap/firejail | Covers RUN_TERMINAL verb |
| Pain-Day awareness | `core/resource_governor.py` + `BehavioralTwinState` | No paper analog — unique strength |

---

## Suggested Sprint Order

**Sprint A (security hardening, ~2-3 PRs):**
- GAP-1: RAG taint scanning in codebase indexer
- GAP-2: Vibe Diff semantic summary in approval gate
- GAP-3: SEARCH_WEB content taint scan

**Sprint B (observability + eval, ~2 PRs):**
- GAP-4: Persist traces to AgentDB + add `TrajectoryReplayer` to evals
- GAP-5: Intent satisfaction eval suite (10-15 cases)

**Sprint C (drift + corrections, ~1-2 PRs):**
- GAP-6: Intent Drift / Trust Decay detection in coordinator
- GAP-9: User correction harvesting + clustering script

**Deferred:**
- GAP-7 (pip integrity check)
- GAP-8 (Kaggle SAE benchmark)
- GAP-10 (DoW alerting)
