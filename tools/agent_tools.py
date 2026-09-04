"""Agent management and swarm tools."""
import os
from datetime import datetime

from tools.vault import write_temp, read_temp, AGENT_TEMP_PATH


def write_blackboard(project_id: str, fragment: str, label: str = "") -> str:
    ts = datetime.now().strftime("%H%M%S")
    file_name = f"blackboard/{project_id}/{label or 'fragment'}_{ts}.md"
    return write_temp(file_name, fragment)


def read_blackboard(project_id: str) -> str:
    board_dir = os.path.join(AGENT_TEMP_PATH, "blackboard", project_id)
    if not os.path.isdir(board_dir):
        return f"Blackboard '{project_id}' is empty or does not exist."
    fragments = []
    for fname in sorted(os.listdir(board_dir)):
        if fname.endswith(".md"):
            path = os.path.join(board_dir, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    fragments.append(f"### {fname}\n{f.read()}")
            except Exception:
                pass
    return "\n\n---\n\n".join(fragments) if fragments else "Blackboard is empty."


def send_signal(recipient: str, message: str, sender: str = "unknown") -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_name = f"signals/{recipient}_{ts}.md"
    content = f"**From:** {sender}  \n**To:** {recipient}  \n**Time:** {ts}\n\n{message}"
    return write_temp(file_name, content)


def read_signals(agent_name: str) -> str:
    sig_dir = os.path.join(AGENT_TEMP_PATH, "signals")
    if not os.path.isdir(sig_dir):
        return f"No signals for {agent_name}."
    signals = []
    for fname in sorted(os.listdir(sig_dir)):
        if fname.startswith(agent_name + "_") and fname.endswith(".md"):
            path = os.path.join(sig_dir, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    signals.append(f.read())
            except Exception:
                pass
    return "\n\n---\n\n".join(signals) if signals else f"No signals for {agent_name}."


def spawn_agent(goal: str, agent_name: str = "Agent", report_to_chat: bool = False) -> str:
    from agent import launch_agent
    from chat_bridge import get_context_injector, get_llm_trigger
    injector = get_context_injector() if report_to_chat else None
    trigger = get_llm_trigger() if report_to_chat else None
    return launch_agent(goal, agent_name, context_injector=injector, llm_trigger=trigger)


def get_agent_status(agent_id: str) -> str:
    from agent import get_agent_status as _status
    return _status(agent_id)


def list_agents() -> str:
    from agent import list_agents as _list
    return _list()


def spawn_peer(goal: str, role: str, project_id: str, swarm_id: str) -> str:
    from agent import _get_swarm_coordinator
    coordinator = _get_swarm_coordinator(swarm_id)
    if coordinator is None:
        return f"Error: swarm '{swarm_id}' not found."
    return coordinator.add_peer(goal=goal, role=role)


def spawn_swarm(
    goal: str, project_id: str,
    roles: list | None = None, swarm_size: int = 5, report_to_chat: bool = False,
) -> str:
    from agent import launch_swarm
    from chat_bridge import get_context_injector, get_llm_trigger
    injector = get_context_injector() if report_to_chat else None
    trigger = get_llm_trigger() if report_to_chat else None
    return launch_swarm(
        goal=goal, project_id=project_id,
        roles=roles, swarm_size=swarm_size,
        context_injector=injector, llm_trigger=trigger,
    )


DEFINITIONS = [
    {"type": "function", "function": {"name": "spawn_agent", "description": "Spawn an autonomous background agent to work on a goal independently.", "parameters": {"type": "object", "properties": {"goal": {"type": "string"}, "agent_name": {"type": "string"}, "report_to_chat": {"type": "boolean"}}, "required": ["goal"]}}},
    {"type": "function", "function": {"name": "get_agent_status", "description": "Check the current status of a background agent.", "parameters": {"type": "object", "properties": {"agent_id": {"type": "string"}}, "required": ["agent_id"]}}},
    {"type": "function", "function": {"name": "list_agents", "description": "List all background agents spawned this session with their status.", "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {"name": "write_blackboard", "description": "Write an insight fragment to the shared swarm blackboard (transient, never indexed).", "parameters": {"type": "object", "properties": {"project_id": {"type": "string"}, "fragment": {"type": "string"}, "label": {"type": "string"}}, "required": ["project_id", "fragment"]}}},
    {"type": "function", "function": {"name": "read_blackboard", "description": "Read all fragments posted to the shared blackboard by all peers in a swarm.", "parameters": {"type": "object", "properties": {"project_id": {"type": "string"}}, "required": ["project_id"]}}},
    {"type": "function", "function": {"name": "send_signal", "description": "Send an async direct message to a specific peer agent.", "parameters": {"type": "object", "properties": {"recipient": {"type": "string"}, "message": {"type": "string"}, "sender": {"type": "string"}}, "required": ["recipient", "message"]}}},
    {"type": "function", "function": {"name": "read_signals", "description": "Read all pending signals addressed to you.", "parameters": {"type": "object", "properties": {"agent_name": {"type": "string"}}, "required": ["agent_name"]}}},
    {"type": "function", "function": {"name": "spawn_peer", "description": "Spawn a new peer agent in the same swarm.", "parameters": {"type": "object", "properties": {"goal": {"type": "string"}, "role": {"type": "string"}, "project_id": {"type": "string"}, "swarm_id": {"type": "string"}}, "required": ["goal", "role", "project_id", "swarm_id"]}}},
    {"type": "function", "function": {"name": "spawn_swarm", "description": "Start a rhizomatic peer swarm where agents collaborate on a shared Blackboard.", "parameters": {"type": "object", "properties": {"goal": {"type": "string"}, "project_id": {"type": "string"}, "roles": {"type": "array", "items": {"type": "string"}}, "swarm_size": {"type": "integer"}, "report_to_chat": {"type": "boolean"}}, "required": ["goal", "project_id"]}}},
]

HANDLERS = {
    "spawn_agent": lambda args: spawn_agent(args["goal"], args.get("agent_name", "Agent"), args.get("report_to_chat", False)),
    "get_agent_status": lambda args: get_agent_status(args["agent_id"]),
    "list_agents": lambda args: list_agents(),
    "write_blackboard": lambda args: write_blackboard(args["project_id"], args["fragment"], args.get("label", "")),
    "read_blackboard": lambda args: read_blackboard(args["project_id"]),
    "send_signal": lambda args: send_signal(args["recipient"], args["message"], args.get("sender", "unknown")),
    "read_signals": lambda args: read_signals(args["agent_name"]),
    "spawn_peer": lambda args: spawn_peer(args["goal"], args["role"], args["project_id"], args["swarm_id"]),
    "spawn_swarm": lambda args: spawn_swarm(args["goal"], args["project_id"], args.get("roles"), args.get("swarm_size", 5), args.get("report_to_chat", False)),
}
