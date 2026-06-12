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


# One actionable sentence for EVERY auth failure mode (missing, expired,
# revoked). The phrase "reconnect Google" is load-bearing: the EmailWatcher
# detects it for the one-time spoken expiry alert, and the coordinator's voice
# "reconnect Google" command is the fix it names.
RECONNECT_MSG = ("Google access needs to be reconnected — say 'reconnect "
                 "Google', or run: python -m skills.servers.google_pim_auth")


# ---------------------------------------------------------------------------
# Credentials / service builders (lazy google imports)
# ---------------------------------------------------------------------------

def _credentials():
    """Load and refresh the stored OAuth credentials.

    Raises _NotAuthorized with the actionable RECONNECT_MSG for every auth
    failure mode: no token yet, expired token without a refresh token, and a
    revoked/expired refresh token (Google RefreshError / invalid_grant).
    """
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    if not _TOKEN_PATH.exists():
        raise _NotAuthorized(RECONNECT_MSG)
    try:
        creds = Credentials.from_authorized_user_file(str(_TOKEN_PATH), _SCOPES)
    except Exception as exc:                      # corrupt/foreign token file
        raise _NotAuthorized(RECONNECT_MSG) from exc
    if not creds.valid:
        if not getattr(creds, "refresh_token", None):
            raise _NotAuthorized(RECONNECT_MSG)
        try:
            creds.refresh(Request())
        except Exception as exc:                  # revoked / invalid_grant
            raise _NotAuthorized(RECONNECT_MSG) from exc
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


def _unread_structured(gmail, n: int = 10) -> list:
    """Unread inbox messages as structured dicts (for the email-arrived watcher)."""
    resp = gmail.users().messages().list(
        userId="me", labelIds=["UNREAD", "INBOX"], maxResults=n,
    ).execute()
    out: list = []
    for m in resp.get("messages", []):
        msg = gmail.users().messages().get(
            userId="me", id=m["id"], format="metadata",
            metadataHeaders=["From", "Subject"],
        ).execute()
        headers = {h["name"]: h["value"]
                   for h in msg.get("payload", {}).get("headers", [])}
        out.append({
            "id": m["id"],
            "from": headers.get("From", ""),
            "subject": headers.get("Subject", ""),
            "snippet": (msg.get("snippet", "") or "")[:200],
            "thread_id": msg.get("threadId", ""),
        })
    return out


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

def _auth_guard(fn):
    """Return RECONNECT_MSG (a normal string the pipeline can speak) instead of
    raising when credentials are missing/expired/revoked — a raw exception
    would surface as a cryptic MCP error. Mid-call 401s from a token revoked
    between refresh and use are caught by the invalid_grant sniff."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except _NotAuthorized:
            return RECONNECT_MSG
        except Exception as exc:
            text = str(exc).lower()
            if "invalid_grant" in text or "unauthorized" in text or "401" in text:
                return RECONNECT_MSG
            raise
    return wrapper


@mcp.tool()
@_auth_guard
def auth_status() -> str:
    """Whether Google access is currently working (read-only health probe)."""
    _credentials()
    return "Google access is connected and valid."


@mcp.tool()
@_auth_guard
def list_next_event() -> str:
    """Return the user's next upcoming calendar event (read-only)."""
    from datetime import datetime, timezone
    return _next_event(_calendar(), now_iso=datetime.now(timezone.utc).isoformat())


@mcp.tool()
@_auth_guard
def list_unread(max_results: int = 5) -> str:
    """Return raw snippets of the most recent unread inbox emails (read-only)."""
    return _list_unread(_gmail(), max_results)


@mcp.tool()
@_auth_guard
def unread_messages(max_results: int = 10) -> str:
    """Unread inbox messages as JSON [{id, from, subject, snippet, thread_id}]
    (read-only; consumed by the email-arrived watcher to publish events)."""
    import json as _json
    return _json.dumps(_unread_structured(_gmail(), max_results))


@mcp.tool()
@_auth_guard
def send_reply(to: str, subject: str, body: str, thread_id: str = "") -> str:
    """Send an email (egress/send). Reply within a thread by passing thread_id."""
    return _send_reply(_gmail(), to=to, subject=subject, body=body, thread_id=thread_id)


@mcp.tool()
@_auth_guard
def create_event(summary: str, start_iso: str, end_iso: str, attendees: str = "") -> str:
    """Create a calendar event (egress/send). attendees = comma-separated emails."""
    att = [a.strip() for a in attendees.split(",") if a.strip()] if attendees else None
    return _create_event(_calendar(), summary=summary, start_iso=start_iso,
                         end_iso=end_iso, attendees=att)


if __name__ == "__main__":
    mcp.run(transport="stdio")
