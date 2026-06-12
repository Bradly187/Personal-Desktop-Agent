# Status & Landscape Assessment — Personal Desktop Agent

*Toward a dynamic, natural-language agentic operating system for a developer with rheumatoid arthritis*

**Date:** 2026-06-11 (corrected same day against master `f78eaf9`) · **Scope:** originally drafted against `CODE_ANALYSIS.md` (657bb2c); §3 updated to reflect the orchestration-v3 and audit-fix merges (PRs #38–#42) · **Orientation:** academic-leaning

---

## 1. Executive summary

The Personal Desktop Agent is, as built, a mature *multimodal accessibility control system with an embedded dev-agent*. It is not yet a *natural-language agentic operating system* in the sense the academic literature now uses that term — but it is closer to one than most published research prototypes, and it occupies a niche almost nobody else is working in: a **single-user, local-first, pain-aware, continuously-adapting agent harness** built around a disability whose defining feature is *day-to-day variability in functional capacity*.

Three findings frame the rest of this report:

1. **The implementation is real and deep.** ~183 Python modules, a 1216-test suite, a native Swift iPad app, six-domain model routing, a four-gate local/cloud coordinator, a behavioral-twin user model, and an AIOS-style kernel layer (scheduler, memory manager, resource governor, supervisor, circuit breaker). On an engineering-maturity axis this is well ahead of the typical academic agent-OS paper, which is usually a reference architecture plus a benchmark run.

2. **The concept is academically current and commercially under-served.** The "LLM as kernel, natural language as the interface, agents as processes" framing (AIOS; AgentOS; Karpathy's "Software 3.0") is exactly the frontier the field is converging on in 2025–2026. The accessibility framing — *ability-based design*, *disability-centered human–agent collaboration* — is a recognised research program. The intersection of the two, grounded in a real homelab with a real disabled user-developer, is rare.

3. **The gap between "what's built" and "the stated goal" is now mostly conceptual and integrative, not a matter of missing parts.** The pieces of an agentic OS exist; what is missing is (a) the *self-evolution loop closing on its own behavior*, (b) a *persistent, queryable world/memory model* beyond per-session twin state, and (c) the reframing of the system from "command executor" to "natural-language process supervisor." These are research directions, not bug fixes — they are listed in §8.

One alignment note worth surfacing immediately: **rheumatoid arthritis (RA) is the authoritative design driver** (per the user's explicit direction, 2026-06-11 — the same direction under which the legacy `svt_attack` condition was removed in PR #45). A handful of older documents still say *juvenile idiopathic arthritis (JIA)*; those are the drift to scrub, not the codebase's RA framing. The motor-accessibility requirements overlap almost completely, so little code is affected, but clinical literature and flare-modeling assumptions should be grounded in adult RA (see §3.4 and Direction R-5).

---

## 2. The concept, restated

**Goal (as given):** an agent-harness orchestration system that uses sensors and feedback from integrated devices to *continuously learn*, in order to facilitate *agentic AI software development* for a person with RA whose functional capacity varies with flares and disability. Homelab-specific. Long-term target: a *dynamic, natural-language agentic operating system*.

Decomposed, the goal asserts five commitments, which become the evaluation axes for the rest of this report:

| # | Commitment | One-line test |
|---|------------|---------------|
| C1 | **Multimodal sensing** from integrated devices (iPad + workstation) | Are sensors fused into a single intent stream? |
| C2 | **Continuous learning** from feedback | Does the system measurably change its own behavior over time? |
| C3 | **Pain/flare-aware adaptation** | Does variable capacity change system behavior, not just thresholds? |
| C4 | **Agentic software development** as the payload task | Can it plan→edit→run→verify code with appropriate autonomy? |
| C5 | **Natural-language agentic OS** as the architecture | Is NL the control plane, with agents as managed processes over shared resources? |

---

## 3. Implementation status assessment

### 3.1 What is solidly built (evidence-backed)

The strongest, production-grade subsystems map cleanly onto a standard agentic-orchestration framework (per `CODE_ANALYSIS.md` §6):

- **Central orchestrator** — `HybridCoordinator` is a single, tested routing entry point; `AccessibilityScheduler` adds 5-tier priority queuing; `DevAgent._run_dag_waves` gives dependency-DAG fan-out for independent plan steps. *(C4, C5)*
- **AIOS-style kernel primitives** — `MemoryManager` (syscall façade over SQLite + ChromaDB), `ResourceGovernor` (pain-aware VRAM/threshold control), `Supervisor` (one-for-one liveness watchdog), `CircuitBreaker`, `VramArbiter` (single admission-policy source). This is a genuine *kernel layer*, not a metaphor. *(C5)*
- **Graceful degradation everywhere** — every coordinate-resolution, vision, TTS, Whisper, RAG, routing, and scheduling path has an explicit fallback chain; every hardware import is `try/except`-guarded. *(C3, robustness)*
- **Structured tool interface + audit** — `mcp_server/tools/` typed tools, per-tool approval policy, append-only `audit.db` with UPDATE/DELETE triggers, deny-by-default Bash allowlist, WSL2 sandbox for `RUN_TERMINAL`. *(C4 safety)*
- **Observability** — `trace_id` ContextVar propagation across coordinator→router→inference→executor; `GET /trace/{id}`; per-domain SLO breach logging in `adaptation_log`.
- **Multimodal fusion** — 60 Hz `FusionEngine` arbitrating touch/voice/tilt/gesture at six priority levels into one `Command` DTO per tick. *(C1)*
- **Local-first inference** — six-domain `DomainClassifier` → `ModelRouter` over Ollama (vLLM optional), with Anthropic cloud only as a gated fallback. *(C5, privacy)*

### 3.2 What is partial

- **Continuous learning (C2)** is implemented but *narrow*. `ContinuousTrainer` adapts routing thresholds, few-shot ranking, gesture velocity floors, and per-domain SLO overrides; `BehavioralTwinState` persists a user model across sessions. This is real online adaptation — but it adapts *parameters and retrieval*, not *skills, tools, or policies*. The system does not yet acquire new capabilities from experience (see §5.2).
- **Saga / compensation** — now substantially built (2026-06-09 audit fixes, merged 2026-06-10): `DevAgent._run_compensations` / `_halt_and_compensate` with restore-not-delete file snapshots, firing on **all** terminal paths (`max_replans`, `max_steps`, `user_cancel`). The remaining gap is the *escalation queue* — exhausted plans compensate and stop, but nothing lands in a human-review backlog (see R-10 residual).
- **Choreography / event bus** — the bus exists (`core/events.py` `EventBus` with topic publish/subscribe, landed with orchestration-v3 in PR #38). What's missing is the *consumers*: no "watcher" agents yet subscribe to topics like "repeated CLARIFY" or "flare onset" (see R-8 residual).
- **Visualization** — TUI dashboard + `/metrics` exist; no DAG/heat-map view of agent call chains.

### 3.3 What is absent (relative to the OS goal)

- No **live plan registry** an external caller can inspect mid-flight.
- No **agent-to-agent messaging** — all inter-agent state flows through the DB or signal files.
- No **persistent world model / long-term episodic memory** beyond twin snapshots and RAG; nothing equivalent to MemGPT's archival/recall tiering (see §5.3).
- No **human-review escalation queue** when `MAX_REPLANS=2` is exhausted — the run now compensates cleanly (no partial side effects), but the failed goal evaporates instead of landing in a reviewable backlog.

### 3.4 Documentation drift (low-effort, worth fixing)

Several docs disagree with the current system and with the stated goal:

- **Residual JIA references — scrubbed (this commit).** RA is authoritative (§1); JIA appeared in `.kiro/specs/sensor-refinement/requirements.md` (7 user stories), `.kiro/specs/tilt-position-mapping/design.md`, and `kiro/specs/accessibility-agent/diagrams/04-component-deployment.md` — all now say RA. The last remaining mention is `README.md` on master, replaced by the PR #44 rewrite.
- **`README.md`** on master still lists *eye gaze* and *head tracking* as live modalities and specifies "32+ GB RAM" — gaze/head were removed 2026-05-30, and the workstation has 192 GB. The rewrite is already in flight as PR #44 (`docs/readme-refresh`, RA framing correct). `ROADMAP.md` (last updated 2026-05-25) still shows gaze calibration G1–G4 as complete features.
- These are cosmetic but they undermine the repo as a *self-describing system*, which matters more than usual for a project whose end-state is an OS that reasons about itself.

---

## 4. Goal-alignment scorecard

| Commitment | Status | Evidence | Gap to goal |
|------------|--------|----------|-------------|
| C1 Multimodal sensing | **Strong** | FusionEngine 6-level priority; 25 WS message types; iPad Swift app | Sensor set narrowed (gaze/head/LiDAR-RealSense in flux); fusion is rule-based, not learned |
| C2 Continuous learning | **Moderate** | ContinuousTrainer + BehavioralTwinState + adaptation_log | Adapts parameters, not skills/tools/policies; no self-evaluation loop |
| C3 Flare-aware adaptation | **Strong (distinctive)** | ResourceGovernor, PainDayEngine, acoustic/voice recalibration, pain-day threshold deltas | Pain model is signal-fusion heuristic; not predictive; not validated against clinical flare data |
| C4 Agentic software development | **Moderate–Strong** | DevAgent plan→execute→reflect, DAG fan-out, saga compensation on all terminal paths, git verbs, sandbox, Kiro IDE bridge | No human-review escalation queue when the replan cap is exhausted; no autonomous multi-step review pipeline |
| C5 Natural-language agentic OS | **Emerging** | Kernel primitives present; NL is *an* input, not yet *the* control plane | NL still maps to a fixed 16-verb vocabulary; agents are not first-class managed processes; no NL-defined process spawning |

**Reading:** C1 and C3 are the project's strengths and its genuine novelty. C2, C4, C5 are where the "dynamic NL agentic OS" ambition outruns the current build — and where the research directions in §8 concentrate.

---

## 5. Comparison against the academic landscape

### 5.1 LLM agent operating systems (AIOS, AgentOS, UFO²)

The canonical reference is **AIOS** (Mei et al., COLM 2025) — an OS-shaped runtime whose kernel defines six modules: *scheduler, context manager, memory manager, storage manager, tool manager, access manager*. ([arXiv 2403.16971](https://arxiv.org/abs/2403.16971); [agiresearch/AIOS](https://github.com/agiresearch/AIOS))

Your system has independently grown four of these six as named components (`AccessibilityScheduler`, `MemoryManager`, `storage/`, `mcp_server` tool layer + `goal_session` access manager). **The architectural convergence is striking and validating** — you are not off in an idiosyncratic direction; you have re-derived the AIOS kernel from first principles, plus two modules AIOS does not emphasize: a **pain-aware `ResourceGovernor`** and a **`Supervisor`**. Where AIOS is multi-tenant and benchmark-driven, yours is single-tenant and *embodied* (real sensors, real desktop, real user).

Two more recent papers sharpen the target:
- **AgentOS: From Application Silos to a Natural Language-Driven Data Ecosystem** ([arXiv 2603.08938](https://arxiv.org/html/2603.08938v1)) — proposes replacing the GUI desktop with a *Natural User Interface* centered on a unified NL/voice portal over an "Agent Kernel." This is almost a literal statement of your C5 goal. It is the paper to read against your own design.
- **UFO²: The Desktop AgentOS** ([arXiv 2504.14603](https://arxiv.org/pdf/2504.14603)) — Microsoft's desktop agent-OS for Windows. It is the closest *system* analogue to your dev-agent execution layer (UIA tree traversal, multi-app workflows). Your `desktop/ui_automation.py` BFS + vision-grounding fallback chain is conceptually parallel; UFO² is the comparison baseline for the *execution* half of your system.

**Where you lead:** embodiment, accessibility, pain-awareness, local-first. **Where they lead:** NL as a true control plane (not a verb dispatcher), and treating agents as first-class processes that can be spawned/scheduled/killed by name.

### 5.2 Self-evolving / lifelong-learning agents

This is the literature that most directly addresses C2. The two anchor surveys:
- **A Survey of Self-Evolving Agents** ([arXiv 2507.21046](https://arxiv.org/pdf/2507.21046)) — frames evolution along *what / when / how / where* to evolve, and crucially distinguishes self-evolving agents from curriculum learning: they adjust **non-parametric components (memory, tools, policies)**, not just weights.
- **A Comprehensive Survey of Self-Evolving AI Agents** ([arXiv 2508.07407](https://arxiv.org/pdf/2508.07407)) and the **Lifelong Learning of LLM-based Agents roadmap** ([arXiv 2501.07278](https://arxiv.org/abs/2501.07278)) — organize the field around perception / memory / action modules adapting continuously, with RAG-based example selection and continual instruction tuning balanced against catastrophic forgetting.

**Assessment:** Your `ContinuousTrainer` sits at the *parameter-adaptation* end of this spectrum — the well-understood, lower-risk end. The frontier the surveys describe is **non-parametric self-evolution**: the agent edits its own tool set, prompts, few-shot library, and routing *policy* in response to outcomes, with guardrails against forgetting and reward hacking. You already log everything needed to drive this (`commands`, `adaptation_log`, `agent_runs`, trace spans) — the data substrate exists; the closed evolution loop does not. This is the single highest-leverage research direction (R-1).

### 5.3 Agent memory architectures

**MemGPT** explicitly borrows OS memory management — core (RAM-like) vs. recall vs. archival (disk-like) tiers, with the agent paging information in and out under context-window pressure. **A-Mem** ([arXiv 2502.12110](https://arxiv.org/html/2502.12110v1)) adds agentic, self-organizing memory notes.

**Assessment:** Your memory story is currently *two-tier and shallow*: working state in `agent.db`, semantic recall in ChromaDB (cosine), plus `BehavioralTwinState` as a compact user model. There is no archival/episodic tier and no autonomous paging policy. For a system whose user has *variable capacity and long time horizons*, a MemGPT-style hierarchical memory — especially an **episodic log of "how the user worked on good vs. flare days"** — is both a natural fit and a research contribution in its own right (R-2).

### 5.4 Accessibility & disability-centered HCI

This is where your project is most defensible as *research*, not just engineering.
- **Ability-based design** (Wobbrock & Gajos) — design to what the user *can* do; place the burden of adaptation on the *system*, and make interfaces *adaptive or adaptable* to a live model of ability. ([CACM 2018](https://dl.acm.org/doi/10.1145/3148051); [TACCESS 2011 PDF](https://kgajos.seas.harvard.edu/papers/wobbrock11abd.pdf); [Characterizing "Motor Ability", ASSETS 2024](https://dl.acm.org/doi/10.1145/3663548.3675646)) Your `PainDayEngine` + `ResourceGovernor` + per-day threshold relaxation is **a working instantiation of ability-based design's central tenet** — arguably a stronger one than most published systems, because ability here is *sensed continuously and re-modeled*, not set once.
- **Disability-centered human–agent collaboration** (Xiao & Holloway, UCL — your `2603.26252v1.pdf`) — a three-layer *channelling / coordinating / collaborating* framework. This is the lens to position your contribution: you are building the "collaborating" layer for a single user.
- **Physiological signals for ability-based design** (Wobbrock, CHI 2024 — [PDF](https://faculty.washington.edu/wobbrock/pubs/chi-24.01.pdf)) — argues for driving adaptation from physiological/behavioral signals. Your acoustic profiler (voice clarity as a flare signal) and gesture-jitter inflammation signal are exactly this idea, applied.

**Assessment:** Your strongest publishable claim is **"continuous, sensed ability-based adaptation in an agentic OS."** No mainstream computer-use agent does this; the ability-based-design literature theorizes it but rarely embodies it in a full agent stack.

### 5.5 Summary: concept vs. academic frontier

| Academic thread | Your position | Lead / lag |
|-----------------|---------------|------------|
| LLM agent OS (AIOS/AgentOS/UFO²) | Re-derived the kernel; embodied + local | **Lead on embodiment; lag on NL-as-control-plane** |
| Self-evolving / lifelong agents | Parameter-level adaptation only | **Lag — biggest opportunity** |
| Agentic memory (MemGPT/A-Mem) | Two-tier, no episodic/archival | **Lag** |
| Ability-based design / disability HCI | Working continuous-ability instantiation | **Lead — your defensible novelty** |

---

## 6. Comparison against the market landscape

(Included for completeness; your emphasis is academic, so this is briefer.)

- **Computer-use / GUI agents** — Anthropic Claude (computer use; ~61% OSWorld-Verified by late 2025), OpenAI Operator→ChatGPT agent mode, Microsoft UFO², Simular Agent S2. ([OSWorld](https://os-world.github.io/); [xlang-ai/OSWorld](https://github.com/xlang-ai/osworld)) These are *general* desktop automators; none are accessibility- or pain-aware, and all are cloud-first. **Your differentiation: local-first, single-user, ability-adaptive.** Their differentiation: breadth, benchmark-validated reliability, and frontier-model planning quality.
- **Voice-coding / hands-free dev** — Talon Voice, Serenade (local, RSI-origin), Cursorless. ([Talon overview](https://www.joshwcomeau.com/blog/hands-free-coding/)) These solve *input* for disabled/RSI developers but are *not agentic* — they translate speech to keystrokes/commands, not goals to plans. **Your dev-agent is a step beyond:** NL goal → planned, verified code change. This is a real white-space: *agentic* hands-free development for motor-impaired engineers.
- **Local-first personal agents (2025–2026 wave)** — Microsoft Scout + on-device Aion models, Nous Hermes Desktop, NVIDIA RTX Spark / DGX Station, the "NemoClaw/OpenClaw" local-agent stack in your `NVIDIAAn.pdf`. ([On-device agents 2026](https://www.digitalapplied.com/blog/on-device-local-ai-agents-2026-privacy-cost-stack-forecast); [Private LLM inference on consumer Blackwell, arXiv 2601.09527](https://arxiv.org/html/2601.09527v1)) The local-agent stack you bet on (RTX 5090 + Ollama/vLLM + MCP) is now the industry's consensus direction — validation that your hardware/runtime choices were not idiosyncratic.

**Market reading:** you are *not* competing with computer-use agents on generality, and you should not try to. The defensible position is the **intersection nobody occupies: local, ability-adaptive, agentic dev assistance for a disabled engineer**, with the homelab as the deployment target rather than a SaaS market.

---

## 7. Synthesis — where the project actually stands

The honest one-paragraph status: *You have built a robust multimodal accessibility agent with a genuine OS-style kernel and a credible dev-agent, and you have a distinctive, research-grade angle (continuous sensed ability-adaptation) that the literature theorizes but rarely embodies. You have not yet built the two things that would make it a "dynamic NL agentic OS": (1) a self-evolution loop that improves the agent's skills/policies from its own logged outcomes, and (2) a natural-language control plane where agents are first-class, spawnable, schedulable processes over a persistent world/memory model — rather than a fixed 16-verb command vocabulary.* The good news is that the substrate for both already exists in your codebase; the work is closing loops and reframing, not greenfield construction.

---

## 8. Research directions

Grouped by the commitment they advance, ordered roughly by leverage. Each is scoped to a single-user homelab and to your existing substrate.

### Closing the learning loop (C2 — highest leverage)

- **R-1 — Non-parametric self-evolution loop.** Build an offline (nightly, on-flare-free time) "reflection" agent that reads `commands` + `adaptation_log` + traces and *proposes* edits to the few-shot library, routing policy, and tool prompts, gated by a held-out replay eval and a forgetting check. This is the concrete path from `ContinuousTrainer` (parameters) to *self-evolving agent* (policies/tools). Anchor: self-evolving surveys (2507.21046, 2508.07407).
- **R-2 — Hierarchical episodic memory (MemGPT-style), keyed by capacity state.** Add an archival/recall tier that records *how tasks were accomplished on good vs. flare days*, so the agent can retrieve "the way that worked last time I was flaring." This unifies C2 and C3 and is publishable on its own.
- **R-3 — Learned sensor fusion.** Replace (or shadow) the rule-based 6-level priority arbitration with a small learned arbiter trained on logged outcomes, so fusion weights adapt to the user's changing reliability per modality per day.

### Pain/flare modeling (C3 — your novelty, make it rigorous)

- **R-4 — Predictive (not reactive) flare model.** Today the `PainDayEngine` fuses current signals. Add a short-horizon predictor (voice clarity trend, gesture jitter trend, command-failure rate, time-of-day, weather/allergy inputs) that *anticipates* capacity decline and pre-adapts. Validate against a self-reported flare diary — this is the experiment that turns the system into evidence.
- **R-5 — Clinical grounding for adult RA.** The JIA-reference scrub is done (§3.4); the open work is aligning flare signals/periodicity with the adult-RA literature — morning stiffness patterns, flare duration distributions, weather/baro sensitivity. (The in-repo `JIA_Desktop_AGENT_Research.pdf` predates the RA framing decision; treat it as background, not the clinical anchor.)
- **R-6 — Formalize as ability-based design with continuous ability estimation.** Position the pain/ability model explicitly against Wobbrock & Gajos and the CHI'24 physiological-signals work; define an "ability state vector" as a first-class system object that every adaptive component reads.

### Toward a natural-language agentic OS (C5 — the long-term reframe)

- **R-7 — NL as control plane, not verb dispatcher.** Move beyond the fixed 16-verb vocabulary toward NL that can *define and spawn* processes ("watch my test suite and tell me when it goes red," "every flare day, switch to dictation-first mode"). Treat agents as first-class, named, schedulable processes under the existing `AccessibilityScheduler`. Anchor: AgentOS (2603.08938), Karpathy "Software 3.0."
- **R-8 — Watcher agents on the existing event bus.** The pub/sub layer now exists (`core/events.py`, PR #38); the open half is the consumers — agents that subscribe to topics (e.g., "repeated CLARIFY," "flare onset") instead of being wired into the coordinator. Prerequisite for an extensible OS.
- **R-9 — Live, queryable plan/world registry.** Expose the in-flight plan and the world/ability state as an inspectable, NL-queryable object ("what are you doing and why?") — both a usability win for a disabled user and the reflective substrate self-evolution needs.

### Agentic software development (C4 — make autonomy safe enough to trust)

- **R-10 — Escalation queue (saga rollback is done).** Reverse compensation landed 2026-06-10 (`_run_compensations` with restore-not-delete snapshots, all terminal paths). The residual is the escalation queue: when `MAX_REPLANS` is exhausted, persist the failed goal + plan + failure context to a reviewable backlog and surface it (voice/status) instead of letting it evaporate after compensation. This is the trust precondition for higher autonomy.
- **R-11 — Autonomous, verified dev-review pipeline.** Wire the existing individual capabilities (RAG, EXPLAIN, sandboxed test run, audit, action-verifier) into a coordinated *analyze→test→verify→gate* loop — closing the "exists individually but not orchestrated" gap §6.4 identifies. Benchmark against UFO² and OSWorld-style tasks scoped to your repo.
- **R-12 — Effort-aware autonomy dialing.** Tie the dev-agent's autonomy level (how much it does before asking) to the *ability state vector* from R-6: more autonomous, fewer confirmations on flare days; more collaborative on good days. This is the cleanest expression of the whole thesis.

### Evaluation & evidence (cross-cutting)

- **R-13 — A personal, longitudinal benchmark.** Define a small OSWorld-style task suite over *your own* workflows and track success/effort/latency across capacity states over weeks. Without this, "continuously learns" is an assertion; with it, it's a result.
- **R-14 — Single-subject study design.** Treat the homelab as an N-of-1 research platform (single-case experimental design) — the disability-HCI field accepts and values N-of-1 work, and it converts your daily use into citable evidence. Position against the Xiao & Holloway collaboration framework.

---

## 9. Recommended near-term sequence

If the aim is to move the *concept* toward the *goal* with least wasted motion: **R-5/R-9 first** (finish the framing scrub; make the system self-describing — cheap, unblocks everything), then **R-1 + R-13 together** (self-evolution loop is meaningless without the benchmark to prove it helps), then **R-7** (the reframe that turns an accessibility agent into an OS). The R-10 residual (escalation queue) should precede any increase in dev-agent autonomy regardless of order — the rollback half is already in.

---

## References

**Agent operating systems**
- AIOS: LLM Agent Operating System — [arXiv 2403.16971](https://arxiv.org/abs/2403.16971) · [code](https://github.com/agiresearch/AIOS)
- AgentOS: From Application Silos to a Natural Language-Driven Data Ecosystem — [arXiv 2603.08938](https://arxiv.org/html/2603.08938v1)
- UFO²: The Desktop AgentOS — [arXiv 2504.14603](https://arxiv.org/pdf/2504.14603)
- LLM-OS concept / "Software 3.0" (Karpathy) — [overview](https://falexm.medium.com/the-llm-os-revolution-how-operating-system-architecture-is-transforming-ai-agents-ca1734f1ee54)

**Self-evolving & lifelong-learning agents**
- A Survey of Self-Evolving Agents — [arXiv 2507.21046](https://arxiv.org/pdf/2507.21046)
- A Comprehensive Survey of Self-Evolving AI Agents — [arXiv 2508.07407](https://arxiv.org/pdf/2508.07407)
- Lifelong Learning of LLM-based Agents: A Roadmap — [arXiv 2501.07278](https://arxiv.org/abs/2501.07278)

**Agent memory**
- MemGPT (OS-inspired hierarchical memory) — [overview](https://informationmatters.org/2025/10/memgpt-engineering-semantic-memory-through-adaptive-retention-and-context-summarization/)
- A-Mem: Agentic Memory for LLM Agents — [arXiv 2502.12110](https://arxiv.org/html/2502.12110v1)

**Computer-use / GUI agents**
- OSWorld benchmark — [site](https://os-world.github.io/) · [code](https://github.com/xlang-ai/osworld)
- Computer-use agent state of the art (2025–26) — [survey](https://o-mega.ai/articles/the-2025-2026-guide-to-ai-computer-use-benchmarks-and-top-ai-agents)

**Accessibility / disability-centered HCI**
- Ability-Based Design (Wobbrock & Gajos) — [CACM 2018](https://dl.acm.org/doi/10.1145/3148051) · [TACCESS 2011](https://kgajos.seas.harvard.edu/papers/wobbrock11abd.pdf)
- Characterizing "Motor Ability" for Ability-Based Design — [ASSETS 2024](https://dl.acm.org/doi/10.1145/3663548.3675646)
- Physiological Signals for Ability-Based Design — [CHI 2024 PDF](https://faculty.washington.edu/wobbrock/pubs/chi-24.01.pdf)
- Channelling, Coordinating, Collaborating: Disability-Centered Human–Agent Collaboration (Xiao & Holloway, UCL) — `docs/research/2603.26252v1.pdf`

**Local-first inference**
- Private LLM Inference on Consumer Blackwell GPUs — [arXiv 2601.09527](https://arxiv.org/html/2601.09527v1)
- The On-Device Agent Era (2026) — [analysis](https://www.digitalapplied.com/blog/on-device-local-ai-agents-2026-privacy-cost-stack-forecast)

**Voice / hands-free development**
- Hands-free coding with Talon — [Comeau](https://www.joshwcomeau.com/blog/hands-free-coding/) · Serenade (local, RSI-origin), Cursorless

**In-repo prior research**
- `docs/research/agentOS_Ppaer.pdf`, `JIA_Desktop_AGENT_Research.pdf`, `disability-technology-report.pdf`, `NVIDIAAn.pdf`, `2603.26252v1.pdf`
