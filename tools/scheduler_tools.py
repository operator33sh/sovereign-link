"""Scheduled task tools."""


def schedule_task(execution_time: str, action: str, parameters: dict, description: str = "") -> str:
    from scheduler import scheduler as _scheduler
    return _scheduler.add_task(execution_time, action, parameters, description)


def list_scheduled_tasks(include_done: bool = False) -> str:
    from scheduler import scheduler as _scheduler
    return _scheduler.list_tasks(include_done)


def cancel_scheduled_task(task_id: str) -> str:
    from scheduler import scheduler as _scheduler
    return _scheduler.cancel_task(task_id)


DEFINITIONS = [
    {"type": "function", "function": {"name": "schedule_task", "description": "Schedule any registered tool to run at a specific future time.", "parameters": {"type": "object", "properties": {"execution_time": {"type": "string"}, "action": {"type": "string"}, "parameters": {"type": "object"}, "description": {"type": "string"}}, "required": ["execution_time", "action", "parameters"]}}},
    {"type": "function", "function": {"name": "list_scheduled_tasks", "description": "Show all scheduled tasks with their execution times and current status.", "parameters": {"type": "object", "properties": {"include_done": {"type": "boolean"}}, "required": []}}},
    {"type": "function", "function": {"name": "cancel_scheduled_task", "description": "Cancel a pending scheduled task by its task_id.", "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}}},
]

HANDLERS = {
    "schedule_task": lambda args: schedule_task(args["execution_time"], args["action"], args["parameters"], args.get("description", "")),
    "list_scheduled_tasks": lambda args: list_scheduled_tasks(args.get("include_done", False)),
    "cancel_scheduled_task": lambda args: cancel_scheduled_task(args["task_id"]),
}
