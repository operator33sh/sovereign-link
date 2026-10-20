"""Tool: get_session_status — real-time session dashboard for Luna.

Returns the session parameters: last ACL sync.
"""

import os
import re


def get_session_status(_args: dict = None) -> str:
    import json

    result: dict = {}

    # ── Session parameters ───────────────────────────────────────────────────
    try:
        from tools.vault import RUNTIME_PATH

        # Read last_sync from ACL file
        last_sync = "onbekend"
        acl_path = os.path.join(RUNTIME_PATH, "active_briefing.md")
        try:
            with open(acl_path, encoding="utf-8") as f:
                acl_content = f.read()
            m = re.search(r"_Last updated:\s*([^\n_]+)_", acl_content)
            if m:
                last_sync = m.group(1).strip()
        except FileNotFoundError:
            last_sync = "geen ACL actief"

        result["session"] = {
            "last_acl_sync": last_sync,
        }
    except Exception as exc:
        result["session"] = {"error": str(exc)}

    return json.dumps(result, ensure_ascii=False, indent=2)


DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_session_status",
            "description": (
                "Geeft een overzicht van de huidige sessie: "
                "wanneer de ACL voor het laatst gesynchroniseerd is."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }
]

HANDLERS = {
    "get_session_status": lambda args: get_session_status(args),
}
