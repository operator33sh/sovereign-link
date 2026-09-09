"""Sovereign Health — MCP Server for Google Fit data.

Exposes tools for steps, sleep, heart rate, and activity summaries.
OAuth token is stored locally; first run opens a browser for authorization.
All data stays strictly local — no cloud routing.
"""
from __future__ import annotations

import os
import datetime
from typing import Optional

from mcp.server.mcpserver import MCPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
_CREDENTIALS = os.path.join(_HERE, "credentials.json")
_TOKEN = os.path.expanduser("~/.sovereign-link/google_fit_token.json")

_SCOPES = [
    "https://www.googleapis.com/auth/fitness.activity.read",
    "https://www.googleapis.com/auth/fitness.sleep.read",
    "https://www.googleapis.com/auth/fitness.heart_rate.read",
    "https://www.googleapis.com/auth/fitness.body.read",
]

server = MCPServer(
    "sovereign-health",
    instructions=(
        "Access the user's Google Fit health data locally via OAuth. "
        "Tools: get_steps, get_heart_rate, get_sleep, get_activity_summary. "
        "Date format: 'YYYY-MM-DD' or 'YYYY-MM-DD/YYYY-MM-DD' for ranges. "
        "All data is fetched directly from Google Fit — no intermediary cloud service."
    ),
)


# ── Auth helper ───────────────────────────────────────────────────────────────

def _get_service():
    """Return an authenticated Google Fit service object."""
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

    return build("fitness", "v1", credentials=creds)


# ── Date parsing ──────────────────────────────────────────────────────────────

def _parse_range(date_range: str) -> tuple[datetime.date, datetime.date]:
    """Parse 'YYYY-MM-DD' or 'YYYY-MM-DD/YYYY-MM-DD' into (start, end) dates."""
    if "/" in date_range:
        start_str, end_str = date_range.split("/", 1)
        start = datetime.date.fromisoformat(start_str.strip())
        end = datetime.date.fromisoformat(end_str.strip())
    else:
        start = end = datetime.date.fromisoformat(date_range.strip())
    return start, end


def _to_millis(d: datetime.date, end_of_day: bool = False) -> int:
    """Convert a date to milliseconds since epoch (start or end of day, UTC)."""
    dt = datetime.datetime(d.year, d.month, d.day, tzinfo=datetime.timezone.utc)
    if end_of_day:
        dt += datetime.timedelta(days=1)
    return int(dt.timestamp() * 1000)


def _to_nanos(d: datetime.date, end_of_day: bool = False) -> int:
    return _to_millis(d, end_of_day) * 1_000_000


# ── Tool 1: get_steps ─────────────────────────────────────────────────────────

@server.tool()
def get_steps(date_range: str) -> str:
    """Retrieve daily step counts from Google Fit.

    date_range: 'YYYY-MM-DD' for a single day, or 'YYYY-MM-DD/YYYY-MM-DD' for a range.
    Returns a list of dates with step counts.
    """
    start, end = _parse_range(date_range)
    service = _get_service()

    body = {
        "aggregateBy": [{"dataTypeName": "com.google.step_count.delta"}],
        "bucketByTime": {"durationMillis": 86400000},
        "startTimeMillis": _to_millis(start),
        "endTimeMillis": _to_millis(end, end_of_day=True),
    }

    resp = service.users().dataset().aggregate(userId="me", body=body).execute()
    lines = []
    for bucket in resp.get("bucket", []):
        date = datetime.datetime.fromtimestamp(
            int(bucket["startTimeMillis"]) / 1000, tz=datetime.timezone.utc
        ).date()
        steps = sum(
            p["value"][0]["intVal"]
            for ds in bucket.get("dataset", [])
            for p in ds.get("point", [])
            if p.get("value")
        )
        lines.append(f"{date}: {steps:,} stappen")

    return "\n".join(lines) if lines else f"Geen stappen gevonden voor '{date_range}'."


# ── Tool 2: get_heart_rate ────────────────────────────────────────────────────

@server.tool()
def get_heart_rate(date_range: str) -> str:
    """Retrieve average daily heart rate (BPM) from Google Fit.

    date_range: 'YYYY-MM-DD' or 'YYYY-MM-DD/YYYY-MM-DD'.
    Returns average BPM per day.
    """
    start, end = _parse_range(date_range)
    service = _get_service()

    body = {
        "aggregateBy": [{"dataTypeName": "com.google.heart_rate.bpm"}],
        "bucketByTime": {"durationMillis": 86400000},
        "startTimeMillis": _to_millis(start),
        "endTimeMillis": _to_millis(end, end_of_day=True),
    }

    resp = service.users().dataset().aggregate(userId="me", body=body).execute()
    lines = []
    for bucket in resp.get("bucket", []):
        date = datetime.datetime.fromtimestamp(
            int(bucket["startTimeMillis"]) / 1000, tz=datetime.timezone.utc
        ).date()
        values = [
            p["value"][0]["fpVal"]
            for ds in bucket.get("dataset", [])
            for p in ds.get("point", [])
            if p.get("value")
        ]
        if values:
            avg = sum(values) / len(values)
            lines.append(f"{date}: {avg:.1f} BPM gemiddeld ({len(values)} metingen)")

    return "\n".join(lines) if lines else f"Geen hartslag gevonden voor '{date_range}'."


# ── Tool 3: get_sleep ─────────────────────────────────────────────────────────

@server.tool()
def get_sleep(date_range: str) -> str:
    """Retrieve sleep segments from Google Fit.

    date_range: 'YYYY-MM-DD' or 'YYYY-MM-DD/YYYY-MM-DD'.
    Returns sleep segments with start/end times and sleep stage.

    Sleep stages: 1=awake, 2=sleep, 3=out-of-bed, 4=light, 5=deep, 6=REM.
    """
    start, end = _parse_range(date_range)
    service = _get_service()

    # Sleep data uses nanosecond dataset range
    start_ns = _to_nanos(start)
    end_ns = _to_nanos(end, end_of_day=True)
    dataset_id = f"{start_ns}-{end_ns}"

    _STAGE_NAMES = {1: "wakker", 2: "slaap", 3: "uit bed", 4: "licht", 5: "diep", 6: "REM"}

    try:
        resp = (
            service.users()
            .dataSources()
            .datasets()
            .get(
                userId="me",
                dataSourceId="derived:com.google.sleep.segment:com.google.android.gms:merged",
                datasetId=dataset_id,
            )
            .execute()
        )
    except Exception:
        # Fall back to aggregate if merged source unavailable
        body = {
            "aggregateBy": [{"dataTypeName": "com.google.sleep.segment"}],
            "bucketByTime": {"durationMillis": 86400000},
            "startTimeMillis": _to_millis(start),
            "endTimeMillis": _to_millis(end, end_of_day=True),
        }
        resp_agg = service.users().dataset().aggregate(userId="me", body=body).execute()
        points = [
            p
            for bucket in resp_agg.get("bucket", [])
            for ds in bucket.get("dataset", [])
            for p in ds.get("point", [])
        ]
        resp = {"point": points}

    lines = []
    for p in resp.get("point", []):
        t_start = datetime.datetime.fromtimestamp(
            int(p["startTimeNanos"]) / 1e9, tz=datetime.timezone.utc
        ).strftime("%Y-%m-%d %H:%M")
        t_end = datetime.datetime.fromtimestamp(
            int(p["endTimeNanos"]) / 1e9, tz=datetime.timezone.utc
        ).strftime("%H:%M")
        stage = p["value"][0]["intVal"] if p.get("value") else 0
        stage_name = _STAGE_NAMES.get(stage, f"stage {stage}")
        lines.append(f"{t_start}–{t_end}: {stage_name}")

    return "\n".join(lines) if lines else f"Geen slaapdata gevonden voor '{date_range}'."


# ── Tool 4: get_activity_summary ──────────────────────────────────────────────

@server.tool()
def get_activity_summary(date_range: str) -> str:
    """Retrieve a combined health summary: steps, calories, active minutes, and heart rate.

    date_range: 'YYYY-MM-DD' or 'YYYY-MM-DD/YYYY-MM-DD'.
    Returns a per-day summary of key health metrics.
    """
    start, end = _parse_range(date_range)
    service = _get_service()

    body = {
        "aggregateBy": [
            {"dataTypeName": "com.google.step_count.delta"},
            {"dataTypeName": "com.google.calories.expended"},
            {"dataTypeName": "com.google.active_minutes"},
            {"dataTypeName": "com.google.heart_rate.bpm"},
        ],
        "bucketByTime": {"durationMillis": 86400000},
        "startTimeMillis": _to_millis(start),
        "endTimeMillis": _to_millis(end, end_of_day=True),
    }

    resp = service.users().dataset().aggregate(userId="me", body=body).execute()
    lines = []

    for bucket in resp.get("bucket", []):
        date = datetime.datetime.fromtimestamp(
            int(bucket["startTimeMillis"]) / 1000, tz=datetime.timezone.utc
        ).date()

        metrics: dict[str, float] = {}
        for ds in bucket.get("dataset", []):
            dtype = ds.get("dataSourceId", "")
            points = ds.get("point", [])
            if not points:
                continue

            if "step_count" in dtype:
                metrics["stappen"] = sum(
                    p["value"][0]["intVal"] for p in points if p.get("value")
                )
            elif "calories" in dtype:
                metrics["kcal"] = sum(
                    p["value"][0]["fpVal"] for p in points if p.get("value")
                )
            elif "active_minutes" in dtype:
                metrics["actieve min"] = sum(
                    p["value"][0]["intVal"] for p in points if p.get("value")
                )
            elif "heart_rate" in dtype:
                vals = [p["value"][0]["fpVal"] for p in points if p.get("value")]
                if vals:
                    metrics["gem. BPM"] = round(sum(vals) / len(vals), 1)

        if metrics:
            summary = " | ".join(f"{k}: {v}" for k, v in metrics.items())
            lines.append(f"**{date}** — {summary}")

    return "\n".join(lines) if lines else f"Geen activiteitsdata gevonden voor '{date_range}'."
