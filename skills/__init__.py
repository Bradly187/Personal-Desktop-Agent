"""skills — the agent's MCP-client skill system (N+1 keystone).

Connecting an external MCP server via a JSON manifest (skills/manifests/*.json)
adds capability to the agent WITHOUT editing core/command_executor.py or any
per-feature LLM prompt. Skills are invoked as DevAgent tool-calls
(SKILL_QUERY / SKILL_CALL).
"""
