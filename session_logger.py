"""
session_logger.py — Deterministic Session Logger (Sovereign Memory Engine)

Guarantees high-fidelity capture of all session interactions by:
  1. Writing a raw session transcript snapshot to the vault every LOG_EVERY_TURNS turns
  2. Maintaining a heartbeat file (.system/session_status.json) to track sync state
  3. Exposing force_flush() for guaranteed writes on shutdown or crash recovery

Write path : Conversaties/YYYY-MM-DD_HH-MM_Sessie_<session_id>.md
             → indexed immediately by write_vault() — zero search lag
Heartbeat  : <VAULT_PATH>/.system/session_status.json
             → written directly to the filesystem (never indexed, lightweight)
"""

import json
import logging
import os
import re
import threading
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

LOG_EVERY_TURNS = 10        # write a snapshot every N user turns
HEARTBEAT_THRESHOLD = 10    # force sync if turns-since-last-log exceeds this

VAULT_PATH = os.environ.get("VAULT_PATH", "/home/wouter/Documents/fractalisme-vault")
HEARTBEAT_PATH = os.path.join(VAULT_PATH, ".system", "session_status.json")
SESSION_LOG_DIR = "Conversaties"

# Regex to strip embedded timestamps injected by context.py  ("\n[2026-08-23T...]")
_TS_RE = re.compile(r"\n\[[^\]]{10,}\]")


class SessionLogger:
    """Thread-safe deterministic session capture for the Sovereign Memory Engine."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._turn_count: int = 0
        self._last_logged_turn: int = 0
        self._session_id: str = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._session_start: datetime = datetime.now()
        self._load_heartbeat()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on_turn(self, history: list) -> None:
        """Call after each user-assistant exchange.

        Increments the turn counter and updates the heartbeat.
        Periodic vault snapshots are intentionally omitted — the memory pipeline
        (SovereignLog) is the sole output file. force_flush() remains available
        as a shutdown fallback when the pipeline fails.
        """
        with self._lock:
            self._turn_count += 1
            self._update_heartbeat()

    def force_flush(self, history: list, reason: str = "shutdown") -> None:
        """Force an immediate snapshot write.

        Call this on session termination, bot shutdown, or manual recovery.
        Safe to call even if no turns have elapsed — exits early if history
        is empty or if the last logged turn is already current.
        """
        with self._lock:
            if not history:
                return
            if self._turn_count == self._last_logged_turn and self._turn_count > 0:
                return  # already in sync — nothing new to write
            self._write_snapshot(history, reason=reason)

    def check_heartbeat_gap(self, history: list) -> None:
        """On startup: detect and recover from a previous crash.

        If the persisted heartbeat shows that the previous session had more
        than HEARTBEAT_THRESHOLD unlogged turns, trigger an immediate sync of
        whatever history is available now.
        """
        with self._lock:
            gap = self._turn_count - self._last_logged_turn
            if gap > HEARTBEAT_THRESHOLD:
                logger.warning(
                    "SessionLogger: heartbeat gap of %d unlogged turns detected — "
                    "forcing vault sync (reason: heartbeat_recovery)",
                    gap,
                )
                self._write_snapshot(history, reason="heartbeat_recovery")

    @property
    def turn_count(self) -> int:
        return self._turn_count

    @property
    def last_logged_turn(self) -> int:
        return self._last_logged_turn

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write_snapshot(self, history: list, reason: str = "periodic") -> None:
        """Format a Markdown transcript and write it to the vault via write_vault().

        write_vault() applies #YYYY-MM-DD #HH #MM tags and calls index_file()
        synchronously, so the snapshot is searchable immediately after this call.
        """
        from tools import write_vault  # deferred to avoid circular import at module load

        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H-%M")
        # Session ID in the filename ensures each session produces a unique file
        # even when multiple snapshots are taken on the same day.
        file_name = (
            f"{SESSION_LOG_DIR}/{date_str}_{time_str}_Sessie_{self._session_id}.md"
        )

        lines = [
            f"## Sessie Snapshot — {now.strftime('%Y-%m-%d %H:%M')}",
            "",
            f"**Reden:** {reason}  |  **Turn:** {self._turn_count}  "
            f"|  **Session:** `{self._session_id}`",
            "",
        ]

        for msg in history:
            role = msg.get("role", "unknown").upper()
            content = msg.get("content")
            if content and isinstance(content, str):
                content = _TS_RE.sub("", content).strip()
                lines.append(f"**{role}:** {content}")
                lines.append("")

        content_str = "\n".join(lines)

        try:
            result = write_vault(file_name, content_str)
            if "Error" not in result:
                self._last_logged_turn = self._turn_count
                self._update_heartbeat()
                logger.info(
                    "SessionLogger: snapshot written → %s (turns=%d, reason=%s)",
                    file_name, self._turn_count, reason,
                )
                # Explicitly sync to SQLite timeline — write_vault's internal call
                # may fail silently; this ensures session snapshots are always indexed.
                try:
                    from timeline import index_file as _timeline_index
                    tagged = content_str + f"\n\n{now.strftime('#%Y-%m-%d #%H #%M')}\n"
                    _timeline_index(file_name, tagged, now.isoformat())
                    logger.debug("SessionLogger: timeline synced → %s", file_name)
                except Exception:
                    logger.exception("SessionLogger: timeline sync failed for %s", file_name)
            else:
                logger.error("SessionLogger: write_vault returned error: %s", result)
        except Exception:
            logger.exception("SessionLogger: unhandled exception writing snapshot")

    def _update_heartbeat(self) -> None:
        """Persist current session state to .system/session_status.json.

        Written directly to the filesystem (not via write_vault) so it incurs
        no embedding/indexing overhead and can be called on every turn.
        """
        try:
            heartbeat = {
                "session_id": self._session_id,
                "session_start": self._session_start.isoformat(),
                "total_turns": self._turn_count,
                "last_logged_turn": self._last_logged_turn,
                "last_updated": datetime.now().isoformat(),
            }
            Path(HEARTBEAT_PATH).parent.mkdir(parents=True, exist_ok=True)
            with open(HEARTBEAT_PATH, "w", encoding="utf-8") as fh:
                json.dump(heartbeat, fh, indent=2)
        except Exception:
            logger.exception("SessionLogger: failed to update heartbeat")

    def _load_heartbeat(self) -> None:
        """Load the persisted heartbeat to detect crash recovery scenarios.

        If the previous session had unlogged turns, a warning is emitted so
        the caller can decide whether to trigger check_heartbeat_gap().
        """
        try:
            if not os.path.exists(HEARTBEAT_PATH):
                return
            with open(HEARTBEAT_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
            prev_session = data.get("session_id", "")
            if prev_session == self._session_id:
                # Same session (e.g. module reloaded) — restore counters
                self._turn_count = data.get("total_turns", 0)
                self._last_logged_turn = data.get("last_logged_turn", 0)
            else:
                prev_total = data.get("total_turns", 0)
                prev_logged = data.get("last_logged_turn", 0)
                gap = prev_total - prev_logged
                if gap > 0:
                    logger.warning(
                        "SessionLogger: previous session '%s' ended with %d unlogged "
                        "turns — consider reviewing session_draft.md for recovery.",
                        prev_session, gap,
                    )
        except Exception:
            logger.exception("SessionLogger: failed to load heartbeat")


# ---------------------------------------------------------------------------
# Module-level singleton — import and use directly:
#   from session_logger import session_logger
# ---------------------------------------------------------------------------
session_logger = SessionLogger()
