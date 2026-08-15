"""
test_notifications.py — Validates the notification queue end-to-end.

Run with:  python3 test_notifications.py
"""
import os
import sys
import tempfile

_tmpdir = tempfile.mkdtemp()
os.environ["VAULT_PATH"] = _tmpdir

sys.path.insert(0, os.path.dirname(__file__))

from notifications import _NotificationManager

_PASS = "\033[32mPASS\033[0m"
_FAIL = "\033[31mFAIL\033[0m"
_results = []

def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    _results.append(ok)


# ------------------------------------------------------------------
# 1. Write and retrieve a single notification
# ------------------------------------------------------------------
print("\n1. write + get_pending")
nm = _NotificationManager()

result = nm.write("Patroonanalyse afgerond.", agent_id="AngerAnalysisAgent",
                  priority="medium", category="insight",
                  related_file="Analyses/Anger_2026-08-16.md")
check("write returns confirmation", "Notificatie opgeslagen" in result, result)

pending = nm.get_pending()
check("get_pending returns content", "Patroonanalyse afgerond" in pending, pending[:120])
check("get_pending shows agent_id", "AngerAnalysisAgent" in pending)
check("get_pending shows related_file", "Analyses/Anger_2026-08-16.md" in pending)


# ------------------------------------------------------------------
# 2. Delivered notifications not returned twice
# ------------------------------------------------------------------
print("\n2. Delivered-once guarantee")
second_call = nm.get_pending()
check("Second call returns empty", "Geen openstaande" in second_call, second_call)


# ------------------------------------------------------------------
# 3. Priority ordering: high before medium before low
# ------------------------------------------------------------------
print("\n3. Priority ordering")
nm2 = _NotificationManager()
nm2.write("Low msg", priority="low", category="system")
nm2.write("High msg", priority="high", category="alert")
nm2.write("Medium msg", priority="medium", category="insight")

ordered = nm2.get_pending()
hi = ordered.index("High msg")
med = ordered.index("Medium msg")
lo = ordered.index("Low msg")
check("high before medium", hi < med, f"positions: high={hi} medium={med}")
check("medium before low", med < lo, f"positions: medium={med} low={lo}")


# ------------------------------------------------------------------
# 4. Invalid priority / category are coerced gracefully
# ------------------------------------------------------------------
print("\n4. Invalid priority / category coercion")
nm3 = _NotificationManager()
nm3.write("Test", priority="CRITICAL", category="unknown_type")
raw = nm3._load()
check("priority coerced to medium", raw[-1]["priority"] == "medium")
check("category coerced to insight", raw[-1]["category"] == "insight")


# ------------------------------------------------------------------
# 5. clear_delivered prunes delivered entries
# ------------------------------------------------------------------
print("\n5. clear_delivered")
nm4 = _NotificationManager()
nm4.write("To be delivered", priority="low", category="system")
nm4.get_pending()  # marks as delivered
before = len(nm4._load())
msg = nm4.clear_delivered()
after = len(nm4._load())
check("clear_delivered removes entries", after < before, f"before={before} after={after}")
check("clear_delivered reports count", "verwijderd" in msg, msg)


# ------------------------------------------------------------------
# 6. End-to-end: simulate agent → queue → Luna retrieval
# ------------------------------------------------------------------
print("\n6. End-to-end agent workflow")
nm5 = _NotificationManager()

# Agent completes task and writes notification
nm5.write(
    content="Ik heb de analyse op 'woede' afgerond. Interessant patroon gevonden.",
    agent_id="AngerAnalysisAgent",
    priority="medium",
    category="insight",
    related_file="Analyses/Anger_2026-08-16.md",
)

# Luna retrieves on "Wat is er gebeurd?"
briefing = nm5.get_pending()
check("Briefing contains header", "notificatie" in briefing.lower())
check("Briefing contains analysis insight", "woede" in briefing)
check("Briefing contains file link", "Anger_2026-08-16" in briefing)
check("Priority icon present", "🟡" in briefing)

# Confirm no double delivery
check("No double delivery", "Geen openstaande" in nm5.get_pending())


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
