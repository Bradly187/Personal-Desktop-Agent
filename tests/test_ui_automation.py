"""Tests for UIAutomationProvider — Sprint 6 Win32 UIA structured access."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# UIElement
# ---------------------------------------------------------------------------

class TestUIElement:
    def test_center_midpoint(self):
        from desktop.ui_automation import UIElement
        el = UIElement(name="btn", role="Button", bounds=(100, 200, 300, 400))
        assert el.center() == (200, 300)

    def test_width_and_height(self):
        from desktop.ui_automation import UIElement
        el = UIElement(name="btn", role="Button", bounds=(10, 20, 110, 70))
        assert el.width() == 100
        assert el.height() == 50

    def test_center_integer_division(self):
        from desktop.ui_automation import UIElement
        el = UIElement(name="btn", role="Button", bounds=(0, 0, 101, 51))
        cx, cy = el.center()
        assert cx == 50
        assert cy == 25


# ---------------------------------------------------------------------------
# _detect_app
# ---------------------------------------------------------------------------

class TestDetectApp:
    def test_vscode(self):
        from desktop.ui_automation import _detect_app
        assert _detect_app("Code.exe") == "VS Code"

    def test_chrome(self):
        from desktop.ui_automation import _detect_app
        assert _detect_app("chrome.exe") == "Chrome"

    def test_kiro(self):
        from desktop.ui_automation import _detect_app
        assert _detect_app("Kiro.exe") == "Kiro IDE"

    def test_windows_terminal(self):
        from desktop.ui_automation import _detect_app
        assert _detect_app("WindowsTerminal.exe") == "Windows Terminal"

    def test_unknown_returns_none(self):
        from desktop.ui_automation import _detect_app
        assert _detect_app("mspaint.exe") is None

    def test_case_insensitive_and_strips_exe(self):
        from desktop.ui_automation import _detect_app
        assert _detect_app("NOTEPAD.EXE") == "Notepad"


# ---------------------------------------------------------------------------
# _score — pure Python, no Win32 required
# ---------------------------------------------------------------------------

class TestScore:
    def setup_method(self):
        from desktop.ui_automation import UIAutomationProvider
        self.p = UIAutomationProvider()

    def test_exact_name_match(self):
        assert self.p._score("Submit", "", 50000, "submit") == pytest.approx(1.0)

    def test_exact_value_match(self):
        assert self.p._score("", "submit", 50000, "submit") == pytest.approx(1.0)

    def test_target_contained_in_name(self):
        # "submit" is in "submit button"
        assert self.p._score("Submit Button", "", 50000, "submit") == pytest.approx(0.85)

    def test_all_target_words_in_name_non_contiguous(self):
        # "file dialog" is NOT a substring of "File Open Dialog", but both words present
        assert self.p._score("File Open Dialog", "", 50000, "file dialog") == pytest.approx(0.80)

    def test_partial_word_overlap(self):
        # 2 of 3 target words present in name → 2/3 ≥ 0.6 → 0.65
        assert self.p._score("Save File Now", "", 50000, "save file dialog") == pytest.approx(0.65)

    def test_value_match_fallback(self):
        # name doesn't match; value *contains* target (not exact) → 0.60
        assert self.p._score("input_field", "username@example.com", 50007, "username") == pytest.approx(0.60)

    def test_no_match_returns_zero(self):
        assert self.p._score("Toolbar", "", 50000, "submit button") == pytest.approx(0.0)

    def test_empty_name_and_value_returns_zero(self):
        assert self.p._score("", "", 50000, "anything") == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# UIAutomationProvider — availability, cache, and error paths
# ---------------------------------------------------------------------------

class TestUIAutomationProvider:
    def test_is_available_false_when_com_unavailable(self):
        from desktop.ui_automation import UIAutomationProvider
        p = UIAutomationProvider()
        with patch.object(p, "_get_uia", return_value=None):
            assert p.is_available() is False

    def test_is_available_true_when_com_ready(self):
        from desktop.ui_automation import UIAutomationProvider
        p = UIAutomationProvider()
        with patch.object(p, "_get_uia", return_value=MagicMock()):
            assert p.is_available() is True

    def test_find_returns_none_when_unavailable(self):
        from desktop.ui_automation import UIAutomationProvider
        p = UIAutomationProvider()
        with patch.object(p, "_get_uia", return_value=None):
            assert p.find("button") is None

    def test_find_cache_hit_skips_uia(self):
        from desktop.ui_automation import UIAutomationProvider, UIElement
        p = UIAutomationProvider()
        cached = UIElement("Submit", "Button", (0, 0, 100, 50))
        p._cache[("submit", "")] = (cached, time.monotonic() + 10.0)

        # _get_uia must not be called on a cache hit
        with patch.object(p, "_get_uia", side_effect=AssertionError("must not call _get_uia")):
            result = p.find("Submit")
        assert result is cached

    def test_find_expired_cache_bypassed(self):
        from desktop.ui_automation import UIAutomationProvider, UIElement
        p = UIAutomationProvider()
        stale = UIElement("Old", "Button", (0, 0, 50, 50))
        p._cache[("old button", "")] = (stale, time.monotonic() - 1.0)  # expired

        with patch.object(p, "_get_uia", return_value=None):
            result = p.find("old button")
        assert result is None  # expired → fresh attempt → unavailable → None

    def test_find_search_exception_returns_none(self):
        from desktop.ui_automation import UIAutomationProvider
        p = UIAutomationProvider()
        with patch.object(p, "_get_uia", return_value=MagicMock()), \
             patch.object(p, "_search", side_effect=RuntimeError("COM crash")):
            assert p.find("broken") is None

    def test_find_caches_successful_result(self):
        from desktop.ui_automation import UIAutomationProvider, UIElement
        p = UIAutomationProvider()
        found = UIElement("Save", "Button", (10, 10, 90, 40))
        with patch.object(p, "_get_uia", return_value=MagicMock()), \
             patch.object(p, "_search", return_value=found):
            result = p.find("save")
        assert result is found
        assert ("save", "") in p._cache

    def test_list_clickable_returns_empty_when_unavailable(self):
        from desktop.ui_automation import UIAutomationProvider
        p = UIAutomationProvider()
        with patch.object(p, "_get_uia", return_value=None):
            assert p.list_clickable() == []

    def test_get_status_keys_present(self):
        from desktop.ui_automation import UIAutomationProvider
        p = UIAutomationProvider()
        status = p.get_status()
        assert "available" in status
        assert "cache_entries" in status
        assert "supported_apps" in status

    def test_get_status_cache_count(self):
        from desktop.ui_automation import UIAutomationProvider, UIElement
        p = UIAutomationProvider()
        p._cache[("btn", "")] = (UIElement("btn", "Button", (0, 0, 1, 1)), time.monotonic() + 5)
        assert p.get_status()["cache_entries"] == 1

    def test_role_name_known(self):
        from desktop.ui_automation import UIAutomationProvider
        assert UIAutomationProvider._role_name(50000) == "Button"
        assert UIAutomationProvider._role_name(50002) == "CheckBox"

    def test_role_name_unknown_formats_id(self):
        from desktop.ui_automation import UIAutomationProvider
        assert UIAutomationProvider._role_name(99999) == "UIA_99999"
