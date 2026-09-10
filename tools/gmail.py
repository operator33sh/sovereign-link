"""Gmail tools for Luna.

Provides list_messages, get_message, search_emails, and send_email.
OAuth token stored at ~/.sovereign-link/gmail_token.json.
First call opens a browser for authorization; subsequent calls refresh silently.
"""
from __future__ import annotations

import os
import base64
from email.mime.text import MIMEText

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CREDENTIALS = os.path.join(_HERE, "health_mcp", "credentials.json")
_TOKEN = os.path.expanduser("~/.sovereign-link/gmail_token.json")

_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


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


def _decode_body(payload: dict) -> str:
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


def list_messages(query: str = "", max_results: int = 10) -> str:
    """List recent emails, optionally filtered by a Gmail query string."""
    service = _get_service()
    max_results = min(int(max_results), 50)

    resp = service.users().messages().list(
        userId="me", q=query or "", maxResults=max_results
    ).execute()

    messages = resp.get("messages", [])
    if not messages:
        suffix = f" voor '{query}'" if query else ""
        return f"Geen berichten gevonden{suffix}."

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


def get_message(message_id: str) -> str:
    """Retrieve the full plain-text content of an email by its Gmail message ID."""
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


def search_emails(query: str, max_results: int = 20) -> str:
    """Search emails using Gmail advanced syntax (from:, subject:, after:, is:unread, etc.)."""
    service = _get_service()
    max_results = min(int(max_results), 100)

    resp = service.users().messages().list(
        userId="me", q=query, maxResults=max_results
    ).execute()

    messages = resp.get("messages", [])
    if not messages:
        return f"Geen resultaten voor: '{query}'."

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


def send_email(to: str, subject: str, body: str) -> str:
    """Send a plain-text email via Gmail."""
    service = _get_service()
    message = MIMEText(body)
    message["to"] = to
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    sent = service.users().messages().send(
        userId="me", body={"raw": raw}
    ).execute()
    return f"E-mail verzonden naar {to}. Message ID: {sent['id']}"


# ── Tool registry ─────────────────────────────────────────────────────────────

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "list_messages",
            "description": "List recent emails from Gmail, optionally filtered by a query string (e.g. 'is:unread', 'label:inbox', 'from:someone@example.com').",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Gmail search filter (optional)."},
                    "max_results": {"type": "integer", "description": "Number of messages to return (default 10, max 50)."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_message",
            "description": "Retrieve the full content of a specific email by its Gmail message ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string", "description": "The Gmail message ID."},
                },
                "required": ["message_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_emails",
            "description": "Search Gmail using advanced syntax: from:, to:, subject:, label:, after:YYYY/MM/DD, before:YYYY/MM/DD, is:unread, has:attachment, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Gmail search query."},
                    "max_results": {"type": "integer", "description": "Max results to return (default 20, max 100)."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_email",
            "description": "Send a plain-text email via the user's Gmail account.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Recipient email address."},
                    "subject": {"type": "string", "description": "Email subject line."},
                    "body": {"type": "string", "description": "Plain-text email body."},
                },
                "required": ["to", "subject", "body"],
            },
        },
    },
]

HANDLERS = {
    "list_messages": lambda args: list_messages(args.get("query", ""), args.get("max_results", 10)),
    "get_message": lambda args: get_message(args["message_id"]),
    "search_emails": lambda args: search_emails(args["query"], args.get("max_results", 20)),
    "send_email": lambda args: send_email(args["to"], args["subject"], args["body"]),
}
