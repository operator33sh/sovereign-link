"""Generic HTTP request tool with Moltbook guard integration."""
import json
import logging
import os
import re
import urllib.error
import urllib.request
from urllib.parse import urlparse

from tools.moltbook import _moltbook_debug_log, attempt_comment_auto_recovery

logger = logging.getLogger(__name__)

# Strict RFC-4122 UUID pattern — rejects numeric IDs, hallucinated UUIDs with non-hex chars, etc.
_STRICT_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

# ─── Moltbook payload filter ─────────────────────────────────────────────────

# Fields that are never useful to the LLM and only bloat the context
_MB_STRIP_FIELDS = frozenset({
    "tsv", "contentHash", "randomBucket", "embeddings",
    "contentEmbedding", "searchVector", "internalMeta",
})

# Subset of author fields worth keeping — the rest (avatar URLs, counts, etc.) are dropped
_MB_AUTHOR_KEEP = frozenset({"id", "name", "username", "display_name", "handle"})

# Max items surfaced from feed / notification list responses
_MB_ITEM_CAP = 10

# If the filtered payload exceeds this many chars, replace with a compact briefing
_MB_BRIEFING_CAP = 3_500


def _strip_mb_item(item: dict) -> dict:
    """Strip noisy fields from a single Moltbook item dict."""
    out = {}
    for k, v in item.items():
        if k in _MB_STRIP_FIELDS:
            continue
        if k == "author" and isinstance(v, dict):
            v = {ak: av for ak, av in v.items() if ak in _MB_AUTHOR_KEEP}
        elif k == "authors" and isinstance(v, list):
            v = [
                {ak: av for ak, av in a.items() if ak in _MB_AUTHOR_KEEP}
                if isinstance(a, dict) else a
                for a in v
            ]
        out[k] = v
    return out


def _find_item_list(data) -> tuple[list | None, str | None]:
    """
    Return (list_of_items, key_name) from a parsed response.
    Handles both root-list responses and dict responses with a list under a known key.
    """
    if isinstance(data, list):
        return data, None
    if isinstance(data, dict):
        for key in ("posts", "notifications", "items", "data", "results", "feed", "comments"):
            if isinstance(data.get(key), list):
                return data[key], key
    return None, None


def _filter_moltbook_response(raw: str, url: str) -> str:
    """
    Filter a Moltbook API response to reduce LLM context load:
      1. Strip noisy fields (tsv, contentHash, randomBucket, author bloat).
      2. Cap list responses to _MB_ITEM_CAP items; note how many were dropped.
      3. If the filtered result is still large, emit a compact briefing instead
         of raw JSON, listing only IDs + key fields for targeted follow-up.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw  # not JSON — pass through unchanged

    items, list_key = _find_item_list(data)

    if items is None:
        # Single object response (e.g. POST result) — just strip noisy fields
        if isinstance(data, dict):
            filtered = json.dumps(_strip_mb_item(data), ensure_ascii=False)
        else:
            filtered = raw
        return filtered if len(filtered) <= _MB_BRIEFING_CAP else filtered[:_MB_BRIEFING_CAP] + "\n[…truncated]"

    total_count = len(items)
    capped = items[:_MB_ITEM_CAP]
    stripped = [_strip_mb_item(i) if isinstance(i, dict) else i for i in capped]
    dropped = total_count - len(capped)

    if list_key:
        result_data = {**{k: v for k, v in data.items() if k != list_key}, list_key: stripped}
        if dropped:
            result_data["_filter_note"] = f"{dropped} items dropped (cap={_MB_ITEM_CAP})"
    else:
        result_data = stripped
        if dropped:
            result_data = {"items": stripped, "_filter_note": f"{dropped} items dropped (cap={_MB_ITEM_CAP})"}

    filtered = json.dumps(result_data, ensure_ascii=False, indent=2)

    if len(filtered) <= _MB_BRIEFING_CAP:
        return filtered

    # Comments are primary requested content — never reduce to a briefing.
    # Return stripped JSON directly (the outer http_request caps at 8000 chars anyway).
    if list_key == "comments":
        _moltbook_debug_log(
            f"PAYLOAD FILTER: comments response kept as JSON (no briefing)\n"
            f"original={len(raw)}chars filtered={len(filtered)}chars"
        )
        return filtered

    # Still large — emit a compact ID briefing so the agent can request specifics
    id_lines = []
    for item in stripped:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id") or item.get("post_id") or item.get("notification_id") or "?"
        preview = str(item.get("content") or item.get("body") or item.get("title") or "")[:80]
        author = ""
        if isinstance(item.get("author"), dict):
            author = item["author"].get("username") or item["author"].get("name") or ""
        id_lines.append(f"- id={item_id} author={author} preview={preview!r}")

    briefing = (
        f"[MOLTBOOK BRIEFING — {len(stripped)} items"
        + (f" of {total_count}" if dropped else "")
        + f" from {url}]\n"
        + "\n".join(id_lines)
        + "\n\n[Payload filtered to prevent context overload. "
        "Request specific IDs via GET /api/v1/posts/<id> or /comments for detail.]"
    )
    _moltbook_debug_log(
        f"PAYLOAD FILTER: response condensed to briefing\n"
        f"original={len(raw)}chars filtered={len(filtered)}chars briefing={len(briefing)}chars"
    )
    return briefing


def _get_moltbook_api_key() -> str:
    """Return the Moltbook API key from env or ~/.config/moltbook/credentials.json."""
    api_key = os.environ.get("MOLTBOOK_API_KEY", "").strip()
    if api_key and api_key != "YOUR_API_KEY_HERE":
        return api_key
    creds_path = os.path.expanduser("~/.config/moltbook/credentials.json")
    try:
        with open(creds_path) as f:
            return json.load(f).get("api_key", "").strip()
    except Exception:
        return ""


def http_request(method: str, url: str, headers: dict | None = None, body=None) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "Error: only HTTP/HTTPS URLs are allowed"

    method = method.upper()
    if method not in ("GET", "POST", "PUT", "DELETE", "PATCH"):
        return f"Error: unsupported HTTP method '{method}'"

    is_moltbook = "moltbook.com" in (parsed.netloc or "")

    # Fix 2: auto-inject Authorization header for Moltbook if absent
    if is_moltbook:
        if headers is None:
            headers = {}
        if not any(k.lower() == "authorization" for k in headers):
            api_key = _get_moltbook_api_key()
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"

    # Fix 3: reject invalid post IDs in comment endpoints before hitting the network
    if is_moltbook and method == "POST":
        m = re.search(r"/posts/([^/]+)/comments", parsed.path)
        if m:
            candidate = m.group(1)
            if not _STRICT_UUID_RE.match(candidate):
                msg = (
                    f"Error: '{candidate}' is geen geldig UUID. "
                    "Gebruik GET /api/v1/feed of GET /api/v1/agents/profile "
                    "om de correcte post_id (UUID-formaat: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx) op te halen."
                )
                _moltbook_debug_log(f"UUID VALIDATION BLOCKED: {url}\nREASON: '{candidate}' is not a valid UUID")
                return msg

    # Fix 5: intercept GET /posts/{post_id}/comments/{comment_id} — this endpoint does not exist.
    # Auto-rewrite to GET /posts/{post_id}/comments (listing) and filter by comment_id.
    if is_moltbook and method == "GET":
        _UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
        m = re.search(rf"/posts/({_UUID})/comments/({_UUID})", parsed.path)
        if m:
            post_id, comment_id = m.group(1), m.group(2)
            listing_url = f"{parsed.scheme}://{parsed.netloc}/api/v1/posts/{post_id}/comments?sort=new&limit=50"
            _moltbook_debug_log(
                f"SINGLE-COMMENT REWRITE: {url}\n"
                f"→ listing fetch: {listing_url} (filter comment_id={comment_id})"
            )
            req2 = urllib.request.Request(listing_url, method="GET")
            for k, v in (headers or {}).items():
                req2.add_header(k, v)
            try:
                with urllib.request.urlopen(req2, timeout=15) as resp2:
                    listing_body = resp2.read().decode("utf-8", errors="replace")
                    listing_data = json.loads(listing_body)
                    comments = listing_data if isinstance(listing_data, list) else listing_data.get("comments", [])
                    match = next((c for c in comments if isinstance(c, dict) and c.get("id") == comment_id), None)
                    if match:
                        _moltbook_debug_log(f"SINGLE-COMMENT FOUND: id={comment_id}")
                        return f"Status: 200\n\n{json.dumps(match, ensure_ascii=False)}"
                    _moltbook_debug_log(f"SINGLE-COMMENT NOT IN LISTING: id={comment_id} (checked {len(comments)} comments)")
                    return (
                        f"Status: 200\n\n"
                        f"{{\"_note\": \"Comment {comment_id} not found in latest 50 comments for post {post_id}. "
                        f"It may have been deleted or is older than the listing window.\"}}"
                    )
            except urllib.error.HTTPError as e2:
                body2 = e2.read().decode("utf-8", errors="replace")[:500]
                _moltbook_debug_log(f"SINGLE-COMMENT LISTING ERROR: {e2.code} {body2}")
                return f"HTTP Error {e2.code}: {e2.reason}\n\n{body2}"
            except Exception as e2:
                _moltbook_debug_log(f"SINGLE-COMMENT EXCEPTION: {e2}")
                return f"Error: {e2}"

    data: bytes | None = None
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode("utf-8")
            if headers is None:
                headers = {}
            headers.setdefault("Content-Type", "application/json")
        elif isinstance(body, str):
            data = body.encode("utf-8")

    if is_moltbook:
        safe_headers = {
            k: (v[:10] + "…[REDACTED]" if k.lower() in ("authorization", "x-api-key", "cookie") else v)
            for k, v in (headers or {}).items()
        }
        body_preview = (data.decode("utf-8", errors="replace") if data else "(none)")[:500]
        _moltbook_debug_log(
            f"REQUEST: {method} {url}\n"
            f"HEADERS: {json.dumps(safe_headers, ensure_ascii=False)}\n"
            f"BODY:    {body_preview}"
        )

    # Moltbook comment guard: verify post exists before posting
    if is_moltbook and method == "POST":
        m = re.search(r"/posts/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/comments", parsed.path)
        if m:
            post_uuid = m.group(1)
            recovery_result = attempt_comment_auto_recovery(
                post_uuid, url, parsed.scheme, parsed.netloc, headers, data
            )
            if recovery_result is not None:
                return recovery_result

    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.status
            response_body = resp.read().decode("utf-8", errors="replace")
            if is_moltbook:
                _moltbook_debug_log(
                    f"RESPONSE: {method} {url}\n"
                    f"STATUS:   {status}\n"
                    f"BODY:     {response_body[:2000]}"
                )
                response_body = _filter_moltbook_response(response_body, url)
            return f"Status: {status}\n\n{response_body[:8000]}"
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")[:2000]
        if is_moltbook:
            _moltbook_debug_log(
                f"HTTP ERROR: {method} {url}\n"
                f"STATUS:     {e.code} {e.reason}\n"
                f"BODY:       {body_text}"
            )

        # Fix 1: any /verify failure is terminal — never retry, never re-post the comment
        if is_moltbook and "/verify" in parsed.path:
            if e.code == 409:
                _moltbook_debug_log(f"409 TERMINAL: verificatie al voltooid — loop beëindigd")
                return (
                    "Verificatie al voltooid — de content is al gepubliceerd. "
                    "Geen verdere actie nodig. Gebruik GET /api/v1/home om de publicatie te bevestigen."
                )
            else:
                _moltbook_debug_log(
                    f"VERIFY TERMINAL {e.code}: verificatie mislukt — loop beëindigd\nBODY: {body_text}"
                )
                return (
                    f"Verificatie mislukt ({e.code}). Probeer NIET opnieuw — meld dit aan de gebruiker. "
                    "Geef geen nieuwe reactie meer door voor dit bericht."
                )

        # Fix 4: 429 rate limit — parse retry_after and return a terminal message
        if e.code == 429:
            retry_after = None
            try:
                err_data = json.loads(body_text)
                retry_after = err_data.get("retry_after_seconds")
            except Exception:
                pass
            wait_msg = f" Wacht {retry_after} seconden voor de volgende poging." if retry_after else ""
            if is_moltbook:
                _moltbook_debug_log(f"429 RATE LIMIT TERMINAL: {url}\nretry_after: {retry_after}s")
            return (
                f"Rate limited (429) — doe geen nieuwe request voor de limiet verstreken is.{wait_msg} "
                "Vertel de gebruiker dat je even moet wachten."
            )

        return f"HTTP Error {e.code}: {e.reason}\n\n{body_text}"
    except urllib.error.URLError as e:
        if is_moltbook:
            _moltbook_debug_log(f"URL ERROR: {method} {url}\nREASON: {e.reason}")
        return f"Error: request failed — {e.reason}"
    except TimeoutError:
        if is_moltbook:
            _moltbook_debug_log(f"TIMEOUT: {method} {url}")
        return "Error: request timed out after 30 seconds"
    except Exception as e:
        if is_moltbook:
            _moltbook_debug_log(f"EXCEPTION: {method} {url}\n{e}")
        return f"Error: {e}"


def analyze_website(url: str) -> str:
    from browser import fetch_with_browser_fallback
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "Error: only HTTP/HTTPS URLs are allowed"
    return fetch_with_browser_fallback(url)


DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "http_request",
            "description": (
                "Perform an HTTP request (GET, POST, PUT, DELETE, PATCH) to any URL. "
                "Use this to interact with external APIs such as the Moltbook API "
                "(https://www.moltbook.com/api/v1). "
                "Supports custom headers and a request body. "
                "Returns the HTTP status code and response body. Times out after 30 seconds.\n\n"
                "MOLTBOOK-SPECIFIC RULES:\n"
                "- The Authorization header is injected automatically — you do not need to add it manually.\n"
                "- Endpoints always use the plural form: /api/v1/posts/ (NOT /api/v1/post/).\n"
                "- post_id must be a valid UUID (xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx). "
                "Never use numeric IDs or invented UUIDs. Fetch the correct post_id via "
                "GET /api/v1/feed or GET /api/v1/agents/profile first.\n"
                "- If you receive 'Rate limited (429)': stop retrying and wait the stated number of seconds.\n"
                "- If you receive 'Verificatie al voltooid': the content is published — do NOT call /verify again.\n\n"
                "PAYLOAD & CONTEXT RULES (prevent typing hangs):\n"
                "- SEQUENTIAL ONLY: never issue two large GET calls (feed, home, notifications, comments) "
                "in the same turn. Make one call, summarize the result to the user, then make the next call "
                "in a separate turn. This is mandatory — not optional.\n"
                "- ID-FIRST: if a response comes back as a '[MOLTBOOK BRIEFING]' block, do NOT attempt "
                "to process all items at once. Pick the relevant IDs and fetch each one individually via "
                "GET /api/v1/posts/<id> or GET /api/v1/posts/<id>/comments.\n"
                "- SUMMARIZE, DON'T ECHO: never paste raw API JSON into your reply. Always translate "
                "the response into a concise natural-language summary before presenting it to the user.\n"
                "- LARGE RESPONSE PROTOCOL: if a response exceeds 2000 characters, extract only the "
                "IDs and key fields you need, then discard the rest. Do not reason over the full payload."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"]},
                    "url": {"type": "string"},
                    "headers": {"type": "object"},
                    "body": {},
                },
                "required": ["method", "url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_website",
            "description": (
                "Fetch and extract the main readable content of a webpage as Markdown. "
                "Only HTTP/HTTPS URLs are supported."
            ),
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
]

HANDLERS = {
    "http_request": lambda args: http_request(args["method"], args["url"], args.get("headers"), args.get("body")),
    "analyze_website": lambda args: analyze_website(args["url"]),
}
