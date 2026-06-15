"""E1 — domain misroute analyzer (logged-first, no behaviour change).

Verifies AgentDB.get_domain_misroutes (per-domain routed vs corrected → rate) and
ContinuousTrainer._adapt_domain_misroutes (populates misroute_status, logs flagged
domains to adaptation_log, respects the min-data floor).
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.command_executor import Command
from storage.db import AgentDB
from adaptive.continuous_trainer import ContinuousTrainer


async def _open_db():
    d = tempfile.mkdtemp()
    db = AgentDB()
    await db.open(os.path.join(d, "agent.db"))
    return db


async def _seed_domain(db, sid, domain, n, n_corrected):
    """Insert n commands routed to `domain`; mark the first n_corrected corrected."""
    for k in range(n):
        cmd = Command(text=f"{domain} cmd {k}", action="", source="voice")
        cid = await db.insert_command(
            session_id=sid, cmd=cmd, action="DO it", route="local",
            gate_that_decided="gate1", latency_ms=1.0, success=True)
        if k < n_corrected:
            await db.mark_command_corrected(cid, "FIXED it")
        await db.insert_inference(
            command_id=cid, model="m", domain=domain, prompt=None, response=None,
            tokens_in=None, tokens_out=None, latency_ms=1.0)


@pytest.mark.asyncio
async def test_get_domain_misroutes_rate():
    db = await _open_db()
    try:
        sid = await db.insert_session(mode="test")
        await _seed_domain(db, sid, "code", n=12, n_corrected=5)
        await _seed_domain(db, sid, "command", n=12, n_corrected=0)
        rows = {r["domain"]: r for r in await db.get_domain_misroutes()}
        assert rows["code"]["routed"] == 12 and rows["code"]["corrected"] == 5
        assert rows["code"]["rate"] == pytest.approx(5 / 12)
        assert rows["command"]["rate"] == 0.0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_analyzer_flags_and_logs_high_rate_domain():
    db = await _open_db()
    try:
        sid = await db.insert_session(mode="test")
        await _seed_domain(db, sid, "code", n=12, n_corrected=5)     # ~0.42 -> flagged
        await _seed_domain(db, sid, "command", n=12, n_corrected=0)  # 0.0  -> not flagged
        trainer = ContinuousTrainer(agent_db=db)
        await trainer._adapt_domain_misroutes()

        assert trainer.misroute_status["code"] == pytest.approx(5 / 12)
        assert trainer.misroute_status["command"] == 0.0
        # flagged domain logged to adaptation_log; clean domain not logged
        logged = await db.get_recent_adaptation_log("misroute:code")
        assert logged, "expected a misroute:code adaptation_log row"
        assert not await db.get_recent_adaptation_log("misroute:command")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_analyzer_respects_min_data_floor():
    db = await _open_db()
    try:
        sid = await db.insert_session(mode="test")
        # only 4 routed (< _MISROUTE_MIN_ROUTED=10) — too little data to judge
        await _seed_domain(db, sid, "math", n=4, n_corrected=3)
        trainer = ContinuousTrainer(agent_db=db)
        await trainer._adapt_domain_misroutes()
        assert "math" not in trainer.misroute_status
        assert not await db.get_recent_adaptation_log("misroute:math")
    finally:
        await db.close()
