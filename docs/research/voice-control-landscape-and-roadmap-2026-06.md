# Voice Control — Landscape Research, Gap Analysis & Roadmap

**Date:** 2026-06-14
**Scope:** Natural-language *voice* control of the Personal Desktop Agent — how it works
today, how it compares to current voice-control products and research, where it is
logically/functionally weak, and a prioritized roadmap to expand it.
**Priority weighting (per request):** conversational intelligence, latency & feel, and
capability breadth. Accessibility-grid ergonomics (numbered overlays / mouse grid) are
covered but are not the headline.

---

## 1. Executive summary

Our voice path is **robust on the happy path** — wake phrase → one verb → resolve target →
execute → verify — and it has three genuine differentiators that none of the comparison
systems combine: it runs **local-first / private** (Whisper + Ollama on the RTX 5090, cloud
only as fallback), it is **pain-aware** (acoustic thresholds and routing adapt on flare days),
and it has **fail-safe approval + goal sessions** plus **multimodal fusion** (voice fused with
tilt, gesture, and touch). Those are worth protecting as we expand.

But the voice layer was built **one verb at a time**, and it shows its age against 2025–2026
voice systems in three structural ways:

1. **Turns carry text, not state.** The prompt *does* include the last ~5 raw command
   *texts* (`inference/local_inference._build_prompt`, local_inference.py:210) on both the
   local and cloud paths — but raw text is a weak referent: it records that the user *said*
   "click save", not that a CLICK on "Save button" succeeded. There is no dialogue-state
   object holding the resolved target/coords/app, so pronouns and references ("close **it**",
   "do **that** again", "the **other** one") have no antecedent, the local 8B model is left to
   guess at anaphora, and clarification is only one level deep.
   *(Correction to an earlier draft: the local model is **not** starved of context as first
   reported — it receives the raw-text recent-commands list; the real gap is structured state
   + deterministic anaphora resolution.)*

2. **Blocking cascaded pipeline.** VAD → STT → LLM → TTS run to completion in series, so total
   latency is the *sum* of stages rather than the *max*. There is no token streaming to TTS,
   no barge-in, no "stop", and a dispatched action (including a 30 s `RUN_TERMINAL`) blocks the
   single-permit executor while new speech queues behind it.

3. **Closed, brittle intent layer.** Verb extraction takes the first whitespace token; any
   unexpected prefix degrades the whole utterance to `CLARIFY`. Misrecognitions are fixed by a
   hand-curated 12-entry phonetic dictionary, and compound commands ("open Chrome **and** search
   X") are punted to the cloud, which is instructed to emit only the **first** action plus a
   `CLARIFY` for the rest.

The single highest-leverage fix is **not a bigger model** — it is a **dialogue-state object**.
It is small and additive, directly closes the top conversational gaps, and is a prerequisite
for "scratch that", a client-side command sequencer, and barge-in cancellation later.

---

## 2. How our system works today (grounded baseline)

This section is deliberately specific so every later claim is auditable against source.

### 2.1 Speech-to-text and activation — `sensors/whisper_stream.py`
- **STT:** `faster-whisper large-v3` on GPU (remote laptop Whisper service first, local CPU/int8
  fallback, Anthropic Haiku as a last resort). Hotword `initial_prompt` biases the model toward
  app names.
- **VAD / endpointing:** neural `webrtcvad` (30 ms frames) with an RMS-energy fallback; force-
  transcribe after a 30 s buffer cap.
- **Wake model:** `WAKE_PHRASES` ("hey agent", "agent", and mishearing variants), longest-phrase-
  first match after naive punctuation stripping. A bare "hey agent" arms a ~6 s window for the
  next segment. **No barge-in** — the mic is continuously hot when unmuted; suppression windows
  (0.8–1.5 s) are reactive, not interrupt-based.
- **Hallucination filter:** drop segments with `no_speech_prob > 0.5` or `avg_logprob < -0.8`
  (floor is profiler-adaptive). Drops are silent — no user feedback.
- **Clarification gate:** after a `CLARIFY`, a 15 s window accepts a wake-free answer if it is
  < 6 words and `logprob > -0.5`. **One level deep** — the answer is routed as a brand-new
  command.
- **Correction openers:** an utterance beginning "no / wait / actually" after a failed command
  is re-routed as a `voice_correction`. This is the *only* correction affordance.

### 2.2 Routing — `core/hybrid_coordinator.py`
- **4-gate router:** Gate 0 privacy (force local on secrets) → Gate 1 confidence (logprob /
  gesture-confidence) → Gate 2 complexity (>40 tokens or multi-step keywords → cloud) → Gate 3
  VRAM (binary local-or-cloud) → Gate 4 latency EMA budget.
- **Phonetic correction:** `_VOICE_CORRECTIONS`, ~12 hard-coded entries ("clothes"→close,
  "clique"→click, "key row"→kiro, …), applied pre-Gate-1, plus a low-logprob `_retranscribe`.
- **Parse:** first whitespace token → uppercased verb; remainder → target. A first token not in
  `_VALID_COMMAND_VERBS` degrades to `CLARIFY`.
- **Context:** session context (last 5 commands) is embedded **only in the cloud prompt**
  (`hybrid_coordinator.py:196–198`). The local model never sees it.
- **Cloud fallback:** `claude-haiku-4-5`, 10 s circuit-breaker → `CLARIFY`.

### 2.3 Intent → action
- `inference/local_inference.py` — verb-first grammar, system prompt with one-shot examples,
  known-app corrections applied pre-gate.
- `core/domain_classifier.py` — keyword scoring into COMMAND/CODE/MATH/VISION/PLAN/GENERAL.
  Note: `undo`/`redo` are in the command keyword set (`domain_classifier.py:31`) but **no verb
  handles them** — they classify and then fall through.
- `core/command_executor.py` — the 16-verb executor (11 accessibility + 5 dev).
- **Target resolution chain** (`command_executor._resolve_coords` → `desktop/ui_automation.py`
  → `desktop/vision_grounder.py` → `desktop/action_verifier.py`): explicit coords (+magnetic
  snap) → Win32 UIAutomation fuzzy match (~10 supported apps) → vision grounding (confidence
  ≥ 0.7) → gaze coords → screen centre + `CLARIFY`. Post-action perceptual-diff verification
  (2 % pixel threshold); failure emits `CLARIFY` but **never auto-retries**.

### 2.4 Adaptation, safety, extensibility
- `calibration/acoustic_profiler.py` + `voice_calibrator.py` — per-user VAD threshold and
  logprob floor, condition-specific profiles (good/flare/allergy), passive drift detection.
  Recalibration is **manual-triggered**; no sudden-environment adaptation.
- `core/approval_keywords.py` — `classify_confirmation` (deliberate yes/no only, deny wins
  ties, ≤6-word answers); static vocabulary.
- `core/goal_session.py` — one-time voice authorization; deny-by-default Bash allowlist.
- `skills/registry.py` — MCP-client skill model (`SKILL_QUERY`/`SKILL_CALL`), extensible by
  dropping a JSON manifest. `core/proactive_scheduler.py` + `core/event_rule_engine.py` —
  time/event-triggered goals.
- TTS: `tts/polly_stream.py` already exposes `speak_stream(tokens)` (`polly_stream.py:247`) —
  the streaming hook exists but is not used on the voice CLARIFY/EXPLAIN path.

---

## 3. Landscape — what current systems do that we don't

Four reference classes, chosen to bracket the design space: a power-user accessibility grammar
engine (**Talon**), the two mainstream OS accessibility stacks (**Windows Voice Access** /
**Apple Voice Control**), and the **2025–2026 LLM voice-agent** architecture that now defines
"good" for conversational latency.

| Capability | Talon Voice | Win Voice Access / Apple Voice Control | Modern LLM voice agents (gpt-realtime, Gemini Live, LiveKit/Pipecat) | **Us (2026-06)** |
|---|---|---|---|---|
| Streaming STT→LLM→TTS (overlapped stages) | partial | n/a (grammar) | **yes** — 600–900 ms time-to-first-audio | **no** (blocking cascade) |
| Barge-in / "stop" / interrupt | yes | yes | **yes** — <150 ms flush | **no** |
| Semantic turn / backchannel detection | n/a | n/a | **yes** | VAD-only |
| Multi-turn context + pronoun resolution | scripted | limited | **yes** (native) | **no** (local LLM stateless) |
| Compound / chained commands | **yes** (grammar) | yes | yes | cloud, first action only |
| Dictation correction ("scratch that", phonetic spelling) | **yes** | **yes** | partial | "no/wait/actually" only |
| Screen-element addressing ("show numbers" / grid) | **yes** | **yes** | n/a | **no** (UIA/vision instead) |
| Read screen content back (TTS) | yes (screen reader integ.) | yes | yes | **no** (CLARIFY only) |
| Open / extensible vocabulary | **yes** (Python grammars) | fixed | **yes** | closed 16 verbs + MCP skills |
| Local / private | yes | yes | mostly cloud | **yes (advantage)** |
| Per-user acoustic adaptation | yes | some | rare | **yes (advantage)** |

**The lesson from each:**

- **Talon** — power comes from a *composable grammar* and *screen-element addressing*, not a
  larger model. Commands chain and stream continuously ("go down five", "scratch that go up"),
  and its 2025 Conformer-D2 + Whisper model improved background rejection for exactly this
  always-listening, command-dense usage. The takeaway is that a deterministic grammar handles
  the high-frequency verbs faster and more reliably than an LLM round-trip.
- **Windows Voice Access / Apple Voice Control** — "show numbers" / "show grid" (a 9-cell
  drill-down that subdivides on each spoken number) and an explicit **correction / spelling
  mode** are the ergonomic backbone of real hands-free use. These are *cheap, deterministic*
  fallbacks for "I can't name the thing I want to click" — precisely the case where our
  UIA→vision chain is most expensive and most likely to miss.
- **2026 LLM voice stacks** — the win is **architectural, not model size**: stages stream and
  overlap so latency approaches `max(stage)` instead of `sum(stage)`, and a dedicated
  **orchestration layer** owns VAD tuning, a turn-taking model, barge-in, and function-call
  routing. The repeated industry finding — *"a fast mid-tier model with a good prompt beats a
  slow flagship; time-to-first-token matters more than raw reasoning"* — validates our
  llama3.1:8b / Gemma-4 routing choices but indicts our blocking pipeline.

**Differentiators to protect:** local/private execution, pain-aware adaptation, fail-safe
approval + goal sessions, and multimodal fusion. No comparison system has this combination;
every expansion below should preserve it (e.g. barge-in and turn windows should themselves
scale on flare days, and UNDO/sequencer/barge-in must keep fail-safe approval semantics).

---

## 4. Gap analysis — logical & functional weaknesses

Grouped by the three priority axes; each gap tagged with the file it lives in.

### A. Conversational intelligence *(highest weight)*
- **A1 — Context is raw text, not resolved state.** `_build_prompt` injects the last ~5
  command *texts* into both local and cloud prompts (local_inference.py:210), but nothing
  records the *resolved* outcome of a turn (target name, coords, app, success). *Severity:
  medium.* *(Implemented 2026-06-14: `core/conversation_state.py` adds a structured
  last-action hint — see addendum.)*
- **A2 — No anaphora / reference resolution.** "close it", "do that again", "the other one"
  carry no meaning; there is no `last_target / last_action / last_app` memory. *Severity:
  high.* *(Implemented 2026-06-14: deterministic anaphora rewrite — see addendum.)*
- **A3 — Clarification is one level deep.** The answer is routed as a new command; no dialogue
  state machine, so refinements cannot stack ("the blue one" → "no, the dark blue").
  *Severity: medium.*
- **A4 — No real correction / undo loop.** "scratch that" and undo are unsupported; `undo` is
  classified but unhandled (`domain_classifier.py:31`); the D8 opener only catches
  "no/wait/actually". *Severity: high.*
- **A5 — Compound commands degraded.** Gate 2 ships multi-step to the cloud, which emits the
  **first** action + `CLARIFY` (`hybrid_coordinator.py` cloud system prompt). No client-side
  sequencer. *Severity: high.*

### B. Latency & feel
- **B1 — Blocking cascade.** VAD→STT→LLM→TTS serialized; latency is the sum, not the max; no
  token streaming to TTS though `speak_stream` exists (`polly_stream.py:247`). *Severity: high.*
- **B2 — No barge-in / "stop".** A dispatched action (incl. 30 s `RUN_TERMINAL`) blocks the
  single-permit executor (`core/scheduler.py`); new speech queues. *Severity: high.*
- **B3 — VAD-only turn-taking.** Fixed silence windows; no semantic endpointing, no backchannel
  ("uh huh") suppression; the clarification gate is a blunt 6-word / 15 s filter. *Severity:
  medium.*
- **B4 — Binary routing.** Local-full **or** cloud; no smaller-local tier, and a cold specialist
  stalls behind the semaphore. *Severity: medium.*

### C. Capability breadth
- **C1 — No read-screen-back.** SCREENSHOT/READ_SCREEN copy to clipboard or return base64;
  nothing is ever spoken. No "read this to me / summarize this window". *Severity: high.*
- **C2 — Closed verb set + brittle parse.** First-token verb extraction; any unexpected prefix →
  `CLARIFY`; no slot-filling ("scroll down by 3" loses the 3). *Severity: medium.*
- **C3 — No screen-element addressing.** No numbered-overlay / grid fallback when UIA + vision
  both miss — the cheap deterministic path the accessibility products rely on. *Severity:
  medium.*
- **C4 — Shallow per-app control.** UIA covers ~10 apps (`desktop/ui_automation.py`); everything
  else is pixel/vision clicks; no app-specific macros. *Severity: medium.*
- **C5 — No app-state introspection by voice.** "what's focused / which windows are open / what's
  selected" are not voice-callable, though FusionEngine already tracks the active window.
  *Severity: low.*
- **C6 — Hard-coded vocabulary.** Phonetic dictionary (12 entries) and approval words are source
  constants, not user-editable like skills are. *Severity: low.*

---

## 5. Roadmap — prioritized & phased

Sequenced so early items are high-value / low-architectural-risk and unblock later ones. Each
item: problem → approach → primary files → effort → risk.

### Phase V1 — Conversational core *(biggest leverage, mostly additive)*

1. **Dialogue-state object** — *Problem:* A1/A2/A4/A5 all stem from there being no memory of the
   turn. *Approach:* new `core/conversation_state.py` holding `last_target` (resolved coords +
   name), `last_action`, `last_app`, `last_command_text`, and a `pending_clarification` stack;
   populated after each action in `command_executor` / `hybrid_coordinator`. *Effort:* S.
   *Risk:* low.
2. **Feed context to the *local* LLM** — *Problem:* A1. *Approach:* pass a compact state summary
   into `inference/local_inference.py` prompts behind a flag; measure the latency delta against
   the Gate-4 budget. *Effort:* S. *Risk:* low–medium (prompt-length latency).
3. **Anaphora + "again" resolution** — *Problem:* A2. *Approach:* pre-resolve
   "it / that / again / the other" against the dialogue state *before* inference; deterministic,
   no model round-trip when resolvable. *Files:* `hybrid_coordinator.py`, `conversation_state.py`.
   *Effort:* S–M. *Risk:* low.
4. **"Scratch that" / undo** — *Problem:* A4. *Approach:* add an `UNDO` verb + per-verb inverse
   where cheap (TYPE → backspace N; last HOTKEY / edit → `ctrl+z` passthrough; CLICK → none).
   Minimum viable: cancel a pending clarification and re-offer the last `CLARIFY`. *Files:*
   `command_executor.py`, `domain_classifier.py` (wire the existing keyword). *Effort:* M.
   *Risk:* medium (inverse correctness; must stay fail-safe).
5. **Client-side command sequencer** — *Problem:* A5. *Approach:* split compound utterances
   locally on a small connective grammar ("and then / after that") and enqueue ordered
   sub-commands through the existing `core/scheduler.py` instead of cloud-first-action-only.
   *Effort:* M. *Risk:* medium (partial-failure handling between steps).

### Phase V2 — Latency & feel *(architectural; after V1 proves the state model)*

6. **Streaming TTS** — sentence-chunk CLARIFY/EXPLAIN through the existing
   `speak_stream` (`polly_stream.py:247`) so audio starts before generation finishes.
   *Effort:* S–M. *Risk:* low.
7. **Barge-in + "stop" verb** — VAD scores while TTS plays; new speech or the word "stop"
   flushes TTS and cancels the in-flight action. *Approach:* add a cancellation token to the
   `Command` path + a cancel hook in `scheduler.py` and `command_executor.py`. *Effort:* L.
   *Risk:* high (the hardest piece; touches the executor lifecycle). *This is the marquee
   latency item but should follow the dialogue-state work, which gives it the cancel surface.*
8. **Semantic endpointing / backchannel filter** — lightweight classifier on the *partial*
   transcript to shorten/lengthen the silence window and drop backchannels. *Files:*
   `whisper_stream.py`. *Effort:* M. *Risk:* medium.
9. **Tiered local fallback** — add a small fast command-model tier between local-full and cloud
   so trivial verbs stay sub-100 ms warm. *Files:* `inference/model_router.py`,
   `hybrid_coordinator.py` Gate 3/4. *Effort:* M. *Risk:* medium.

### Phase V3 — Capability breadth

10. **Read-screen-back** — `SPEAK_SCREEN` / "read this to me / summarize this window":
    screenshot → vision/OCR → TTS, reusing `desktop/vision_grounder.py` + `_polly_speak`.
    *Effort:* M. *Risk:* low–medium. *(Strong standalone quick win.)*
11. **Grammar-based intent layer** — replace first-token parsing with a small robust slot-filler
    (amounts/directions/targets) for the closed verb set, LLM as *fallback* not primary. *Files:*
    `hybrid_coordinator._parse_action`, `inference/local_inference.py`. *Effort:* L. *Risk:*
    medium.
12. **Numbered-overlay fallback** — when UIA + vision both miss, paint a numbered grid / element
    overlay and accept "click 7" (deterministic, cheap; matches Talon / Windows / Apple).
    *Files:* `desktop/` (new overlay), `command_executor._resolve_coords`. *Effort:* M–L.
    *Risk:* medium (overlay must not destabilize the compositor — see the prior
    `magnetic_overlay` lesson).
13. **App-state verbs** — expose active-window / window-list / selection as voice-callable reads;
    FusionEngine already has the data. *Effort:* S–M. *Risk:* low.
14. **User-extensible vocabulary** — move the phonetic dictionary and approval words into a
    user-editable config (mirroring the skills model). *Files:* `hybrid_coordinator.py`,
    `core/approval_keywords.py`. *Effort:* S. *Risk:* low.

### Cross-cutting — protect the differentiators
Keep everything **local-first**; keep **pain-aware adaptation** in the loop (barge-in and
turn-taking windows should also scale on flare days); keep **fail-safe approval** semantics when
adding UNDO, the sequencer, and barge-in cancellation.

---

## 6. Quick wins vs. big bets

- **Quick wins (days):** dialogue-state object + anaphora (#1–#3), local-LLM context (#2),
  read-screen-back (#10), user-extensible vocabulary (#14).
- **Big bets (architectural):** streaming + barge-in + cancellation (#6–#8), grammar parser
  (#11), numbered-overlay (#12).

**Recommended first build — the dialogue-state object (#1–#3).** It directly closes the top
conversational gaps (A1/A2), is additive and low-risk, and is the prerequisite for "scratch
that" (#4), the command sequencer (#5), and barge-in cancellation (#7). Build it first and the
rest of the roadmap gets materially cheaper.

---

## Addendum — V1.1–V1.3 implemented (2026-06-14)

The recommended first build (dialogue-state core) shipped on branch
`feat/conversation-state`:

- **`core/conversation_state.py`** (new) — `ConversationState` records each resolved `Turn`
  (verb, target, coords, success) in a rolling buffer and offers two pure/deterministic
  services: `resolve_anaphora(text)` (tightly-anchored rewrite of "do that again" / "click it"
  against the previous actionable turn) and `prompt_hint()` (one structured "Last action: …"
  line). Matching is conservative — bare-repeat and `<verb> it/that` patterns only — so
  dictation ("type that's great") and ordinary commands are never hijacked; CLARIFY and failed
  turns are excluded as antecedents.
- **`core/hybrid_coordinator.py`** — wired in three places: constructed in `__init__`; in
  `route()`, voice utterances are anaphora-resolved *before* inference and a `prompt_hint()`
  line is appended to `session_context` (so both local and cloud prompts see resolved state);
  in `_execute_action()`, each turn is recorded post-execution (best-effort).
- **`tests/test_conversation_state.py`** (new, 32 tests) — unit coverage of recording/cap,
  antecedent selection, the anaphora patterns and their negative cases, and the prompt hint,
  plus coordinator-wiring tests (turn recorded after `_execute_action`; "do that again"
  rewritten before inference; hint appended to the next prompt's context).

Still open from Phase V1: "scratch that" / UNDO (#4) and the client-side command sequencer
(#5), both of which now have the dialogue-state surface they need.

## Sources

- [awesome-talon](https://github.com/trillium/awesome-talon) ·
  [Talon community command set](https://github.com/talonhub/community) ·
  [Talon docs](https://talonvoice.com/docs/) ·
  [Talon Beta update (Dec 2025)](https://talonvoice.com/update/qyO6k0Y0jHOeI94q51eTKV/Talon-115-0.4.0-1046-ac6a.html)
- [Apple — Use Voice Control commands on Mac](https://support.apple.com/guide/mac-help/use-voice-control-commands-mh40719/mac) ·
  [Apple Accessibility features](https://www.apple.com/accessibility/features/)
- [Microsoft — Use voice to interact with items on the screen](https://support.microsoft.com/en-us/topic/use-voice-to-interact-with-items-on-the-screen-e1b53c8a-7765-495a-9611-d9b37c008319) ·
  [Microsoft — Use the mouse with voice](https://support.microsoft.com/en-US/accessibility/windows/voice-access/use-the-mouse-with-voice)
- [Retell AI — How real-time voice AI works (STT→LLM→TTS)](https://www.retellai.com/blog/how-real-time-voice-ai-works-stt-llm-tts) ·
  [AssemblyAI — Voice agent architecture](https://www.assemblyai.com/blog/voice-agent-architecture) ·
  [LiveKit — Sequential pipeline architecture for voice agents](https://livekit.com/blog/sequential-pipeline-architecture-voice-agents)
- [Future AGI — Voice AI barge-in & turn-taking (2026)](https://futureagi.com/blog/voice-ai-barge-in-turn-taking-2026/) ·
  [Softcery — Real-time vs turn-based voice agents (2026)](https://softcery.com/lab/ai-voice-agents-real-time-vs-turn-based-tts-stt-architecture) ·
  [getstream.io — Top real-time speech-to-speech APIs](https://getstream.io/blog/speech-apis/)
