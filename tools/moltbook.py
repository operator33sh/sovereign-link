"""Moltbook-specific guard, auto-recovery, and debug logging."""
import json
import logging
import os
import re
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_TEMP_PATH = os.path.join(PROJECT_ROOT, ".agent_temp")

_UUID_RE = re.compile(r"/posts/([0-9a-f-]{36})/comments")


def _moltbook_debug_log(entry: str) -> None:
    """Append a raw trace entry to .agent_temp/moltbook_debug.log."""
    try:
        from datetime import datetime
        os.makedirs(AGENT_TEMP_PATH, exist_ok=True)
        log_path = os.path.join(AGENT_TEMP_PATH, "moltbook_debug.log")
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n[{ts}]\n{entry}\n{'─'*60}\n")
    except Exception:
        pass


def extract_mention_username(data: bytes | None) -> str | None:
    """Extract the first @mention username from a JSON body's 'content' field."""
    if not data:
        return None
    try:
        body_dict = json.loads(data.decode("utf-8", errors="replace"))
        mention = re.search(r"@(\w+)", body_dict.get("content", ""))
        if mention:
            return mention.group(1)
    except Exception:
        pass
    return None


def fetch_user_latest_post_id(
    username: str,
    scheme: str,
    netloc: str,
    headers: dict | None,
) -> str | None:
    """Look up a user's profile and return their latest post_id."""
    profile_url = f"{scheme}://{netloc}/api/v1/agents/profile?name={username}"
    req = urllib.request.Request(profile_url, method="GET")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as pr:
            raw = pr.read().decode("utf-8", errors="replace")
            profile_data = json.loads(raw)
            for field in ("posts", "recent_posts", "latest_posts"):
                posts = profile_data.get(field)
                if posts and isinstance(posts, list):
                    first = posts[0]
                    post_id = first.get("id") or first.get("post_id")
                    _moltbook_debug_log(
                        f"AUTO-RECOVERY: profile @{username} opgehaald\n"
                        f"gevonden post_id: {post_id}\n"
                        f"profile: {raw[:800]}"
                    )
                    return post_id
    except Exception as e:
        _moltbook_debug_log(f"AUTO-RECOVERY MISLUKT: profiel @{username} niet bereikbaar: {e}")
    return None


def attempt_comment_auto_recovery(
    post_uuid: str,
    url: str,
    scheme: str,
    netloc: str,
    headers: dict | None,
    data: bytes | None,
) -> str | None:
    """
    Guard: verify the target post exists before posting a comment.
    If the post returns 404, attempt auto-recovery via @mention lookup.

    Returns a result string if the request was handled (success or blocked),
    or None if the post exists and normal HTTP flow should continue.
    """
    verify_url = f"{scheme}://{netloc}/api/v1/posts/{post_uuid}"
    verify_req = urllib.request.Request(verify_url, method="GET")
    for k, v in (headers or {}).items():
        verify_req.add_header(k, v)

    try:
        with urllib.request.urlopen(verify_req, timeout=10):
            return None  # post exists — proceed normally
    except urllib.error.HTTPError as e:
        if e.code != 404:
            return None  # non-404 error — let caller handle it

    # Post returned 404 — attempt auto-recovery
    username = extract_mention_username(data)
    correct_post_id = None
    if username:
        correct_post_id = fetch_user_latest_post_id(username, scheme, netloc, headers)

    if correct_post_id:
        retry_url = f"{scheme}://{netloc}/api/v1/posts/{correct_post_id}/comments"
        _moltbook_debug_log(f"AUTO-RETRY: {retry_url}")
        retry_req = urllib.request.Request(retry_url, data=data, method="POST")
        for k, v in (headers or {}).items():
            retry_req.add_header(k, v)
        try:
            with urllib.request.urlopen(retry_req, timeout=30) as rr:
                rs = rr.status
                rb = rr.read().decode("utf-8", errors="replace")
                _moltbook_debug_log(f"AUTO-RETRY GESLAAGD: {rs}\n{rb[:500]}")
                return f"Status: {rs}\n\n{rb[:8000]}"
        except urllib.error.HTTPError as e2:
            rb2 = e2.read().decode("utf-8", errors="replace")
            _moltbook_debug_log(f"AUTO-RETRY MISLUKT: {e2.code} {e2.reason}\n{rb2[:500]}")
            return f"HTTP Error {e2.code}: {e2.reason}\n\n{rb2[:2000]}"
    else:
        msg = (
            f"GUARD BLOCKED: POST {url}\n"
            f"REASON: GET /posts/{post_uuid} returned 404 — post does not exist\n"
            f"AUTO-RECOVERY: geen @mention of profiel niet gevonden\n"
            f"ACTION: agent must look up the correct post_id via GET /feed or GET /agents/profile"
        )
        _moltbook_debug_log(msg)
        return (
            f"Error: POST geblokkeerd — post_id '{post_uuid}' bestaat niet (404).\n"
            f"Haal eerst de actuele post_id op via GET /api/v1/feed of GET /api/v1/agents/profile."
        )
