"""
automations.py — Contextual Triggers & Automations Engine for Sovereign-Link.

Automation definitions are stored in {VAULT_PATH}/.system/automations.json.
Execution logs are written to {VAULT_PATH}/.system/automation_logs.json.

The engine runs as a background daemon thread, polling every 60 seconds.
Cron expressions follow standard 5-field syntax: minute hour dom month dow.
Interval automations specify a frequency in seconds.

Idempotency: each automation stores `last_fired` as a minute-bucket string
(YYYY-MM-DDTHH:MM in local time) so it never fires twice in the same minute.
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
_AUTOMATIONS_PATH = os.path.join(_VAULT_PATH, ".system", "automations.json")
_LOGS_PATH = os.path.join(_VAULT_PATH, ".system", "automation_logs.json")
_POLL_INTERVAL = 60  # seconds
_MAX_LOG_ENTRIES = 500


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_local() -> datetime:
    """Current time in the user's configured timezone."""
    try:
        from timezone_manager import get_zoneinfo
        return datetime.now(get_zoneinfo())
    except Exception:
        return datetime.now(timezone.utc)


def _minute_bucket(dt: datetime) -> str:
    """YYYY-MM-DDTHH:MM string used as idempotency key."""
    return dt.strftime("%Y-%m-%dT%H:%M")


def _match_cron_basic(schedule: str, now: datetime) -> bool:
    """
    Minimal 5-field cron matcher (minute hour dom month dow) used as fallback
    when croniter is not installed. Supports *, ranges (a-b), steps (*/n), and lists (a,b).
    dow: 0=Monday … 6=Sunday (Python convention).
    """
    fields = schedule.strip().split()
    if len(fields) != 5:
        return False
    minute_f, hour_f, dom_f, month_f, dow_f = fields

    def _match(value: int, field: str) -> bool:
        if field == "*":
            return True
        if "," in field:
            return any(_match(value, p) for p in field.split(","))
        if "/" in field:
            parts = field.split("/", 1)
            step = int(parts[1])
            start = 0 if parts[0] == "*" else int(parts[0])
            return value >= start and (value - start) % step == 0
        if "-" in field:
            a, b = map(int, field.split("-", 1))
            return a <= value <= b
        return value == int(field)

    return (
        _match(now.minute, minute_f)
        and _match(now.hour, hour_f)
        and _match(now.day, dom_f)
        and _match(now.month, month_f)
        and _match(now.weekday(), dow_f)
    )


# ---------------------------------------------------------------------------
# AutomationEngine
# ---------------------------------------------------------------------------

class AutomationEngine:
    """
    Background engine that fires automation actions when their trigger conditions
    are met. Runs as a daemon thread; never blocks the main chat loop.

    Schema for each entry in automations.json:
    {
        "id":           str,        # 8-char uuid fragment
        "name":         str,        # human label
        "trigger_type": str,        # "cron" | "interval"
        "schedule":     str,        # cron expr OR seconds (for interval)
        "action":       str,        # TOOL_HANDLERS key, e.g. "spawn_agent"
        "parameters":   dict,       # arguments passed verbatim to the action
        "enabled":      bool,
        "last_fired":   str | null, # YYYY-MM-DDTHH:MM in local time (idempotency)
        "created_at":   str         # ISO 8601 UTC
    }
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    def _ensure_dir(self) -> None:
        os.makedirs(os.path.dirname(_AUTOMATIONS_PATH), exist_ok=True)

    def _load(self) -> list[dict]:
        self._ensure_dir()
        if not os.path.exists(_AUTOMATIONS_PATH):
            return []
        try:
            with open(_AUTOMATIONS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            logger.exception("AutomationEngine: failed to load automations.json")
            return []

    def _save(self, automations: list[dict]) -> None:
        self._ensure_dir()
        try:
            with open(_AUTOMATIONS_PATH, "w", encoding="utf-8") as f:
                json.dump(automations, f, indent=2, ensure_ascii=False)
        except Exception:
            logger.exception("AutomationEngine: failed to save automations.json")

    def _append_log(
        self, automation_id: str, name: str, status: str, message: str
    ) -> None:
        self._ensure_dir()
        entry = {
            "automation_id": automation_id,
            "name": name,
            "status": status,
            "message": message[:600],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        logs: list[dict] = []
        if os.path.exists(_LOGS_PATH):
            try:
                with open(_LOGS_PATH, "r", encoding="utf-8") as f:
                    logs = json.load(f)
            except Exception:
                logs = []
        logs.append(entry)
        logs = logs[-_MAX_LOG_ENTRIES:]
        try:
            with open(_LOGS_PATH, "w", encoding="utf-8") as f:
                json.dump(logs, f, indent=2, ensure_ascii=False)
        except Exception:
            logger.exception("AutomationEngine: failed to write automation_logs.json")

    # ------------------------------------------------------------------
    # Trigger matching
    # ------------------------------------------------------------------

    def _is_due(self, automation: dict, now: datetime) -> bool:
        """Return True if the automation should fire at `now`."""
        trigger_type = automation.get("trigger_type", "cron")
        schedule = str(automation.get("schedule", ""))
        last_fired = automation.get("last_fired")
        bucket = _minute_bucket(now)

        # Idempotency: never fire twice in the same minute
        if last_fired == bucket:
            return False

        if trigger_type == "cron":
            try:
                from croniter import croniter
                return croniter.match(schedule, now)
            except ImportError:
                return _match_cron_basic(schedule, now)
            except Exception:
                logger.warning(
                    "AutomationEngine: cron parse error for '%s' (id=%s)",
                    schedule, automation.get("id"),
                )
                return False

        elif trigger_type == "interval":
            try:
                interval_secs = int(schedule)
            except (ValueError, TypeError):
                return False
            if not last_fired:
                return True
            try:
                last_dt = datetime.strptime(last_fired, "%Y-%m-%dT%H:%M")
                last_dt = last_dt.replace(tzinfo=now.tzinfo)
                return (now - last_dt).total_seconds() >= interval_secs
            except Exception:
                return True

        return False

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _fire(self, automation: dict) -> tuple[bool, str]:
        """Dispatch an automation to TOOL_HANDLERS. Returns (success, message)."""
        from tools import TOOL_HANDLERS

        action = automation["action"]
        params = dict(automation.get("parameters", {}))

        # For spawn_agent: prefix goal with automation name for traceability
        if action == "spawn_agent":
            if "goal" in params:
                params["goal"] = f"[Automation: {automation['name']}] {params['goal']}"
            if "agent_name" not in params:
                safe_name = "".join(
                    c for c in automation["name"] if c.isalnum() or c in "-_"
                )
                params["agent_name"] = f"{safe_name}Agent"

        handler = TOOL_HANDLERS.get(action)
        if not handler:
            return False, f"Unknown action '{action}' — not registered in TOOL_HANDLERS"
        try:
            result = handler(params)
            return True, str(result)[:500]
        except Exception as e:
            logger.exception(
                "AutomationEngine: error firing automation '%s'", automation["name"]
            )
            return False, str(e)[:300]

    def _tick(self) -> None:
        now = _now_local()
        bucket = _minute_bucket(now)

        # Phase 1: identify due automations (under lock)
        with self._lock:
            automations = self._load()

        due = [
            a for a in automations
            if a.get("enabled", True) and self._is_due(a, now)
        ]
        if not due:
            return

        # Phase 2: fire WITHOUT holding lock (prevents deadlock if action calls back)
        results: dict[str, tuple[bool, str]] = {}
        for auto in due:
            logger.info(
                "AutomationEngine: firing '%s' (id=%s)", auto["name"], auto["id"]
            )
            success, output = self._fire(auto)
            results[auto["id"]] = (success, output)
            self._append_log(
                auto["id"], auto["name"],
                "success" if success else "error", output,
            )
            if not success:
                try:
                    from tools import write_notification
                    write_notification(
                        content=f"Automation '{auto['name']}' mislukt: {output[:200]}",
                        agent_id="AutomationEngine",
                        priority="high",
                        category="alert",
                    )
                except Exception:
                    logger.exception(
                        "AutomationEngine: could not push failure notification for '%s'",
                        auto["name"],
                    )

        # Phase 3: persist last_fired (under lock, re-read to avoid overwriting concurrent changes)
        with self._lock:
            automations = self._load()
            changed = False
            for auto in automations:
                if auto["id"] in results:
                    auto["last_fired"] = bucket
                    changed = True
            if changed:
                self._save(automations)

    def _run_loop(self) -> None:
        logger.info("AutomationEngine: loop started (poll every %ds)", _POLL_INTERVAL)
        while True:
            try:
                self._tick()
            except Exception:
                logger.exception("AutomationEngine: unhandled error in tick")
            time.sleep(_POLL_INTERVAL)

    def start(self) -> None:
        """Start the background polling thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="AutomationEngine"
        )
        self._thread.start()
        logger.info("AutomationEngine: daemon thread started")

    # ------------------------------------------------------------------
    # Public API — called by Luna tool handlers
    # ------------------------------------------------------------------

    def list_automations(self) -> str:
        with self._lock:
            automations = self._load()

        if not automations:
            return (
                "Geen automations gedefinieerd. "
                "Gebruik `create_automation` om er een toe te voegen."
            )

        try:
            from croniter import croniter as _ci
            _has_croniter = True
        except ImportError:
            _has_croniter = False

        now = _now_local()
        lines = ["**Automations:**\n"]
        for a in automations:
            icon = "✅" if a.get("enabled", True) else "⏸"
            trigger = a.get("trigger_type", "cron")
            schedule = a.get("schedule", "")
            last = a.get("last_fired") or "nooit"

            next_run = "?"
            if trigger == "cron" and _has_croniter:
                try:
                    nxt = _ci(schedule, now).get_next(datetime)
                    next_run = nxt.strftime("%d-%m-%Y %H:%M")
                except Exception:
                    next_run = schedule
            elif trigger == "interval":
                next_run = f"elke {schedule}s"

            lines.append(
                f"{icon} `{a['id']}` **{a['name']}**\n"
                f"   Trigger: `{trigger}` `{schedule}` | "
                f"Actie: `{a['action']}` | "
                f"Volgende: {next_run} | "
                f"Laatste: {last}"
            )
        return "\n".join(lines)

    def create_automation(
        self,
        name: str,
        trigger_type: str,
        schedule: str,
        action: str,
        parameters: dict,
        enabled: bool = True,
    ) -> str:
        if trigger_type not in ("cron", "interval"):
            return (
                f"Error: trigger_type moet 'cron' of 'interval' zijn, "
                f"niet '{trigger_type}'"
            )

        # Validate cron expression early
        if trigger_type == "cron":
            try:
                from croniter import croniter
                if not croniter.is_valid(schedule):
                    return f"Error: ongeldige cron-expressie '{schedule}'"
            except ImportError:
                pass  # basic matcher will handle it at runtime

        auto_id = str(uuid.uuid4())[:8]
        automation = {
            "id": auto_id,
            "name": name,
            "trigger_type": trigger_type,
            "schedule": schedule,
            "action": action,
            "parameters": parameters,
            "enabled": enabled,
            "last_fired": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            automations = self._load()
            automations.append(automation)
            self._save(automations)

        state = "ingeschakeld" if enabled else "uitgeschakeld"
        return (
            f"Automation `{auto_id}` **{name}** aangemaakt ({state}).\n"
            f"Trigger: `{trigger_type}` `{schedule}` → `{action}`"
        )

    def toggle_automation(self, automation_id: str, enabled: bool) -> str:
        with self._lock:
            automations = self._load()
            for a in automations:
                if a["id"] == automation_id:
                    a["enabled"] = enabled
                    self._save(automations)
                    state = "ingeschakeld" if enabled else "uitgeschakeld"
                    return (
                        f"Automation `{automation_id}` ({a['name']}) "
                        f"is nu **{state}**."
                    )
        return f"Automation `{automation_id}` niet gevonden."


# Module-level singleton
automation_engine = AutomationEngine()
