"""Browser automation tools."""


def _navigate(url: str, session_id: str) -> str:
    from browser import browser_navigate
    return browser_navigate(url, session_id)

def _click(selector: str, session_id: str) -> str:
    from browser import browser_click
    return browser_click(selector, session_id)

def _extract_content(session_id: str) -> str:
    from browser import browser_extract_content
    return browser_extract_content(session_id)

def _screenshot(session_id: str) -> str:
    from browser import browser_screenshot
    return browser_screenshot(session_id)

def _close_session(session_id: str) -> str:
    from browser import browser_close_session
    return browser_close_session(session_id)


DEFINITIONS = [
    {"type": "function", "function": {"name": "browser_navigate", "description": "Open a URL in a headless browser session.", "parameters": {"type": "object", "properties": {"url": {"type": "string"}, "session_id": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {"name": "browser_click", "description": "Click an element on the current page using a CSS selector.", "parameters": {"type": "object", "properties": {"selector": {"type": "string"}, "session_id": {"type": "string"}}, "required": ["selector"]}}},
    {"type": "function", "function": {"name": "browser_extract_content", "description": "Extract the current page content as Markdown from an active browser session.", "parameters": {"type": "object", "properties": {"session_id": {"type": "string"}}, "required": []}}},
    {"type": "function", "function": {"name": "browser_screenshot", "description": "Take a screenshot of the current page in a browser session.", "parameters": {"type": "object", "properties": {"session_id": {"type": "string"}}, "required": []}}},
    {"type": "function", "function": {"name": "browser_close_session", "description": "Close and clean up a browser session.", "parameters": {"type": "object", "properties": {"session_id": {"type": "string"}}, "required": []}}},
]

HANDLERS = {
    "browser_navigate": lambda args: _navigate(args["url"], args.get("session_id", "default")),
    "browser_click": lambda args: _click(args["selector"], args.get("session_id", "default")),
    "browser_extract_content": lambda args: _extract_content(args.get("session_id", "default")),
    "browser_screenshot": lambda args: _screenshot(args.get("session_id", "default")),
    "browser_close_session": lambda args: _close_session(args.get("session_id", "default")),
}
