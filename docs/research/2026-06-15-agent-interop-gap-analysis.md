# Agent Tools & Interoperability — Gap Analysis

*Mapping Google's "Agent Tools & Interoperability" (Day 2, May 2026) whitepaper against the Personal Desktop Agent*

**Date:** 2026-06-15 · **Source:** `Agent Tools & Interoperability_Day_2.pdf` (49 pp.) · **Reviewed against:** `master` per CLAUDE.md (2026-06-15, schema v7, 42 tables) · **Orientation:** practical / build-vs-skip

---

## 1. Executive summary

The whitepaper is a "vibe coder's" tour of five agent-interoperability standards — **MCP**, **A2A**, **A2UI**, **AP2**, **UCP** — plus two cross-cutting concepts (**Skills** as markdown "playbooks", **OpenResponses/Interactions** as stateful inference "power plugs"). Its thesis: stop hand-wiring bespoke integrations; adopt open protocols so your agent's harness becomes a plug-and-play platform and you move from *conductor* to *orchestrator*.

Measured against that frame, the Personal Desktop Agent is **already mature on the dimensions that matter for a single-user, local-first, zero-egress accessibility tool**, and **deliberately absent on the dimensions built for distributed, commercial, multi-vendor ecosystems**. There is exactly **one high-value, mission-aligned gap: A2UI** — agent-generated, declaratively-described UI rendered by the iPad's SwiftUI catalog. Everything else is either done, validated-but-no-action, or correctly out of scope.

| Pillar | Project status | Severity | Action |
|---|---|---|---|
| **MCP — server** (`mcp_server/`) | 14 desktop tools exposed to Claude over stdio | ✅ None | — |
| **MCP — client** (`skills/registry.py`) | Consumes stdio MCP servers via JSON manifests; taint + scrub + send-gate | 🟡 Maturity | Optional hardening |
| **A2A** | Internal specialization (DomainClassifier → ModelRouter, DevAgent DAG, scheduler fan-out); no network boundary | 🟢 Mostly N/A | Document, don't build |
| **A2UI** | iPad UI is 100 % hand-coded SwiftUI; ~11 fixed PC→iPad message types | 🔴 **Real, high-value** | **Build (see plan)** |
| **AP2 / UCP** | None | ⚪ N/A by design | Record decision |
| **Skills / Power Plugs** | "Skill" term reused for MCP servers; statefulness reinvented in orchestration | 🟡 Minor | Note terminology |

---

## 2. What the paper covers (one-line each)

- **MCP ("USB-C")** — consume pre-built tool servers via registries (public / 3P-vetted / internal); configure scope + creds; connect over **stdio** (local) or **SSE/HTTP** (remote); debug with **MCP Inspector** / Chrome DevTools; best practices: *RAG-for-tools*, HITL on tool inputs, audit logging, scoping, read-only, env-var creds.
- **A2A ("Factory Radio")** — the arc *Single-Agent Monolith → internal specialization → distributed multi-agent*; **bounded vs. unbounded** domains; the **GOTO problem** (collaborative agents need pause/negotiate/resume, unlike fire-and-forget tools); **Agent Card** (machine-readable CV); public/private **registries**; expose via Agent Executor + endpoint; monetize as **AaaS** (incl. x402/L402 micropayments).
- **A2UI ("Generative Display Window")** — agents emit **declarative UI intent** (a flat component adjacency list) against a **trusted catalog**; any renderer (React, Flutter, **SwiftUI**, …) performs it natively. Two patterns: **LLM-generates-UI** (intent-driven) and **tool-as-template** (deterministic). Safe because the agent ships *intent*, never executable code or pixels. Canvas for persistent shared workspaces; hybrid `data`+`ui` output.
- **AP2 / UCP** — agentic commerce: UCP = machine-readable catalogs/menus; AP2 = signed spend **mandates** ("up to $25 at X") with cryptographic proof-of-payment.
- **Skills / Power Plugs** — markdown+script playbooks in a sandbox; stateful long-running inference APIs that blur stateless-turn vs. stateful-agent.

---

## 3. Detailed findings

### 🔴 3.1 A2UI — the one gap worth building

**This is the headline.** A2UI's premise is precisely what the accessibility mission needs, and **SwiftUI is explicitly named as a supported A2UI renderer** (p.33).

**Current state (grounded in code):**
- The iPad surface is entirely **pre-built SwiftUI**. Every sheet, dashboard, banner, and toast is a hand-coded view; new interaction patterns require Swift change → rebuild → TestFlight.
- PC→iPad is a **closed set of ~11 typed messages** (`WebSocketManager.swift:394–479`): `ack`, `status`, `mic_state`, `screenshot`, `handwriting_result`, `recalibration_request`, `proactive_notification`, `calibration_*`. Each maps to one bespoke UI reaction.
- **CLARIFY is voice-only**: `command_executor.py:664` speaks the question through Polly/SAPI and returns. The user must answer by voice — the hardest channel on a flare day.
- The approval gate is voice-confirmation only (`approval_hook.py` + `core/approval_keywords.py`).

**Why A2UI is uniquely aligned here (not just a generic "nice to have"):**
1. **Accessibility payoff is direct.** A2UI components render as large touch targets — `DesignTokens.Size.touchTargetMin = 80pt`, and `DAButton` already enforces it. For a user with RA, **tapping a generated Approve/Deny or multiple-choice card beats precise voice disambiguation or pointer work**. This converts the project's weakest interaction (voice clarification under flare) into its strongest (touch).
2. **The catalog substrate already exists.** The paper's "bring-your-own catalog" recommendation (p.34) is satisfied by the existing `DesignSystem/` (`DAButton`, `DACard`, `DAConnectionBanner`, `DASectionHeader`). A2UI types map onto components you've already built and accessibility-annotated.
3. **Both patterns fit clean seams.** *Tool-as-template* → deterministic flows (approval = fixed Approve/Deny; schedule confirmation). *LLM-generated* → novel CLARIFY disambiguation. The DomainClassifier/coordinator already decides where each command lands.
4. **Security model matches zero-egress.** The agent emits declarative JSON instantiating only trusted catalog components — **no executable code, no pixels, no new egress** (p.33). This is the rare new capability that *adds no attack surface*.
5. **Removes real rebuild friction.** New agent-driven interactions stop requiring an iPad binary cycle — a recurring cost flagged across the CI notes.

**Empirical backing (agent.db audit, 2026-06-15).** Of 405 logged commands, **65 are real user-facing CLARIFY events (16 %)** — the single largest non-CLICK outcome. Bucketed by answerability:

| Bucket | Share | Example |
|---|---|---|
| **Enumerable** (fixed buttons) | **42 %** | scroll direction up/down/left/right; "app, file, or other?"; "click, drag, or dwell?" |
| **Semi** (dynamic list) | **28 %** | "what would you like to open?" → recent-apps list |
| Free-form (voice stays) | 31 % | "what is the click target?" |

So **~70 % of clarifications could be resolved with a tap.** That is the quantitative case for A2UI, and it specifically justifies extending past the approval gate (Phase 1) into CLARIFY (Phase 2).

**Scope discipline:** this is *not* a call to adopt the full Google `a2ui-agent-sdk`/ADK stack (that is Gemini/Flutter-oriented and egress-coupled). Adopt the **A2UI message shape and catalog discipline**; render it with a small native Swift `A2UIRenderer` over the existing design system. See the companion plan: [`2026-06-15-a2ui-integration-plan.md`](./2026-06-15-a2ui-integration-plan.md).

**Status (2026-06-15):** Phase 1 (approval-gate) scaffold landed on this branch — `core/a2ui.py`, bridge send/event wiring, the Swift `A2UIRenderer`/`A2UIOverlay`, and the WhisperStream gate-open trigger. PC side is unit-tested (123 passing in the bridge/whisper/approval/a2ui subset); end-to-end verification needs the iPad (one build to ship the renderer).

---

### 🟡 3.2 MCP consumption — three maturity items

You already consume MCP more rigorously than the paper's baseline (`SkillRegistry` owns stdio sessions; `MCPTrustClassifier` taint; `ContentFilter` outbound scrub; fail-safe-DENY send-gate; `skill_invocations` audit). Three best-practices remain open:

1. **RAG-for-tools / dynamic tool loading** (p.15). The paper warns that holding every tool schema in context dilutes attention. You now run 7+ always-enabled skill servers. **Recommendation:** load a skill's tool schemas into the prompt **only when the DomainClassifier routes `skill`**, and drop them afterward. The domain gate is already the natural seam; this keeps the high-frequency *command* path's prompt lean.
2. **Registry-based discovery** (p.11). Static manifests in `skills/manifests/` are correct for one user, but if skill count keeps growing, an internal registry index (you already have the `enabled.json` override layer) is the paper's recommended evolution.
3. **MCP Inspector in the dev loop** (p.15). When a skill mis-calls a tool you currently debug via the test suite. Adding MCP Inspector lets you inspect raw JSON-RPC 2.0 transport without booting the full pipeline — a cheap dev-workflow win.

**Already satisfied** (no action): HITL on tool inputs ✅, audit logging ✅ (hash-chained `audit.db`), no hardcoded creds ✅ (0600 token files / OAuth), scoping ✅ (writable-root allowlist + path-scope), untrusted-server handling ✅ (HIGH-trust drop on remote).

---

### 🟢 3.3 A2A — validate the architecture, don't build the protocol

The paper's maturity arc *describes your trajectory exactly.* You sit firmly at **internal specialization**: sub-agents (`DomainClassifier` → `ModelRouter` specialists, `DevAgent` plan/DAG, `scheduler.fan_out`) share one runtime with no network boundary — which the paper explicitly endorses for low-latency, simplified-state single-process apps (p.20). Distributed A2A runs **counter to the zero-egress design value**, so it is a deliberate non-gap for the core agent.

Two extractions worth banking:
- **The "GOTO / bounded-vs-unbounded" framing (pp.23–24) names machinery you already built.** Your `DevAgent` does pause / clarify / negotiate / resume via **goal sessions**, **replan loops**, and the **`dev_escalations`** human-review queue — the paper's exact prescription for collaborative-agent control flow. No code gap; a `docs/architecture/` framing opportunity (explain *why* the escalation queue and goal-session isolation exist in A2A vocabulary).
- **One genuine future angle: the iPad↔PC (and future multi-device) relationship.** If the PC agent should ever be "hireable" by another orchestrator or a second device, an **Agent Card** (capabilities + security policy + interaction schema, p.25) is the standard exposure. Park it; do not build now.

---

### ⚪ 3.4 AP2 / UCP — out of scope by design

No commerce dimension. The only money flow on the roadmap is the planned **$9.99 StoreKit subscription**, which is App-Store billing — not agentic payments. AP2/UCP are correctly absent. Recorded here so it reads as a *decision*, not an oversight. (If the agent ever autonomously procures anything on the user's behalf — e.g., ordering supplies — revisit AP2's signed-mandate model as the safety pattern.)

---

### 🟡 3.5 Skills & stateful inference — terminology / minor

- **"Skill" collision.** The whitepaper's *Skill* = markdown playbook + scripts in a sandbox (the same sense as this Claude Code environment). The project's `skills/` = MCP **servers**. Same word, different concept. No action beyond awareness when reading cross-project material; the project's usage predates and is internally consistent.
- **Stateful inference ("Power Plugs").** The paper's OpenResponses/Interactions APIs offer server-side long-running stateful turns. Local Ollama/vLLM don't, so the project reinvents statefulness in its orchestration layer (`agent_runs` lifecycle, goal sessions, context namespacing). This is the correct local-first trade; noted only for completeness.

---

## 4. Recommendation

**Build A2UI (tool-as-template first).** It is the single item in the paper that simultaneously (a) advances the RA-accessibility mission via large tappable targets, (b) reuses an asset already built (the design system), (c) preserves zero-egress, and (d) removes iPad-rebuild friction. Start by routing the **approval gate** and **CLARIFY** through a declarative `a2ui_surface` message + a native `A2UIRenderer`.

**Optionally harden MCP consumption** (RAG-for-tools is the most worthwhile of the three).

**Take no action on A2A/AP2/UCP** beyond a short `docs/architecture/` note that frames the existing escalation/goal-session machinery in A2A's bounded/unbounded vocabulary, and records AP2/UCP as a deliberate non-goal.

---

*Companion: [`2026-06-15-a2ui-integration-plan.md`](./2026-06-15-a2ui-integration-plan.md) — concrete message schema, renderer design, and phased wiring.*
