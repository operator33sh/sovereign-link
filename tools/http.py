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
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.status
            response_body = resp.read().decode("utf-8", errors="replace")
            if is_moltbook:
                _moltbook_debug_log(
                    f"RESPONSE: {method} {url}\n"
                    f"STATUS:   {status}\n"
                    f"BODY:     {response_body[:2000]}"
                )
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
                "- If you receive 'Verificatie al voltooid': the content is published — do NOT call /verify again."
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
