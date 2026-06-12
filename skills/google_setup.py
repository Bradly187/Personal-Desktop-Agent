"""skills/google_setup.py — Google PIM auth lifecycle helpers.

Shared by the one-time auth script (google_pim_auth), the SkillRegistry
(enabled-state overrides), and the coordinator's voice "connect Google" path.

Design notes:
  - The OAuth client secret lives at a DEFAULT location
    (~/.claude/skills/credentials/google_pim/client_secret.json) so no
    environment variable is needed; GOOGLE_OAUTH_CLIENT_SECRETS still wins.
  - Skill enabled-state is USER state, not repo state: a successful auth flips
    ~/.claude/skills/enabled.json {"google_pim": true} instead of editing the
    checked-in manifest. The registry merges this override at load.
  - The consent flow runs as a SUBPROCESS (python -m skills.servers.
    google_pim_auth) so the agent's event loop never blocks on the browser.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_CRED_DIR = Path.home() / ".claude" / "skills" / "credentials" / "google_pim"
DEFAULT_CLIENT_SECRET = _CRED_DIR / "client_secret.json"
TOKEN_PATH = _CRED_DIR / "token.json"
ENABLED_OVERRIDES_PATH = Path.home() / ".claude" / "skills" / "enabled.json"

_AUTH_FLOW_TIMEOUT_S = 300.0   # browser consent can take a while


def google_libs_available() -> bool:
    try:
        import googleapiclient        # noqa: F401
        import google_auth_oauthlib   # noqa: F401
        return True
    except ImportError:
        return False


def client_secret_path() -> Optional[Path]:
    """The OAuth client secret: env var first, then the default location."""
    env = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRETS")
    if env and Path(env).is_file():
        return Path(env)
    if DEFAULT_CLIENT_SECRET.is_file():
        return DEFAULT_CLIENT_SECRET
    return None


def has_token() -> bool:
    return TOKEN_PATH.is_file()


# ---------------------------------------------------------------------------
# Enabled-state overrides (user state in ~/.claude, not repo manifests)
# ---------------------------------------------------------------------------

def load_enabled_overrides(path: "Path | None" = None) -> dict:
    p = path or ENABLED_OVERRIDES_PATH
    try:
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): bool(v) for k, v in data.items()}
    except (OSError, ValueError) as exc:
        log.warning("google_setup: bad enabled-overrides file %s: %s", p, exc)
    return {}


def set_skill_enabled(skill_id: str, enabled: bool,
                      path: "Path | None" = None) -> None:
    """Persist a per-skill enabled override (atomic replace)."""
    p = path or ENABLED_OVERRIDES_PATH
    overrides = load_enabled_overrides(p)
    overrides[skill_id] = bool(enabled)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(overrides, indent=2), encoding="utf-8")
    os.replace(tmp, p)


# ---------------------------------------------------------------------------
# Setup-state diagnosis + the consent flow
# ---------------------------------------------------------------------------

def setup_blocker() -> Optional[str]:
    """A spoken-friendly description of what blocks the auth flow, or None."""
    if not google_libs_available():
        return ("The Google libraries aren't installed. In a terminal, run: "
                "pip install google-api-python-client google-auth-oauthlib "
                "google-auth-httplib2 — then say 'connect Google' again.")
    if client_secret_path() is None:
        return ("I need a Google OAuth client file first. In Google Cloud "
                "Console, create an OAuth desktop client and save the JSON as "
                f"{DEFAULT_CLIENT_SECRET} — then say 'connect Google' again.")
    return None


async def run_auth_flow(timeout_s: float = _AUTH_FLOW_TIMEOUT_S) -> tuple[bool, str]:
    """Run the consent flow as a subprocess. Returns (success, message).

    The subprocess opens the user's browser; success means the refresh token
    was written to TOKEN_PATH and the skill was auto-enabled by the script.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "skills.servers.google_pim_auth",
            cwd=str(Path(__file__).resolve().parents[1]),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            return False, "Google sign-in timed out — say 'connect Google' to try again."
    except Exception as exc:
        return False, f"Couldn't start the Google sign-in flow: {exc}"

    if proc.returncode == 0 and has_token():
        return True, "Google connected — Gmail and Calendar are ready."
    tail = (out or b"").decode(errors="replace").strip().splitlines()
    detail = tail[-1] if tail else f"exit code {proc.returncode}"
    return False, f"Google sign-in didn't complete: {detail}"
