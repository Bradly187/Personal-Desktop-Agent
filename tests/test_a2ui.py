"""Tests for core/a2ui.py — A2UI surface builder + validation, and the bridge
a2ui_event resolution paths (approval response file + pending-surface Future)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core import a2ui


# --------------------------------------------------------------------------- #
# Builder + template surfaces
# --------------------------------------------------------------------------- #


def test_approval_surface_is_valid_and_not_dismissible():
    s = a2ui.approval_surface("Write to config.json")
    assert a2ui.validate_surface(s) == []
    assert s["type"] == "a2ui_surface"
    assert s["dismissible"] is False  # a security gate must not tap-away resolve
    assert s["surface_id"].startswith("approval-")
    # both Approve and Deny carry an approval action
    actions = {c["action"]["value"] for c in s["components"] if c.get("action")}
    assert actions == {"approve", "deny"}
    assert all(
        c["action"]["event"] == "approval"
        for c in s["components"]
        if c.get("action")
    )


def test_choice_surface_direction_is_valid_four_buttons():
    opts = [("Up", "up"), ("Down", "down"), ("Left", "left"), ("Right", "right")]
    s = a2ui.choice_surface("Which direction?", opts)
    assert a2ui.validate_surface(s) == []
    buttons = [c for c in s["components"] if c["component"] == "Button"]
    assert len(buttons) == 4
    assert [b["action"]["value"] for b in buttons] == ["up", "down", "left", "right"]
    assert s["dismissible"] is True


def test_choice_surface_requires_options():
    with pytest.raises(ValueError):
        a2ui.choice_surface("Pick", [])


def test_explicit_surface_id_is_preserved():
    s = a2ui.approval_surface("x", surface_id="approval-fixed")
    assert s["surface_id"] == "approval-fixed"


def test_builder_rejects_duplicate_ids():
    b = a2ui.A2UIBuilder()
    b.text("q", "hi")
    with pytest.raises(ValueError):
        b.text("q", "again")


def test_builder_rejects_unknown_text_variant():
    b = a2ui.A2UIBuilder()
    with pytest.raises(ValueError):
        b.text("q", "hi", variant="gigantic")


def test_surface_rejects_unbuilt_root():
    b = a2ui.A2UIBuilder()
    b.text("q", "hi")
    with pytest.raises(ValueError):
        b.surface("nonexistent", surface_id="s1")


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def test_validate_flags_unknown_component():
    bad = {
        "type": "a2ui_surface",
        "root": "root",
        "components": [
            {"id": "root", "component": "Column", "children": ["x"]},
            {"id": "x", "component": "WebView"},  # not in catalog
        ],
    }
    errs = a2ui.validate_surface(bad)
    assert any("unknown component" in e for e in errs)


def test_validate_flags_missing_child_reference():
    bad = {
        "type": "a2ui_surface",
        "root": "root",
        "components": [{"id": "root", "component": "Column", "children": ["ghost"]}],
    }
    errs = a2ui.validate_surface(bad)
    assert any("missing child" in e for e in errs)


def test_validate_flags_button_without_action():
    bad = {
        "type": "a2ui_surface",
        "root": "b",
        "components": [{"id": "b", "component": "Button", "label": "Go"}],
    }
    errs = a2ui.validate_surface(bad)
    assert any("missing action" in e for e in errs)


def test_validate_flags_bad_root():
    bad = {
        "type": "a2ui_surface",
        "root": "nope",
        "components": [{"id": "t", "component": "Text", "text": "hi", "variant": "body"}],
    }
    errs = a2ui.validate_surface(bad)
    assert any("root" in e for e in errs)


def test_validate_requires_components():
    assert a2ui.validate_surface({"type": "a2ui_surface", "components": []})


# --------------------------------------------------------------------------- #
# CLARIFY template library (Phase 2)
# --------------------------------------------------------------------------- #


def test_template_matches_scroll_direction():
    s = a2ui.template_for_clarify(
        "In which direction would you like to scroll: up, down, left, or right?")
    assert s is not None and a2ui.validate_surface(s) == []
    values = [c["action"]["value"] for c in s["components"] if c.get("action")]
    assert values == ["up", "down", "left", "right"]


def test_template_matches_open_type_and_extracts_name():
    s = a2ui.template_for_clarify(
        'What is "VS Code"? Is it an application name, a file, or something else?')
    assert s is not None
    title = next(c["text"] for c in s["components"] if c["id"] == "q")
    assert "VS Code" in title
    values = [c["action"]["value"] for c in s["components"] if c.get("action")]
    assert values == ["application", "file", "other"]


def test_template_matches_post_open_action():
    s = a2ui.template_for_clarify(
        "What specific action would you like me to perform after opening VS Code? "
        "Should I click, drag, or interact with a particular element?")
    assert s is not None
    values = [c["action"]["value"] for c in s["components"] if c.get("action")]
    assert values == ["click", "drag", "dwell"]


def test_template_returns_none_for_free_form():
    assert a2ui.template_for_clarify("What would you like me to click on?") is None
    assert a2ui.template_for_clarify("What is the target for the click?") is None
    assert a2ui.template_for_clarify("") is None


def test_open_target_template_uses_recent_apps():
    s = a2ui.template_for_clarify(
        "What would you like to open? (an application, file, or folder name)",
        recent_apps=["VS Code", "Chrome", "Slack"])
    assert s is not None and a2ui.validate_surface(s) == []
    values = [c["action"]["value"] for c in s["components"] if c.get("action")]
    assert values == ["VS Code", "Chrome", "Slack"]


def test_open_target_template_falls_back_to_voice_without_recent_apps():
    # No recent apps → no enumerable card → voice fallback.
    assert a2ui.template_for_clarify("What would you like to open?") is None
    assert a2ui.template_for_clarify("What would you like to open?", recent_apps=[]) is None


def test_open_target_surface_caps_at_five():
    s = a2ui.open_target_surface(["a", "b", "c", "d", "e", "f", "g"])
    buttons = [c for c in s["components"] if c["component"] == "Button"]
    assert len(buttons) == 5


def test_template_events_are_clarify_so_taps_route_as_commands():
    s = a2ui.direction_surface()
    assert all(c["action"]["event"] == "clarify"
               for c in s["components"] if c.get("action"))


# --------------------------------------------------------------------------- #
# Click-target palette (Phase 3 prototype)
# --------------------------------------------------------------------------- #


def test_is_click_target_clarify():
    assert a2ui.is_click_target_clarify("What would you like me to click on?")
    assert a2ui.is_click_target_clarify("What is the target for the click?")
    assert a2ui.is_click_target_clarify("Which input field would you like me to click on?")
    assert not a2ui.is_click_target_clarify("In which direction would you like to scroll: up, down?")
    assert not a2ui.is_click_target_clarify("")


def test_click_target_surface_encodes_coords_and_event():
    s = a2ui.click_target_surface([("Submit", 100, 200), ("Search box", 50, 60)])
    assert a2ui.validate_surface(s) == []
    buttons = [c for c in s["components"] if c["component"] == "Button"]
    assert buttons[0]["label"] == "Submit"
    assert buttons[0]["action"] == {"event": "click_target", "value": "100,200"}


def test_click_target_surface_caps_and_requires_one():
    s = a2ui.click_target_surface([(f"el{i}", i, i) for i in range(20)])
    assert len([c for c in s["components"] if c["component"] == "Button"]) == 8
    with pytest.raises(ValueError):
        a2ui.click_target_surface([])


class _FakeTarget:
    def __init__(self, name, bounds):
        self.name = name
        self.bounds = bounds


def test_rank_click_targets_filters_dedups_and_orders():
    from core.action_executor import ActionExecutor
    snap = [
        _FakeTarget("", (0, 0, 50, 20)),            # unnamed → dropped
        _FakeTarget("Tiny", (0, 0, 4, 4)),          # too small → dropped
        _FakeTarget("Huge", (0, 0, 1600, 1200)),    # oversized → dropped
        _FakeTarget("Near", (100, 100, 140, 120)),  # center (120,110)
        _FakeTarget("Far", (900, 900, 940, 920)),   # center (920,910)
        _FakeTarget("Near", (100, 100, 140, 120)),  # dup name → dropped
    ]
    ranked = ActionExecutor.rank_click_targets(snap, cursor=(120, 110))
    names = [r[0] for r in ranked]
    assert names == ["Near", "Far"]          # nearest first, deduped, filtered
    assert ranked[0][1:] == (120, 110)       # center coords


def test_rank_click_targets_reading_order_without_cursor():
    from core.action_executor import ActionExecutor
    snap = [
        _FakeTarget("Bottom", (10, 500, 60, 520)),
        _FakeTarget("Top", (10, 10, 60, 30)),
    ]
    ranked = ActionExecutor.rank_click_targets(snap, cursor=None)
    assert [r[0] for r in ranked] == ["Top", "Bottom"]


def test_record_open_target_dedup_recency_and_cap():
    """ActionExecutor's recent-OPEN buffer: most-recent-first, deduped, capped."""
    from core.action_executor import ActionExecutor

    from core.coordinator_state import CoordinatorState
    state = CoordinatorState()
    ae = ActionExecutor(
        executor=lambda: None, grounder=lambda: None, conversation=lambda: None,
        metrics=lambda: None, whisper=lambda: None, bridge=lambda: None,
        target_cache=lambda: None,
        state=state,
    )

    for app in ["Chrome", "vscode", "Slack"]:
        ae.record_open_target(app)
    assert state.recent_open_targets == ["Slack", "vscode", "Chrome"]

    # Re-open vscode → moves to front, no duplicate (case-insensitive).
    ae.record_open_target("vscode")
    assert state.recent_open_targets == ["vscode", "Slack", "Chrome"]

    # Blank target is ignored.
    ae.record_open_target("   ")
    assert state.recent_open_targets == ["vscode", "Slack", "Chrome"]

    # Cap at 8.
    for i in range(10):
        ae.record_open_target(f"App{i}")
    assert len(state.recent_open_targets) == 8
    assert state.recent_open_targets[0] == "App9"


# --------------------------------------------------------------------------- #
# Bridge a2ui_event resolution
# --------------------------------------------------------------------------- #


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


def _make_bridge(tmp_path: Path):
    from core.ipad_bridge import IPadBridge

    # Inject a token so construction never touches ~/.claude/ipad_bridge/.
    bridge = IPadBridge(port=0, host="127.0.0.1", token="test-token")
    # Redirect approval dir into the tmp sandbox.
    bridge._approval_dir = tmp_path / "approval"
    return bridge


@pytest.mark.asyncio
async def test_a2ui_approval_event_writes_response_file(tmp_path):
    bridge = _make_bridge(tmp_path)
    ws = _FakeWS()
    await bridge._handle_a2ui_event(
        ws, {"type": "a2ui_event", "surface_id": "approval-1",
             "event": "approval", "value": "approve"}
    )
    resp = (tmp_path / "approval" / "response").read_text(encoding="utf-8")
    assert resp == "approve"


@pytest.mark.asyncio
async def test_a2ui_approval_unknown_value_fails_safe_to_deny(tmp_path):
    bridge = _make_bridge(tmp_path)
    ws = _FakeWS()
    await bridge._handle_a2ui_event(
        ws, {"surface_id": "approval-2", "event": "approval", "value": "garbage"}
    )
    resp = (tmp_path / "approval" / "response").read_text(encoding="utf-8")
    assert resp == "deny"


@pytest.mark.asyncio
async def test_a2ui_choice_event_resolves_pending_future(tmp_path):
    bridge = _make_bridge(tmp_path)
    ws = _FakeWS()
    fut = bridge.register_a2ui_surface("clarify-9")
    await bridge._handle_a2ui_event(
        ws, {"surface_id": "clarify-9", "event": "choice", "value": "down"}
    )
    assert fut.done()
    result = await fut
    assert result["value"] == "down"


@pytest.mark.asyncio
async def test_a2ui_event_for_unknown_surface_is_noop(tmp_path):
    bridge = _make_bridge(tmp_path)
    ws = _FakeWS()
    bridge._coordinator = None  # no coordinator, no future → pure no-op
    # No future registered, no approval — must not raise, just ack.
    await bridge._handle_a2ui_event(
        ws, {"surface_id": "ghost", "event": "choice", "value": "x"}
    )
    assert ws.sent and ws.sent[-1].get("status") == "ok"


@pytest.mark.asyncio
async def test_a2ui_click_target_tap_dispatches_click_at_coords(tmp_path):
    """A click_target tap dispatches a coordinate-precise CLICK via the
    touch-bypass path (source=touch, gaze_coords set), not a voice re-route."""
    bridge = _make_bridge(tmp_path)
    ws = _FakeWS()
    routed = []

    class _FakeCoordinator:
        async def route(self, cmd):
            routed.append(cmd)

    bridge._coordinator = _FakeCoordinator()
    await bridge._handle_a2ui_event(
        ws, {"surface_id": "clicktgt-1", "event": "click_target", "value": "640,480"}
    )
    await asyncio.sleep(0)
    assert len(routed) == 1
    cmd = routed[0]
    assert cmd.action == "CLICK"
    assert cmd.source == "touch"
    assert cmd.gaze_coords == (640, 480)


@pytest.mark.asyncio
async def test_a2ui_click_target_bad_value_is_safe(tmp_path):
    bridge = _make_bridge(tmp_path)
    ws = _FakeWS()

    class _FakeCoordinator:
        async def route(self, cmd):
            raise AssertionError("should not route on bad coords")

    bridge._coordinator = _FakeCoordinator()
    await bridge._handle_a2ui_event(
        ws, {"surface_id": "x", "event": "click_target", "value": "not-coords"}
    )
    assert ws.sent and ws.sent[-1].get("status") == "ok"


@pytest.mark.asyncio
async def test_a2ui_clarify_tap_routes_voice_command(tmp_path):
    """A CLARIFY tap with no registered Future re-enters the pipeline as a
    voice-equivalent command so the pending-clarification context resolves it."""
    bridge = _make_bridge(tmp_path)
    ws = _FakeWS()

    routed = []

    class _FakeCoordinator:
        async def route(self, cmd):
            routed.append(cmd)

    bridge._coordinator = _FakeCoordinator()
    await bridge._handle_a2ui_event(
        ws, {"surface_id": "clarify-x", "event": "clarify", "value": "up"}
    )
    # route() is scheduled via create_task — let it run.
    await asyncio.sleep(0)
    assert len(routed) == 1
    assert routed[0].text == "up"
    assert routed[0].source == "voice"


# --------------------------------------------------------------------------- #
# Persistent dashboard canvas
# --------------------------------------------------------------------------- #


def test_canvas_builder_has_canvas_type_and_no_timeout():
    b = a2ui.A2UIBuilder()
    b.text("t", "Agent", "headline")
    b.column("root", ["t"])
    c = b.canvas("root")
    assert c["type"] == "a2ui_canvas"
    assert c["surface_id"] == a2ui.CANVAS_ID
    assert "timeout_s" not in c and "dismissible" not in c   # persistent, not transient
    assert a2ui.validate_surface(c) == []


def test_status_dashboard_valid_and_renders_rows():
    c = a2ui.status_dashboard(
        "Agent Status",
        [("Connection", "online"), ("Pain day", "no")],
        actions=[("Pause", "pause")],
    )
    assert c["type"] == "a2ui_canvas"
    assert a2ui.validate_surface(c) == []
    texts = [n["text"] for n in c["components"] if n["component"] == "Text"]
    assert "Connection: online" in texts
    btns = [n for n in c["components"] if n["component"] == "Button"]
    assert btns and btns[0]["action"] == {"event": "canvas", "value": "pause"}


def test_validate_accepts_canvas_type():
    c = a2ui.status_dashboard("X", [("a", "b")])
    assert a2ui.validate_surface(c) == []


def test_canvas_update_and_clear_message_shapes():
    upd = a2ui.canvas_update_message(
        [{"id": "row0", "component": "Text", "text": "x", "variant": "body"}]
    )
    assert upd["type"] == "a2ui_canvas_update" and upd["surface_id"] == "dashboard"
    assert upd["components"][0]["id"] == "row0"
    clr = a2ui.canvas_clear_message()
    assert clr == {"type": "a2ui_canvas_clear", "surface_id": "dashboard"}


@pytest.mark.asyncio
async def test_bridge_send_canvas_validates(tmp_path):
    bridge = _make_bridge(tmp_path)
    sent = []

    async def _capture(payload):
        sent.append(payload)

    bridge.broadcast_json = _capture

    ok = await bridge.send_a2ui_canvas(a2ui.status_dashboard("X", [("a", "b")]))
    assert ok is True and sent and sent[-1]["type"] == "a2ui_canvas"

    bad = await bridge.send_a2ui_canvas({"type": "a2ui_canvas", "components": []})
    assert bad is False   # invalid → not sent


@pytest.mark.asyncio
async def test_canvas_tap_routes_value_as_command(tmp_path):
    """A dashboard button tap with no registered Future routes its value as a
    voice-equivalent command through the coordinator."""
    bridge = _make_bridge(tmp_path)
    ws = _FakeWS()
    routed = []

    class _FakeCoordinator:
        async def route(self, cmd):
            routed.append(cmd)

    bridge._coordinator = _FakeCoordinator()
    await bridge._handle_a2ui_event(
        ws, {"surface_id": "dashboard", "event": "canvas", "value": "open vscode"}
    )
    await asyncio.sleep(0)
    assert len(routed) == 1
    assert routed[0].text == "open vscode"
    assert routed[0].source == "voice"


@pytest.mark.asyncio
async def test_canvas_tap_resolves_registered_future(tmp_path):
    """If the agent registered a Future for the canvas (awaiting an interaction),
    a tap resolves it instead of routing a command."""
    bridge = _make_bridge(tmp_path)
    ws = _FakeWS()
    routed = []

    class _FakeCoordinator:
        async def route(self, cmd):
            routed.append(cmd)

    bridge._coordinator = _FakeCoordinator()
    fut = bridge.register_a2ui_surface("dashboard")
    await bridge._handle_a2ui_event(
        ws, {"surface_id": "dashboard", "event": "canvas", "value": "pause"}
    )
    await asyncio.sleep(0)
    assert fut.done()
    assert (await fut)["value"] == "pause"
    assert routed == []   # future path wins; no command routed


def test_build_status_dashboard_is_valid_canvas(tmp_path):
    bridge = _make_bridge(tmp_path)
    # No coordinator/whisper wired — must still build a valid dashboard.
    c = bridge._build_status_dashboard()
    assert c["type"] == "a2ui_canvas"
    assert a2ui.validate_surface(c) == []
    texts = [n["text"] for n in c["components"] if n["component"] == "Text"]
    assert any("Status" in t for t in texts)
    # The action button routes as a canvas command.
    btns = [n for n in c["components"] if n["component"] == "Button"]
    assert btns and btns[0]["action"]["event"] == "canvas"


def test_build_status_dashboard_reads_pain_day(tmp_path):
    bridge = _make_bridge(tmp_path)

    class _Mem:
        def get_pain_day_active(self):
            return True

    class _Coord:
        _memory = _Mem()

    bridge._coordinator = _Coord()
    c = bridge._build_status_dashboard()
    texts = [n["text"] for n in c["components"] if n["component"] == "Text"]
    assert "Pain day: Yes" in texts


@pytest.mark.asyncio
async def test_push_status_dashboard_broadcasts(tmp_path):
    bridge = _make_bridge(tmp_path)
    sent = []

    async def _capture(payload):
        sent.append(payload)

    bridge.broadcast_json = _capture
    await bridge.push_status_dashboard()
    assert sent and sent[-1]["type"] == "a2ui_canvas"
