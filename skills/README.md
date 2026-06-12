# Skills — MCP-client extensibility

A **skill** is an external [MCP](https://modelcontextprotocol.io) server declared
by a JSON manifest in `skills/manifests/`. Connecting one adds capability to the
agent **without editing `core/command_executor.py` or any LLM prompt**. Skills run
as DevAgent tool-calls through two verbs:

- `SKILL_QUERY <skill_id> <tool> {json}` — read-only tools
- `SKILL_CALL <skill_id> <tool> {json}` — send/mutation tools (voice-approved)

At startup `SkillRegistry` (`skills/registry.py`) launches each **enabled** manifest's
server over stdio, discovers its tools, and registers the manifest's intent
keywords so utterances route to the `skill` domain.

## Trust model

- **Inbound** read results are untrusted data: every `SKILL_QUERY` result passes
  the `MCPTrustClassifier` before it can reach a prompt (HIGH risk → quarantined).
- **Outbound** send payloads are scrubbed by `ContentFilter` and require the
  fail-safe-DENY voice gate (`DevAgent._confirm_destructive_op`) — no injected
  instruction can cause a send without an explicit spoken "yes".

## Adding a skill

1. Add `skills/manifests/<id>.json`:
   ```json
   {
     "skill_id": "my_skill", "enabled": true, "transport": "stdio",
     "server": {"command": "python", "args": ["-m", "skills.servers.my_server"]},
     "tools": {"allow": ["read_tool", "write_tool"], "send_tools": ["write_tool"]},
     "intents": {
       "do_read":  {"keywords": ["show me x"], "tool": "read_tool",  "send": false},
       "do_write": {"keywords": ["change x"],  "tool": "write_tool", "send": true}
     }
   }
   ```
   An intent may set `"summarize": true` so a read result is summarised on-device
   (e.g. "summarize my inbox").
2. Implement the server with `FastMCP` (see `skills/servers/echo_server.py`).
   `send_tools` are the egress/mutation tools that get gated.

## Bundled skills (auth-free, enabled by default)

| Skill | Say | Notes |
|-------|-----|-------|
| `notes` | "add a note: …", "append to my journal: …", "read my recent notes" | Writes markdown under `~/Notes` (override: `DA_NOTES_ROOT`); the Personal KB indexes it |
| `arxiv` | "any new papers in quant-ph?", "find papers about …", "download the paper 2406.01234" | Open arXiv API; PDFs land in `~/Documents/papers/` (KB-indexed); abstracts summarised on-device |
| `weather` | "what's the weather?", "the forecast", "set my location to Austin Texas" | Open-Meteo, keyless; includes the 12-h barometric **pressure trend** (flare-relevant) |
| `files` | "my recent files", "my latest download", "find the file called budget" | Name search + open across Downloads/Documents/Desktop/Notes (allowlisted); content search belongs to the KB |
| `diagrams` | "draw a diagram of …", "make a wireframe of …", "list my diagrams" | The planner generates Mermaid/SVG, the skill renders+opens it; saved under `~/Documents/diagrams/` |

These write only inside their dedicated directories (path-locked in each
server) and are not send-gated — the blast radius is new files in one folder,
and gating "add a note" behind a voice approval would defeat the accessibility
purpose. An intent may set `"plan": true` when its tool needs LLM-generated
input (the diagrams `create` intent does this), routing the utterance through
the planner instead of a direct call.

## Gmail + Calendar (`google_pim`)

A **locally-owned** server (not a third-party MCP server, so your mail and OAuth
token never leave the machine). Ships **disabled** until connected. Setup:

```bash
pip install google-api-python-client google-auth-oauthlib google-auth-httplib2
# save your OAuth *desktop* client JSON from Google Cloud Console as:
#   ~/.claude/skills/credentials/google_pim/client_secret.json
```

then just say **"connect Google"** — the agent opens the browser consent flow,
auto-enables the skill (via `~/.claude/skills/enabled.json`, never a manifest
edit), and hot-starts it without a restart. (`python -m
skills.servers.google_pim_auth` does the same from a terminal.)

**Token expiry is handled:** every tool returns an actionable "reconnect
Google" message instead of a raw error, and the email watcher speaks a one-time
alert when access expires. Say **"reconnect Google"** to repair it.

Capabilities: "read my next meeting", "summarize my unread email" (read +
on-device summary), "reply to this email" / "create an event" (gated sends). The
refresh token is stored at `~/.claude/skills/credentials/google_pim/token.json`
(0600); scopes are minimal (`gmail.readonly`, `gmail.send`, `calendar.readonly`,
`calendar.events`).
