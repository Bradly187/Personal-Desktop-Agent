"""Tests for core.crash_marker — unclean-exit detection across restarts."""

import json
import os

import pytest

from core import crash_marker


@pytest.fixture
def marker(tmp_path, monkeypatch):
    path = tmp_path / "logs" / "agent.running"
    monkeypatch.setattr(crash_marker, "_MARKER", path)
    return path


class TestCheckAndMark:
    def test_first_start_is_clean(self, marker):
        assert crash_marker.check_and_mark() is False
        assert marker.exists()

    def test_marker_contains_pid_and_timestamp(self, marker):
        crash_marker.check_and_mark()
        info = json.loads(marker.read_text(encoding="utf-8"))
        assert info["pid"] == os.getpid()
        assert info["started"]

    def test_existing_marker_means_unclean_exit(self, marker):
        crash_marker.check_and_mark()
        # No clear() — simulates a crash, then the next startup:
        assert crash_marker.check_and_mark() is True

    def test_corrupt_marker_still_detected(self, marker):
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("not json{{{", encoding="utf-8")
        assert crash_marker.check_and_mark() is True
        # And it was rewritten with valid content for the current run
        assert json.loads(marker.read_text(encoding="utf-8"))["pid"] == os.getpid()

    def test_creates_logs_dir_when_missing(self, marker):
        assert not marker.parent.exists()
        crash_marker.check_and_mark()
        assert marker.exists()

    def test_unwritable_marker_never_raises(self, tmp_path, monkeypatch):
        # Parent "directory" is actually a file → mkdir/write_text fail.
        blocker = tmp_path / "logs"
        blocker.write_text("a file, not a dir")
        monkeypatch.setattr(crash_marker, "_MARKER", blocker / "agent.running")
        assert crash_marker.check_and_mark() is False  # degrades, no exception


class TestClear:
    def test_clear_removes_marker(self, marker):
        crash_marker.check_and_mark()
        crash_marker.clear()
        assert not marker.exists()
        # Next start is clean again
        assert crash_marker.check_and_mark() is False

    def test_clear_without_marker_is_noop(self, marker):
        crash_marker.clear()  # nothing to remove, must not raise
        assert not marker.exists()


class TestFullCycle:
    def test_graceful_cycle_never_reports_crash(self, marker):
        for _ in range(3):
            assert crash_marker.check_and_mark() is False
            crash_marker.clear()

    def test_crash_then_graceful_then_clean(self, marker):
        assert crash_marker.check_and_mark() is False   # run 1 ... crashes
        assert crash_marker.check_and_mark() is True    # run 2 detects it
        crash_marker.clear()                            # run 2 shuts down cleanly
        assert crash_marker.check_and_mark() is False   # run 3 is clean
