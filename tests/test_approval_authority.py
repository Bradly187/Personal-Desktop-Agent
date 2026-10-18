"""Phase 5: structured, replay-resistant approval authority."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from core.approval_authority import (
    ApprovalAuthority,
    ApprovalResponse,
    DuplicateApprovalResponseError,
    action_digest,
    canonical_json,
    content_digest,
    plan_digest,
)
from core.atomic_file import atomic_write_text


def _digest(**changes) -> str:
    values = {
        "action": "WRITE_FILE",
        "trace_id": "trace-1",
        "goal": "update config",
        "canonical_path": "C:/repo/config.py",
        "canonical_cwd": None,
        "security_args": {"mode": "replace", "args": "config.py"},
        "body_hash": content_digest("new content"),
        "step_identity": {"step_num": 2, "deps": [1]},
        "approval_context": "plan",
    }
    values.update(changes)
    return action_digest(**values)


def _request(authority: ApprovalAuthority, digest: str = "d" * 64):
    return authority.create_request(
        trace_id="trace-1", action_digest=digest, description="Approve?"
    )


def _approve(authority: ApprovalAuthority, request, *, digest=None, request_id=None):
    authority.write_response(ApprovalResponse(
        request_id=request.request_id if request_id is None else request_id,
        action_digest=request.action_digest if digest is None else digest,
        decision="approve",
        responded_at=request.created_at + 1,
    ))


def test_canonical_json_and_digest_are_deterministic():
    assert canonical_json({"b": 2, "a": 1}) == '{"a":1,"b":2}'
    left = _digest(security_args={"z": 2, "a": 1})
    right = _digest(security_args={"a": 1, "z": 2})
    assert left == right


def test_action_digest_is_sensitive_to_every_authority_field():
    baseline = _digest()
    variants = [
        _digest(action="EDIT_FILE"),
        _digest(trace_id="trace-2"),
        _digest(goal="different goal"),
        _digest(canonical_path="C:/repo/other.py"),
        _digest(canonical_cwd="C:/repo"),
        _digest(security_args={"mode": "append", "args": "config.py"}),
        _digest(body_hash=content_digest("changed content")),
        _digest(step_identity={"step_num": 3, "deps": [1]}),
        _digest(approval_context="critic_force"),
    ]
    assert len(set(variants)) == len(variants)
    assert all(item != baseline for item in variants)


def test_plan_digest_binds_order_goal_and_trace():
    a, b = _digest(), _digest(canonical_path="C:/repo/b.py")
    baseline = plan_digest([a, b], goal="g", trace_id="t")
    assert plan_digest([b, a], goal="g", trace_id="t") != baseline
    assert plan_digest([a, b], goal="other", trace_id="t") != baseline
    assert plan_digest([a, b], goal="g", trace_id="other") != baseline
    assert plan_digest([a, b, a], goal="g", trace_id="t") != baseline


def test_request_ids_are_unique_and_request_write_is_atomic(tmp_path, monkeypatch):
    authority = ApprovalAuthority(tmp_path / "approval", clock=lambda: 100.0)
    replacements: list[tuple[Path, Path]] = []
    import core.atomic_file as atomic
    original = atomic.os.replace

    def observe(source, destination):
        replacements.append((Path(source), Path(destination)))
        return original(source, destination)

    monkeypatch.setattr(atomic.os, "replace", observe)
    first = _request(authority)
    second = _request(authority)
    _approve(authority, first)

    assert first.request_id != second.request_id
    assert len(first.request_id) >= 32 and len(second.request_id) >= 32
    assert json.loads(authority.request_path(first.request_id).read_text())[
        "request_id"
    ] == first.request_id
    assert replacements
    assert all(source.parent == destination.parent for source, destination in replacements)
    assert not list(authority.root.rglob("*.tmp"))


def test_exact_response_issues_expected_grant_and_consumes_files(tmp_path):
    authority = ApprovalAuthority(tmp_path, clock=lambda: 102.0)
    approved = ("a" * 64, "b" * 64)
    request = authority.create_request(
        trace_id="trace",
        action_digest=plan_digest(approved, goal="plan goal", trace_id="trace"),
        description="plan",
        now=100.0,
    )
    _approve(authority, request)
    grant = authority.consume_response(
        request,
        approved_digests=approved,
        plan_goal="plan goal",
        now=102.0,
    )

    assert grant is not None
    assert grant.request_id == request.request_id
    assert grant.approved_digests == ("a" * 64, "b" * 64)
    assert not authority.request_path(request.request_id).exists()
    assert not authority.response_path(request.request_id).exists()
    assert authority.consumed_path(request.request_id).exists()
    assert authority.grant_allows(grant, "a" * 64, now=102.0)
    assert not authority.grant_allows(grant, "c" * 64, now=102.0)


def test_plan_grant_rejects_substituted_approved_membership(tmp_path):
    authority = ApprovalAuthority(tmp_path, clock=lambda: 101.0)
    shown = ("a" * 64, "b" * 64)
    request = authority.create_request(
        trace_id="trace",
        action_digest=plan_digest(shown, goal="goal", trace_id="trace"),
        description="plan",
        now=100.0,
    )
    _approve(authority, request)
    assert authority.consume_response(
        request,
        approved_digests=("a" * 64, "c" * 64),
        plan_goal="goal",
    ) is None


def test_missing_response_denies_and_request_cannot_be_replayed(tmp_path):
    authority = ApprovalAuthority(tmp_path, clock=lambda: 101.0)
    request = authority.create_request(
        trace_id="t", action_digest="d" * 64, description="x", now=100.0
    )
    assert authority.consume_response(request, approved_digests=("d" * 64,)) is None
    with pytest.raises(DuplicateApprovalResponseError):
        _approve(authority, request)
    assert authority.consume_response(request, approved_digests=("d" * 64,)) is None


def test_digest_mismatch_denies(tmp_path):
    authority = ApprovalAuthority(tmp_path, clock=lambda: 102.0)
    request = authority.create_request(
        trace_id="t", action_digest="d" * 64, description="x", now=100.0
    )
    _approve(authority, request, digest="e" * 64)
    assert authority.consume_response(request, approved_digests=("d" * 64,)) is None


def test_expired_and_stale_response_deny(tmp_path):
    expired = ApprovalAuthority(tmp_path / "expired", clock=lambda: 200.0)
    request = expired.create_request(
        trace_id="t", action_digest="d" * 64, description="x", now=100.0, ttl_s=5
    )
    _approve(expired, request)
    assert expired.consume_response(
        request, approved_digests=("d" * 64,), now=200.0
    ) is None

    stale = ApprovalAuthority(tmp_path / "stale", clock=lambda: 101.0)
    request = stale.create_request(
        trace_id="t", action_digest="d" * 64, description="x", now=100.0
    )
    stale.write_response(ApprovalResponse(
        request_id=request.request_id,
        action_digest=request.action_digest,
        decision="approve",
        responded_at=99.0,
    ))
    assert stale.consume_response(request, approved_digests=("d" * 64,)) is None


def test_malformed_response_denies(tmp_path):
    authority = ApprovalAuthority(tmp_path, clock=lambda: 101.0)
    request = authority.create_request(
        trace_id="t", action_digest="d" * 64, description="x", now=100.0
    )
    atomic_write_text(authority.response_path(request.request_id), "{not-json")
    assert authority.consume_response(request, approved_digests=("d" * 64,)) is None


def test_duplicate_and_conflicting_responses_deny(tmp_path):
    for second_decision in ("approve", "deny"):
        authority = ApprovalAuthority(
            tmp_path / second_decision, clock=lambda: 101.0
        )
        request = authority.create_request(
            trace_id="t", action_digest="d" * 64,
            description="x", now=100.0,
        )
        _approve(authority, request)
        with pytest.raises(DuplicateApprovalResponseError):
            authority.write_response(ApprovalResponse(
                request_id=request.request_id,
                action_digest=request.action_digest,
                decision=second_decision,
                responded_at=101.0,
            ))
        assert authority.consume_response(
            request, approved_digests=(request.action_digest,)
        ) is None


def test_concurrent_response_writers_make_request_ambiguous(tmp_path):
    authority = ApprovalAuthority(tmp_path, clock=lambda: 101.0)
    request = authority.create_request(
        trace_id="t", action_digest="d" * 64, description="x", now=100.0
    )
    response = ApprovalResponse(
        request_id=request.request_id,
        action_digest=request.action_digest,
        decision="approve",
        responded_at=101.0,
    )

    def publish(_):
        try:
            authority.write_response(response)
            return "published"
        except DuplicateApprovalResponseError:
            return "duplicate"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, range(2)))
    assert sorted(results) == ["duplicate", "published"]
    assert authority.consume_response(
        request, approved_digests=(request.action_digest,)
    ) is None


def test_crossed_responses_deny_both_requests(tmp_path):
    authority = ApprovalAuthority(tmp_path, clock=lambda: 101.0)
    first = authority.create_request(
        trace_id="t1", action_digest="a" * 64, description="first", now=100.0
    )
    second = authority.create_request(
        trace_id="t2", action_digest="b" * 64, description="second", now=100.0
    )
    crossed_first = ApprovalResponse(
        request_id=second.request_id,
        action_digest=second.action_digest,
        decision="approve",
        responded_at=101.0,
    )
    crossed_second = ApprovalResponse(
        request_id=first.request_id,
        action_digest=first.action_digest,
        decision="approve",
        responded_at=101.0,
    )
    atomic_write_text(
        authority.response_path(first.request_id), canonical_json(crossed_first.__dict__)
    )
    atomic_write_text(
        authority.response_path(second.request_id), canonical_json(crossed_second.__dict__)
    )
    assert authority.consume_response(first, approved_digests=(first.action_digest,)) is None
    assert authority.consume_response(second, approved_digests=(second.action_digest,)) is None


def test_concurrent_consumers_issue_only_one_grant(tmp_path):
    authority = ApprovalAuthority(tmp_path, clock=lambda: 101.0)
    request = authority.create_request(
        trace_id="t", action_digest="d" * 64, description="x", now=100.0
    )
    _approve(authority, request)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda _: authority.consume_response(
                request, approved_digests=(request.action_digest,)
            ),
            range(8),
        ))
    assert sum(result is not None for result in results) == 1


def test_legacy_compatibility_window_allows_only_one_active_request(tmp_path):
    authority = ApprovalAuthority(
        tmp_path, clock=lambda: 100.0, legacy_read_enabled=True
    )
    first = _request(authority, "a" * 64)
    second = _request(authority, "b" * 64)
    assert authority.begin_legacy_request(first)
    assert not authority.begin_legacy_request(second)
    authority.end_legacy_request(first)
    assert authority.begin_legacy_request(second)
    authority.end_legacy_request(second)


def test_consume_store_failure_denies_without_raising(tmp_path):
    authority = ApprovalAuthority(tmp_path / "approval", clock=lambda: 101.0)
    request = authority.create_request(
        trace_id="t", action_digest="d" * 64, description="x", now=100.0
    )
    _approve(authority, request)
    authority.consumed_dir.write_text("not a directory", encoding="utf-8")
    assert authority.consume_response(
        request, approved_digests=(request.action_digest,)
    ) is None


def test_legacy_lease_is_cross_instance_and_stale_lease_is_bounded(tmp_path):
    now = [100.0]
    first_authority = ApprovalAuthority(
        tmp_path, clock=lambda: now[0], legacy_read_enabled=True
    )
    second_authority = ApprovalAuthority(
        tmp_path, clock=lambda: now[0], legacy_read_enabled=True
    )
    first = first_authority.create_request(
        trace_id="t", action_digest="a" * 64,
        description="first", now=100.0, ttl_s=5.0,
    )
    second = second_authority.create_request(
        trace_id="t", action_digest="b" * 64,
        description="second", now=100.0, ttl_s=10.0,
    )
    third = ApprovalAuthority(tmp_path, clock=lambda: now[0]).create_request(
        trace_id="t", action_digest="c" * 64,
        description="third", now=100.0, ttl_s=10.0,
    )

    assert first_authority.begin_legacy_request(first)
    assert not second_authority.begin_legacy_request(second)
    now[0] = 106.0
    assert second_authority.begin_legacy_request(second)
    # The expired owner must not unlink its successor's live lease.
    first_authority.end_legacy_request(first)
    third_authority = ApprovalAuthority(
        tmp_path, clock=lambda: now[0], legacy_read_enabled=True
    )
    assert not third_authority.begin_legacy_request(third)
    second_authority.end_legacy_request(second)


def _plan_grant(authority: ApprovalAuthority, digests: tuple[str, ...]):
    request = authority.create_request(
        trace_id="trace",
        action_digest=plan_digest(digests, goal="goal", trace_id="trace"),
        description="plan",
        now=100.0,
        ttl_s=10.0,
    )
    authority.write_response(ApprovalResponse(
        request_id=request.request_id,
        action_digest=request.action_digest,
        decision="approve",
        responded_at=101.0,
    ))
    return authority.consume_response(
        request, approved_digests=digests, plan_goal="goal", now=101.0
    )


def test_grant_claims_preserve_order_and_deny_reverse_use(tmp_path):
    authority = ApprovalAuthority(tmp_path, clock=lambda: 101.0)
    a, b = "a" * 64, "b" * 64
    grant = _plan_grant(authority, (a, b))

    assert not authority.claim_grant(grant, b)
    assert authority.claim_grant(grant, a)
    assert authority.claim_grant(grant, b)
    assert not authority.claim_grant(grant, a)


def test_duplicate_digest_multiplicity_is_consumed_exactly(tmp_path):
    authority = ApprovalAuthority(tmp_path, clock=lambda: 101.0)
    digest = "a" * 64
    grant = _plan_grant(authority, (digest, digest))

    assert grant is not None
    assert grant.approved_digests == (digest, digest)
    assert authority.claim_grant(grant, digest)
    assert authority.claim_grant(grant, digest)
    assert not authority.claim_grant(grant, digest)


def test_concurrent_double_claim_allows_exactly_one(tmp_path):
    authority = ApprovalAuthority(tmp_path, clock=lambda: 101.0)
    digest = "a" * 64
    grant = _plan_grant(authority, (digest,))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda _: authority.claim_grant(grant, digest), range(8)
        ))
    assert sum(results) == 1


def test_expiry_boundary_denies_response_and_grant_claim(tmp_path):
    now_authority = ApprovalAuthority(tmp_path / "now", clock=lambda: 105.0)
    request = now_authority.create_request(
        trace_id="t", action_digest="d" * 64,
        description="x", now=100.0, ttl_s=5.0,
    )
    now_authority.write_response(ApprovalResponse(
        request_id=request.request_id,
        action_digest=request.action_digest,
        decision="approve",
        responded_at=101.0,
    ))
    assert now_authority.consume_response(
        request, approved_digests=(request.action_digest,), now=request.expires_at
    ) is None

    response_authority = ApprovalAuthority(tmp_path / "response", clock=lambda: 104.0)
    request = response_authority.create_request(
        trace_id="t", action_digest="d" * 64,
        description="x", now=100.0, ttl_s=5.0,
    )
    response_authority.write_response(ApprovalResponse(
        request_id=request.request_id,
        action_digest=request.action_digest,
        decision="approve",
        responded_at=request.expires_at,
    ))
    assert response_authority.consume_response(
        request, approved_digests=(request.action_digest,), now=104.0
    ) is None

    claim_authority = ApprovalAuthority(tmp_path / "claim", clock=lambda: 101.0)
    grant = _plan_grant(claim_authority, ("a" * 64,))
    assert grant is not None
    assert not claim_authority.claim_grant(
        grant, "a" * 64, now=grant.expires_at
    )


def test_partial_grant_can_be_revoked_and_cannot_resume(tmp_path):
    authority = ApprovalAuthority(tmp_path, clock=lambda: 101.0)
    a, b = "a" * 64, "b" * 64
    grant = _plan_grant(authority, (a, b))

    assert authority.claim_grant(grant, a)
    authority.revoke_grant(grant)
    authority.revoke_grant(grant)  # idempotent cleanup
    assert not authority.claim_grant(grant, b)


def test_expired_claim_removes_abandoned_cursor(tmp_path):
    authority = ApprovalAuthority(tmp_path, clock=lambda: 101.0)
    digest = "a" * 64
    grant = _plan_grant(authority, (digest,))

    assert not authority.claim_grant(grant, digest, now=grant.expires_at)
    # A backwards test clock cannot resurrect state removed at expiry.
    assert not authority.claim_grant(grant, digest, now=101.0)


def test_structured_pending_markers_preserve_exact_requests_and_overlap(tmp_path):
    authority = ApprovalAuthority(tmp_path, clock=lambda: 101.0)
    first = authority.create_request(
        trace_id="t1", action_digest="a" * 64, description="first", now=100.0
    )
    second = authority.create_request(
        trace_id="t2", action_digest="b" * 64, description="second", now=100.0
    )
    authority.publish_pending_request(first)
    assert authority.active_pending_requests() == (first,)
    authority.publish_pending_request(second)
    assert set(authority.active_pending_requests()) == {first, second}
    authority.close_pending_request(first)
    assert authority.active_pending_requests() == (second,)


def test_strict_mode_rejects_unbound_bool_and_bare_yes_file(tmp_path):
    authority = ApprovalAuthority(tmp_path, clock=lambda: 101.0)
    request = authority.create_request(
        trace_id="t", action_digest="d" * 64, description="x", now=100.0
    )
    (tmp_path / "response").write_text("yes", encoding="utf-8")
    assert authority.begin_legacy_request(request) is False
    assert authority.resolve_legacy_decision(
        request, True, approved_digests=(request.action_digest,)
    ) is None
    assert authority.consume_response(
        request, approved_digests=(request.action_digest,)
    ) is None


def test_legacy_migration_requires_real_bool_and_records_acceptance(tmp_path):
    from monitoring.metrics import get_metrics

    metrics = get_metrics()
    before = metrics.get_snapshot()["counters"]["approval_legacy_acceptances"]
    authority = ApprovalAuthority(
        tmp_path, clock=lambda: 101.0, legacy_read_enabled=True
    )
    request = authority.create_request(
        trace_id="t", action_digest="d" * 64, description="x", now=100.0
    )
    assert authority.begin_legacy_request(request)
    assert authority.resolve_legacy_decision(
        request, "yes", approved_digests=(request.action_digest,)
    ) is None
    assert metrics.get_snapshot()["counters"]["approval_legacy_acceptances"] == before
    grant = authority.resolve_legacy_decision(
        request, True, approved_digests=(request.action_digest,)
    )
    assert grant is not None
    assert metrics.get_snapshot()["counters"]["approval_legacy_acceptances"] == before + 1
    authority.end_legacy_request(request)


def test_response_writer_rejects_boolean_timestamp(tmp_path):
    authority = ApprovalAuthority(tmp_path)
    request = authority.create_request(
        trace_id="t", action_digest="d" * 64, description="x"
    )
    with pytest.raises(ValueError, match="timing"):
        authority.write_response(ApprovalResponse(
            request_id=request.request_id,
            action_digest=request.action_digest,
            decision="approve",
            responded_at=True,
        ))


def test_closed_remote_window_rejects_late_response_but_allows_local_fallback(tmp_path):
    authority = ApprovalAuthority(tmp_path)
    request = authority.create_request(
        trace_id="t", action_digest="d" * 64, description="x"
    )
    authority.open_response_window(request)
    authority.close_response_window(request)
    response = ApprovalResponse(
        request_id=request.request_id,
        action_digest=request.action_digest,
        decision="approve",
        responded_at=request.created_at,
    )
    with pytest.raises(OSError):
        authority.write_response(response, require_open_window=True)
    assert not authority.response_path(request.request_id).exists()
    authority.write_response(response)
    assert authority.consume_response(
        request, approved_digests=(request.action_digest,)
    ) is not None


def test_response_window_close_raises_when_lock_unavailable(tmp_path, monkeypatch):
    authority = ApprovalAuthority(tmp_path)
    request = authority.create_request(
        trace_id="t", action_digest="d" * 64, description="x"
    )
    authority.open_response_window(request)
    monkeypatch.setattr(
        authority, "_acquire_response_window_mutex",
        lambda: (_ for _ in ()).throw(OSError("lock unavailable")),
    )
    with pytest.raises(OSError, match="lock unavailable"):
        authority.close_response_window(request)
    assert authority.response_window_path(request.request_id).exists()


def test_response_window_close_raises_when_marker_cannot_be_removed(tmp_path, monkeypatch):
    authority = ApprovalAuthority(tmp_path)
    request = authority.create_request(
        trace_id="t", action_digest="d" * 64, description="x"
    )
    authority.open_response_window(request)
    target = authority.response_window_path(request.request_id)
    original_unlink = Path.unlink

    def fail_target(path, *args, **kwargs):
        if path == target:
            raise OSError("marker busy")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_target)
    with pytest.raises(OSError, match="marker busy"):
        authority.close_response_window(request)
    assert target.exists()
