"""
Headless browser engine for Sovereign-Link using Playwright.

Architecture:
- Single dedicated event loop (_BROWSER_LOOP) running in its own daemon thread.
  All Playwright async calls execute there, never on the bot's asyncio loop or
  on asyncio.to_thread worker threads — eliminating the greenlet conflict.
- BrowserSession: owns its own playwright/browser/context/page stack (async).
- Session registry: dict protected by a threading.Lock (registry ops only).
- Sync public API: submits coroutines to _BROWSER_LOOP via run_coroutine_threadsafe
  and blocks for the result — safe to call from any thread.
- fast-path: httpx first, fall back to browser on 403/blocked/empty content.
- Interactive tools: browser_navigate → browser_click → browser_extract_content / browser_screenshot
"""

import asyncio
import logging
import os
import random
import threading
import time
from concurrent.futures import Future
from datetime import datetime
from urllib.parse import urlparse

import httpx
import trafilatura

logger = logging.getLogger(__name__)

try:
    from playwright.async_api import async_playwright, Page
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False
    Page = object  # type: ignore[assignment,misc]
    logger.warning(
        "browser.py: playwright not installed — browser tools unavailable. "
        "Run: pip install playwright && playwright install chromium"
    )

_SESSION_TIMEOUT = 600  # seconds of inactivity before auto-close
_MAX_CONTENT_CHARS = 8000
_DISPATCH_TIMEOUT = 60  # seconds to wait for a browser coroutine to complete

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
# Dedicated browser event loop — one thread, one loop, all playwright
# ------------------------------------------------------------------

_BROWSER_LOOP: asyncio.AbstractEventLoop | None = None
_BROWSER_LOOP_LOCK = threading.Lock()


def _get_browser_loop() -> asyncio.AbstractEventLoop:
    """Return (and lazily start) the dedicated playwright event loop."""
    global _BROWSER_LOOP
    if _BROWSER_LOOP is not None and _BROWSER_LOOP.is_running():
        return _BROWSER_LOOP
    with _BROWSER_LOOP_LOCK:
        if _BROWSER_LOOP is not None and _BROWSER_LOOP.is_running():
            return _BROWSER_LOOP
        loop = asyncio.new_event_loop()

        def _run() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        t = threading.Thread(target=_run, name="browser-loop", daemon=True)
        t.start()
        _BROWSER_LOOP = loop
    return _BROWSER_LOOP


def _dispatch(coro) -> object:
    """
    Submit an async coroutine to the dedicated browser loop and block until done.
    Raises the coroutine's exception (if any) in the calling thread.
    """
    loop = _get_browser_loop()
    future: Future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=_DISPATCH_TIMEOUT)


# ------------------------------------------------------------------
# BrowserSession — async, lives entirely on _BROWSER_LOOP
# ------------------------------------------------------------------

class BrowserSession:
    """
    Owns a full async playwright → browser → context → page stack.
    Must only be created, used, and closed on _BROWSER_LOOP.
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.last_used = time.time()
        self._pw = None
        self._browser = None
        self._context = None
        self.page: Page | None = None

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-infobars",
            ],
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=random.choice(_USER_AGENTS),
            locale="nl-NL",
            extra_http_headers={"Accept-Language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7"},
        )
        await self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            "Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});"
        )
        self.page = await self._context.new_page()

    def touch(self) -> None:
        self.last_used = time.time()

    def is_stale(self) -> bool:
        return time.time() - self.last_used > _SESSION_TIMEOUT

    async def close(self) -> None:
        for obj, method in [
            (self._context, "close"),
            (self._browser, "close"),
            (self._pw, "stop"),
        ]:
            if obj is None:
                continue
            try:
                await getattr(obj, method)()
            except Exception:
                pass
        self._context = self._browser = self._pw = self.page = None


# ------------------------------------------------------------------
# Session registry
# ------------------------------------------------------------------

_registry: dict[str, BrowserSession] = {}
_registry_lock = threading.Lock()


async def _async_get_or_create_session(session_id: str) -> BrowserSession:
    """Create or return a session. Must run on _BROWSER_LOOP."""
    await _async_cleanup_stale()
    if session_id not in _registry:
        session = BrowserSession(session_id)
        await session.start()
        with _registry_lock:
            _registry[session_id] = session
    else:
        with _registry_lock:
            session = _registry[session_id]
    session.touch()
    return session


async def _async_cleanup_stale() -> None:
    stale: list[str] = []
    with _registry_lock:
        stale = [sid for sid, s in _registry.items() if s.is_stale()]
    for sid in stale:
        with _registry_lock:
            session = _registry.pop(sid, None)
        if session:
            logger.info("browser: auto-closing stale session '%s'", sid)
            await session.close()


def _get_session(session_id: str) -> BrowserSession | None:
    with _registry_lock:
        return _registry.get(session_id)


def _pop_session(session_id: str) -> BrowserSession | None:
    with _registry_lock:
        return _registry.pop(session_id, None)


# ------------------------------------------------------------------
# Async helpers (run on _BROWSER_LOOP)
# ------------------------------------------------------------------

async def _async_dismiss_cookies(page: Page) -> bool:
    for selector in _COOKIE_SELECTORS:
        try:
            btn = await page.query_selector(selector)
            if btn and await btn.is_visible():
                await btn.click()
                await page.wait_for_timeout(800)
                logger.info("browser: dismissed cookie wall via '%s'", selector)
                return True
        except Exception:
            continue
    return False


async def _async_extract_markdown(page: Page) -> str:
    try:
        html = await page.content()
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
    try:
        return await page.inner_text("body")
    except Exception as e:
        return f"Error extracting content: {e}"


def _truncate(text: str) -> str:
    if len(text) > _MAX_CONTENT_CHARS:
        return text[:_MAX_CONTENT_CHARS] + f"\n\n[... truncated at {_MAX_CONTENT_CHARS} chars ...]"
    return text


# ------------------------------------------------------------------
# Async implementations (dispatched to _BROWSER_LOOP)
# ------------------------------------------------------------------

async def _async_navigate(url: str, session_id: str) -> str:
    try:
        session = await _async_get_or_create_session(session_id)
    except Exception as e:
        return f"Error: could not create browser session: {e}"

    try:
        await session.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await session.page.wait_for_timeout(1500)
    except Exception as e:
        return f"Navigation failed: {e}"

    dismissed = await _async_dismiss_cookies(session.page)

    try:
        title = await session.page.title()
    except Exception:
        title = "(unknown)"

    content = await _async_extract_markdown(session.page)
    header = f"**Page:** {title}\n**URL:** {url}\n**Session:** `{session_id}`"
    if dismissed:
        header += "\n*(Cookie wall dismissed)*"
    return _truncate(header + "\n\n---\n\n" + content)


async def _async_click(selector: str, session_id: str) -> str:
    session = _get_session(session_id)
    if session is None:
        return f"Error: no active session '{session_id}'. Call browser_navigate first."
    session.touch()
    try:
        await session.page.click(selector, timeout=5000)
        await session.page.wait_for_timeout(1000)
        title = await session.page.title()
        return f"Clicked '{selector}'. Current page: {title}"
    except Exception as e:
        return f"Error clicking '{selector}': {e}"


async def _async_extract(session_id: str) -> str:
    session = _get_session(session_id)
    if session is None:
        return f"Error: no active session '{session_id}'. Call browser_navigate first."
    session.touch()
    content = await _async_extract_markdown(session.page)
    return _truncate(content)


async def _async_screenshot(session_id: str) -> str:
    session = _get_session(session_id)
    if session is None:
        return f"Error: no active session '{session_id}'. Call browser_navigate first."
    session.touch()
    try:
        screenshots_dir = _get_screenshots_dir()
        os.makedirs(screenshots_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(screenshots_dir, f"{session_id}_{ts}.png")
        await session.page.screenshot(path=path, full_page=False)
        return f"Screenshot saved: {path}"
    except Exception as e:
        return f"Error taking screenshot: {e}"


async def _async_close_session(session_id: str) -> str:
    session = _pop_session(session_id)
    if session is None:
        return f"No active session '{session_id}'."
    await session.close()
    return f"Session '{session_id}' closed."


# ------------------------------------------------------------------
# Public sync API — callable from any thread
# ------------------------------------------------------------------

def browser_navigate(url: str, session_id: str = "default") -> str:
    """
    Navigate to a URL in a browser session.
    Auto-dismisses cookie walls and returns page content as Markdown.
    Creates the session if it doesn't exist yet.
    """
    if not _PLAYWRIGHT_AVAILABLE:
        return "Error: playwright not installed. Run: pip install playwright && playwright install chromium"
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "Error: only HTTP/HTTPS URLs are allowed"
    try:
        return _dispatch(_async_navigate(url, session_id))
    except Exception as e:
        return f"Error: browser_navigate failed: {e}"


def browser_click(selector: str, session_id: str = "default") -> str:
    """
    Click a CSS selector on the active browser session's current page.
    After clicking, waits briefly for the page to settle.
    """
    if not _PLAYWRIGHT_AVAILABLE:
        return "Error: playwright not installed. Run: pip install playwright && playwright install chromium"
    try:
        return _dispatch(_async_click(selector, session_id))
    except Exception as e:
        return f"Error: browser_click failed: {e}"


def browser_extract_content(session_id: str = "default") -> str:
    """Extract the current page content as Markdown from an active session."""
    if not _PLAYWRIGHT_AVAILABLE:
        return "Error: playwright not installed. Run: pip install playwright && playwright install chromium"
    try:
        return _dispatch(_async_extract(session_id))
    except Exception as e:
        return f"Error: browser_extract_content failed: {e}"


def browser_screenshot(session_id: str = "default") -> str:
    """
    Take a screenshot of the current page.
    Saves to AGENT_TEMP_PATH/screenshots/ and returns the file path.
    """
    if not _PLAYWRIGHT_AVAILABLE:
        return "Error: playwright not installed. Run: pip install playwright && playwright install chromium"
    try:
        return _dispatch(_async_screenshot(session_id))
    except Exception as e:
        return f"Error: browser_screenshot failed: {e}"


def browser_close_session(session_id: str = "default") -> str:
    """Close and clean up a browser session, freeing all browser resources."""
    if not _PLAYWRIGHT_AVAILABLE:
        session = _pop_session(session_id)
        return f"No active session '{session_id}'." if session is None else f"Session '{session_id}' closed."
    try:
        return _dispatch(_async_close_session(session_id))
    except Exception as e:
        return f"Error: browser_close_session failed: {e}"


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
        logger.info(
            "browser: fast-path extraction thin (%d chars), trying browser fallback",
            len(extracted or ""),
        )
    else:
        logger.info("browser: fast-path blocked (HTTP %s), trying browser fallback", response.status_code)

    return _browser_fallback(url)


def _browser_fallback(url: str) -> str:
    """One-shot browser fetch: navigate, extract, close session immediately."""
    if not _PLAYWRIGHT_AVAILABLE:
        return (
            "Error: playwright not installed — cannot use browser fallback. "
            "Run: pip install playwright && playwright install chromium"
        )
    session_id = f"_fallback_{int(time.time())}"
    try:
        result = browser_navigate(url, session_id=session_id)
    except Exception as e:
        result = f"Error: browser fallback failed: {e}"
    finally:
        # Always clean up, even if navigate raised — ignore errors here
        try:
            browser_close_session(session_id)
        except Exception:
            pass
    return result
