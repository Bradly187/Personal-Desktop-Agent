"""Skill send-gate — a SKILL_CALL (egress) tool requires the fail-safe-DENY
voice gate and scrubs its payload before the call leaves the process. Read tools
(SKILL_QUERY) are never gated.

Run:
    python -m pytest tests/test_skill_send_gate.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from inference.dev_agent import DevAgent, AgentStep


def _agent(send: bool = True):
    agent = DevAgent(router=MagicMock())
    reg = MagicMock()
    reg.is_send_tool = MagicMock(return_value=send)
    reg.call = AsyncMock(return_value={"status": "ok", "text": "done"})
    agent._skill_registry = reg
    return agent, reg


async def test_send_denied_not_called():
    agent, reg = _agent(send=True)
    agent._confirm_destructive_op = AsyncMock(return_value=False)  # silence / "no"
    step = AgentStep(action="SKILL_CALL", args='gmail send_reply {"to":"x","body":"hi"}')
    out = await agent._execute_skill_step(step)
    assert "cancelled" in out.lower()
    reg.call.assert_not_called()


async def test_send_approved_scrubs_payload():
    agent, reg = _agent(send=True)
    agent._confirm_destructive_op = AsyncMock(return_value=True)
    aws_key = "AKIAIOSFODNN7EXAMPLE"  # canonical AWS example key — matches the filter
    step = AgentStep(
        action="SKILL_CALL",
        args=f'gmail send_reply {{"to":"x","body":"my key is {aws_key}"}}',
    )
    out = await agent._execute_skill_step(step)
    reg.call.assert_called_once()
    sent = reg.call.call_args.args  # (skill_id, tool, args)
    body = sent[2].get("body", "")
    assert aws_key not in body          # secret redacted before egress
    assert "REDACT" in body.upper()


async def test_read_tool_not_gated():
    agent, reg = _agent(send=False)
    reg.call = AsyncMock(return_value={"status": "ok", "text": "next meeting 3pm"})
    agent._confirm_destructive_op = AsyncMock(return_value=False)
    step = AgentStep(action="SKILL_QUERY", args="gmail list_next_event {}")
    out = await agent._execute_skill_step(step)
    assert "3pm" in out
    agent._confirm_destructive_op.assert_not_called()


async def test_skill_call_verb_forces_gate_even_if_registry_says_read():
    # The verb (SKILL_CALL) is authoritative for gating even when is_send_tool
    # returns False — a planner-emitted send must always be confirmed.
    agent, reg = _agent(send=False)
    agent._confirm_destructive_op = AsyncMock(return_value=False)
    step = AgentStep(action="SKILL_CALL", args='gmail send_reply {"to":"x"}')
    out = await agent._execute_skill_step(step)
    assert "cancelled" in out.lower()
    reg.call.assert_not_called()
