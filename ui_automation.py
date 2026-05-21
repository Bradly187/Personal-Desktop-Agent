"""ui_automation.py — Win32 UIAutomation structured element access.

Provides a faster, more reliable alternative to vision grounding and OCR
for applications that implement Windows accessibility (VS Code, Chrome,
Acrobat, Windows Terminal).

Fallback chain for CLICK with named target:
    1. UIAutomationProvider.find(target, app)  ← this module (structured)
    2. VisionGrounder.ground(target, screenshot) ← claude-sonnet-4-6 vision
    3. find_text_on_screen(target)               ← Tesseract OCR word-match
    4. Screen centre + CLARIFY

Usage (wired by HybridCoordinator._execute_action):
    provider = UIAutomationProvider()
    element  = await asyncio.to_thread(provider.find, "submit button", app_name)
    if element:
        coords = element.center()   # (x, y) pixels
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# UIElement dataclass
# ---------------------------------------------------------------------------

@dataclass
class UIElement:
    name:       str
    role:       str
    bounds:     tuple[int, int, int, int]   # (left, top, right, bottom) pixels
    is_enabled: bool = True
    value:      str  = ""

    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.bounds
        return ((left + right) // 2, (top + bottom) // 2)

    def width(self)  -> int: return self.bounds[2] - self.bounds[0]
    def height(self) -> int: return self.bounds[3] - self.bounds[1]


# ---------------------------------------------------------------------------
# Supported application detection
# ---------------------------------------------------------------------------

_SUPPORTED_APP_FRAGMENTS = {
    "code":      "VS Code",
    "cursor":    "Cursor IDE",
    "kiro":      "Kiro IDE",
    "chrome":    "Chrome",
    "msedge":    "Edge",
    "firefox":   "Firefox",
    "acrobat":   "Acrobat",
    "acrord":    "Acrobat Reader",
    "zotero":    "Zotero",
    "windowsterminal": "Windows Terminal",
    "wt":        "Windows Terminal",
    "notepad":   "Notepad",
    "explorer":  "File Explorer",
}

def _detect_app(exe_name: str) -> Optional[str]:
    """Map an exe name fragment to a friendly app name, or None if unsupported."""
    exe_lower = exe_name.lower().replace(".exe", "")
    for fragment, friendly in _SUPPORTED_APP_FRAGMENTS.items():
        if fragment in exe_lower:
            return friendly
    return None


# ---------------------------------------------------------------------------
# UIAutomationProvider
# ---------------------------------------------------------------------------

class UIAutomationProvider:
    """Find interactive UI elements in Windows apps via UIA COM API.

    Lazy-imports comtypes so the module loads cleanly even without the
    Windows SDK; degrades gracefully (returns None) when unavailable.
    """

    # Cache: (element_name_lower, exe_name) → (UIElement, expiry_mono)
    _CACHE_TTL_S = 1.0

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], tuple[UIElement, float]] = {}
        self._uia = None   # lazy-initialised COM object
        self._available: Optional[bool] = None

    def _get_uia(self):
        """Lazy-init the UIAutomation COM object. Returns None if unavailable."""
        if self._available is False:
            return None
        if self._uia is not None:
            return self._uia
        try:
            import comtypes.client
            import comtypes.gen.UIAutomationClient as UIA
            self._uia = comtypes.client.CreateObject(
                "{ff48dba4-60ef-4201-aa87-54103eef594e}",
                interface=UIA.IUIAutomation,
            )
            self._available = True
            log.info("UIAutomationProvider: COM initialised")
        except Exception as exc:
            log.warning("UIAutomationProvider: COM unavailable — %s", exc)
            self._available = False
        return self._uia

    # ── Public API ─────────────────────────────────────────────────────────

    def is_available(self) -> bool:
        return self._get_uia() is not None

    def find(
        self,
        target: str,
        app_name: str = "",
        timeout_s: float = 0.5,
    ) -> Optional[UIElement]:
        """Find a named interactive element in the active window.

        Args:
            target:    Human-readable element name, e.g. "submit button",
                       "File menu", "search box".
            app_name:  Optional app exe name hint for scoping the search.
            timeout_s: Max time to spend searching (blocking — call via to_thread).

        Returns:
            UIElement with center() coords, or None if not found.
        """
        cache_key = (target.lower().strip(), (app_name or "").lower())
        now = time.monotonic()
        if cache_key in self._cache:
            elem, expiry = self._cache[cache_key]
            if now < expiry:
                log.debug("UIAutomation cache hit: %r", target)
                return elem

        uia = self._get_uia()
        if uia is None:
            return None

        try:
            result = self._search(uia, target, timeout_s)
        except Exception as exc:
            log.warning("UIAutomationProvider.find failed: %s", exc)
            return None

        if result is not None:
            self._cache[cache_key] = (result, now + self._CACHE_TTL_S)

        return result

    def list_clickable(self, max_results: int = 50) -> list[UIElement]:
        """Return all clickable elements in the active window (for debugging)."""
        uia = self._get_uia()
        if uia is None:
            return []
        try:
            return self._collect_clickable(uia, max_results)
        except Exception as exc:
            log.warning("UIAutomationProvider.list_clickable failed: %s", exc)
            return []

    # ── Internal search ────────────────────────────────────────────────────

    def _search(self, uia, target: str, timeout_s: float) -> Optional[UIElement]:
        """Walk the UIA tree of the focused window looking for target."""
        try:
            import comtypes.gen.UIAutomationClient as UIA
        except ImportError:
            return None

        root = uia.GetFocusedElement()
        if root is None:
            root = uia.GetRootElement()

        # Walk up to the top-level window
        walker = uia.CreateTreeWalker(uia.ControlViewCondition)
        parent = root
        for _ in range(10):
            p = walker.GetParentElement(parent)
            if p is None:
                break
            parent = p

        target_lower = target.lower().strip()
        deadline = time.monotonic() + timeout_s

        # BFS through the element tree
        queue = [parent]
        best: Optional[UIElement] = None
        best_score = 0.0

        while queue and time.monotonic() < deadline:
            elem = queue.pop(0)
            try:
                name  = (elem.CurrentName  or "").strip()
                role  = elem.CurrentControlType
                bounds = elem.CurrentBoundingRectangle
                enabled = bool(elem.CurrentIsEnabled)
                value = (elem.CurrentValue if hasattr(elem, "CurrentValue") else "") or ""
            except Exception:
                continue

            # Score this element against the target
            score = self._score(name, value, role, target_lower)
            if score > best_score and score >= 0.5:
                left   = bounds.left
                top    = bounds.top
                right  = bounds.right
                bottom = bounds.bottom
                if right > left and bottom > top:  # valid bounds
                    best = UIElement(
                        name=name,
                        role=self._role_name(role),
                        bounds=(left, top, right, bottom),
                        is_enabled=enabled,
                        value=value,
                    )
                    best_score = score
                    if score >= 0.9:
                        break  # high-confidence match — stop early

            # Enqueue children
            try:
                child = walker.GetFirstChildElement(elem)
                while child is not None and time.monotonic() < deadline:
                    queue.append(child)
                    child = walker.GetNextSiblingElement(child)
            except Exception:
                pass

        return best

    def _score(self, name: str, value: str, role: int, target: str) -> float:
        """Score how well (name, value, role) matches the target string."""
        if not name and not value:
            return 0.0

        name_lower  = name.lower()
        value_lower = value.lower()

        # Exact match
        if name_lower == target or value_lower == target:
            return 1.0

        # Target contained in name
        if target in name_lower:
            return 0.85

        # Name words all appear in target or vice versa
        target_words = set(target.split())
        name_words   = set(name_lower.split())
        if target_words and name_words:
            overlap = len(target_words & name_words)
            if overlap == len(target_words):
                return 0.80
            if overlap / max(len(target_words), 1) >= 0.6:
                return 0.65

        # Value match
        if target in value_lower:
            return 0.60

        return 0.0

    def _collect_clickable(self, uia, max_results: int) -> list[UIElement]:
        """Return interactive elements from the focused window."""
        try:
            import comtypes.gen.UIAutomationClient as UIA
        except ImportError:
            return []

        CLICKABLE_ROLES = {
            50000,  # Button
            50003,  # CheckBox
            50004,  # ComboBox
            50007,  # Edit
            50008,  # Hyperlink
            50009,  # Image (if interactive)
            50011,  # ListItem
            50015,  # MenuItem
            50020,  # RadioButton
            50025,  # TabItem
        }

        root = uia.GetFocusedElement()
        walker = uia.CreateTreeWalker(uia.ControlViewCondition)

        results: list[UIElement] = []
        queue = [root]
        while queue and len(results) < max_results:
            elem = queue.pop(0)
            try:
                role   = elem.CurrentControlType
                name   = (elem.CurrentName or "").strip()
                bounds = elem.CurrentBoundingRectangle
                enabled = bool(elem.CurrentIsEnabled)
                if role in CLICKABLE_ROLES and name and enabled:
                    results.append(UIElement(
                        name=name,
                        role=self._role_name(role),
                        bounds=(bounds.left, bounds.top, bounds.right, bounds.bottom),
                        is_enabled=True,
                    ))
            except Exception:
                pass
            try:
                child = walker.GetFirstChildElement(elem)
                while child is not None:
                    queue.append(child)
                    child = walker.GetNextSiblingElement(child)
            except Exception:
                pass

        return results

    @staticmethod
    def _role_name(role_id: int) -> str:
        _ROLES = {
            50000: "Button", 50001: "Calendar", 50002: "CheckBox",
            50003: "ComboBox", 50004: "Edit", 50005: "Hyperlink",
            50006: "Image", 50007: "ListItem", 50008: "List",
            50009: "Menu", 50010: "MenuBar", 50011: "MenuItem",
            50012: "ProgressBar", 50013: "RadioButton", 50014: "ScrollBar",
            50015: "Slider", 50016: "Spinner", 50017: "StatusBar",
            50018: "Tab", 50019: "TabItem", 50020: "Text",
            50021: "ToolBar", 50022: "ToolTip", 50023: "Tree",
            50024: "TreeItem", 50025: "Custom", 50026: "Group",
            50027: "Thumb", 50028: "DataGrid", 50029: "DataItem",
            50030: "Document", 50031: "SplitButton", 50032: "Window",
            50033: "Pane", 50034: "Header", 50035: "HeaderItem",
            50036: "Table", 50037: "TitleBar", 50038: "Separator",
        }
        return _ROLES.get(role_id, f"UIA_{role_id}")

    def get_status(self) -> dict:
        avail = self._available
        return {
            "available": avail is True,
            "cache_entries": len(self._cache),
            "supported_apps": list(_SUPPORTED_APP_FRAGMENTS.values()),
        }
