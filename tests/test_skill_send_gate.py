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

from core.approval_authority import ApprovalAuthority, ApprovalResponse, plan_digest
from inference.dev_agent import DevAgent, AgentStep
import inference.step_executor as se


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
    approval = agent._confirm_destructive_op.await_args.kwargs
    assert approval["approval_action"] == "SKILL_CALL"
    assert approval["security_args"] == {
        "skill_id": "gmail", "tool": "send_reply", "arguments": sent[2],
    }


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


def _issue_plan_grant(agent, digests, *, goal="skill plan", trace="trace-skill"):
    authority = agent._approval_authority
    request = authority.create_request(
        trace_id=trace,
        action_digest=plan_digest(digests, goal=goal, trace_id=trace),
        description="plan",
    )
    authority.write_response(ApprovalResponse(
        request_id=request.request_id,
        action_digest=request.action_digest,
        decision="approve",
        responded_at=request.created_at,
    ))
    grant = authority.consume_response(
        request, approved_digests=digests, plan_goal=goal
    )
    assert grant is not None
    agent._approval_grant = grant


async def test_skill_only_send_uses_whole_plan_grant_without_second_prompt(
    tmp_path, monkeypatch
):
    agent, reg = _agent(send=True)
    agent._approval_authority = ApprovalAuthority(tmp_path / "approval")
    agent._active_trace_id = "trace-skill"
    agent._current_goal = "skill plan"
    step = AgentStep(
        action="SKILL_CALL",
        args='gmail send_reply {"to":"x","body":"hi"}',
        step_num=1,
    )
    digests = await agent._destructive_plan_digests("skill plan", [step])
    _issue_plan_grant(agent, digests)
    gate = AsyncMock(side_effect=AssertionError("unexpected per-op prompt"))
    monkeypatch.setattr(se, "confirm_destructive_op_locked", gate)

    assert await agent._execute_skill_step(step) == "done"
    reg.call.assert_awaited_once()
    gate.assert_not_awaited()


async def test_mixed_write_and_send_share_one_ordered_plan_grant(tmp_path, monkeypatch):
    agent, reg = _agent(send=True)
    agent._approval_authority = ApprovalAuthority(tmp_path / "approval")
    agent._active_trace_id = "trace-skill"
    agent._current_goal = "skill plan"
    agent._active_plan_model = "model"
    agent._router.edit_format_for.return_value = "whole_file"
    write = AgentStep(
        action="WRITE_FILE", args=str(tmp_path / "a.txt"), body="content", step_num=1
    )
    send = AgentStep(
        action="SKILL_CALL", args='gmail send_reply {"to":"x"}', step_num=2
    )
    digests = await agent._destructive_plan_digests("skill plan", [write, send])
    assert len(digests) == 2
    _issue_plan_grant(agent, digests)
    gate = AsyncMock(side_effect=AssertionError("unexpected per-op prompt"))
    monkeypatch.setattr(se, "confirm_destructive_op_locked", gate)

    assert await se.confirm_destructive_op(
        agent,
        "write?",
        step=write,
        canonical_path=write.args,
        content="content",
    )
    assert await agent._execute_skill_step(send) == "done"
    reg.call.assert_awaited_once()
    gate.assert_not_awaited()


async def test_registry_classified_send_is_in_upfront_authority(tmp_path):
    agent, _ = _agent(send=True)
    agent._approval_authority = ApprovalAuthority(tmp_path / "approval")
    agent._active_trace_id = "trace-skill"
    step = AgentStep(
        action="SKILL_QUERY", args='gmail send_reply {"to":"x"}', step_num=1
    )

    digests = await agent._destructive_plan_digests("skill plan", [step])

    assert len(digests) == 1
    assert step._approval_destructive is True


async def test_changed_send_arguments_require_new_per_op_approval(tmp_path, monkeypatch):
    agent, reg = _agent(send=True)
    agent._approval_authority = ApprovalAuthority(tmp_path / "approval")
    agent._active_trace_id = "trace-skill"
    agent._current_goal = "skill plan"
    step = AgentStep(
        action="SKILL_CALL", args='gmail send_reply {"to":"first"}', step_num=1
    )
    digests = await agent._destructive_plan_digests("skill plan", [step])
    _issue_plan_grant(agent, digests)
    step.args = 'gmail send_reply {"to":"changed"}'
    gate = AsyncMock(return_value=False)
    monkeypatch.setattr(se, "confirm_destructive_op_locked", gate)

    result = await agent._execute_skill_step(step)

    assert "cancelled" in result.lower()
    reg.call.assert_not_awaited()
    gate.assert_awaited_once()


async def test_non_send_skill_remains_read_only_for_plan_authority(tmp_path):
    agent, _ = _agent(send=False)
    agent._approval_authority = ApprovalAuthority(tmp_path / "approval")
    step = AgentStep(
        action="SKILL_QUERY", args="gmail list_next_event {}", step_num=1
    )

    assert await agent._destructive_plan_digests("read calendar", [step]) == ()
    assert step._approval_destructive is False
