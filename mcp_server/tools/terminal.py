"""Interactive Terminal MCP Tools.

Provides a bridge between the MCP server and the core PTYManager
to allow spawning long-running background processes.
"""

from core.pty_manager import manager

def spawn_process(command: str) -> dict:
    """Spawn a background process and return its session ID."""
    try:
        session_id = manager.spawn(command)
        return {"ok": True, "session_id": session_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def read_stream(session_id: str, max_lines: int = 100) -> dict:
    """Read the latest output buffer from a PTY session."""
    session = manager.get_session(session_id)
    if not session:
        return {"ok": False, "error": "Invalid or expired session_id"}
        
    try:
        output = session.read_stream(max_lines=max_lines)
        return {
            "ok": True,
            "output": output,
            "is_alive": session.is_alive()
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

def send_input(session_id: str, text: str) -> dict:
    """Send text to the standard input of a PTY session."""
    session = manager.get_session(session_id)
    if not session:
        return {"ok": False, "error": "Invalid or expired session_id"}
        
    try:
        session.send_input(text)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def terminate_process(session_id: str) -> dict:
    """Terminate a PTY session."""
    manager.terminate(session_id)
    return {"ok": True}
