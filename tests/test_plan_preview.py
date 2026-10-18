import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from inference.dev_agent import DevAgent, AgentStep

@pytest.fixture
def mock_router():
    router = MagicMock()
    res = MagicMock()
    res.ok = True
    res.text = "I plan to refactor the database schema and migrate 3 tables."
    router.infer = AsyncMock(return_value=res)
    return router

@pytest.fixture
def dev_agent(mock_router, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    agent = DevAgent(router=mock_router)
    agent._publish_live = AsyncMock()
    return agent


def _approval_message(agent):
    return next(
        call.args[1]["message"]
        for call in agent._publish_live.await_args_list
        if call.args[0] == "dag.approval_requested"
    )


@pytest.mark.asyncio
async def test_preview_off(dev_agent, monkeypatch):
    monkeypatch.setenv("DA_PLAN_PREVIEW", "0")
    steps = [AgentStep(step_num=i, action="write_file", args="file.py") for i in range(5)]
    
    with patch("tts.polly_stream.get_client", side_effect=RuntimeError("no tts")):
        await dev_agent._approve_plan_upfront("My goal", steps)
        
    dev_agent._router.infer.assert_not_called()
    published_msg = _approval_message(dev_agent)
    assert "I'll run 5 steps:" in published_msg

@pytest.mark.asyncio
async def test_preview_below_threshold(dev_agent, monkeypatch):
    monkeypatch.setenv("DA_PLAN_PREVIEW", "1")
    monkeypatch.setenv("DA_PLAN_PREVIEW_THRESHOLD", "3")
    
    steps = [AgentStep(step_num=i, action="write_file", args="file.py") for i in range(2)]
    
    with patch("tts.polly_stream.get_client", side_effect=RuntimeError("no tts")):
        await dev_agent._approve_plan_upfront("My goal", steps)
        
    dev_agent._router.infer.assert_not_called()
    published_msg = _approval_message(dev_agent)
    assert "I'll run 2 steps:" in published_msg

@pytest.mark.asyncio
async def test_preview_on_above_threshold(dev_agent, monkeypatch):
    monkeypatch.setenv("DA_PLAN_PREVIEW", "1")
    monkeypatch.setenv("DA_PLAN_PREVIEW_THRESHOLD", "3")
    
    steps = [AgentStep(step_num=i, action="write_file", args="file.py") for i in range(3)]
    
    with patch("tts.polly_stream.get_client", side_effect=RuntimeError("no tts")):
        await dev_agent._approve_plan_upfront("My goal", steps)
        
    dev_agent._router.infer.assert_called_once()
    published_msg = _approval_message(dev_agent)
    assert published_msg == "I plan to refactor the database schema and migrate 3 tables. Approve all?"

@pytest.mark.asyncio
async def test_preview_failsafe(dev_agent, monkeypatch):
    monkeypatch.setenv("DA_PLAN_PREVIEW", "1")
    dev_agent._router.infer.side_effect = Exception("Model down")
    
    steps = [AgentStep(step_num=i, action="write_file", args="file.py") for i in range(5)]
    
    with patch("tts.polly_stream.get_client", side_effect=RuntimeError("no tts")):
        await dev_agent._approve_plan_upfront("My goal", steps)
        
    published_msg = _approval_message(dev_agent)
    assert "I'll run 5 steps:" in published_msg
