"""google_pim_auth — one-time OAuth setup for the Google PIM skill.

Run once, after installing the Google libraries and pointing
GOOGLE_OAUTH_CLIENT_SECRETS at your OAuth client_secret.json:

    pip install google-api-python-client google-auth-oauthlib google-auth-httplib2
    set GOOGLE_OAUTH_CLIENT_SECRETS=C:\\path\\to\\client_secret.json
    python -m skills.servers.google_pim_auth

Opens a browser for consent and stores the refresh token at
~/.claude/skills/credentials/google_pim/token.json (0600). After that, enable
the skill by setting "enabled": true in skills/manifests/google_pim.json.
"""

from __future__ import annotations

import os
from pathlib import Path

from skills.servers.google_pim_server import _SCOPES, _TOKEN_PATH


def main() -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow

    secrets = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRETS")
    if not secrets or not Path(secrets).exists():
        raise SystemExit(
            "Set GOOGLE_OAUTH_CLIENT_SECRETS to the path of your OAuth "
            "client_secret.json before running this."
        )
    flow = InstalledAppFlow.from_client_secrets_file(secrets, _SCOPES)
    creds = flow.run_local_server(port=0)
    _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    try:
        os.chmod(_TOKEN_PATH, 0o600)
    except OSError:
        pass  # best-effort on Windows
    print(f"Saved Google token to {_TOKEN_PATH}")
    print('Now set "enabled": true in skills/manifests/google_pim.json')


if __name__ == "__main__":
    main()
