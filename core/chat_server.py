"""core/chat_server.py — PC desktop chat UI server (chat + live DAG preview).

A standalone aiohttp app (its own port, localhost-only by default) that lets the
user converse with the agent in natural language and *watch how it works*: the
right-hand pane renders a live DAG of the 4-gate routing flow (simple commands)
or the DevAgent plan (dev queries), updating per-step as the agent executes.

It is deliberately SEPARATE from `core/ipad_bridge.py` so the iPad WebSocket
protocol stays an iPad concern (AGENTS.md #3). It holds references to the live
pipeline objects and:

  * input  — a chat message becomes a Command(source="chat", trace_id=…) and is
    submitted through the AccessibilityScheduler at Priority.VOICE, so it never
    touches the 60 Hz sensor loop (AGENTS.md #2) and never queues behind the dev
    pool.
  * output — one EventBus subscriber ("%", large queue) fans every event whose
    trace_id matches an in-flight chat request to that request's socket. All
    liveness (gate decisions, plan/DAG steps, streamed tokens, the approval card)
    rides the EventBus keyed by trace_id; this server just maps envelopes to
    client frames. It never changes coordinator.route()'s return contract.
  * approval — destructive steps surface an in-chat Approve/Deny card. The card
    is *displayed* from a dag.approval_requested event; the yes/no answer is
    written back through the EXISTING ~/.claude/approval signal-file protocol —
    no new approval authority, fail-safe-DENY preserved (AGENTS.md #4).

The chat request itself runs as a background task (NOT awaited inside the socket
read loop) so the loop stays free to receive the approval click while a plan is
blocked on the approval gate inside route().
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
import time
import uuid
from pathlib import Path
from typing import Optional

from aiohttp import web, WSMsgType

from core.command_executor import Command
from core.scheduler import Priority

log = logging.getLogger(__name__)

# Topics broadcast to ALL dashboard clients (not trace-targeted like chat frames).
_DASHBOARD_TOPICS = {
    "command.executed", "goal.dequeued", "goal.completed",
    "vram.evicted", "vram.restored", "breaker.opened", "inference.stalled",
    # Alerting + autonomous-action topics surfaced in the Activity feed.
    "metric.threshold_crossed", "slo.breached",
    "voice.drift", "step.failed", "replan.exhausted", "email.arrived",
}

# Topics surfaced in the Alerts panel (durable read from event_log).
_ALERT_TOPICS = ("metric.threshold_crossed", "slo.breached")

_STATIC_DIR = Path(__file__).parent.parent / "web_client_chat"
_APPROVAL_DIR = Path.home() / ".claude" / "approval"

# File-context attachments (specs/chat-context-attachments R2). Uploads land in a
# scratch dir under the active root; only these types/size are accepted.
_UPLOAD_DIRNAME = ".chat_uploads"
_MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB cap (R2.1)

# ---------------------------------------------------------------------------
# Access token — authenticate every request (mirrors the iPad bridge C1 gate)
# ---------------------------------------------------------------------------
# A separate token from the iPad pairing token so either can be rotated
# without re-pairing the other surface. Delivered browser-style: main.py opens
# the UI at /?token=…, the middleware answers with an HttpOnly cookie, and all
# follow-up requests (static assets, /chat WS handshake, fetch) ride the cookie.

_CHAT_TOKEN_DIR = Path.home() / ".claude" / "chat_server"
_CHAT_TOKEN_PATH = _CHAT_TOKEN_DIR / "token"
_TOKEN_COOKIE = "da_chat_token"


def _load_or_create_chat_token() -> str:
    """Return the chat-server access token, generating one on first run.

    Stored at ``~/.claude/chat_server/token`` (0600). Every request except
    ``/health`` must present it (``X-Agent-Token`` header, ``?token=`` query,
    or the session cookie) or it is rejected with 401. Fail-closed: if the
    token cannot be read or created we raise rather than serve unauthenticated.
    """
    try:
        if _CHAT_TOKEN_PATH.exists():
            tok = _CHAT_TOKEN_PATH.read_text(encoding="utf-8").strip()
            if tok:
                return tok
        _CHAT_TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        tok = secrets.token_urlsafe(32)
        _CHAT_TOKEN_PATH.write_text(tok, encoding="utf-8")
        try:
            os.chmod(_CHAT_TOKEN_PATH, 0o600)
        except OSError:
            pass  # best-effort; Windows ACLs differ
        return tok
    except OSError as exc:
        raise RuntimeError(
            f"Cannot read or create the chat server access token at "
            f"{_CHAT_TOKEN_PATH}: {exc}. Refusing to start an unauthenticated "
            f"chat server."
        ) from exc


def _live_session_kpis(db_path: str, session_id: int) -> dict:
    """Read-only aggregation over the current session's commands rows.

    Returns the same KPI shape as session_summaries so dashboard consumers
    can treat both interchangeably. Never raises; returns {} on any error.
    """
    import sqlite3
    import statistics
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
        con.row_factory = sqlite3.Row
        with con:
            rows = con.execute(
                """
                SELECT success, route, gate_that_decided, latency_ms, source
                FROM commands WHERE session_id = ?
                """,
                (session_id,),
            ).fetchall()
        con.close()
    except Exception:
        return {}

    if not rows:
        return {"session_id": session_id, "total_commands": 0}

    total = len(rows)
    successes = sum(1 for r in rows if r["success"])
    cloud = sum(1 for r in rows if (r["route"] or "") == "cloud")
    latencies = [r["latency_ms"] for r in rows if r["latency_ms"] is not None]
    source_counts: dict = {}
    gate_counts: dict = {}
    for r in rows:
        s = r["source"] or "unknown"
        source_counts[s] = source_counts.get(s, 0) + 1
        g = r["gate_that_decided"] or "unknown"
        gate_counts[g] = gate_counts.get(g, 0) + 1

    latencies_sorted = sorted(latencies)
    p50 = latencies_sorted[int(len(latencies_sorted) * 0.50)] if latencies_sorted else None
    p95 = latencies_sorted[int(len(latencies_sorted) * 0.95)] if latencies_sorted else None

    return {
        "session_id": session_id,
        "total_commands": total,
        "success_rate": round(successes / total, 4),
        "cloud_escalation_rate": round(cloud / total, 4),
        "latency_p50_ms": round(p50, 1) if p50 is not None else None,
        "latency_p95_ms": round(p95, 1) if p95 is not None else None,
        "source_counts": source_counts,
        "gate_counts": gate_counts,
    }


def _recent_alerts(db_path: str, limit: int = 50) -> dict:
    """Read-only newest-first pull of alert topics from the durable event_log.

    Returns {"alerts": [...]} so the dashboard Alerts panel never has to poll the
    live feed for history. Never raises; returns {"alerts": []} on any error
    (R2.5 — the page degrades to "no alerts", never errors).
    """
    import sqlite3
    placeholders = ",".join("?" for _ in _ALERT_TOPICS)
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
        con.row_factory = sqlite3.Row
        with con:
            rows = con.execute(
                f"SELECT ts, topic, source, payload FROM event_log "
                f"WHERE topic IN ({placeholders}) ORDER BY id DESC LIMIT ?",
                (*_ALERT_TOPICS, int(limit)),
            ).fetchall()
        con.close()
    except Exception:
        return {"alerts": []}

    alerts = []
    for r in rows:
        try:
            p = json.loads(r["payload"]) if r["payload"] else {}
        except Exception:
            p = {}
        topic = r["topic"]
        if topic == "metric.threshold_crossed":
            # severity "info" is a recovery notice → not an active alert.
            active = p.get("severity") != "info"
            text = p.get("message") or f"{p.get('metric')} = {p.get('value')}"
        else:  # slo.breached — always an active breach
            active = True
            text = (f"SLO breach: {p.get('domain')} "
                    f"{p.get('metric')}={p.get('value')} (budget {p.get('budget')})")
        alerts.append({"ts": r["ts"], "topic": topic, "source": r["source"],
                       "metric": p.get("metric"), "active": active, "text": text})
    return {"alerts": alerts}


def _ro_query(db_path: str, sql: str, params: tuple = ()) -> list[dict]:
    """Read-only list-of-dicts query. Returns [] on any error (missing table,
    bad path) so every operational panel degrades to an empty state (R8.5)."""
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
        con.row_factory = sqlite3.Row
        with con:
            rows = [dict(r) for r in con.execute(sql, params).fetchall()]
        con.close()
        return rows
    except Exception:
        return []


def _recent_goals(db_path: str, limit: int = 50) -> dict:
    """Goal queue (newest first) — pending/in-flight/failed autonomous goals."""
    return {"goals": _ro_query(
        db_path,
        "SELECT id, ts, goal, domain, status, attempts, max_attempts, "
        "last_error, source_trigger FROM goal_queue ORDER BY ts DESC LIMIT ?",
        (int(limit),),
    )}


def _recent_escalations(db_path: str, limit: int = 50) -> dict:
    """Dev-escalation queue (newest first). READ-ONLY display — there is NO
    approve/deny control here; the only approval path stays the voice-approved
    gate (AGENTS.md #4, R8.3)."""
    return {"escalations": _ro_query(
        db_path,
        "SELECT id, ts, goal, reason, failed_action, replans, status "
        "FROM dev_escalations ORDER BY ts DESC LIMIT ?",
        (int(limit),),
    )}


def _recent_corrections(db_path: str, limit: int = 50) -> dict:
    """Recent user corrections — commands the user re-aimed (corrected_to set)."""
    return {"corrections": _ro_query(
        db_path,
        "SELECT ts, source, text, action, corrected_to FROM commands "
        "WHERE corrected_to IS NOT NULL AND corrected_to != '' "
        "ORDER BY ts DESC LIMIT ?",
        (int(limit),),
    )}


class _ChatClient:
    """One connected chat socket plus a writer task, so a slow socket never
    blocks the shared EventBus pump."""

    def __init__(self, ws: web.WebSocketResponse) -> None:
        self.ws_id = uuid.uuid4().hex
        self._ws = ws
        self._q: asyncio.Queue = asyncio.Queue(maxsize=2048)
        self._writer: Optional[asyncio.Task] = None
        self._closed = False

    def start(self) -> None:
        self._writer = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        try:
            while True:
                frame = await self._q.get()
                if frame is None:
                    break
                await self._ws.send_json(frame)
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            log.debug("ChatClient writer ended: %s", exc)

    def push(self, frame: dict) -> None:
        """Enqueue a frame for delivery. Drops (never blocks) if the buffer is
        full — the chat UI is best-effort and reconciles on the final frame."""
        if self._closed:
            return
        try:
            self._q.put_nowait(frame)
        except asyncio.QueueFull:
            log.debug("ChatClient %s buffer full — dropping frame", self.ws_id[:8])

    async def close(self) -> None:
        self._closed = True
        if self._writer is not None:
            self._q.put_nowait(None)
            try:
                await asyncio.wait_for(self._writer, timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._writer.cancel()


class ChatServer:
    """aiohttp server hosting the desktop chat UI and live DAG feed."""

    def __init__(self, *, host: str = "127.0.0.1", port: int = 8770,
                 allow_destructive: bool = True,
                 token: Optional[str] = None) -> None:
        self._host = host
        self._port = port
        self._allow_destructive = allow_destructive

        # Access token: required on every request except /health. Injectable
        # for tests; defaults to the persisted per-install token (fail-closed).
        self._token = token or _load_or_create_chat_token()

        # Injected pipeline references (set_* before run()).
        self._coordinator = None
        self._scheduler = None
        self._event_bus = None
        self._agent_db = None
        self._session_id: Optional[int] = None

        self._clients: dict[str, _ChatClient] = {}      # ws_id -> client
        self._active: dict[str, str] = {}               # trace_id -> ws_id
        self._requests: dict[str, asyncio.Task] = {}    # trace_id -> route task

        self._runner: Optional[web.AppRunner] = None
        self._pump_task: Optional[asyncio.Task] = None
        self._running = False

        # Backend-health probe cache (throttled — R6.2). (result, monotonic_ts)
        self._health_cache: tuple[dict, float] = ({}, 0.0)

        # File-context attachments (specs/chat-context-attachments R2):
        # attachment_id -> absolute stored path.
        self._uploads: dict[str, str] = {}

    # ── wiring ──────────────────────────────────────────────────────────────
    def set_coordinator(self, c) -> None: self._coordinator = c
    def set_scheduler(self, s) -> None: self._scheduler = s
    def set_event_bus(self, b) -> None: self._event_bus = b

    def set_agent_db(self, db, session_id: Optional[int] = None) -> None:
        self._agent_db = db
        self._session_id = session_id

    def url(self, *, with_token: bool = False) -> str:
        base = f"http://{self._host}:{self._port}/"
        return f"{base}?token={self._token}" if with_token else base

    # ── lifecycle ───────────────────────────────────────────────────────────
    async def start(self) -> None:
        """Bind the socket and start the EventBus pump, then return (listening).

        Lets main.py open the desktop shell only once the server is reachable.
        """
        app = web.Application(middlewares=[self._auth_middleware])
        app.router.add_get("/", self._index_handler)
        app.router.add_get("/chat", self._ws_handler)
        app.router.add_get("/health", self._health_handler)
        app.router.add_post("/upload", self._upload_handler)
        # Unified observability dashboard (read-only JSON + a static page). The
        # live "Now"/activity panels reuse the EventBus pump (broadcast frames);
        # the historical panels call the monitoring/* modules off the loop.
        app.router.add_get("/dashboard", self._dashboard_handler)
        app.router.add_get("/api/metrics", self._api_metrics)
        app.router.add_get("/api/recent-traces", self._api_recent_traces)
        app.router.add_get("/api/replay/{tid}", self._api_replay)
        app.router.add_get("/api/trends", self._api_trends)
        app.router.add_get("/api/cost", self._api_cost)
        app.router.add_get("/api/models", self._api_models)
        app.router.add_get("/api/routing", self._api_routing)
        app.router.add_get("/api/session-live", self._api_session_live)
        app.router.add_get("/api/alerts", self._api_alerts)
        app.router.add_get("/api/health-backends", self._api_health_backends)
        app.router.add_get("/api/goals", self._api_goals)
        app.router.add_get("/api/escalations", self._api_escalations)
        app.router.add_get("/api/corrections", self._api_corrections)
        if _STATIC_DIR.exists():
            app.router.add_static("/static/", _STATIC_DIR, show_index=False)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port, reuse_address=True)
        await site.start()
        self._running = True
        if self._event_bus is not None:
            self._pump_task = asyncio.create_task(self._event_pump())
        log.info("ChatServer listening on %s", self.url())
        log.info("  → open with the access token: %s", self.url(with_token=True))

    async def run(self) -> None:
        """Start the server and run until cancelled (standalone / test use)."""
        await self.start()
        try:
            await asyncio.Event().wait()
        finally:
            await self.stop()

    async def stop(self) -> None:
        self._running = False
        if self._pump_task is not None:
            self._pump_task.cancel()
            self._pump_task = None
        for client in list(self._clients.values()):
            await client.close()
        self._clients.clear()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ShutdownManager / Supervisor friendliness (mirrors other subsystems).
    async def shutdown(self) -> None:
        await self.stop()

    def is_healthy(self) -> bool:
        return self._running

    # ── auth ────────────────────────────────────────────────────────────────
    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        """Token gate on every route except /health (liveness probe only).

        Accepts the token via ``X-Agent-Token`` header, ``?token=`` query, or
        the session cookie; compares constant-time; rejects with 401 before the
        handler runs. When the token arrived via header/query, the response
        sets an HttpOnly cookie so the static UI's follow-up requests (assets,
        the /chat WS handshake, fetch calls) authenticate without JS changes.
        """
        if request.path == "/health":
            return await handler(request)
        supplied = (request.headers.get("X-Agent-Token")
                    or request.query.get("token", "")
                    or request.cookies.get(_TOKEN_COOKIE, ""))
        if not supplied or not hmac.compare_digest(
                supplied.encode("utf-8"), self._token.encode("utf-8")):
            log.warning("ChatServer: rejected unauthenticated request to %s "
                        "from %s", request.path, request.remote)
            return web.Response(status=401, text="chat token missing or invalid")
        resp = await handler(request)
        # WebSocket/streaming responses are already prepared inside the handler
        # — headers are gone; only plain responses can still carry the cookie.
        if (request.cookies.get(_TOKEN_COOKIE, "") != self._token
                and not resp.prepared):
            resp.set_cookie(_TOKEN_COOKIE, self._token,
                            httponly=True, samesite="Strict", path="/")
        return resp

    # ── HTTP handlers ───────────────────────────────────────────────────────
    async def _index_handler(self, request: web.Request) -> web.StreamResponse:
        index = _STATIC_DIR / "index.html"
        if index.exists():
            return web.FileResponse(index)
        return web.Response(
            text="chat UI assets missing (web_client_chat/index.html)",
            status=404,
        )

    async def _health_handler(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "clients": len(self._clients)})

    # ── File-context attachments (specs/chat-context-attachments R2) ──────────
    def _active_root(self) -> str:
        """The chat session's active root (upload base + relative-path base).

        Falls back to cwd when the coordinator isn't wired (tests/standalone)."""
        if self._coordinator is not None:
            try:
                return self._coordinator.list_writable_roots().get("active_root") \
                    or os.getcwd()
            except Exception:  # noqa: BLE001
                pass
        return os.getcwd()

    async def _upload_handler(self, request: web.Request) -> web.Response:
        """Accept a single .pdf/.png/.svg file (multipart) up to the size cap and
        store it under ``<active_root>/.chat_uploads/`` (R2.1, R2.2). Returns
        ``{attachment_id, name, kind}``; rejects bad type / oversize / traversal."""
        from inference.attachments import is_allowed, ALLOWED_EXTS
        try:
            reader = await request.multipart()
            field = await reader.next()
            if field is None or field.name != "file":
                return web.json_response({"error": "expected a 'file' part"}, status=400)
            raw_name = os.path.basename(field.filename or "")
            if not raw_name or not is_allowed(raw_name):
                return web.json_response(
                    {"error": f"type not allowed (need {sorted(ALLOWED_EXTS)})"},
                    status=415)
            # Sanitize the filename: basename already strips dirs; reject anything
            # that still smells like traversal (R2.2 — never escape the root).
            if os.path.sep in raw_name or (os.path.altsep and os.path.altsep in raw_name) \
                    or raw_name in (".", ".."):
                return web.json_response({"error": "invalid filename"}, status=400)

            root = self._active_root()
            up_dir = Path(root) / _UPLOAD_DIRNAME
            up_dir.mkdir(parents=True, exist_ok=True)
            attachment_id = uuid.uuid4().hex
            dest = up_dir / f"{attachment_id}_{raw_name}"
            # Final containment check on the resolved destination (defense in depth).
            if not str(os.path.realpath(dest)).startswith(str(os.path.realpath(up_dir)) + os.sep):
                return web.json_response({"error": "path escapes upload dir"}, status=400)

            size = 0
            try:
                with open(dest, "wb") as fh:
                    while True:
                        chunk = await field.read_chunk(64 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > _MAX_UPLOAD_BYTES:
                            fh.close()
                            os.remove(dest)
                            return web.json_response(
                                {"error": f"file exceeds {_MAX_UPLOAD_BYTES} bytes"},
                                status=413)
                        fh.write(chunk)
            except Exception as exc:  # noqa: BLE001
                return web.json_response({"error": f"write failed: {exc}"}, status=500)

            self._uploads[attachment_id] = str(dest)
            ext = os.path.splitext(raw_name)[1].lower()
            kind = "image" if ext in (".png", ".svg") else "text"
            return web.json_response(
                {"attachment_id": attachment_id, "name": raw_name, "kind": kind})
        except Exception as exc:  # noqa: BLE001
            log.warning("ChatServer: upload failed: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)

    # ── Dashboard (read-only observability) ───────────────────────────────────
    def _db_path(self) -> Optional[str]:
        return getattr(self._agent_db, "path", None) if self._agent_db is not None else None

    @staticmethod
    def _qint(request: web.Request, key: str, default: int) -> int:
        try:
            return int(request.query.get(key, default))
        except (TypeError, ValueError):
            return default

    async def _dashboard_handler(self, request: web.Request) -> web.StreamResponse:
        page = _STATIC_DIR / "dashboard.html"
        if page.exists():
            return web.FileResponse(page)
        return web.Response(text="dashboard assets missing (web_client_chat/dashboard.html)",
                            status=404)

    async def _api_metrics(self, request: web.Request) -> web.Response:
        try:
            from monitoring.metrics import get_metrics
            return web.json_response(get_metrics().get_snapshot())
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": str(exc)}, status=500)

    async def _api_recent_traces(self, request: web.Request) -> web.Response:
        path = self._db_path()
        if not path:
            return web.json_response([], status=200)
        from monitoring import replay
        limit = self._qint(request, "limit", 25)
        source = request.query.get("source") or None
        action = request.query.get("action") or None
        succ_q = request.query.get("success")
        success = None
        if succ_q:
            success = succ_q.lower() in ("1", "true", "yes", "ok")
        rows = await asyncio.to_thread(
            replay.recent_traces, path, limit, source, success, action)
        return web.json_response(rows)

    async def _api_replay(self, request: web.Request) -> web.Response:
        path = self._db_path() or "agent.db"
        audit = str(Path(path).parent / "audit.db")
        from monitoring import replay
        tid = request.match_info["tid"]
        result = await asyncio.to_thread(replay.replay_trace, tid, path, audit)
        return web.json_response(result, dumps=lambda o: json.dumps(o, default=str))

    async def _api_trends(self, request: web.Request) -> web.Response:
        path = self._db_path() or "agent.db"
        from monitoring import trends
        limit = self._qint(request, "limit", 30)
        result = await asyncio.to_thread(trends.session_trends, path, limit)
        return web.json_response(result, dumps=lambda o: json.dumps(o, default=str))

    async def _api_cost(self, request: web.Request) -> web.Response:
        path = self._db_path() or "agent.db"
        from monitoring import cost_ledger
        days_q = request.query.get("days", "30")
        days = None if str(days_q).lower() in ("0", "all", "none") else self._qint(request, "days", 30)
        result = await asyncio.to_thread(cost_ledger.cost_rollup, path, days)
        return web.json_response(result, dumps=lambda o: json.dumps(o, default=str))

    async def _api_models(self, request: web.Request) -> web.Response:
        """Per-model usage (local + cloud) for the Model-usage card."""
        path = self._db_path() or "agent.db"
        from monitoring import cost_ledger
        days_q = request.query.get("days", "30")
        days = None if str(days_q).lower() in ("0", "all", "none") else self._qint(request, "days", 30)
        result = await asyncio.to_thread(cost_ledger.model_usage, path, days)
        return web.json_response(result, dumps=lambda o: json.dumps(o, default=str))

    async def _api_routing(self, request: web.Request) -> web.Response:
        """Gate-decision / route split / error breakdown for the Routing card."""
        path = self._db_path() or "agent.db"
        from monitoring import routing
        days_q = request.query.get("days", "30")
        days = None if str(days_q).lower() in ("0", "all", "none") else self._qint(request, "days", 30)
        result = await asyncio.to_thread(routing.routing_breakdown, path, days)
        return web.json_response(result, dumps=lambda o: json.dumps(o, default=str))

    async def _api_session_live(self, request: web.Request) -> web.Response:
        """Live KPI rollup for the current session — same shape as session_summaries
        but computed on-the-fly without writing to DB. Returns {} when no session
        is active or the DB is unavailable."""
        path = self._db_path()
        sid = self._session_id
        if not path or sid is None or sid < 0:
            return web.json_response({})
        result = await asyncio.to_thread(_live_session_kpis, path, sid)
        return web.json_response(result, dumps=lambda o: json.dumps(o, default=str))

    async def _api_alerts(self, request: web.Request) -> web.Response:
        """Recent alert events (metric.threshold_crossed + slo.breached) read from
        the durable event_log. Read off the 60 Hz loop (AGENTS.md #2)."""
        path = self._db_path()
        if not path:
            return web.json_response({"alerts": []})
        limit = self._qint(request, "limit", 50)
        result = await asyncio.to_thread(_recent_alerts, path, limit)
        return web.json_response(result, dumps=lambda o: json.dumps(o, default=str))

    async def _api_goals(self, request: web.Request) -> web.Response:
        path = self._db_path()
        if not path:
            return web.json_response({"goals": []})
        result = await asyncio.to_thread(_recent_goals, path, self._qint(request, "limit", 50))
        return web.json_response(result, dumps=lambda o: json.dumps(o, default=str))

    async def _api_escalations(self, request: web.Request) -> web.Response:
        path = self._db_path()
        if not path:
            return web.json_response({"escalations": []})
        result = await asyncio.to_thread(_recent_escalations, path, self._qint(request, "limit", 50))
        return web.json_response(result, dumps=lambda o: json.dumps(o, default=str))

    async def _api_corrections(self, request: web.Request) -> web.Response:
        path = self._db_path()
        if not path:
            return web.json_response({"corrections": []})
        result = await asyncio.to_thread(_recent_corrections, path, self._qint(request, "limit", 50))
        return web.json_response(result, dumps=lambda o: json.dumps(o, default=str))

    async def _api_health_backends(self, request: web.Request) -> web.Response:
        """Throttled reachability of Ollama / action-proxy + Bedrock cred presence.
        Cached for `health_probe_ttl_s` so rapid polls don't hammer backends; each
        probe times out fast and reports 'unknown' rather than hanging (R6.2)."""
        cached, ts = self._health_cache
        if cached and (time.monotonic() - ts) < 10.0:
            return web.json_response(cached)
        result = await self._probe_backends()
        self._health_cache = (result, time.monotonic())
        return web.json_response(result)

    @staticmethod
    async def _probe_backends() -> dict:
        async def _http_ok(url: str) -> str:
            try:
                import aiohttp
                timeout = aiohttp.ClientTimeout(total=2.0)
                async with aiohttp.ClientSession(timeout=timeout) as s:
                    async with s.get(url) as r:
                        return "up" if r.status < 500 else "down"
            except Exception:
                return "down"

        ollama_host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        ollama, proxy = await asyncio.gather(
            _http_ok(f"{ollama_host.rstrip('/')}/api/version"),
            _http_ok("http://127.0.0.1:8768/health"),
        )
        # Bedrock: report credential PRESENCE only — never the token (R6.4).
        bedrock = "configured" if os.environ.get("AWS_BEARER_TOKEN_BEDROCK") else "absent"
        return {"backends": [
            {"name": "ollama", "status": ollama, "detail": ollama_host},
            {"name": "action-proxy", "status": proxy, "detail": ":8768"},
            {"name": "bedrock", "status": bedrock, "detail": "cloud creds"},
        ]}

    async def _ws_handler(self, request: web.Request) -> web.StreamResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        client = _ChatClient(ws)
        client.start()
        self._clients[client.ws_id] = client
        log.info("ChatServer: client %s connected", client.ws_id[:8])
        client.push({"type": "ready", "config": {
            "allow_destructive": self._allow_destructive,
        }})
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._on_client_message(client, msg.data)
                elif msg.type == WSMsgType.ERROR:
                    log.debug("ChatServer WS error: %s", ws.exception())
        finally:
            self._clients.pop(client.ws_id, None)
            # Drop trace→socket bindings for this client; let in-flight route
            # tasks finish (their dev plans own their own cancel/saga) — frames
            # to a gone client are simply dropped.
            for tid, wsid in list(self._active.items()):
                if wsid == client.ws_id:
                    self._active.pop(tid, None)
            await client.close()
            log.info("ChatServer: client %s disconnected", client.ws_id[:8])
        return ws

    async def _on_client_message(self, client: _ChatClient, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return
        mtype = msg.get("type")
        if mtype == "user_message":
            text = (msg.get("text") or "").strip()
            attachment_ids = msg.get("attachment_ids") or []
            if text or attachment_ids:
                await self._start_request(client, text, attachment_ids)
        elif mtype == "approval_response":
            await self._write_approval(bool(msg.get("approve")))
        elif mtype == "list_dirs":
            client.push({"type": "dirs", **self._list_dirs()})
        elif mtype == "set_active_dir":
            client.push({"type": "active_dir",
                         **self._set_active_dir(msg.get("path") or "",
                                                bool(msg.get("confirm")))})

    # ── active-directory switching (specs/chat-context-attachments R1) ─────────
    def _list_dirs(self) -> dict:
        if self._coordinator is None:
            return {"active_root": os.getcwd(), "writable_roots": [os.getcwd()]}
        try:
            return self._coordinator.list_writable_roots()
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}

    def _set_active_dir(self, path: str, confirm: bool) -> dict:
        if self._coordinator is None:
            return {"status": "invalid", "error": "agent pipeline not wired"}
        try:
            return self._coordinator.set_active_directory(path, confirm=confirm)
        except Exception as exc:  # noqa: BLE001
            return {"status": "invalid", "error": str(exc)}

    # ── request lifecycle ─────────────────────────────────────────────────────
    async def _start_request(self, client: _ChatClient, text: str,
                             attachment_ids: Optional[list] = None) -> None:
        """Route a chat message as a background task so the socket read loop
        stays free (e.g. to receive the approval-card click)."""
        if self._coordinator is None or self._scheduler is None:
            client.push({"type": "error", "error": "agent pipeline not wired"})
            return
        trace_id = uuid.uuid4().hex
        self._active[trace_id] = client.ws_id
        task = asyncio.create_task(
            self._run_request(client, trace_id, text, attachment_ids or []))
        self._requests[trace_id] = task
        task.add_done_callback(lambda _t, tid=trace_id: self._requests.pop(tid, None))

    def _build_attachment_params(self, attachment_ids: list) -> dict:
        """Extract uploaded attachments into Command.params (R2.4). Off the hot
        path (chat only, AGENTS.md #2). Text extractions → attachment_context;
        the first image → attachment_image_b64; names → attachment_names."""
        from inference.attachments import extract_attachment, render_attachment_context
        atts = []
        for aid in attachment_ids:
            path = self._uploads.get(aid)
            if path:
                atts.append(extract_attachment(path))
        if not atts:
            return {}
        image_b64 = next((a.image_b64 for a in atts if a.kind == "image" and a.image_b64), None)
        return {
            "attachment_context": render_attachment_context(atts),
            "attachment_image_b64": image_b64,
            "attachment_names": [a.name for a in atts],
        }

    async def _run_request(self, client: _ChatClient, trace_id: str, text: str,
                           attachment_ids: Optional[list] = None) -> None:
        params = {}
        if attachment_ids:
            params = await asyncio.to_thread(self._build_attachment_params, attachment_ids)
        cmd = Command(text=text, action="CLARIFY", source="chat", trace_id=trace_id,
                      params=params)
        try:
            fut = self._scheduler.submit(
                self._coordinator.route(cmd), Priority.VOICE,
                label="chat", trace_id=trace_id,
            )
            result = await fut
            client.push({"type": "final", "trace_id": trace_id,
                         "result": result if isinstance(result, dict) else {"response": str(result)}})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("ChatServer: request failed: %s", exc)
            client.push({"type": "error", "trace_id": trace_id, "error": str(exc)})
        finally:
            self._active.pop(trace_id, None)

    async def _write_approval(self, approve: bool) -> None:
        """Answer the agent's approval gate via the shared signal-file protocol.

        Writes "yes"/"no" to ~/.claude/approval/response — the same file the
        voice/iPad responders use. DevAgent reads it and runs it through
        core.approval_keywords.classify_confirmation; first writer wins.
        """
        def _write() -> None:
            _APPROVAL_DIR.mkdir(parents=True, exist_ok=True)
            (_APPROVAL_DIR / "response").write_text(
                "yes" if approve else "no", encoding="utf-8"
            )
        try:
            await asyncio.to_thread(_write)
        except Exception as exc:  # noqa: BLE001
            log.debug("ChatServer: approval write failed: %s", exc)

    # ── EventBus pump → client frames ─────────────────────────────────────────
    async def _event_pump(self) -> None:
        """Forward every event whose trace_id matches an in-flight chat request
        to that request's socket. A large queue resists slow-consumer drops."""
        try:
            async for ev in self._event_bus.subscribe("chat_server", "%", maxsize=1024):
                # 1) Dashboard broadcast — ops topics go to EVERY client regardless
                #    of trace_id (background events like vram.evicted have none).
                if ev.get("topic") in _DASHBOARD_TOPICS:
                    dframe = self._to_dashboard_frame(ev)
                    if dframe is not None:
                        for c in list(self._clients.values()):
                            c.push(dframe)
                # 2) Chat frame — fan only to the in-flight request's own socket.
                tid = ev.get("trace_id")
                if not tid:
                    continue
                ws_id = self._active.get(tid)
                if not ws_id:
                    continue
                client = self._clients.get(ws_id)
                if client is None:
                    continue
                frame = self._to_frame(ev)
                if frame is not None:
                    client.push(frame)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            log.warning("ChatServer event pump ended: %s", exc)

    @staticmethod
    def _to_frame(ev: dict) -> Optional[dict]:
        """Map an EventBus envelope to a client frame, or None to ignore."""
        topic = ev.get("topic", "")
        p = ev.get("payload") or {}
        if topic == "gate.decided":
            return {"type": "gate", "gate": p.get("gate"),
                    "latency_ms": p.get("latency_ms"), "domain": p.get("domain")}
        if topic == "command.executed":
            return {"type": "executed", "action": p.get("action"),
                    "route": p.get("route"), "gate": p.get("gate"),
                    "success": p.get("success"), "latency_ms": p.get("latency_ms")}
        if topic == "plan.generated":
            return {"type": "plan", "goal": p.get("goal"), "steps": p.get("steps", [])}
        if topic == "dag.step_started":
            return {"type": "node", "n": p.get("n"), "status": "running",
                    "action": p.get("action")}
        if topic == "dag.step_completed":
            return {"type": "node", "n": p.get("n"),
                    "status": "success" if p.get("success") else "failed",
                    "action": p.get("action"), "latency_ms": p.get("latency_ms"),
                    "result": p.get("result_snippet")}
        if topic == "dag.approval_requested":
            return {"type": "approval", "message": p.get("message"),
                    "destructive": bool(p.get("destructive"))}
        if topic == "chat.token":
            return {"type": "token", "text": p.get("text", "")}
        if topic == "step.failed":
            return {"type": "activity",
                    "text": f"step {p.get('step_num')} failed: {p.get('action')} — {p.get('error', '')}"}
        if topic == "replan.exhausted":
            return {"type": "activity",
                    "text": f"replan exhausted after {p.get('failed_action')} ({p.get('replans')} replans)"}
        return None

    @staticmethod
    def _to_dashboard_frame(ev: dict) -> Optional[dict]:
        """Map an ops EventBus envelope to a broadcast dashboard frame.

        These power the dashboard's live "Now" counters + activity feed. Unlike
        chat frames they are NOT trace-targeted — every dashboard client sees them.
        """
        topic = ev.get("topic", "")
        p = ev.get("payload") or {}
        ts = ev.get("ts")
        if topic == "command.executed":
            return {"type": "dash_event", "kind": "command", "ts": ts,
                    "action": p.get("action"), "route": p.get("route"),
                    "success": p.get("success"), "latency_ms": p.get("latency_ms"),
                    "gate": p.get("gate")}
        if topic == "goal.dequeued":
            return {"type": "dash_event", "kind": "goal", "ts": ts, "severity": "info",
                    "text": f"goal {p.get('goal_id')} started: {str(p.get('goal',''))[:80]}"}
        if topic == "goal.completed":
            ok = p.get("success")
            return {"type": "dash_event", "kind": "goal", "ts": ts,
                    "severity": "info" if ok else "warn",
                    "text": f"goal {p.get('goal_id')} {p.get('status')}"}
        if topic == "vram.evicted":
            return {"type": "dash_event", "kind": "vram", "ts": ts, "severity": "warn",
                    "text": f"VRAM eviction ({p.get('reason')}); free={p.get('free_gb')} GB"}
        if topic == "vram.restored":
            return {"type": "dash_event", "kind": "vram", "ts": ts, "severity": "info",
                    "text": f"models restored; free={p.get('free_gb')} GB"}
        if topic == "breaker.opened":
            return {"type": "dash_event", "kind": "breaker", "ts": ts, "severity": "warn",
                    "text": f"circuit breaker open: {p.get('name')} — {p.get('reason')}"}
        if topic == "inference.stalled":
            return {"type": "dash_event", "kind": "stall", "ts": ts, "severity": "warn",
                    "text": f"{p.get('backend')} stall ≥{p.get('timeout_s')}s ({p.get('phase')})"}
        if topic == "metric.threshold_crossed":
            # MetricWatcher publishes severity "warning"/"info" (recovery); map to
            # the feed's warn/info styling.
            sev = "info" if p.get("severity") == "info" else "warn"
            return {"type": "dash_event", "kind": "alert", "ts": ts, "severity": sev,
                    "text": p.get("message") or f"{p.get('metric')} = {p.get('value')}"}
        if topic == "slo.breached":
            return {"type": "dash_event", "kind": "alert", "ts": ts, "severity": "warn",
                    "text": (f"SLO breach: {p.get('domain')} "
                             f"{p.get('metric')}={p.get('value')} (budget {p.get('budget')})")}
        if topic == "voice.drift":
            return {"type": "dash_event", "kind": "voice", "ts": ts, "severity": "warn",
                    "text": f"voice drift {p.get('drift_pct')}% — recalibration advised"}
        if topic == "step.failed":
            return {"type": "dash_event", "kind": "dev", "ts": ts, "severity": "warn",
                    "text": (f"step {p.get('step_num')} {p.get('action')} failed: "
                             f"{str(p.get('error', ''))[:80]}")}
        if topic == "replan.exhausted":
            return {"type": "dash_event", "kind": "dev", "ts": ts, "severity": "warn",
                    "text": f"replan exhausted ({p.get('replans')}×): {str(p.get('goal', ''))[:80]}"}
        if topic == "email.arrived":
            return {"type": "dash_event", "kind": "email", "ts": ts, "severity": "info",
                    "text": f"email: {str(p.get('subject', ''))[:80]}"}
        return None
