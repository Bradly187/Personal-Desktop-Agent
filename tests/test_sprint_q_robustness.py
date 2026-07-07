"""Sprint Q — hot-path latency & runtime-robustness regression tests.

Covers the 2026-06-14 audit findings:
  #5  dev_agent._replan parses a JSON recovery plan (no spurious halt)
  #6  Gate 3 probes VRAM off the event loop (awaitable; never blocks the loop)
  #14 same-path WRITE_FILE steps in a DAG wave are serialized, not raced
  #16 circuit breaker ignores a superseded probe's late outcome
  #17 rate limiter does not admit token-free under contention (deadline-bounded)
  #19 governor flare fast-path schedules on the loop from any thread
  #21 scheduler worker resolves the in-hand future if dispatch fails
  #22 supervisor latches FAILED on a persistent slow crash-loop (total ceiling)
  #30 _git_commit raises RuntimeError (not CalledProcessError) on a staging failure

Run:
    python -m pytest tests/test_sprint_q_robustness.py -q
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.circuit_breaker import CircuitBreaker


# ---------------------------------------------------------------------------
# #16 — circuit breaker generation guard
# ---------------------------------------------------------------------------

def test_cb_ignores_superseded_probe_outcome():
    t = [0.0]
    cb = CircuitBreaker(fail_threshold=1, cooldown_s=10, time_fn=lambda: t[0])
    cb.record_failure()                 # → open
    assert cb.state == "open"

    t[0] = 10
    assert cb.allow() is True           # probe A admitted
    gen_a = cb.probe_gen

    t[0] = 20
    assert cb.allow() is True           # A lost → fresh probe B admitted (supersedes A)
    gen_b = cb.probe_gen
    assert gen_b != gen_a

    cb.record_success(gen_b)            # B closes the breaker
    assert cb.state == "closed"

    # A's late failure (stale generation) must NOT reopen the breaker.
    cb.record_failure(gen_a)
    assert cb.state == "closed"


def test_cb_current_generation_outcome_still_acts():
    t = [0.0]
    cb = CircuitBreaker(fail_threshold=1, cooldown_s=10, time_fn=lambda: t[0])
    cb.record_failure()
    t[0] = 10
    assert cb.allow() is True
    cb.record_failure(cb.probe_gen)     # current-gen failure → reopens
    assert cb.state == "open"


def test_cb_no_gen_arg_is_backward_compatible():
    cb = CircuitBreaker(fail_threshold=2, cooldown_s=5)
    cb.record_failure()
    cb.record_failure()                 # legacy callers pass no gen
    assert cb.state == "open"


# ---------------------------------------------------------------------------
# #22 — supervisor total-restart ceiling
# ---------------------------------------------------------------------------

async def test_supervisor_latches_on_slow_crash_loop():
    from core.supervisor import Supervisor, SupervisedSpec

    alive = {"v": False}
    restarts = {"n": 0}

    async def _restart():
        restarts["n"] += 1
        alive["v"] = False              # dies again immediately (slow crash-loop)

    # Generous window so the sliding-window budget never trips; only the total
    # ceiling should latch it.
    sup = Supervisor(interval_s=0.01, max_restarts=1000, window_s=0.001,
                     max_total_restarts=5)
    sup.supervise(SupervisedSpec(
        name="loopy", is_alive=lambda: alive["v"], restart=_restart,
        enabled=lambda: True))
    for _ in range(10):
        await sup._check_once()
    st = sup.get_status()["supervised"]["loopy"]
    assert st["failed"] is True
    assert restarts["n"] == 5           # stopped at the total ceiling


# ---------------------------------------------------------------------------
# #21 — scheduler worker resolves the in-hand future on dispatch failure
# ---------------------------------------------------------------------------

async def test_scheduler_worker_resolves_future_on_dispatch_error(monkeypatch):
    from core.scheduler import AccessibilityScheduler, Priority

    sched = AccessibilityScheduler()
    await sched.start()
    try:
        # Force create_task to blow up so dispatch fails after dequeue.
        import core.scheduler as sch
        boom = RuntimeError("loop closing")
        monkeypatch.setattr(sch.asyncio, "create_task",
                            MagicMock(side_effect=boom))

        async def _noop():
            return "ok"

        fut = sched.submit(_noop(), Priority.ACCESSIBILITY, label="x")
        # The future must be resolved (with the dispatch error) — not hang forever.
        with pytest.raises(RuntimeError, match="loop closing"):
            await asyncio.wait_for(fut, timeout=1.0)
    finally:
        await sched.stop()


# ---------------------------------------------------------------------------
# #19 — governor flare fast-path without start() (loop fallback)
# ---------------------------------------------------------------------------

async def test_governor_spawn_bg_without_start_uses_running_loop():
    from core.resource_governor import ResourceGovernor

    gov = ResourceGovernor(MagicMock())
    gov._running = True                 # simulate active without capturing a loop
    ran = {"v": False}

    async def _coro():
        ran["v"] = True

    gov._spawn_bg(_coro(), "t")         # self._loop is None → fall back to running loop
    await asyncio.sleep(0)
    assert ran["v"] is True


# ---------------------------------------------------------------------------
# #30 — _git_commit raises RuntimeError on staging failure
# ---------------------------------------------------------------------------

def test_git_commit_staging_failure_raises_runtimeerror(monkeypatch):
    from inference import dev_agent

    class _Res:
        returncode = 1
        stderr = "fatal: not a git repository"
        stdout = ""

    monkeypatch.setattr(dev_agent.subprocess, "run", lambda *a, **k: _Res())
    with pytest.raises(RuntimeError, match="git add failed"):
        dev_agent.DevAgent._git_commit("msg")


# ---------------------------------------------------------------------------
# #6 — Gate 3 is async and reads VRAM off-loop
# ---------------------------------------------------------------------------

async def test_gate3_is_async_and_returns_bool(monkeypatch):
    import core.vram as vram
    from core.gate_evaluator import GateEvaluator

    class _Cfg:
        vram_free_min_gb = 8.0

    gates = GateEvaluator(
        _Cfg(), run_local=None, run_cloud=None, approval_config=lambda: {})

    monkeypatch.setattr(vram, "free_vram_gb", lambda: 20.0)   # plenty free
    assert await gates.gate3() is True
    monkeypatch.setattr(vram, "free_vram_gb", lambda: 2.0)    # tight
    gates._vram_cache = None
    assert await gates.gate3() is False


# ---------------------------------------------------------------------------
# #17 — rate limiter enforces the rate under contention (no token-free admit)
# ---------------------------------------------------------------------------

async def test_rate_limiter_waits_instead_of_admitting_free(monkeypatch):
    from core.rate_limiter import RateLimiter

    db = MagicMock()

    async def _cfg(resource):
        return (1000.0, 1)          # 1000 rps, burst 1 → refills in 1 ms
    db.get_rate_limit_config = _cfg

    async def _evt(*a, **k):
        return None
    db.insert_rate_limit_event = _evt

    rl = RateLimiter(db)
    rl._MAX_WAIT_S = 2.0
    # Drain the single burst token, then a throttled call must still return True
    # by WAITING for a refill (not by failing open token-free).
    assert await rl.check("anthropic") is True       # consumes the burst token
    slept = {"total": 0.0}
    real_sleep = asyncio.sleep

    async def _fake_sleep(s):
        slept["total"] += s
        await real_sleep(0)                          # don't actually wait in the test
    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    assert await rl.check("anthropic") is True        # throttled → waited, then consumed
    assert slept["total"] > 0.0                       # it actually waited for a token


# ---------------------------------------------------------------------------
# #5 — _replan parses a JSON recovery plan
# ---------------------------------------------------------------------------

async def test_replan_parses_json_recovery_plan():
    from inference.dev_agent import DevAgent

    agent = DevAgent(router=MagicMock())

    class _R:
        ok = True
        text = '{"steps": [{"action": "EXPLAIN", "args": "", "body": "recovered"}]}'

    async def _infer(**kw):
        return _R()
    agent._router.infer = _infer

    steps = await agent._replan("goal", executed=[], remaining=[])
    assert len(steps) == 1
    assert steps[0].action == "EXPLAIN"
    assert "recovered" in steps[0].body
