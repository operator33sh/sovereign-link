"""
test_scheduler.py — Verifies the task scheduler end-to-end.

Run with:  python test_scheduler.py
"""
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(__file__))

# Point at a temp vault so tests don't pollute the real vault
import tempfile
_tmpdir = tempfile.mkdtemp()
os.environ.setdefault("VAULT_PATH", _tmpdir)

import scheduler as sched_mod
from scheduler import _Scheduler, _parse_dt, _now_utc

_PASS = "\033[32mPASS\033[0m"
_FAIL = "\033[31mFAIL\033[0m"
_results = []

def check(name: str, condition: bool, detail: str = "") -> None:
    status = _PASS if condition else _FAIL
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
    _results.append(condition)


# ------------------------------------------------------------------
# 1. Parse datetimes
# ------------------------------------------------------------------
print("\n1. Datetime parsing")
utc_dt = _parse_dt("2026-08-16T03:00:00Z")
check("UTC with Z", utc_dt.tzinfo is not None and utc_dt.hour == 3)

naive_dt = _parse_dt("2026-08-16T03:00:00")
check("Naive treated as local", naive_dt.tzinfo is not None)

iso_offset = _parse_dt("2026-08-16T03:00:00+02:00")
check("ISO with offset", iso_offset.hour == 1)  # 03:00+02:00 = 01:00 UTC


# ------------------------------------------------------------------
# 2. Add and list tasks
# ------------------------------------------------------------------
print("\n2. add_task / list_tasks")
s = _Scheduler()

future = (_now_utc() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
result = s.add_task(
    execution_time=future,
    action="write_vault",
    parameters={"file_name": "test/hello.md", "content": "hello"},
    description="Test write",
)
check("add_task returns task_id string", "Ingepland" in result, result)
task_id = result.split("`")[1]  # extract id between backticks

listing = s.list_tasks()
check("list_tasks shows pending task", task_id in listing, listing[:80])


# ------------------------------------------------------------------
# 3. Cancel a task
# ------------------------------------------------------------------
print("\n3. cancel_task")
cancel_result = s.cancel_task(task_id)
check("cancel returns confirmation", task_id in cancel_result, cancel_result)

listing_after = s.list_tasks()
check("cancelled task no longer in pending list", task_id not in listing_after)

listing_all = s.list_tasks(include_done=True)
check("cancelled task visible in include_done=True", task_id in listing_all)


# ------------------------------------------------------------------
# 4. Task execution (fire a task that's due NOW)
# ------------------------------------------------------------------
print("\n4. Task execution")

# Inject a fake TOOL_HANDLERS module so we don't need the full stack
import types, sys
fired_calls = []
fake_tools = types.ModuleType("tools")
fake_tools.TOOL_HANDLERS = {"_test_action": lambda args: fired_calls.append(args) or "ok"}
sys.modules["tools"] = fake_tools

past = (_now_utc() - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
s2 = _Scheduler()
add_result = s2.add_task(
    execution_time=past,
    action="_test_action",
    parameters={"key": "value"},
    description="Immediate test",
)
tid2 = add_result.split("`")[1]

# Manually tick (don't wait 60s)
s2._tick()

# Check storage updated
tasks = s2._load()
task_record = next((t for t in tasks if t["task_id"] == tid2), None)
check("Task marked completed", task_record and task_record["status"] == "completed",
      task_record["status"] if task_record else "not found")
check("Tool was called with correct args", len(fired_calls) == 1 and fired_calls[0] == {"key": "value"})
check("Result stored", task_record and task_record["result"] == "ok")

# Remove fake module so subsequent tests are unaffected
sys.modules.pop("tools", None)


# ------------------------------------------------------------------
# 5. Invalid execution_time
# ------------------------------------------------------------------
print("\n5. Invalid execution_time")
bad = s.add_task("not-a-date", "write_vault", {})
check("Invalid time returns error string", bad.startswith("Error:"), bad)


# ------------------------------------------------------------------
# 6. Unknown action fires gracefully
# ------------------------------------------------------------------
print("\n6. Unknown action")

# Inject an empty handlers dict so _fire sees no matching action
fake_tools3 = types.ModuleType("tools")
fake_tools3.TOOL_HANDLERS = {}
sys.modules["tools"] = fake_tools3

s3 = _Scheduler()
past2 = (_now_utc() - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
s3.add_task(past2, "nonexistent_tool", {}, "Should fail gracefully")
s3._tick()
tasks3 = s3._load()
failed = [t for t in tasks3 if t["status"] == "failed"]
check("Unknown action marks task as failed", len(failed) == 1)
check("Error message stored", "nonexistent_tool" in (failed[0].get("error") or ""))

sys.modules.pop("tools", None)


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
