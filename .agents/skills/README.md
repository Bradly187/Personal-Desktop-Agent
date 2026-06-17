# Dev / meta skills (agentskills.io `SKILL.md` format)

These are **real [agentskills.io](https://agentskills.io) Agent Skills** — procedural
memory for the *dev-time* agent (Claude Code / Antigravity) that builds and maintains
this repo. Each is a folder with a `SKILL.md` (YAML frontmatter + instructions) and
optional `references/`, loaded on demand via **progressive disclosure** (only the
`description` is always in context; the body loads when it triggers).

They are **not** the runtime accessibility agent's skills — that system
(`skills/manifests/*.json` + FastMCP servers, routed by `SkillRegistry`) is an
**MCP-connector** layer, a different primitive. These `SKILL.md` skills never run on
the 60 Hz pipeline; they encode *how we change the code*, crystallized out of the
prose that used to live (and drift) in `CLAUDE.md` / `AGENTS.md` / `skills/README.md`.

| Skill | Fires when you're about to… |
|-------|------------------------------|
| `adding-a-connector-skill` | add a new MCP-connector skill (manifest + FastMCP server) |
| `changing-the-db-schema`   | add/alter a table, column, or migration in `agent.db` |
| `running-the-eval-harness` | run, lock, or extend the behavioral evals in `evals/` |

**Cross-tool location:** these live in `.agents/skills/` — the shared, version-controlled
convention that Antigravity and other coding agents read (and the one the whitepaper
recommends). Claude Code's native discovery path is `.claude/skills/` (gitignored in this
repo); to have Claude Code auto-load these too, symlink it locally:
`ln -s ../.agents/skills .claude/skills` (or the Windows `mklink /D` equivalent). The
`SKILL.md` format is identical across tools — only the discovery path differs.

Each skill **references** the canonical source (e.g. `storage/db.py`, `evals/README.md`)
rather than duplicating it, so it can't drift out of sync with the code.
