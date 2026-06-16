"""PR 2 (R-2) — DevAgent episodic recall injection into planner context.

Exercises the real recall path (AgentDB episodic_memory + MemoryManager) through
DevAgent._recall_episodes, plus empty/no-memory no-ops.
"""
import os
import tempfile

from storage.db import AgentDB
from storage.memory_manager import MemoryManager
from inference.dev_agent import DevAgent


async def _open():
    d = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    db = AgentDB()
    await db.open(os.path.join(d.name, "agent.db"))
    return db, d


async def test_recall_formats_block_with_real_store():
    db, d = await _open()
    try:
        mm = MemoryManager(agent_db=db)
        await mm.write_memory_note(
            kind="recovery", goal="fix flaky deploy",
            summary="deploy failed on a missing env var; adding it to the systemd unit fixed it",
            domain="ops",
        )
        agent = DevAgent(router=None)
        agent.set_memory(mm)
        block = await agent._recall_episodes("deploy failing missing env var", n=3)
        assert block.startswith("Past episodes")
        assert "missing env var" in block
        assert "[recovery/normal]" in block
    finally:
        await db.close()
        d.cleanup()


async def test_recall_empty_store_returns_blank():
    db, d = await _open()
    try:
        agent = DevAgent(router=None)
        agent.set_memory(MemoryManager(agent_db=db))
        assert await agent._recall_episodes("nothing here") == ""
    finally:
        await db.close()
        d.cleanup()


async def test_no_memory_returns_blank():
    agent = DevAgent(router=None)   # set_memory never called
    assert await agent._recall_episodes("anything") == ""
