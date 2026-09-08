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


# ─── Core sentinel logic ──────────────────────────────────────────────────────

def run_moltbook_sentinel(_args: dict | None = None) -> str:
    """
    Main entry point called by AutomationEngine.

    Returns a short status string that is stored in automation_logs.json.
    """
    from notifications import notification_manager

    # 1. Fetch /home to get unread count
    status, home_data = _get(_HOME_URL)
    if status == 0 or home_data is None:
        return "Sentinel: /home request failed — skipping"
    if status == 429:
        return "Sentinel: rate limited — skipping this cycle"
    if status >= 400:
        return f"Sentinel: /home returned HTTP {status} — skipping"

    # Handle briefing format from payload filter
    if isinstance(home_data, dict) and "_raw" in home_data:
        raw_text = home_data["_raw"]
        if "MOLTBOOK BRIEFING" in raw_text:
            logger.info("Sentinel: /home returned briefing — parsing unread from text")
            # Try to extract unread_notification_count from briefing text
            unread = _extract_unread_from_briefing(raw_text)
        else:
            unread = 0
    else:
        unread = (
            home_data.get("unread_notification_count")
            or home_data.get("unread_notifications")
            or home_data.get("unreadCount")
            or 0
        )

    if not unread:
        logger.debug("Sentinel: no unread notifications")
        return "Sentinel: no unread notifications"

    # 2. Fetch notifications list
    notif_status, notif_data = _get(_NOTIFICATIONS_URL)
    if notif_status >= 400 or notif_data is None:
        return f"Sentinel: /notifications returned HTTP {notif_status}"

    notifications = _extract_notifications(notif_data)
    if not notifications:
        return "Sentinel: /home reports unread but /notifications list is empty"

    # 3. Load deduplication state
    seen = _evict_old(_load_seen())
    new_count = 0
    errors = []

    for notif in notifications:
        notif_id = str(
            notif.get("id") or notif.get("notification_id") or notif.get("post_id") or ""
        )
        if not notif_id or notif_id in seen:
            continue

        # Build alert content
        author = _extract_author(notif)
        preview = _extract_preview(notif)
        content = f"Moltbook Signal: New notification from @{author} regarding '{preview}'"

        # Push to notification queue
        try:
            notification_manager.write(
                content=content,
                agent_id="moltbook_sentinel",
                priority="high",
                category="alert",
            )
            new_count += 1
        except Exception as e:
            errors.append(f"write_notification failed for {notif_id}: {e}")
            continue

        # Mark as read on Moltbook
        read_url = _READ_URL_TEMPLATE.format(id=notif_id)
        try:
            _post(read_url)
        except Exception as e:
            logger.warning("Sentinel: could not mark %s as read: %s", notif_id, e)

        # Record as seen
        seen[notif_id] = datetime.now(timezone.utc).isoformat()

    _save_seen(seen)

    summary = f"Sentinel: {new_count} new notification(s) pushed"
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
