import os
import pytest
from unittest.mock import AsyncMock, MagicMock

from inference.dev_agent import DevAgent

# Mock classes since core.models might not be easily importable without side effects
class MockAgentStep:
    def __init__(self, step_num, action, args):
        self.step_num = step_num
        self.action = action
        self.args = args

class MockAgentResult:
    def __init__(self, success, steps):
        self.success = success
        self.steps = steps
        self.response_text = "legacy response"

@pytest.fixture
def mock_router():
    router = MagicMock()
    res = MagicMock()
    res.ok = True
    res.text = "This is a walkthrough.\n<spoken>I did the task.</spoken>\nMore text."
    router.infer = AsyncMock(return_value=res)
    return router

@pytest.fixture
def dev_agent(mock_router):
    # Bypass actual DevAgent init which might require db, supervisor, etc.
    agent = object.__new__(DevAgent)
    agent._router = mock_router
    agent._current_goal = "Test goal"
    return agent

@pytest.mark.asyncio
async def test_walkthrough_off(dev_agent, monkeypatch):
    monkeypatch.setenv("DA_POST_RUN_WALKTHROUGH", "0")
    result = MockAgentResult(success=True, steps=[])
    
    spoken = await dev_agent._generate_walkthrough(result)
    assert spoken is None
    dev_agent._router.infer.assert_not_called()

@pytest.mark.asyncio
async def test_walkthrough_on(dev_agent, monkeypatch, tmp_path):
    monkeypatch.setenv("DA_POST_RUN_WALKTHROUGH", "1")
    monkeypatch.chdir(tmp_path)
    
    step1 = MockAgentStep(step_num=1, action="write_file", args="file.py")
    result = MockAgentResult(success=True, steps=[step1])
    
    spoken = await dev_agent._generate_walkthrough(result)
    
    assert spoken == "I did the task."
    dev_agent._router.infer.assert_called_once()
    
    with open("walkthrough.md", "r") as f:
        content = f.read()
    assert "<spoken>" not in content
    assert "This is a walkthrough." in content
    assert "More text." in content

@pytest.mark.asyncio
async def test_walkthrough_no_tags(dev_agent, monkeypatch, tmp_path):
    monkeypatch.setenv("DA_POST_RUN_WALKTHROUGH", "1")
    monkeypatch.chdir(tmp_path)
    
    dev_agent._router.infer.return_value.text = "Just a walkthrough."
    
    result = MockAgentResult(success=True, steps=[])
    spoken = await dev_agent._generate_walkthrough(result)
    
    assert spoken is None
    with open("walkthrough.md", "r") as f:
        content = f.read()
    assert "Just a walkthrough." in content

@pytest.mark.asyncio
async def test_walkthrough_failsafe(dev_agent, monkeypatch, tmp_path):
    monkeypatch.setenv("DA_POST_RUN_WALKTHROUGH", "1")
    monkeypatch.chdir(tmp_path)
    dev_agent._router.infer.side_effect = Exception("Model down")
    
    result = MockAgentResult(success=True, steps=[])
    spoken = await dev_agent._generate_walkthrough(result)
    
    assert spoken is None
    assert not os.path.exists("walkthrough.md")
