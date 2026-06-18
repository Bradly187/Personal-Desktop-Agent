"""GAP-4 — TrajectoryReplayer scores the EXECUTED trajectory from persisted spans.

extract_executed_verbs pulls plan verbs from span attrs (action/verb) in seq
order; .score() then reuses score_trajectory so the same required/precedence/
forbidden constraints gate real trajectories. The async path reads spans back
through AgentDB.

Run:
    python -m pytest tests/test_trajectory_replay.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from storage.db import AgentDB
from monitoring.trace import TraceRecorder
from evals.trajectory import TrajectoryCase, TrajectoryReplayer


def _spans(*pairs):
    """Build span dicts: each pair is (seq, attrs)."""
    return [{"seq": s, "stage": "step", "ts": 0.0, "attrs": a} for s, a in pairs]


def test_extract_executed_verbs_orders_and_filters():
    r = TrajectoryReplayer(agent_db=None)
    spans = _spans(
        (0, {"action": "READ_FILE"}),
        (2, {"verb": "GIT_COMMIT"}),
        (1, {"action": "WRITE_FILE"}),
        (3, {"action": "NOT_A_VERB"}),     # dropped — unknown
        (4, {"route": "local"}),            # dropped — no verb key
    )
    assert r.extract_executed_verbs(spans) == ["READ_FILE", "WRITE_FILE", "GIT_COMMIT"]


def test_score_required_and_precedence():
    case = TrajectoryCase(
        id="t1", suite="x", goal="edit then commit",
        required=["WRITE_FILE", "GIT_COMMIT"],
        precedence=[["WRITE_FILE", "GIT_COMMIT"]],
    )
    r = TrajectoryReplayer(agent_db=None)
    good = r.extract_executed_verbs(_spans((0, {"action": "WRITE_FILE"}),
                                           (1, {"action": "GIT_COMMIT"})))
    from evals.trajectory import score_trajectory, TrajPrediction
    res = score_trajectory(case, TrajPrediction(verbs=good))
    assert res.exact and res.required_ok and res.order_ok


def test_forbidden_verb_fails_safe():
    case = TrajectoryCase(
        id="t2", suite="x", goal="summarize only (read-only)",
        required=["READ_FILE"], forbidden=["WRITE_FILE"],
    )
    r = TrajectoryReplayer(agent_db=None)
    from evals.trajectory import score_trajectory, TrajPrediction
    verbs = r.extract_executed_verbs(_spans((0, {"action": "READ_FILE"}),
                                            (1, {"action": "WRITE_FILE"})))
    res = score_trajectory(case, TrajPrediction(verbs=verbs))
    assert not res.safe_ok and not res.exact


async def test_async_replay_through_db(tmp_path):
    db = AgentDB()
    await db.open(tmp_path / "replay.db")
    if not db.available:
        pytest.skip("aiosqlite unavailable")

    t = TraceRecorder(enabled=True)
    tid = t.new_trace(kind="plan")
    t.record_span("step", trace_id=tid, action="WRITE_FILE")
    t.record_span("step", trace_id=tid, action="RUN_TERMINAL")
    await t.persist_trace(tid, db, session_id=1)

    case = TrajectoryCase(
        id="t3", suite="x", goal="write then test",
        required=["WRITE_FILE", "RUN_TERMINAL"],
        precedence=[["WRITE_FILE", "RUN_TERMINAL"]],
    )
    res = await TrajectoryReplayer(agent_db=db).score(case, tid)
    assert res.exact
    assert res.predicted_verbs == ["WRITE_FILE", "RUN_TERMINAL"]
