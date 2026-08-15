"""
scheduler.py — Persistent task scheduler for Sovereign-Link.

Tasks are stored in {VAULT_PATH}/.system/scheduled_tasks.json and executed
by a background daemon thread that polls every 60 seconds.

Execution dispatches directly to TOOL_HANDLERS, so any registered tool
(spawn_agent, write_vault, search_vault_semantic, …) can be scheduled.
"""
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_VAULT_PATH = os.environ.get("VAULT_PATH", "/home/wouter/Documents/fractalisme-vault")
_TASKS_PATH = os.path.join(_VAULT_PATH, ".system", "scheduled_tasks.json")
_POLL_INTERVAL = 60  # seconds


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(s: str) -> datetime:
    """Parse ISO 8601 string and return UTC datetime.

    Naive datetimes (no offset) are interpreted as the user's configured
    timezone (from timezone_manager), NOT the server's system timezone.
    Explicit offsets and the 'Z' suffix are always honoured as-is.
    """
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        from timezone_manager import get_zoneinfo
        dt = dt.replace(tzinfo=get_zoneinfo())
    return dt.astimezone(timezone.utc)


def _fmt_local(dt: datetime) -> str:
    return dt.astimezone().strftime("%d-%m-%Y %H:%M")


class _Scheduler:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    def _ensure_dir(self) -> None:
        os.makedirs(os.path.dirname(_TASKS_PATH), exist_ok=True)

    def _load(self) -> list[dict]:
        self._ensure_dir()
        if not os.path.exists(_TASKS_PATH):
            return []
        try:
            with open(_TASKS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            logger.exception("Scheduler: failed to load tasks")
            return []

    def _save(self, tasks: list[dict]) -> None:
        self._ensure_dir()
        try:
            with open(_TASKS_PATH, "w", encoding="utf-8") as f:
                json.dump(tasks, f, indent=2, ensure_ascii=False)
        except Exception:
            logger.exception("Scheduler: failed to save tasks")

    # ------------------------------------------------------------------
    # Public API (called by tool handlers)
    # ------------------------------------------------------------------

    def add_task(
        self,
        execution_time: str,
        action: str,
        parameters: dict,
        description: str = "",
    ) -> str:
        try:
            exec_dt = _parse_dt(execution_time)
        except ValueError as e:
            return f"Error: invalid execution_time '{execution_time}': {e}"

        task_id = str(uuid.uuid4())[:8]
        task = {
            "task_id": task_id,
            "description": description or action,
            "created_at": _now_utc().isoformat(),
            "execution_time": exec_dt.isoformat(),
            "action": action,
            "parameters": parameters,
            "status": "pending",
            "result": None,
            "error": None,
            "executed_at": None,
        }

        with self._lock:
            tasks = self._load()
            tasks.append(task)
            self._save(tasks)

        return f"Ingepland `{task_id}`: **{task['description']}** om {_fmt_local(exec_dt)}"

    def list_tasks(self, include_done: bool = False) -> str:
        with self._lock:
            tasks = self._load()

        if not tasks:
            return "Geen geplande taken."

        if not include_done:
            tasks = [t for t in tasks if t["status"] == "pending"]
        if not tasks:
            return "Geen openstaande taken."

        _icon = {"pending": "⏳", "completed": "✅", "failed": "❌", "cancelled": "🚫"}
        lines = []
        for t in sorted(tasks, key=lambda x: x["execution_time"]):
            icon = _icon.get(t["status"], "?")
            local_time = _fmt_local(_parse_dt(t["execution_time"]))
            lines.append(f"{icon} `{t['task_id']}` — {t['description']} → {local_time}")
            if t["status"] == "failed" and t.get("error"):
                lines.append(f"   ↳ {t['error'][:120]}")
        return "\n".join(lines)

    def cancel_task(self, task_id: str) -> str:
        with self._lock:
            tasks = self._load()
            for t in tasks:
                if t["task_id"] == task_id:
                    if t["status"] != "pending":
                        return f"Taak `{task_id}` is al '{t['status']}' en kan niet worden geannuleerd."
                    t["status"] = "cancelled"
                    self._save(tasks)
                    return f"Taak `{task_id}` geannuleerd."
        return f"Taak `{task_id}` niet gevonden."

    # ------------------------------------------------------------------
    # Execution loop
    # ------------------------------------------------------------------

    def _fire(self, task: dict) -> tuple[bool, str]:
        """Dispatch a task to TOOL_HANDLERS. Returns (success, message)."""
        try:
            # Late import inside try to avoid silent fail if import raises
            from tools import TOOL_HANDLERS
            action = task["action"]
            if action not in TOOL_HANDLERS:
                return False, f"Unknown action '{action}' — not in TOOL_HANDLERS"
            result = TOOL_HANDLERS[action](task["parameters"])
            return True, str(result)[:500]
        except Exception as e:
            logger.exception("Scheduler: error executing task %s", task["task_id"])
            return False, str(e)[:300]

    def _tick(self) -> None:
        now = _now_utc()

        # Phase 1: identify due tasks (under lock, no tool execution)
        with self._lock:
            tasks = self._load()
            due = []
            for task in tasks:
                if task["status"] != "pending":
                    continue
                try:
                    exec_dt = _parse_dt(task["execution_time"])
                except Exception:
                    continue
                if exec_dt <= now:
                    due.append(task)

        if not due:
            return

        # Phase 2: fire tasks WITHOUT holding the lock
        # (prevents deadlock when a tool handler calls back into the scheduler)
        results: dict[str, tuple[bool, str]] = {}
        for task in due:
            logger.info(
                "Scheduler: firing task %s — %s",
                task["task_id"], task["description"],
            )
            try:
                success, output = self._fire(task)
            except Exception as e:
                # _fire() should never raise, but guard anyway so task gets marked failed
                logger.exception("Scheduler: unexpected error firing task %s", task["task_id"])
                success, output = False, str(e)[:300]
            results[task["task_id"]] = (success, output)

        # Phase 3: persist results (under lock, re-read to catch concurrent changes)
        with self._lock:
            tasks = self._load()
            changed = False
            for task in tasks:
                if task["task_id"] not in results:
                    continue
                success, output = results[task["task_id"]]
                task["status"] = "completed" if success else "failed"
                task["executed_at"] = now.isoformat()
                if success:
                    task["result"] = output
                else:
                    task["error"] = output
                changed = True
                logger.info(
                    "Scheduler: task %s %s",
                    task["task_id"],
                    "completed" if success else f"FAILED: {output[:80]}",
                )
            if changed:
                self._save(tasks)

    def _run_loop(self) -> None:
        logger.info("Scheduler: loop started, polling every %ds", _POLL_INTERVAL)
        while True:
            try:
                self._tick()
            except Exception:
                logger.exception("Scheduler: unhandled error in tick")
            time.sleep(_POLL_INTERVAL)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="Scheduler"
        )
        self._thread.start()
        logger.info("Scheduler: daemon thread started")


# Module-level singleton
scheduler = _Scheduler()
