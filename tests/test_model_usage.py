"""Tests for cost_ledger.model_usage() — per-model usage across ALL models.

Unlike cost_rollup (cloud-only), model_usage reports every model that ran —
local (llama/qwen, $0) and cloud (claude, priced) — with calls, token totals,
avg/p95 latency, error count, and cost. Feeds the dashboard "Model usage" card.

Run:
    python -m pytest tests/test_model_usage.py -q
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from monitoring import cost_ledger


def _make_db(tmp_path, rows) -> str:
    """Create a minimal `inferences` table with the columns model_usage reads."""
    path = str(tmp_path / "agent.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE inferences (id INTEGER PRIMARY KEY, ts REAL, model TEXT, "
        "backend TEXT, tokens_in INTEGER, tokens_out INTEGER, latency_ms REAL, error TEXT)"
    )
    conn.executemany(
        "INSERT INTO inferences (ts, model, backend, tokens_in, tokens_out, latency_ms, error) "
        "VALUES (?,?,?,?,?,?,?)", rows,
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def db(tmp_path) -> str:
    now = time.time()
    rows = [
        # (ts, model, backend, tokens_in, tokens_out, latency_ms, error)
        (now, "llama3.1:8b",      "ollama",    400, 4,  7467.0, None),
        (now, "llama3.1:8b",      "ollama",    410, 5,   190.0, None),
        (now, "llama3.1:8b",      "ollama",    None, None, 200.0, None),   # null tokens
        (now, "claude-haiku-4-5", "anthropic", 510, 6,  2220.0, None),
        (now, "claude-haiku-4-5", "anthropic", 520, 7,  1180.0, None),
        (now, "qwen3-coder:30b",  "ollama",    900, 50,  None,  "boom"),   # error, null latency
    ]
    return _make_db(tmp_path, rows)


def _by_model(result: dict) -> dict:
    return {m["model"]: m for m in result["models"]}


def test_includes_local_and_cloud_models(db):
    result = cost_ledger.model_usage(db, days=None)
    models = _by_model(result)
    assert set(models) == {"llama3.1:8b", "claude-haiku-4-5", "qwen3-coder:30b"}


def test_local_flag_and_zero_cost(db):
    m = _by_model(cost_ledger.model_usage(db, days=None))
    assert m["llama3.1:8b"]["local"] is True
    assert m["llama3.1:8b"]["cost"] == 0.0
    assert m["qwen3-coder:30b"]["local"] is True
    assert m["claude-haiku-4-5"]["local"] is False
    assert m["claude-haiku-4-5"]["cost"] > 0.0


def test_call_and_token_aggregation(db):
    m = _by_model(cost_ledger.model_usage(db, days=None))
    llama = m["llama3.1:8b"]
    assert llama["calls"] == 3
    assert llama["tokens_in"] == 810   # 400 + 410 + 0 (null coalesced)
    assert llama["tokens_out"] == 9


def test_cloud_cost_matches_price_table(db):
    m = _by_model(cost_ledger.model_usage(db, days=None))
    haiku = m["claude-haiku-4-5"]
    # haiku price (1.0 in / 5.0 out per MTok): (1030 in, 13 out)
    expected = 1030 / 1e6 * 1.0 + 13 / 1e6 * 5.0
    assert haiku["cost"] == pytest.approx(round(expected, 6))


def test_latency_avg_and_p95_skip_nulls(db):
    m = _by_model(cost_ledger.model_usage(db, days=None))
    llama = m["llama3.1:8b"]
    # avg of [7467, 190, 200]
    assert llama["avg_latency_ms"] == pytest.approx(round((7467 + 190 + 200) / 3, 1))
    assert llama["p95_latency_ms"] is not None
    # qwen has only a null latency → both None, not a crash
    assert m["qwen3-coder:30b"]["avg_latency_ms"] is None
    assert m["qwen3-coder:30b"]["p95_latency_ms"] is None


def test_error_count(db):
    m = _by_model(cost_ledger.model_usage(db, days=None))
    assert m["qwen3-coder:30b"]["errors"] == 1
    assert m["llama3.1:8b"]["errors"] == 0


def test_totals_and_sorting(db):
    result = cost_ledger.model_usage(db, days=None)
    assert result["n_inferences"] == 6
    assert result["totals"]["calls"] == 6
    # most-used first → llama (3 calls) leads
    assert result["models"][0]["model"] == "llama3.1:8b"
    # cloud cost is the only non-zero contribution to totals (totals rounds to
    # 4 places, per-model to 6 — so compare at 4-place tolerance).
    assert result["totals"]["cost"] == pytest.approx(
        _by_model(result)["claude-haiku-4-5"]["cost"], abs=1e-4)


def test_missing_db_is_safe():
    result = cost_ledger.model_usage("/no/such/agent.db", days=30)
    assert result["models"] == []
    assert result["n_inferences"] == 0
    assert result["totals"]["cost"] == 0.0
