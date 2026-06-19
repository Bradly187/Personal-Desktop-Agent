"""Tests for monitoring.painday.painday_breakdown().

Reads twin_pain_day_log → current score + driver signals, % time in flare,
peak/avg, and a downsampled sparkline.

Run:
    python -m pytest tests/test_painday_breakdown.py -q
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from monitoring import painday


def _make_db(tmp_path, rows) -> str:
    path = str(tmp_path / "agent.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE twin_pain_day_log (id INTEGER PRIMARY KEY, session_id INTEGER, "
        "ts REAL, pain_day_score REAL, pain_day_active INTEGER, fail_ratio REAL, "
        "clarify_ratio REAL, gesture_conf_delta REAL, cmd_rate_delta REAL)"
    )
    conn.executemany(
        "INSERT INTO twin_pain_day_log (ts, pain_day_score, pain_day_active, fail_ratio, "
        "clarify_ratio, gesture_conf_delta, cmd_rate_delta) VALUES (?,?,?,?,?,?,?)", rows,
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def db(tmp_path) -> str:
    now = time.time()
    rows = [
        # (ts, score, active, fail, clarify, gesture_d, cmd_d)
        (now - 300, 0.20, 0, 0.0, 0.0, 0.0, 0.10),
        (now - 240, 0.50, 0, 0.1, 0.1, -0.05, -0.10),
        (now - 180, 0.70, 1, 0.3, 0.2, -0.15, -0.30),
        (now - 120, 0.80, 1, 0.4, 0.3, -0.20, -0.40),
        (now -  60, 0.35, 0, 0.1, 0.0, -0.05, -0.05),
    ]
    return _make_db(tmp_path, rows)


def test_current_is_latest_row(db):
    res = painday.painday_breakdown(db, days=None)
    assert res["current"]["pain_day_score"] == 0.35
    assert res["current"]["pain_day_active"] == 0
    # driver signals carried through
    assert res["current"]["fail_ratio"] == 0.1


def test_pct_active_peak_avg(db):
    res = painday.painday_breakdown(db, days=None)
    assert res["n_samples"] == 5
    assert res["pct_active"] == pytest.approx(2 / 5)       # 2 of 5 active
    assert res["peak_score"] == 0.80
    assert res["avg_score"] == pytest.approx((0.20 + 0.50 + 0.70 + 0.80 + 0.35) / 5, abs=1e-6)


def test_spark_series(db):
    res = painday.painday_breakdown(db, days=None)
    # few samples → no downsampling, one point each, in chronological order
    assert res["spark"] == [0.20, 0.50, 0.70, 0.80, 0.35]


def test_spark_downsampled(tmp_path):
    now = time.time()
    rows = [(now - (200 - i), 0.1 + i * 0.001, 0, 0, 0, 0, 0) for i in range(200)]
    res = painday.painday_breakdown(_make_db(tmp_path, rows), days=None, points=20)
    assert res["n_samples"] == 200
    assert len(res["spark"]) <= 20          # bucketed down


def test_flare_threshold_exposed(db):
    res = painday.painday_breakdown(db, days=None)
    assert res["flare_threshold"] == painday.FLARE_THRESHOLD


def test_missing_db_is_safe():
    res = painday.painday_breakdown("/no/such/agent.db", days=7)
    assert res["current"] is None
    assert res["n_samples"] == 0
    assert res["spark"] == []
    assert res["pct_active"] is None
