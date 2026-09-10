"""Sovereign Gmail — MCP Server for Gmail integration.

Exposes tools: list_messages, get_message, send_email, search_emails.
Reuses the same credentials.json as health_mcp (Google Cloud project).
Token stored at ~/.sovereign-link/gmail_token.json.
First run opens a browser for OAuth authorization.
"""
from __future__ import annotations

import os
import base64
import email as email_lib
from email.mime.text import MIMEText

from mcp.server.mcpserver import MCPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
_CREDENTIALS = os.path.join(_PROJECT_ROOT, "health_mcp", "credentials.json")
_TOKEN = os.path.expanduser("~/.sovereign-link/gmail_token.json")

_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

server = MCPServer(
    "sovereign-gmail",
    instructions=(
        "Access Gmail: list, read, search, and send emails. "
        "Tools: list_messages, get_message, search_emails, send_email. "
        "Supports Gmail's full search syntax (from:, to:, subject:, label:, after:, before:, etc.). "
        "All data stays strictly local — OAuth token stored on disk, no cloud proxy."
    ),
)


# ── Auth ──────────────────────────────────────────────────────────────────────

def _get_service():
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds = None
    os.makedirs(os.path.dirname(_TOKEN), exist_ok=True)

    if os.path.exists(_TOKEN):
        creds = Credentials.from_authorized_user_file(_TOKEN, _SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(_CREDENTIALS, _SCOPES)
            creds = flow.run_local_server(port=0)
        with open(_TOKEN, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _decode_body(payload: dict) -> str:
    """Recursively extract plain text body from a Gmail message payload."""
    mime_type = payload.get("mimeType", "")
    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace") if data else ""
    if mime_type.startswith("multipart/"):
        for part in payload.get("parts", []):
            text = _decode_body(part)
            if text:
                return text
    return ""


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


# ── Tool 1: list_messages ─────────────────────────────────────────────────────

@server.tool()
def list_messages(query: str = "", max_results: int = 10) -> str:
    """List recent emails from Gmail.

    query: Optional Gmail search filter (e.g. 'is:unread', 'label:inbox', 'from:someone@example.com').
    max_results: Number of messages to return (default 10, max 50).
    Returns sender, subject, date, and snippet for each message.
    """
    service = _get_service()
    max_results = min(int(max_results), 50)

    resp = service.users().messages().list(
        userId="me",
        q=query or "",
        maxResults=max_results,
    ).execute()

    messages = resp.get("messages", [])
    if not messages:
        return f"Geen berichten gevonden{f\" voor '{query}'\" if query else ''}."

    lines = []
    for msg in messages:
        meta = service.users().messages().get(
            userId="me", id=msg["id"], format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
        headers = meta.get("payload", {}).get("headers", [])
        snippet = meta.get("snippet", "")[:120]
        lines.append(
            f"**ID:** {msg['id']}\n"
            f"**Van:** {_header(headers, 'From')}\n"
            f"**Onderwerp:** {_header(headers, 'Subject')}\n"
            f"**Datum:** {_header(headers, 'Date')}\n"
            f"*{snippet}*"
        )

    return "\n\n---\n\n".join(lines)


# ── Tool 2: get_message ───────────────────────────────────────────────────────

@server.tool()
def get_message(message_id: str) -> str:
    """Retrieve the full content of a specific email by its ID.

    message_id: The Gmail message ID (from list_messages or search_emails).
    Returns full headers and decoded plain-text body.
    """
    service = _get_service()

    msg = service.users().messages().get(
        userId="me", id=message_id, format="full"
    ).execute()

    headers = msg.get("payload", {}).get("headers", [])
    body = _decode_body(msg.get("payload", {}))

    return (
        f"**Van:** {_header(headers, 'From')}\n"
        f"**Aan:** {_header(headers, 'To')}\n"
        f"**Onderwerp:** {_header(headers, 'Subject')}\n"
        f"**Datum:** {_header(headers, 'Date')}\n\n"
        f"{body or '(geen leesbare tekst gevonden)'}"
    )


# ── Tool 3: search_emails ─────────────────────────────────────────────────────

@server.tool()
def search_emails(query: str, max_results: int = 20) -> str:
    """Search emails using Gmail's advanced search syntax.

    Supports operators like: from:, to:, subject:, label:, has:attachment,
    after:YYYY/MM/DD, before:YYYY/MM/DD, is:unread, is:starred, etc.
    Returns ID, sender, subject, and date for each match.
    """
    service = _get_service()
    max_results = min(int(max_results), 100)

    resp = service.users().messages().list(
        userId="me", q=query, maxResults=max_results
    ).execute()

    messages = resp.get("messages", [])
    if not messages:
        return f"Geen resultaten voor zoekopdracht: '{query}'."

    lines = []
    for msg in messages:
        meta = service.users().messages().get(
            userId="me", id=msg["id"], format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
        headers = meta.get("payload", {}).get("headers", [])
        lines.append(
            f"**{_header(headers, 'Date')}** | {_header(headers, 'From')} | "
            f"{_header(headers, 'Subject')} | ID: `{msg['id']}`"
        )

    return f"**{len(lines)} resultaten voor '{query}':**\n\n" + "\n".join(lines)


# ── Tool 4: send_email ────────────────────────────────────────────────────────

@server.tool()
def send_email(to: str, subject: str, body: str) -> str:
    """Send an email via Gmail.

    to: Recipient email address (e.g. 'name@example.com').
    subject: Email subject line.
    body: Plain-text email body.
    Returns confirmation with the sent message ID.
    """
    service = _get_service()

    message = MIMEText(body)
    message["to"] = to
    message["subject"] = subject

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    sent = service.users().messages().send(
        userId="me", body={"raw": raw}
    ).execute()

    return f"E-mail verzonden naar {to}. Message ID: {sent['id']}"
