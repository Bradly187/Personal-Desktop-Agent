---
name: adding-a-connector-skill
description: >
  Add a new MCP-connector skill to this repo — a JSON manifest in skills/manifests/
  plus a FastMCP stdio server in skills/servers/. Use when adding a capability,
  integration, data source, or external tool the agent should reach via voice, WITHOUT
  editing core/command_executor.py or any LLM prompt. Do NOT use for authoring
  agentskills.io SKILL.md skills, or for adding accessibility verbs (CLICK/TYPE/etc).
version: 1.0.0
license: MIT
allowed-tools: Read Edit Write Bash Grep Glob
---

# Adding a connector skill

A *connector skill* (this repo's MCP-client extensibility) is an external MCP server
declared by a manifest. Connecting one adds capability through two DevAgent verbs —
`SKILL_QUERY` (read) and `SKILL_CALL` (send/mutate, voice-gated) — and never touches
the command vocabulary or prompts.

## When to use
- You want a new voice-reachable capability (a new API, file source, integration).
- The work is a discrete tool surface: "search arxiv", "current weather", "my notes".

## When NOT to use
- Adding/altering accessibility verbs (`CLICK`, `TYPE`, …) → that's `command_executor`.
- Authoring an agentskills.io `SKILL.md` (procedural memory) → different primitive.
- Pure prompt/routing tuning → no new server needed.

## Workflow
1. **Read the canonical guide first:** `skills/README.md` (full contract) and the
   reference server `skills/servers/echo_server.py` (the minimal FastMCP shape).
2. **Write the server** `skills/servers/<id>_server.py` with `FastMCP`. Path-lock any
   write tool to its own directory. Mark egress/mutation tools — they become the
   send-gated surface. Lazy-import heavy/optional deps so a missing lib never blocks
   startup (see `skills/servers/google_pim_server.py`).
3. **Write the manifest** `skills/manifests/<id>.json`: `skill_id`, `enabled`,
   `transport: stdio`, `server.command/args`, `tools.allow` (every tool) +
   `tools.send_tools` (the gated ones), and `intents` (keyword → tool, `send`,
   optional `summarize`/`plan`). Front-load distinctive trigger keywords; avoid
   keywords that collide with another skill (longest-match, first-seen wins).
4. **Mind the trust model.** Inbound `SKILL_QUERY` results are untrusted data;
   outbound sends are scrubbed + voice-gated. See `references/trust-model.md`.
5. **Add trigger evals.** Append ≥3 positive + ≥1 negative case to
   `evals/suites/skill_triggers.jsonl`, then
   `python -m evals.run --suite skill_triggers --predictor skill_trigger --update-baseline`.
   Confirm `python -m evals.token_budget` still exits 0 (keywords are always-loaded).
6. **Verify routing** without starting servers:
   `python -m pytest tests/test_evals_skill_trigger.py -q`.

## Anti-patterns
- Don't reinvent an existing MCP server as bespoke scripts — connect it.
- Don't make a write tool send-gated *and* path-unlocked; pick the right guard
  (path-lock for low-blast-radius local writes, send-gate for egress/irreversible).
- Don't pile unrelated tools into one skill — "one skill, one job"; split it.
- Don't hard-code secrets/paths in the server; read from `~/.claude/...` config.
