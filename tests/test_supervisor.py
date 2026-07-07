"""Tests for the Supervisor (orchestration gap #2) and the is_healthy/restart
hooks it drives on AccessibilityScheduler and ResourceGovernor.

Covers:
- A dead-but-enabled subsystem is restarted; an alive one is left alone
- A disabled subsystem (enabled()==False) is never restarted (no shutdown fight)
- Restart budget: > max_restarts within window latches FAILED, stops restarting
- Restart timestamps outside the window are pruned (don't count toward budget)
- A raising is_alive/enabled predicate doesn't crash the pass
- Scheduler: a cancelled worker is detected dead and relaunched, queue preserved
- Governor: a dead poll loop is relaunched without re-triggering flare eviction
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent))

from core.supervisor import Supervisor, SupervisedSpec
from core.scheduler import AccessibilityScheduler, Priority


# ---------------------------------------------------------------------------
# Fake subsystem
# ---------------------------------------------------------------------------

class _Fake:
    def __init__(self, alive=True, enabled=True):
        self._alive = alive
        self._enabled = enabled
        self.restarts = 0

    def is_alive(self):
        return self._alive

    def enabled(self):
        return self._enabled

    async def restart(self):
        self.restarts += 1
        self._alive = True   # a successful restart brings it back


def _spec(fake: _Fake, name="x") -> SupervisedSpec:
    return SupervisedSpec(name=name, is_alive=fake.is_alive,
                          restart=fake.restart, enabled=fake.enabled)


# ---------------------------------------------------------------------------
# Supervisor policy
# ---------------------------------------------------------------------------

async def test_dead_enabled_subsystem_restarted():
    sup = Supervisor()
    f = _Fake(alive=False, enabled=True)
    sup.supervise(_spec(f))
    await sup._check_once()
    assert f.restarts == 1
    assert f.is_alive() is True


async def test_alive_subsystem_not_restarted():
    sup = Supervisor()
    f = _Fake(alive=True, enabled=True)
    sup.supervise(_spec(f))
    await sup._check_once()
    assert f.restarts == 0


async def test_disabled_subsystem_never_restarted():
    """A subsystem mid-teardown (enabled()==False) is left alone."""
    sup = Supervisor()
    f = _Fake(alive=False, enabled=False)
    sup.supervise(_spec(f))
    await sup._check_once()
    assert f.restarts == 0


async def test_restart_budget_latches_failed():
    sup = Supervisor(max_restarts=3, window_s=1000.0)
    f = _Fake(alive=False, enabled=True)
    # Make restart NOT revive it, so every pass sees it dead.
    async def _restart_no_revive():
        f.restarts += 1
    f.restart = _restart_no_revive
    sup.supervise(_spec(f))

    for _ in range(5):
        await sup._check_once()

    assert f.restarts == 3   # capped at the budget
    assert sup._subsystems["x"].failed is True
    # A failed subsystem is skipped on subsequent passes.
    await sup._check_once()
    assert f.restarts == 3


async def test_old_restarts_pruned_from_window(monkeypatch):
    """Restarts older than window_s don't count toward the budget (no false latch)."""
    import core.supervisor as sup_mod
    sup = Supervisor(max_restarts=2, window_s=10.0)
    g = _Fake(alive=False, enabled=True)
    async def _restart_no_revive():
        g.restarts += 1          # never revives → stays dead every pass
    g.restart = _restart_no_revive
    sup.supervise(_spec(g, name="g"))

    clock = {"t": 1000.0}
    monkeypatch.setattr(sup_mod.time, "monotonic", lambda: clock["t"])

    await sup._check_once()      # t=1000 → restart #1
    clock["t"] = 1005.0
    await sup._check_once()      # t=1005 → restart #2 (both within 10s window; budget now full)
    assert g.restarts == 2
    assert sup._subsystems["g"].failed is False   # full but not yet exceeded

    clock["t"] = 1100.0          # 95s later → both prior timestamps fall out of the window
    await sup._check_once()      # window empty → restart again, still no latch
    assert g.restarts == 3
    assert sup._subsystems["g"].failed is False


async def test_raising_predicate_does_not_crash_pass():
    sup = Supervisor()
    def _boom():
        raise RuntimeError("nvml exploded")
    spec = SupervisedSpec(name="bad", is_alive=_boom, restart=_noop, enabled=lambda: True)
    sup.supervise(spec)
    # Must not raise.
    await sup._check_once()
    assert sup._subsystems["bad"].failed is False


async def _noop():
    return None


# ---------------------------------------------------------------------------
# Escalation on FAILED (gap E)
# ---------------------------------------------------------------------------

async def test_on_failed_fires_once_when_latched():
    fired: list[str] = []

    async def _handler(name):
        fired.append(name)

    sup = Supervisor(max_restarts=2, window_s=1000.0, on_failed=_handler)
    f = _Fake(alive=False, enabled=True)
    async def _no_revive():
        f.restarts += 1
    f.restart = _no_revive
    sup.supervise(_spec(f, name="sched"))

    for _ in range(5):          # restart, restart, latch, then skip…
        await sup._check_once()

    assert fired == ["sched"]   # exactly once, at the latch — not per restart


async def test_on_failed_sync_handler_supported():
    fired = []
    sup = Supervisor(max_restarts=1, window_s=1000.0)
    sup.set_on_failed(lambda name: fired.append(name))   # sync handler
    f = _Fake(alive=False, enabled=True)
    async def _no_revive():
        f.restarts += 1
    f.restart = _no_revive
    sup.supervise(_spec(f, name="gov"))
    await sup._check_once()     # restart #1 (budget=1 full)
    await sup._check_once()     # latch → escalate
    assert fired == ["gov"]


async def test_on_failed_handler_exception_does_not_crash_pass():
    async def _boom(name):
        raise RuntimeError("tts down")
    sup = Supervisor(max_restarts=1, window_s=1000.0, on_failed=_boom)
    f = _Fake(alive=False, enabled=True)
    async def _no_revive():
        f.restarts += 1
    f.restart = _no_revive
    sup.supervise(_spec(f, name="x"))
    await sup._check_once()
    await sup._check_once()     # must not raise despite handler exploding
    assert sup._subsystems["x"].failed is True


# ---------------------------------------------------------------------------
# Heartbeat staleness — alive-but-wedged detection (#2)
# ---------------------------------------------------------------------------

def _beat_spec(fake, hb_fn, stale_after_s, name="hb"):
    return SupervisedSpec(name=name, is_alive=fake.is_alive, restart=fake.restart,
                          enabled=fake.enabled, last_heartbeat=hb_fn,
                          stale_after_s=stale_after_s)


async def test_stale_heartbeat_restarts_alive_loop(monkeypatch):
    """A loop whose task is alive but whose heartbeat has gone stale (wedged on a
    hung await) is restarted — is_alive() alone would miss it."""
    import core.supervisor as sup_mod
    clock = {"t": 1000.0}
    monkeypatch.setattr(sup_mod.time, "monotonic", lambda: clock["t"])

    f = _Fake(alive=True, enabled=True)        # task is alive…
    beat = {"t": 1000.0}                        # …but its heartbeat is frozen
    sup = Supervisor()
    sup.supervise(_beat_spec(f, lambda: beat["t"], stale_after_s=60.0))

    clock["t"] = 1030.0                         # 30s < 60s → still fresh
    await sup._check_once()
    assert f.restarts == 0

    clock["t"] = 1100.0                         # 100s since last beat > 60s → stale
    await sup._check_once()
    assert f.restarts == 1


async def test_fresh_heartbeat_not_restarted(monkeypatch):
    import core.supervisor as sup_mod
    clock = {"t": 1000.0}
    monkeypatch.setattr(sup_mod.time, "monotonic", lambda: clock["t"])
    f = _Fake(alive=True, enabled=True)
    sup = Supervisor()
    # Heartbeat tracks the clock (loop is cycling normally).
    sup.supervise(_beat_spec(f, lambda: clock["t"], stale_after_s=60.0))
    for dt in (10, 200, 5000):
        clock["t"] += dt
        await sup._check_once()
    assert f.restarts == 0


async def test_unseeded_heartbeat_not_stale(monkeypatch):
    """A 0.0 heartbeat means 'never beaten yet' (pre-start) — must not false-trip."""
    import core.supervisor as sup_mod
    clock = {"t": 9_000.0}
    monkeypatch.setattr(sup_mod.time, "monotonic", lambda: clock["t"])
    f = _Fake(alive=True, enabled=True)
    sup = Supervisor()
    sup.supervise(_beat_spec(f, lambda: 0.0, stale_after_s=1.0))
    await sup._check_once()
    assert f.restarts == 0


async def test_no_heartbeat_spec_behaves_as_before():
    """A spec without a heartbeat (event-driven loop) uses only is_alive()."""
    sup = Supervisor()
    f = _Fake(alive=True, enabled=True)
    sup.supervise(_spec(f))             # no last_heartbeat / stale_after_s
    await sup._check_once()
    assert f.restarts == 0


# ---------------------------------------------------------------------------
# Scheduler integration
# ---------------------------------------------------------------------------

async def test_scheduler_restart_relaunches_dead_worker():
    sched = AccessibilityScheduler()
    await sched.start()
    try:
        assert sched.is_healthy() is True

        # Simulate an unexpected worker death (not via stop(), so _running stays True).
        sched._worker_task.cancel()
        try:
            await sched._worker_task
        except asyncio.CancelledError:
            pass
        assert sched.is_healthy() is False
        assert sched._running is True   # still *supposed* to run

        await sched.restart()
        assert sched.is_healthy() is True

        # Worker still functions: a submitted coro resolves.
        fut = sched.submit(_ok(), Priority.ACCESSIBILITY)
        assert await asyncio.wait_for(fut, timeout=2.0) == "ok"
    finally:
        await sched.stop()


async def test_scheduler_restart_noop_when_stopped():
    sched = AccessibilityScheduler()
    await sched.start()
    await sched.stop()
    assert sched._running is False
    await sched.restart()          # must not relaunch a deliberately-stopped scheduler
    assert sched.is_healthy() is False


async def _ok():
    return "ok"


# ---------------------------------------------------------------------------
# Governor integration
# ---------------------------------------------------------------------------

class _FakeMemory:
    def __init__(self, score=0.0):
        self._score = score

    def get_pain_day_score(self):
        return self._score


async def test_governor_restart_relaunches_without_reflaring():
    from core.resource_governor import ResourceGovernor
    gov = ResourceGovernor(memory=_FakeMemory(score=0.0))
    # Neutralise the Ollama keep-alive network call so start()/stop() don't hit a
    # live server (a keep_alive POST can load a 30B model — slow + non-hermetic).
    gov._post_keepalive = lambda *a, **k: None
    await gov.start()
    try:
        assert gov.is_healthy() is True
        gov._flare_active = True   # pretend we're mid-flare

        gov._task.cancel()
        try:
            await gov._task
        except asyncio.CancelledError:
            pass
        assert gov.is_healthy() is False

        await gov.restart()
        assert gov.is_healthy() is True
        # Restart preserves flare state — it does NOT re-run the eviction sequence.
        assert gov._flare_active is True
    finally:
        await gov.stop()


async def test_governor_restart_cancels_wedged_alive_loop():
    """The staleness path calls restart() while the task is still ALIVE (wedged).
    restart() must cancel the old task before relaunching — no leaked duplicate."""
    from core.resource_governor import ResourceGovernor
    gov = ResourceGovernor(memory=_FakeMemory(score=0.0))
    gov._post_keepalive = lambda *a, **k: None
    await gov.start()
    try:
        old_task = gov._task
        assert old_task is not None and not old_task.done()

        await gov.restart()             # called while alive (simulated staleness)

        assert gov._task is not old_task          # a fresh task replaced it
        assert old_task.cancelled() or old_task.done()  # the wedged one was reaped
        assert gov.is_healthy() is True
    finally:
        await gov.stop()


async def test_governor_heartbeat_advances():
    """The poll loop records a heartbeat each iteration so the Supervisor can see
    progress (POLL_INTERVAL is short enough to observe within the test)."""
    from core.resource_governor import ResourceGovernor
    gov = ResourceGovernor(memory=_FakeMemory(score=0.0))
    gov._post_keepalive = lambda *a, **k: None
    gov.POLL_INTERVAL_S = 0.01
    await gov.start()
    try:
        first = gov.last_heartbeat()
        assert first > 0
        await asyncio.sleep(0.05)        # let the loop cycle a few times
        assert gov.last_heartbeat() >= first
    finally:
        await gov.stop()
