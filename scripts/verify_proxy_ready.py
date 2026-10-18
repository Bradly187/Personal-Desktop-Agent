"""Validate a proxy /ready response without printing credential material."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import string
import sys
from pathlib import Path


def _is_hex(value: str, length: int) -> bool:
    return len(value) == length and all(char in string.hexdigits for char in value)


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    token = os.environ.get("WSL_ACTION_PROXY_TOKEN", "")
    challenge = os.environ.get("WSL_PROXY_CHALLENGE", "")
    if not token or not _is_hex(challenge, 32):
        return 1
    try:
        data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return 1
    instance_id = data.get("instance_id", "")
    proof = data.get("proof", "")
    if (
        data.get("ready") is not True
        or not isinstance(instance_id, str)
        or not _is_hex(instance_id, 32)
        or not isinstance(proof, str)
    ):
        return 1
    expected = hmac.new(
        token.encode("utf-8"),
        (challenge + instance_id).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(proof, expected):
        return 1
    print(instance_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
