import pytest
from unittest.mock import MagicMock, patch
import time

from core.pty_manager import manager, PTYSession
from mcp_server.tools import terminal

@pytest.fixture(autouse=True)
def clean_manager():
    # Clean up any sessions before and after tests
    yield
    sessions_to_kill = list(manager.sessions.keys())
    for s_id in sessions_to_kill:
        manager.terminate(s_id)

def test_spawn_and_read():
    # Spawn a simple echo command
    res = terminal.spawn_process("echo hello world")
    assert res["ok"] is True
    session_id = res["session_id"]
    
    # Allow process to start and print
    time.sleep(0.5)
    
    # Read stream
    read_res = terminal.read_stream(session_id)
    assert read_res["ok"] is True
    assert "hello world" in read_res["output"]

def test_send_input():
    # Spawn a python process that reads input and echoes it back
    res = terminal.spawn_process("python -c \"import sys; print(sys.stdin.read())\"")
    assert res["ok"] is True
    session_id = res["session_id"]
    
    time.sleep(0.5)
    
    # Send input and close stdin by sending EOF? We can't send EOF easily 
    # through send_input but we can test if it accepts input without crashing.
    in_res = terminal.send_input(session_id, "test input\n")
    assert in_res["ok"] is True

def test_invalid_session():
    res = terminal.read_stream("invalid_id")
    assert res["ok"] is False
    assert "Invalid" in res["error"]
    
    res = terminal.send_input("invalid_id", "text")
    assert res["ok"] is False

def test_terminate():
    res = terminal.spawn_process("python -c \"import time; time.sleep(10)\"")
    session_id = res["session_id"]
    
    term_res = terminal.terminate_process(session_id)
    assert term_res["ok"] is True
    
    # Wait for process to die
    time.sleep(0.2)
    session = manager.get_session(session_id)
    assert session is None
