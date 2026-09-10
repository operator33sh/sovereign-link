"""Sovereign Calendar — MCP Server for Google Calendar integration.

Exposes tools: list_events, get_event, create_event, update_event,
delete_event, check_availability.
Reuses the same credentials.json as health_mcp (Google Cloud project).
Token stored at ~/.sovereign-link/google_calendar_token.json.
First run opens a browser for OAuth authorization.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from mcp.server.mcpserver import MCPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
_CREDENTIALS = os.path.join(_PROJECT_ROOT, "health_mcp", "credentials.json")
_TOKEN = os.path.expanduser("~/.sovereign-link/google_calendar_token.json")

_SCOPES = ["https://www.googleapis.com/auth/calendar"]

server = MCPServer(
    "sovereign-calendar",
    instructions=(
        "Manage Google Calendar: list, read, create, update, and delete events. "
        "Tools: list_events, get_event, create_event, update_event, delete_event, check_availability. "
        "All date/times must be ISO 8601 with timezone (e.g. '2026-09-10T14:00:00+02:00'). "
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

    return build("calendar", "v3", credentials=creds)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _format_event(event: dict) -> str:
    start = event.get("start", {})
    end = event.get("end", {})
    start_str = start.get("dateTime") or start.get("date", "?")
    end_str = end.get("dateTime") or end.get("date", "?")
    return (
        f"**ID:** {event.get('id', '?')}\n"
        f"**Titel:** {event.get('summary', '(geen titel)')}\n"
        f"**Start:** {start_str}\n"
        f"**Einde:** {end_str}\n"
        f"**Locatie:** {event.get('location', '-')}\n"
        f"**Beschrijving:** {event.get('description', '-')}\n"
        f"**Status:** {event.get('status', '?')}"
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Tool 1: list_events ───────────────────────────────────────────────────────

@server.tool()
def list_events(
    time_min: str = "",
    time_max: str = "",
    max_results: int = 10,
    calendar_id: str = "primary",
) -> str:
    """List upcoming calendar events.

    time_min: ISO 8601 start (default: now). E.g. '2026-09-10T00:00:00+02:00'.
    time_max: ISO 8601 end (optional). E.g. '2026-09-17T23:59:59+02:00'.
    max_results: Number of events to return (default 10, max 50).
    calendar_id: Calendar to query (default 'primary').
    """
    service = _get_service()
    max_results = min(int(max_results), 50)

    kwargs: dict = {
        "calendarId": calendar_id,
        "timeMin": time_min or _now_iso(),
        "maxResults": max_results,
        "singleEvents": True,
        "orderBy": "startTime",
    }
    if time_max:
        kwargs["timeMax"] = time_max

    result = service.events().list(**kwargs).execute()
    events = result.get("items", [])

    if not events:
        return "Geen evenementen gevonden in de opgegeven periode."

    return "\n\n---\n\n".join(_format_event(e) for e in events)


# ── Tool 2: get_event ─────────────────────────────────────────────────────────

@server.tool()
def get_event(event_id: str, calendar_id: str = "primary") -> str:
    """Retrieve full details of a specific calendar event.

    event_id: The Google Calendar event ID.
    calendar_id: Calendar containing the event (default 'primary').
    """
    service = _get_service()
    event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
    return _format_event(event)


# ── Tool 3: create_event ──────────────────────────────────────────────────────

@server.tool()
def create_event(
    summary: str,
    start: str,
    end: str,
    description: str = "",
    location: str = "",
    calendar_id: str = "primary",
) -> str:
    """Create a new calendar event.

    summary: Event title.
    start: ISO 8601 start datetime with timezone. E.g. '2026-09-15T10:00:00+02:00'.
    end: ISO 8601 end datetime with timezone. E.g. '2026-09-15T11:00:00+02:00'.
    description: Optional event description.
    location: Optional location string.
    calendar_id: Calendar to add the event to (default 'primary').
    """
    service = _get_service()

    body: dict = {
        "summary": summary,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location

    event = service.events().insert(calendarId=calendar_id, body=body).execute()
    return f"Evenement aangemaakt.\n\n{_format_event(event)}"


# ── Tool 4: update_event ──────────────────────────────────────────────────────

@server.tool()
def update_event(
    event_id: str,
    summary: str = "",
    start: str = "",
    end: str = "",
    description: str = "",
    location: str = "",
    calendar_id: str = "primary",
) -> str:
    """Update an existing calendar event (only provided fields are changed).

    event_id: The Google Calendar event ID.
    summary: New title (leave empty to keep current).
    start: New ISO 8601 start datetime (leave empty to keep current).
    end: New ISO 8601 end datetime (leave empty to keep current).
    description: New description (leave empty to keep current).
    location: New location (leave empty to keep current).
    calendar_id: Calendar containing the event (default 'primary').
    """
    service = _get_service()

    event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()

    if summary:
        event["summary"] = summary
    if start:
        event["start"] = {"dateTime": start}
    if end:
        event["end"] = {"dateTime": end}
    if description:
        event["description"] = description
    if location:
        event["location"] = location

    updated = service.events().update(
        calendarId=calendar_id, eventId=event_id, body=event
    ).execute()
    return f"Evenement bijgewerkt.\n\n{_format_event(updated)}"


# ── Tool 5: delete_event ──────────────────────────────────────────────────────

@server.tool()
def delete_event(event_id: str, calendar_id: str = "primary") -> str:
    """Delete a calendar event permanently.

    event_id: The Google Calendar event ID.
    calendar_id: Calendar containing the event (default 'primary').
    """
    service = _get_service()
    service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
    return f"Evenement {event_id} verwijderd."


# ── Tool 6: check_availability ────────────────────────────────────────────────

@server.tool()
def check_availability(
    time_min: str,
    time_max: str,
    calendar_id: str = "primary",
) -> str:
    """Check whether a time slot is free in the calendar.

    time_min: ISO 8601 start of the slot. E.g. '2026-09-15T10:00:00+02:00'.
    time_max: ISO 8601 end of the slot.   E.g. '2026-09-15T11:00:00+02:00'.
    calendar_id: Calendar to check (default 'primary').
    Returns 'Vrij' if the slot is free, or lists conflicting events.
    """
    service = _get_service()

    result = service.events().list(
        calendarId=calendar_id,
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    events = result.get("items", [])
    if not events:
        return f"Vrij — geen evenementen tussen {time_min} en {time_max}."

    conflicts = "\n\n---\n\n".join(_format_event(e) for e in events)
    return f"Bezet — {len(events)} conflicterend(e) evenement(en):\n\n{conflicts}"
