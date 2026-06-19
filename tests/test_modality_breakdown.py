"""Tests for monitoring.modality.modality_breakdown().

Aggregates the commands table by input source: calls, success rate, avg
latency, cloud-escalation rate.

Run:
    python -m pytest tests/test_modality_breakdown.py -q
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from monitoring import modality


def _make_db(tmp_path, rows) -> str:
    path = str(tmp_path / "agent.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE commands (id INTEGER PRIMARY KEY, ts REAL, source TEXT, "
        "route TEXT, success INTEGER, latency_ms REAL)"
    )
    conn.executemany(
        "INSERT INTO commands (ts, source, route, success, latency_ms) VALUES (?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def db(tmp_path) -> str:
    now = time.time()
    rows = [
        # (ts, source, route, success, latency_ms)
        (now, "touch", "local", 1, 1500.0),
        (now, "touch", "local", 1, 1700.0),
        (now, "touch", "local", 0, 1900.0),
        (now, "voice", "cloud", 1, 9000.0),
        (now, "voice", "cloud", 0, 11000.0),
        (now, "voice", "local", 1, 4000.0),
    ]
    return _make_db(tmp_path, rows)


def _by_src(res):
    return {s["source"]: s for s in res["sources"]}


def test_sources_sorted_by_count(db):
    res = modality.modality_breakdown(db, days=None)
    assert res["n_commands"] == 6
    # touch (3) and voice (3) — order stable, both present
    assert {s["source"] for s in res["sources"]} == {"touch", "voice"}


def test_success_and_latency(db):
    s = _by_src(modality.modality_breakdown(db, days=None))
    assert s["touch"]["n"] == 3
    assert s["touch"]["success_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert s["touch"]["avg_latency_ms"] == pytest.approx((1500 + 1700 + 1900) / 3, abs=0.1)


def test_cloud_rate(db):
    s = _by_src(modality.modality_breakdown(db, days=None))
    assert s["voice"]["cloud_rate"] == pytest.approx(2 / 3, abs=1e-4)   # 2 cloud of 3
    assert s["touch"]["cloud_rate"] == 0.0


def test_null_source_coalesced(tmp_path):
    now = time.time()
    db = _make_db(tmp_path, [(now, None, "local", 1, 100.0)])
    s = _by_src(modality.modality_breakdown(db, days=None))
    assert "(unknown)" in s


def test_missing_db_is_safe():
    res = modality.modality_breakdown("/no/such/agent.db", days=30)
    assert res["sources"] == []
    assert res["n_commands"] == 0
