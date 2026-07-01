"""EH-4 — resilience wiring: breakers on alt backends, governor eviction
backoff, Gate-3 VRAM probe timeout, goal-queue lease TTL, supervisor disable log.

Covers:
- E7  VLLMServerInference / LlamaCppInference now carry a CircuitBreaker and
      fast-fail when it is open (no full-timeout per request on a hung server).
- E8  ResourceGovernor._evict_heavy_models stops the batch on the first failed
      keep_alive POST (no 5 s × N stall when Ollama is down during a flare).
- E9  GateEvaluator.gate3 bounds the NVML probe and fails open on timeout.
- E15 AgentDB.reap_expired_leases requeues a 'running' goal whose claim lease
      outlived the TTL, but never an actively-claimed (fresh) goal.
- E21 Supervisor logs once when a supervised subsystem flips to disabled.

Run:
    python -m pytest tests/test_eh4_resilience.py -q
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.command_executor import Command


# ===========================================================================
# E7 — alternate inference backends carry a circuit breaker
# ===========================================================================

@pytest.mark.parametrize("factory", [
    lambda: __import__("inference.local_inference", fromlist=["VLLMServerInference"])
            .VLLMServerInference(base_url="http://127.0.0.1:9", model="m", timeout=1.0),
    lambda: __import__("inference.local_inference", fromlist=["LlamaCppInference"])
            .LlamaCppInference(host="http://127.0.0.1:9", timeout=1.0),
])
async def test_alt_backend_has_breaker_and_fast_fails_when_open(factory):
    be = factory()
    assert be._breaker is not None
    # Force the breaker open without any network I/O.
    for _ in range(3):
        be._breaker.record_failure()
    assert be._breaker.state == "open"

    t0 = time.monotonic()
    out = await be.infer(Command(text="click save", action="", source="voice"))
    # Fast-fail: returns the circuit-open CLARIFY well under the call timeout.
    assert "circuit open" in out.lower()
    assert time.monotonic() - t0 < 0.5


# ===========================================================================
# E8 — governor eviction short-circuits on the first failure
# ===========================================================================

def _governor():
    from core.resource_governor import ResourceGovernor
    return ResourceGovernor(memory=Mock())


def test_evict_short_circuits_on_first_failure():
    gov = _governor()
    gov._heavy_models = lambda: ["a", "b", "c"]
    calls = []

    def boom(model, keep_alive):
        calls.append(model)
        raise RuntimeError("ollama down")

    gov._post_keepalive = boom
    gov._evict_heavy_models()
    assert calls == ["a"]   # stopped after the first failure — no 5 s × N stall


def test_evict_calls_all_when_healthy():
    gov = _governor()
    gov._heavy_models = lambda: ["a", "b", "c"]
    calls = []
    gov._post_keepalive = lambda model, keep_alive: calls.append(model)
    gov._evict_heavy_models()
    assert calls == ["a", "b", "c"]


# ===========================================================================
# E9 — Gate-3 VRAM probe is timeout-bounded and fails open
# ===========================================================================

async def test_gate3_vram_probe_timeout_fails_open(monkeypatch):
    from core.hybrid_coordinator import CoordinatorConfig, HybridCoordinator
    import core.vram as vram

    coord = HybridCoordinator(config=CoordinatorConfig())
    coord._gates._vram_probe_timeout_s = 0.05

    monkeypatch.setattr(vram, "free_vram_gb", lambda: time.sleep(0.5) or 0.0)

    t0 = time.monotonic()
    result = await coord._gates.gate3()
    assert result is True                       # fail-open
    assert time.monotonic() - t0 < 0.4          # did not wait the full 0.5 s


async def test_gate3_passes_when_vram_ample(monkeypatch):
    from core.hybrid_coordinator import CoordinatorConfig, HybridCoordinator
    import core.vram as vram

    coord = HybridCoordinator(config=CoordinatorConfig())
    monkeypatch.setattr(vram, "free_vram_gb", lambda: 99.0)
    assert await coord._gates.gate3() is True


# ===========================================================================
# E15 — goal-queue claim lease TTL (runtime reaper)
# ===========================================================================

@pytest.fixture
async def db(tmp_path):
    from storage.db import AgentDB
    d = AgentDB()
    await d.open(tmp_path / "agent.db")
    if not d.available:
        pytest.skip("aiosqlite unavailable")
    yield d
    await d.close()


async def _set_claimed_at(db, gid, ts):
    await db._conn.execute(
        "UPDATE goal_queue SET claimed_at=? WHERE id=?", (ts, gid))
    await db._conn.commit()


async def test_reap_requeues_expired_lease(db):
    gid = await db.enqueue_goal("wedged", idempotency_key="k")
    claimed = await db.claim_next_goal()       # → running, claimed_at=now
    assert claimed is not None
    # Age the lease well past the TTL (default 1800 s).
    await _set_claimed_at(db, gid, time.time() - 5000.0)

    n = await db.reap_expired_leases()
    assert n == 1
    # Recovered → claimable again.
    again = await db.claim_next_goal()
    assert again is not None and again["goal"] == "wedged"


async def test_reap_leaves_fresh_claim_untouched(db):
    await db.enqueue_goal("running-now", idempotency_key="k")
    claimed = await db.claim_next_goal()       # fresh claimed_at
    assert claimed is not None

    n = await db.reap_expired_leases()
    assert n == 0                               # actively-executing goal untouched
    assert await db.claim_next_goal() is None   # still 'running', not requeued


async def test_reap_respects_explicit_ttl(db):
    gid = await db.enqueue_goal("g", idempotency_key="k")
    await db.claim_next_goal()
    await _set_claimed_at(db, gid, time.time() - 10.0)   # 10 s old

    assert await db.reap_expired_leases(lease_ttl_s=3600.0) == 0   # within TTL
    assert await db.reap_expired_leases(lease_ttl_s=5.0) == 1      # past TTL


# ===========================================================================
# E21 — supervisor logs once on enabled()→False
# ===========================================================================

async def test_supervisor_logs_once_on_disable(caplog):
    from core.supervisor import Supervisor, SupervisedSpec

    sup = Supervisor()
    enabled = {"v": False}
    sup.supervise(SupervisedSpec(
        name="widget",
        is_alive=lambda: True,
        restart=_noop,
        enabled=lambda: enabled["v"],
    ))

    with caplog.at_level(logging.WARNING):
        await sup._check_once()
        await sup._check_once()   # second pass must NOT re-log
    disable_logs = [r for r in caplog.records if "no longer enabled" in r.message]
    assert len(disable_logs) == 1

    # Re-enable then disable again → re-arms the one-shot.
    enabled["v"] = True
    await sup._check_once()
    enabled["v"] = False
    with caplog.at_level(logging.WARNING):
        await sup._check_once()
    disable_logs = [r for r in caplog.records if "no longer enabled" in r.message]
    assert len(disable_logs) == 2


async def _noop():
    return None
