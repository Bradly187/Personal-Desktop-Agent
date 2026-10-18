"""Same-directory atomic file replacement helpers."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> int:
    """Flush complete text to a sibling temp file, then atomically replace path.

    The caller must authorize ``path`` before invoking this helper. Existing
    practical permission bits are copied to the temporary file before replace.
    On every pre-replace failure the old destination is untouched and the temp
    file is removed.
    """
    data = content.encode(encoding)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode: int | None = None
    try:
        existing_mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        pass

    fd, raw_temp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temp_path = Path(raw_temp)
    replaced = False
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if existing_mode is not None:
            os.chmod(temp_path, existing_mode)
        os.replace(temp_path, path)
        replaced = True
        return len(data)
    finally:
        if not replaced:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
