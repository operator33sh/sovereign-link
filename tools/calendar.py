"""Google Calendar tools for Luna.

Provides list_events, get_event, create_event, update_event,
delete_event, and check_availability.
OAuth token stored at ~/.sovereign-link/google_calendar_token.json.
First call opens a browser for authorization; subsequent calls refresh silently.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CREDENTIALS = os.path.join(_HERE, "health_mcp", "credentials.json")
_TOKEN = os.path.expanduser("~/.sovereign-link/google_calendar_token.json")

_SCOPES = ["https://www.googleapis.com/auth/calendar"]


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


def list_events(
    time_min: str = "",
    time_max: str = "",
    max_results: int = 10,
    calendar_id: str = "primary",
) -> str:
    """List upcoming calendar events."""
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


def get_event(event_id: str, calendar_id: str = "primary") -> str:
    """Retrieve full details of a specific calendar event."""
    service = _get_service()
    event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
    return _format_event(event)


def create_event(
    summary: str,
    start: str,
    end: str,
    description: str = "",
    location: str = "",
    calendar_id: str = "primary",
) -> str:
    """Create a new calendar event."""
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


def update_event(
    event_id: str,
    summary: str = "",
    start: str = "",
    end: str = "",
    description: str = "",
    location: str = "",
    calendar_id: str = "primary",
) -> str:
    """Update an existing calendar event."""
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


def delete_event(event_id: str, calendar_id: str = "primary") -> str:
    """Delete a calendar event permanently."""
    service = _get_service()
    service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
    return f"Evenement {event_id} verwijderd."


def check_availability(
    time_min: str,
    time_max: str,
    calendar_id: str = "primary",
) -> str:
    """Check whether a time slot is free."""
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


# ── Tool registry ─────────────────────────────────────────────────────────────

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "list_events",
            "description": "List upcoming Google Calendar events. Optionally filter by time range.",
            "parameters": {
                "type": "object",
                "properties": {
                    "time_min": {"type": "string", "description": "ISO 8601 start datetime (default: now)."},
                    "time_max": {"type": "string", "description": "ISO 8601 end datetime (optional)."},
                    "max_results": {"type": "integer", "description": "Max events to return (default 10, max 50)."},
                    "calendar_id": {"type": "string", "description": "Calendar ID (default 'primary')."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_event",
            "description": "Retrieve full details of a specific Google Calendar event by its ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string", "description": "The Google Calendar event ID."},
                    "calendar_id": {"type": "string", "description": "Calendar ID (default 'primary')."},
                },
                "required": ["event_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_event",
            "description": "Create a new Google Calendar event.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Event title."},
                    "start": {"type": "string", "description": "ISO 8601 start datetime with timezone."},
                    "end": {"type": "string", "description": "ISO 8601 end datetime with timezone."},
                    "description": {"type": "string", "description": "Optional event description."},
                    "location": {"type": "string", "description": "Optional location."},
                    "calendar_id": {"type": "string", "description": "Calendar ID (default 'primary')."},
                },
                "required": ["summary", "start", "end"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_event",
            "description": "Update an existing Google Calendar event. Only provided fields are changed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string", "description": "The event ID to update."},
                    "summary": {"type": "string", "description": "New title (optional)."},
                    "start": {"type": "string", "description": "New ISO 8601 start (optional)."},
                    "end": {"type": "string", "description": "New ISO 8601 end (optional)."},
                    "description": {"type": "string", "description": "New description (optional)."},
                    "location": {"type": "string", "description": "New location (optional)."},
                    "calendar_id": {"type": "string", "description": "Calendar ID (default 'primary')."},
                },
                "required": ["event_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_event",
            "description": "Permanently delete a Google Calendar event.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string", "description": "The event ID to delete."},
                    "calendar_id": {"type": "string", "description": "Calendar ID (default 'primary')."},
                },
                "required": ["event_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_availability",
            "description": "Check if a time slot is free in Google Calendar. Returns 'Vrij' or lists conflicting events.",
            "parameters": {
                "type": "object",
                "properties": {
                    "time_min": {"type": "string", "description": "ISO 8601 start of the slot."},
                    "time_max": {"type": "string", "description": "ISO 8601 end of the slot."},
                    "calendar_id": {"type": "string", "description": "Calendar ID (default 'primary')."},
                },
                "required": ["time_min", "time_max"],
            },
        },
    },
]

HANDLERS = {
    "list_events": lambda args: list_events(
        args.get("time_min", ""),
        args.get("time_max", ""),
        args.get("max_results", 10),
        args.get("calendar_id", "primary"),
    ),
    "get_event": lambda args: get_event(args["event_id"], args.get("calendar_id", "primary")),
    "create_event": lambda args: create_event(
        args["summary"], args["start"], args["end"],
        args.get("description", ""), args.get("location", ""),
        args.get("calendar_id", "primary"),
    ),
    "update_event": lambda args: update_event(
        args["event_id"],
        args.get("summary", ""), args.get("start", ""), args.get("end", ""),
        args.get("description", ""), args.get("location", ""),
        args.get("calendar_id", "primary"),
    ),
    "delete_event": lambda args: delete_event(args["event_id"], args.get("calendar_id", "primary")),
    "check_availability": lambda args: check_availability(
        args["time_min"], args["time_max"], args.get("calendar_id", "primary")
    ),
}
