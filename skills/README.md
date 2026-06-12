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

## Gmail + Calendar (`google_pim`)

A **locally-owned** server (not a third-party MCP server, so your mail and OAuth
token never leave the machine). Ships **disabled**. To enable:

```bash
pip install google-api-python-client google-auth-oauthlib google-auth-httplib2
set GOOGLE_OAUTH_CLIENT_SECRETS=C:\path\to\client_secret.json
python -m skills.servers.google_pim_auth     # one-time browser consent
# then set "enabled": true in skills/manifests/google_pim.json
```

Capabilities: "read my next meeting", "summarize my unread email" (read +
on-device summary), "reply to this email" / "create an event" (gated sends). The
refresh token is stored at `~/.claude/skills/credentials/google_pim/token.json`
(0600); scopes are minimal (`gmail.readonly`, `gmail.send`, `calendar.readonly`,
`calendar.events`).
