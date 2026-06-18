"""GAP-7 — sandbox network isolation for purely-computational commands.

A compute-only command (file-write + test-run, no network op) must run with the
network unshared; a curated package/VCS op (pip install) gets network. Verifies
command_needs_network + build_sandbox_argv.

Run:
    python -m pytest tests/test_sandbox_netiso.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from inference.sandbox import command_needs_network, build_sandbox_argv


def test_compute_only_is_offline():
    assert command_needs_network("pytest -q") is False
    assert command_needs_network("python build.py && pytest") is False
    bwrap = build_sandbox_argv("bwrap", "pytest -q", "/proj", allow_network=False)
    assert "--unshare-net" in bwrap
    firejail = build_sandbox_argv("firejail", "pytest -q", "/proj", allow_network=False)
    assert "--net=none" in firejail


def test_pip_install_gets_network():
    assert command_needs_network("pip install requests") is True
    bwrap = build_sandbox_argv("bwrap", "pip install requests", "/proj", allow_network=True)
    assert "--unshare-net" not in bwrap


def test_git_push_gets_network_but_status_does_not():
    assert command_needs_network("git push origin main") is True
    assert command_needs_network("git status") is False
