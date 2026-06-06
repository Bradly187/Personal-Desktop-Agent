"""Tests for per-domain SLO-driven routing (gap H).

Covers the SLO framework (config + evaluate + difficulty), AgentDB per-domain
inference stats, the per-domain Gate-4 budget helper, and the trainer's per-domain
SLO adaptation (breach logging + Gate-4 override).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.slo import (
    SLOConfig, DomainSLO, evaluate, estimate_difficulty,
    OK, BREACH_LATENCY, BREACH_SUCCESS, HEADROOM,
)


# ---------------------------------------------------------------------------
# SLO framework
# ---------------------------------------------------------------------------

def test_slo_config_per_domain_and_fallback():
    cfg = SLOConfig()
    assert cfg.latency_budget_ms("command") == 600.0
    assert cfg.latency_budget_ms("plan") > cfg.latency_budget_ms("command")
    # Unknown domain → fallback (not a crash).
    assert cfg.get("nonsense").latency_budget_ms > 0


def test_slo_overrides():
    cfg = SLOConfig(overrides={"command": DomainSLO(200.0, 0.99)})
    assert cfg.latency_budget_ms("command") == 200.0


@pytest.mark.parametrize("p50,sr,expected", [
    (100.0, 0.99, HEADROOM),        # well under 600 budget + meets success
    (500.0, 0.99, OK),              # under budget but not in headroom band
    (700.0, 0.99, BREACH_LATENCY),  # over latency budget
    (100.0, 0.50, BREACH_SUCCESS),  # success breach dominates even when fast
    (700.0, 0.50, BREACH_SUCCESS),  # success breach beats latency breach
])
def test_evaluate(p50, sr, expected):
    slo = DomainSLO(latency_budget_ms=600.0, min_success_rate=0.95)
    assert evaluate(slo, p50, sr) == expected


def test_evaluate_handles_missing_data():
    slo = DomainSLO(600.0, 0.95)
    assert evaluate(slo, None, None) == OK   # no data → don't flag


@pytest.mark.parametrize("text,lo,hi", [
    ("click ok", 0.0, 0.2),
    ("refactor the module and then run tests and then commit", 0.5, 1.0),
    ("", 0.0, 0.0),
])
def test_estimate_difficulty_range(text, lo, hi):
    d = estimate_difficulty(text)
    assert lo <= d <= hi


# ---------------------------------------------------------------------------
# CoordinatorConfig.latency_budget_for
# ---------------------------------------------------------------------------

def test_latency_budget_for_resolution():
    from core.hybrid_coordinator import CoordinatorConfig
    cfg = CoordinatorConfig()
    # command → legacy global field (preserves Gate-4 behaviour)
    assert cfg.latency_budget_for("command") == cfg.latency_budget_ms
    # non-command → per-domain SLO
    assert cfg.latency_budget_for("plan") == cfg.slo.latency_budget_ms("plan")
    # override wins
    cfg.per_domain_latency_budget["plan"] = 1234.0
    assert cfg.latency_budget_for("plan") == 1234.0


# ---------------------------------------------------------------------------
# AgentDB per-domain inference stats
# ---------------------------------------------------------------------------

@pytest.fixture
async def db(tmp_path):
    from storage.db import AgentDB
    d = AgentDB()
    await d.open(tmp_path / "agent.db")
    if not d.available:
        pytest.skip("aiosqlite unavailable")
    yield d
    await d.close()


async def test_inference_stats_by_domain(db):
    # Seed inferences directly.
    async def _ins(domain, lat, err=None):
        import time
        await db._conn.execute(
            "INSERT INTO inferences (command_id, ts, model, domain, latency_ms, error) "
            "VALUES (NULL, ?, 'm', ?, ?, ?)",
            (time.time(), domain, lat, err),
        )
    for lat in (100, 200, 300):
        await _ins("code", lat)
    await _ins("code", 400, err="boom")          # one failure
    await _ins("vision", 5000)
    await db._conn.commit()

    stats = await db.get_inference_stats_by_domain()
    assert stats["code"]["count"] == 4
    assert stats["code"]["p50_latency_ms"] in (200, 300)   # median of 4
    assert stats["code"]["success_rate"] == 0.75            # 3/4 ok
    assert stats["vision"]["count"] == 1


# ---------------------------------------------------------------------------
# Trainer per-domain SLO adaptation
# ---------------------------------------------------------------------------

async def test_trainer_logs_breach_and_sets_override(db, monkeypatch):
    from adaptive.continuous_trainer import ContinuousTrainer
    from core.hybrid_coordinator import CoordinatorConfig

    cfg = CoordinatorConfig()
    trainer = ContinuousTrainer(agent_db=db, config=cfg, gesture_samples_min=3)

    # 'plan' SLO budget is 30000 ms; seed slow inferences that breach it.
    import time
    for _ in range(4):
        await db._conn.execute(
            "INSERT INTO inferences (command_id, ts, model, domain, latency_ms, error) "
            "VALUES (NULL, ?, 'm', 'plan', 60000, NULL)",
            (time.time(),),
        )
    await db._conn.commit()

    await trainer._adapt_per_domain_slo()

    assert trainer.slo_status.get("plan") == BREACH_LATENCY
    # Per-domain Gate-4 override was set for the breaching dev domain.
    assert "plan" in cfg.per_domain_latency_budget
    # And an adaptation_log row was written with the domain tag.
    async with db._conn.execute(
        "SELECT component, domain FROM adaptation_log WHERE domain='plan'"
    ) as cur:
        rows = await cur.fetchall()
    assert rows and rows[0]["component"] == "slo:plan"


async def test_trainer_skips_low_sample_domains(db):
    from adaptive.continuous_trainer import ContinuousTrainer
    from core.hybrid_coordinator import CoordinatorConfig

    trainer = ContinuousTrainer(agent_db=db, config=CoordinatorConfig(), gesture_samples_min=10)
    import time
    await db._conn.execute(
        "INSERT INTO inferences (command_id, ts, model, domain, latency_ms, error) "
        "VALUES (NULL, ?, 'm', 'code', 99999, NULL)", (time.time(),),
    )
    await db._conn.commit()
    await trainer._adapt_per_domain_slo()
    assert "code" not in trainer.slo_status   # only 1 sample < 10 → skipped
