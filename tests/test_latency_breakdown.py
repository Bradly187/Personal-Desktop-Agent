"""Tests for monitoring.latency.latency_breakdown().

Unblends command latency into inference vs execute stages, split by local/cloud
route, joining commands.latency_ms with SUM(inferences.latency_ms).

Run:
    python -m pytest tests/test_latency_breakdown.py -q
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from monitoring import latency


def _make_db(tmp_path, commands, inferences) -> str:
    path = str(tmp_path / "agent.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE commands (id INTEGER PRIMARY KEY, ts REAL, route TEXT, latency_ms REAL)")
    conn.execute("CREATE TABLE inferences (id INTEGER PRIMARY KEY, command_id INTEGER, ts REAL, latency_ms REAL)")
    conn.executemany("INSERT INTO commands (id, ts, route, latency_ms) VALUES (?,?,?,?)", commands)
    conn.executemany("INSERT INTO inferences (command_id, ts, latency_ms) VALUES (?,?,?)", inferences)
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def db(tmp_path) -> str:
    now = time.time()
    commands = [
        # (id, ts, route, total_latency_ms)
        (1, now, "local", 10000.0),
        (2, now, "local", 2000.0),
        (3, now, "cloud", 3000.0),
        (4, now, "cloud", 4000.0),
        (5, now, "local", None),   # no latency → excluded
        (6, now, "local", 500.0),  # bypass-like: no inference row → excluded
    ]
    inferences = [
        # (command_id, ts, infer_ms)
        (1, now, 7000.0),   # local cold: execute = 3000
        (2, now, 1500.0),   # local warm: execute = 500
        (3, now, 1200.0),   # cloud: execute = 1800
        (4, now, 2200.0),   # cloud: execute = 1800
        (5, now, 999.0),    # parent command has null total → excluded
    ]
    return _make_db(tmp_path, commands, inferences)


def test_routes_present(db):
    res = latency.latency_breakdown(db, days=None)
    assert set(res["routes"]) == {"local", "cloud"}


def test_counts_exclude_unjoinable(db):
    r = latency.latency_breakdown(db, days=None)["routes"]
    # cmd 5 (null total) and cmd 6 (no inference) excluded
    assert r["local"]["n"] == 2
    assert r["cloud"]["n"] == 2


def test_stage_split_math(db):
    r = latency.latency_breakdown(db, days=None)["routes"]
    # local inference values [7000, 1500]; execute = total-infer = [3000, 500]
    assert r["local"]["infer"]["p95"] == 7000.0
    assert r["local"]["execute"]["p50"] in (500.0, 3000.0)  # nearest-rank p50
    # cloud total [3000,4000]; infer [1200,2200]; execute [1800,1800]
    assert r["cloud"]["execute"]["p50"] == 1800.0
    assert r["cloud"]["total"]["p95"] == 4000.0


def test_execute_clamped_non_negative(tmp_path):
    now = time.time()
    # inference longer than recorded total → execute clamps to 0, never negative
    db = _make_db(tmp_path, [(1, now, "local", 1000.0)], [(1, now, 1500.0)])
    r = latency.latency_breakdown(db, days=None)["routes"]
    assert r["local"]["execute"]["p50"] == 0.0


def test_multiple_inferences_summed(tmp_path):
    now = time.time()
    # one command, two inference rows (e.g. replan) → inference = sum
    db = _make_db(tmp_path, [(1, now, "cloud", 5000.0)], [(1, now, 1000.0), (1, now, 1500.0)])
    r = latency.latency_breakdown(db, days=None)["routes"]
    assert r["cloud"]["infer"]["p50"] == 2500.0
    assert r["cloud"]["execute"]["p50"] == 2500.0


def test_missing_db_is_safe():
    res = latency.latency_breakdown("/no/such/agent.db", days=30)
    assert res["routes"] == {}
