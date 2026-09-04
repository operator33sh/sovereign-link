"""Generic HTTP request tool with Moltbook guard integration."""
import json
import logging
import urllib.error
import urllib.request
from urllib.parse import urlparse

from tools.moltbook import _moltbook_debug_log, attempt_comment_auto_recovery

logger = logging.getLogger(__name__)


def http_request(method: str, url: str, headers: dict | None = None, body=None) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "Error: only HTTP/HTTPS URLs are allowed"

    method = method.upper()
    if method not in ("GET", "POST", "PUT", "DELETE", "PATCH"):
        return f"Error: unsupported HTTP method '{method}'"

    is_moltbook = "moltbook.com" in (parsed.netloc or "")

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
        import re as _re
        m = _re.search(r"/posts/([0-9a-f-]{36})/comments", parsed.path)
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
                "Returns the HTTP status code and response body. Times out after 30 seconds."
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
