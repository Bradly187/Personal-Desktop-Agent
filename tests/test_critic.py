"""Tests for the independent code Critic (specs/dev-agent-critic).

Covers the deterministic verdict parser and the WRITE_FILE wiring: a non-pass
verdict blocks the write and drives replan; a low-confidence / error verdict
escalates the approval gate (never weakens it); the disabled default is
byte-identical. The model is stubbed so everything runs in CI.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from inference.critic import (
    BLOCK, PASS, REVISE, Critic, CriticVerdict, Finding, parse_verdict,
)
from inference.dev_agent import AgentStep, DevAgent


# --------------------------------------------------------------------------- #
# parse_verdict (pure, deterministic)
# --------------------------------------------------------------------------- #

def test_parse_clean_pass():
    v = parse_verdict('{"decision":"pass","confidence":0.9,"findings":[]}')
    assert v.decision == PASS and v.confidence == 0.9


def test_parse_unparseable_is_conservative_revise():
    v = parse_verdict("not json at all")
    assert v.decision == REVISE          # never silently passes (AGENTS.md #4)


def test_parse_unknown_decision_becomes_revise():
    v = parse_verdict('{"decision":"looks-fine","confidence":1.0}')
    assert v.decision == REVISE


def test_parse_security_finding_floors_pass_to_revise():
    v = parse_verdict('{"decision":"pass","confidence":0.95,'
                      '"findings":[{"severity":"security","message":"sqli"}]}')
    assert v.decision == REVISE          # a flagged block-severity can't pass


def test_parse_confidence_clamped_and_fenced_json():
    v = parse_verdict('```json\n{"decision":"block","confidence":3.0}\n```')
    assert v.decision == BLOCK and v.confidence == 1.0


def test_parse_block_with_findings():
    v = parse_verdict('{"decision":"block","confidence":0.8,'
                      '"findings":[{"severity":"correctness","message":"off by one","target":"f.py:10"}]}')
    assert v.decision == BLOCK
    assert v.findings[0].target == "f.py:10"


# --------------------------------------------------------------------------- #
# Critic.review (model stubbed)
# --------------------------------------------------------------------------- #

class _Result:
    def __init__(self, text, ok=True, error=None):
        self.text, self.ok, self.error = text, ok, error


class _Router:
    def __init__(self, result):
        self._result = result
        self.calls = 0
        self.last_context = "sentinel"

    async def infer(self, domain, user_text, context=None):
        self.calls += 1
        self.last_context = context
        return self._result


async def test_review_passes_fresh_empty_context():
    router = _Router(_Result('{"decision":"pass","confidence":0.9}'))
    critic = Critic(router, model_domain="plan")
    v = await critic.review(goal="add x", path="f.py", old_text="a", new_text="b")
    assert v.decision == PASS
    assert router.last_context == ""     # R1.2 — no generator context leaks in


async def test_review_raises_on_inference_error():
    critic = Critic(_Router(_Result("", ok=False, error="boom")), model_domain="plan")
    with pytest.raises(RuntimeError):
        await critic.review(goal="g", path="f.py", old_text="", new_text="x")


# --------------------------------------------------------------------------- #
# WRITE_FILE wiring — build a DevAgent with stubbed side effects
# --------------------------------------------------------------------------- #

class _FakeCritic:
    def __init__(self, verdict=None, raises=False):
        self._verdict = verdict
        self._raises = raises
        self.calls = 0

    async def review(self, goal, path, old_text, new_text):
        self.calls += 1
        if self._raises:
            raise RuntimeError("critic down")
        return self._verdict


def _agent_with_critic(verdict=None, *, raises=False, plan_authorized=False,
                       confirm=True, floor=0.6, max_revisions=1):
    agent = DevAgent(router=MagicMock())
    agent._current_goal = "do the thing"
    agent._plan_authorized = plan_authorized
    agent._critic_confidence_floor = floor
    agent._critic_max_revisions = max_revisions
    # Stub the side-effecting helpers.
    agent._apply_edit = MagicMock(return_value="NEW CONTENT")
    agent._write_file = MagicMock(return_value="Written 11 bytes")
    agent._snapshot_for_write = MagicMock(return_value={})
    agent._read_current_for_critic = MagicMock(return_value="OLD")
    agent._confirm_destructive_op = AsyncMock(return_value=confirm)
    agent.set_critic(_FakeCritic(verdict, raises=raises))
    return agent


def _wf_step():
    return AgentStep(action="WRITE_FILE", args="foo.py", body="print(1)")


async def test_pass_high_confidence_writes_without_escalation():
    agent = _agent_with_critic(CriticVerdict(decision=PASS, confidence=0.9))
    step = _wf_step()
    out = await agent._execute_step(step)
    assert "Written" in out
    agent._write_file.assert_called_once()
    # confirm called WITHOUT force (no escalation on a confident pass).
    assert agent._confirm_destructive_op.call_args.kwargs.get("force") is False


async def test_revise_blocks_write_and_returns_diagnostic():
    agent = _agent_with_critic(CriticVerdict(
        decision=REVISE, confidence=0.8,
        findings=[Finding("correctness", "wrong branch", "foo.py")]))
    step = _wf_step()
    out = await agent._execute_step(step)
    assert "revise by critic" in out and "wrong branch" in out
    agent._write_file.assert_not_called()          # no write
    assert step.comp_args is None                   # R2.4 — no compensation registered


async def test_block_blocks_write():
    agent = _agent_with_critic(CriticVerdict(decision=BLOCK, confidence=0.95,
                               findings=[Finding("security", "rm -rf", "foo.py")]))
    out = await agent._execute_step(_wf_step())
    assert "block by critic" in out
    agent._write_file.assert_not_called()


async def test_low_confidence_pass_escalates_confirm():
    agent = _agent_with_critic(CriticVerdict(decision=PASS, confidence=0.2),
                               plan_authorized=True)   # would auto-approve without escalation
    await agent._execute_step(_wf_step())
    # R2.2 — low-confidence pass forces an explicit confirm even on an authorized plan.
    assert agent._confirm_destructive_op.call_args.kwargs.get("force") is True
    agent._write_file.assert_called_once()


async def test_critic_error_fails_safe_to_escalated_confirm():
    agent = _agent_with_critic(raises=True, plan_authorized=True)
    await agent._execute_step(_wf_step())
    # R1.5 — a critic outage never silently auto-approves; it escalates to confirm.
    assert agent._confirm_destructive_op.call_args.kwargs.get("force") is True
    agent._write_file.assert_called_once()


async def test_critic_pass_does_not_bypass_a_denying_gate():
    # R2.3 — the Critic can never WEAKEN a gate: a confirm that denies still blocks.
    agent = _agent_with_critic(CriticVerdict(decision=PASS, confidence=0.9), confirm=False)
    out = await agent._execute_step(_wf_step())
    assert "cancelled by user" in out
    agent._write_file.assert_not_called()


async def test_revise_budget_is_bounded():
    # R1.7 — after max_revisions revise cycles for a path, hand to normal flow
    # (escalate + allow) so a step can't be revised forever.
    agent = _agent_with_critic(CriticVerdict(decision=REVISE, confidence=0.8),
                               plan_authorized=True, max_revisions=1)
    out1 = await agent._execute_step(_wf_step())
    assert "revise by critic" in out1
    agent._write_file.assert_not_called()
    out2 = await agent._execute_step(_wf_step())     # same path, 2nd revise > budget
    assert "Written" in out2
    agent._write_file.assert_called_once()
    assert agent._confirm_destructive_op.call_args.kwargs.get("force") is True


def test_critic_default_on_when_env_unset(monkeypatch):
    # Regression gate (default flipped ON 2026-06-24, baseline dev_critic.json
    # catch_rate=1.0). If this fails, the default was silently reverted to OFF.
    monkeypatch.delenv("DA_CRITIC", raising=False)
    agent = DevAgent(router=MagicMock())
    assert agent._critic_enabled is True and agent._critic is not None


async def test_disabled_critic_is_byte_identical_legacy_path(monkeypatch):
    # R4.1 — with the Critic explicitly OFF, the WRITE_FILE path is the legacy
    # flow: confirm → apply → snapshot → write, and the critic is never consulted.
    monkeypatch.setenv("DA_CRITIC", "0")
    agent = DevAgent(router=MagicMock())
    assert agent._critic is None and agent._critic_enabled is False
    agent._apply_edit = MagicMock(return_value="NEW")
    agent._write_file = MagicMock(return_value="Written 3 bytes")
    agent._snapshot_for_write = MagicMock(return_value={})
    agent._confirm_destructive_op = AsyncMock(return_value=True)
    review_spy = AsyncMock()
    agent._critic_review = review_spy
    out = await agent._execute_step(_wf_step())
    assert "Written" in out
    review_spy.assert_not_called()                   # critic path not taken
