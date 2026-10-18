from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import core.atomic_file as atomic_file
from core.atomic_file import atomic_write_text
from inference.step_executor import write_file


def test_atomic_write_replaces_complete_content_in_same_directory(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "value.txt"
    target.parent.mkdir()
    target.write_text("old", encoding="utf-8")
    source_parent = None
    real_replace = os.replace

    def capture_replace(source, destination):
        nonlocal source_parent
        source_parent = Path(source).parent
        real_replace(source, destination)

    monkeypatch.setattr(atomic_file.os, "replace", capture_replace)
    count = atomic_write_text(target, "new complete value")

    assert count == len("new complete value".encode("utf-8"))
    assert target.read_text(encoding="utf-8") == "new complete value"
    assert source_parent == target.parent
    assert not list(target.parent.glob(f".{target.name}.*.tmp"))


def test_atomic_interruption_leaves_old_content_and_cleans_temp(tmp_path, monkeypatch):
    target = tmp_path / "value.txt"
    target.write_text("old", encoding="utf-8")

    def interrupt(_source, _destination):
        raise OSError("replace interrupted")

    monkeypatch.setattr(atomic_file.os, "replace", interrupt)
    with pytest.raises(OSError, match="interrupted"):
        atomic_write_text(target, "partial must never appear")

    assert target.read_text(encoding="utf-8") == "old"
    assert not list(tmp_path.glob(f".{target.name}.*.tmp"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission-bit assertion")
def test_atomic_write_preserves_existing_permission_bits(tmp_path):
    target = tmp_path / "mode.txt"
    target.write_text("old", encoding="utf-8")
    target.chmod(0o640)
    atomic_write_text(target, "new")
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_step_executor_write_file_uses_atomic_helper(tmp_path, monkeypatch):
    target = tmp_path / "step.txt"
    calls = []

    def fake_atomic(path, content):
        calls.append((Path(path), content))
        return len(content.encode("utf-8"))

    monkeypatch.setattr("inference.step_executor.atomic_write_text", fake_atomic)
    result = write_file(str(target), "body")
    assert calls == [(target, "body")]
    assert "4 bytes" in result
