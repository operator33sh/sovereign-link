"""Personality, timezone, user status, and notification tools."""


def update_personality(updated_profile: str) -> str:
    from personality import update_personality as _update
    return _update(updated_profile)


def set_timezone(timezone_string: str) -> str:
    from timezone_manager import set_timezone as _set_tz
    return _set_tz(timezone_string)


def get_timezone() -> str:
    from timezone_manager import get_timezone_info
    return get_timezone_info()


def set_sleep_mode(sleeping: bool) -> str:
    from proactive import user_status
    return user_status.set_sleep_mode(sleeping)


def get_user_status() -> str:
    from proactive import user_status
    return user_status.get_status_summary()


DEFINITIONS = [
    {"type": "function", "function": {"name": "update_personality", "description": "Update Luna's personality profile.", "parameters": {"type": "object", "properties": {"updated_profile": {"type": "string"}}, "required": ["updated_profile"]}}},
    {"type": "function", "function": {"name": "set_timezone", "description": "Set the user's local timezone.", "parameters": {"type": "object", "properties": {"timezone_string": {"type": "string"}}, "required": ["timezone_string"]}}},
    {"type": "function", "function": {"name": "get_timezone", "description": "Show the currently configured user timezone and local time.", "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {"name": "set_sleep_mode", "description": "Enable or disable sleep mode.", "parameters": {"type": "object", "properties": {"sleeping": {"type": "boolean"}}, "required": ["sleeping"]}}},
    {"type": "function", "function": {"name": "get_user_status", "description": "Return the current user status: whether sleep mode is active.", "parameters": {"type": "object", "properties": {}, "required": []}}},
]

HANDLERS = {
    "update_personality": lambda args: update_personality(args["updated_profile"]),
    "set_timezone": lambda args: set_timezone(args["timezone_string"]),
    "get_timezone": lambda args: get_timezone(),
    "set_sleep_mode": lambda args: set_sleep_mode(args["sleeping"]),
    "get_user_status": lambda args: get_user_status(),
}
