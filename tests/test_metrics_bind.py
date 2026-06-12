"""H1 — /metrics and /trace must bind loopback, not 0.0.0.0.

/trace returns recent traces including command text. Binding 0.0.0.0 exposed it
to the whole LAN with no auth. This is a lightweight source-level guard that the
metrics endpoint binds 127.0.0.1 (and fails if anyone flips it back to 0.0.0.0).

Run:
    python -m pytest tests/test_metrics_bind.py -q
"""

from __future__ import annotations

from pathlib import Path

_MAIN = Path(__file__).parent.parent / "main.py"


def test_metrics_binds_loopback():
    src = _MAIN.read_text(encoding="utf-8")
    assert '_metrics_host = "127.0.0.1"' in src
    assert "_aio_web.TCPSite(_metrics_runner, _metrics_host, args.metrics_port)" in src


def test_metrics_not_bound_to_all_interfaces():
    src = _MAIN.read_text(encoding="utf-8")
    assert 'TCPSite(_metrics_runner, "0.0.0.0"' not in src
