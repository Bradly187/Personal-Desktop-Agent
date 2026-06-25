"""Desktop Agent MCP Server

Exposes Windows desktop control as MCP tools so Claude can orchestrate
the PC directly — mouse, keyboard, screenshots, window management, plus
first-class code search (grep, glob_files) and web fetch (fetch_url).

Set SAFE_MODE=1 to block keyboard_type and mouse_drag (useful for testing).
grep/glob_files are read-only but scoped to the writable-root allowlist;
fetch_url is http(s)-only and its output is trust-classified.

Usage:
    python mcp_server/desktop_mcp_server.py

Claude Code config (~/.claude/claude_desktop_config.json):
    {
      "mcpServers": {
        "desktop-agent": {
          "command": "python",
          "args": ["E:/Personal_Desktop_Agent/mcp_server/desktop_mcp_server.py"]
        }
      }
    }
"""

import json
import os
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import ImageContent, TextContent, Tool

from tools import keyboard, mouse, screen, web, windows
from tools import search as search_tools

# Trust classifier — scans tool outputs for injection patterns before returning to LLM
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# Search/glob tools are scoped to the writable-root allowlist so a direct
# grep/glob_files call can't read outside it (deny-by-default, AGENTS.md #7).
# Resolved lazily + cached: avoids importing the CommandExecutor stack at startup
# and fails closed (empty allowlist → deny) if the resolver is unavailable.
_search_scopes_cache: "list[str] | None" = None


def _search_scopes() -> list[str]:
    global _search_scopes_cache
    if _search_scopes_cache is None:
        try:
            from core.command_executor import _load_writable_roots
            _search_scopes_cache = _load_writable_roots()
        except Exception:  # pragma: no cover - fail-closed
            _search_scopes_cache = []
    return _search_scopes_cache
try:
    from adaptive.mcp_trust_classifier import MCPTrustClassifier, RiskLevel
    _trust_classifier = MCPTrustClassifier()
except ImportError:
    _trust_classifier = None

try:
    from storage.audit_log import AuditLog
    _audit_available = True
except ImportError:
    _audit_available = False

SAFE_MODE = os.environ.get("SAFE_MODE", "0") == "1"

# Audit log instance — initialized in main() if available
_audit: "AuditLog | None" = None

app = Server("desktop-agent")


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

@app.list_tools()
async def list_tools() -> list[Tool]:
    tools = [
        # --- Mouse ---
        Tool(
            name="mouse_move",
            description="Move the mouse cursor to absolute screen coordinates.",
            inputSchema={
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate in pixels"},
                    "y": {"type": "integer", "description": "Y coordinate in pixels"},
                },
                "required": ["x", "y"],
            },
        ),
        Tool(
            name="mouse_click",
            description="Click at the given screen coordinates.",
            inputSchema={
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
                    "clicks": {"type": "integer", "default": 1, "minimum": 1},
                    "interval": {"type": "number", "default": 0.0, "description": "Seconds between clicks"},
                },
                "required": ["x", "y"],
            },
        ),
        Tool(
            name="mouse_double_click",
            description="Double-click at the given screen coordinates.",
            inputSchema={
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                },
                "required": ["x", "y"],
            },
        ),
        Tool(
            name="mouse_scroll",
            description="Scroll at the given coordinates.",
            inputSchema={
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                    "clicks": {"type": "integer", "default": 3, "minimum": 1},
                },
                "required": ["x", "y", "direction"],
            },
        ),
        Tool(
            name="mouse_drag",
            description="Click-drag from start to end coordinates. Blocked in SAFE_MODE.",
            inputSchema={
                "type": "object",
                "properties": {
                    "start_x": {"type": "integer"},
                    "start_y": {"type": "integer"},
                    "end_x": {"type": "integer"},
                    "end_y": {"type": "integer"},
                    "duration": {"type": "number", "default": 0.3, "description": "Drag duration in seconds"},
                },
                "required": ["start_x", "start_y", "end_x", "end_y"],
            },
        ),
        # --- Keyboard ---
        Tool(
            name="keyboard_type",
            description="Type a string of text at the current cursor position. ASCII characters only — use keyboard_paste for unicode or math symbols. Blocked in SAFE_MODE.",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "interval": {"type": "number", "default": 0.0, "description": "Seconds between keystrokes"},
                },
                "required": ["text"],
            },
        ),
        Tool(
            name="keyboard_paste",
            description="Write text to the clipboard then send Ctrl+V. Supports full unicode including mathematical symbols (π, √, ∑, Greek letters, etc.). Blocked in SAFE_MODE.",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to paste, including any unicode or math symbols"},
                },
                "required": ["text"],
            },
        ),
        Tool(
            name="keyboard_hotkey",
            description="Press a key combination, e.g. ['ctrl','c'] or ['win','d'].",
            inputSchema={
                "type": "object",
                "properties": {
                    "keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Keys to press together, e.g. ['ctrl','c']",
                    }
                },
                "required": ["keys"],
            },
        ),
        Tool(
            name="keyboard_press",
            description="Press a single named key: enter, escape, tab, space, backspace, up, down, left, right, etc.",
            inputSchema={
                "type": "object",
                "properties": {
                    "key": {"type": "string"}
                },
                "required": ["key"],
            },
        ),
        # --- Screen ---
        Tool(
            name="screenshot",
            description="Capture the screen and return a base64 PNG. Optionally capture a region.",
            inputSchema={
                "type": "object",
                "properties": {
                    "region": {
                        "type": "object",
                        "description": "Optional crop region",
                        "properties": {
                            "left": {"type": "integer"},
                            "top": {"type": "integer"},
                            "width": {"type": "integer"},
                            "height": {"type": "integer"},
                        },
                        "required": ["left", "top", "width", "height"],
                    }
                },
            },
        ),
        Tool(
            name="get_screen_size",
            description="Return the width and height of the primary monitor in pixels.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="find_text_on_screen",
            description="OCR the screen and locate the given text. Returns {found, x, y, confidence}.",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to search for on screen"}
                },
                "required": ["text"],
            },
        ),
        # --- Windows ---
        Tool(
            name="get_active_window",
            description="Return the title, position, size, and PID of the currently focused window.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="list_windows",
            description="Return a list of all visible top-level windows (title + PID).",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="focus_window",
            description="Bring the first window whose title contains the given substring to the foreground.",
            inputSchema={
                "type": "object",
                "properties": {
                    "title_substring": {"type": "string"}
                },
                "required": ["title_substring"],
            },
        ),
        # --- Search / Web ---
        Tool(
            name="grep",
            description="Regex-search text files under a path. Returns file:line matches. "
                        "Scoped to the writable-root allowlist.",
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Python regex"},
                    "path": {"type": "string", "default": ".", "description": "File or directory to search"},
                    "max_lines": {"type": "integer", "default": 100, "minimum": 1},
                },
                "required": ["pattern"],
            },
        ),
        Tool(
            name="glob_files",
            description="Find files matching a glob pattern (e.g. '*.py' or '**/*.ts') under a path. "
                        "Scoped to the writable-root allowlist.",
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob, e.g. **/*.py"},
                    "path": {"type": "string", "default": ".", "description": "Root directory"},
                    "max_results": {"type": "integer", "default": 200, "minimum": 1},
                },
                "required": ["pattern"],
            },
        ),
        Tool(
            name="fetch_url",
            description="Fetch an http(s) URL and return its extracted text (HTML stripped, capped). "
                        "Output is screened by the trust classifier.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "max_chars": {"type": "integer", "default": 4000, "minimum": 1},
                },
                "required": ["url"],
            },
        ),
    ]
    return tools


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent | ImageContent]:
    try:
        result = _dispatch(name, arguments)
    except Exception as exc:
        if _audit:
            await _audit.log_mcp_call(name, params=arguments, outcome=f"error: {exc}")
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}, indent=2))]

    # Log the MCP call to audit trail
    if _audit:
        outcome = "ok" if not (isinstance(result, dict) and "error" in result) else result.get("error", "unknown")
        await _audit.log_mcp_call(name, params=arguments, outcome=str(outcome)[:200])

    # Return screenshots as native MCP image content so they render properly
    if isinstance(result, dict) and "image_base64" in result:
        return [ImageContent(
            type="image",
            data=result["image_base64"],
            mimeType="image/png",
        )]

    result_text = json.dumps(result, indent=2)

    # Trust classification — scan text results for injection patterns
    if _trust_classifier and isinstance(result, dict):
        # Only classify tools that return content from external sources
        scannable = result.get("text", "") or result.get("matches", "") or ""
        if isinstance(scannable, list):
            scannable = " ".join(str(s) for s in scannable)
        if scannable:
            verdict = _trust_classifier.classify_sync(name, str(scannable))
            if verdict.should_block:
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "warning": "BLOCKED by trust classifier",
                        "risk": verdict.risk.value,
                        "flags": verdict.flags,
                        "recommendation": "This tool output contains suspicious patterns. Review before acting on it.",
                        "original_result": result,
                    }, indent=2),
                )]
            elif verdict.should_warn:
                result_text = json.dumps({
                    "caution": "Trust classifier flagged this output",
                    "risk": verdict.risk.value,
                    "flags": verdict.flags,
                    "result": result,
                }, indent=2)

    return [TextContent(type="text", text=result_text)]


def _dispatch(name: str, args: dict) -> dict:
    # Mouse
    if name == "mouse_move":
        return mouse.mouse_move(args["x"], args["y"])
    if name == "mouse_click":
        return mouse.mouse_click(
            args["x"], args["y"],
            button=args.get("button", "left"),
            clicks=args.get("clicks", 1),
            interval=args.get("interval", 0.0),
        )
    if name == "mouse_double_click":
        return mouse.mouse_double_click(args["x"], args["y"])
    if name == "mouse_scroll":
        return mouse.mouse_scroll(
            args["x"], args["y"],
            direction=args["direction"],
            clicks=args.get("clicks", 3),
        )
    if name == "mouse_drag":
        if SAFE_MODE:
            return {"error": "mouse_drag is disabled in SAFE_MODE"}
        return mouse.mouse_drag(
            args["start_x"], args["start_y"],
            args["end_x"], args["end_y"],
            duration=args.get("duration", 0.3),
        )

    # Keyboard
    if name == "keyboard_type":
        if SAFE_MODE:
            return {"error": "keyboard_type is disabled in SAFE_MODE"}
        return keyboard.keyboard_type(args["text"], interval=args.get("interval", 0.0))
    if name == "keyboard_paste":
        if SAFE_MODE:
            return {"error": "keyboard_paste is disabled in SAFE_MODE"}
        return keyboard.keyboard_paste(args["text"])
    if name == "keyboard_hotkey":
        return keyboard.keyboard_hotkey(*args["keys"])
    if name == "keyboard_press":
        return keyboard.keyboard_press(args["key"])

    # Screen
    if name == "screenshot":
        return screen.screenshot(region=args.get("region"))
    if name == "get_screen_size":
        return screen.get_screen_size()
    if name == "find_text_on_screen":
        return screen.find_text_on_screen(args["text"])

    # Windows
    if name == "get_active_window":
        return windows.get_active_window()
    if name == "list_windows":
        return windows.list_windows()
    if name == "focus_window":
        return windows.focus_window(args["title_substring"])

    # Search / Web
    if name == "grep":
        return search_tools.search_text(
            args["pattern"], path=args.get("path", "."),
            max_lines=args.get("max_lines", 100), scopes=_search_scopes(),
        )
    if name == "glob_files":
        return search_tools.glob_paths(
            args["pattern"], path=args.get("path", "."),
            max_results=args.get("max_results", 200), scopes=_search_scopes(),
        )
    if name == "fetch_url":
        return web.fetch_url(args["url"], max_chars=args.get("max_chars", 4000))

    return {"error": f"Unknown tool: {name}"}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    global _audit
    mode = "SAFE MODE" if SAFE_MODE else "full control"
    print(f"Desktop Agent MCP server running ({mode})", file=sys.stderr)

    # Initialize audit log for MCP tool call recording
    if _audit_available:
        from pathlib import Path
        _audit = AuditLog()
        await _audit.open(Path(__file__).parent.parent / "audit.db")

    async with stdio_server() as streams:
        await app.run(streams[0], streams[1], app.create_initialization_options())

    if _audit:
        await _audit.close()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
