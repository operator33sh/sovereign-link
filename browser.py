"""
Headless browser engine for Sovereign-Link using Playwright.

Architecture:
- BrowserSession: owns its own playwright/browser/context/page stack (thread-safe by isolation)
- Session registry: dict protected by a threading.Lock (registry ops only)
- fast-path: httpx first, fall back to browser on 403/blocked/empty content
- Interactive tools: browser_navigate → browser_click → browser_extract_content / browser_screenshot
"""

import logging
import os
import random
import threading
import time
from datetime import datetime
from urllib.parse import urlparse

import httpx
import trafilatura

logger = logging.getLogger(__name__)

try:
    from playwright.sync_api import sync_playwright, Page
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False
    logger.warning("browser.py: playwright not installed — browser tools unavailable. Run: pip install playwright && playwright install chromium")

_SESSION_TIMEOUT = 600  # seconds of inactivity before auto-close
_MAX_CONTENT_CHARS = 8000

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]

_COOKIE_SELECTORS = [
    "button:has-text('Alles accepteren')",
    "button:has-text('Accepteer alles')",
    "button:has-text('Accepteer')",
    "button:has-text('Accept all')",
    "button:has-text('Accept All')",
    "button:has-text('Accept')",
    "button:has-text('Akkoord')",
    "button:has-text('OK')",
    "button[id*='accept']",
    "button[id*='cookie']",
    "button[class*='accept']",
    "button[class*='cookie-accept']",
    "[data-testid*='accept']",
    "#accept-cookies",
    ".accept-cookies",
    ".cookie-accept",
    "[aria-label*='accept' i]",
    "[aria-label*='accepteer' i]",
]

# Screenshot directory — inside AGENT_TEMP_PATH, never indexed
_SCREENSHOTS_DIR: str | None = None


def _get_screenshots_dir() -> str:
    global _SCREENSHOTS_DIR
    if _SCREENSHOTS_DIR is None:
        from tools import AGENT_TEMP_PATH
        _SCREENSHOTS_DIR = os.path.join(AGENT_TEMP_PATH, "screenshots")
    return _SCREENSHOTS_DIR


# ------------------------------------------------------------------
# BrowserSession — one playwright stack per session (thread-isolated)
# ------------------------------------------------------------------

class BrowserSession:
    """
    Owns a full playwright → browser → context → page stack.
    Each session is self-contained so concurrent threads don't share state.
    """

    def __init__(self, session_id: str):
        if not _PLAYWRIGHT_AVAILABLE:
            raise RuntimeError(
                "playwright not installed. Run: pip install playwright && playwright install chromium"
            )
        self.session_id = session_id
        self.last_used = time.time()

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-infobars",
            ],
        )
        self._context = self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=random.choice(_USER_AGENTS),
            locale="nl-NL",
            extra_http_headers={"Accept-Language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7"},
        )
        # Minimal stealth: hide navigator.webdriver
        self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            "Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});"
        )
        self.page: Page = self._context.new_page()

    def touch(self) -> None:
        self.last_used = time.time()

    def is_stale(self) -> bool:
        return time.time() - self.last_used > _SESSION_TIMEOUT

    def close(self) -> None:
        for obj, method in [
            (self._context, "close"),
            (self._browser, "close"),
            (self._pw, "stop"),
        ]:
            try:
                getattr(obj, method)()
            except Exception:
                pass


# ------------------------------------------------------------------
# Session registry
# ------------------------------------------------------------------

_registry: dict[str, BrowserSession] = {}
_registry_lock = threading.Lock()


def _get_or_create_session(session_id: str) -> BrowserSession:
    """Return existing session or create a new one. Cleans up stale sessions."""
    with _registry_lock:
        _cleanup_stale_locked()
        if session_id not in _registry:
            _registry[session_id] = BrowserSession(session_id)
        session = _registry[session_id]
        session.touch()
    return session


def _cleanup_stale_locked() -> None:
    """Must be called with _registry_lock held."""
    stale = [sid for sid, s in _registry.items() if s.is_stale()]
    for sid in stale:
        session = _registry.pop(sid)
        logger.info("browser: auto-closing stale session '%s'", sid)
        session.close()


def _remove_session(session_id: str) -> BrowserSession | None:
    with _registry_lock:
        return _registry.pop(session_id, None)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _dismiss_cookies(page: Page) -> bool:
    """Try cookie-wall selectors and click the first visible accept button."""
    for selector in _COOKIE_SELECTORS:
        try:
            btn = page.query_selector(selector)
            if btn and btn.is_visible():
                btn.click()
                page.wait_for_timeout(800)
                logger.info("browser: dismissed cookie wall via '%s'", selector)
                return True
        except Exception:
            continue
    return False


def _extract_markdown(page: Page) -> str:
    """Extract rendered page content as Markdown."""
    try:
        html = page.content()
        md = trafilatura.extract(
            html,
            output_format="markdown",
            include_comments=False,
            include_tables=True,
            no_fallback=False,
        )
        if md and len(md.strip()) > 100:
            return md
    except Exception:
        pass
    # Fallback: raw text
    try:
        return page.inner_text("body")
    except Exception as e:
        return f"Error extracting content: {e}"


def _truncate(text: str) -> str:
    if len(text) > _MAX_CONTENT_CHARS:
        return text[:_MAX_CONTENT_CHARS] + f"\n\n[... truncated at {_MAX_CONTENT_CHARS} chars ...]"
    return text


# ------------------------------------------------------------------
# Public tool functions
# ------------------------------------------------------------------

def browser_navigate(url: str, session_id: str = "default") -> str:
    """
    Navigate to a URL in a browser session.
    Auto-dismisses cookie walls and returns page content as Markdown.
    Creates the session if it doesn't exist yet.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "Error: only HTTP/HTTPS URLs are allowed"

    try:
        session = _get_or_create_session(session_id)
    except RuntimeError as e:
        return f"Error: {e}"

    try:
        session.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        session.page.wait_for_timeout(1500)
    except Exception as e:
        return f"Navigation failed: {e}"

    dismissed = _dismiss_cookies(session.page)

    try:
        title = session.page.title()
    except Exception:
        title = "(unknown)"

    content = _extract_markdown(session.page)

    header = f"**Page:** {title}\n**URL:** {url}\n**Session:** `{session_id}`"
    if dismissed:
        header += "\n*(Cookie wall dismissed)*"

    return _truncate(header + "\n\n---\n\n" + content)


def browser_click(selector: str, session_id: str = "default") -> str:
    """
    Click a CSS selector on the active browser session's current page.
    After clicking, waits briefly for the page to settle.
    """
    with _registry_lock:
        session = _registry.get(session_id)

    if session is None:
        return f"Error: no active session '{session_id}'. Call browser_navigate first."

    session.touch()
    try:
        session.page.click(selector, timeout=5000)
        session.page.wait_for_timeout(1000)
        title = session.page.title()
        return f"Clicked '{selector}'. Current page: {title}"
    except Exception as e:
        return f"Error clicking '{selector}': {e}"


def browser_extract_content(session_id: str = "default") -> str:
    """Extract the current page content as Markdown from an active session."""
    with _registry_lock:
        session = _registry.get(session_id)

    if session is None:
        return f"Error: no active session '{session_id}'. Call browser_navigate first."

    session.touch()
    content = _extract_markdown(session.page)
    return _truncate(content)


def browser_screenshot(session_id: str = "default") -> str:
    """
    Take a screenshot of the current page.
    Saves to AGENT_TEMP_PATH/screenshots/ and returns the file path.
    """
    with _registry_lock:
        session = _registry.get(session_id)

    if session is None:
        return f"Error: no active session '{session_id}'. Call browser_navigate first."

    session.touch()
    try:
        screenshots_dir = _get_screenshots_dir()
        os.makedirs(screenshots_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(screenshots_dir, f"{session_id}_{ts}.png")
        session.page.screenshot(path=path, full_page=False)
        return f"Screenshot saved: {path}"
    except Exception as e:
        return f"Error taking screenshot: {e}"


def browser_close_session(session_id: str = "default") -> str:
    """Close and clean up a browser session, freeing all browser resources."""
    session = _remove_session(session_id)
    if session is None:
        return f"No active session '{session_id}'."
    session.close()
    return f"Session '{session_id}' closed."


# ------------------------------------------------------------------
# Fast-path with browser fallback — used by analyze_website
# ------------------------------------------------------------------

def fetch_with_browser_fallback(url: str) -> str:
    """
    Try httpx first (fast). If blocked (403/401) or content is empty,
    fall back to a one-shot headless browser fetch.
    """
    headers = {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    try:
        response = httpx.get(url, headers=headers, follow_redirects=True, timeout=20.0)
    except httpx.TimeoutException:
        return "Error: request timed out"
    except Exception as e:
        logger.info("browser: fast-path fetch error (%s), trying browser fallback", e)
        return _browser_fallback(url)

    if response.status_code == 429:
        return "Error: rate limited (429) — try again later"

    if response.status_code not in (401, 403) and response.status_code < 400:
        extracted = trafilatura.extract(
            response.text,
            output_format="markdown",
            include_comments=False,
            include_tables=True,
            no_fallback=False,
        )
        if extracted and len(extracted.strip()) > 200:
            return _truncate(extracted)
        # Content present but couldn't extract meaningfully → try browser
        logger.info("browser: fast-path extraction thin (%d chars), trying browser fallback", len(extracted or ""))
    else:
        logger.info("browser: fast-path blocked (HTTP %s), trying browser fallback", response.status_code)

    return _browser_fallback(url)


def _browser_fallback(url: str) -> str:
    """One-shot browser fetch: navigate, extract, close session immediately."""
    if not _PLAYWRIGHT_AVAILABLE:
        return "Error: playwright not installed — cannot use browser fallback. Run: pip install playwright && playwright install chromium"

    session_id = f"_fallback_{int(time.time())}"
    try:
        result = browser_navigate(url, session_id=session_id)
    except Exception as e:
        result = f"Error: browser fallback failed: {e}"
    finally:
        browser_close_session(session_id)

    return result
