"""GAP-7 — pip-install slopsquatting integrity check.

verify_pip_install blocks a `pip install` of a package that does not exist on
PyPI (a hallucinated name → 404), allows real packages, ignores non-pip
commands, and fails OPEN on a network error so offline dev is never blocked.

Run:
    python -m pytest tests/test_pip_integrity.py -q
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.goal_session import verify_pip_install, _extract_pip_packages


def test_extract_packages():
    assert _extract_pip_packages("pip install requests") == ["requests"]
    assert _extract_pip_packages("pip install requests==2.0 flask") == ["requests", "flask"]
    assert _extract_pip_packages("python -m pip install numpy") == ["numpy"]
    assert _extract_pip_packages("pip install -r requirements.txt") == []
    assert _extract_pip_packages("pip install ./localpkg") == []
    assert _extract_pip_packages("pip list") == []
    assert _extract_pip_packages("pytest -q") == []


class _OK:
    status = 200

    def read(self):
        return b"{}"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_real_package_allowed(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _OK())
    ok, reason = verify_pip_install("pip install requests")
    assert ok and reason == ""


def test_hallucinated_package_blocked(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    ok, reason = verify_pip_install("pip install reqests-totally-made-up-xyz")
    assert not ok and "not found on pypi" in reason.lower()


def test_network_error_fails_open(monkeypatch):
    def boom(req, timeout=None):
        raise TimeoutError("offline")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    ok, _ = verify_pip_install("pip install requests")
    assert ok  # fail OPEN — don't block offline dev


def test_non_pip_command_passes():
    ok, reason = verify_pip_install("pytest -q && ruff check")
    assert ok and reason == ""
