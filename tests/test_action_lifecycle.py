from __future__ import annotations

import asyncio

import pytest

from core.action_lifecycle import (
    ActionIdentity,
    ActionIdentityMismatch,
    ActionStatus,
    RegistryFullError,
    TrackedWorkRegistry,
)
from core.proc_utils import ProcessTimeoutError, TerminationEvidence


def _identity(action_id: str, *, trace: str = "trace-original") -> ActionIdentity:
    return ActionIdentity(action_id, "WRITE_FILE", trace, 17, 4)


async def test_timed_out_worker_later_succeeds_with_original_identity():
    registry = TrackedWorkRegistry(limit=2, ttl_s=10)
    release = asyncio.Event()

    async def work():
        await release.wait()
        return {"written": "x.py"}

    identity = _identity("success-" + "a" * 32)
    outcome = await registry.execute(
        identity, work, timeout_s=0.01, destructive=True
    )
    assert outcome.status is ActionStatus.OUTCOME_UNKNOWN
    assert outcome.trace_id == "trace-original"

    release.set()
    await asyncio.sleep(0.02)
    observed = await registry.query(identity)
    assert observed is not None
    assert observed.status is ActionStatus.COMPLETED
    assert observed.identity == identity


async def test_timed_out_worker_later_fails_with_original_identity():
    registry = TrackedWorkRegistry(limit=2, ttl_s=10)
    release = asyncio.Event()

    async def work():
        await release.wait()
        raise RuntimeError("late boom")

    identity = _identity("failure-" + "b" * 32)
    first = await registry.execute(identity, work, timeout_s=0.01, destructive=True)
    assert first.status is ActionStatus.OUTCOME_UNKNOWN
    release.set()
    await asyncio.sleep(0.02)
    observed = await registry.query(identity)
    assert observed is not None
    assert observed.status is ActionStatus.FAILED
    assert observed.error == "late boom"
    assert observed.run_id == 17 and observed.step_num == 4


async def test_duplicate_action_id_is_idempotent_and_identity_bound():
    registry = TrackedWorkRegistry(limit=2, ttl_s=10)
    release = asyncio.Event()
    calls = 0

    async def work():
        nonlocal calls
        calls += 1
        await release.wait()
        return "done"

    identity = _identity("duplicate-" + "c" * 32)
    first = asyncio.create_task(
        registry.execute(identity, work, timeout_s=0.01, destructive=True)
    )
    second = asyncio.create_task(
        registry.execute(identity, work, timeout_s=0.01, destructive=True)
    )
    await asyncio.gather(first, second)
    assert calls == 1
    with pytest.raises(ActionIdentityMismatch):
        await registry.query(_identity(identity.action_id, trace="other-trace"))
    release.set()


async def test_registry_saturation_denies_before_work_starts_and_retains_live_expired():
    registry = TrackedWorkRegistry(limit=1, ttl_s=0.01)
    release = asyncio.Event()
    second_started = False

    async def first_work():
        await release.wait()

    async def second_work():
        nonlocal second_started
        second_started = True

    await registry.execute(
        _identity("first-" + "d" * 32), first_work,
        timeout_s=0.001, destructive=True,
    )
    await asyncio.sleep(0.02)
    with pytest.raises(RegistryFullError):
        await registry.execute(
            _identity("second-" + "e" * 32), second_work,
            timeout_s=0.001, destructive=True,
        )
    assert second_started is False
    release.set()


async def test_terminal_registry_entry_expires_after_ttl():
    registry = TrackedWorkRegistry(limit=1, ttl_s=0.01)
    identity = _identity("expiry-" + "f" * 32)
    result = await registry.execute(
        identity, lambda: asyncio.sleep(0, result="ok"),
        timeout_s=1, destructive=True,
    )
    assert result.status is ActionStatus.COMPLETED
    await asyncio.sleep(0.02)
    assert await registry.query(identity) is None


@pytest.mark.parametrize(
    ("verified", "expected"),
    [(True, ActionStatus.FAILED), (False, ActionStatus.OUTCOME_UNKNOWN)],
)
async def test_process_timeout_maps_from_termination_evidence(verified, expected):
    registry = TrackedWorkRegistry(limit=1, ttl_s=10)
    identity = ActionIdentity("process-" + "1" * 32, "RUN_TERMINAL", "t", 1, 1)

    async def work():
        raise ProcessTimeoutError(
            "cmd", 1, termination=TerminationEvidence(verified, "test")
        )

    outcome = await registry.execute(identity, work, timeout_s=1, destructive=True)
    assert outcome.status is expected
    assert "termination" in (outcome.reconciliation or "")
