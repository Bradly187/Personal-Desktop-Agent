---
name: changing-the-db-schema
description: >
  Change the agent.db SQLite schema safely — add or alter a table, column, index, or
  migration in storage/schema/agent.py. Use whenever you add or change persisted state for the
  runtime agent. Enforces this repo's rules: storage/schema/agent.py is the schema source of truth,
  additive + backwards-compatible migrations gated by PRAGMA user_version, and an
  updated table count. Do NOT use for the DuckDB AnalyticsDB or the ChromaDB stores.
version: 1.0.0
license: MIT
allowed-tools: Read Edit Grep Bash
---

# Changing the agent.db schema

`storage/schema/agent.py` is the **single source of truth** for the `agent.db` schema — never a
doc, never a memory file. Migrations must be additive and backwards-compatible: an
existing database file on a user's machine must keep working after an upgrade.

## When to use
- Adding a new table, or a new column to an existing table.
- Adding an index, or a new persisted counter/log the runtime writes.

## When NOT to use
- Analytics (`AnalyticsDB`, DuckDB `benchmark_*` tables) — separate store.
- Vector stores (`chroma_db`, `personal_kb`) — not SQLite, no migration.
- Read-only queries — no schema change, skip this.

## Workflow
1. **Read `storage/schema/agent.py`** — the `CREATE TABLE IF NOT EXISTS` block, `AgentDB.open()`,
   `AgentDB._migrate()`, and the additive-migration list near `_AGENT_DB_SCHEMA_VERSION`.
2. **New table:** add a `CREATE TABLE IF NOT EXISTS` to the schema block. Idempotent
   create needs no version bump *only* if every reader tolerates its absence; otherwise
   treat it like a migration. (An over-cautious version bump is harmless.)
3. **New column on an existing table:** add it to the additive-migration list
   (`ALTER TABLE … ADD COLUMN`) AND **bump `_AGENT_DB_SCHEMA_VERSION`**. `_migrate()`
   is gated by `PRAGMA user_version`, advancing it only when the whole batch applied,
   so a partial migration retries next boot — do not break that property.
4. **Indexes on migrated columns** are built in `open()` *after* `_migrate()` runs
   (a migrated column doesn't exist yet at first-create time) — follow that ordering.
5. **Reconcile the table count.** The authoritative count + `user_version` live in the
   `CLAUDE.md` "Schema fact" line and `AGENTS.md` rule 1. Update them if you added a
   table. Stale counts in older docs are historical — leave them.
6. **Test the upgrade path**, not just a fresh DB: open an *old* db file (or a copy)
   and confirm `_migrate()` brings it forward with no data loss. Run the db tests:
   `python -m pytest tests/ -k "db or migrat" -q`.

## Anti-patterns
- Don't edit a doc's table count and call the schema "changed" — change `storage/schema/agent.py`.
- Don't write a destructive migration (DROP/rename that loses data); tombstone a dead
  column instead (existing DBs keep the orphan; see the gaze-removal precedent).
- Don't bump `user_version` without a matching migration step, or add a migration
  step without bumping it — the two move together.
- Don't add synchronous/blocking DB work to the 60 Hz path; writes go through
  `async_utils.fire_and_log`.
