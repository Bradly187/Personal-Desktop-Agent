"""Regression test for HybridCoordinator._run_cloud secret-scrub path.

The migration's _run_cloud rebuilt the Command with a nonexistent `_gaze_coords`
kwarg, so every cloud call that followed a ContentFilter redaction raised
TypeError. The existing test missed it because its payload tripped Gate 0
(forced local) and never reached _run_cloud. This drives _run_cloud directly.
"""

import asyncio

from core.command_executor import Command
from core.hybrid_coordinator import HybridCoordinator


class _Filter:
    async def scrub(self, text):
        return "REDACTED", [{"type": "secret"}]  # non-empty findings


class _Cloud:
    def __init__(self):
        self.received = None

    async def infer(self, cmd):
        self.received = cmd
        return "CLICK ok"


def test_run_cloud_scrub_path_rebuilds_command_without_typeerror():
    coord = HybridCoordinator()
    coord._content_filter = _Filter()
    coord._cloud = _Cloud()

    cmd = Command(text="my api key is sk-abc123", action="CLARIFY",
                  source="voice", gaze_coords=(10, 20))
    out = asyncio.run(coord._run_cloud(cmd))  # must not raise TypeError

    assert out == "CLICK ok"
    assert coord._cloud.received is not None
    assert coord._cloud.received.text == "REDACTED"       # scrubbed text sent
    assert coord._cloud.received.action == "CLARIFY"      # required field preserved
    assert coord._cloud.received.gaze_coords == (10, 20)  # preserved across rebuild


def test_run_cloud_without_findings_passes_through():
    class _NoFindings:
        async def scrub(self, text):
            return text, []  # nothing to redact

    coord = HybridCoordinator()
    coord._content_filter = _NoFindings()
    coord._cloud = _Cloud()

    cmd = Command(text="open the browser", action="OPEN", source="voice")
    out = asyncio.run(coord._run_cloud(cmd))
    assert out == "CLICK ok"
    assert coord._cloud.received.text == "open the browser"  # untouched
