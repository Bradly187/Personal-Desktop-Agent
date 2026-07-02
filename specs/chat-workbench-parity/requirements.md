# Spec: Chat Workbench Parity (Claude-Code / Antigravity-style transcript)

> One feature → one folder. This spec is the source of truth; the code is the
> build artifact. Design + Tasks kept inline (§4–§6) until they outgrow the file.

---

## 1. Background — the "Why"

The desktop chat UI (`core/chat_server.py` :8770 + `web_client_chat/`) is a
command console with a live DAG viewer. Claude Code and Antigravity present chat
as a **conversation-and-artifact workbench**: rendered markdown, inspectable tool
results, diffs shown before approval, an interruptible run, a reviewable plan,
and a transcript that survives reload. For Brad — driving multi-step dev work
by voice/touch during RA flares — every one of those is an accessibility win:
fewer re-asks, fewer blind approvals, no lost context on an accidental reload.

Evaluation (2026-07-02, this session) found five gaps, plus one **latent
correctness bug**: chat frames do not carry `trace_id` (only `final`/`error`
do), and `chat.js` keeps a single global current-turn — a second message sent
while one is streaming steals the first request's tokens. That fix is
prerequisite plumbing for interrupt, queued messages, and everything below.

**Status:** Building
**Approved:** Brad, 2026-07-02 (spec + §6 task plan approved together in-session — both gates)
**Owner / author session:** Claude Code (Fable 5)
**Related:** `../chat-context-attachments/` (composer + upload), `../plan-preview-voice-gate/`
(CG-7 — the gate this surfaces in chat), `../post-run-walkthrough/` (CG-5 — the
artifact this renders), `../dev-agent-sagas/` (rewind), `../edit-format-aci/`
(the diffs shown in approval cards). Honors AGENTS.md #2 (all new work rides the
chat path, never the 60 Hz loop), #3 (N/A — chat WS is not the iPad bridge
protocol), #4 (approvals keep the existing signal-file authority; nothing
auto-approves), #7 (no new filesystem surface).

---

## 2. Glossary

- **Turn:** one user message + the agent activity and response it produced,
  keyed by its `trace_id`. Today `chat.js` has one implicit global turn (`cur`).
- **Frame:** a JSON message from `ChatServer` to the browser (`token`, `gate`,
  `node`, `approval`, `final`, …), produced by `_to_frame()` from EventBus events.
- **Step card:** the transcript element for one plan step — collapsible, showing
  action, args, status, latency, and the `result_snippet` the server already sends.
- **Artifact card:** a rendered document embedded in the transcript (plan
  preview, post-run walkthrough) — Antigravity's "artifact" concept.
- **Approval signal file:** `~/.claude/approval/response` — the single existing
  approval authority (voice / iPad / chat all write it; first writer wins;
  fail-safe DENY on silence). This spec adds **no** new approval path.

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: Turn correctness — `trace_id` on every frame

**User Story:** As Brad, I want each request's output to land in its own turn,
so that sending a follow-up while the agent is working never corrupts the transcript.

#### Acceptance Criteria
1. THE `ChatServer` event pump SHALL stamp `trace_id` onto EVERY trace-targeted
   frame it forwards (not only `final`/`error`).
2. THE client SHALL maintain a `turns[trace_id]` map and route each frame to its
   own turn's activity log and bubble; frames with an unknown `trace_id` SHALL
   be dropped, never appended to the newest turn.
3. WHILE a request is in flight, WHEN the user sends another message, THE client
   SHALL open a new turn and both turns SHALL render concurrently and correctly
   (the server already handles concurrent traces).
4. FOR ALL existing frame types, a client with old assets SHALL keep working —
   the added field is ignored by the current switch (backward compatible).

### Requirement 2: Markdown transcript

**User Story:** As Brad, I want the agent's answers rendered as markdown, so
that code blocks, lists, and links are readable instead of a wall of plain text.

#### Acceptance Criteria
1. THE client SHALL render assistant bubbles (streamed tokens and `final`
   `response` text) as sanitized markdown: headings, lists, tables, links,
   inline code, and fenced code blocks.
2. FOR ALL rendered HTML, output SHALL be sanitized (no script execution, no
   event-handler attributes) — user-visible text originates from LLM output and
   MUST be treated as untrusted.
3. Fenced code blocks SHALL get a one-click Copy button.
4. WHILE streaming, THE client SHALL re-render the bubble incrementally without
   losing scroll position; IF markdown parsing fails, THEN THE client SHALL fall
   back to plain-text rendering (never a blank bubble).
5. THE renderer SHALL be a vendored static asset under `web_client_chat/vendor/`
   (no build step, works offline — unlike the mermaid CDN import, the transcript
   MUST NOT degrade without internet).

### Requirement 3: Inspectable step cards

**User Story:** As Brad, I want to expand a plan step and see what it actually
did, so that I can audit a run without opening the dashboard replay.

#### Acceptance Criteria
1. WHEN a `node` frame arrives with status `success`/`failed`, THE client SHALL
   render it as a collapsible step card showing action, latency, and the
   `result` snippet the frame ALREADY carries (today it is ignored).
2. Step cards SHALL be collapsed by default and update in place
   (running → success/failed), not append duplicate lines.
3. THE `dag.step_completed` payload SHALL additionally carry `args_snippet`
   (truncated, like `result_snippet`) so the card can show what the step was
   asked to do. Truncation caps ride the server (≤ 2,000 chars per snippet).
4. IF a step failed, THEN its card SHALL render expanded with the error text.

### Requirement 4: Interrupt an in-flight request

**User Story:** As Brad, I want a Stop button (and Esc), so that I can halt a
run that is going wrong without waiting for it to finish or killing the agent.

#### Acceptance Criteria
1. WHEN the client sends `{type:"cancel", trace_id}`, THE server SHALL cancel
   the matching task in `self._requests` and reply
   `{type:"final", trace_id, result:{response:"(cancelled)"}, cancelled:true}`.
2. Cancellation SHALL propagate as `asyncio.CancelledError` through
   `scheduler.submit` → `coordinator.route()`; DevAgent's existing per-step
   saga/compensation owns cleanup of a cancelled plan (no new rollback logic).
3. IF a cancel arrives for an unknown/finished `trace_id`, THEN THE server SHALL
   ignore it silently (idempotent).
4. IF a cancel arrives while the run is blocked on the approval gate, THEN the
   gate SHALL resolve as it does on timeout — **DENY** (AGENTS.md #4); the
   cancel SHALL NOT write an approval response.
5. WHILE a turn is streaming, THE client SHALL show a Stop control on that turn
   and bind Esc to cancelling the newest in-flight turn.

### Requirement 5: Informed approval cards (diff-carrying)

**User Story:** As Brad, I want the approval card to show the exact edit or
command being approved, so that approving is informed, not trust-based.

#### Acceptance Criteria
1. THE `dag.approval_requested` payload SHALL additionally carry, when the
   pending step is a file write/edit: `file_path` and `diff` (unified diff of
   pending change, produced from the already-computed `inference/edit_format`
   result; truncated ≥ 400 lines with a "… N more lines" marker). For a
   RUN_TERMINAL step it SHALL carry the exact `command` string.
2. THE client SHALL render the diff monospaced with +/− line coloring inside
   the existing approval card; long diffs collapse past 20 lines.
3. THE answer path SHALL be unchanged: buttons write yes/no through the existing
   approval signal file — same authority, same fail-safe DENY (AGENTS.md #4).
4. IF the diff/command is unavailable (non-file step, legacy event), THEN the
   card SHALL render exactly as today (message-only) — additive payload.

### Requirement 6: Plan preview card (CG-7 in chat)

**User Story:** As Brad, I want large plans presented in chat for approval
before execution, so that plan preview is not voice-only.

#### Acceptance Criteria
1. WHEN `DA_PLAN_PREVIEW` triggers for a chat-sourced command, THE server SHALL
   forward the preview event as `{type:"plan_preview", trace_id, goal, steps}`
   and THE client SHALL render an artifact card listing the numbered steps with
   Approve / Deny buttons.
2. THE buttons SHALL answer through the existing approval signal file (R5.3) —
   the voice path keeps working in parallel; first responder wins, exactly as
   the voice/iPad race behaves today.
3. IF neither chat nor voice answers within the gate's existing timeout, THEN
   the outcome SHALL remain **DENY** (no behavior change to the gate itself).
4. WHILE `DA_PLAN_PREVIEW` is OFF, THE chat UI SHALL be byte-identical to today.

### Requirement 7: Transcript persistence (survive reload)

**User Story:** As Brad, I want the conversation to survive a page reload, so
that an accidental refresh doesn't erase my working context.

#### Acceptance Criteria
1. THE client SHALL persist completed turns to `localStorage` (single-user,
   localhost UI) and rehydrate them on load, capped (default 100 turns /
   ~2 MB, oldest evicted first).
2. Streaming/in-flight state SHALL NOT be persisted — only completed turns
   (user text, final markdown, collapsed step summaries).
3. THE client SHALL provide a "Clear history" control; clearing is local-only
   (agent.db traces are untouched — the dashboard replay remains the durable
   record).
4. IF `localStorage` is unavailable or corrupt, THEN THE client SHALL start
   with an empty transcript and keep working (never block on rehydration).

### Requirement 8: Run artifacts — walkthrough card, rewind, usage badge

**User Story:** As Brad, I want each completed dev run to show its walkthrough,
an undo affordance, and what it cost, so that review happens in the transcript
instead of my memory.

#### Acceptance Criteria
1. WHEN a post-run walkthrough is generated (`DA_POST_RUN_WALKTHROUGH` ON) for a
   chat-sourced run, THE server SHALL forward it as
   `{type:"walkthrough", trace_id, markdown}` and THE client SHALL render it as
   a collapsible artifact card (markdown per R2).
2. WHEN a dev run completes with persisted saga checkpoints, THE `final` frame
   SHALL carry `rewindable:true`; THE client SHALL show an "Undo this run"
   control on that turn.
3. WHEN "Undo this run" is clicked, THE client SHALL send
   `{type:"rewind", trace_id}` and THE server SHALL route it through the SAME
   rollback path `VoiceRewindHandler` uses — surfaced first as a standard
   approval card (R5) confirming the file list to restore; DENY/timeout rolls
   back nothing (AGENTS.md #4).
4. THE `final` frame SHALL carry a `usage` object — `{model, route, tokens_in,
   tokens_out, cost_usd, latency_ms}` — read off-loop (`asyncio.to_thread`) from
   the trace's spans; THE client SHALL render it as a dim per-turn footer badge.
   IF spans are unavailable, the badge is omitted (never an error).
5. Composer paste: WHEN an image is pasted into the composer, THE client SHALL
   upload it through the EXISTING `/upload` endpoint as `.png` and attach it as
   a chip (`../chat-context-attachments/` R2 rules apply unchanged).

---

## 4. Technical Design

- **Entry point / pipeline boundary:** all server changes live in
  `core/chat_server.py` (frame mapping, cancel/rewind WS message types, payload
  enrichment) plus small event-payload additions where the events are published
  (`inference/dev_agent.py` step/approval/walkthrough events). No new verb, no
  scheduler change, no `Command` field change.
- **R1:** `_event_pump` stamps `frame["trace_id"] = tid` before `client.push`.
  Client: `turns = new Map()`; `newTurn(traceId)`; all handlers take the turn
  from the frame. `user_message` send returns the `trace_id`? No — the server
  generates it; add `{type:"accepted", trace_id}` pushed by `_start_request` so
  the client can bind the pending turn to its trace (client renders the turn
  immediately, binds on `accepted`).
- **R2:** vendor `marked.min.js` + `dompurify.min.js` (2 files, ~60 KB) under
  `web_client_chat/vendor/`; render = `DOMPurify.sanitize(marked.parse(text))`.
  Streaming: re-parse the accumulated buffer per animation frame (throttled),
  not per token. Copy buttons injected post-render.
  *Rejected alternative (log as D-entry at build time):* CDN import like
  mermaid — rejected because the transcript is core UX and must work offline;
  hand-rolled subset renderer — rejected as a correctness/maintenance sink.
- **R3:** `dag.step_completed` publisher adds `args_snippet` (mirror of the
  existing `result_snippet` truncation). Client replaces `activityLine` for
  `node` frames with an updating `<details>` card keyed `(trace_id, n)`.
- **R4:** new WS message `cancel` → `self._requests.get(tid).cancel()`. The
  route task already runs as a background task; `_run_request` catches
  `CancelledError` — change it to push the cancelled-final frame instead of
  re-raising when the cancel came from the client (flag on the task).
- **R5/R6/R8.1:** payload enrichment at the publishing site in DevAgent
  (approval event gains `file_path`/`diff`/`command`; plan-preview and
  walkthrough events forwarded by two new `_to_frame` branches). Confirm exact
  topic names against `specs/plan-preview-voice-gate/` and
  `specs/post-run-walkthrough/` during build — this spec names frame types,
  not EventBus topics.
- **R8.3:** `rewind` WS message calls the same coordinator/rollback entry
  `VoiceRewindHandler` uses, wrapped in the standard approval flow. No new
  rollback implementation.
- **R8.4:** on `final`, `asyncio.to_thread` query of the trace's spans
  (same tables `monitoring/replay.py` reads) → `usage` dict.
- **Models / VRAM:** none new (AGENTS.md #6). **Persistence:** none — no
  `agent.db` change (AGENTS.md #1); transcript history is browser
  `localStorage`. **Cross-platform:** N/A — chat WS is not the iPad protocol
  (AGENTS.md #3).

### Configuration

**As built (2026-07-02):** there is no chat-UI config file today, so the caps
ship as named constants instead of a new YAML surface (deviation from the
draft's YAML block — no config plumbing invented for four numbers):

- `DevAgent._RESULT_SNIPPET_CHARS = 600` / `_ARGS_SNIPPET_CHARS = 200` /
  `_CONFIRM_DIFF_MAX_LINES = 400` (R3.3, R5.1; all within the ≤2000 spec cap)
- `chat.js`: `HISTORY_MAX_TURNS = 100`, `HISTORY_MAX_CHARS = 2e6` (R7.1),
  `DIFF_COLLAPSE_LINES = 20` (R5.2)

R1 (trace_id stamping) and R4 (cancel) ship unconditionally — correctness
fixes, additive and backward-compatible; not flag-gated. No new DA_* flag.

### Deferred (separate specs, not built here)

- `/` slash-command palette over self-skilling macros + skill manifests.
- `@file` mention autocomplete (needs a file-index endpoint scoped to
  `writable_roots`).
- Server-side durable chat history / resume across browsers (localStorage v1
  is deliberate — single user, localhost).
- Multi-session "agent manager" view over `goal_queue` / WorkflowRunner.
- Permission-mode selector (always-ask / auto-approve-safe / plan-only).

---

## 5. Behavior Verification (executable)

- `tests/test_chat_workbench_frames.py` — pump stamps `trace_id` on every
  trace-targeted frame (R1.1); two concurrent traces fan to the right sockets;
  `accepted` frame emitted on `_start_request`; `node` frames carry
  `args_snippet` ≤ cap (R3.3); `final` carries `usage` when spans exist and
  omits it cleanly when not (R8.4).
- `tests/test_chat_cancel.py` — cancel kills the in-flight task and pushes a
  cancelled `final` (R4.1); cancel on unknown tid is a no-op (R4.3); cancel
  during a blocked approval gate leaves the response file unwritten → gate
  DENYs on timeout (R4.4).
- `tests/test_chat_approval_payloads.py` — approval frame passes through
  `file_path`/`diff`/`command` when present and renders message-only shape when
  absent (R5.1/5.4); rewind message routes through the approval flow and rolls
  back nothing on DENY (R8.3).
- Client behavior (markdown sanitization R2.2, turn routing R1.2/1.3,
  localStorage rehydrate/evict/corrupt R7): verified via `scripts/chat_demo.py`
  stub-coordinator walkthrough + a small `tests/test_chat_assets.py` static
  check that vendor files exist and `index.html` references no new CDN for the
  transcript (R2.5).
- Eval suites: no LLM-behavior change → no new eval suite; existing baselines
  must stay green.

---

## 6. Tasks

> **Gate 2 (AGENTS.md #11):** this list is a DRAFT. No task executes until the
> spec is approved (Status → In Progress) AND this plan is explicitly approved.

- [x] 1. `trace_id` on every frame + `accepted` frame; client `turns` map —
      R1 (correctness fix; lands first, everything else keys off it).
- [x] 2. Vendor marked+DOMPurify; markdown bubbles + copy buttons + streaming
      re-render + plain-text fallback — R2.
- [x] 3. Step cards: `args_snippet` at the publisher; collapsible in-place
      `<details>` cards; failed-step auto-expand — R3.
- [x] 4. `cancel` WS message + task cancellation + Stop/Esc UI — R4. (Includes
      the pre-start cancel race: `_on_request_done` resolves a turn whose task
      was cancelled before its coroutine ever ran.)
- [x] 5. Approval payload enrichment (diff/command) + diff rendering in the
      card — R5. Diffs ride the Critic-enabled WRITE_FILE/EDIT_FILE path (the
      legacy Critic-OFF path confirms before the edit is applied, so its card
      is path-only per R5.4). The per-op confirm gained a chat/signal-file
      responder window ONLY when a chat trace is live; voice-only runs are
      byte-identical (verified by test).
- [x] 6. Plan-preview card (CG-7 chat surface) — R6, implemented as `steps` +
      `goal` on the existing plan-approval card (one gate, one card — not a
      separate `plan_preview` frame type as drafted; same authority/timeout).
- [x] 7. localStorage transcript persistence + Clear control — R7.
- [x] 8. Walkthrough card, rewind control (approval-gated, incl. restore file
      list via new read-only `AgentDB.get_checkpoint_compensations`), usage
      badge, paste-to-attach — R8.
- [x] 9. Tests per §5 (22 new across 4 files); `scripts/chat_demo.py` extended
      for new frame types; verified live in the browser preview.
- [x] 10. Doc pass: D023 (vendored markdown renderer, Rule 12); no new gotcha
      (behavior fully specced here); no new DA_* flag.

**Built 2026-07-02** (this spec's §6 both-gates approval: Brad, in-session).
Full suite: 2718 passed; the 2 failures (`test_dev_agent_egress`,
`test_evals_trajectory`) pre-exist on master, verified via stash.
