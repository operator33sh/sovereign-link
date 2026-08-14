"""
Background Agent infrastructure for Sovereign-Link.

Architecture: Goal → [Observe → Reason → Act → Evaluate] loop → Report

Agents run in daemon threads so they never block Luna's main chat.
Luna can spawn, monitor and manage agents via tool calls.
"""

import json
import logging
import os
import threading
from datetime import datetime

# Configurable via environment variables
_AGENT_MAX_ITERATIONS = int(os.environ.get("AGENT_MAX_ITERATIONS", 50))
_AGENT_LLM_TIMEOUT = float(os.environ.get("AGENT_LLM_TIMEOUT", 180.0))

from tools import TOOL_DEFINITIONS, TOOL_HANDLERS, write_vault, sync_vault

# ------------------------------------------------------------------
# Global agent registry — thread-safe
# ------------------------------------------------------------------

_registry_lock = threading.Lock()
_registry: dict[str, dict] = {}
# Schema per entry:
# {
#   "id": str,
#   "name": str,
#   "goal": str,
#   "status": "running" | "completed" | "failed" | "timeout",
#   "result": str | None,
#   "log_file": str,
#   "started_at": str (ISO),
#   "finished_at": str | None,
# }

logger = logging.getLogger(__name__)

_AGENT_SYSTEM_PROMPT = """You are a Background Agent running inside the Sovereign-Link AI system.
Your job is to autonomously achieve the given goal using the tools available to you.

Available tools: read_vault, write_vault, sync_vault, analyze_website, search_vault_semantic.

Work methodically through the Observe → Reason → Act → Evaluate cycle:
- OBSERVE: gather information needed to make progress
- REASON: decide the best next action
- ACT: call the appropriate tool
- EVALUATE: assess whether the goal has been reached

When the goal is fully achieved, end your response with exactly this marker on its own line:
GOAL_COMPLETE: <one-sentence summary of what was accomplished>

Do not claim GOAL_COMPLETE until all deliverables (files written, reports saved, etc.) are done.
"""

_EVAL_PROMPT = (
    "Evaluate your progress toward the goal so far. "
    "If it is fully achieved (all files written, all checks done), respond with "
    "'GOAL_COMPLETE: <summary>'. Otherwise, state what still needs to be done and take the next action."
)


class BackgroundAgent:
    """Goal-driven agent that loops until completion or max_iterations."""

    def __init__(
        self,
        goal: str,
        agent_name: str = "Agent",
        max_iterations: int = _AGENT_MAX_ITERATIONS,
        context_injector=None,
        llm_trigger=None,
    ):
        self.goal = goal
        self.agent_name = agent_name
        self.max_iterations = max_iterations
        self._context_injector = context_injector  # callable(text: str) | None
        self._llm_trigger = llm_trigger            # callable() | None

        date_str = datetime.now().strftime("%Y-%m-%d")
        self.log_file = f"agents/AgentLog_{date_str}.md"
        self._log_lines: list[str] = [
            f"# Agent Log — {date_str}\n\n",
            f"**Agent:** {agent_name}  \n",
            f"**Goal:** {goal}  \n",
            f"**Started:** {datetime.now().strftime('%H:%M:%S')}  \n\n",
            "---\n\n",
        ]
        self._messages: list[dict] = []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _flush_log(self) -> None:
        """Write current log state to vault (best-effort)."""
        try:
            write_vault(self.log_file, "".join(self._log_lines))
        except Exception:
            logger.exception("Agent: failed to flush log to vault")

    def _append_log(self, phase: str, content: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_lines.append(f"### [{ts}] {phase}\n\n{content}\n\n---\n\n")
        self._flush_log()

    def _llm_call(self) -> dict:
        """Call the LLM with the current message history and tool definitions."""
        # Import here to avoid circular imports at module load time
        import httpx
        import os

        base_url = os.environ.get("OLLAMA_BASE_URL", "https://ollama.com")
        api_key = os.environ.get("OLLAMA_API_KEY", "")
        model = os.environ.get("OLLAMA_MODEL", "llama3.1")

        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        payload = {
            "model": model,
            "messages": self._messages,
            "tools": TOOL_DEFINITIONS,
            "stream": False,
        }
        client = httpx.Client(base_url=base_url, headers=headers, timeout=_AGENT_LLM_TIMEOUT)
        response = client.post("/v1/chat/completions", json=payload)
        response.raise_for_status()
        return response.json()

    def _execute_tool_calls(self, tool_calls: list) -> list[str]:
        """Run each tool call and return formatted log entries."""
        log_entries = []
        for tc in tool_calls:
            fn_name = tc["function"]["name"]
            try:
                fn_args = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError:
                fn_args = {}

            handler = TOOL_HANDLERS.get(fn_name)
            result = handler(fn_args) if handler else f"Error: unknown tool '{fn_name}'"

            log_entries.append(
                f"**Tool:** `{fn_name}`  \n"
                f"**Args:** `{json.dumps(fn_args)}`  \n"
                f"**Result:**\n```\n{str(result)[:800]}\n```"
            )
            self._messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })

        return log_entries

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> str:
        """
        Execute the agent loop.
        Returns a human-readable result string.
        Designed to be called inside asyncio.to_thread().
        """
        self._messages = [
            {"role": "system", "content": _AGENT_SYSTEM_PROMPT},
            {"role": "user", "content": f"Goal: {self.goal}"},
        ]
        self._append_log("INIT", f"Goal accepted: **{self.goal}**")

        for iteration in range(1, self.max_iterations + 1):
            self._append_log(
                f"ITERATION {iteration} — OBSERVE / REASON",
                "Calling LLM to determine next action…",
            )

            try:
                data = self._llm_call()
            except Exception as e:
                msg = f"LLM call failed: {e}"
                self._append_log("ERROR", msg)
                self._finalize("FAILED")
                return f"Agent '{self.agent_name}' failed: {e}"

            choice = data["choices"][0]
            message = choice["message"]
            finish_reason = choice.get("finish_reason", "stop")

            # --- ACT: tool calls ---
            if finish_reason == "tool_calls" or message.get("tool_calls"):
                tool_calls = message["tool_calls"]
                self._messages.append({"role": "assistant", "tool_calls": tool_calls})

                log_entries = self._execute_tool_calls(tool_calls)
                self._append_log(
                    f"ITERATION {iteration} — ACT",
                    "\n\n".join(log_entries),
                )
                # Loop back for the next reasoning step
                continue

            # --- EVALUATE: text response ---
            text = message.get("content") or ""
            self._messages.append({"role": "assistant", "content": text})

            if "GOAL_COMPLETE:" in text:
                self._append_log("COMPLETE", text)
                self._finalize("SUCCESS")
                sync_vault()
                self._notify_chat(text)
                return text

            # Not done yet — log this reasoning step and prompt for evaluation
            self._append_log(f"ITERATION {iteration} — EVALUATE", text)
            self._messages.append({"role": "user", "content": _EVAL_PROMPT})

        # Max iterations reached without completion
        self._append_log(
            "TIMEOUT",
            f"Reached maximum of {self.max_iterations} iterations without GOAL_COMPLETE.",
        )
        self._finalize("TIMEOUT")
        sync_vault()
        result = (
            f"Agent '{self.agent_name}' reached max iterations ({self.max_iterations}). "
            f"Partial log saved to `{self.log_file}`."
        )
        self._notify_chat(result)
        return result

    def _finalize(self, status: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_lines.append(
            f"## Final Status: {status}\n\n**Finished:** {ts}  \n"
        )
        self._flush_log()

    def _write_final_report(self, result: str) -> str:
        """Write a structured final report to the vault. Returns the report path."""
        date_str = datetime.now().strftime("%Y-%m-%d")
        report_path = f"agents/reports/{self.agent_name}_{date_str}.md"

        conclusion = ""
        for line in result.splitlines():
            if line.strip().startswith("GOAL_COMPLETE:"):
                conclusion = line.split("GOAL_COMPLETE:", 1)[1].strip()
                break

        status = "Voltooid" if conclusion else "Timeout / Gedeeltelijk"
        content = (
            f"# Agent Rapport — {self.agent_name}\n\n"
            f"**Datum:** {date_str}  \n"
            f"**Doel:** {self.goal}  \n"
            f"**Status:** {status}  \n\n"
            "---\n\n"
            "## Conclusie\n\n"
            + (conclusion if conclusion else "_Geen GOAL_COMPLETE marker gevonden._")
            + "\n\n"
            "## Volledige uitvoer\n\n"
            f"{result}\n\n"
            f"**Uitvoeringslog:** `{self.log_file}`\n"
        )
        write_vault(report_path, content)
        return report_path

    def _notify_chat(self, result: str) -> None:
        """Always write a final vault report. Notify Luna via context injection if callbacks set."""
        report_path = self._write_final_report(result)

        if not self._context_injector and not self._llm_trigger:
            return

        if self._context_injector:
            try:
                notification = (
                    f"[AGENT VOLTOOID — {self.agent_name}]\n\n"
                    f"**Doel:** {self.goal}\n"
                    f"**Rapport:** `{report_path}`\n\n"
                    "Lees het rapport via `read_vault` en presenteer de bevindingen "
                    "aan de gebruiker in jouw eigen stem."
                )
                self._context_injector(notification)
            except Exception:
                logger.exception("Agent %s: failed to inject notification", self.agent_name)

        if self._llm_trigger:
            try:
                self._llm_trigger()
            except Exception:
                logger.exception("Agent %s: autonomous LLM trigger failed", self.agent_name)


# ------------------------------------------------------------------
# Registry helpers
# ------------------------------------------------------------------

def _register(agent_id: str, name: str, goal: str, log_file: str) -> None:
    with _registry_lock:
        _registry[agent_id] = {
            "id": agent_id,
            "name": name,
            "goal": goal,
            "status": "running",
            "result": None,
            "log_file": log_file,
            "started_at": datetime.now().isoformat(),
            "finished_at": None,
        }


def _update_registry(agent_id: str, status: str, result: str) -> None:
    with _registry_lock:
        if agent_id in _registry:
            _registry[agent_id]["status"] = status
            _registry[agent_id]["result"] = result
            _registry[agent_id]["finished_at"] = datetime.now().isoformat()


# ------------------------------------------------------------------
# Public API — callable as Luna tool calls
# ------------------------------------------------------------------

def launch_agent(goal: str, agent_name: str = "Agent", context_injector=None, llm_trigger=None) -> str:
    """
    Start a background agent in a daemon thread.
    Returns immediately with an agent_id string.
    Luna can use get_agent_status(agent_id) to poll for results.

    context_injector: optional callable(text: str) — injects agent notification into LLM context.
    llm_trigger: optional callable() — triggers autonomous Luna response after injection.
    """
    agent_id = f"{agent_name}_{datetime.now().strftime('%H%M%S')}"
    agent = BackgroundAgent(goal=goal, agent_name=agent_name, context_injector=context_injector, llm_trigger=llm_trigger)
    _register(agent_id, agent_name, goal, agent.log_file)

    def _run() -> None:
        try:
            result = agent.run()
            status = "completed" if "GOAL_COMPLETE:" in result else "timeout"
            _update_registry(agent_id, status, result)
        except Exception as e:
            _update_registry(agent_id, "failed", f"Exception: {e}")
            logger.exception("Agent %s failed", agent_id)

    thread = threading.Thread(target=_run, daemon=True, name=agent_id)
    thread.start()

    return (
        f"Agent started.\n"
        f"**ID:** `{agent_id}`\n"
        f"**Goal:** {goal}\n"
        f"**Log:** `{agent.log_file}`\n\n"
        f"Use `get_agent_status` with id `{agent_id}` to check progress."
    )


def get_agent_status(agent_id: str) -> str:
    """Return current status and result (if done) for a given agent_id."""
    with _registry_lock:
        entry = _registry.get(agent_id)

    if not entry:
        ids = list(_registry.keys())
        hint = f"Known agents: {ids}" if ids else "No agents have been launched this session."
        return f"Unknown agent id '{agent_id}'. {hint}"

    lines = [
        f"**Agent:** {entry['name']}",
        f"**Status:** {entry['status']}",
        f"**Goal:** {entry['goal']}",
        f"**Started:** {entry['started_at']}",
    ]
    if entry["finished_at"]:
        lines.append(f"**Finished:** {entry['finished_at']}")
    lines.append(f"**Log file:** `{entry['log_file']}`")
    if entry["result"]:
        lines.append(f"\n**Result:**\n{entry['result'][:1200]}")
    return "\n".join(lines)


def list_agents() -> str:
    """Return a summary of all agents launched this session."""
    with _registry_lock:
        entries = list(_registry.values())

    if not entries:
        return "No agents have been launched this session."

    lines = ["**Background Agents — this session:**\n"]
    for e in entries:
        lines.append(
            f"- `{e['id']}` — **{e['status']}** — {e['goal'][:80]}"
            + ("…" if len(e["goal"]) > 80 else "")
        )
    return "\n".join(lines)


# ------------------------------------------------------------------
# Built-in agents
# ------------------------------------------------------------------

def run_system_check() -> str:
    """
    System Check Agent: scans tools, tests LLM reasoning, checks vault
    connectivity, and writes a health report to the vault.
    Blocking — intended for /syscheck Telegram command.
    """
    goal = (
        "Perform a complete system health check for Sovereign-Link. Steps:\n"
        "1. List all available tools and summarise each one's purpose.\n"
        "2. Test LLM reasoning: what is the sum of the first 7 prime numbers? Show working.\n"
        "3. Test vault connectivity: call search_vault_semantic with query 'system health test'.\n"
        "4. Write a structured health report to 'agents/SystemCheck_Report.md' with sections: "
        "   Tool Inventory, LLM Reasoning Test, Vault Connectivity, Overall Health Verdict.\n"
        "5. Once the report is written, declare GOAL_COMPLETE."
    )
    agent = BackgroundAgent(goal=goal, agent_name="SystemCheckAgent")
    return agent.run()


def spawn_agent_blocking(goal: str, agent_name: str = "BackgroundAgent") -> str:
    """Blocking wrapper for Telegram /agent command (runs in asyncio.to_thread)."""
    agent = BackgroundAgent(goal=goal, agent_name=agent_name)
    return agent.run()
