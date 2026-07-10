"""B2 — CRITIC_REJECT crash-path regression.

Spec: specs/bugfix-b2-critic-reject/

The replan critic can veto an entire recovery plan by returning a synthetic
``AgentStep(action="CRITIC_REJECT")``. The handler in ``_try_replan`` previously
crashed two ways, both latent only because ``DA_REPLAN_CRITIC`` defaults OFF:

  1. ``AgentResult`` was built without the required ``model_used`` field
     → ``TypeError`` at dataclass construction.
  2. ``agent._observations`` was dereferenced — an attribute defined nowhere
     → ``AttributeError``.

Either killed the whole plan loop on the first critic rejection. These tests
lock the fixed behavior: the handler swallows the rejection, records a PLAN
step in ``executed``, logs a WARNING (not the except-branch ERROR), and returns
``[]`` so the caller's replan-budget loop decides whether to retry.
"""
import logging

from inference.executors import plan_executor
from inference.plan_parser import AgentStep


def _patch_replan_to_reject(monkeypatch, body="rejected: unsafe recovery plan"):
    """Force ``_replan`` to return a single CRITIC_REJECT step."""
    async def _fake_replan(agent, goal, executed, remaining):
        return [AgentStep(action="CRITIC_REJECT", body=body)]

    monkeypatch.setattr(plan_executor, "_replan", _fake_replan)


async def test_critic_reject_returns_empty_list(monkeypatch):
    _patch_replan_to_reject(monkeypatch)
    # object() as agent: the reject branch must not dereference it (R1.2, R2.1).
    result = await plan_executor._try_replan(object(), "do a thing", [], [])
    assert result == []


async def test_critic_reject_appends_plan_step_to_executed(monkeypatch):
    _patch_replan_to_reject(monkeypatch)
    executed: list[AgentStep] = []
    await plan_executor._try_replan(object(), "do a thing", executed, [])
    assert len(executed) == 1
    assert executed[0].action == "PLAN"
    assert executed[0].step_num == 1


async def test_critic_reject_logs_warning_not_error(monkeypatch, caplog):
    _patch_replan_to_reject(monkeypatch, body="the plan deletes prod")
    with caplog.at_level(logging.WARNING):
        result = await plan_executor._try_replan(object(), "my special goal", [], [])

    assert result == []
    messages = [r.getMessage() for r in caplog.records]
    # Happy path: a WARNING carrying the goal and the rejection body (R2.3).
    assert any("my special goal" in m for m in messages)
    assert any("the plan deletes prod" in m for m in messages)
    # Regression guard: the except-branch ERROR ("handler failed unexpectedly")
    # must NOT fire — its presence would mean a TypeError/AttributeError regressed
    # and was merely swallowed by the try/except (R1.2, R2.1, R3.1).
    assert not any("handler failed unexpectedly" in m for m in messages)
