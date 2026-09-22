"""Proton Mail tools for Luna — connects to the local Proton Mail Bridge via IMAP.

Credentials are loaded from a .env file at the project root:
    PROTON_IMAP_USER=your@proton.me
    PROTON_IMAP_PASS=bridge-generated-password
    PROTON_IMAP_HOST=127.0.0.1   (optional, default 127.0.0.1)
    PROTON_IMAP_PORT=1143         (optional, default 1143)

The connection is opened and closed per call to conserve resources.
Never communicates outside localhost.
"""
from __future__ import annotations

import email
import imaplib
import os
from email.header import decode_header as _decode_header

# ---------------------------------------------------------------------------
# Config — loaded lazily from .env
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENV_PATH = os.path.join(_HERE, ".env")


def _load_env() -> None:
    """Load key=value pairs from .env into os.environ (skip if already set)."""
    if not os.path.exists(_ENV_PATH):
        return
    with open(_ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key not in os.environ:
                os.environ[key] = value


def _config() -> tuple[str, str, str, int]:
    _load_env()
    user = os.environ.get("PROTON_IMAP_USER", "")
    pw = os.environ.get("PROTON_IMAP_PASS", "")
    host = os.environ.get("PROTON_IMAP_HOST", "127.0.0.1")
    port = int(os.environ.get("PROTON_IMAP_PORT", "1143"))
    if not user or not pw:
        raise RuntimeError(
            "PROTON_IMAP_USER en PROTON_IMAP_PASS zijn niet ingesteld in .env. "
            "Zorg dat de Proton Mail Bridge actief is en voeg de bridge-inloggegevens toe."
        )
    # Security: only localhost is allowed
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(f"PROTON_IMAP_HOST '{host}' is niet toegestaan — alleen localhost.")
    return user, pw, host, port


# ---------------------------------------------------------------------------
# IMAP helpers
# ---------------------------------------------------------------------------

def _connect() -> imaplib.IMAP4:
    user, pw, host, port = _config()
    try:
        conn = imaplib.IMAP4(host, port)
        conn.login(user, pw)
        return conn
    except ConnectionRefusedError:
        raise RuntimeError(
            "Kan geen verbinding maken met de Proton Mail Bridge op "
            f"{host}:{port}. Is de Bridge gestart en IMAP ingeschakeld?"
        )
    except imaplib.IMAP4.error as exc:
        raise RuntimeError(f"IMAP-authenticatiefout: {exc}") from exc


def _decode_str(value: str | bytes) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    parts = _decode_header(value)
    result = []
    for fragment, charset in parts:
        if isinstance(fragment, bytes):
            result.append(fragment.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(fragment)
    return "".join(result)


def _extract_text(msg: email.message.Message) -> str:
    """Return the plain-text body of an email.message.Message."""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in disp:
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace") if payload else ""
        return "(geen leesbare tekst)"
    else:
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace") if payload else "(geen leesbare tekst)"


# ---------------------------------------------------------------------------
# Public tool functions
# ---------------------------------------------------------------------------

def check_proton_mail(args: dict) -> str:
    """Scan the inbox for unread messages and return metadata as Intel."""
    max_results = min(int(args.get("max_results", 10)), 50)
    folder = args.get("folder", "INBOX")

    conn = _connect()
    try:
        conn.select(folder, readonly=True)
        _, data = conn.search(None, "UNSEEN")
        uids = data[0].split() if data[0] else []
        if not uids:
            return f"📭 Geen ongelezen berichten in {folder}."

        # Fetch most recent first
        uids = uids[-max_results:][::-1]
        lines = [f"📬 **{len(uids)} ongelezen bericht(en) in {folder}**\n"]
        for uid in uids:
            _, msg_data = conn.fetch(uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            raw = msg_data[0][1] if msg_data and msg_data[0] else b""
            msg = email.message_from_bytes(raw)
            lines.append(
                f"**ID:** {uid.decode()}\n"
                f"**Van:** {_decode_str(msg.get('From', ''))}\n"
                f"**Onderwerp:** {_decode_str(msg.get('Subject', '(geen onderwerp)'))}\n"
                f"**Datum:** {msg.get('Date', '')}"
            )
        return "\n\n---\n\n".join(lines)
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def read_proton_email(args: dict) -> str:
    """Fetch the full plain-text content of one email by its IMAP UID."""
    uid = str(args.get("message_id", "")).strip()
    if not uid:
        return "Fout: message_id is verplicht."
    folder = args.get("folder", "INBOX")

    conn = _connect()
    try:
        conn.select(folder, readonly=True)
        _, msg_data = conn.fetch(uid, "(RFC822)")
        if not msg_data or not msg_data[0]:
            return f"Bericht met ID '{uid}' niet gevonden in {folder}."
        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)
        body = _extract_text(msg)
        return (
            f"**Van:** {_decode_str(msg.get('From', ''))}\n"
            f"**Aan:** {_decode_str(msg.get('To', ''))}\n"
            f"**Onderwerp:** {_decode_str(msg.get('Subject', '(geen onderwerp)'))}\n"
            f"**Datum:** {msg.get('Date', '')}\n\n"
            f"{body.strip()}"
        )
    finally:
        try:
            conn.logout()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "check_proton_mail",
            "description": (
                "Scan de Proton Mail inbox op ongelezen berichten via de lokale Bridge. "
                "Geeft metadata (afzender, onderwerp, datum, ID) terug als Intel. "
                "Vereist dat de Proton Mail Bridge actief is op localhost."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "max_results": {
                        "type": "integer",
                        "description": "Maximaal aantal berichten (standaard 10, max 50).",
                    },
                    "folder": {
                        "type": "string",
                        "description": "IMAP-map (standaard 'INBOX').",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_proton_email",
            "description": (
                "Haal de volledige tekst van één Proton Mail bericht op via zijn IMAP UID. "
                "Gebruik check_proton_mail eerst om de ID te verkrijgen."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "De IMAP UID van het bericht (verkregen via check_proton_mail).",
                    },
                    "folder": {
                        "type": "string",
                        "description": "IMAP-map (standaard 'INBOX').",
                    },
                },
                "required": ["message_id"],
            },
        },
    },
]

HANDLERS = {
    "check_proton_mail": check_proton_mail,
    "read_proton_email": read_proton_email,
}
