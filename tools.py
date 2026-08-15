import logging
import os
import subprocess
from datetime import datetime
from urllib.parse import urlparse

from vector import index_file as _index_file
from vector import search_vault_semantic as _search_vault_semantic

logger = logging.getLogger(__name__)

VAULT_PATH = os.environ.get("VAULT_PATH", "/home/wouter/Documents/fractalisme-vault")
AGENT_TEMP_PATH = os.path.join(VAULT_PATH, ".agent_temp")


def generate_time_tag() -> str:
    """Return a chronological search tag for the current month, e.g. '#2026-08'."""
    return datetime.now().strftime("#%Y-%m")


def read_vault(file_name: str) -> str:
    path = os.path.join(VAULT_PATH, file_name)
    if not os.path.realpath(path).startswith(os.path.realpath(VAULT_PATH)):
        return "Error: path traversal not allowed"
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return f"Error: file '{file_name}' not found in vault"
    except Exception as e:
        return f"Error reading file: {e}"


def write_vault(file_name: str, content: str, timestamp: str | None = None) -> str:
    path = os.path.join(VAULT_PATH, file_name)
    if not os.path.realpath(path).startswith(os.path.realpath(VAULT_PATH)):
        return "Error: path traversal not allowed"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        return f"Error writing file: {e}"

    try:
        _index_file(file_name, content, timestamp or datetime.now().isoformat())
    except Exception:
        logger.exception("write_vault: failed to index %s", file_name)

    return f"Written successfully to '{file_name}'"


def sync_vault() -> str:
    try:
        result = subprocess.run(
            'git add . && git commit -m "Sovereign-Link Update" && git push',
            shell=True,
            cwd=VAULT_PATH,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = result.stdout + result.stderr
        if result.returncode != 0:
            return f"Sync failed:\n{output}"
        return f"Vault synced:\n{output}"
    except subprocess.TimeoutExpired:
        return "Error: git operation timed out"
    except Exception as e:
        return f"Error during sync: {e}"


def analyze_website(url: str) -> str:
    from browser import fetch_with_browser_fallback
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "Error: only HTTP/HTTPS URLs are allowed"
    return fetch_with_browser_fallback(url)


def write_temp(file_name: str, content: str) -> str:
    """Write a transient file to .agent_temp/. Never indexed by the VectorDB."""
    path = os.path.join(AGENT_TEMP_PATH, file_name)
    if not os.path.realpath(path).startswith(os.path.realpath(AGENT_TEMP_PATH)):
        return "Error: path traversal not allowed"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Temp written: '{file_name}'"
    except Exception as e:
        return f"Error writing temp file: {e}"


def read_temp(file_name: str) -> str:
    """Read a transient file from .agent_temp/."""
    path = os.path.join(AGENT_TEMP_PATH, file_name)
    if not os.path.realpath(path).startswith(os.path.realpath(AGENT_TEMP_PATH)):
        return "Error: path traversal not allowed"
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return f"Error: temp file '{file_name}' not found"
    except Exception as e:
        return f"Error reading temp file: {e}"


def cleanup_transient_data(agent_id: str) -> str:
    """Delete all transient files for a given agent_id from .agent_temp/."""
    import shutil
    target = os.path.join(AGENT_TEMP_PATH, agent_id)
    real_target = os.path.realpath(target)
    if not real_target.startswith(os.path.realpath(AGENT_TEMP_PATH)):
        return "Error: path traversal not allowed"
    if not os.path.exists(target):
        return f"No transient data found for agent '{agent_id}'."
    try:
        shutil.rmtree(target)
        return f"Transient data for '{agent_id}' purged."
    except Exception as e:
        return f"Error during cleanup: {e}"


def list_files(directory: str = "") -> str:
    target = os.path.join(VAULT_PATH, directory) if directory else VAULT_PATH
    real_target = os.path.realpath(target)
    if not real_target.startswith(os.path.realpath(VAULT_PATH)):
        return "Error: path traversal not allowed"
    if not os.path.isdir(real_target):
        return f"Error: '{directory}' is not a directory in the vault"
    paths = []
    for root, _, files in os.walk(real_target):
        for fname in sorted(files):
            full = os.path.join(root, fname)
            paths.append(os.path.relpath(full, VAULT_PATH))
    if not paths:
        return "Directory is empty."
    return "\n".join(sorted(paths))


def move_file(source_path: str, destination_path: str) -> str:
    src = os.path.join(VAULT_PATH, source_path)
    dst = os.path.join(VAULT_PATH, destination_path)
    real_vault = os.path.realpath(VAULT_PATH)
    if not os.path.realpath(src).startswith(real_vault):
        return "Error: source path traversal not allowed"
    if not os.path.realpath(os.path.dirname(dst) or VAULT_PATH).startswith(real_vault):
        return "Error: destination path traversal not allowed"
    if not os.path.exists(src):
        return f"Error: source file '{source_path}' not found"
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.rename(src, dst)
        return f"Moved '{source_path}' → '{destination_path}'"
    except Exception as e:
        return f"Error moving file: {e}"


def delete_file(file_path: str) -> str:
    path = os.path.join(VAULT_PATH, file_path)
    if not os.path.realpath(path).startswith(os.path.realpath(VAULT_PATH)):
        return "Error: path traversal not allowed"
    if not os.path.exists(path):
        return f"Error: file '{file_path}' not found"
    if os.path.isdir(path):
        return "Error: delete_file only deletes files, not directories"
    try:
        os.remove(path)
        return f"Deleted '{file_path}'"
    except Exception as e:
        return f"Error deleting file: {e}"


def write_blackboard(project_id: str, fragment: str, label: str = "") -> str:
    """Write an insight fragment to the shared blackboard for a swarm project."""
    ts = datetime.now().strftime("%H%M%S")
    file_name = f"agents/blackboard/{project_id}/{label or 'fragment'}_{ts}.md"
    return write_vault(file_name, fragment)


def read_blackboard(project_id: str) -> str:
    """Read all fragments posted to the blackboard by all peers."""
    board_dir = os.path.join(VAULT_PATH, "agents", "blackboard", project_id)
    if not os.path.isdir(board_dir):
        return f"Blackboard '{project_id}' is empty or does not exist."
    fragments = []
    for fname in sorted(os.listdir(board_dir)):
        if fname.endswith(".md"):
            content = read_vault(f"agents/blackboard/{project_id}/{fname}")
            fragments.append(f"### {fname}\n{content}")
    return "\n\n---\n\n".join(fragments) if fragments else "Blackboard is empty."


def send_signal(recipient: str, message: str, sender: str = "unknown") -> str:
    """Send an async signal to another agent via the vault."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_name = f"agents/signals/{recipient}_{ts}.md"
    content = f"**From:** {sender}  \n**To:** {recipient}  \n**Time:** {ts}\n\n{message}"
    return write_vault(file_name, content)


def read_signals(agent_name: str) -> str:
    """Read all pending signals addressed to this agent."""
    sig_dir = os.path.join(VAULT_PATH, "agents", "signals")
    if not os.path.isdir(sig_dir):
        return "No signals."
    signals = []
    for fname in sorted(os.listdir(sig_dir)):
        if fname.startswith(agent_name + "_") and fname.endswith(".md"):
            signals.append(read_vault(f"agents/signals/{fname}"))
    return "\n\n---\n\n".join(signals) if signals else f"No signals for {agent_name}."


def set_timezone(timezone_string: str) -> str:
    from timezone_manager import set_timezone as _set_tz
    return _set_tz(timezone_string)


def get_timezone() -> str:
    from timezone_manager import get_timezone_info
    return get_timezone_info()


def write_notification(
    content: str,
    agent_id: str = "system",
    priority: str = "medium",
    category: str = "insight",
    related_file: str | None = None,
) -> str:
    from notifications import notification_manager
    return notification_manager.write(content, agent_id, priority, category, related_file)


def get_pending_notifications() -> str:
    from notifications import notification_manager
    return notification_manager.get_pending()


def schedule_task(execution_time: str, action: str, parameters: dict, description: str = "") -> str:
    from scheduler import scheduler as _scheduler
    return _scheduler.add_task(execution_time, action, parameters, description)


def list_scheduled_tasks(include_done: bool = False) -> str:
    from scheduler import scheduler as _scheduler
    return _scheduler.list_tasks(include_done)


def cancel_scheduled_task(task_id: str) -> str:
    from scheduler import scheduler as _scheduler
    return _scheduler.cancel_task(task_id)


def list_automations() -> str:
    from automations import automation_engine
    return automation_engine.list_automations()


def create_automation(
    name: str,
    trigger_type: str,
    schedule: str,
    action: str,
    parameters: dict,
    enabled: bool = True,
) -> str:
    from automations import automation_engine
    return automation_engine.create_automation(name, trigger_type, schedule, action, parameters, enabled)


def toggle_automation(automation_id: str, enabled: bool) -> str:
    from automations import automation_engine
    return automation_engine.toggle_automation(automation_id, enabled)


def delete_automation(automation_id: str) -> str:
    from automations import automation_engine
    return automation_engine.delete_automation(automation_id)


def set_sleep_mode(sleeping: bool) -> str:
    from proactive import user_status
    return user_status.set_sleep_mode(sleeping)


def get_user_status() -> str:
    from proactive import user_status
    return user_status.get_status_summary()


def spawn_peer(goal: str, role: str, project_id: str, swarm_id: str) -> str:
    """Spawn a peer agent in the same swarm (horizontal, not hierarchical)."""
    from agent import _get_swarm_coordinator
    coordinator = _get_swarm_coordinator(swarm_id)
    if coordinator is None:
        return f"Error: swarm '{swarm_id}' not found."
    return coordinator.add_peer(goal=goal, role=role)


TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "read_vault",
            "description": "Read a file from the fractalisme vault.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_name": {
                        "type": "string",
                        "description": "The file name (or relative path) to read from the vault.",
                    }
                },
                "required": ["file_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_vault",
            "description": "Write or overwrite a file in the fractalisme vault.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_name": {
                        "type": "string",
                        "description": "The file name (or relative path) to write in the vault.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The full content to write to the file.",
                    },
                },
                "required": ["file_name", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sync_vault",
            "description": "Commit and push all vault changes to git (git add . && git commit && git push).",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_website",
            "description": (
                "Fetch and extract the main readable content of a webpage as Markdown. "
                "Use this when the user shares a URL or asks what a website says. "
                "Strips navigation, ads, and boilerplate. Content is capped at 8000 chars. "
                "Only HTTP/HTTPS URLs are supported."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The full URL to fetch (must start with http:// or https://).",
                    }
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_navigate",
            "description": (
                "Open a URL in a headless browser session. "
                "Automatically dismisses cookie walls and returns the page content as Markdown. "
                "Use this instead of analyze_website when you need to interact with the page afterwards "
                "(click buttons, scroll, take screenshots). "
                "The session persists for 10 minutes — reuse the same session_id for follow-up actions. "
                "Only HTTP/HTTPS URLs are supported."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The full URL to navigate to (must start with http:// or https://).",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Optional session identifier. Use a consistent name (e.g. 'research') across calls to keep the same browser tab open. Defaults to 'default'.",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": (
                "Click an element on the current page of a browser session using a CSS selector. "
                "Use after browser_navigate to interact with buttons, links, or form elements. "
                "Waits 1 second for the page to settle after clicking."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "CSS selector for the element to click (e.g. 'button.load-more', '#show-article', 'a:has-text(\"Lees meer\")').",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "The session_id used in the preceding browser_navigate call. Defaults to 'default'.",
                    },
                },
                "required": ["selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_extract_content",
            "description": (
                "Extract the current page content as Markdown from an active browser session. "
                "Use this after browser_click or other interactions to re-read updated content. "
                "Content is capped at 8000 chars."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "The session_id to extract content from. Defaults to 'default'.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_screenshot",
            "description": (
                "Take a screenshot of the current page in a browser session. "
                "Saves the PNG to disk and returns the file path. "
                "Use this for visual debugging — e.g. to see what the page looks like before deciding which selector to click."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "The session_id to screenshot. Defaults to 'default'.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_close_session",
            "description": (
                "Close and clean up a browser session, freeing all resources. "
                "Call this when you are done browsing to avoid resource leaks. "
                "Sessions also auto-close after 10 minutes of inactivity."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "The session_id to close. Defaults to 'default'.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_agent",
            "description": (
                "Spawn an autonomous background agent to work on a goal independently. "
                "The agent runs in a separate thread — it does NOT block Luna's conversation. "
                "It has access to all tools (read/write vault, web analysis, search) and uses "
                "its own Observe → Reason → Act → Evaluate loop until the goal is achieved. "
                "Returns immediately with an agent_id. Use get_agent_status to check progress. "
                "Good for: deep research, multi-step analysis, generating structured reports, "
                "tasks that take too long for a single conversation turn."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "Clear, detailed description of what the agent should accomplish, including any deliverables (files to write, etc.).",
                    },
                    "agent_name": {
                        "type": "string",
                        "description": "Short descriptive name for this agent, e.g. 'ResearchAgent', 'GrowthAgent', 'AnalysisAgent'.",
                    },
                    "report_to_chat": {
                        "type": "boolean",
                        "description": (
                            "If true, the agent sends a summary directly to this chat when it finishes. "
                            "Use true when the user will want to hear the result soon. "
                            "Use false for silent background processing (heavy/long tasks). "
                            "Default: false."
                        ),
                    },
                },
                "required": ["goal"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_agent_status",
            "description": (
                "Check the current status of a background agent. "
                "Returns: status (running/completed/failed/timeout), result if done, and log file path. "
                "Call this when the user asks about an agent's progress or results."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "The agent ID returned by spawn_agent.",
                    }
                },
                "required": ["agent_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_agents",
            "description": "List all background agents spawned this session with their status (running/completed/failed). Use this to give an overview of active and finished agents.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_blackboard",
            "description": (
                "Write an insight fragment to the shared blackboard for a swarm project. "
                "Use this to post observations, hypotheses, or conclusions so peer agents can read them. "
                "All agents in the same swarm share a single blackboard per project_id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "The swarm project identifier."},
                    "fragment": {"type": "string", "description": "The insight or finding to post."},
                    "label": {"type": "string", "description": "Optional short label for the fragment file (e.g. 'hypothesis', 'finding')."},
                },
                "required": ["project_id", "fragment"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_blackboard",
            "description": (
                "Read all fragments posted to the shared blackboard by all peers in a swarm. "
                "Call this periodically to observe what peers have discovered and adjust your research accordingly."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "The swarm project identifier."},
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_signal",
            "description": "Send an async direct message to a specific peer agent via the vault.",
            "parameters": {
                "type": "object",
                "properties": {
                    "recipient": {"type": "string", "description": "The agent_name of the recipient peer."},
                    "message": {"type": "string", "description": "The message content to send."},
                    "sender": {"type": "string", "description": "Your own agent_name as sender."},
                },
                "required": ["recipient", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_signals",
            "description": "Read all pending signals (direct messages from peers) addressed to you.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": {"type": "string", "description": "Your own agent_name to read signals for."},
                },
                "required": ["agent_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_peer",
            "description": (
                "Spawn a new peer agent in the same swarm (horizontal, not hierarchical). "
                "Use when you need a complementary perspective — e.g. a 'Skeptic' to challenge your hypothesis. "
                "Subject to the swarm's maximum size limit."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string", "description": "What this peer should investigate."},
                    "role": {"type": "string", "description": "Role label for the peer (e.g. 'Skeptic', 'FactChecker')."},
                    "project_id": {"type": "string", "description": "The swarm project identifier."},
                    "swarm_id": {"type": "string", "description": "The swarm_id this peer should join."},
                },
                "required": ["goal", "role", "project_id", "swarm_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_timezone",
            "description": (
                "Set the user's local timezone so that scheduled tasks use the correct local time. "
                "Must be a valid IANA timezone string (e.g. 'Europe/Amsterdam', 'America/New_York'). "
                "Call this once when setting up, or when the user changes location."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "timezone_string": {
                        "type": "string",
                        "description": "IANA timezone name, e.g. 'Europe/Amsterdam'.",
                    },
                },
                "required": ["timezone_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_timezone",
            "description": "Show the currently configured user timezone and local time.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_notification",
            "description": (
                "Push a notification into the persistent queue so the user sees it later. "
                "Use this as the final step of any background agent or scheduled task to report completion. "
                "Notifications are stored in vault/.system/notifications.json and retrieved via get_pending_notifications."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The notification message in natural Dutch language.",
                    },
                    "agent_id": {
                        "type": "string",
                        "description": "Identifier of the agent or system that generated this notification (e.g. 'AngerAnalysisAgent').",
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                        "description": "Urgency level. Default: 'medium'.",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["insight", "alert", "system", "wellness", "task"],
                        "description": "Type of notification. Default: 'insight'.",
                    },
                    "related_file": {
                        "type": "string",
                        "description": "Optional vault path to a file where the full result is stored (e.g. 'Analyses/Anger_2026-08-16.md').",
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_pending_notifications",
            "description": (
                "Retrieve all unread notifications from the queue, sorted by priority then time. "
                "Marks them as delivered so they won't appear again. "
                "Call this when the user asks 'wat is er gebeurd?', 'zijn er meldingen?', or returns after being away."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_task",
            "description": (
                "Schedule any registered tool to run at a specific future time. "
                "The scheduler checks every minute and fires tasks when their time arrives. "
                "Tasks are persisted to vault/.system/scheduled_tasks.json and survive bot restarts. "
                "Example: schedule spawn_agent at 3am to run a pattern analysis while the user sleeps."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "execution_time": {
                        "type": "string",
                        "description": "ISO 8601 datetime when the task should run. Use the same local time shown in the system prompt — e.g. if it shows '2026-08-15 14:30:00', schedule 5 min later as '2026-08-15T14:35:00'. Do NOT convert to UTC. Naive datetimes are interpreted as the user's configured local timezone.",
                    },
                    "action": {
                        "type": "string",
                        "description": "The tool name to execute — must be a registered tool such as 'spawn_agent', 'write_vault', or 'search_vault_semantic'.",
                    },
                    "parameters": {
                        "type": "object",
                        "description": "Arguments for the action, matching that tool's parameter schema exactly.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Human-readable label shown in the task list (e.g. 'Patroonanalyse woede'). Optional but recommended.",
                    },
                },
                "required": ["execution_time", "action", "parameters"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_scheduled_tasks",
            "description": "Show all scheduled tasks with their execution times and current status (pending / completed / failed / cancelled).",
            "parameters": {
                "type": "object",
                "properties": {
                    "include_done": {
                        "type": "boolean",
                        "description": "If true, also show completed, failed, and cancelled tasks. Default: false (pending only).",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_scheduled_task",
            "description": "Cancel a pending scheduled task by its task_id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The 8-character task_id returned by schedule_task.",
                    },
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_sleep_mode",
            "description": (
                "Enable or disable sleep mode. In sleep mode, only high-priority notifications "
                "are pushed proactively; medium and low notifications are held until the user returns. "
                "Sleep mode is automatically cleared when the user sends any message."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sleeping": {
                        "type": "boolean",
                        "description": "True to enable sleep mode, false to disable it.",
                    },
                },
                "required": ["sleeping"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_user_status",
            "description": (
                "Return the current user status: whether sleep mode is active and when the user was last active. "
                "Use this before deciding whether to push a notification or hold it."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_automations",
            "description": (
                "List all defined automations with their trigger schedule, action, "
                "next scheduled run time, and enabled/disabled status. "
                "Use this when the user asks what recurring automations are configured."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_automation",
            "description": (
                "Create a new recurring automation that fires a tool action on a schedule. "
                "Supports cron expressions (e.g. '0 8 * * 1' = every Monday at 08:00) "
                "and interval triggers (frequency in seconds). "
                "For spawn_agent actions, the automation name is automatically prepended "
                "to the goal for traceability in logs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Human-readable label for this automation, e.g. 'Weekly Review'.",
                    },
                    "trigger_type": {
                        "type": "string",
                        "enum": ["cron", "interval"],
                        "description": (
                            "'cron' for calendar-based schedules (uses 5-field cron syntax); "
                            "'interval' for fixed frequency (schedule = seconds between runs)."
                        ),
                    },
                    "schedule": {
                        "type": "string",
                        "description": (
                            "For cron: 5-field cron expression in local time "
                            "(e.g. '0 8 * * 1' = Monday 08:00, '30 9 * * 1-5' = weekdays 09:30). "
                            "For interval: number of seconds between runs (e.g. '3600' = hourly)."
                        ),
                    },
                    "action": {
                        "type": "string",
                        "description": (
                            "The tool to call when this automation fires. "
                            "Must be a registered tool such as 'spawn_agent', 'write_vault', "
                            "or 'search_vault_semantic'."
                        ),
                    },
                    "parameters": {
                        "type": "object",
                        "description": (
                            "Arguments for the action, matching that tool's parameter schema exactly. "
                            "For spawn_agent: include 'goal' (and optionally 'agent_name'). "
                            "The automation name will be prepended to the goal automatically."
                        ),
                    },
                    "enabled": {
                        "type": "boolean",
                        "description": "Whether to activate the automation immediately. Default: true.",
                    },
                },
                "required": ["name", "trigger_type", "schedule", "action", "parameters"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "toggle_automation",
            "description": (
                "Enable or disable a specific automation by its ID. "
                "Disabled automations remain in the registry but are skipped by the engine. "
                "Use list_automations to find the automation ID."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "automation_id": {
                        "type": "string",
                        "description": "The 8-character automation ID returned by create_automation.",
                    },
                    "enabled": {
                        "type": "boolean",
                        "description": "True to enable, false to disable.",
                    },
                },
                "required": ["automation_id", "enabled"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_automation",
            "description": (
                "Permanently remove an automation from the registry by its 8-character ID. "
                "This cannot be undone. Use list_automations to find the automation ID."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "automation_id": {
                        "type": "string",
                        "description": "The 8-character automation ID to permanently delete.",
                    },
                },
                "required": ["automation_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_swarm",
            "description": (
                "Start a rhizomatic peer swarm where agents collaborate on a shared Blackboard. "
                "Peers run independently, observe each other's findings, and may spawn additional peers. "
                "A SynthesisAgent spawns automatically when all peers complete to distill the final result. "
                "Returns swarm_id immediately — use list_agents() to monitor progress. "
                "Good for: complex multi-perspective research, vault-wide analysis, topics requiring debate."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string", "description": "The overarching goal for the swarm."},
                    "project_id": {"type": "string", "description": "Short identifier for this project's blackboard (no spaces, e.g. 'ai_analyse')."},
                    "roles": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of peer roles to spawn. Default: ['Researcher', 'Skeptic', 'Synthesist'].",
                    },
                    "swarm_size": {"type": "integer", "description": "Max number of peers (including auto-spawned). Default: 5."},
                    "report_to_chat": {
                        "type": "boolean",
                        "description": "If true, Luna is notified when the synthesis completes. Default: false.",
                    },
                },
                "required": ["goal", "project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_temp",
            "description": (
                "Write a transient/working-memory file to .agent_temp/. "
                "Use this for intermediate reasoning, scratchpad notes, draft fragments, "
                "and agent-to-agent communication. "
                "These files are NEVER indexed into the VectorDB and will be purged after the goal is complete. "
                "NEVER use write_vault for intermediate thoughts — use write_temp instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_name": {
                        "type": "string",
                        "description": "Relative path within .agent_temp/ (e.g. '{agent_id}/scratchpad.md').",
                    },
                    "content": {
                        "type": "string",
                        "description": "The transient content to write.",
                    },
                },
                "required": ["file_name", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_temp",
            "description": "Read a transient file from .agent_temp/ (working memory).",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_name": {
                        "type": "string",
                        "description": "Relative path within .agent_temp/ to read.",
                    }
                },
                "required": ["file_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cleanup_transient_data",
            "description": (
                "Purge all transient working-memory files for a given agent_id from .agent_temp/. "
                "MUST be called at the end of every agent goal cycle, after the final result "
                "has been committed to Sovereign Memory (write_vault)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "The agent_id whose transient directory should be deleted.",
                    }
                },
                "required": ["agent_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "Recursively list all files in a vault directory. "
                "Returns a sorted list of relative file paths. "
                "Use this to get a real-time overview of vault structure without relying on semantic search. "
                "Omit 'directory' or pass an empty string to list the entire vault."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": "Relative path of the directory to list (e.g. '20_Werk'). Leave empty for the vault root.",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_file",
            "description": (
                "Move or rename a file within the vault. "
                "Use this to physically reorganize notes — e.g. promote a note from '20_Werk' to '10_Kern', "
                "or rename a file. Both source and destination must stay inside the vault."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source_path": {
                        "type": "string",
                        "description": "Relative path of the file to move (e.g. '20_Werk/idee.md').",
                    },
                    "destination_path": {
                        "type": "string",
                        "description": "Relative destination path including filename (e.g. '10_Kern/idee.md').",
                    },
                },
                "required": ["source_path", "destination_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": (
                "Permanently delete a file from the vault. "
                "Use this to remove duplicates, test-notes, or systemic noise. "
                "This action is irreversible — confirm intent before calling."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Relative path of the file to delete (e.g. '99_Archief/oud.md').",
                    }
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_vault_semantic",
            "description": (
                "Semantic search across the entire fractalisme vault using vector embeddings. "
                "ALWAYS call this before claiming that information is missing or unknown. "
                "A background Sovereign Memory Engine writes SovereignLog files to the vault automatically — "
                "content may exist even if write_vault was never called in this session. "
                "Use this to find relevant notes by meaning and context rather than exact filenames. "
                "Returns the top 5 most relevant text fragments, each prefixed with its filename and "
                "ISO 8601 timestamp so you can reason about temporal evolution of insights. "
                "Every entry is tagged with a chronological search tag in the format #YYYY-MM (e.g. #2026-08). "
                "To filter for recent entries, include the current month tag in your query (e.g. '#2026-08 zelfzorg'). "
                "To search across a specific period, combine month tags with your topic keywords."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query in natural language.",
                    }
                },
                "required": ["query"],
            },
        },
    },
]

TOOL_HANDLERS = {
    "read_vault": lambda args: read_vault(args["file_name"]),
    "write_vault": lambda args: write_vault(args["file_name"], args["content"]),
    "sync_vault": lambda args: sync_vault(),
    "write_temp": lambda args: write_temp(args["file_name"], args["content"]),
    "read_temp": lambda args: read_temp(args["file_name"]),
    "cleanup_transient_data": lambda args: cleanup_transient_data(args["agent_id"]),
    "list_files": lambda args: list_files(args.get("directory", "")),
    "move_file": lambda args: move_file(args["source_path"], args["destination_path"]),
    "delete_file": lambda args: delete_file(args["file_path"]),
    "search_vault_semantic": lambda args: _search_vault_semantic(args["query"]),
    "analyze_website": lambda args: analyze_website(args["url"]),
    "browser_navigate": lambda args: _browser_navigate(args["url"], args.get("session_id", "default")),
    "browser_click": lambda args: _browser_click(args["selector"], args.get("session_id", "default")),
    "browser_extract_content": lambda args: _browser_extract_content(args.get("session_id", "default")),
    "browser_screenshot": lambda args: _browser_screenshot(args.get("session_id", "default")),
    "browser_close_session": lambda args: _browser_close_session(args.get("session_id", "default")),
    # Agent management — imported lazily to avoid circular imports
    "spawn_agent": lambda args: _agent_spawn(args["goal"], args.get("agent_name", "Agent"), args.get("report_to_chat", False)),
    "get_agent_status": lambda args: _agent_status(args["agent_id"]),
    "list_agents": lambda args: _agent_list(),
    # Swarm tools
    "write_blackboard": lambda args: write_blackboard(args["project_id"], args["fragment"], args.get("label", "")),
    "read_blackboard": lambda args: read_blackboard(args["project_id"]),
    "send_signal": lambda args: send_signal(args["recipient"], args["message"], args.get("sender", "unknown")),
    "read_signals": lambda args: read_signals(args["agent_name"]),
    "spawn_peer": lambda args: spawn_peer(args["goal"], args["role"], args["project_id"], args["swarm_id"]),
    "spawn_swarm": lambda args: _swarm_spawn(args["goal"], args["project_id"], args.get("roles"), args.get("swarm_size", 5), args.get("report_to_chat", False)),
    # Timezone
    "set_timezone": lambda args: set_timezone(args["timezone_string"]),
    "get_timezone": lambda args: get_timezone(),
    # Notifications
    "write_notification": lambda args: write_notification(
        args["content"],
        args.get("agent_id", "system"),
        args.get("priority", "medium"),
        args.get("category", "insight"),
        args.get("related_file"),
    ),
    "get_pending_notifications": lambda args: get_pending_notifications(),
    # Scheduler
    "schedule_task": lambda args: schedule_task(args["execution_time"], args["action"], args["parameters"], args.get("description", "")),
    "list_scheduled_tasks": lambda args: list_scheduled_tasks(args.get("include_done", False)),
    "cancel_scheduled_task": lambda args: cancel_scheduled_task(args["task_id"]),
    # User status / sleep mode
    "set_sleep_mode": lambda args: set_sleep_mode(args["sleeping"]),
    "get_user_status": lambda args: get_user_status(),
    # Automations
    "list_automations": lambda args: list_automations(),
    "create_automation": lambda args: create_automation(
        args["name"],
        args["trigger_type"],
        args["schedule"],
        args["action"],
        args["parameters"],
        args.get("enabled", True),
    ),
    "toggle_automation": lambda args: toggle_automation(args["automation_id"], args["enabled"]),
    "delete_automation": lambda args: delete_automation(args["automation_id"]),
}


def _browser_navigate(url: str, session_id: str) -> str:
    from browser import browser_navigate
    return browser_navigate(url, session_id)


def _browser_click(selector: str, session_id: str) -> str:
    from browser import browser_click
    return browser_click(selector, session_id)


def _browser_extract_content(session_id: str) -> str:
    from browser import browser_extract_content
    return browser_extract_content(session_id)


def _browser_screenshot(session_id: str) -> str:
    from browser import browser_screenshot
    return browser_screenshot(session_id)


def _browser_close_session(session_id: str) -> str:
    from browser import browser_close_session
    return browser_close_session(session_id)


def _agent_spawn(goal: str, agent_name: str, report_to_chat: bool = False) -> str:
    from agent import launch_agent
    from chat_bridge import get_context_injector, get_llm_trigger
    injector = get_context_injector() if report_to_chat else None
    trigger = get_llm_trigger() if report_to_chat else None
    return launch_agent(goal, agent_name, context_injector=injector, llm_trigger=trigger)


def _agent_status(agent_id: str) -> str:
    from agent import get_agent_status
    return get_agent_status(agent_id)


def _agent_list() -> str:
    from agent import list_agents
    return list_agents()


def _swarm_spawn(
    goal: str, project_id: str,
    roles: list | None, swarm_size: int, report_to_chat: bool = False,
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
