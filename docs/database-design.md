# Database Design — Personal Desktop Agent

**Context:** The project originally stored data across four incompatible formats — `trainer.db` (SQLite), `routing_log.jsonl` (JSONL), `benchmark_results.json`, and `gesture_calibration.json`. This document records the decisions made when consolidating them into two purpose-fit stores.

Diagrams: `.kiro/specs/ipad-sensor-focus/diagrams/14-database-schema.md`  
Implementation: `db.py`

---

## 1. Why Two Stores

**`agent.db` (SQLite)** handles all operational writes from the hot-path pipeline. It is always open, written to on every routed command, and queried synchronously by `ContinuousTrainer` every 5 minutes. SQLite via `aiosqlite` is the right choice here: no server process, async-safe, zero-configuration, and `aiosqlite` was already a dependency.

**`analytics.duckdb` (DuckDB)** handles analytical queries and benchmark storage. DuckDB's columnar engine is significantly faster than SQLite for OLAP workloads — aggregations, percentiles, and joins over millions of rows. The critical architectural choice: DuckDB can attach `agent.db` directly via its sqlite extension (`ATTACH 'agent.db' AS ops (TYPE SQLITE)`), which means all of `agent.db`'s data is queryable from DuckDB without any ETL sync or data duplication. Benchmark data lives natively in DuckDB because it is write-once and read analytically.

**What was rejected:**
- A single SQLite file for everything: SQLite does not support concurrent OLAP queries well, and complex analytical queries (percentiles, window functions over large result sets) are slow.
- A single DuckDB file for everything: DuckDB has no `aiosqlite`-equivalent async driver; putting it on the hot path would require `asyncio.to_thread` wrappers on every insert, adding latency and complexity.
- PostgreSQL/TimescaleDB: overkill for a single-user local tool; server process is an unnecessary dependency.

---

## 2. The Session Anchor

Every `commands` row has a `session_id` foreign key pointing to `sessions`. This was the most structurally important addition over the old JSONL format, which had no session concept at all.

**What it enables:**
- Filter all commands from a specific run: `WHERE session_id = ?`
- Correlate startup metadata (git hash, mode) with the commands that ran under it
- Detect regressions: compare gate distributions across sessions before and after a code change
- Longitudinal analysis: watch how the system's routing behaviour evolves as more examples accumulate

`sessions.mode` distinguishes normal operation from safe-mode testing and benchmark runs, so production data doesn't pollute training sets.

---

## 3. Commands as the Central Fact Table

`commands` is the fact table everything else references. Every significant pipeline event — an inference, a sensor event, a few-shot example, a gesture sample, a DevAgent run — carries an optional `command_id` back-reference.

This is a star schema around the command event. The design tradeoff is that `command_id` is `NULL` for records that were created without a command context (e.g., gesture samples recorded during calibration before any command fired). `NULL` foreign keys are intentional and not a data quality issue — they mean "this event was not triggered by a command."

The `commands` table also carries fields that were previously scattered or absent:
- `gaze_x / gaze_y` — previously only inferred from `gaze_coords` in the `Command` object, never persisted
- `success` — previously inferred from whether an error appeared in the JSONL; now explicit
- `corrected_to` — previously only recorded in the few-shot DB as a new example, now also marks the original command row

---

## 4. Append-Only Tables

Two tables are deliberately append-only and never have rows updated or deleted: `gesture_calibration` and `settings_versions`.

**`gesture_calibration`** stores every calibration event with a timestamp. To get the current floor for a gesture: `SELECT confidence_floor FROM gesture_calibration WHERE gesture=? ORDER BY ts DESC LIMIT 1`. The history of how the floor evolved is a free training signal — you can see whether a floor drifted up (user became more consistent) or down (conditions changed). The old `gesture_calibration.json` overwrote the previous value on every write and had no history.

**`settings_versions`** records every threshold change made by the adaptation loop, user, or benchmark. When a gate threshold gets relaxed at 3am and something breaks, this table tells you exactly when it changed, from what value, to what value, and why (`changed_by = 'adaptation_loop'`).

Both tables grow at low rate (at most a few dozen rows per day) and are cheap to keep forever.

---

## 5. The Embedding Column

`few_shot_examples.embedding` is a nullable `BLOB` column that is always `NULL` in the current implementation. It exists for forward compatibility with semantic retrieval.

**Current retrieval:** Jaccard word overlap × recency decay × log(usage_count). Fast, zero dependencies, works well for the current ~100-row dataset. The scoring function lives in `db.py:_fse_score()`.

**Future upgrade path (no schema migration required):** When `few_shot_examples` grows to thousands of rows and Jaccard overlap misses semantically similar commands ("close the browser" vs "exit Chrome"), populate the `embedding` column with a 384-dimensional float32 vector from a sentence transformer (`all-MiniLM-L6-v2`, ~80MB). Switch `get_few_shot_examples()` to use cosine similarity over the stored embeddings instead of Jaccard. The column is already there — the upgrade is purely in `db.py`.

The BLOB stores raw float32 bytes (`numpy.ndarray.tobytes()` / `numpy.frombuffer()`). 384 floats × 4 bytes = 1536 bytes per row — negligible.

---

## 6. Why `routing_log.jsonl` Was a Problem

The JSONL file had three structural problems that made it progressively worse as the log grew:

1. **Full-file scan every 5 minutes.** `ContinuousTrainer._adaptation_loop()` called `_read_routing_log()` which opened the file and parsed every line on every adaptation pass. At 1000 entries this is fast; at 100,000 entries it becomes noticeable.

2. **No session context.** Every entry was independent — there was no way to ask "what was the gate distribution during the session where I was testing the new threshold?" without correlating timestamps manually.

3. **No referential integrity.** The JSONL had no link to the few-shot examples that were created from the same commands. If you wanted to know "did the commands that ended up as few-shot examples have different gate distributions than the ones that didn't?", you had no way to answer it.

The `commands` table solves all three: the adaptation loop now queries `SELECT route, action FROM commands ORDER BY ts DESC LIMIT 1000`, sessions provide context, and `few_shot_examples.command_id` links back to the source command.

---

## 7. Benchmark Data in DuckDB, Not JSON

`benchmark_results.json` was a flat array of model results from a single run. It had no history — re-running the benchmark overwrote the previous results. Putting benchmarks in `analytics.duckdb` adds:

- **Run history:** every benchmark run gets a row in `benchmark_runs` with a git hash. You can query "how has llama3.2:3b accuracy changed across the last 10 benchmark runs?"
- **Cross-run queries:** `SELECT r.model, AVG(r.p50_ms) FROM benchmark_results r GROUP BY r.model ORDER BY AVG(r.p50_ms)` — impossible from a single JSON array.
- **Cross-database joins:** `ATTACH 'agent.db' AS ops` then `SELECT b.model, AVG(ops.commands.latency_ms) FROM benchmark_results b JOIN ops.commands c ON c.action = 'CLICK' ...` — real production latency vs. benchmark latency in one query.

The JSON fallback in `benchmark_models.py` remains for environments where DuckDB is unavailable. DuckDB is a graceful-degradation optional dependency, consistent with the rest of the project's approach to optional packages.

---

## 8. What Each Legacy File Became

| Legacy format | Problems | Replaced by |
|---|---|---|
| `trainer.db` (SQLite, 3 tables) | No session context; `command_id` backlink missing; embedding path absent | `agent.db` — same 3 tables expanded + 8 new tables |
| `routing_log.jsonl` | Full-file read every 5 min; no session; no referential integrity | `agent.db` `commands` table |
| `gesture_calibration.json` | Overwrote history on every write; in-memory samples lost on crash | `agent.db` `gesture_samples` (full history) + `gesture_calibration` (append-only floor log) |
| `benchmark_results.json` | Single-run snapshot; no history; not queryable | `analytics.duckdb` `benchmark_runs / results / prompts` |

---

## 9. ML Readiness Design Choices

Three specific decisions were made to support future ML work without requiring schema migrations:

**Routing classifier dataset:** `commands` stores all fields the classifier needs as features: `source`, `text`, `whisper_logprob`, `gesture_confidence`, `gate_that_decided` (label). When the log reaches 200+ entries with diverse gate labels, the training set is a single `SELECT` away. The `session_id` link allows temporal train/test splits (train on sessions 1–N, test on sessions N+1 onwards).

**Gesture model training data:** `gesture_samples` stores every individual confidence reading with `lidar_depth_m`. The old code kept only the last 100 samples in memory (lost on shutdown) and a p10 summary in JSON. The DB now has full history — all samples, timestamped, linked to the command they contributed to. Training a confidence calibration model just requires a `SELECT gesture, confidence, lidar_depth_m FROM gesture_samples` export.

**Fine-tuning corpus:** Every `(text, action)` pair in `few_shot_examples` is a training example. The `source` and `domain` columns allow filtering by modality or domain. The `command_id` backlink lets you join to `commands` for additional metadata (whisper confidence, route decision) that can be used as training signal beyond the raw text→verb pair. The `embedding` column is reserved for DPO/contrastive fine-tuning applications where you need the encoded representation alongside the training label.

---

## Analytical Query Patterns

```python
import duckdb
con = duckdb.connect("analytics.duckdb")
con.execute("ATTACH 'agent.db' AS ops (TYPE SQLITE)")

# Gate distribution over last 1000 commands
con.sql("""
    SELECT gate_that_decided, source, COUNT(*) as n,
           ROUND(AVG(latency_ms), 1) as avg_ms,
           PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms) as p95_ms
    FROM ops.commands
    ORDER BY n DESC
""").show()

# Per-model latency trend (agent.db via attachment)
con.sql("""
    SELECT model, DATE_TRUNC('day', TO_TIMESTAMP(ts)) as day,
           COUNT(*) as calls,
           ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY latency_ms), 1) as p50,
           ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms), 1) as p95
    FROM ops.inferences
    GROUP BY model, day
    ORDER BY day DESC, model
""").show()

# Benchmark accuracy over time (analytics.duckdb native)
con.sql("""
    SELECT r.ts::DATE as date, res.model, res.accuracy_pct, res.p50_ms
    FROM benchmark_runs r JOIN benchmark_results res ON res.run_id = r.id
    ORDER BY r.ts DESC, res.accuracy_pct DESC
""").show()

# Routing classifier feature export
con.sql("""
    SELECT source, text, whisper_logprob, gesture_confidence,
           gate_that_decided as label
    FROM ops.commands
    WHERE gate_that_decided IS NOT NULL
      AND source NOT IN ('trackpad')
    ORDER BY ts
""").df()  # returns pandas DataFrame
```
