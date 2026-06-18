"""Cross-layer per-command tracing (opt-in via DA_TRACE).

Reconstructs a single command's journey across the pipeline as one linked trace:

    enqueue  (FusionEngine._emit)
      → dispatch       (AccessibilityScheduler._worker — queue wait)
      → route_decision (HybridCoordinator.route — gate/route/domain/outcome)
      → inference      (local/cloud, or ModelRouter specialist)
      → execute        (CommandExecutor verb + verify)

Two-part id propagation (mirrors how the pipeline actually hands work off):
  - A `trace_id` rides on the `Command` dataclass, so it survives the
    scheduler's `create_task` boundary (a ContextVar set upstream would NOT,
    because the worker re-dispatches in its own context).
  - Within the synchronous-await subtree (coordinator → router → executor) a
    ContextVar carries the id, so deep layers attach spans without new params.

Zero cost when disabled: `DA_TRACE` unset → every method is an early-return
no-op and nothing is allocated. Enable with `DA_TRACE=1`.
"""

from __future__ import annotations

import contextlib
import os
import time
import uuid
from collections import OrderedDict
from contextvars import ContextVar, Token
from threading import Lock
from typing import Optional

# Current trace id for the running async subtree (set at HybridCoordinator.route
# entry; propagates to every awaited descendant in the same task).
_current_trace: ContextVar[Optional[str]] = ContextVar("da_current_trace", default=None)


def _enabled_from_env() -> bool:
    return os.environ.get("DA_TRACE", "0").strip().lower() not in ("", "0", "false", "no", "off")


class TraceRecorder:
    """In-memory ring buffer of recent per-command traces. Thread-safe; non-fatal."""

    def __init__(self, enabled: Optional[bool] = None, maxlen: int = 200) -> None:
        self._enabled = _enabled_from_env() if enabled is None else enabled
        self._maxlen = maxlen
        self._traces: "OrderedDict[str, dict]" = OrderedDict()
        self._lock = Lock()

    # ── State ────────────────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, on: bool) -> None:
        """Toggle at runtime (used by tests; production reads DA_TRACE once)."""
        self._enabled = on

    # ── ContextVar helpers ─────────────────────────────────────────────────────

    @staticmethod
    def set_current(trace_id: Optional[str]) -> Token:
        return _current_trace.set(trace_id)

    @staticmethod
    def get_current() -> Optional[str]:
        return _current_trace.get()

    @staticmethod
    def reset_current(token: Token) -> None:
        try:
            _current_trace.reset(token)
        except Exception:
            pass

    # ── Recording ──────────────────────────────────────────────────────────────

    def new_trace(self, trace_id: Optional[str] = None, **attrs) -> str:
        """Create (or reuse) a trace and return its id. Empty string if disabled."""
        if not self._enabled:
            return trace_id or ""
        tid = trace_id or uuid.uuid4().hex[:8]
        with self._lock:
            if tid not in self._traces:
                self._traces[tid] = {"id": tid, "t0": time.time(),
                                     "attrs": dict(attrs), "spans": []}
                self._evict_locked()
            else:
                self._traces.move_to_end(tid)
        return tid

    def record_span(
        self,
        stage: str,
        trace_id: Optional[str] = None,
        dur_ms: Optional[float] = None,
        **attrs,
    ) -> None:
        """Append a span to the trace (current ContextVar trace if id omitted)."""
        if not self._enabled:
            return
        tid = trace_id or _current_trace.get()
        if not tid:
            return
        span: dict = {"stage": stage, "ts": time.time()}
        if dur_ms is not None:
            span["dur_ms"] = round(dur_ms, 1)
        if attrs:
            span["attrs"] = {k: v for k, v in attrs.items() if v is not None}
        with self._lock:
            tr = self._traces.get(tid)
            if tr is None:
                tr = {"id": tid, "t0": time.time(), "attrs": {}, "spans": []}
                self._traces[tid] = tr
                self._evict_locked()
            tr["spans"].append(span)
            self._traces.move_to_end(tid)

    @contextlib.contextmanager
    def timed(self, stage: str, trace_id: Optional[str] = None, **attrs):
        """Context manager that records a span with the elapsed wall time.

        Safe to wrap an `await` block — __exit__ runs after the awaited body.
        No-op (no timing, no allocation) when disabled.
        """
        if not self._enabled:
            yield
            return
        t0 = time.monotonic()
        try:
            yield
        finally:
            self.record_span(stage, trace_id=trace_id,
                             dur_ms=(time.monotonic() - t0) * 1000, **attrs)

    def _evict_locked(self) -> None:
        while len(self._traces) > self._maxlen:
            self._traces.popitem(last=False)

    # ── Read ───────────────────────────────────────────────────────────────────

    def get_trace(self, trace_id: str) -> Optional[dict]:
        with self._lock:
            tr = self._traces.get(trace_id)
            return dict(tr) if tr else None

    def get_recent(self, n: int = 20) -> list[dict]:
        with self._lock:
            return [dict(t) for t in list(self._traces.values())[-n:]]

    # ── Persistence (GAP-4 — durable traces for eval replay) ───────────────────

    async def persist_trace(self, trace_id: str, agent_db, session_id: int = -1) -> int:
        """Flush a completed trace's spans to durable storage. Returns spans written.

        Decoupled from the 60 Hz hot path — call via `async_utils.fire_and_log`
        AFTER the command finishes. Best-effort: returns 0 (never raises) when
        tracing is disabled, the trace is unknown/empty, or the DB is unavailable.
        """
        if not self._enabled:
            return 0
        tr = self.get_trace(trace_id)
        spans = (tr or {}).get("spans") or []
        if not spans:
            return 0
        if agent_db is None or not getattr(agent_db, "available", False):
            return 0
        try:
            return await agent_db.insert_trace_spans(trace_id, session_id, spans)
        except Exception:
            return 0


# ── Singleton ──────────────────────────────────────────────────────────────────

_recorder: Optional[TraceRecorder] = None
_singleton_lock = Lock()


def get_tracer() -> TraceRecorder:
    global _recorder
    if _recorder is None:
        with _singleton_lock:
            if _recorder is None:
                _recorder = TraceRecorder()
    return _recorder
