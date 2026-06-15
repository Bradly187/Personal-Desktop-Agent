"""A2UI surface builder — agent-generated declarative touch UI for the iPad.

This is a *local-first, zero-egress* adaptation of Google's A2UI standard
(see docs/research/2026-06-15-a2ui-integration-plan.md). The PC describes an
interactive surface as a flat, id-referenced component adjacency list; the iPad
renders it natively from a closed, trusted catalog (``A2UIRenderer.swift``).
There is no executable code or markup in the payload — the renderer instantiates
only known component types — which is what makes the channel safe.

Catalog (v1) — every type maps to an existing DesignSystem component:

    Column, Row, Card, Text, Button, ChoicePicker, TextField, Divider

Two construction patterns from the whitepaper:

  * tool-as-template — deterministic surfaces (``approval_surface``); no LLM.
  * LLM-generated    — novel clarifications (``choice_surface`` fed by the
                       coordinator's enumerated options).

Usage::

    from core import a2ui
    surface = a2ui.approval_surface("Write to config.json")
    await bridge.send_a2ui_surface(surface)

The matching iPad → PC reply is an ``a2ui_event`` message
(``{surface_id, event, value, values}``) handled in ``core/ipad_bridge.py``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

VERSION = "v0.9"

# Closed v1 catalog. The renderer enforces the same set by exhaustive switch;
# this mirror lets us validate before sending (and in tests) so the iPad never
# receives an unknown component type.
CATALOG: frozenset[str] = frozenset(
    {"Column", "Row", "Card", "Text", "Button", "ChoicePicker", "TextField", "Divider"}
)

# Text variants the renderer maps onto DesignTokens.Typography.
TEXT_VARIANTS: frozenset[str] = frozenset({"headline", "body", "caption"})


def _gen_surface_id(prefix: str) -> str:
    """Short unique surface id. Runtime-only — never used in cached/replayed paths."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@dataclass
class A2UIBuilder:
    """Assembles a flat component adjacency list with managed, unique ids.

    Build children first, then call :meth:`surface` with the root's children.
    Every ``add_*`` returns the node id so callers can wire parents by id.
    """

    _nodes: list[dict] = field(default_factory=list)
    _ids: set[str] = field(default_factory=set)

    def _add(self, node: dict) -> str:
        nid = node["id"]
        if nid in self._ids:
            raise ValueError(f"duplicate component id: {nid!r}")
        self._ids.add(nid)
        self._nodes.append(node)
        return nid

    def text(self, nid: str, text: str, variant: str = "body") -> str:
        if variant not in TEXT_VARIANTS:
            raise ValueError(f"unknown text variant: {variant!r}")
        return self._add({"id": nid, "component": "Text", "text": text, "variant": variant})

    def button(
        self,
        nid: str,
        label: str,
        value: str,
        *,
        event: str = "choice",
        icon: Optional[str] = None,
    ) -> str:
        node: dict[str, Any] = {
            "id": nid,
            "component": "Button",
            "label": label,
            "action": {"event": event, "value": value},
        }
        if icon:
            node["icon"] = icon
        return self._add(node)

    def text_field(self, nid: str, field_id: str, placeholder: str = "") -> str:
        return self._add(
            {"id": nid, "component": "TextField", "field_id": field_id, "placeholder": placeholder}
        )

    def divider(self, nid: str) -> str:
        return self._add({"id": nid, "component": "Divider"})

    def row(self, nid: str, children: list[str]) -> str:
        return self._add({"id": nid, "component": "Row", "children": list(children)})

    def column(self, nid: str, children: list[str]) -> str:
        return self._add({"id": nid, "component": "Column", "children": list(children)})

    def card(self, nid: str, children: list[str]) -> str:
        return self._add({"id": nid, "component": "Card", "children": list(children)})

    def surface(
        self,
        root: str,
        *,
        surface_id: str,
        dismissible: bool = True,
        timeout_s: float = 30.0,
    ) -> dict:
        if root not in self._ids:
            raise ValueError(f"root id {root!r} not among built components")
        return {
            "type": "a2ui_surface",
            "surface_id": surface_id,
            "version": VERSION,
            "root": root,
            "dismissible": bool(dismissible),
            "timeout_s": float(timeout_s),
            "components": list(self._nodes),
        }


# --------------------------------------------------------------------------- #
# Tool-as-template surfaces (deterministic — no LLM)
# --------------------------------------------------------------------------- #


def approval_surface(action_desc: str, *, surface_id: Optional[str] = None, timeout_s: float = 30.0) -> dict:
    """Approve / Deny card for the approval gate (Phase 1).

    A tap on either button produces an ``a2ui_event`` with ``event="approval"``
    and ``value="approve"|"deny"`` — the bridge writes the SAME canonical token
    to ``~/.claude/approval/response`` that a voice confirmation would, so this
    is a parallel input, not a new decision path. Timeout fails safe to deny.
    """
    sid = surface_id or _gen_surface_id("approval")
    b = A2UIBuilder()
    b.text("q", action_desc, "headline")
    b.text("hint", "Approve this action?", "caption")
    b.button("approve", "Approve", "approve", event="approval", icon="checkmark.circle.fill")
    b.button("deny", "Deny", "deny", event="approval", icon="xmark.circle.fill")
    b.row("buttons", ["approve", "deny"])
    b.card("root", ["q", "hint", "buttons"])
    # Not dismissible: a stray tap-away must not silently resolve a security gate.
    return b.surface("root", surface_id=sid, dismissible=False, timeout_s=timeout_s)


def choice_surface(
    question: str,
    options: list[tuple[str, str]],
    *,
    surface_id: Optional[str] = None,
    event: str = "clarify",
    timeout_s: float = 30.0,
) -> dict:
    """Multiple-choice card for enumerable CLARIFY (Phase 2).

    ``options`` is a list of ``(label, value)`` pairs — e.g. scroll direction
    ``[("Up","up"),("Down","down"),...]`` or app/file/other type disambiguation.
    A tap yields ``a2ui_event`` with the chosen ``value``; voice still works in
    parallel via the existing ``_pending_clarification`` path.
    """
    if not options:
        raise ValueError("choice_surface requires at least one option")
    sid = surface_id or _gen_surface_id("clarify")
    b = A2UIBuilder()
    b.text("q", question, "headline")
    btn_ids: list[str] = []
    for i, (label, value) in enumerate(options):
        bid = b.button(f"opt{i}", label, value, event=event)
        btn_ids.append(bid)
    b.column("choices", btn_ids)
    b.card("root", ["q", "choices"])
    return b.surface("root", surface_id=sid, dismissible=True, timeout_s=timeout_s)


# --------------------------------------------------------------------------- #
# CLARIFY template library (Phase 2) — token-free: deterministic Python builds
# the surface, the LLM is never asked to generate UI. Keyed to the recurring
# enumerable CLARIFY shapes found in the agent.db audit (2026-06-15):
#   direction (42% bucket), type-disambiguation, post-open action.
# A tap re-enters the pipeline as a voice-equivalent answer ("up", "app", …),
# which the existing pending-clarification context resolves exactly like speech.
# --------------------------------------------------------------------------- #

import re

_DIRECTION_OPTS = [("Up", "up"), ("Down", "down"), ("Left", "left"), ("Right", "right")]
_OPEN_TYPE_OPTS = [("Application", "application"), ("File", "file"), ("Something else", "other")]
_POST_OPEN_OPTS = [("Click", "click"), ("Drag", "drag"), ("Dwell", "dwell")]


def direction_surface(question: str = "Which direction?", *, surface_id: Optional[str] = None) -> dict:
    return choice_surface(question, _DIRECTION_OPTS, surface_id=surface_id)


def open_type_surface(name: str = "", *, surface_id: Optional[str] = None) -> dict:
    q = f'What is "{name}"?' if name else "What kind of thing is that?"
    return choice_surface(q, _OPEN_TYPE_OPTS, surface_id=surface_id)


def post_open_action_surface(app: str = "", *, surface_id: Optional[str] = None) -> dict:
    q = f"What should I do in {app}?" if app else "What should I do next?"
    return choice_surface(q, _POST_OPEN_OPTS, surface_id=surface_id)


# Max app buttons to show — keep the card glanceable and within a flare-day reach.
_MAX_OPEN_TARGETS = 5


def open_target_surface(apps: list[str], *, surface_id: Optional[str] = None) -> dict:
    """'What would you like to open?' → buttons for the user's recent apps.

    SEMI-enumerable: the options come from the live recent-apps list, not a fixed
    set. Each button's value is the app name, so a tap routes 'open <app>'-style
    text resolved by the pending-clarification context. Still token-free.
    """
    opts = [(a, a) for a in apps[:_MAX_OPEN_TARGETS] if a]
    if not opts:
        raise ValueError("open_target_surface requires at least one app")
    return choice_surface("What would you like to open?", opts, surface_id=surface_id)


def template_for_clarify(message: str, *, recent_apps: Optional[list[str]] = None) -> Optional[dict]:
    """Map a CLARIFY message to a deterministic surface, or None for free-form.

    Pure string classification — no model call. Returns None for the ~31% of
    CLARIFYs that aren't enumerable (the coordinator then falls back to voice).
    """
    if not message:
        return None
    low = message.lower()

    # Scroll direction — strongest, most unambiguous signal.
    if "up, down" in low or "up/down" in low or "which direction" in low:
        return direction_surface(message)

    # Post-open action — "should I click, drag, ...?" / "what action ... perform".
    if "should i click" in low or "click, drag" in low or "action would you like me to perform" in low:
        m = re.search(r"\b(?:in|after opening|with)\s+([A-Z][\w.-]*)", message)
        return post_open_action_surface(m.group(1) if m else "")

    # Type disambiguation — 'What is "X"? Is it an application name, a file…'.
    if "is it an application" in low or "application name, a file" in low or "application name, file" in low:
        m = re.search(r'"([^"]+)"', message)
        return open_type_surface(m.group(1) if m else "")

    # Open target — "What would you like to open?". SEMI-enumerable: only render
    # a card when we have the user's recent apps; otherwise fall back to voice.
    if "would you like to open" in low and recent_apps:
        try:
            return open_target_surface(recent_apps)
        except ValueError:
            return None

    return None


# Named registry for callers that want to pick a template explicitly.
TEMPLATES = {
    "scroll_direction": direction_surface,
    "open_type": open_type_surface,
    "post_open_action": post_open_action_surface,
}


# --------------------------------------------------------------------------- #
# Click-target palette (Phase 3, prototype) — "what would you like to click?"
# is not enumerable from text, but the live UI tree IS: the coordinator ranks
# clickable elements from target_cache and we render them as a tappable list.
# Each button carries the element's pixel center as "x,y"; the bridge dispatches
# a coordinate-precise CLICK (no re-grounding), so it's still token-free.
# --------------------------------------------------------------------------- #

_CLICK_TARGET_PATTERNS = (
    "click on", "target for the click", "target of the click", "which input field",
)

_MAX_CLICK_TARGETS = 8


def is_click_target_clarify(message: str) -> bool:
    """True when a CLARIFY is asking the user which on-screen element to click."""
    if not message:
        return False
    low = message.lower()
    return any(p in low for p in _CLICK_TARGET_PATTERNS)


def click_target_surface(targets: list[tuple[str, int, int]], *, surface_id: Optional[str] = None) -> dict:
    """Render ranked clickable elements as buttons.

    ``targets`` is a list of ``(label, x, y)``; a tap fires ``event="click_target"``
    with ``value="x,y"`` which the bridge turns into a direct CLICK at that point.
    """
    items = [(str(lbl), int(x), int(y)) for (lbl, x, y) in targets[:_MAX_CLICK_TARGETS] if lbl]
    if not items:
        raise ValueError("click_target_surface requires at least one target")
    sid = surface_id or _gen_surface_id("clicktgt")
    b = A2UIBuilder()
    b.text("q", "Which element?", "headline")
    btn_ids: list[str] = []
    for i, (lbl, x, y) in enumerate(items):
        bid = b.button(f"t{i}", lbl[:40], f"{x},{y}", event="click_target")
        btn_ids.append(bid)
    b.column("choices", btn_ids)
    b.card("root", ["q", "choices"])
    return b.surface("root", surface_id=sid, dismissible=True, timeout_s=30.0)


# --------------------------------------------------------------------------- #
# Validation — run before send so the renderer never sees a malformed payload
# --------------------------------------------------------------------------- #


def validate_surface(payload: dict) -> list[str]:
    """Return a list of schema problems (empty list == valid).

    Checks: required top-level fields, known component types, unique ids, that
    every ``children`` / root reference resolves, and that Buttons carry an
    action. Mirrors the renderer's tolerances so PC-side validation is
    authoritative (whitepaper best practice: validate, then fall back to voice).
    """
    errs: list[str] = []
    if payload.get("type") != "a2ui_surface":
        errs.append("type must be 'a2ui_surface'")
    comps = payload.get("components")
    if not isinstance(comps, list) or not comps:
        errs.append("components must be a non-empty list")
        return errs

    ids: set[str] = set()
    for c in comps:
        cid = c.get("id")
        ctype = c.get("component")
        if not cid:
            errs.append("component missing id")
            continue
        if cid in ids:
            errs.append(f"duplicate id: {cid!r}")
        ids.add(cid)
        if ctype not in CATALOG:
            errs.append(f"unknown component type {ctype!r} (id={cid!r})")
        if ctype == "Button" and "action" not in c:
            errs.append(f"Button {cid!r} missing action")
        if ctype == "Text" and c.get("variant", "body") not in TEXT_VARIANTS:
            errs.append(f"Text {cid!r} bad variant {c.get('variant')!r}")

    root = payload.get("root")
    if root not in ids:
        errs.append(f"root {root!r} not a known component id")

    for c in comps:
        for child in c.get("children", []) or []:
            if child not in ids:
                errs.append(f"{c.get('id')!r} references missing child {child!r}")
    return errs
