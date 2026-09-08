"""
tools/ — Sovereign-Link tool registry.

Each submodule exports DEFINITIONS (list[dict]) and HANDLERS (dict[str, callable]).
This __init__ merges them and exposes TOOL_DEFINITIONS, TOOL_HANDLERS,
and AGENT_TOOL_DEFINITIONS used by llm.py and agent.py.
"""

# Re-export path constants and vault helpers that other modules import directly from tools
from tools.vault import (
    VAULT_PATH,
    AGENT_TEMP_PATH,
    PROJECT_LOGS_PATH,
    RUNTIME_PATH,
    sync_vault,
    write_vault,
    generate_time_tag,
)

# Re-export write_blackboard so agent.py monitor can do: from tools import write_blackboard
from tools.agent_tools import write_blackboard

# Re-export write_notification so automations.py can do: from tools import write_notification
from tools.notification_tools import write_notification

from tools.vault import DEFINITIONS as _vd, HANDLERS as _vh
from tools.http import DEFINITIONS as _hd, HANDLERS as _hh
from tools.search import DEFINITIONS as _sd, HANDLERS as _sh
from tools.browser_tools import DEFINITIONS as _bd, HANDLERS as _bh
from tools.agent_tools import DEFINITIONS as _ad, HANDLERS as _ah
from tools.notification_tools import DEFINITIONS as _nd, HANDLERS as _nh
from tools.scheduler_tools import DEFINITIONS as _scd, HANDLERS as _sch
from tools.automation_tools import DEFINITIONS as _aud, HANDLERS as _auh
from tools.personality_tools import DEFINITIONS as _pd, HANDLERS as _ph
from tools.resonance_tools import DEFINITIONS as _rd, HANDLERS as _rh

TOOL_DEFINITIONS: list[dict] = _vd + _hd + _sd + _bd + _ad + _nd + _scd + _aud + _pd + _rd

TOOL_HANDLERS: dict[str, callable] = {
    **_vh, **_hh, **_sh, **_bh, **_ah, **_nh, **_sch, **_auh, **_ph, **_rh,
}

# Sub-agents do not get write_vault — they must use write_temp → commit_to_vault
_AGENT_EXCLUDED_TOOLS = {"write_vault"}

AGENT_TOOL_DEFINITIONS: list[dict] = [
    t for t in TOOL_DEFINITIONS
    if t.get("function", {}).get("name") not in _AGENT_EXCLUDED_TOOLS
]
