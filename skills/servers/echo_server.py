"""echo_server — a minimal stdio MCP server used as a SkillRegistry proof skill.

Proves the agent's MCP-client loop end-to-end (manifest → spawn → discover →
call → result) with zero side effects. `echo_text` is a read tool; `record_note`
is a stand-in "send"/mutation tool so the send-gate path can be exercised.

Run standalone:  python -m skills.servers.echo_server
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo-skill")


@mcp.tool()
def echo_text(text: str) -> str:
    """Echo the given text back unchanged (read-only)."""
    return text


@mcp.tool()
def record_note(note: str) -> str:
    """Record a note (a stand-in egress/'send' tool; no real side effect)."""
    return f"recorded: {note}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
