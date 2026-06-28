# Spec: Cloud Plan Routing (agentic plan-generation → Sonnet 4.6)

---

## 1. Background — the "Why"

The agentic executor's plan-generation step is the single heaviest local LLM op
on the RTX 5090. `DevAgent.plan_and_run` calls `ModelRouter.infer(domain="plan")`
→ **`qwen3-coder:30b` (~18 GB)**. With the command model (`llama3.1:8b`, ~9 GB) and
general model (`gemma4:12b`, ~9 GB) already resident on top of the ~12.5 GB
baseline+Whisper floor, a plan request cannot co-reside — it forces a
~50–60 s evict/reload swap (the "VRAM budget exceeded" thrash Brad hits).

Plan generation is the ideal thing to offload: it is **off the 60 Hz path**
(AGENTS.md #2), **one-shot per plan/replan/repair**, **infrequent** (cents/day),
and **quality-sensitive** (a better plan means fewer Critic-REVISE / replan loops,
i.e. *less* downstream local compute). Claude Sonnet 4.6 via Amazon Bedrock is a
materially stronger planner than the local 30B and roughly half Opus cost.

This routes **only the `domain="plan"` inferences** to Sonnet 4.6 while keeping
**execution (WRITE_FILE/RUN_TERMINAL/commit) and the local model as fail-safe
fallback** on-device. It is distinct from the existing answer-only cloud path:
[`../`](.) — `CloudDevAgent` (see `inference/cloud_dev_agent.py`) routes dev-domain
*answer* queries to the cloud at the `HybridCoordinator` layer and returns early
with `steps: 0` (no execution). This spec keeps the full agentic loop and swaps
only the planner's brain.

Related: `inference/cloud_dev_agent.py` (answer path), `core/cloud_backend.py`
(Bedrock seam), `specs/dev-agent-plan-contract/` (plan JSON schema + auto-repair),
`specs/dev-agent-critic/` (the replan loop a better plan shortens).

**Status:** Draft
**Owner / author session:** Claude Code

---

## 2. Glossary

- **CloudPlanRouter**: a transparent proxy that wraps the real `ModelRouter`.
  It intercepts `infer(domain="plan")` and routes it to Sonnet 4.6 on Bedrock;
  every other method/domain delegates unchanged to the wrapped router.
- **Plan domain**: the `domain="plan"` inference used by `DevAgent` for plan
  generation, auto-repair (`_acquire_plan_steps`), replan, the DELEGATE
  investigation sub-plan, and synth — all of which call
  `self._router.infer(domain="plan", …)`.
- **Plan JSON schema**: `inference.model_router._PLAN_JSON_SCHEMA` — the
  `{"steps": [{action, args, body, after}]}` contract that
  `DevAgent._parse_plan_json_report` consumes. Single source of truth, shared
  between the local Ollama `format` path and this cloud path.
- **Bedrock backend**: `core.cloud_backend.resolve_backend()` →
  `AsyncAnthropicBedrock`; credential `AWS_BEARER_TOKEN_BEDROCK`. Sonnet 4.6 base
  id `anthropic.claude-sonnet-4-6` (already mapped).
- **ContentFilter**: `adaptive.content_filter.ContentFilter` — async
  `scrub(text) -> (clean_text, findings)`; redacts secrets/PII before egress.

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: Route plan generation to Sonnet 4.6, keep execution local

**User Story:** As Brad, I want the agentic planner to think on Sonnet 4.6 in the
cloud while the file writes / terminal / commits still run on my PC, so that the
18 GB local plan model never loads and I stop blowing my VRAM budget.

#### Acceptance Criteria
1. WHEN `cloud-plan-routing` is enabled AND a Bedrock credential is configured,
   THE `CloudPlanRouter` SHALL serve `infer(domain="plan")` from Claude
   Sonnet 4.6 via Bedrock and SHALL NOT cause the local plan specialist
   (`qwen3-coder:30b`) to load for planning.
2. THE `CloudPlanRouter` SHALL intercept ONLY `domain="plan"`; every other domain
   (`command`/`code`/`math`/`vision`/`general`) and every other method
   (`select_profile`, `edit_format_for`, `infer_stream`, `analyse_screen`,
   `get_status`, …) SHALL delegate unchanged to the wrapped `ModelRouter`.
3. THE cloud plan call SHALL return a `RouterResult` whose `.text` is a JSON
   string matching the **Plan JSON schema**, so `DevAgent._parse_plan_json_report`
   parses it byte-compatibly with the local path (no executor change).
4. THE `CloudPlanRouter` SHALL NOT execute any desktop/file/shell action — it is
   pure inference; all execution stays in the local `DevAgent` loop.

### Requirement 2: Fail-safe — never hard-fail, never lose offline planning

#### Acceptance Criteria
1. IF the `anthropic` SDK is missing, the credential is unset, the model is not
   yet granted, the network/Bedrock call errors, OR the returned tool input does
   not satisfy the schema, THEN THE `CloudPlanRouter` SHALL transparently fall
   back to the wrapped local `infer(domain="plan")` and the agent SHALL behave
   exactly as today (AGENTS.md #4).
2. WHILE `cloud-plan-routing` is disabled (default), THE wiring SHALL pass the raw
   `ModelRouter` to `DevAgent` so behavior is **byte-identical** to legacy.
3. THE local plan specialist SHALL remain available as the fallback path — this
   spec MUST NOT delete or unregister `qwen3-coder:30b` / the plan fallback chain.

### Requirement 3: Privacy — the plan context is new cloud egress of source code

> The agentic plan context (`extra_ctx`) contains the user's source: repo-context
> (AGENTS.md/CLAUDE.md), the **git working-tree diff**, RAG snippets over the
> codebase, file paths, and prior-session memory. This has never left the machine.

#### Acceptance Criteria
1. BEFORE any cloud egress, THE `CloudPlanRouter` SHALL scrub BOTH the goal
   (`user_text`) AND the assembled `context` through `ContentFilter.scrub`.
2. IF scrubbing surfaces a **critical** finding (private key, API key, AWS/cloud
   credential), THEN THE `CloudPlanRouter` SHALL force the LOCAL plan path for that
   call (never send even-redacted critical-secret context to the cloud) and log
   the forced-local decision.
3. THE forced-local privacy path (R3.2) and every fallback (R2.1) SHALL be logged
   at INFO so a plan that silently stayed local is observable.

### Requirement 5: Explicit "plan" word trigger (reliable opt-in)

**User Story:** As Brad, I want any prompt containing the word "plan" to route to the
agentic planner (and thus cloud plan routing), so that I don't have to keyword-tune
my wording to land in the plan domain.

> Context: `DomainClassifier` is pure keyword scoring; "search the codebase / read /
> summarize" keyword-matched to `code` and went single-shot to the local 30B
> (hallucinating, since single-shot code has no file access) instead of `plan`.

#### Acceptance Criteria
1. WHEN `DA_PLAN_WORD_TRIGGER` is on AND a query contains a literal
   `plan`/`plans`/`planning` token, THE `DomainClassifier` SHALL return `plan`
   (winning score `60.0`, above the command short-verb boost and any keyword score),
   bypassing the `_MIN_WORDS_FOR_DEV` gate so a short "plan X" still routes to plan.
2. WHILE the flag is OFF (default), THE classifier SHALL be **byte-identical** to the
   static classifier — the `router_domains` eval baseline holds.
3. THE trigger SHALL match on token equality, not substring (so "explain",
   "explanation", etc. never trigger), and SHALL NOT affect command-bypass sources
   (touch / voice-click never reach the classifier) — the accessibility path is
   unaffected (AGENTS.md #2).
4. IF a query has no plan token, THEN classification SHALL be unchanged (command
   stays command, code stays code).

### Requirement 4: Cost accounting

#### Acceptance Criteria
1. WHEN a cloud plan call completes, THE `CloudPlanRouter` SHALL record the
   Bedrock token usage to the cost ledger (`AgentDB.insert_inference`, backend
   `"bedrock"`, domain `"plan"`), mirroring the `CloudDevAgent` answer path, so the
   most expensive plan path is never an unrecorded blind spot.
2. THE `CloudPlanRouter` SHALL share the coordinator's `RateLimiter` `"anthropic"`
   bucket when wired, and fail-open (proceed) when it is not.

---

## 4. Technical Design

- **Entry point / pipeline boundary:** a proxy wrapping `ModelRouter`, installed in
  `main.py` between `router = ModelRouter()` (+ optional vLLM pool wiring) and
  `DevAgent(router=router, …)`. `DevAgent` is the only consumer of this router, so
  wrapping there is sufficient and contained. The `HybridCoordinator` command path
  (`local`/`LocalInference`) is untouched.
- **New module:** `inference/cloud_plan_router.py` — `class CloudPlanRouter`.
  - `__init__(self, inner: ModelRouter, *, content_filter=None, agent_db=None, model="claude-sonnet-4-6", timeout=60.0)`.
  - `__getattr__` → delegate every non-overridden attribute to `inner` (transparent
    proxy; `select_profile`, `edit_format_for`, `infer_stream`, `analyse_screen`,
    `get_status`, `dev_model_roster`, `set_vllm_pool`, etc. all pass through).
  - `async def infer(self, domain, user_text, screenshot_b64=None, context=None)`:
    - `if domain != "plan"` → `return await self._inner.infer(...)` unchanged.
    - else: scrub (R3.1); critical finding → local fallback (R3.2); else call
      Bedrock with **forced tool-use** and return a `RouterResult(text=<json>,
      model=<sonnet id>, domain="plan", free_form=True, backend="bedrock")`.
    - ANY exception / unavailable / schema-invalid → `return await
      self._inner.infer("plan", …)` (R2.1).
  - `set_rate_limiter(limiter)` — stored, used best-effort before egress (R4.2).
- **Structured output:** define one Bedrock tool `emit_plan` whose `input_schema`
  is `model_router._PLAN_JSON_SCHEMA`; call with
  `tool_choice={"type": "tool", "name": "emit_plan"}` and **thinking disabled**
  (forced tool_choice + extended thinking is not a supported combo). The system
  prompt reuses `model_router._PLAN_PROMPT` (single source of truth — same verbs,
  same `after` dependency semantics). Read the `tool_use` block's `.input`,
  `json.dumps` it → `RouterResult.text`. A response with no `tool_use` block (e.g.
  the model emitted plain text) counts as schema-invalid → local fallback.
- **Reuse, do not fork, the Bedrock client:** build via
  `core.cloud_backend.resolve_backend().make_client(async_=True, timeout=…)` and
  `backend.map_model("claude-sonnet-4-6")`, exactly like `CloudDevAgent._get_client`.
- **Models / VRAM:** no new local model. When enabled + reachable, the plan domain
  never selects `qwen3-coder:30b`, freeing ~18 GB; the command + general resident
  set (~17.6 GB) now fits comfortably under the 32 GB budget with no plan swap.
  `ResourceGovernor` / `heavy_model_names` are unaffected (qwen stays registered as
  the offline fallback — AGENTS.md #6).
- **Persistence:** no schema change — reuses the existing `inferences` table via
  `AgentDB.insert_inference` (AGENTS.md #1, no `user_version` bump).
- **Cross-platform:** none — no bridge payload changes (AGENTS.md #3 N/A).
- **Auto-repair interaction:** `_acquire_plan_steps` re-prompts via
  `self._router.infer(domain="plan", …)` — that re-prompt also flows through the
  shim, so repair benefits from Sonnet too, with the same fallback guarantees.

### Configuration (flat YAML / env)

The flag is read from the environment (matches the existing `DA_*` dev-agent
knobs); no nested YAML needed.

```yaml
cloud_plan_routing:
  enabled: false                 # env DA_CLOUD_PLAN (default OFF — byte-identical legacy)
  model: claude-sonnet-4-6       # env DA_CLOUD_PLAN_MODEL (Bedrock alias)
  timeout_s: 60                  # env DA_CLOUD_PLAN_TIMEOUT_S
  plan_word_trigger: false       # env DA_PLAN_WORD_TRIGGER (R5) — literal "plan" forces plan domain
  # credential: AWS_BEARER_TOKEN_BEDROCK (shared with the cloud answer path)
  # force_local_on_critical: true (R3.2 — not separately configurable in v1)
```

Enabling requires `AWS_BEARER_TOKEN_BEDROCK` set and the Sonnet 4.6 model granted
on the Bedrock account; otherwise R2.1 keeps planning local (logged).

### Non-goals (v1)

- Routing `code`/`vision`/`general` generation to the cloud (the existing
  `--cloud-dev-agent` answer path already covers Q&A; this spec is plan-only).
- Streaming the plan (the agentic loop uses `infer`, not `infer_stream`; plan is
  consumed whole by the parser).
- Removing or downgrading the local plan model (it is the offline fallback).
- A per-query UI toggle (the global `DA_CLOUD_PLAN` flag is sufficient for a
  single-user box).

---

## 5. Behavior Verification (executable, not prose)

- **Unit/integration tests:** `tests/test_cloud_plan_router.py`, one assertion per
  numbered criterion (cite the number in the test name). Use a fake inner router
  (records `infer` calls + returns a sentinel) and a fake Bedrock client so no
  network is needed:
  - R1.2 — non-plan domain + non-infer attrs delegate to inner verbatim.
  - R1.3 — a stubbed `emit_plan` tool input yields `RouterResult.text` that
    `_parse_plan_json_report` parses into the expected steps.
  - R2.1 — SDK-missing / client-raises / no-tool-use-block → inner `infer("plan")`
    is called exactly once and its result is returned.
  - R2.2 — with `DA_CLOUD_PLAN` unset, `main`-style wiring passes the raw router
    (the shim is not installed).
  - R3.1/R3.2 — a context containing a fake private key forces the local path and
    no cloud client is constructed; a benign context is scrubbed then sent.
  - R4.1 — a successful cloud plan calls `insert_inference` with backend
    `"bedrock"`, domain `"plan"`, and the stubbed token counts.
- **Eval suite:** none in v1 — plan *quality* on Sonnet is a cloud, non-deterministic
  output and the existing `plan_contract` eval already gates the parse/repair
  contract that this path must satisfy. Re-run `plan_contract` against shim output
  (fed the stubbed JSON) to confirm no parser regression.

---

## 6. Tasks

- [ ] 1. Add `inference/cloud_plan_router.py` — `CloudPlanRouter` proxy with the
      plan intercept, forced-tool-use Bedrock call, and transparent fallback —
      satisfies R1.1–R1.4, R2.1, R2.3.
- [ ] 2. Scrub + critical-finding force-local + INFO logging — satisfies R3.1–R3.3.
- [ ] 3. Cost-ledger `insert_inference` + rate-limiter share — satisfies R4.1, R4.2.
- [ ] 4. Wire into `main.py` behind `DA_CLOUD_PLAN` (default OFF); pass raw router
      when off — satisfies R2.2. Add the startup `check(...)` line like the cloud
      answer path so status is visible.
- [ ] 5. `tests/test_cloud_plan_router.py` — one test per criterion above.
- [ ] 6. Update `CLAUDE.md` Known Gotchas + the model-router docstring note, and add
      a memory pointer.
