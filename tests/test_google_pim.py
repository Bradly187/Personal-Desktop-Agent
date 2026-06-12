"""Google PIM skill — server tool logic with fully-mocked Google services (no
libraries, no network), the shipped manifest, and the on-device summarise flow.

Run:
    python -m pytest tests/test_google_pim.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from skills.servers import google_pim_server as g


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

def test_next_event_formats():
    cal = MagicMock()
    cal.events.return_value.list.return_value.execute.return_value = {"items": [{
        "summary": "Standup",
        "start": {"dateTime": "2026-06-13T09:00:00Z"},
        "attendees": [{"email": "alice@x.com"}],
    }]}
    out = g._next_event(cal, now_iso="2026-06-12T00:00:00Z")
    assert "Standup" in out and "alice@x.com" in out


def test_next_event_empty():
    cal = MagicMock()
    cal.events.return_value.list.return_value.execute.return_value = {"items": []}
    assert "No upcoming" in g._next_event(cal, now_iso="x")


def test_create_event_builds_body():
    cal = MagicMock()
    insert = cal.events.return_value.insert
    insert.return_value.execute.return_value = {"htmlLink": "http://cal/x"}
    out = g._create_event(cal, summary="Sync", start_iso="2026-06-13T10:00:00Z",
                          end_iso="2026-06-13T10:30:00Z", attendees=["a@x.com"])
    assert "Sync" in out
    body = insert.call_args.kwargs["body"]
    assert body["attendees"] == [{"email": "a@x.com"}]
    assert body["start"]["dateTime"] == "2026-06-13T10:00:00Z"


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------

def test_list_unread_formats():
    gmail = MagicMock()
    msgs = gmail.users.return_value.messages.return_value
    msgs.list.return_value.execute.return_value = {"messages": [{"id": "m1"}]}
    msgs.get.return_value.execute.return_value = {
        "snippet": "hello there",
        "payload": {"headers": [
            {"name": "From", "value": "bob@x.com"},
            {"name": "Subject", "value": "Hi"},
        ]},
    }
    out = g._list_unread(gmail, 5)
    assert "bob@x.com" in out and "Hi" in out and "hello there" in out


def test_list_unread_empty():
    gmail = MagicMock()
    gmail.users.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": []
    }
    assert "No unread" in g._list_unread(gmail, 5)


def test_send_reply_builds_raw_and_thread():
    gmail = MagicMock()
    send = gmail.users.return_value.messages.return_value.send
    send.return_value.execute.return_value = {"id": "sent1"}
    out = g._send_reply(gmail, to="a@x.com", subject="Re: hi", body="thanks", thread_id="t1")
    assert "sent1" in out
    body = send.call_args.kwargs["body"]
    assert "raw" in body and body["threadId"] == "t1"


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def test_manifest_valid_and_send_aware():
    path = Path(__file__).parent.parent / "skills" / "manifests" / "google_pim.json"
    m = json.loads(path.read_text(encoding="utf-8"))
    assert m["skill_id"] == "google_pim"
    assert m["enabled"] is False  # ships disabled until OAuth is set up
    assert set(m["tools"]["send_tools"]) == {"send_reply", "create_event"}
    assert m["intents"]["summarize_unread"]["summarize"] is True
    assert m["intents"]["next_meeting"]["send"] is False


# ---------------------------------------------------------------------------
# On-device summarise flow (DevAgent._handle_skill, mocked registry + router)
# ---------------------------------------------------------------------------

async def test_summarize_flow_runs_local_llm(monkeypatch):
    import tts.polly_stream as _ps
    monkeypatch.setattr(_ps, "get_client", lambda *a, **k: MagicMock(speak=AsyncMock()))

    from inference.dev_agent import DevAgent
    router = MagicMock()
    router.infer = AsyncMock(return_value=MagicMock(ok=True, text="2 unread: Bob, Alice."))
    agent = DevAgent(router=router)

    reg = MagicMock()
    reg.match_intent = MagicMock(return_value={
        "skill_id": "google_pim", "tool": "list_unread", "send": False,
        "summarize": True, "keyword": "unread email", "score": 11,
    })
    reg.tool_schema = MagicMock(return_value={})
    reg.is_send_tool = MagicMock(return_value=False)
    reg.call = AsyncMock(return_value={
        "status": "ok",
        "text": "- From Bob | Hi | hello\n- From Alice | Yo | hey",
    })
    agent._skill_registry = reg

    res = await agent._handle_skill("summarize my unread email")
    router.infer.assert_awaited()           # summarisation happened on-device
    assert res.response_text == "2 unread: Bob, Alice."
