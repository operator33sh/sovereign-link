"""
Moltbook Sentinel — background automation tool.

Polls GET /api/v1/home every 15 minutes (via the AutomationEngine cron),
checks for unread notifications, deduplicates, pushes high-priority alerts
to the notification queue, and marks notifications as read on Moltbook.

Deduplication state: .agent_temp/moltbook_sentinel_seen.json
  { "seen": { "<notification_id>": "<ISO timestamp>" } }
Entries older than SEEN_TTL_HOURS are evicted automatically.
"""
import json
import logging
import os
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

_SENTINEL_SEEN_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", ".agent_temp", "moltbook_sentinel_seen.json",
)
_SEEN_TTL_HOURS = 24  # evict seen IDs after this many hours
_HOME_URL = "https://www.moltbook.com/api/v1/home"
_NOTIFICATIONS_URL = "https://www.moltbook.com/api/v1/notifications"
_READ_URL_TEMPLATE = "https://www.moltbook.com/api/v1/notifications/read-by-post/{id}"


# ─── Seen-ID store ────────────────────────────────────────────────────────────

def _load_seen() -> dict[str, str]:
    """Return dict of {notification_id: ISO timestamp} from the seen cache."""
    try:
        with open(_SENTINEL_SEEN_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("seen", {})
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_seen(seen: dict[str, str]) -> None:
    os.makedirs(os.path.dirname(_SENTINEL_SEEN_PATH), exist_ok=True)
    with open(_SENTINEL_SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump({"seen": seen}, f, indent=2)


def _evict_old(seen: dict[str, str]) -> dict[str, str]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=_SEEN_TTL_HOURS)
    return {
        nid: ts for nid, ts in seen.items()
        if _parse_ts(ts) > cutoff
    }


def _parse_ts(ts: str) -> datetime:
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


# ─── HTTP helpers ─────────────────────────────────────────────────────────────

def _get(url: str) -> tuple[int, dict | list | None]:
    """Perform a GET via http_request and return (status, parsed_json | None)."""
    from tools.http import http_request
    raw = http_request("GET", url)
    # raw is "Status: 200\n\n{...}"
    try:
        status_line, _, body = raw.partition("\n\n")
        status = int(status_line.replace("Status:", "").strip())
        # body may be a briefing string (if filter kicked in) or JSON
        try:
            return status, json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return status, {"_raw": body}
    except Exception:
        return 0, None


def _post(url: str) -> int:
    """Perform a POST with empty body; return HTTP status."""
    from tools.http import http_request
    raw = http_request("POST", url, body={})
    try:
        status_line = raw.split("\n\n", 1)[0]
        return int(status_line.replace("Status:", "").strip())
    except Exception:
        return 0


# ─── Unread filter ────────────────────────────────────────────────────────────

def _filter_unread(notifications: list[dict]) -> list[dict]:
    """Return only unread notifications.

    If the API exposes an explicit 'read' or 'is_read' flag, respect it.
    If no such flag exists on any item, treat ALL items as candidates — the
    seen-IDs dedup layer prevents duplicate pushes.
    """
    has_read_flag = any(
        "read" in n or "is_read" in n
        for n in notifications
    )
    if not has_read_flag:
        return notifications  # dedup handles it
    return [
        n for n in notifications
        if not n.get("read") and not n.get("is_read")
    ]


# ─── Heartbeat logging ─────────────────────────────────────────────────────────

def _heartbeat_log(endpoint: str, status: int, data) -> None:
    """Write a one-line heartbeat to the automation log path for observability."""
    import json as _json
    try:
        log_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", ".agent_temp", "moltbook_sentinel_heartbeat.log",
        )
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()
        if isinstance(data, (dict, list)):
            snippet = _json.dumps(data)[:200]
        else:
            snippet = str(data)[:200] if data else "None"
        line = f"{ts} | {endpoint} | status={status} | {snippet}\n"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        logger.debug("Sentinel: heartbeat log write failed (non-fatal)")


# ─── Core sentinel logic ──────────────────────────────────────────────────────

def run_moltbook_sentinel(_args: dict | None = None) -> str:
    """
    Main entry point called by AutomationEngine.

    Strategy: skip /home entirely — go straight to /notifications and rely
    on deduplication (seen-IDs) as the reliability layer. This avoids
    fragile field-name guessing on the /home response.

    Returns a short heartbeat string stored in automation_logs.json.
    """
    from notifications import notification_manager

    # 1. Fetch /notifications directly — dedup handles "already seen" items
    notif_status, notif_data = _get(_NOTIFICATIONS_URL)

    # Heartbeat: always log what the API actually returned
    _heartbeat_log("notifications", notif_status, notif_data)

    if notif_status == 0 or notif_data is None:
        return "Sentinel: /notifications request failed — skipping"
    if notif_status == 429:
        return "Sentinel: rate limited — skipping this cycle"
    if notif_status >= 400:
        return f"Sentinel: /notifications returned HTTP {notif_status} — skipping"

    notifications = _extract_notifications(notif_data)
    if not notifications:
        return f"Sentinel[heartbeat]: /notifications returned 0 items (status={notif_status})"

    # 2. Filter to only unread items where the API exposes a read flag;
    #    if no read flag exists, treat ALL items as candidates — dedup prevents spam
    unread = _filter_unread(notifications)

    # 3. Load deduplication state
    seen = _evict_old(_load_seen())
    new_count = 0
    errors = []

    for notif in unread:
        notif_id = str(
            notif.get("id") or notif.get("notification_id") or notif.get("post_id") or ""
        )
        if not notif_id or notif_id in seen:
            continue

        author = _extract_author(notif)
        preview = _extract_preview(notif)
        # Classify priority: comments/mentions always high; rest also high per spec
        content = f"Moltbook Signal: New notification from @{author} — '{preview}'"

        try:
            notification_manager.write(
                content=content,
                agent_id="moltbook_sentinel",
                priority="high",
                category="alert",
            )
            new_count += 1
        except Exception as e:
            errors.append(f"write failed for {notif_id}: {e}")
            continue

        # Mark as read on Moltbook (best-effort)
        read_url = _READ_URL_TEMPLATE.format(id=notif_id)
        try:
            _post(read_url)
        except Exception as e:
            logger.warning("Sentinel: could not mark %s as read: %s", notif_id, e)

        seen[notif_id] = datetime.now(timezone.utc).isoformat()

    _save_seen(seen)

    total = len(notifications)
    unread_count = len(unread)
    summary = (
        f"Sentinel[heartbeat]: {total} notification(s) fetched, "
        f"{unread_count} unread candidate(s), {new_count} new alert(s) pushed"
    )
    if errors:
        summary += f" | {len(errors)} error(s): {errors[0]}"
    logger.info(summary)
    return summary


# ─── Extraction helpers ───────────────────────────────────────────────────────

def _extract_unread_from_briefing(text: str) -> int:
    """Try to find a numeric unread count in a briefing string."""
    import re
    m = re.search(r"unread[_\s]?(?:notification[_\s]?)?count[\"']?\s*[:=]\s*(\d+)", text, re.I)
    if m:
        return int(m.group(1))
    # If briefing lists items, assume each item is unread
    items = re.findall(r"^- id=", text, re.MULTILINE)
    return len(items)


def _extract_notifications(data) -> list[dict]:
    """Pull the notification list out of various response shapes."""
    if isinstance(data, list):
        return [i for i in data if isinstance(i, dict)]
    if isinstance(data, dict):
        for key in ("notifications", "items", "data", "results"):
            if isinstance(data.get(key), list):
                return [i for i in data[key] if isinstance(i, dict)]
        # Briefing fallback: parse id lines
        raw = data.get("_raw", "")
        if "MOLTBOOK BRIEFING" in raw:
            import re
            items = []
            for m in re.finditer(r"id=([^\s]+)\s+author=([^\s]*)\s+preview='([^']*)'", raw):
                items.append({
                    "id": m.group(1),
                    "_author_raw": m.group(2),
                    "content": m.group(3),
                })
            return items
    return []


def _extract_author(notif: dict) -> str:
    author = notif.get("author") or notif.get("_author_raw") or ""
    if isinstance(author, dict):
        return (
            author.get("username")
            or author.get("handle")
            or author.get("name")
            or "unknown"
        )
    return str(author) or "unknown"


def _extract_preview(notif: dict) -> str:
    for key in ("content", "body", "title", "post_title", "preview", "summary"):
        val = notif.get(key)
        if val and isinstance(val, str):
            return val[:80].strip()
    return "—"


# ─── Tool registration ────────────────────────────────────────────────────────

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "moltbook_sentinel",
            "description": (
                "Run the Moltbook Sentinel: polls /home for unread notifications, "
                "deduplicates, pushes high-priority alerts to the notification queue, "
                "and marks notifications as read. Called automatically by the cron "
                "automation every 15 minutes. Can also be triggered manually."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    }
]

HANDLERS = {
    "moltbook_sentinel": lambda args: run_moltbook_sentinel(args),
}
