"""GAP-2 — Vibe Diff plain-English summary in the approval gate.

For the configured high-impact tools, `_vibe_summary` asks a local LLM what the
action DOES and returns a one-sentence spoken prompt. It must fail open: a tool
not on the list, an Ollama timeout, or a malformed reply all yield None so the
caller falls back to the static description and the gate is never blocked.

Run:
    python -m pytest tests/test_vibe_diff.py -q
"""

from __future__ import annotations

import io
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import approval_hook

_CONFIG = {"vibe_diff_tools": ["Bash", "PowerShell"], "vibe_diff_model": "llama3.1:8b"}


class _FakeResp:
    def __init__(self, payload: dict):
        self._data = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_summary_returned_for_listed_tool(monkeypatch):
    def fake_urlopen(req, timeout=None):
        return _FakeResp({"response": "This deletes your build folder."})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    out = approval_hook._vibe_summary("Bash", {"command": "rm -rf ./build"}, _CONFIG)
    # Returns the plain-English effect only; the caller prepends it to the static
    # "Approve running rm?" prompt so the exe identity is never hidden.
    assert out == "This deletes your build folder."


def test_unlisted_tool_returns_none(monkeypatch):
    # Should not even attempt a network call.
    def boom(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("urlopen should not be called for an unlisted tool")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert approval_hook._vibe_summary("Read", {"file_path": "x"}, _CONFIG) is None


def test_timeout_falls_back_to_none(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise TimeoutError("ollama slow")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert approval_hook._vibe_summary("Bash", {"command": "ls"}, _CONFIG) is None


def test_empty_response_returns_none(monkeypatch):
    def fake_urlopen(req, timeout=None):
        return _FakeResp({"response": "   "})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert approval_hook._vibe_summary("Bash", {"command": "ls"}, _CONFIG) is None


def test_feature_off_when_key_absent(monkeypatch):
    def boom(*a, **k):  # pragma: no cover
        raise AssertionError("urlopen should not be called when vibe_diff_tools absent")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert approval_hook._vibe_summary("Bash", {"command": "ls"}, {}) is None
