# Gap Analysis: Personal Desktop Agent vs. the Real OS-Agent Literature

**Date:** 2026-06-22
**Author:** Claude Code (Opus 4.8)
**Status:** Supersedes the Antigravity draft of June 2026, which was withdrawn — all five of its academic
citations were verified non-existent (see [§0](#0-provenance-note)) and ~half of its "gaps" described
features that already ship. Every claim below is traceable to either a resolving arXiv ID / conference
record, or a file in this repository.

---

## 0. Provenance note

The prior draft compared PDA against five fabricated papers ("OS-Agents/Chen 2024 SOSP", "CogOS/Lin&Zhao 2023
TCAD", "A3OS/Rashid 2025 NeurIPS", "Ubiquitous AI OS/Kumar 2022 HCII", "Zhang 2024 CSUR 57(4)"). None resolve;
SOSP 2024's accepted list contains no such paper, and the only real "CogOS" is an unrelated 2026 AGI-alignment
preprint. That document is not a valid roadmap input and should not be cited.

This analysis instead uses the **real** adjacent literature:

- **OS Agents survey** — Tang et al., *OS Agents: A Survey on MLLM-based Agents for General Computing Devices
  Use*, arXiv:2508.04482 (2025). Used here for its taxonomy (perception / planning / memory / action /
  foundation models / safety & privacy / personalization & self-evolution / evaluation).
- **AIOS** — Mei et al., *AIOS: LLM Agent Operating System*, arXiv:2403.16971 (2024). The actual "LLM-as-OS-kernel"
  line of work.
- **LLM-friendly OS interfaces** — *From Imperative to Declarative: Towards LLM-friendly OS Interfaces for Boosted
  Computer-Use Agents*, arXiv:2510.04607 (2025). Declarative > imperative interface argument.
- **Benchmarks** — OSWorld (arXiv:2404.07972), WindowsAgentArena (arXiv:2409.08264), OS-MAP (arXiv:2507.19132).

**Framing caveat:** every one of these targets *general-purpose, multi-tenant computer-use agents or OS kernels*.
PDA is a **single-user, local-first multimodal accessibility controller** for a user with rheumatoid arthritis.
That difference is not a deficiency — it is the design center. Recommendations below are weighed against PDA's
actual goals and the constraints in `AGENTS.md`, not against an OS-kernel ambition PDA does not hold.

---

## 1. Comparison by the survey's own taxonomy

| Dimension (Tang et al. 2025) | PDA implementation (file evidence) | Honest gap |
|---|---|---|
| **Perception** | Multimodal sensor fusion at 60 Hz — tilt/gesture/voice/touch (`core/fusion_engine.py`), Whisper large-v3 (`sensors/whisper_stream.py`), iPad WebSocket protocol (`core/ipad_bridge.py`). | None of significance. PDA's *sensor* breadth exceeds the GUI-screenshot perception assumed by most computer-use agents. |
| **Planning** | DevAgent plan→execute with replan/reflect (`inference/dev_agent.py`); plan-parse auto-repair behind `DA_PLAN_REPAIR` with a model-free eval gate (`evals/plan_contract.py`). | Planner is local-model + verb-grammar. No formal hierarchical task network — appropriate at this scale. |
| **Memory** | Episodic memory + personal KB (ChromaDB, `storage/personal_kb.py`); trajectory reduction for token budget (`inference/trajectory.py`). | No long-horizon learned task memory beyond retrieval. Minor. |
| **Action** | 16-verb vocabulary executed via `core/command_executor.py` → `mcp_server/tools/`. | See **§2.1** — the action surface is *imperative GUI* (CLICK/TYPE/SCROLL), which the declarative-interface literature flags as the weaker substrate. Genuine, but partly intrinsic to accessibility control. |
| **Foundation models** | Local-first Ollama pool, VRAM-governed (`inference/model_router.py`, `core/resource_governor.py`); flare eviction via `keep_alive=0` (`resource_governor.py:427`). Cloud escalation = Bedrock only (`core/cloud_backend.py`). | Routing is **deterministic**, not learned. See **§2.2**. |
| **Safety & privacy** | Pre-write lint gate (`ast.parse` → `EditError`, fail-closed, `inference/edit_format.py`); independent Critic (`inference/critic.py`); autonomous Tester (`inference/tester.py`); namespace sandbox (`inference/sandbox.py`) + WSL routing (`tests/test_wsl_routing.py`); path allowlist realpath-resolved (`goal_session._path_in_scope`); fail-safe-DENY voice approval (`core/approval_keywords.py`). | **Strong — ahead of most cited prototypes.** The prior draft's "safety is post-hoc" claim is false: lint + Critic + path checks all run *before* disk/exec. |
| **Personalization & self-evolution** | Pain-Day threshold adaptation (`adaptive/behavioral_twin_state.py` `apply_pain_day`); domain-classifier learned overlay behind `DA_DOMAIN_LEARN` (`adaptive/continuous_trainer.py`). | Adaptation is bounded/heuristic, not RL. See **§2.3** — but the data regime makes RL premature, not missing. |
| **Evaluation** | Executable eval harness with locked baselines and gates (`evals/`); ~1,400+ tests. | No *standardized* computer-use benchmark (OSWorld/WindowsAgentArena). See **§2.4**. |

**Net:** PDA leads the real literature on perception breadth and on pre-execution safety, matches it on
planning/memory/action for its scope, and trails it only in two genuinely open areas (learned routing,
standardized benchmarking) plus one semantic-tooling gap (§2.5).

---

## 2. Genuine gaps (each with real evidence on both sides)

### 2.1 Imperative GUI action surface vs. declarative interfaces
- **Literature:** arXiv:2510.04607 argues LLMs perform better against *declarative* ("what") interfaces than
  *imperative* ("how") GUI step sequences.
- **PDA today:** the accessibility verbs (CLICK/TYPE/SCROLL/MOUSEDOWN…) are inherently imperative because they
  *are* the user's hands — that is the point of the accessibility pipeline and shouldn't change.
- **Real gap:** the **dev-agent** path, by contrast, *could* be more declarative. It already trends that way
  (WRITE_FILE/RUN_TERMINAL are goal-level, hashline edits are structured). The honest opportunity is narrow:
  expose more app actions to the dev-agent as structured tool-calls rather than synthesized clicks, where an MCP
  or API exists. **Low urgency.**

### 2.2 Deterministic model routing (no learned policy)
- **Literature:** AIOS (2403.16971) schedules agent workloads as a first-class OS concern.
- **PDA today:** `ModelRouter.select_profile` picks by domain + live VRAM headroom, with flare eviction. Safe and
  predictable; not adaptive to latency/cost feedback.
- **Real gap:** a *bandit-style* selector (not a "policy network") could trade off latency vs. correctness using
  the cost/latency telemetry already captured in `cost_ledger.py` and the tracer. **Medium value, but gate it
  behind a flag + eval baseline like every other learning feature here** (`AGENTS.md` #6). Do **not** rip out the
  deterministic path.

### 2.3 Personalization is bounded-heuristic, not learned
- **Literature:** the survey lists self-evolution / learned user models as a frontier.
- **PDA today:** Pain-Day adaptation is real and wired through `BehavioralTwinState` (not the "static bool" the
  prior draft claimed); the domain overlay already learns vocabulary, bounded and rollback-guarded.
- **Real gap / counter-point:** full RL on implicit feedback is **data-starved for a single user** (the
  fine-tuning memo already notes ~21/200 gold cases). The right next step is *more bounded overlays*, not RL.
  **Recommend: no RL module.** This is a place where the academic frame actively mis-fits a single-user tool.

### 2.4 No standardized computer-use benchmark
- **Literature:** OSWorld (2404.07972) and WindowsAgentArena (2409.08264) are the real desktop-agent benchmarks
  (the prior draft's "OS-Agent benchmark suite" was invented).
- **PDA today:** strong internal regression + eval gates, but nothing comparable to external task-success
  numbers.
- **Real gap:** running a **subset** of WindowsAgentArena (it is Windows-native — a natural fit for the RTX 5090
  host) would give defensible external numbers. **Worthwhile, scoped to a representative slice** — not a full
  leaderboard chase, and not an n=10 human study (overkill for one user).

### 2.5 No LSP / semantic code navigation
- **PDA today:** `inference/codebase_indexer.py` + hashline anchoring give text/line-level edits; no symbol-level
  go-to-definition / find-references.
- **Real gap:** a `pylsp` client feeding symbol-aware `READ_FILE`/`WRITE_FILE` would reduce navigation error on
  the dev-agent path. **This is the single highest-confidence, citation-independent recommendation** — it stood
  on its own in the prior draft and survives here unchanged.

---

## 3. Claims from the prior draft that are FALSE (do not re-import)

| Prior-draft claim | Reality |
|---|---|
| "Sandboxed execution environments" are a gap; add Docker (M4). | `inference/sandbox.py` already runs RUN_TERMINAL in a bwrap/firejail namespace jail; WSL routing applies it on the Windows host. Docker is a different trade-off, not a missing primitive. |
| "Safety checks are reactive / post-hoc; no pre-execution verification" (M7: add Z3). | Lint gate (`ast.parse`, fail-closed) + Critic + realpath path-allowlist all run **before** write/exec. Z3 formal verification is heavy machinery for a problem already handled. |
| "Pain-Day = static boolean, manually toggled." | `BehavioralTwinState.apply_pain_day()` + `PainDayEngine` + flare profiles wire thresholds dynamically (`AGENTS.md` #5). |
| "Skills static, no runtime discovery or permission negotiation." | `skills/registry.py`: runtime enable/disable, hot-start, taint + content-filter + fail-safe-DENY send-gate. Capability-*query* discovery is the only real sub-gap. |
| "No learning component; personalization is rule-based." | `DA_DOMAIN_LEARN` overlay learning already exists, bounded and eval-gated. |

---

## 4. Prioritized, right-sized recommendations

| Priority | Item | Why now | Guardrail |
|---|---|---|---|
| **P1** | LSP client (`pylsp`) for symbol-level dev-agent file ops (§2.5) | Highest confidence, citation-independent, clear error reduction | New spec under `specs/`; eval case before default-on |
| **P2** | Run a WindowsAgentArena subset for external numbers (§2.4) | Defensible benchmarking; Windows-native fits the host | Representative slice only; report coverage honestly (`AGENTS.md` #9) |
| **P3** | Bandit-style routing experiment over existing cost/latency telemetry (§2.2) | Could improve latency under contention | Flag-gated + eval baseline; keep deterministic path (`AGENTS.md` #6) |
| **P4** | Capability-query discovery for the skill registry (§2.4 skills sub-gap) | Modest extensibility win | Reuse existing taint/send-gate model |
| **Reject** | Z3 formal verification; RL Pain-Day model; Docker-per-command; n=10 user study | Poor fit for a hardened single-user accessibility tool; some rest on fabricated framing | — |

---

## 5. References (all verified to resolve)

- Tang et al. (2025). *OS Agents: A Survey on MLLM-based Agents for General Computing Devices Use.* arXiv:2508.04482.
- Mei et al. (2024). *AIOS: LLM Agent Operating System.* arXiv:2403.16971.
- *From Imperative to Declarative: Towards LLM-friendly OS Interfaces for Boosted Computer-Use Agents* (2025). arXiv:2510.04607.
- Xie et al. (2024). *OSWorld: Benchmarking Multimodal Agents for Open-Ended Tasks in Real Computer Environments.* arXiv:2404.07972.
- Bonatti et al. (2024). *Windows Agent Arena: Evaluating Multi-Modal OS Agents at Scale.* arXiv:2409.08264.
- *OS-MAP: How Far Can Computer-Using Agents Go in Breadth and Depth?* (2025). arXiv:2507.19132.
- Repo sources: `core/fusion_engine.py`, `inference/{dev_agent,edit_format,critic,tester,sandbox,model_router,trajectory}.py`, `core/resource_governor.py`, `adaptive/behavioral_twin_state.py`, `skills/registry.py`, `storage/personal_kb.py`, `evals/`.
