"""Tests for monitoring.routing.routing_breakdown().

Aggregates commands.gate_that_decided / route / success and inferences.error to
power the dashboard Routing + Errors cards.

Run:
    python -m pytest tests/test_routing_breakdown.py -q
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from monitoring import routing


def _make_db(tmp_path, commands, inferences) -> str:
    path = str(tmp_path / "agent.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE commands (id INTEGER PRIMARY KEY, ts REAL, "
        "gate_that_decided TEXT, route TEXT, success INTEGER)"
    )
    conn.execute(
        "CREATE TABLE inferences (id INTEGER PRIMARY KEY, ts REAL, model TEXT, "
        "backend TEXT, error TEXT)"
    )
    conn.executemany(
        "INSERT INTO commands (ts, gate_that_decided, route, success) VALUES (?,?,?,?)",
        commands,
    )
    conn.executemany(
        "INSERT INTO inferences (ts, model, backend, error) VALUES (?,?,?,?)",
        inferences,
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def db(tmp_path) -> str:
    now = time.time()
    commands = [
        # (ts, gate, route, success)
        (now, "bypass",        "local", 1),
        (now, "bypass",        "local", 1),
        (now, "gate4_latency", "cloud", 1),
        (now, "gate4_latency", "cloud", 0),
        (now, "all_pass",      "local", 1),
        (now, "gate2_complexity", "cloud", 1),
    ]
    inferences = [
        # (ts, model, backend, error)
        (now, "llama3.1:8b",      "ollama",    "CLARIFY inference error: Cannot connect to host localhost:11434"),
        (now, "llama3.1:8b",      "ollama",    "CLARIFY inference error: Cannot connect to host localhost:11434"),
        (now, "claude-haiku-4-5", "anthropic", "CLARIFY cloud error: auth"),
        (now, "llama3.1:8b",      "ollama",    None),   # success, ignored
        (now, "llama3.1:8b",      "ollama",    ""),     # empty, ignored
    ]
    return _make_db(tmp_path, commands, inferences)


def _gates(result):
    return {g["gate"]: g for g in result["gates"]}


def test_gate_counts_and_sorting(db):
    res = routing.routing_breakdown(db, days=None)
    g = _gates(res)
    assert g["bypass"]["count"] == 2
    assert g["gate4_latency"]["count"] == 2
    assert g["all_pass"]["count"] == 1
    assert g["gate2_complexity"]["count"] == 1
    # most frequent first; bypass & gate4_latency (both 2) lead
    assert res["gates"][0]["count"] == 2


def test_gate_route_and_desc(db):
    g = _gates(routing.routing_breakdown(db, days=None))
    assert g["gate4_latency"]["route"] == "cloud"
    assert g["all_pass"]["route"] == "local"
    assert "cloud" in g["gate4_latency"]["desc"]
    # internal route-tally field is not leaked
    assert "_route_n" not in g["bypass"]


def test_route_split_and_success(db):
    res = routing.routing_breakdown(db, days=None)
    assert res["routes"] == {"local": 3, "cloud": 3}
    assert res["n_commands"] == 6
    assert res["success"]["ok"] == 5
    assert res["success"]["fail"] == 1
    assert res["success"]["rate"] == pytest.approx(5 / 6, abs=1e-4)


def test_errors_grouped_and_classified(db):
    res = routing.routing_breakdown(db, days=None)
    errs = {(e["model"], e["error"]): e for e in res["errors"]}
    llama = errs[("llama3.1:8b", "inference error: Cannot connect to host localhost:11434")]
    assert llama["count"] == 2           # the two identical errors grouped
    assert llama["local"] is True        # ollama backend → local
    haiku = errs[("claude-haiku-4-5", "cloud error: auth")]
    assert haiku["local"] is False       # anthropic backend → cloud
    # null/empty errors excluded
    assert all(e["error"] for e in res["errors"])


def test_clarify_prefix_stripped(db):
    res = routing.routing_breakdown(db, days=None)
    assert all(not e["error"].upper().startswith("CLARIFY ") for e in res["errors"])


def test_missing_db_is_safe():
    res = routing.routing_breakdown("/no/such/agent.db", days=30)
    assert res["gates"] == []
    assert res["errors"] == []
    assert res["n_commands"] == 0
    assert res["success"]["rate"] is None
