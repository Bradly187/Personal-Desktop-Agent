"""Typed, action-bound authority for destructive-operation approvals.

Every responder publishes an exact :class:`ApprovalResponse`; only this module
validates and consumes it into a one-use grant.  Legacy bool rebinding is off by
default and exists solely as an observable, explicitly enabled migration seam.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Sequence

from core.atomic_file import atomic_write_text

Decision = Literal["approve", "deny"]
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")


def _require_request_id(value: str) -> str:
    if not isinstance(value, str) or _REQUEST_ID_RE.fullmatch(value) is None:
        raise ValueError("invalid approval request ID")
    return value


def _require_digest(value: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError("invalid approval digest")
    return value


@dataclass(frozen=True)
class ApprovalRequest:
    request_id: str
    trace_id: str
    action_digest: str
    description: str
    created_at: float
    expires_at: float


@dataclass(frozen=True)
class ApprovalResponse:
    request_id: str
    action_digest: str
    decision: Decision
    responded_at: float


@dataclass(frozen=True)
class ApprovalGrant:
    request_id: str
    trace_id: str
    approved_digests: tuple[str, ...]
    expires_at: float


@dataclass
class _GrantClaimState:
    grant: ApprovalGrant
    next_index: int = 0


def canonical_json(value: Any) -> str:
    """Return deterministic UTF-8 JSON; reject non-finite/unsupported values."""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def sha256_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def content_digest(content: str | bytes) -> str:
    data = content if isinstance(content, bytes) else content.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def action_digest(
    *,
    action: str,
    trace_id: str,
    goal: str,
    canonical_path: str | None = None,
    canonical_cwd: str | None = None,
    security_args: Mapping[str, Any] | None = None,
    body_hash: str | None = None,
    step_identity: Mapping[str, Any] | None = None,
    approval_context: str = "plan",
) -> str:
    """Bind all security-relevant action material, independent of display text."""
    payload = {
        "version": 1,
        "kind": "action",
        "action": action.strip().upper(),
        "trace_id": trace_id,
        "goal": goal,
        "canonical_path": canonical_path,
        "canonical_cwd": canonical_cwd,
        "security_args": dict(security_args or {}),
        "body_hash": body_hash,
        "step_identity": dict(step_identity or {}),
        "approval_context": approval_context,
    }
    return sha256_digest(payload)


def step_action_digest(
    step: Any,
    *,
    trace_id: str,
    goal: str,
    canonical_path: str | None = None,
    canonical_cwd: str | None = None,
    content: str | bytes | None = None,
    approval_context: str = "plan",
) -> str:
    """Build an exact digest from an AgentStep-like object and canonical scope."""
    body = getattr(step, "body", "") or ""
    hashed_content = body if content is None else content
    return action_digest(
        action=str(getattr(step, "action", "")),
        trace_id=trace_id,
        goal=goal,
        canonical_path=canonical_path,
        canonical_cwd=canonical_cwd,
        security_args={"args": getattr(step, "args", "") or ""},
        body_hash=content_digest(hashed_content),
        step_identity={
            "step_num": getattr(step, "step_num", None),
            "deps": list(getattr(step, "deps", []) or []),
        },
        approval_context=approval_context,
    )


def plan_digest(
    destructive_step_digests: Sequence[str], *, goal: str, trace_id: str
) -> str:
    """Bind a whole-plan approval to exact destructive actions in exact order."""
    return sha256_digest({
        "version": 1,
        "kind": "plan",
        "trace_id": trace_id,
        "goal": goal,
        "destructive_step_digests": list(destructive_step_digests),
    })


class DuplicateApprovalResponseError(ValueError):
    """A request received more than one responder publication."""


class ApprovalAuthority:
    """Issues requests and is the sole validator/consumer of approval grants."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        request_ttl_s: float = 30.0,
        clock: Callable[[], float] = time.time,
        legacy_read_enabled: bool = False,
    ) -> None:
        if request_ttl_s <= 0:
            raise ValueError("request_ttl_s must be positive")
        self.root = Path(root) if root is not None else Path.home() / ".claude" / "approval"
        self.requests_dir = self.root / "requests"
        self.responses_dir = self.root / "responses"
        self.consumed_dir = self.root / "consumed"
        self.pending_dir = self.root / "pending_requests"
        self.response_windows_dir = self.root / "response_windows"
        self._request_ttl_s = float(request_ttl_s)
        self._clock = clock
        self._legacy_read_enabled = legacy_read_enabled is True
        self._grant_claim_lock = threading.Lock()
        self._grant_claims: dict[str, _GrantClaimState] = {}

    def request_path(self, request_id: str) -> Path:
        return self.requests_dir / f"{_require_request_id(request_id)}.json"

    def response_path(self, request_id: str) -> Path:
        return self.responses_dir / f"{_require_request_id(request_id)}.json"

    def consumed_path(self, request_id: str) -> Path:
        return self.consumed_dir / f"{_require_request_id(request_id)}.json"

    def pending_path(self, request_id: str) -> Path:
        return self.pending_dir / f"{_require_request_id(request_id)}.json"

    def response_window_path(self, request_id: str) -> Path:
        return self.response_windows_dir / f"{_require_request_id(request_id)}.json"

    @property
    def _response_window_mutex_path(self) -> Path:
        return self.root / ".response_window.mutex"

    def _acquire_response_window_mutex(self):
        """Acquire the cross-process responder-window lock, blocking briefly."""
        self.root.mkdir(parents=True, exist_ok=True)
        stream = self._response_window_mutex_path.open("a+b")
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            else:  # pragma: no cover - Windows is the production platform
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            return stream
        except Exception:
            stream.close()
            raise

    def open_response_window(self, request: ApprovalRequest) -> Path:
        """Allow structured remote responders until an atomic close."""
        persisted = self._parse_request(
            self.request_path(request.request_id).read_text(encoding="utf-8")
        )
        if persisted != request:
            raise ValueError("approval request is not active")
        mutex = self._acquire_response_window_mutex()
        try:
            path = self.response_window_path(request.request_id)
            atomic_write_text(path, canonical_json(asdict(request)))
            return path
        finally:
            self._release_legacy_mutex(mutex)

    def close_response_window(self, request: ApprovalRequest) -> None:
        """Atomically seal remote responders or raise if closure is unproven."""
        mutex = self._acquire_response_window_mutex()
        try:
            path = self.response_window_path(request.request_id)
            path.unlink(missing_ok=True)
            if path.exists():
                raise OSError("approval response window remains open")
        finally:
            self._release_legacy_mutex(mutex)

    def publish_pending_request(self, request: ApprovalRequest) -> Path:
        """Expose one exact request to voice/A2UI responders without authority."""
        persisted = self._parse_request(
            self.request_path(request.request_id).read_text(encoding="utf-8")
        )
        if persisted != request:
            raise ValueError("approval request is not active")
        path = self.pending_path(request.request_id)
        atomic_write_text(path, canonical_json(asdict(request)))
        return path

    def close_pending_request(self, request: ApprovalRequest) -> None:
        """Remove only this request's responder marker."""
        self._unlink_quiet(self.pending_path(request.request_id))

    def active_pending_requests(
        self, *, now: float | None = None
    ) -> tuple[ApprovalRequest, ...]:
        """Return exact, unexpired responder requests; malformed markers vanish."""
        current = self._clock() if now is None else float(now)
        if not math.isfinite(current):
            return ()
        try:
            paths = tuple(self.pending_dir.glob("*.json"))
        except OSError:
            return ()
        active: list[ApprovalRequest] = []
        for path in paths:
            try:
                request = self._parse_request(path.read_text(encoding="utf-8"))
                persisted = self._parse_request(
                    self.request_path(request.request_id).read_text(encoding="utf-8")
                )
                if persisted == request and request.created_at <= current < request.expires_at:
                    active.append(request)
            except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
                continue
        return tuple(sorted(active, key=lambda item: item.request_id))

    def _response_claim_path(self, request_id: str) -> Path:
        return self.responses_dir / f".{_require_request_id(request_id)}.claimed"

    def _ambiguous_path(self, request_id: str) -> Path:
        return self.consumed_dir / f"{_require_request_id(request_id)}.ambiguous.json"

    @property
    def legacy_lease_path(self) -> Path:
        return self.root / "legacy_active.lock"

    @property
    def _legacy_mutex_path(self) -> Path:
        return self.root / ".legacy_active.mutex"

    def _try_acquire_legacy_mutex(self):
        """Return a process-owned OS file lock, or None without blocking."""
        try:
            stream = self._legacy_mutex_path.open("a+b")
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - Windows is the production platform
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return stream
        except (OSError, ImportError):
            try:
                stream.close()
            except (OSError, UnboundLocalError):
                pass
            return None

    @staticmethod
    def _release_legacy_mutex(stream) -> None:
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - Windows is the production platform
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except (OSError, ImportError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    @staticmethod
    def _unlink_quiet(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def create_request(
        self,
        *,
        trace_id: str,
        action_digest: str,
        description: str,
        now: float | None = None,
        ttl_s: float | None = None,
    ) -> ApprovalRequest:
        created_at = self._clock() if now is None else float(now)
        lifetime = self._request_ttl_s if ttl_s is None else float(ttl_s)
        if not math.isfinite(created_at) or not math.isfinite(lifetime) or lifetime <= 0:
            raise ValueError("invalid approval request timing")
        request = ApprovalRequest(
            request_id=secrets.token_urlsafe(32),
            trace_id=str(trace_id),
            action_digest=_require_digest(action_digest),
            description=str(description),
            created_at=created_at,
            expires_at=created_at + lifetime,
        )
        atomic_write_text(
            self.request_path(request.request_id),
            canonical_json(asdict(request)),
        )
        return request

    def write_response(
        self,
        response: ApprovalResponse,
        *,
        require_open_window: bool = False,
    ) -> Path:
        """Atomically publish once; duplicate/conflicting responders mark DENY."""
        if require_open_window:
            mutex = self._acquire_response_window_mutex()
            try:
                window = self._parse_request(
                    self.response_window_path(response.request_id).read_text(
                        encoding="utf-8"
                    )
                )
                if (
                    window.request_id != response.request_id
                    or not secrets.compare_digest(
                        window.action_digest, response.action_digest
                    )
                ):
                    raise ValueError("approval response window mismatch")
                return self.write_response(response)
            finally:
                self._release_legacy_mutex(mutex)
        if response.decision not in ("approve", "deny"):
            raise ValueError("invalid approval decision")
        if isinstance(response.responded_at, bool) or not isinstance(
            response.responded_at, (int, float)
        ) or not math.isfinite(float(response.responded_at)):
            raise ValueError("invalid approval response timing")
        _require_digest(response.action_digest)
        path = self.response_path(response.request_id)
        claim = self._response_claim_path(response.request_id)
        self.responses_dir.mkdir(parents=True, exist_ok=True)
        try:
            with claim.open("x", encoding="utf-8") as stream:
                stream.write(response.request_id)
                stream.flush()
        except FileExistsError as exc:
            # Race the consumer for the active request itself.  If ambiguity
            # wins, consumption cannot claim the request and must deny.  If
            # consumption already won, this is a harmless post-consumption
            # replay and cannot affect the issued one-use grant.
            source = self.request_path(response.request_id)
            ambiguous = self._ambiguous_path(response.request_id)
            try:
                self.consumed_dir.mkdir(parents=True, exist_ok=True)
                source.replace(ambiguous)
            except OSError:
                # Either consumption already atomically claimed the request or
                # storage failed.  Removing any still-active source is a
                # best-effort fail-closed fallback for the latter case.
                self._unlink_quiet(source)
            raise DuplicateApprovalResponseError(
                "duplicate approval response"
            ) from exc
        try:
            # A consumed/stale request cannot accept a late publication.
            if not self.request_path(response.request_id).exists():
                raise DuplicateApprovalResponseError(
                    "approval request is no longer active"
                )
            atomic_write_text(path, canonical_json(asdict(response)))
            return path
        except Exception:
            self._unlink_quiet(claim)
            raise

    def resolve_legacy_decision(
        self,
        request: ApprovalRequest,
        decision: bool,
        *,
        approved_digests: Sequence[str],
        plan_goal: str | None = None,
        responded_at: float | None = None,
    ) -> ApprovalGrant | None:
        """Temporary migration seam; strict mode rejects every unbound bool."""
        if not self._legacy_read_enabled or not isinstance(decision, bool):
            return None
        try:
            lease = json.loads(self.legacy_lease_path.read_text(encoding="utf-8"))
            if lease.get("request_id") != request.request_id:
                return None
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
        response = ApprovalResponse(
            request_id=request.request_id,
            action_digest=request.action_digest,
            decision="approve" if decision else "deny",
            responded_at=self._clock() if responded_at is None else float(responded_at),
        )
        try:
            self.write_response(response)
            from monitoring.metrics import get_metrics
            get_metrics().inc("approval_legacy_acceptances")
        except (OSError, TypeError, ValueError):
            pass
        return self.consume_response(
            request, approved_digests=approved_digests, plan_goal=plan_goal
        )

    def consume_response(
        self,
        request: ApprovalRequest,
        *,
        approved_digests: Sequence[str],
        plan_goal: str | None = None,
        now: float | None = None,
    ) -> ApprovalGrant | None:
        """Atomically consume once; every malformed/missing/mismatch path denies."""
        current = self._clock() if now is None else float(now)
        claimed = self.consumed_path(request.request_id)
        source = self.request_path(request.request_id)
        response_path = self.response_path(request.request_id)
        response_claim = self._response_claim_path(request.request_id)
        marker = self.consumed_dir / f".{request.request_id}.used"

        # O_EXCL is the cross-thread/process one-use arbiter.  Once any consumer
        # attempts validation, no replay can make this request usable again.
        try:
            self.consumed_dir.mkdir(parents=True, exist_ok=True)
            with marker.open("x", encoding="utf-8") as stream:
                stream.write("used")
                stream.flush()
        except (FileExistsError, OSError):
            return None

        try:
            source.replace(claimed)
        except OSError:
            self._unlink_quiet(response_path)
            self._unlink_quiet(response_claim)
            return None

        try:
            persisted = self._parse_request(claimed.read_text(encoding="utf-8"))
            response = self._parse_response(response_path.read_text(encoding="utf-8"))
            if persisted != request:
                return None
            if not math.isfinite(current) or current >= request.expires_at:
                return None
            if response.request_id != request.request_id:
                return None
            if not secrets.compare_digest(response.action_digest, request.action_digest):
                return None
            if not (request.created_at <= response.responded_at < request.expires_at):
                return None
            if response.responded_at > current:
                return None
            if response.decision != "approve":
                return None
            ordered = tuple(_require_digest(str(item)) for item in approved_digests)
            if not ordered:
                return None
            if plan_goal is None:
                # A per-operation request can grant only its own exact digest.
                if ordered != (request.action_digest,):
                    return None
            else:
                expected = plan_digest(
                    ordered, goal=plan_goal, trace_id=request.trace_id
                )
                if not secrets.compare_digest(expected, request.action_digest):
                    return None
            grant = ApprovalGrant(
                request_id=request.request_id,
                trace_id=request.trace_id,
                approved_digests=ordered,
                expires_at=request.expires_at,
            )
            with self._grant_claim_lock:
                self._grant_claims[grant.request_id] = _GrantClaimState(grant=grant)
            return grant
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
            return None
        finally:
            self._unlink_quiet(response_path)
            self._unlink_quiet(response_claim)

    def claim_grant(
        self, grant: ApprovalGrant | None, digest: str, *, now: float | None = None
    ) -> bool:
        """Atomically consume the next exact action in an ordered grant."""
        if grant is None:
            return False
        try:
            required_digest = _require_digest(digest)
        except (TypeError, ValueError):
            return False
        current = self._clock() if now is None else float(now)
        if not math.isfinite(current):
            return False
        with self._grant_claim_lock:
            state = self._grant_claims.get(grant.request_id)
            if state is None or state.grant != grant:
                return False
            if current >= grant.expires_at:
                self._grant_claims.pop(grant.request_id, None)
                return False
            if state.next_index >= len(grant.approved_digests):
                return False
            expected = grant.approved_digests[state.next_index]
            if not secrets.compare_digest(expected, required_digest):
                return False
            state.next_index += 1
            if state.next_index == len(grant.approved_digests):
                self._grant_claims.pop(grant.request_id, None)
            return True

    def revoke_grant(self, grant: ApprovalGrant | None) -> None:
        """Forget unused claim positions when a plan ends or is abandoned."""
        if grant is None:
            return
        with self._grant_claim_lock:
            state = self._grant_claims.get(grant.request_id)
            if state is not None and state.grant == grant:
                self._grant_claims.pop(grant.request_id, None)

    def grant_allows(
        self, grant: ApprovalGrant | None, digest: str, *, now: float | None = None
    ) -> bool:
        """Compatibility name for the consuming, ordered grant claim."""
        return self.claim_grant(grant, digest, now=now)

    def begin_legacy_request(self, request: ApprovalRequest) -> bool:
        """Acquire the migration-only shared legacy lease when explicitly enabled."""
        if not self._legacy_read_enabled:
            return False
        lease = self.legacy_lease_path
        payload = canonical_json({
            "request_id": request.request_id,
            "expires_at": request.expires_at,
        }).encode("utf-8")
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
        mutex = self._try_acquire_legacy_mutex()
        if mutex is None:
            return False
        try:
            try:
                fd = os.open(lease, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                try:
                    os.write(fd, payload)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                return True
            except FileExistsError:
                # Take over only a parseable expired lease (or a malformed
                # lease older than the bounded compatibility lifetime).
                stale = False
                try:
                    data = json.loads(lease.read_text(encoding="utf-8"))
                    stale = self._clock() > float(data["expires_at"])
                except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                    try:
                        stale = (
                            self._clock() - lease.stat().st_mtime
                            > self._request_ttl_s
                        )
                    except OSError:
                        return False
                if not stale:
                    return False
                self._unlink_quiet(lease)
                try:
                    fd = os.open(lease, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                    try:
                        os.write(fd, payload)
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                    return True
                except OSError:
                    return False
            except OSError:
                return False
        finally:
            self._release_legacy_mutex(mutex)

    def end_legacy_request(self, request: ApprovalRequest) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        mutex = self._try_acquire_legacy_mutex()
        if mutex is None:
            return
        lease = self.legacy_lease_path
        try:
            try:
                data = json.loads(lease.read_text(encoding="utf-8"))
                if data.get("request_id") == request.request_id:
                    self._unlink_quiet(lease)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                # Never remove a lease whose ownership cannot be proven.
                return
        finally:
            self._release_legacy_mutex(mutex)

    @staticmethod
    def _parse_request(raw: str) -> ApprovalRequest:
        data = json.loads(raw)
        required = {
            "request_id", "trace_id", "action_digest", "description",
            "created_at", "expires_at",
        }
        if not isinstance(data, dict) or set(data) != required:
            raise ValueError("malformed approval request")
        if not all(isinstance(data[key], str) for key in (
            "request_id", "trace_id", "action_digest", "description"
        )):
            raise ValueError("malformed approval request strings")
        created = data["created_at"]
        expires = data["expires_at"]
        if isinstance(created, bool) or isinstance(expires, bool):
            raise ValueError("malformed approval request timing")
        created_at = float(created)
        expires_at = float(expires)
        if (
            not math.isfinite(created_at)
            or not math.isfinite(expires_at)
            or expires_at <= created_at
        ):
            raise ValueError("malformed approval request timing")
        return ApprovalRequest(
            request_id=_require_request_id(data["request_id"]),
            trace_id=data["trace_id"],
            action_digest=_require_digest(data["action_digest"]),
            description=data["description"],
            created_at=created_at,
            expires_at=expires_at,
        )

    @staticmethod
    def _parse_response(raw: str) -> ApprovalResponse:
        data = json.loads(raw)
        required = {"request_id", "action_digest", "decision", "responded_at"}
        if not isinstance(data, dict) or set(data) != required:
            raise ValueError("malformed approval response")
        if not all(isinstance(data[key], str) for key in (
            "request_id", "action_digest", "decision"
        )):
            raise ValueError("malformed approval response strings")
        if data["decision"] not in ("approve", "deny"):
            raise ValueError("malformed approval response decision")
        responded = data["responded_at"]
        if isinstance(responded, bool):
            raise ValueError("malformed approval response timing")
        responded_at = float(responded)
        if not math.isfinite(responded_at):
            raise ValueError("malformed approval response timing")
        return ApprovalResponse(
            request_id=_require_request_id(data["request_id"]),
            action_digest=_require_digest(data["action_digest"]),
            decision=data["decision"],
            responded_at=responded_at,
        )
