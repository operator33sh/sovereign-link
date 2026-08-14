import logging
import os
import random
import subprocess
from datetime import datetime
from urllib.parse import urlparse

import httpx
import trafilatura

from vector import index_file as _index_file
from vector import search_vault_semantic as _search_vault_semantic

logger = logging.getLogger(__name__)

VAULT_PATH = os.environ.get("VAULT_PATH", "/home/wouter/Documents/fractalisme-vault")


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


_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

_MAX_CONTENT_CHARS = 8000


def analyze_website(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "Error: only HTTP/HTTPS URLs are allowed"

    headers = {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }

    try:
        response = httpx.get(url, headers=headers, follow_redirects=True, timeout=20.0)
    except httpx.TimeoutException:
        return "Error: request timed out"
    except Exception as e:
        return f"Error fetching URL: {e}"

    if response.status_code == 403:
        return "Error: access forbidden (403) — the site blocked the request"
    if response.status_code == 429:
        return "Error: rate limited (429) — try again later"
    if response.status_code >= 400:
        return f"Error: HTTP {response.status_code}"

    extracted = trafilatura.extract(
        response.text,
        output_format="markdown",
        include_comments=False,
        include_tables=True,
        no_fallback=False,
    )

    if not extracted:
        return "Error: could not extract readable content from this page"

    if len(extracted) > _MAX_CONTENT_CHARS:
        extracted = extracted[:_MAX_CONTENT_CHARS] + f"\n\n[... truncated at {_MAX_CONTENT_CHARS} chars ...]"

    return extracted


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
    "search_vault_semantic": lambda args: _search_vault_semantic(args["query"]),
    "analyze_website": lambda args: analyze_website(args["url"]),
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
}


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
