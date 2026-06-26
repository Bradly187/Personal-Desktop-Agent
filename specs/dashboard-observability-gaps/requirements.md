# Spec: Dashboard Observability Gap Closure

> Closes the gaps found in the `/dashboard` (chat-server observability UI) gap
> analysis of 2026-06-26: signals the system already produces but the dashboard
> never surfaces, plus two dead backend endpoints. One feature → one folder.

---

## 1. Background — the "Why"

The unified observability dashboard (`core/chat_server.py` + `web_client_chat/dashboard.js`,
served on :8770 under `main.py --chat`) renders 7 KPIs, a live Activity feed, and
polled Traces/Trends/Models/Routing/Errors panels. A 2026-06-26 gap analysis found
the **backend already emits far more than the UI shows**: a wired-and-running
`MetricWatcher` publishes threshold alerts that map to nothing on screen; the
Activity feed drops 5 warn-worthy EventBus topics (incl. `voice.drift`, which is
accessibility-critical for Brad's voice channel); the "Now" panel reads
process-lifetime counters so it shows empty after every restart even though a live
session and 30 days of history exist; and two registered endpoints
(`/api/session-live`, `/api/cost`) are never called by the frontend.

For a single accessibility user who relies on the agent running unattended, the
dashboard is the only window into autonomous behavior — silent alerts and a
misleading-empty "Now" panel directly undermine that.

**Status:** Draft
**Owner / author session:** Claude Code

---

## 2. Glossary

- **Dashboard**: the read-only observability page at `GET /dashboard`, JS in `web_client_chat/dashboard.js`.
- **Activity feed**: the live panel fed by `{type:"dash_event"}` WS frames built in `ChatServer._to_dashboard_frame` (`core/chat_server.py`).
- **MetricWatcher**: `monitoring/metric_watcher.py` — background coroutine (wired in `main.py:1299`) that publishes `metric.threshold_crossed` when a KPI breaches a threshold (edge-triggered, hysteresis).
- **SLO breach**: `adaptive/continuous_trainer.py` per-domain latency/success budget violation — currently **logged only**, not published.
- **session-live**: `ChatServer._api_session_live` → `_live_session_kpis()` — on-the-fly KPI rollup for the *current* session (same shape as `session_summaries`), already implemented, never called.
- **cost_rollup / model_usage**: `monitoring/cost_ledger.py` rollups; `model_usage` powers the Model card, `cost_rollup` (temporal) is unused.
- **Backend-health**: live reachability of Ollama (:11434), Bedrock creds, Whisper, and the action proxy (:8768).

---

## 3. Requirements (EARS acceptance criteria)

### Requirement 1: Surface alert + missing ops topics in the Activity feed

**User Story:** As Brad, I want autonomous warn-level events to appear live, so that I notice drift, failures, and alerts without reading logs.

#### Acceptance Criteria
1. THE `ChatServer._to_dashboard_frame` SHALL map `metric.threshold_crossed` to a `dash_event` of kind `alert`, severity `warn`, with the breached metric, value, and threshold in the text.
2. THE `ChatServer._to_dashboard_frame` SHALL map `voice.drift`, `step.failed`, `replan.exhausted`, and `email.arrived` to `dash_event` frames (kinds `voice`, `dev`, `dev`, `email`).
3. THE `core/events.py` module SHALL define a `TOPIC_METRIC_THRESHOLD = "metric.threshold_crossed"` constant, and `MetricWatcher` SHALL publish via that constant (no bare string).
4. WHEN no matching topic fires, THE feed SHALL remain unchanged (additive; the existing 7 mappings are byte-identical).
5. THE dashboard JS SHALL render `severity:"warn"` rows with the existing warn styling and SHALL not crash on an unknown `kind` (falls back to `ev.text`).

### Requirement 2: Alerts / SLO panel

**User Story:** As Brad, I want a panel of active and recent alerts, so that a transient feed row is not the only record of an SLO breach.

#### Acceptance Criteria
1. THE `adaptive.continuous_trainer` SHALL publish an `slo.breached` EventBus event (domain, metric, value, budget, verdict) when it detects a breach it currently only logs.
2. THE `ChatServer` SHALL expose `GET /api/alerts?limit=N` returning recent `metric.threshold_crossed` + `slo.breached` events read from the durable `event_log` (newest first).
3. THE Dashboard SHALL render an **Alerts** panel from `/api/alerts` showing time, source metric, and message, with active (unrecovered) alerts visually distinct.
4. THE `/api/alerts` query SHALL run off the 60 Hz loop via `asyncio.to_thread` (AGENTS.md #2) and SHALL NOT require a schema change (reads existing `event_log`).
5. IF the `event_log` read fails, THEN THE endpoint SHALL return `{"alerts": []}` with HTTP 200 (dashboard degrades to "no alerts", never errors the page).

### Requirement 3: "Now" reflects the live session, not process lifetime

**User Story:** As Brad, I want the Now panel to show the current session's real numbers, so that it isn't empty after every restart.

#### Acceptance Criteria
1. THE Dashboard "Now" panel SHALL source command count, success rate, cloud rate, and p95 from `GET /api/session-live` (current-session rollup) rather than process-lifetime counters.
2. WHILE no session is active OR `/api/session-live` returns `{}`, THE panel SHALL fall back to `/api/metrics` counters (current behavior) and label the source.
3. THE process-lifetime gauges that ARE meaningful across restarts (VRAM free, pain-day, latency EMA) SHALL continue to come from `/api/metrics`.
4. THE "Now" panel SHALL indicate which window each number covers (e.g. "this session" vs "1m") so a zero is never ambiguous.

### Requirement 4: Cost-over-time + retire dead endpoints

**User Story:** As Brad, I want to see cloud spend trend, so that I can catch a cost spike.

#### Acceptance Criteria
1. THE Dashboard SHALL call `GET /api/cost` (`cost_ledger.cost_rollup`) and render a per-day cloud-cost trend (table or sparkline).
2. THE `/api/session-live` and `/api/cost` endpoints SHALL each be either consumed by the frontend (R3, R4.1) or removed; no endpoint SHALL remain registered-but-unused after this spec.
3. THE cost panel SHALL show `$0.00` / empty gracefully when no cloud calls exist in the window.

### Requirement 5: Surface accessibility & backpressure KPIs

**User Story:** As Brad, I want my input-channel quality and the agent's backpressure visible, so that degradation in voice/gesture or a coordinator stall is obvious.

#### Acceptance Criteria
1. THE "Now" panel (or a sibling row) SHALL display the **clarify rate** (`commands_clarify / commands_total`) — how often the agent had to ask Brad to clarify.
2. THE dashboard SHALL display `whisper_logprob_ema` (voice transcription quality) and `gesture_conf_ema` (gesture confidence) when present in the metrics snapshot.
3. THE dashboard SHALL display `scheduler_queue_depth`; WHEN it exceeds its MetricWatcher threshold, THE value SHALL render with warn styling.
4. FOR ALL of these gauges, IF the value is absent from the snapshot, THE card SHALL render `—` rather than `0` (absent ≠ zero).

### Requirement 6: Backend-health strip

**User Story:** As Brad, I want at-a-glance backend status, so that I learn Ollama or Bedrock is down before reading error counts.

#### Acceptance Criteria
1. THE `ChatServer` SHALL expose `GET /api/health-backends` reporting reachability of Ollama (:11434), Whisper, the action proxy (:8768), and whether a Bedrock credential is configured.
2. THE health probe SHALL be cached/throttled (≥10 s) and run off the 60 Hz loop (AGENTS.md #2); a probe SHALL time out quickly (≤2 s) and report `unknown` rather than hang.
3. THE Dashboard SHALL render a health strip with one indicator per backend (up / down / unknown).
4. THE health endpoint SHALL NOT leak secret values — it reports credential *presence*, never the token.

### Requirement 7: Traces & Trends enrichment

**User Story:** As Brad, I want to filter traces and see why one failed, so that I can find the expensive or broken command fast.

#### Acceptance Criteria
1. THE `/api/recent-traces` endpoint SHALL accept optional `source`, `success`, and `action` filters and an adjustable `limit`.
2. THE Traces list SHALL show tokens and (cloud) cost per trace, and a failed trace SHALL show its error reason inline without requiring a replay.
3. THE Trends panel SHALL add latency-p50, tokens/session, and clarify-rate dimensions to the existing per-session table.
4. THE Traces/Trends queries SHALL run off the 60 Hz loop and SHALL NOT change `replay.replay_trace`'s existing contract.

### Requirement 8: DB-backed operational panels

**User Story:** As Brad, I want to see the goal queue, pending approvals, and recent corrections, so that I can see what the agent is about to do and what it has learned.

#### Acceptance Criteria
1. THE `ChatServer` SHALL expose read-only endpoints for: the goal queue (pending/in-flight/failed), the approval / dev-escalation queue, recent corrections, and macros, each reading existing `agent.db` tables.
2. THE Dashboard SHALL render a panel for each, newest-first, with sensible caps.
3. THE approvals panel SHALL be **strictly read-only** — it SHALL NOT expose any approve/deny control; the only approval path remains the voice-approved gate (AGENTS.md #4). <!-- Safe-by-default: no UI bypass of the consent gate. -->
4. THE audit-log panel (if included) SHALL display chain status (intact / broken) read from `audit.db`, never mutate it.
5. FOR ALL new endpoints, an empty table SHALL render an empty-state, never an error.

---

## 4. Technical Design

- **Entry point / pipeline boundary:** all work is in `core/chat_server.py` (HTTP handlers + `_to_dashboard_frame`), `web_client_chat/dashboard.{js,html,css}`, plus one publisher each in `monitoring/metric_watcher.py` (constant) and `adaptive/continuous_trainer.py` (R2.1). **No change to `FusionEngine`, `HybridCoordinator`, `CommandExecutor`, or `coordinator.route()`.**
- **60 Hz protection (AGENTS.md #2):** every new endpoint reads via `asyncio.to_thread`, exactly like the existing `/api/*` handlers. The dashboard is poll/push only; nothing runs on the sensor loop.
- **Persistence (AGENTS.md #1):** **no schema change required.** Alerts read the existing `event_log` (EventBus is already durable); operational panels read existing tables (`goal_session*`, approvals/escalation, corrections, macros, `audit.db`). `storage/db.py` stays at `user_version = 8`. If any panel is later found to need a column, it gets its own migration + version bump in a separate change.
- **New `Command` fields:** none.
- **Models / VRAM:** none (read-only observability; no model load, `ResourceGovernor` untouched).
- **Cross-platform:** none — the dashboard is PC-only; the iPad/bridge protocol is not touched (AGENTS.md #3 N/A).
- **Secrets:** `/api/health-backends` reports Bedrock credential *presence* only (R6.4).

### New / changed server surface

```
events.py            + TOPIC_METRIC_THRESHOLD = "metric.threshold_crossed"
                     + TOPIC_SLO_BREACHED      = "slo.breached"
metric_watcher.py    publish via TOPIC_METRIC_THRESHOLD (was bare string)
continuous_trainer.py + publish TOPIC_SLO_BREACHED on a breach it already logs
chat_server.py       _to_dashboard_frame: + alert, voice.drift, step.failed,
                                            replan.exhausted, email.arrived
                     + GET /api/alerts            (event_log read)
                     + GET /api/health-backends   (throttled probe)
                     + GET /api/goals, /api/approvals, /api/corrections, /api/macros
                     /api/recent-traces: + source/success/action filters, token/cost, error
                     - retire /api/session-live & /api/cost if not wired (they will be)
dashboard.{js,html}  + Alerts panel, Health strip, Cost trend, session-aware Now,
                       accessibility KPIs, trace filters, operational panels
```

### Configuration (flat YAML)

```yaml
dashboard:
  # read-only observability — no agent-behavior flag needed; panels degrade to empty
  health_probe_ttl_s: 10      # cache window for /api/health-backends
  health_probe_timeout_s: 2   # per-backend probe timeout (report 'unknown' on timeout)
  alerts_limit: 50            # default rows for /api/alerts
# metric thresholds remain configured via DA_METRIC_THRESHOLDS (metric_watcher.py)
```

---

## 5. Behavior Verification (executable, not prose)

- **Unit/integration tests** (`tests/test_dashboard_observability.py`), one per criterion:
  - `_to_dashboard_frame` returns the right frame for each new topic (R1.1–R1.2); unchanged for the existing 7 (R1.4).
  - `/api/alerts` reads `event_log` and shapes rows; returns `{"alerts":[]}` on read error (R2.2, R2.5).
  - `continuous_trainer` publishes `slo.breached` on a synthetic breach (R2.1).
  - `/api/session-live` shape + fallback when no session (R3.1–R3.2).
  - `/api/health-backends` caches, times out to `unknown`, and never returns the token (R6.2, R6.4).
  - `/api/recent-traces` honors filters (R7.1).
  - approvals endpoint exposes no mutation route (R8.3) — assert only GET is registered.
- **No new eval suite** — this is read-only UI surfacing; runtime agent behavior is unchanged, so the `evals/` baselines must stay green (regression guard), not grow.

Each §3 criterion maps to ≥1 test above.

---

## 6. Tasks

See [`tasks.md`](tasks.md) — phased P0 (quick wins) → P2.
