"""Notification queue tools."""


def write_notification(content: str, agent_id: str = "system", priority: str = "medium", category: str = "insight", related_file: str | None = None) -> str:
    from notifications import notification_manager
    return notification_manager.write(content, agent_id, priority, category, related_file)


def get_pending_notifications() -> str:
    from notifications import notification_manager
    return notification_manager.get_pending()


DEFINITIONS = [
    {"type": "function", "function": {"name": "write_notification", "description": "Push a notification into the persistent queue so the user sees it later.", "parameters": {"type": "object", "properties": {"content": {"type": "string"}, "agent_id": {"type": "string"}, "priority": {"type": "string", "enum": ["low", "medium", "high"]}, "category": {"type": "string", "enum": ["insight", "alert", "system", "wellness", "task"]}, "related_file": {"type": "string"}}, "required": ["content"]}}},
    {"type": "function", "function": {"name": "get_pending_notifications", "description": "Retrieve all unread notifications from the queue.", "parameters": {"type": "object", "properties": {}, "required": []}}},
]

HANDLERS = {
    "write_notification": lambda args: write_notification(args["content"], args.get("agent_id", "system"), args.get("priority", "medium"), args.get("category", "insight"), args.get("related_file")),
    "get_pending_notifications": lambda args: get_pending_notifications(),
}
