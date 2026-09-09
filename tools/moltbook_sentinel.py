"""
Moltbook Sentinel — background automation tool.

Polls GET /api/v1/notifications every 15 minutes (via the AutomationEngine cron),
aggregates new notifications, enriches comment-type notifications with actual
comment text via GET /api/v1/posts/{id}/comments, then pushes ONE consolidated
Intelligence Report to the notification queue and marks all as read.

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
_COMMENTS_URL_TEMPLATE = "https://www.moltbook.com/api/v1/posts/{id}/comments?sort=new&limit=1"
_READ_BY_POST_TEMPLATE = "https://www.moltbook.com/api/v1/notifications/read-by-post/{id}"
_READ_ALL_URL = "https://www.moltbook.com/api/v1/notifications/read-all"

# Comment notification type identifiers (case-insensitive match on type field OR content text)
_COMMENT_TYPES = frozenset({
    "comment", "reply", "mention",
    "comment_reply", "post_reply", "reply_to_comment",
    "new_comment", "new_reply", "commented", "replied",
})


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
    try:
        status_line, _, body = raw.partition("\n\n")
        status = int(status_line.replace("Status:", "").strip())
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
        return notifications
    return [
        n for n in notifications
        if not n.get("read") and not n.get("is_read")
    ]


# ─── Heartbeat logging ─────────────────────────────────────────────────────────

def _heartbeat_log(endpoint: str, status: int, data) -> None:
    """Write a one-line heartbeat to .agent_temp for observability."""
    import json as _json
    try:
        log_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", ".agent_temp", "moltbook_sentinel_heartbeat.log",
        )
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()
        snippet = (_json.dumps(data)[:200] if isinstance(data, (dict, list))
                   else str(data)[:200] if data else "None")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"{ts} | {endpoint} | status={status} | {snippet}\n")
    except Exception:
        logger.debug("Sentinel: heartbeat log write failed (non-fatal)")


# ─── Extraction helpers ───────────────────────────────────────────────────────

def _extract_notifications(data) -> list[dict]:
    """Pull the notification list out of various response shapes."""
    if isinstance(data, list):
        return [i for i in data if isinstance(i, dict)]
    if isinstance(data, dict):
        for key in ("notifications", "items", "data", "results"):
            if isinstance(data.get(key), list):
                return [i for i in data[key] if isinstance(i, dict)]
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
    """Extract author name from whichever field Moltbook uses."""
    for key in ("author", "actor", "from_user", "user", "sender", "notifier", "_author_raw"):
        val = notif.get(key)
        if not val:
            continue
        if isinstance(val, dict):
            name = (
                val.get("username")
                or val.get("handle")
                or val.get("name")
                or val.get("display_name")
                or ""
            )
            if name:
                return str(name)
        elif isinstance(val, str) and val.strip():
            return val.strip()
    return "unknown"


def _extract_preview(notif: dict) -> str:
    for key in ("content", "body", "title", "post_title", "preview", "summary", "message"):
        val = notif.get(key)
        if val and isinstance(val, str):
            return val[:80].strip()
    return "—"


def _extract_author_from_content(text: str) -> str:
    """Extract username from patterns like 'plotracanvas started following you'.

    Returns empty string on no-match so callers can chain: result or fallback.
    """
    import re
    m = re.match(
        r'^([\w][\w._-]*)\s+(?:started following|is now following|followed you)',
        text, re.IGNORECASE,
    )
    if m:
        return m.group(1)
    return ""


def _notif_type(notif: dict) -> str:
    """Return a lowercase type string.

    Checks explicit type fields first; falls back to content-text inference
    so notifications with a null/missing type are still classified correctly.
    """
    t = str(
        notif.get("type") or notif.get("notification_type") or notif.get("kind") or ""
    ).lower().strip()
    if t and t not in ("null", "none", "unknown"):
        return t
    # Infer from generic Moltbook description text
    text = str(
        notif.get("content") or notif.get("body") or notif.get("message") or notif.get("title") or ""
    ).lower()
    if any(w in text for w in ("comment", "replied", "reply", "mentioned", "reacted")):
        return "comment"
    if "follow" in text:
        return "follow"
    if any(w in text for w in ("like", "liked", "heart", "upvote")):
        return "like"
    return "unknown"


def _notif_id(notif: dict) -> str:
    return str(
        notif.get("id") or notif.get("notification_id") or ""
    )


def _post_id(notif: dict) -> str | None:
    """Find a post UUID in whichever field Moltbook uses this response cycle."""
    for key in (
        "post_id", "target_post_id", "parent_post_id", "original_post_id",
        "reply_to_post_id", "object_id", "reference_id", "target_id",
    ):
        v = notif.get(key)
        if v and str(v).strip():
            return str(v).strip()
    # Try nested objects
    for obj_key in ("post", "target", "object", "subject"):
        obj = notif.get(obj_key)
        if isinstance(obj, dict):
            v = obj.get("id") or obj.get("post_id") or obj.get("uuid")
            if v:
                return str(v).strip()
    return None


# ─── Enrichment ───────────────────────────────────────────────────────────────

def _fetch_latest_comment(pid: str) -> tuple[str, str, str]:
    """
    Fetch comments for post `pid`.
    Returns (author_name, comment_snippet, post_title).
    Falls back to ("unknown", "—", pid) on any failure.
    """
    status, data = _get(_COMMENTS_URL_TEMPLATE.format(id=pid))
    if status != 200 or data is None:
        return "unknown", "—", pid

    comments = []
    if isinstance(data, list):
        comments = [c for c in data if isinstance(c, dict)]
    elif isinstance(data, dict):
        for key in ("comments", "items", "data", "results"):
            if isinstance(data.get(key), list):
                comments = [c for c in data[key] if isinstance(c, dict)]
                break

    if not comments:
        return "unknown", "Comment deleted", pid

    # Pick the most recent comment (last in list, or highest created_at)
    latest = comments[-1]
    author = _extract_author(latest)
    snippet = _extract_preview(latest)
    # Try to get post title from the response envelope or first comment's post field
    post_title = ""
    if isinstance(data, dict):
        post_title = (
            data.get("post_title") or data.get("title")
            or (data.get("post", {}).get("title") if isinstance(data.get("post"), dict) else "")
            or ""
        )
    if not post_title:
        post_obj = latest.get("post") or {}
        post_title = (
            post_obj.get("title") if isinstance(post_obj, dict) else ""
        ) or pid[:8]

    return author, snippet, post_title


_SENTINEL_LATEST_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", ".agent_temp", "moltbook_sentinel_latest.json",
)


def _save_latest(activities: list[dict], report_lines: list[str]) -> None:
    """Persist the most recently reported activity so the agent has context.

    Stored in .agent_temp/moltbook_sentinel_latest.json.
    The agent can read this file when the user asks a follow-up question
    (e.g. "open that post", "show me what they said") after a Telegram alert.
    """
    try:
        os.makedirs(os.path.dirname(_SENTINEL_LATEST_PATH), exist_ok=True)
        payload = {
            "reported_at": datetime.now(timezone.utc).isoformat(),
            "report_lines": report_lines,
            "posts": [
                {
                    "post_id": a.get("post_id"),
                    "post_title": a.get("post_title"),
                    "submolt": a.get("submolt_name"),
                    "commenters": a.get("latest_commenters", []),
                    "count": a.get("new_notification_count", 1),
                }
                for a in activities
                if a.get("post_id")
            ],
        }
        with open(_SENTINEL_LATEST_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except Exception:
        logger.debug("Sentinel: could not write latest activity file (non-fatal)")


# ─── Core sentinel logic ──────────────────────────────────────────────────────

def run_moltbook_sentinel(_args: dict | None = None) -> str:
    """
    Main entry point called by AutomationEngine.

    Workflow:
      1. Fetch /home — rich activity data with real commenter names and post titles.
      2. Fetch /notifications — for follows and other non-comment events.
      3. Dedup both sources against seen cache.
      4. Enrich comment activity via GET /posts/{id}/comments for actual comment text.
      5. Build ONE consolidated Intelligence Report and push it.
      6. Call POST /notifications/read-all to clear unread state.
      7. Update seen cache.
    """
    from notifications import notification_manager

    seen = _evict_old(_load_seen())
    report_lines: list[str] = []
    seen_post_ids: list[str] = []   # home activity IDs to mark in cache
    seen_notif_ids: list[str] = []  # notification IDs to mark in cache

    # ── 1. Fetch /home ────────────────────────────────────────────────────────
    home_status, home_data = _get(_HOME_URL)
    _heartbeat_log("home", home_status, home_data)

    if home_status == 429:
        return "Sentinel: rate limited on /home — skipping this cycle"

    home_activities: list[dict] = []
    if home_status == 200 and isinstance(home_data, dict):
        home_activities = [
            a for a in home_data.get("activity_on_your_posts", [])
            if isinstance(a, dict) and a.get("post_id")
        ]

    # ── 2. Fetch /notifications ───────────────────────────────────────────────
    notif_status, notif_data = _get(_NOTIFICATIONS_URL)
    _heartbeat_log("notifications", notif_status, notif_data)

    all_notifs: list[dict] = []
    if notif_status == 200 and notif_data is not None:
        all_notifs = _extract_notifications(notif_data)

    # ── 3. Process new home activities (comments / replies) ───────────────────
    new_activities = [
        a for a in home_activities
        if ("home_" + a["post_id"]) not in seen
    ]

    for i, activity in enumerate(new_activities):
        if i > 0:
            import time
            time.sleep(0.3)  # avoid hammering the API when batch is large

        pid = activity["post_id"]
        post_title = activity.get("post_title") or pid[:8]
        commenters = activity.get("latest_commenters") or []
        author = commenters[0] if commenters else "unknown"
        count = activity.get("new_notification_count", 1)

        # Fetch actual comment text
        _, snippet, _ = _fetch_latest_comment(pid)

        extra = (len(commenters) - 1) if len(commenters) > 1 else (count - 1)
        if extra > 0:
            line = f'💬 @{author} (+{extra} others): "{snippet}" — Post: _{post_title}_'
        else:
            line = f'💬 @{author}: "{snippet}" — Post: _{post_title}_'
        report_lines.append(line)
        seen_post_ids.append(pid)

    # ── 4. Process /notifications for follows and other non-comment events ─────
    unread_notifs = _filter_unread(all_notifs) if all_notifs else []
    new_notifs = [
        n for n in unread_notifs
        if _notif_id(n) and _notif_id(n) not in seen
    ]

    if new_notifs:
        _heartbeat_log("first_new_notif_keys", 0, {
            "keys": list(new_notifs[0].keys()),
            "sample": {k: str(v)[:60] for k, v in new_notifs[0].items()
                       if not isinstance(v, (list, dict))},
        })

    for notif in new_notifs:
        ntype = _notif_type(notif)
        content = str(notif.get("content") or notif.get("body") or notif.get("message") or "")

        if ntype == "follow":
            author = _extract_author_from_content(content) or _extract_author(notif)
            line = f"👤 @{author} is je gaan volgen"
        elif ntype in _COMMENT_TYPES:
            # Comment notifications without a post_id — home already covered the
            # ones with a post_id, so skip duplicates; show the rest as fallback.
            pid = _post_id(notif)
            if pid and ("home_" + pid) in {("home_" + p) for p in seen_post_ids}:
                seen_notif_ids.append(_notif_id(notif))
                continue  # already reported via home
            author = _extract_author(notif)
            preview = _extract_preview(notif)
            post_title = pid[:8] if pid else "—"
            line = f'💬 @{author}: "{preview}" — Post: _{post_title}_'
        elif ntype in ("like", "upvote", "heart"):
            author = _extract_author(notif)
            preview = _extract_preview(notif)
            line = f"❤️ @{author} vond je post leuk: _{preview}_"
        else:
            author = _extract_author(notif)
            preview = _extract_preview(notif)
            if preview and preview != "—":
                line = f"🔔 @{author} [{ntype}]: \"{preview}\""
            else:
                line = f"🔔 @{author} [{ntype}]"
        report_lines.append(line)
        seen_notif_ids.append(_notif_id(notif))

    # ── 5. Nothing new? ───────────────────────────────────────────────────────
    if not report_lines:
        return (
            f"Sentinel[heartbeat]: {len(home_activities)} home activities, "
            f"{len(all_notifs)} notifications — 0 new — nothing to push"
        )

    # ── 6. Push ONE consolidated report ──────────────────────────────────────
    now_str = datetime.now(timezone.utc).strftime("%H:%M UTC")
    header = f"📬 *Moltbook Intelligence Report* ({now_str}):\n"
    report_body = "\n".join(f"- {l}" for l in report_lines)
    full_report = header + report_body

    try:
        notification_manager.write(
            content=full_report,
            agent_id="moltbook_sentinel",
            priority="high",
            category="alert",
        )
    except Exception as e:
        return f"Sentinel: notification_manager.write failed — {e}"

    # ── 7. Save latest activity for agent context ─────────────────────────────
    # Written to .agent_temp so the agent can reference which posts/follows
    # were just reported without needing Moltbook to still show them as unread.
    _save_latest(new_activities, report_lines)

    # ── 8. Update seen cache ──────────────────────────────────────────────────
    # NOTE: We intentionally do NOT call POST /notifications/read-all here.
    # The local seen cache prevents re-reporting. Keeping Moltbook notifications
    # unread means the agent can still look up the relevant posts via /home when
    # the user asks a follow-up question after receiving the Telegram alert.
    now_iso = datetime.now(timezone.utc).isoformat()
    for pid in seen_post_ids:
        seen["home_" + pid] = now_iso
    for nid in seen_notif_ids:
        if nid:
            seen[nid] = now_iso
    _save_seen(seen)

    summary = (
        f"Sentinel[ok]: {len(new_activities)} home activities + "
        f"{len(seen_notif_ids)} notifications → 1 report pushed"
    )
    logger.info(summary)
    return summary


# ─── Tool registration ────────────────────────────────────────────────────────

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "moltbook_sentinel",
            "description": (
                "Run the Moltbook Sentinel: polls /notifications for unread items, "
                "enriches comment notifications with actual text via /posts/{id}/comments, "
                "pushes ONE consolidated Intelligence Report to the notification queue, "
                "then marks all as read. Called automatically every 15 minutes."
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
