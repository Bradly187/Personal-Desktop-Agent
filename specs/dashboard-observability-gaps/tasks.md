# Tasks: Dashboard Observability Gap Closure

> Phased for independent shipping. Each phase = one PR. References to `R#.#` are
> acceptance criteria in [`requirements.md`](requirements.md). No schema change in
> any task (reads existing tables / `event_log`) — `storage/db.py` stays `user_version = 8`.

## Phase P0 — Quick wins (wire up what already exists)
*Effort: XS–S. Highest visible payoff; no new panels, no schema, no backend probes.*

- [x] 1. Add `TOPIC_METRIC_THRESHOLD` (+`TOPIC_SLO_BREACHED`) constants to `core/events.py`; switch `metric_watcher.py` to publish via the constant. — R1.3
- [x] 2. Extend `ChatServer._to_dashboard_frame` with `metric.threshold_crossed` → kind `alert` and `voice.drift` / `step.failed` / `replan.exhausted` / `email.arrived` mappings (+ `slo.breached`); add all to `_DASHBOARD_TOPICS`; existing 7 unchanged. — R1.1, R1.2, R1.4
- [x] 3. Harden `dashboard.js pushFeed` for `severity:"warn"` rows + per-`kind` class + unknown-kind fallback; add `.kind-alert` accent in `style.css`. — R1.5
- [x] 4. Point the "Now" panel at `/api/session-live` with `/api/metrics` fallback; label each number's window; keep VRAM/pain-day/latency-EMA from `/api/metrics`. — R3.1–R3.4
- [x] 5. Tests: frame mappings (new + unchanged) in `test_chat_server_frame_mapping.py`; session-live shape + fallback + topic constants in `test_dashboard_observability.py`. — R1, R3
- [x] 6. Docs: N/A — `CLAUDE.md` has no dashboard description to amend; the spec is the source of truth.

## Phase P1 — Alerts, health, accessibility KPIs
*Effort: M. The "make autonomous problems visible" core.*

- [x] 7. `adaptive/continuous_trainer.py`: publish `slo.breached` (via `set_event_bus`, wired in `main.py`) on the breach it currently only logs. — R2.1
- [x] 8. `GET /api/alerts?limit=N` (`_recent_alerts`) reading `metric.threshold_crossed` + `slo.breached` from `event_log` via `asyncio.to_thread`; `{"alerts":[]}` on error. — R2.2, R2.4, R2.5
- [x] 9. Dashboard **Alerts** panel (active vs recovered). — R2.3
- [x] 10. `GET /api/health-backends` — cached (TTL 10s), per-probe timeout 2s, Bedrock = presence only, never the token. — R6.1, R6.2, R6.4
- [x] 11. Dashboard **Health strip** (Ollama / action-proxy / Bedrock; Whisper is in-process post-cluster-excision, so omitted). — R6.3
- [x] 12. Accessibility/backpressure KPI cards: clarify-rate, `whisper_logprob_ema`, `gesture_conf_ema`, `scheduler_queue_depth` (warn on threshold; `—` when absent). — R5.1–R5.4
- [x] 13. Tests: `slo.breached` publish, `/api/alerts` read+degrade, `/api/health-backends` no-secret. — `test_dashboard_observability.py`

## Phase P2 — Cost trend, trace/trend depth, operational panels
*Effort: M+. Depth + retiring dead surface.*

- [x] 14. Wired `GET /api/cost` into a per-day cloud-cost bar panel (`by_day`); `/api/cost` + `/api/session-live` now both consumed → no registered-but-unused endpoints. — R4.1, R4.2, R4.3
- [x] 15. `recent_traces` + `/api/recent-traces`: `source`/`success`/`action` filters, summed tokens, inline `error_msg`. Per-row $ cost folded into the Cost + Model-usage panels rather than duplicated per trace (token sum is the per-trace cost proxy). — R7.1, R7.2
- [x] 16. Trends: added latency-p50 + corrections columns (both already in `session_trends`). Tokens-per-session is surfaced via the Cost panel's `by_session`; clarify-rate lives in the Now panel (P1) — neither needs a `session_summaries` schema change. — R7.3
- [x] 17. Read-only endpoints + panels: goal queue (`/api/goals`), dev-escalations (`/api/escalations`, **no approve/deny control** — R8.3), corrections (`/api/corrections`). **Macros panel deferred** — no persistent store (in-memory `MacroStore`, feature OFF). Audit-chain panel deferred (R8.4 optional). — R8.1–R8.3, R8.5
- [x] 18. Tests: trace filters + tokens/error, operational read helpers + empty-state, GET-only guard (no `add_post`/`put`/`delete`). — `test_dashboard_observability.py`

## Cross-cutting (every phase)
- [ ] Keep `evals/` baselines green — this spec changes no runtime agent behavior (read-only surfacing).
- [ ] Every new handler reads off the 60 Hz loop via `asyncio.to_thread` (AGENTS.md #2).
- [ ] No schema change; if one becomes unavoidable, it gets its own migration + `user_version` bump in a separate PR (AGENTS.md #1).
