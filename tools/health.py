"""Google Fit health tools for Luna.

Provides get_steps, get_heart_rate, get_sleep, and get_activity_summary.
OAuth token is stored at ~/.sovereign-link/google_fit_token.json.
First call opens a browser for authorization; subsequent calls refresh silently.
"""
from __future__ import annotations

import os
import datetime

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CREDENTIALS = os.path.join(_HERE, "health_mcp", "credentials.json")
_TOKEN = os.path.expanduser("~/.sovereign-link/google_fit_token.json")

_SCOPES = [
    "https://www.googleapis.com/auth/fitness.activity.read",
    "https://www.googleapis.com/auth/fitness.sleep.read",
    "https://www.googleapis.com/auth/fitness.heart_rate.read",
    "https://www.googleapis.com/auth/fitness.body.read",
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

    return build("fitness", "v1", credentials=creds)


def _parse_range(date_range: str) -> tuple[datetime.date, datetime.date]:
    if "/" in date_range:
        s, e = date_range.split("/", 1)
        return datetime.date.fromisoformat(s.strip()), datetime.date.fromisoformat(e.strip())
    d = datetime.date.fromisoformat(date_range.strip())
    return d, d


def _to_millis(d: datetime.date, end_of_day: bool = False) -> int:
    dt = datetime.datetime(d.year, d.month, d.day, tzinfo=datetime.timezone.utc)
    if end_of_day:
        dt += datetime.timedelta(days=1)
    return int(dt.timestamp() * 1000)


def _to_nanos(d: datetime.date, end_of_day: bool = False) -> int:
    return _to_millis(d, end_of_day) * 1_000_000


def get_steps(date_range: str) -> str:
    """Retrieve daily step counts from Google Fit for a date or range (YYYY-MM-DD or YYYY-MM-DD/YYYY-MM-DD)."""
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


def get_heart_rate(date_range: str) -> str:
    """Retrieve average daily heart rate (BPM) from Google Fit for a date or range."""
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
            lines.append(f"{date}: {sum(values)/len(values):.1f} BPM gemiddeld ({len(values)} metingen)")
    return "\n".join(lines) if lines else f"Geen hartslag gevonden voor '{date_range}'."


def get_sleep(date_range: str) -> str:
    """Retrieve sleep segments from Google Fit. Stages: 1=wakker, 4=licht, 5=diep, 6=REM."""
    start, end = _parse_range(date_range)
    service = _get_service()
    start_ns = _to_nanos(start)
    end_ns = _to_nanos(end, end_of_day=True)
    _STAGE_NAMES = {1: "wakker", 2: "slaap", 3: "uit bed", 4: "licht", 5: "diep", 6: "REM"}

    try:
        resp = (
            service.users().dataSources().datasets().get(
                userId="me",
                dataSourceId="derived:com.google.sleep.segment:com.google.android.gms:merged",
                datasetId=f"{start_ns}-{end_ns}",
            ).execute()
        )
        points = resp.get("point", [])
    except Exception:
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

    lines = []
    for p in points:
        t_start = datetime.datetime.fromtimestamp(
            int(p["startTimeNanos"]) / 1e9, tz=datetime.timezone.utc
        ).strftime("%Y-%m-%d %H:%M")
        t_end = datetime.datetime.fromtimestamp(
            int(p["endTimeNanos"]) / 1e9, tz=datetime.timezone.utc
        ).strftime("%H:%M")
        stage = p["value"][0]["intVal"] if p.get("value") else 0
        lines.append(f"{t_start}–{t_end}: {_STAGE_NAMES.get(stage, f'stage {stage}')}")
    return "\n".join(lines) if lines else f"Geen slaapdata gevonden voor '{date_range}'."


def get_activity_summary(date_range: str) -> str:
    """Retrieve a combined daily summary: steps, calories, active minutes, and heart rate."""
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
        metrics: dict[str, object] = {}
        for ds in bucket.get("dataset", []):
            dtype = ds.get("dataSourceId", "")
            pts = ds.get("point", [])
            if not pts:
                continue
            if "step_count" in dtype:
                metrics["stappen"] = f"{sum(p['value'][0]['intVal'] for p in pts if p.get('value')):,}"
            elif "calories" in dtype:
                metrics["kcal"] = f"{sum(p['value'][0]['fpVal'] for p in pts if p.get('value')):.0f}"
            elif "active_minutes" in dtype:
                metrics["actieve min"] = sum(p["value"][0]["intVal"] for p in pts if p.get("value"))
            elif "heart_rate" in dtype:
                vals = [p["value"][0]["fpVal"] for p in pts if p.get("value")]
                if vals:
                    metrics["gem. BPM"] = f"{sum(vals)/len(vals):.1f}"
        if metrics:
            lines.append(f"**{date}** — " + " | ".join(f"{k}: {v}" for k, v in metrics.items()))
    return "\n".join(lines) if lines else f"Geen activiteitsdata gevonden voor '{date_range}'."


# ── Tool registry ─────────────────────────────────────────────────────────────

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_steps",
            "description": "Retrieve daily step counts from Google Fit.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date_range": {
                        "type": "string",
                        "description": "Single day 'YYYY-MM-DD' or range 'YYYY-MM-DD/YYYY-MM-DD'.",
                    }
                },
                "required": ["date_range"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_heart_rate",
            "description": "Retrieve average daily heart rate (BPM) from Google Fit.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date_range": {
                        "type": "string",
                        "description": "Single day 'YYYY-MM-DD' or range 'YYYY-MM-DD/YYYY-MM-DD'.",
                    }
                },
                "required": ["date_range"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sleep",
            "description": "Retrieve sleep segments and stages from Google Fit (light, deep, REM, awake).",
            "parameters": {
                "type": "object",
                "properties": {
                    "date_range": {
                        "type": "string",
                        "description": "Single day 'YYYY-MM-DD' or range 'YYYY-MM-DD/YYYY-MM-DD'.",
                    }
                },
                "required": ["date_range"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_activity_summary",
            "description": "Retrieve a combined daily health summary: steps, calories, active minutes, and heart rate.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date_range": {
                        "type": "string",
                        "description": "Single day 'YYYY-MM-DD' or range 'YYYY-MM-DD/YYYY-MM-DD'.",
                    }
                },
                "required": ["date_range"],
            },
        },
    },
]

HANDLERS = {
    "get_steps": lambda args: get_steps(args["date_range"]),
    "get_heart_rate": lambda args: get_heart_rate(args["date_range"]),
    "get_sleep": lambda args: get_sleep(args["date_range"]),
    "get_activity_summary": lambda args: get_activity_summary(args["date_range"]),
}
