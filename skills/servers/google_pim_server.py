"""google_pim_server — local stdio MCP server for Gmail + Google Calendar.

Owned locally (NOT a third-party MCP server) so the user's mail and OAuth token
never leave this machine. OAuth is set up ONCE via ``google_pim_auth.py``; this
server only loads/refreshes the stored token. The Google libraries are imported
lazily (inside the credential/service helpers) so this module — and its plain
logic functions — import and test fine without them installed.

Capabilities:
  list_next_event   (read)  — the next upcoming calendar event
  list_unread       (read)  — raw snippets of recent unread inbox mail
                              (the DevAgent summarises these on-device, AFTER the
                              taint check, for the "summarize my inbox" intent)
  send_reply        (send)  — send an email (gated + scrubbed by the DevAgent)
  create_event      (send)  — create a calendar event (gated)

Setup:
  pip install google-api-python-client google-auth-oauthlib google-auth-httplib2
  set GOOGLE_OAUTH_CLIENT_SECRETS=C:\\path\\to\\client_secret.json
  python -m skills.servers.google_pim_auth        # one-time browser consent
  # then flip skills/manifests/google_pim.json "enabled": true
"""

from __future__ import annotations

import base64
from email.mime.text import MIMEText
from pathlib import Path

from mcp.server.fastmcp import FastMCP

_TOKEN_PATH = Path.home() / ".claude" / "skills" / "credentials" / "google_pim" / "token.json"
_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]

mcp = FastMCP("google-pim")


class _NotAuthorized(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Credentials / service builders (lazy google imports)
# ---------------------------------------------------------------------------

def _credentials():
    """Load and refresh the stored OAuth credentials. Raises _NotAuthorized if
    no token has been set up yet."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    if not _TOKEN_PATH.exists():
        raise _NotAuthorized(
            f"No Google token at {_TOKEN_PATH}. Run: "
            "python -m skills.servers.google_pim_auth"
        )
    creds = Credentials.from_authorized_user_file(str(_TOKEN_PATH), _SCOPES)
    if not creds.valid and getattr(creds, "refresh_token", None):
        creds.refresh(Request())
        _TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    return creds


def _gmail():
    from googleapiclient.discovery import build
    return build("gmail", "v1", credentials=_credentials(), cache_discovery=False)


def _calendar():
    from googleapiclient.discovery import build
    return build("calendar", "v3", credentials=_credentials(), cache_discovery=False)


# ---------------------------------------------------------------------------
# Plain logic (unit-testable with a mock service; no google import here)
# ---------------------------------------------------------------------------

def _next_event(cal, *, now_iso: str) -> str:
    items = cal.events().list(
        calendarId="primary", timeMin=now_iso, maxResults=1,
        singleEvents=True, orderBy="startTime",
    ).execute().get("items", [])
    if not items:
        return "No upcoming events on your calendar."
    e = items[0]
    start = e.get("start", {}).get("dateTime") or e.get("start", {}).get("date", "?")
    summary = e.get("summary", "(no title)")
    attendees = [a.get("email", "") for a in e.get("attendees", []) if a.get("email")]
    who = f" with {', '.join(attendees)}" if attendees else ""
    return f"Next: {summary} at {start}{who}."


def _list_unread(gmail, n: int = 5) -> str:
    resp = gmail.users().messages().list(
        userId="me", labelIds=["UNREAD", "INBOX"], maxResults=n,
    ).execute()
    ids = [m["id"] for m in resp.get("messages", [])]
    if not ids:
        return "No unread email."
    lines: list[str] = []
    for mid in ids:
        msg = gmail.users().messages().get(
            userId="me", id=mid, format="metadata",
            metadataHeaders=["From", "Subject"],
        ).execute()
        headers = {h["name"]: h["value"]
                   for h in msg.get("payload", {}).get("headers", [])}
        lines.append(
            f"- From {headers.get('From', '?')} | "
            f"{headers.get('Subject', '(no subject)')} | "
            f"{(msg.get('snippet', '') or '')[:160]}"
        )
    return "\n".join(lines)


def _send_reply(gmail, *, to: str, subject: str, body: str, thread_id: str = "") -> str:
    mime = MIMEText(body)
    mime["To"] = to
    mime["Subject"] = subject
    payload = {"raw": base64.urlsafe_b64encode(mime.as_bytes()).decode()}
    if thread_id:
        payload["threadId"] = thread_id
    sent = gmail.users().messages().send(userId="me", body=payload).execute()
    return f"Sent (id {sent.get('id', '?')})."


def _create_event(cal, *, summary: str, start_iso: str, end_iso: str,
                  attendees: "list | None" = None) -> str:
    body = {
        "summary": summary,
        "start": {"dateTime": start_iso},
        "end": {"dateTime": end_iso},
    }
    if attendees:
        body["attendees"] = [{"email": a} for a in attendees]
    ev = cal.events().insert(calendarId="primary", body=body).execute()
    return f"Created event '{summary}' ({ev.get('htmlLink', '')})."


# ---------------------------------------------------------------------------
# MCP tool wrappers
# ---------------------------------------------------------------------------

@mcp.tool()
def list_next_event() -> str:
    """Return the user's next upcoming calendar event (read-only)."""
    from datetime import datetime, timezone
    return _next_event(_calendar(), now_iso=datetime.now(timezone.utc).isoformat())


@mcp.tool()
def list_unread(max_results: int = 5) -> str:
    """Return raw snippets of the most recent unread inbox emails (read-only)."""
    return _list_unread(_gmail(), max_results)


@mcp.tool()
def send_reply(to: str, subject: str, body: str, thread_id: str = "") -> str:
    """Send an email (egress/send). Reply within a thread by passing thread_id."""
    return _send_reply(_gmail(), to=to, subject=subject, body=body, thread_id=thread_id)


@mcp.tool()
def create_event(summary: str, start_iso: str, end_iso: str, attendees: str = "") -> str:
    """Create a calendar event (egress/send). attendees = comma-separated emails."""
    att = [a.strip() for a in attendees.split(",") if a.strip()] if attendees else None
    return _create_event(_calendar(), summary=summary, start_iso=start_iso,
                         end_iso=end_iso, attendees=att)


if __name__ == "__main__":
    mcp.run(transport="stdio")
