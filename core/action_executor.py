"""ActionExecutor — verb execution, target grounding, and click-target surfacing
extracted from HybridCoordinator.

Owns:
  - execute_action: parses the LLM's action string, validates it against the
    command-path verb vocabulary, grounds CLICK targets via vision, dispatches
    to CommandExecutor, and handles the verify_failed/resolve_miss → CLARIFY
    conversion (with one corrective UIA retry for a vision-resolved CLICK).
  - ground_target: screenshot + VisionGrounder resolution for a named target.
  - parse_action / parse_params: pure LLM-output parsing helpers.
  - The click-target A2UI palette: rank_click_targets, build_click_target_surface,
    maybe_emit_clarify_surface, clear_clarify_surface, record_open_target.

Cross-object coordinator state (executor, grounder, metrics, whisper, bridge,
target_cache, conversation) is injected as zero-arg accessor callables resolved
at call time — several are wired onto HybridCoordinator after construction via
a set_* method, or monkeypatched directly by tests post-construction, so a
captured-by-value reference would silently go stale (see GateEvaluator /
InferenceRunner for the same pattern). ``pending_clarification`` and
``active_clarify_surface_id`` and ``recent_open_targets`` remain physically
stored on HybridCoordinator (accessed via get/set callables) since route()
reads pending_clarification directly outside of execute_action.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import replace as _dc_replace
from typing import Callable, Optional

from core.command_executor import Command
from monitoring.trace import get_tracer

log = logging.getLogger(__name__)


class ActionExecutor:
    def __init__(
        self,
        *,
        executor: Callable[[], object],
        grounder: Callable[[], object],
        conversation: Callable[[], object],
        metrics: Callable[[], object],
        whisper: Callable[[], object],
        bridge: Callable[[], object],
        target_cache: Callable[[], object],
        state: "CoordinatorState",
    ) -> None:
        self._executor = executor
        self._grounder = grounder
        self._conversation = conversation
        self._metrics = metrics
        self._whisper = whisper
        self._bridge = bridge
        self._target_cache = target_cache
        self._state = state

    # ---------------------------------------------------------------------- #
    # Action execution
    # ---------------------------------------------------------------------- #

    async def execute_action(
        self, action_str: str, cmd: Command, route_label: str = "local"
    ) -> dict:
        """Parse the LLM's action string and execute it via CommandExecutor."""
        if not action_str:
            return {"status": "error", "error": "empty action string"}

        # Deferred import: avoids a circular import with core.hybrid_coordinator,
        # which defines the vocabulary and imports ActionExecutor at module level.
        from core.hybrid_coordinator import _VALID_COMMAND_VERBS

        verb, target = self.parse_action(action_str)

        # Output-schema validation: the command-path LLM must answer with one of
        # the 11 accessibility verbs. A response whose first token is anything
        # else (prose, a hallucinated verb, a refusal) is malformed — degrade to
        # CLARIFY so the user is re-prompted instead of dispatching a bad verb
        # that would surface as a raw "Unknown action" error. The original
        # malformed action_str is still recorded to agent.db by route() for
        # later analysis.
        if verb not in _VALID_COMMAND_VERBS:
            log.warning(
                "Malformed LLM action %r — first token %r not in command "
                "vocabulary; degrading to CLARIFY",
                action_str, verb,
            )
            metrics = self._metrics()
            if metrics is not None:
                _rec = getattr(metrics, "record_malformed_action", None)
                if _rec is not None:
                    try:
                        _rec(action_str)
                    except Exception:
                        pass
            verb = "CLARIFY"
            target = "Sorry, I didn't catch that. Could you say it again?"

        params = self.parse_params(verb, target, cmd)
        # CLARIFY from a cloud route should speak via Polly TTS
        if route_label == "cloud":
            params["route"] = "cloud"

        # Vision grounding: resolve named CLICK targets to pixel coords.
        # Only runs when there's a named target and no coords already supplied
        # (explicit click coords or x/y from touch). Falls through silently on
        # any failure — CommandExecutor's Tesseract + cursor fallback takes over.
        grounded_coords = cmd.gaze_coords
        used_vision = False
        if verb == "CLICK" and target and "x" not in params:
            vision_coords = await self.ground_target(target)
            if vision_coords:
                params["x"], params["y"] = vision_coords
                grounded_coords = vision_coords
                used_vision = True

        exec_cmd = Command(
            text=target or cmd.text,
            action=verb,
            source=cmd.source,
            whisper_logprob=cmd.whisper_logprob,
            gesture_confidence=cmd.gesture_confidence,
            session_context=cmd.session_context,
            gaze_coords=grounded_coords,
            params=params,
        )

        # Pre-suppress: flush buffer and block mic BEFORE TTS starts playing.
        # This prevents Danielle's voice from accumulating in the WhisperStream
        # buffer and being transcribed as a new command mid-playback.
        whisper = self._whisper()
        if verb == "CLARIFY" and whisper is not None:
            word_count = len((target or "").split())
            # Generative TTS is ~0.45s/word; add 1s safety margin before + tail
            pre_suppress_s = max(3.0, word_count * 0.45 + 1.0)
            whisper.suppress(pre_suppress_s)
            log.debug("Pre-CLARIFY mic suppressed for %.1fs", pre_suppress_s)

        executor = self._executor()
        with get_tracer().timed("execute", verb=verb):
            result = await executor.execute(exec_cmd)

        # EH-1 (E1): a vision-resolved CLICK that produced no visible change gets
        # ONE corrective retry forcing the structured (UIAutomation) resolver —
        # the vision coords may simply have been wrong. Strictly one attempt; a
        # second miss falls through to the CLARIFY conversion below.
        if (result.get("status") == "verify_failed" and verb == "CLICK"
                and used_vision):
            log.info(
                "CLICK %r verify-failed on vision coords — retrying via UIAutomation",
                target,
            )
            retry_params = {k: v for k, v in params.items() if k not in ("x", "y")}
            retry_cmd = _dc_replace(exec_cmd, params=retry_params, gaze_coords=None)
            with get_tracer().timed("execute", verb=verb):
                result = await executor.execute(retry_cmd)

        # EH-1 (E1/E2): a verifiable action that still had no effect, or a named
        # target nothing could resolve, becomes an honest CLARIFY instead of a
        # silently-recorded miss. The original action_str is reported as the
        # outcome (status != "ok") so route() persists and learns it as a
        # failure, while the clarification is spoken to the user.
        miss_status = result.get("status")
        if miss_status in ("verify_failed", "resolve_miss"):
            if miss_status == "resolve_miss" and target:
                msg = f"I couldn't find {target}. Could you say it another way?"
            elif target:
                msg = (f"I tried to {verb.lower()} {target}, but nothing seemed "
                       "to happen. Could you say it another way?")
            else:
                msg = ("I tried that, but nothing seemed to happen. Could you say "
                       "it another way?")
            await self.execute_action(f"CLARIFY {msg}", cmd, route_label=route_label)
            return {
                "status": miss_status,
                "action": verb,
                "spoke_clarification": True,
                "verification": result.get("verification"),
            }

        # Post-action suppression:
        #   CLARIFY  — long suppress covers TTS echo (already pre-suppressed too)
        #   All else — short cooldown prevents app-startup sounds / utterance
        #              tail from being transcribed as a second command.
        if verb == "CLARIFY":
            self._state.set_pending_clarification(target or "unclear command")
            # A2UI (token-free): if this clarification is an enumerable shape,
            # push a tappable choice card. Free-form CLARIFYs return no template
            # and stay voice-only. Best-effort — never blocks the voice path.
            self.maybe_emit_clarify_surface(target)
            if whisper is not None:
                whisper.suppress(1.5)
                whisper.set_awaiting_clarification(True)
                log.debug("Post-CLARIFY mic suppressed 1.5s; wake-phrase bypassed")
        else:
            self._state.set_pending_clarification(None)
            self.clear_clarify_surface()   # answer arrived (tap or voice) → dismiss card
            # Remember successful OPEN targets for the "what would you like to
            # open?" choice card (most-recent first, deduped, capped).
            if verb == "OPEN" and target and result.get("status") == "ok":
                self.record_open_target(target)
            if whisper is not None:
                whisper.set_awaiting_clarification(False)
                if result.get("status") == "ok":
                    whisper.suppress(0.8)
                    log.debug("Post-action mic cooldown 0.8s (verb=%s)", verb)

        # Record the resolved turn so the NEXT utterance can resolve anaphora
        # ("do that again", "click it") and the prompt can carry a last-action
        # hint. Best-effort — never let bookkeeping fail a command.
        try:
            self._conversation().record(
                command_text=cmd.text,
                verb=verb,
                target=target or "",
                coords=grounded_coords,
                success=result.get("status") == "ok",
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("ConversationState.record failed: %s", exc)

        return result

    async def ground_target(self, target: str) -> Optional[tuple[int, int]]:
        """Screenshot the desktop and ask VisionGrounder to resolve the target."""
        try:
            # mcp_server/ is already in sys.path via command_executor import
            from tools import screen as _screen
            screenshot_result = await asyncio.to_thread(_screen.screenshot)
            screenshot_b64: str = screenshot_result.get("image_base64", "")
            if not screenshot_b64:
                return None
            result = await asyncio.to_thread(
                self._grounder().ground, target, screenshot_b64
            )
            if result is None:
                return None
            return (result.x, result.y)
        except Exception as exc:
            log.warning("ground_target failed for %r: %s", target, exc)
            return None

    @staticmethod
    def parse_action(action_str: str) -> tuple[str, str]:
        """Split a raw LLM action string into (verb, target).

        verb is upper-cased; target is the remainder (may be empty). Callers
        validate verb against _VALID_COMMAND_VERBS before dispatch. Tolerates a
        single leading "Action:"/"ACTION:" label some models prepend, but does
        not otherwise hunt for a verb mid-string — an accessibility tool should
        re-prompt rather than guess at a destructive action.
        """
        text = action_str.strip()
        # Strip one optional leading label, e.g. "Action: CLICK button".
        low = text.lower()
        for label in ("action:", "command:"):
            if low.startswith(label):
                text = text[len(label):].strip()
                break
        parts = text.split(None, 1)
        if not parts:
            return "", ""
        verb = parts[0].upper().rstrip(":")
        target = parts[1].strip() if len(parts) > 1 else ""
        return verb, target

    @staticmethod
    def parse_params(verb: str, target: str, original: Command) -> dict:
        """Extract verb-specific params from the LLM target string."""
        params: dict = {}

        if verb == "SCROLL":
            words = target.lower().split()
            direction = "down"
            for w in words:
                if w in ("up", "down", "left", "right"):
                    direction = w
                    break
            amount = 3
            for w in words:
                try:
                    amount = int(w)
                    break
                except ValueError:
                    pass
            params = {"direction": direction, "amount": amount}

        elif verb == "CLICK":
            # Carry through explicit touch coords (tilt-tap pins cursor x/y +
            # snap_nearest in FusionEngine.on_touch). Without this the coords are
            # dropped and the click falls back to re-searching UIA for the text.
            if "x" in original.params and "y" in original.params:
                params = {"x": original.params["x"], "y": original.params["y"]}
                if original.params.get("snap_nearest"):
                    params["snap_nearest"] = True
            elif original.gaze_coords:
                x, y = original.gaze_coords
                params = {"x": x, "y": y}

        elif verb == "TYPE":
            params = {"text": target}

        elif verb == "OPEN":
            params = {"target": target}

        elif verb == "HOTKEY":
            keys = [k.strip() for k in target.replace("+", " ").split() if k.strip()]
            params = {"keys": keys}

        elif verb == "CLARIFY":
            params = {"message": target}

        return params

    # ---------------------------------------------------------------------- #
    # Click-target A2UI palette
    # ---------------------------------------------------------------------- #

    def maybe_emit_clarify_surface(self, message: Optional[str]) -> None:
        """Push a tappable A2UI choice card for enumerable CLARIFYs (token-free).

        No-op when no bridge is wired or the message isn't an enumerable shape.
        Never raises — the voice clarification path is authoritative.
        """
        bridge = self._bridge()
        if bridge is None or not message:
            return
        try:
            from core import a2ui
            surface = a2ui.template_for_clarify(
                message, recent_apps=self._state.get_recent_open_targets())
            if surface is None and a2ui.is_click_target_clarify(message):
                surface = self.build_click_target_surface()
            if surface is None:
                return
            self._state.set_active_clarify_surface_id(surface["surface_id"])
            asyncio.create_task(bridge.send_a2ui_surface(surface))
        except Exception as exc:
            log.debug("a2ui: clarify surface emit failed: %s", exc)

    @staticmethod
    def rank_click_targets(snapshot, cursor, limit: int = 8) -> list[tuple[str, int, int]]:
        """Filter + rank clickable targets into (name, cx, cy), nearest-first.

        Drops unnamed / tiny / oversized elements, dedups by name (keeping the
        nearest), and ranks by squared distance to the cursor (or reading order
        when no cursor is available). Pure + cursor-injectable for testing.
        """
        scored: list[tuple[int, str, int, int]] = []
        for t in snapshot:
            name = (getattr(t, "name", "") or "").strip()
            if not name:
                continue
            try:
                left, top, right, bottom = t.bounds
            except Exception:
                continue
            w, h = right - left, bottom - top
            if w < 8 or h < 8 or w > 1400 or h > 1000:
                continue
            cx, cy = (left + right) // 2, (top + bottom) // 2
            if cursor is not None:
                d = (cx - cursor[0]) ** 2 + (cy - cursor[1]) ** 2
            else:
                d = cy * 100000 + cx          # top-to-bottom, left-to-right
            scored.append((d, name, cx, cy))
        scored.sort(key=lambda z: z[0])
        seen: set[str] = set()
        ranked: list[tuple[str, int, int]] = []
        for _d, name, cx, cy in scored:
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            ranked.append((name, cx, cy))
            if len(ranked) >= limit:
                break
        return ranked

    def build_click_target_surface(self) -> Optional[dict]:
        """Build a tappable element palette from the live UI tree, or None.

        Flag-gated (DA_A2UI_CLICK_TARGETS=1) while we evaluate whether a ranked
        list beats voice. Logs the ranked names at INFO so they can be eyeballed.
        """
        target_cache = self._target_cache()
        if target_cache is None:
            return None
        if os.environ.get("DA_A2UI_CLICK_TARGETS", "0") != "1":
            return None
        try:
            snapshot = target_cache.snapshot()
        except Exception as exc:
            log.debug("a2ui: target_cache.snapshot failed: %s", exc)
            return None
        if not snapshot:
            return None
        cursor = None
        try:
            import pyautogui
            cursor = pyautogui.position()
        except Exception:
            cursor = None
        ranked = self.rank_click_targets(snapshot, cursor)
        if not ranked:
            return None
        log.info("a2ui: click-target palette (%d): %s",
                 len(ranked), [r[0] for r in ranked])
        try:
            from core import a2ui
            return a2ui.click_target_surface(ranked)
        except Exception as exc:
            log.debug("a2ui: click_target_surface build failed: %s", exc)
            return None

    def record_open_target(self, target: str) -> None:
        """Push an opened app/file to the front of the recent-targets buffer
        (deduped, capped at 8) so the 'what would you like to open?' card can
        offer the user's actual apps."""
        t = target.strip()
        if not t:
            return
        lower = t.lower()
        current = self._state.get_recent_open_targets()
        updated = [t] + [x for x in current if x.lower() != lower]
        if len(updated) > 8:
            updated = updated[:8]
        self._state.set_recent_open_targets(updated)

    def clear_clarify_surface(self) -> None:
        """Dismiss a live CLARIFY card once the clarification has resolved."""
        sid = self._state.get_active_clarify_surface_id()
        bridge = self._bridge()
        if not sid or bridge is None:
            self._state.set_active_clarify_surface_id(None)
            return
        self._state.set_active_clarify_surface_id(None)
        try:
            asyncio.create_task(bridge.clear_a2ui_surface(sid))
        except Exception as exc:
            log.debug("a2ui: clarify surface clear failed: %s", exc)
