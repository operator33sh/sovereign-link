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
from tools.moltbook_sentinel import DEFINITIONS as _msd, HANDLERS as _msh
from tools.health import DEFINITIONS as _hld, HANDLERS as _hlh
from tools.gmail import DEFINITIONS as _gmd, HANDLERS as _gmh
from tools.calendar import DEFINITIONS as _cld, HANDLERS as _clh
from tools.scraping_tools import DEFINITIONS as _scrd, HANDLERS as _scrh
from tools.claim_tools import DEFINITIONS as _ctd, HANDLERS as _cth
from tools.moltbook_ignore import DEFINITIONS as _mid, HANDLERS as _mih
from tools.mental_state_tools import DEFINITIONS as _mntd, HANDLERS as _mnth
from tools.twitter import DEFINITIONS as _twd, HANDLERS as _twh
from tools.soul_tools import DEFINITIONS as _sld, HANDLERS as _slh
from tools.session_status_tools import DEFINITIONS as _ssd, HANDLERS as _ssh

# Search tools (_sd) placed last so they survive context-window truncation.
# Vault tools (_vd) second-to-last for the same reason.
TOOL_DEFINITIONS: list[dict] = _hd + _bd + _ad + _nd + _scd + _aud + _pd + _rd + _msd + _hld + _gmd + _cld + _scrd + _ctd + _mid + _mntd + _twd + _sld + _ssd + _vd + _sd

_CORE_NAMES = {
    # Vault
    "write_vault", "read_vault", "search_vault_semantic", "search_timeline",
    "list_files", "file_exists", "sync_vault",
    # Notifications
    "write_notification", "get_pending_notifications",
    # Gmail
    "list_messages", "send_email",
    # Calendar
    "list_events", "create_event",
    # Health
    "get_steps", "get_heart_rate", "get_sleep", "get_activity_summary",
    # Personality & scheduling
    "update_personality", "schedule_task",
    # Web
    "http_request",
    # X (Twitter)
    "x_post_tweet", "x_get_tweets", "x_send_dm", "x_get_dms",
    # Cognitive architecture
    "write_soul_md",
    # Cognitive brake
    "clear_stop_order",
    "set_pause_duration",
    "set_session_timer",
    # Session dashboard
    "get_session_status",
}

CORE_TOOL_DEFINITIONS: list[dict] = [
    t for t in TOOL_DEFINITIONS
    if t.get("function", {}).get("name") in _CORE_NAMES
]

TOOL_HANDLERS: dict[str, callable] = {
    **_vh, **_hh, **_sh, **_bh, **_ah, **_nh, **_sch, **_auh, **_ph, **_rh, **_msh, **_hlh, **_gmh, **_clh, **_scrh, **_cth, **_mih, **_mnth, **_twh, **_slh, **_ssh,
}

# Sub-agents do not get write_vault — they must use write_temp → commit_to_vault
_AGENT_EXCLUDED_TOOLS = {"write_vault"}

AGENT_TOOL_DEFINITIONS: list[dict] = [
    t for t in TOOL_DEFINITIONS
    if t.get("function", {}).get("name") not in _AGENT_EXCLUDED_TOOLS
]
