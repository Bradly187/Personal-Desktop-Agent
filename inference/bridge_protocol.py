"""inference.bridge_protocol — shared file contract for the VS Code Desktop Agent Bridge.

The TypeScript extension (``desktop-agent-bridge/``) runs a WebSocket server on
localhost:8767; the Python backend (``inference.bridge_client.BridgeClient``)
connects in as a client. Three small JSON/text files under
``~/.claude/desktop_agent_bridge/`` carry the out-of-band state the WebSocket
request/response channel can't (auth + the user-driven model override):

    token              — shared secret. Whoever starts first generates it
                         (random hex, 0600). The extension requires it as a
                         ``?token=`` query param on the WS handshake; the client
                         appends it. Generate-if-missing on BOTH sides so they
                         converge; never overwrite an existing token.

    roster.json        — written by the BACKEND (ModelRouter) on startup:
                         {"models": [<name>, ...], "updated": <unix_ts>}. The
                         extension reads it to populate the model-picker
                         QuickPick.

    override.json      — written by the EXTENSION when the user picks a model:
                         {"model": "<name>"}  → pin that model for ALL dev-agent
                                                domains (code/math/plan/general;
                                                vision only if image-capable).
                         {"model": null} / absent → Auto (domain routing).
                         Read by ModelRouter.select_profile().

The extension mirrors these exact paths and JSON shapes in TypeScript — keep the
two sides in sync (AGENTS.md #3). This module is import-safe with no heavy deps
so ``model_router`` / ``bridge_client`` / ``main`` can all share it.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ── Shared file locations (mirrored in desktop-agent-bridge/src/extension.ts) ──
BRIDGE_DIR = Path.home() / ".claude" / "desktop_agent_bridge"
TOKEN_FILE = BRIDGE_DIR / "token"
ROSTER_FILE = BRIDGE_DIR / "roster.json"
OVERRIDE_FILE = BRIDGE_DIR / "override.json"

# Domain that must never be overridden — the accessibility command classifier is
# latency- and format-critical (verb-first, 32-token budget). The picker is a
# *dev-agent* override only.
COMMAND_DOMAIN = "command"


def _ensure_dir() -> None:
    try:
        BRIDGE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # pragma: no cover - filesystem edge
        log.debug("bridge_protocol: cannot create %s: %s", BRIDGE_DIR, exc)


def ensure_token() -> Optional[str]:
    """Return the shared bridge token, generating it (0600) if absent.

    Never overwrites an existing token, so the backend and the extension
    converge on whichever value was written first. Returns None only if the
    directory is unwritable (auth then fails closed on the extension side).
    """
    _ensure_dir()
    try:
        if TOKEN_FILE.exists():
            tok = TOKEN_FILE.read_text(encoding="utf-8").strip()
            if tok:
                return tok
    except OSError as exc:
        log.debug("bridge_protocol: cannot read token: %s", exc)

    token = secrets.token_hex(32)
    try:
        # Atomic create: O_CREAT|O_EXCL loses the race gracefully — if another
        # process created it first, read theirs back instead of clobbering.
        fd = os.open(str(TOKEN_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, token.encode("utf-8"))
        finally:
            os.close(fd)
        log.info("bridge_protocol: generated new bridge token at %s", TOKEN_FILE)
        return token
    except FileExistsError:
        try:
            return TOKEN_FILE.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
    except OSError as exc:
        log.warning("bridge_protocol: cannot write token: %s", exc)
        return None


def read_token() -> Optional[str]:
    """Return the existing token without generating one (None if absent)."""
    try:
        if TOKEN_FILE.exists():
            return TOKEN_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        pass
    return None


def write_roster(models: list[str]) -> None:
    """Publish the model roster for the extension's picker (best-effort)."""
    _ensure_dir()
    # De-dup, preserve first-seen order.
    seen: dict[str, None] = {}
    for m in models:
        if m and m not in seen:
            seen[m] = None
    payload = {"models": list(seen.keys()), "updated": int(time.time())}
    try:
        tmp = ROSTER_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(str(tmp), str(ROSTER_FILE))  # atomic — picker never sees a half-write
    except OSError as exc:
        log.debug("bridge_protocol: cannot write roster: %s", exc)


def read_override() -> Optional[str]:
    """Return the user-pinned model name, or None for Auto (domain routing).

    Tolerant: a missing file, malformed JSON, or an explicit ``null`` all mean
    "no override" so a corrupt write can never wedge routing.
    """
    try:
        if not OVERRIDE_FILE.exists():
            return None
        data = json.loads(OVERRIDE_FILE.read_text(encoding="utf-8"))
        model = data.get("model") if isinstance(data, dict) else None
        if isinstance(model, str) and model.strip():
            return model.strip()
    except (OSError, ValueError):
        pass
    return None


def write_override(model: Optional[str]) -> None:
    """Set (or clear, with ``None``) the pinned model. Used by tests/tools."""
    _ensure_dir()
    try:
        OVERRIDE_FILE.write_text(json.dumps({"model": model}), encoding="utf-8")
    except OSError as exc:
        log.debug("bridge_protocol: cannot write override: %s", exc)
