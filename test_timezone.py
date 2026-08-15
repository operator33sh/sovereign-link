"""
test_timezone.py — Validates timezone-aware scheduling.

Run with:  python3 test_timezone.py
"""
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

_tmpdir = tempfile.mkdtemp()
os.environ["VAULT_PATH"] = _tmpdir
sys.path.insert(0, os.path.dirname(__file__))

import timezone_manager as tzm
from scheduler import _parse_dt, _now_utc

_results = []

def check(name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
    _results.append(ok)


# ------------------------------------------------------------------
# 1. set_timezone validates IANA strings
# ------------------------------------------------------------------
print("\n1. set_timezone validation")

result = tzm.set_timezone("Europe/Amsterdam")
check("Valid timezone accepted", "Amsterdam" in result, result[:80])

result_bad = tzm.set_timezone("Mars/OlympusMons")
check("Invalid timezone rejected", "Onbekende" in result_bad, result_bad[:80])

# Confirm config persisted
check("get_timezone_name returns set value", tzm.get_timezone_name() == "Europe/Amsterdam")


# ------------------------------------------------------------------
# 2. get_zoneinfo returns correct ZoneInfo object
# ------------------------------------------------------------------
print("\n2. get_zoneinfo")
zi = tzm.get_zoneinfo()
check("Returns ZoneInfo", isinstance(zi, ZoneInfo))
check("Correct key", str(zi.key) == "Europe/Amsterdam")


# ------------------------------------------------------------------
# 3. _parse_dt interprets naive datetime as configured timezone
# ------------------------------------------------------------------
print("\n3. Naive datetime → configured timezone")

# Set server env to UTC to simulate UTC server
os.environ["TZ"] = "UTC"

# Naive "03:00" must be Amsterdam 03:00, not UTC 03:00
naive_str = "2026-08-16T03:00:00"
parsed_utc = _parse_dt(naive_str)

amsterdam_tz = ZoneInfo("Europe/Amsterdam")
expected_local = datetime(2026, 8, 16, 3, 0, 0, tzinfo=amsterdam_tz)
expected_utc = expected_local.astimezone(timezone.utc)

check(
    "Naive time interpreted as Amsterdam time",
    parsed_utc == expected_utc,
    f"got={parsed_utc.isoformat()} expected={expected_utc.isoformat()}",
)

# Explicit UTC offset must NOT be overridden
explicit_utc = _parse_dt("2026-08-16T03:00:00Z")
check(
    "Explicit Z suffix stays UTC",
    explicit_utc.hour == 3 and explicit_utc.tzinfo == timezone.utc,
    explicit_utc.isoformat(),
)

explicit_offset = _parse_dt("2026-08-16T03:00:00+02:00")
check(
    "Explicit +02:00 offset honoured",
    explicit_offset.hour == 1,  # 03:00+02:00 = 01:00 UTC
    explicit_offset.isoformat(),
)


# ------------------------------------------------------------------
# 4. schedule_task "10 minutes from now" fires correctly
# ------------------------------------------------------------------
print("\n4. Scheduler fires at correct UTC time")

import types
fake_tools = types.ModuleType("tools")
fired = []
fake_tools.TOOL_HANDLERS = {"_tz_test": lambda args: fired.append(args) or "ok"}
sys.modules["tools"] = fake_tools

from scheduler import _Scheduler

s = _Scheduler()

# "10 minutes from now" expressed as Amsterdam naive time
amsterdam_now = datetime.now(amsterdam_tz)
ten_min_later_local = amsterdam_now + timedelta(minutes=10)
naive_10min = ten_min_later_local.strftime("%Y-%m-%dT%H:%M:%S")  # no tzinfo

add_result = s.add_task(
    execution_time=naive_10min,
    action="_tz_test",
    parameters={"x": 1},
    description="TZ test task",
)
task_id = add_result.split("`")[1]

# Stored UTC must equal Amsterdam now + 10min in UTC
tasks = s._load()
task = next(t for t in tasks if t["task_id"] == task_id)
stored_utc = _parse_dt(task["execution_time"])
expected_fire_utc = ten_min_later_local.astimezone(timezone.utc).replace(microsecond=0)

delta_seconds = abs((stored_utc - expected_fire_utc).total_seconds())
check("Stored UTC matches Amsterdam+10min", delta_seconds < 2, f"delta={delta_seconds}s")

# Should NOT fire yet (10 min in the future)
s._tick()
check("Task not fired before execution_time", len(fired) == 0)

# Simulate time passing: backdate the task to 1 second ago
task["execution_time"] = (_now_utc() - timedelta(seconds=1)).isoformat()
s._save(tasks)
s._tick()
check("Task fired when execution_time reached", len(fired) == 1)

sys.modules.pop("tools", None)


# ------------------------------------------------------------------
# 5. Different timezone (America/New_York)
# ------------------------------------------------------------------
print("\n5. Timezone switch to America/New_York")
tzm.set_timezone("America/New_York")
check("Timezone updated", tzm.get_timezone_name() == "America/New_York")

ny_tz = ZoneInfo("America/New_York")
parsed_ny = _parse_dt("2026-01-15T09:00:00")  # naive, should be NY time
expected_ny_utc = datetime(2026, 1, 15, 9, 0, 0, tzinfo=ny_tz).astimezone(timezone.utc)
check(
    "Naive time now interpreted as New York time",
    parsed_ny == expected_ny_utc,
    f"got={parsed_ny.isoformat()} expected={expected_ny_utc.isoformat()}",
)

# Reset back to Amsterdam for any downstream tests
tzm.set_timezone("Europe/Amsterdam")


# ------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------
passed = sum(_results)
total = len(_results)
print(f"\n{'='*40}")
print(f"Results: {passed}/{total} passed")
if passed == total:
    print("All tests passed.")
    sys.exit(0)
else:
    print(f"{total - passed} test(s) FAILED.")
    sys.exit(1)
