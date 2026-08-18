"""
notifications.py — Persistent notification queue for Sovereign-Link.

Agents and scheduled tasks write notifications here. Luna reads them
on demand via get_pending_notifications(), which marks them delivered.

Storage: {VAULT_PATH}/.system/notifications.json
"""
import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Callable

logger = logging.getLogger(__name__)

_VAULT_PATH = os.environ.get("VAULT_PATH", "/home/wouter/Documents/fractalisme-vault")
_QUEUE_PATH = os.path.join(_VAULT_PATH, ".system", "notifications.json")

_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
_VALID_PRIORITIES = set(_PRIORITY_ORDER)
_VALID_CATEGORIES = {"insight", "alert", "system", "wellness", "task"}


class _NotificationManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._on_write: Callable | None = None  # set by proactive_dispatcher

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    def _ensure_dir(self) -> None:
        os.makedirs(os.path.dirname(_QUEUE_PATH), exist_ok=True)

    def _load(self) -> list[dict]:
        self._ensure_dir()
        if not os.path.exists(_QUEUE_PATH):
            return []
        try:
            with open(_QUEUE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            logger.exception("NotificationManager: failed to load queue")
            return []

    def _save(self, entries: list[dict]) -> None:
        self._ensure_dir()
        try:
            with open(_QUEUE_PATH, "w", encoding="utf-8") as f:
                json.dump(entries, f, indent=2, ensure_ascii=False)
        except Exception:
            logger.exception("NotificationManager: failed to save queue")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def write(
        self,
        content: str,
        agent_id: str = "system",
        priority: str = "medium",
        category: str = "insight",
        related_file: str | None = None,
    ) -> str:
        if priority not in _VALID_PRIORITIES:
            priority = "medium"
        if category not in _VALID_CATEGORIES:
            category = "insight"

        # Check if we should hold this notification during sleep mode
        held_for_sleep = False
        if priority != "high":
            try:
                from proactive import user_status
                held_for_sleep = user_status.is_sleeping()
            except Exception:
                pass

        entry = {
            "id": str(uuid.uuid4())[:8],
            "agent_id": agent_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "priority": priority,
            "category": category,
            "content": content,
            "status": "pending",
            "related_file": related_file,
            "held_for_sleep": held_for_sleep,
        }

        with self._lock:
            entries = self._load()
            entries.append(entry)
            self._save(entries)

        logger.info(
            "Notification queued [%s/%s] from %s: %s",
            priority, category, agent_id, content[:80],
        )

        # Signal the proactive dispatcher (if registered) without holding the lock
        if self._on_write is not None:
            try:
                self._on_write()
            except Exception:
                logger.exception("NotificationManager: _on_write callback failed")

        return f"Notificatie opgeslagen (id=`{entry['id']}`, prioriteit={priority})"

    def get_pending(self) -> str:
        """Return all pending notifications sorted by priority then timestamp,
        and mark them as delivered atomically."""
        with self._lock:
            entries = self._load()
            pending = [e for e in entries if e["status"] == "pending"]

            if not pending:
                return "Geen openstaande notificaties."

            # Sort: high → medium → low, then by timestamp ascending
            pending.sort(key=lambda e: (
                _PRIORITY_ORDER.get(e["priority"], 99),
                e["timestamp"],
            ))

            # Mark delivered
            ids_delivered = {e["id"] for e in pending}
            for e in entries:
                if e["id"] in ids_delivered:
                    e["status"] = "delivered"
            self._save(entries)

        _icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}
        lines = []
        for e in pending:
            icon = _icon.get(e["priority"], "•")
            ts = datetime.fromisoformat(e["timestamp"]).astimezone().strftime("%d-%m %H:%M")
            line = f"{icon} [{e['category'].upper()}] {e['agent_id']} ({ts}): {e['content']}"
            if e.get("related_file"):
                line += f"\n   → `{e['related_file']}`"
            lines.append(line)

        header = f"*{len(pending)} notificatie(s) terwijl je weg was:*\n\n"
        return header + "\n\n".join(lines)

    def get_morning_briefing(self) -> str:
        """Return a categorised morning briefing of all pending notifications.

        Marks every returned notification as delivered atomically.
        Returns a warm all-clear message when the queue is empty.
        """
        with self._lock:
            entries = self._load()
            pending = [e for e in entries if e["status"] == "pending"]

            if not pending:
                return "🌅 *Goedemorgen!* Niets gemist terwijl je sliep — je bent helemaal bijgewerkt. ☀️"

            # Sort: high → medium → low, then chronological
            pending.sort(key=lambda e: (
                _PRIORITY_ORDER.get(e.get("priority", "medium"), 99),
                e.get("timestamp", ""),
            ))

            # Mark delivered atomically
            delivered_ids = {e["id"] for e in pending}
            for e in entries:
                if e["id"] in delivered_ids:
                    e["status"] = "delivered"
            self._save(entries)

        _icon  = {"high": "🔴", "medium": "🟡", "low": "🟢"}
        _label = {"high": "Urgent", "medium": "Info", "low": "Laag"}

        # Group by priority
        groups: dict[str, list] = {"high": [], "medium": [], "low": []}
        for e in pending:
            groups.setdefault(e.get("priority", "medium"), []).append(e)

        lines: list[str] = []
        for prio in ("high", "medium", "low"):
            items = groups.get(prio, [])
            if not items:
                continue
            lines.append(f"*{_icon[prio]} {_label[prio]}*")
            for e in items:
                ts    = datetime.fromisoformat(e["timestamp"]).astimezone().strftime("%d-%m %H:%M")
                agent = e.get("agent_id", "systeem")
                body  = e.get("content", "")
                line  = f"  • [{e.get('category', 'system').upper()}] *{agent}* ({ts}): {body}"
                if e.get("related_file"):
                    line += f"\n    → `{e['related_file']}`"
                lines.append(line)

        n = len(pending)
        noun = "berichten" if n != 1 else "bericht"
        header = f"🌅 *Goedemorgen! Je hebt {n} {noun} gemist:*\n\n"
        return header + "\n".join(lines)

    def clear_delivered(self) -> str:
        """Prune delivered notifications to keep the file lean."""
        with self._lock:
            entries = self._load()
            before = len(entries)
            entries = [e for e in entries if e["status"] != "delivered"]
            self._save(entries)
        pruned = before - len(entries)
        return f"{pruned} afgeleverde notificatie(s) verwijderd."


# Module-level singleton
notification_manager = _NotificationManager()
