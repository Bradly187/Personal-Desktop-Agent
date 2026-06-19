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
import json
import logging
import uuid
from pathlib import Path
from typing import Optional

from aiohttp import web, WSMsgType

from core.command_executor import Command
from core.scheduler import Priority

log = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent.parent / "web_client_chat"
_APPROVAL_DIR = Path.home() / ".claude" / "approval"


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
                 allow_destructive: bool = True) -> None:
        self._host = host
        self._port = port
        self._allow_destructive = allow_destructive

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

    # ── wiring ──────────────────────────────────────────────────────────────
    def set_coordinator(self, c) -> None: self._coordinator = c
    def set_scheduler(self, s) -> None: self._scheduler = s
    def set_event_bus(self, b) -> None: self._event_bus = b

    def set_agent_db(self, db, session_id: Optional[int] = None) -> None:
        self._agent_db = db
        self._session_id = session_id

    def url(self) -> str:
        return f"http://{self._host}:{self._port}/"

    # ── lifecycle ───────────────────────────────────────────────────────────
    async def start(self) -> None:
        """Bind the socket and start the EventBus pump, then return (listening).

        Lets main.py open the desktop shell only once the server is reachable.
        """
        app = web.Application()
        app.router.add_get("/", self._index_handler)
        app.router.add_get("/chat", self._ws_handler)
        app.router.add_get("/health", self._health_handler)
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
            if text:
                await self._start_request(client, text)
        elif mtype == "approval_response":
            await self._write_approval(bool(msg.get("approve")))

    # ── request lifecycle ─────────────────────────────────────────────────────
    async def _start_request(self, client: _ChatClient, text: str) -> None:
        """Route a chat message as a background task so the socket read loop
        stays free (e.g. to receive the approval-card click)."""
        if self._coordinator is None or self._scheduler is None:
            client.push({"type": "error", "error": "agent pipeline not wired"})
            return
        trace_id = uuid.uuid4().hex
        self._active[trace_id] = client.ws_id
        task = asyncio.create_task(self._run_request(client, trace_id, text))
        self._requests[trace_id] = task
        task.add_done_callback(lambda _t, tid=trace_id: self._requests.pop(tid, None))

    async def _run_request(self, client: _ChatClient, trace_id: str, text: str) -> None:
        cmd = Command(text=text, action="CLARIFY", source="chat", trace_id=trace_id)
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
