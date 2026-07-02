# Spec: Scheduled Tasks over Goal Queue (CG-6)

## 1. Background
The user needs the ability to schedule delayed or recurring background tasks. Instead of building a new bespoke task engine, we leverage the existing `dev_escalations`/`goal_queue` system by adding scheduling capabilities (`execute_at` and `recurrence` rules) to queued goals.

**Status:** Done
**Approved:** Brad, 2026-07-02
**Owner / author session:** Antigravity

---

## 2. Requirements (EARS)
1. THE system SHALL support scheduling goals for future execution via `enqueue_scheduled_goal`.
2. THE `ProactiveScheduler` SHALL poll for due goals, update their status to `queued`, and kick the `DevAgent` drainer to execute them.
3. THE system SHALL support recurring goals (e.g. daily, interval) by re-laying the next occurrence after promoting the current one.
4. THE system SHALL support voice commands to schedule tasks (implemented in `voice_system_control.py`).

---

## 3. Technical Design
- **Database:** `storage/db.py` handles `enqueue_scheduled_goal` and `promote_due_goals`.
- **Scheduler:** `core/proactive_scheduler.py` uses `_tick()` to poll the DB and uses `next_occurrence` to calculate recurring dates.
- **Voice Control:** `core/voice_system_control.py` parses scheduling phrases and creates scheduled goals.

---

## 4. Verification
- Handled entirely in `tests/test_proactive_scheduler.py`.
