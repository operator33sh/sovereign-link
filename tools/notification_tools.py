"""Notification queue tools."""


def write_notification(content: str, agent_id: str = "system", priority: str = "medium", category: str = "insight", related_file: str | None = None) -> str:
    from notifications import notification_manager
    return notification_manager.write(content, agent_id, priority, category, related_file)


def get_pending_notifications() -> str:
    from notifications import notification_manager
    return notification_manager.get_pending()


def clear_stop_order(_args: dict = None) -> str:
    """Release the active Cognitive Brake Stop Order after the required pause has elapsed."""
    import cognitive_brake
    return cognitive_brake.clear_stop_order()


def set_pause_duration(args: dict) -> str:
    """Set the required pause duration (minimum 15 minutes) before Stop Order can be cleared."""
    import cognitive_brake
    minutes = float(args.get("minutes", 15))
    return cognitive_brake.set_pause_duration(minutes)


def set_session_timer(args: dict) -> str:
    """Override the session stop threshold to a user-defined duration."""
    import cognitive_brake
    minutes = float(args.get("minutes", 60))
    return cognitive_brake.set_session_timer(minutes)


DEFINITIONS = [
    {"type": "function", "function": {"name": "write_notification", "description": "Push a notification into the persistent queue so the user sees it later.", "parameters": {"type": "object", "properties": {"content": {"type": "string"}, "agent_id": {"type": "string"}, "priority": {"type": "string", "enum": ["low", "medium", "high"]}, "category": {"type": "string", "enum": ["insight", "alert", "system", "wellness", "task"]}, "related_file": {"type": "string"}}, "required": ["content"]}}},
    {"type": "function", "function": {"name": "get_pending_notifications", "description": "Retrieve all unread notifications from the queue.", "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {"name": "clear_stop_order", "description": "Release the active Cognitive Brake Stop Order after the user has confirmed they have rested. Enforces the required pause duration — returns a refusal if the minimum rest time has not yet elapsed. Only call this when the user explicitly says they have rested or asks to resume.", "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {"name": "set_pause_duration", "description": "Stel de vereiste pauzetijd in voordat een Stop Order vrijgegeven kan worden. Minimum is 15 minuten — hogere waarden zijn toegestaan voor zware sessies. Gebruik dit proactief wanneer de sessie emotioneel intensief is.", "parameters": {"type": "object", "properties": {"minutes": {"type": "number", "description": "Gewenste pauzetijd in minuten (minimum 15)."}}, "required": ["minutes"]}}},
    {"type": "function", "function": {"name": "set_session_timer", "description": "Stel een aangepaste sessietimer in. De Stop Order activeert op dit tijdstip ongeacht de actieve fase. De override blijft actief na een Stop Order — alleen reset_session() wist hem. Gebruik dit wanneer de gebruiker een specifieke sessieduur aangeeft.", "parameters": {"type": "object", "properties": {"minutes": {"type": "number", "description": "Gewenste sessieduur in minuten (minimaal 1)."}}, "required": ["minutes"]}}},
]

HANDLERS = {
    "write_notification": lambda args: write_notification(args["content"], args.get("agent_id", "system"), args.get("priority", "medium"), args.get("category", "insight"), args.get("related_file")),
    "get_pending_notifications": lambda args: get_pending_notifications(),
    "clear_stop_order": lambda args: clear_stop_order(args),
    "set_pause_duration": lambda args: set_pause_duration(args),
    "set_session_timer": lambda args: set_session_timer(args),
}
