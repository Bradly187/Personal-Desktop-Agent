"""google_pim_auth — one-time OAuth setup for the Google PIM skill.

Setup (no environment variable needed):
    pip install google-api-python-client google-auth-oauthlib google-auth-httplib2
    # save your OAuth desktop client JSON from Google Cloud Console as:
    #   ~/.claude/skills/credentials/google_pim/client_secret.json
    python -m skills.servers.google_pim_auth

(GOOGLE_OAUTH_CLIENT_SECRETS still overrides the default location.)

Opens a browser for consent, stores the refresh token at
~/.claude/skills/credentials/google_pim/token.json (0600), and AUTO-ENABLES the
skill via the ~/.claude/skills/enabled.json override — no manifest edit, no
agent restart needed when triggered by the voice "connect Google" command.
Also runnable any time the token expires or is revoked ("reconnect Google").
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Runnable both as a module (python -m skills.servers.google_pim_auth) and as a
# script from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from skills.google_setup import client_secret_path, set_skill_enabled, DEFAULT_CLIENT_SECRET
from skills.servers.google_pim_server import _SCOPES, _TOKEN_PATH


def main() -> None:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        raise SystemExit(
            "Google libraries missing. Run: pip install "
            "google-api-python-client google-auth-oauthlib google-auth-httplib2"
        )

    secrets = client_secret_path()
    if secrets is None:
        raise SystemExit(
            "No OAuth client file found. In Google Cloud Console create an "
            "OAuth *desktop* client and save the JSON as "
            f"{DEFAULT_CLIENT_SECRET} (or set GOOGLE_OAUTH_CLIENT_SECRETS)."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(secrets), _SCOPES)
    creds = flow.run_local_server(port=0)
    _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    try:
        os.chmod(_TOKEN_PATH, 0o600)
    except OSError:
        pass  # best-effort on Windows

    # Auto-enable: user state in ~/.claude, not a repo-manifest edit.
    set_skill_enabled("google_pim", True)
    print(f"Saved Google token to {_TOKEN_PATH}")
    print("google_pim skill enabled. If the agent is running, say "
          "'connect Google' to hot-start it; otherwise it starts next launch.")


if __name__ == "__main__":
    main()
