# Connector-skill trust model (detail)

Loaded on demand from `adding-a-connector-skill`. The authoritative implementation is
`skills/registry.py` + `inference/dev_agent.py` (`_execute_skill_step`); this is the
mental model, not a second source of truth.

## Two directions, two guards

**Inbound — every `SKILL_QUERY` result is untrusted data.**
A skill talks to the outside world, so its output may carry injected instructions.
Before any result reaches a prompt it passes the `MCPTrustClassifier`; a HIGH-risk
result is quarantined (and remote results are size-capped + fenced as DATA). When an
intent sets `"summarize": true`, the on-device general model summarizes the result
*after* the taint check — raw untrusted text is never spoken on a synthesis failure
(that would be an egress path).

**Outbound — every `SKILL_CALL` send is scrubbed and voice-gated.**
A tool listed in `tools.send_tools` is an egress/mutation. Its payload is scrubbed by
`ContentFilter` (strips secrets/tokens/OAuth) and the call routes through the
fail-safe-DENY voice gate (`DevAgent._confirm_destructive_op`): silence/ambiguity =
DENY. No injected instruction can cause a send without an explicit spoken "yes".

## Choosing the right guard for a write tool

| Tool effect | Guard | Example |
|-------------|-------|---------|
| Local write, tiny blast radius | **Path-lock** the server to one directory; `send: false` | `notes` writes only under `~/Notes` |
| Egress / irreversible / others see it | **Send-gate** (`send_tools` + `send: true`) | `google_pim` send_reply / create_event |

Gating "add a note" behind a voice approval would defeat the accessibility purpose, so
low-risk local writes are path-locked instead of gated. Pick one deliberately — a tool
that is both unlocked *and* ungated is the bug to avoid.

## Enablement vs. routing

The router (and the `skill_triggers` eval) consider **all** manifests regardless of the
`enabled` flag — routing LOGIC is gated, not user state. A skill that needs credentials
(e.g. `google_pim`) ships `enabled: false` and is hot-started after setup via
`~/.claude/skills/enabled.json` (never a manifest edit).
