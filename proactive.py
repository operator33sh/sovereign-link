"""
proactive.py — Proactive notification dispatcher for Sovereign-Link.

Monitors the notification queue and pushes messages to Telegram
without requiring user input. Respects sleep mode and rate limits.

Push policy (rule-based, no LLM call):
  priority=high   → always push, even in sleep mode
  priority=medium → push only when user is active (< _AWAY_THRESHOLD_HOURS
                    since last message and not in explicit sleep mode)
  priority=low    → never push proactively; surfaces via get_pending_notifications()

Rate limit:  at most one push per _MIN_PUSH_INTERVAL seconds.
Debounce:    waits _DEBOUNCE_SECS after the first signal to batch rapid bursts.
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Callable

logger = logging.getLogger(__name__)

_AWAY_THRESHOLD_HOURS = 2   # inactive longer than this → considered away
_MIN_PUSH_INTERVAL    = 60  # seconds between proactive pushes (rate limit)
_DEBOUNCE_SECS        = 3   # wait after first signal to batch bursts

_PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}


class UserStatusManager:
    """Tracks last user activity timestamp and explicit sleep-mode flag.

    Stored in vault/.system/user_status.json — path is evaluated at
    call time so tests can override VAULT_PATH at runtime.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    def _config_path(self) -> str:
        vault = os.environ.get("VAULT_PATH", "/home/wouter/Documents/fractalisme-vault")
        return os.path.join(vault, ".system", "user_status.json")

    def _load(self) -> dict:
        path = self._config_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            logger.exception("UserStatusManager: failed to load")
            return {}

    def _save(self, data: dict) -> None:
        path = self._config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception:
            logger.exception("UserStatusManager: failed to save")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update_activity(self, chat_id: int | None = None) -> None:
        """Call on every incoming user message.

        Also clears sleep mode — any message means the user is awake.
        """
        with self._lock:
            data = self._load()
            data["last_activity"] = datetime.now(timezone.utc).isoformat()
            data["sleep_mode"] = False
            if chat_id is not None:
                data["chat_id"] = chat_id
            self._save(data)

    def set_sleep_mode(self, sleeping: bool) -> str:
        with self._lock:
            data = self._load()
            data["sleep_mode"] = sleeping
            data["sleep_set_at"] = datetime.now(timezone.utc).isoformat()
            self._save(data)
        if sleeping:
            return "Slaapstand ingeschakeld. Ik hou notificaties vast tot je terugkomt. 🌙"
        return "Slaapstand uitgeschakeld. Ik ben weer actief. ☀️"

    def get_chat_id(self) -> int | None:
        cid = self._load().get("chat_id")
        return int(cid) if cid is not None else None

    def is_sleeping(self) -> bool:
        return bool(self._load().get("sleep_mode", False))

    def is_active(self) -> bool:
        """True when NOT sleeping and last activity < _AWAY_THRESHOLD_HOURS ago."""
        data = self._load()
        if data.get("sleep_mode"):
            return False
        last = data.get("last_activity")
        if not last:
            return False
        try:
            last_dt = datetime.fromisoformat(last)
            return (datetime.now(timezone.utc) - last_dt) < timedelta(hours=_AWAY_THRESHOLD_HOURS)
        except Exception:
            return False

    def get_status_summary(self) -> str:
        data = self._load()
        sleep = data.get("sleep_mode", False)
        last  = data.get("last_activity", "onbekend")
        if last != "onbekend":
            try:
                last = datetime.fromisoformat(last).astimezone().strftime("%d-%m %H:%M")
            except Exception:
                pass
        return (
            f"Slaapstand: {'🌙 aan' if sleep else '☀️ uit'} | "
            f"Laatste activiteit: {last}"
        )


class ProactiveDispatcher:
    """Daemon that watches for new notifications and pushes them to Telegram."""

    def __init__(self) -> None:
        self._wake           = threading.Event()
        self._last_push_lock = threading.Lock()
        self._last_push: datetime | None = None
        self._send_fn: Callable[[str], None] | None = None
        self._thread: threading.Thread | None = None

    def set_send_fn(self, fn: Callable[[str], None]) -> None:
        """Register a thread-safe send function: fn(markdown_text) → None."""
        self._send_fn = fn

    def notify(self) -> None:
        """Signal that a new notification was written. Called by notification_manager."""
        self._wake.set()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _rate_ok(self) -> bool:
        with self._last_push_lock:
            if self._last_push is None:
                return True
            elapsed = (datetime.now(timezone.utc) - self._last_push).total_seconds()
            return elapsed >= _MIN_PUSH_INTERVAL

    def _dispatch(self) -> None:
        if self._send_fn is None:
            logger.debug("ProactiveDispatcher: no send_fn registered, skipping")
            return

        from notifications import notification_manager

        with notification_manager._lock:
            all_entries = notification_manager._load()
            pending = [e for e in all_entries if e["status"] == "pending"]

        if not pending:
            return

        user_active = user_status.is_active()
        rate_ok     = self._rate_ok()

        push_entries = []
        for e in pending:
            p = e.get("priority", "medium")
            if p == "high":
                push_entries.append(e)                        # always push, bypass rate limit
            elif p == "medium" and user_active and rate_ok:
                push_entries.append(e)                        # push only when active + rate OK
            # low → never push proactively

        if not push_entries:
            logger.debug(
                "ProactiveDispatcher: nothing to push "
                "(active=%s, rate_ok=%s, priorities=%s)",
                user_active, rate_ok,
                [e.get("priority") for e in pending],
            )
            return

        if not push_entries:
            logger.debug(
                "ProactiveDispatcher: nothing to push "
                "(active=%s, priorities=%s)",
                user_active,
                [e.get("priority") for e in pending],
            )
            return

        # Sort: high → medium, then chronological
        push_entries.sort(key=lambda e: (
            _PRIORITY_RANK.get(e.get("priority", "medium"), 99),
            e.get("timestamp", ""),
        ))

        # Format message
        _icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}
        lines = []
        for e in push_entries:
            icon  = _icon.get(e.get("priority", "medium"), "•")
            agent = e.get("agent_id", "systeem")
            body  = e.get("content", "")
            line  = f"{icon} *{agent}:* {body}"
            if e.get("related_file"):
                line += f"\n   → `{e['related_file']}`"
            lines.append(line)

        n = len(push_entries)
        header = "📬 *Nieuw bericht:*\n\n" if n == 1 else f"📬 *{n} nieuwe berichten:*\n\n"
        message = header + "\n\n".join(lines)

        # Mark as delivered atomically before sending
        pushed_ids = {e["id"] for e in push_entries}
        with notification_manager._lock:
            all_entries = notification_manager._load()
            for e in all_entries:
                if e["id"] in pushed_ids:
                    e["status"] = "delivered"
            notification_manager._save(all_entries)

        try:
            self._send_fn(message)
            with self._last_push_lock:
                self._last_push = datetime.now(timezone.utc)
            logger.info("ProactiveDispatcher: pushed %d notification(s)", n)
        except Exception:
            logger.exception("ProactiveDispatcher: send failed")

    def _run_loop(self) -> None:
        logger.info("ProactiveDispatcher: daemon started")
        while True:
            self._wake.wait()
            self._wake.clear()
            time.sleep(_DEBOUNCE_SECS)  # batch rapid bursts into one push
            try:
                self._dispatch()
            except Exception:
                logger.exception("ProactiveDispatcher: unhandled error in dispatch")

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="ProactiveDispatcher"
        )
        self._thread.start()


# Module-level singletons
user_status          = UserStatusManager()
proactive_dispatcher = ProactiveDispatcher()
