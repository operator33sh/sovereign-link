"""Automation engine tools."""


def list_automations() -> str:
    from automations import automation_engine
    return automation_engine.list_automations()


def create_automation(name: str, trigger_type: str, schedule: str, action: str, parameters: dict, enabled: bool = True) -> str:
    from automations import automation_engine
    return automation_engine.create_automation(name, trigger_type, schedule, action, parameters, enabled)


def toggle_automation(automation_id: str, enabled: bool) -> str:
    from automations import automation_engine
    return automation_engine.toggle_automation(automation_id, enabled)


def delete_automation(automation_id: str) -> str:
    from automations import automation_engine
    return automation_engine.delete_automation(automation_id)


DEFINITIONS = [
    {"type": "function", "function": {"name": "list_automations", "description": "List all defined automations.", "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {"name": "create_automation", "description": "Create a new recurring automation.", "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "trigger_type": {"type": "string", "enum": ["cron", "interval"]}, "schedule": {"type": "string"}, "action": {"type": "string"}, "parameters": {"type": "object"}, "enabled": {"type": "boolean"}}, "required": ["name", "trigger_type", "schedule", "action", "parameters"]}}},
    {"type": "function", "function": {"name": "toggle_automation", "description": "Enable or disable a specific automation by its ID.", "parameters": {"type": "object", "properties": {"automation_id": {"type": "string"}, "enabled": {"type": "boolean"}}, "required": ["automation_id", "enabled"]}}},
    {"type": "function", "function": {"name": "delete_automation", "description": "Permanently remove an automation from the registry.", "parameters": {"type": "object", "properties": {"automation_id": {"type": "string"}}, "required": ["automation_id"]}}},
]

HANDLERS = {
    "list_automations": lambda args: list_automations(),
    "create_automation": lambda args: create_automation(args["name"], args["trigger_type"], args["schedule"], args["action"], args["parameters"], args.get("enabled", True)),
    "toggle_automation": lambda args: toggle_automation(args["automation_id"], args["enabled"]),
    "delete_automation": lambda args: delete_automation(args["automation_id"]),
}
