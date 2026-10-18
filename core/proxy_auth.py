"""Replay-resistant authentication primitives for the loopback action proxy."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import Mapping, MutableMapping
from typing import Any

TOKEN_ENV = "WSL_ACTION_PROXY_TOKEN"
INSTANCE_ENV = "WSL_ACTION_PROXY_INSTANCE"
_TRANSIENT_ENV = frozenset({
    TOKEN_ENV,
    INSTANCE_ENV,
    "WSL_PROXY_CHALLENGE",
    "WSL_PROXY_PROOF",
})

INSTANCE_HEX_LENGTH = 32
NONCE_HEX_LENGTH = 32
READINESS_CHALLENGE_HEX_LENGTH = 32
AUTH_WINDOW_SECONDS = 30
NONCE_CACHE_LIMIT = 4096

HEADER_INSTANCE = "X-Agent-Instance"
HEADER_TIMESTAMP = "X-Agent-Timestamp"
HEADER_NONCE = "X-Agent-Nonce"
HEADER_SIGNATURE = "X-Agent-Signature"


def capture_proxy_credentials(
    environ: MutableMapping[str, str] | None = None,
) -> tuple[str, str]:
    """Capture bootstrap credentials once and remove them from child inheritance."""
    target = os.environ if environ is None else environ
    token = target.pop(TOKEN_ENV, "").strip()
    instance_id = target.pop(INSTANCE_ENV, "").strip()
    target.pop("WSL_PROXY_CHALLENGE", None)
    target.pop("WSL_PROXY_PROOF", None)
    return token, instance_id


def sanitized_child_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a child environment with all proxy authentication material removed."""
    env = dict(os.environ if base is None else base)
    for name in _TRANSIENT_ENV:
        env.pop(name, None)
    return env


def canonical_json_bytes(payload: Any) -> bytes:
    """Serialize a request body deterministically for hashing and transmission."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def body_sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _signature_message(
    method: str,
    path: str,
    body_hash: str,
    timestamp: str,
    nonce: str,
    instance_id: str,
) -> bytes:
    return "\n".join((
        method.upper(),
        path,
        body_hash,
        timestamp,
        nonce,
        instance_id,
    )).encode("utf-8")


def request_signature(
    token: str,
    *,
    method: str,
    path: str,
    body_hash: str,
    timestamp: str,
    nonce: str,
    instance_id: str,
) -> str:
    return hmac.new(
        token.encode("utf-8"),
        _signature_message(method, path, body_hash, timestamp, nonce, instance_id),
        hashlib.sha256,
    ).hexdigest()


def readiness_proof(token: str, challenge: str, instance_id: str) -> str:
    """Bind readiness to both the fresh challenge and this server process."""
    return hmac.new(
        token.encode("utf-8"),
        (challenge + instance_id).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
