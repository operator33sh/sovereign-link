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
import time
from datetime import datetime

# Configurable via environment variables
_AGENT_MAX_ITERATIONS = int(os.environ.get("AGENT_MAX_ITERATIONS", 50))
_AGENT_LLM_TIMEOUT = float(os.environ.get("AGENT_LLM_TIMEOUT", 180.0))
_AGENT_SWARM_SIZE = int(os.environ.get("AGENT_SWARM_SIZE", 5))
_SWARM_AGENT_TIMEOUT = int(os.environ.get("SWARM_AGENT_TIMEOUT", 300))   # 5 min watchdog
_SWARM_MAX_RETRIES = int(os.environ.get("SWARM_MAX_RETRIES", 3))

_RETRY_CONTEXT_PREFIX = (
    "Vorige poging is getimed uit. Analyseer het blackboard om te zien waar de "
    "blokkade zat en probeer een alternatieve route naar het doel."
)

from tools import TOOL_DEFINITIONS, AGENT_TOOL_DEFINITIONS, TOOL_HANDLERS, sync_vault, AGENT_TEMP_PATH, PROJECT_LOGS_PATH

# Execution logs — outside the vault, never indexed or synced
LOGS_PATH = os.path.join(PROJECT_LOGS_PATH, "agents")

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
#   "swarm_id": str | None,
#   "role": str | None,
# }

_swarm_registry: dict[str, "SwarmCoordinator"] = {}

logger = logging.getLogger(__name__)

_AGENT_SYSTEM_PROMPT = """You are a Background Agent running inside the Sovereign-Link AI system.
Your job is to autonomously achieve the given goal using the tools available to you.

## Memory Architecture — STRICT SEPARATION

### Working Memory (Transient) — use write_temp
Any intermediate reasoning, scratchpad notes, draft fragments, hypotheses in progress,
or agent-to-agent communication must go to .agent_temp/ using write_temp.
These files are NEVER indexed and will be purged after goal completion.

### Sovereign Memory (Permanent) — use write_vault
Only validated, final-state insights, deliverables, and SovereignLog entries belong here.
Before calling write_vault, ask yourself:
  "Is this a final, validated insight that contributes to long-term memory?"
If the answer is "not yet" — use write_temp instead.

### Taal van Vault-notities
Alle notities die je opslaat via write_vault MOETEN in het Nederlands geschreven zijn.
Als de broninhoud in het Engels of een andere taal is, vertaal je deze naar het Nederlands vóór het opslaan.
Titels, koppen, tags en de volledige inhoud zijn altijd in het Nederlands.

### Forbidden patterns
- NO write_vault for intermediate thoughts, reasoning logs, or draft notes.
- NO files with agent IDs, hashes, or "_temp/_draft/_v1" in their names in main vault dirs.
- NO "Report_XXXXX.md" files unless explicitly requested as a final deliverable.

### Cleanup
When GOAL_COMPLETE, call cleanup_transient_data with your agent_id to purge working memory.

## Reasoning cycle
Work methodically through Observe → Reason → Act → Evaluate:
- OBSERVE: gather information (read_vault, search_vault_semantic, analyze_website)
- REASON: decide the best next action; use write_temp for scratchpad
- ACT: call the appropriate tool
- EVALUATE: assess whether the goal has been reached

When the goal is fully achieved, end your response with exactly this marker on its own line:
GOAL_COMPLETE: <one-sentence summary of what was accomplished>

Do not claim GOAL_COMPLETE until all deliverables are written and transient data is cleaned up.
"""

_EVAL_PROMPT = (
    "Evaluate your progress toward the goal so far. "
    "If it is fully achieved (all files written, all checks done), respond with "
    "'GOAL_COMPLETE: <summary>'. Otherwise, state what still needs to be done and take the next action."
)

_SWARM_SYSTEM_PROMPT_TEMPLATE = _AGENT_SYSTEM_PROMPT + """

## Swarm Protocol
You are part of a peer swarm working on project '{project_id}' as a '{role}'.
- Use `write_blackboard(project_id, fragment, label)` to post insights for peers.
- Use `read_blackboard(project_id)` periodically to observe peer findings and adjust your course.
  If you notice a contradiction or gap, pivot your research to address it ('constructive interference').
- Use `send_signal(recipient, message, sender)` to notify a specific peer directly.
- Use `read_signals(agent_name)` to check for incoming peer messages.
- Use `spawn_peer(goal, role, project_id, swarm_id)` to add a complementary peer
  (e.g. a 'Skeptic' if you need your hypothesis challenged). Max swarm size: {swarm_size}.
- You do NOT manage other agents — you collaborate as equals.
- When done, post your final conclusion to the blackboard and declare GOAL_COMPLETE.

## Mandatory Evidence Loop — VERPLICHT
Elke tool-call die een permanente wijziging aanbrengt (`write_vault`, `move_file`, `delete_file`)
MOET onmiddellijk gevolgd worden door een `write_blackboard` entry. Een actie is pas 'voltooid'
als het bewijs op het blackboard staat. Sla NOOIT GOAL_COMPLETE op voordat alle vault-acties
zijn gelogd op het blackboard.

Gebruik het volgende formaat voor elke blackboard-entry na een vault-actie:
  `[ACTIE] | [BESTAND] | [RESULTAAT/WIJZIGING]`

Voorbeelden:
  `WRITE_VAULT | concepts/recursive-autonomy.md | Nieuw inzicht aangemaakt: definitie van recursieve autonomie`
  `MOVE_FILE | drafts/idee.md → concepts/idee.md | Verplaatst van draft naar definitieve locatie`
  `DELETE_FILE | temp/oud-fragment.md | Verouderd fragment verwijderd na synthese`

## Read-Modify-Write Safeguard
Wanneer je een bestaand vault-bestand moet updaten, volg dan ALTIJD dit protocol:
1. LEZEN — roep `read_vault` aan op het doelbestand en sla de volledige inhoud op.
2. TRANSFORMEREN — voer de wijziging door in je redenering (voeg toe, pas aan, verwijder secties).
3. SCHRIJVEN — roep `write_vault` aan met de VOLLEDIGE gecombineerde inhoud (oud + nieuw).
Schrijf NOOIT alleen de nieuwe sectie; dit overschrijft bestaande inhoud. Bij grote bestanden
(>500 regels): verwerk in logische blokken en log elk blok apart op het blackboard.
"""


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
        ts_str = datetime.now().strftime("%H%M%S")
        self._temp_dir_name = f"{agent_name}_{ts_str}"
        # Execution log lives outside the vault — never indexed, never synced to git
        self.log_file = os.path.join(LOGS_PATH, f"{agent_name}_{date_str}_{ts_str}.md")
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
        """Write execution log to LOGS_PATH — outside the vault, never indexed."""
        try:
            os.makedirs(LOGS_PATH, exist_ok=True)
            with open(self.log_file, "w", encoding="utf-8") as f:
                f.write("".join(self._log_lines))
        except Exception:
            logger.exception("Agent: failed to flush log to logs/")

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
            "tools": AGENT_TOOL_DEFINITIONS,
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
            if handler is None:
                result = f"Error: unknown tool '{fn_name}'"
            else:
                try:
                    result = handler(fn_args)
                except KeyError as exc:
                    result = f"Error: missing required argument {exc} for tool '{fn_name}'"
                except Exception as exc:
                    logger.exception("Tool '%s' raised an unexpected error", fn_name)
                    result = f"Error: tool '{fn_name}' failed: {exc}"

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
        identity_block = (
            f"\n\n## Your Identity\n"
            f"- **agent_name:** `{self.agent_name}`\n"
            f"- **temp_dir:** `{self._temp_dir_name}/` — use this prefix for all write_temp calls "
            f"(e.g. `write_temp(\"{self._temp_dir_name}/scratchpad.md\", ...)`)\n"
            f"- **cleanup:** call `cleanup_transient_data` with agent_id=`{self._temp_dir_name}` at GOAL_COMPLETE\n"
        )
        self._messages = [
            {"role": "system", "content": _AGENT_SYSTEM_PROMPT + identity_block},
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
                self._purge_temp()
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
            f"Partial log at: {self.log_file}"
        )
        self._notify_chat(result)
        return result

    def _purge_temp(self) -> None:
        """Delete this agent's transient working directory from .agent_temp/."""
        import shutil
        temp_dir = os.path.join(AGENT_TEMP_PATH, self._temp_dir_name)
        try:
            if os.path.isdir(temp_dir):
                shutil.rmtree(temp_dir)
                logger.info("Agent %s: purged transient data at %s", self.agent_name, temp_dir)
        except Exception:
            logger.exception("Agent %s: failed to purge temp dir %s", self.agent_name, temp_dir)

    def _finalize(self, status: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_lines.append(
            f"## Final Status: {status}\n\n**Finished:** {ts}  \n"
        )
        self._flush_log()

    def _notify_chat(self, result: str) -> None:
        """Notify Luna via context injection if callbacks are set. No vault write."""
        if not self._context_injector and not self._llm_trigger:
            return

        conclusion = ""
        for line in result.splitlines():
            if line.strip().startswith("GOAL_COMPLETE:"):
                conclusion = line.split("GOAL_COMPLETE:", 1)[1].strip()
                break

        if self._context_injector:
            try:
                notification = (
                    f"[AGENT VOLTOOID — {self.agent_name}]\n\n"
                    f"**Doel:** {self.goal}\n"
                    f"**Conclusie:** {conclusion or '(timeout — geen GOAL_COMPLETE bereikt)'}\n"
                    f"**Uitvoeringslog:** `{self.log_file}`"
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
# Swarm Agent — peer-aware subclass of BackgroundAgent
# ------------------------------------------------------------------

class SwarmAgent(BackgroundAgent):
    """Peer agent that reads/writes a shared Blackboard and can spawn sibling peers."""

    def __init__(
        self,
        goal: str,
        agent_name: str,
        project_id: str,
        role: str,
        swarm_id: str,
        swarm_size: int = _AGENT_SWARM_SIZE,
        max_iterations: int = _AGENT_MAX_ITERATIONS,
    ):
        super().__init__(goal=goal, agent_name=agent_name, max_iterations=max_iterations)
        self.project_id = project_id
        self.role = role
        self.swarm_id = swarm_id
        self.swarm_size = swarm_size
        self._system_prompt = _SWARM_SYSTEM_PROMPT_TEMPLATE.format(
            project_id=project_id,
            role=role,
            swarm_size=swarm_size,
        )

    def run(self) -> str:
        """Override to use swarm-aware system prompt."""
        identity_block = (
            f"\n\n## Your Identity\n"
            f"- **agent_name:** `{self.agent_name}`\n"
            f"- **temp_dir:** `{self._temp_dir_name}/` — use this prefix for all write_temp calls "
            f"(e.g. `write_temp(\"{self._temp_dir_name}/scratchpad.md\", ...)`)\n"
            f"- **cleanup:** call `cleanup_transient_data` with agent_id=`{self._temp_dir_name}` at GOAL_COMPLETE\n"
        )
        self._messages = [
            {"role": "system", "content": self._system_prompt + identity_block},
            {"role": "user", "content": f"Goal: {self.goal}"},
        ]
        self._append_log("INIT", f"[{self.role}] Goal accepted: **{self.goal}**")
        self._append_log("SWARM", f"Project: `{self.project_id}` | Swarm: `{self.swarm_id}`")

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

            if finish_reason == "tool_calls" or message.get("tool_calls"):
                tool_calls = message["tool_calls"]
                self._messages.append({"role": "assistant", "tool_calls": tool_calls})
                log_entries = self._execute_tool_calls(tool_calls)
                self._append_log(f"ITERATION {iteration} — ACT", "\n\n".join(log_entries))
                continue

            text = message.get("content") or ""
            self._messages.append({"role": "assistant", "content": text})

            if "GOAL_COMPLETE:" in text:
                self._append_log("COMPLETE", text)
                self._finalize("SUCCESS")
                self._purge_temp()
                sync_vault()
                self._notify_chat(text)
                return text

            self._append_log(f"ITERATION {iteration} — EVALUATE", text)
            self._messages.append({"role": "user", "content": _EVAL_PROMPT})

        self._append_log(
            "TIMEOUT",
            f"Reached maximum of {self.max_iterations} iterations without GOAL_COMPLETE.",
        )
        self._finalize("TIMEOUT")
        sync_vault()
        result = (
            f"Agent '{self.agent_name}' reached max iterations ({self.max_iterations}). "
            f"Partial log at: {self.log_file}"
        )
        self._notify_chat(result)
        return result


# ------------------------------------------------------------------
# Swarm Coordinator — manages a peer swarm and auto-spawns SynthesisAgent
# ------------------------------------------------------------------

class SwarmCoordinator:
    """Manages a set of peer SwarmAgents on a shared Blackboard."""

    def __init__(self, project_id: str, swarm_size: int = _AGENT_SWARM_SIZE):
        self.project_id = project_id
        self.swarm_size = swarm_size
        self._lock = threading.Lock()
        # agent_id → {thread, agent, role, goal, start_time, retry_count, retry_key, watchdog_triggered}
        self._peers: dict[str, dict] = {}
        # retry_key (role) → count of attempts used
        self._retry_counts: dict[str, int] = {}
        # retry_keys that have exhausted all retries
        self._permanently_failed: set[str] = set()
        self._swarm_id = f"swarm_{project_id}_{datetime.now().strftime('%H%M%S')}"
        self.context_injector = None
        self.llm_trigger = None

    def launch(self, initial_peers: list[dict]) -> str:
        """Start all initial peers and the completion monitor. Returns swarm_id."""
        for p in initial_peers:
            self.add_peer(p["goal"], p["role"])
        monitor = threading.Thread(target=self._monitor, daemon=True, name=f"{self._swarm_id}_monitor")
        monitor.start()
        return self._swarm_id

    def add_peer(self, goal: str, role: str, retry_key: str | None = None,
                 retry_count: int = 0) -> str:
        """Spawn a peer agent. retry_key groups retries under the same logical slot."""
        with self._lock:
            active = sum(1 for p in self._peers.values() if p["thread"].is_alive())
            if active >= self.swarm_size:
                return f"Swarm size limit ({self.swarm_size}) reached — peer not spawned."
            agent_name = f"{role}_{datetime.now().strftime('%H%M%S')}"
            if retry_key is None:
                retry_key = role

        agent = SwarmAgent(
            goal=goal,
            agent_name=agent_name,
            project_id=self.project_id,
            role=role,
            swarm_id=self._swarm_id,
            swarm_size=self.swarm_size,
        )
        agent_id = agent_name
        _register(agent_id, agent_name, goal, agent.log_file,
                  swarm_id=self._swarm_id, role=role)

        def _run():
            result = agent.run()
            status = "completed" if "GOAL_COMPLETE:" in result else "timeout"
            _update_registry(agent_id, status, result)

        t = threading.Thread(target=_run, daemon=True, name=agent_id)
        with self._lock:
            self._peers[agent_id] = {
                "thread": t,
                "agent": agent,
                "role": role,
                "goal": goal,
                "start_time": time.monotonic(),
                "retry_count": retry_count,
                "retry_key": retry_key,
                "watchdog_triggered": False,
            }
        t.start()
        return f"Peer '{agent_name}' ({role}) spawned in swarm '{self._swarm_id}'."

    def _monitor(self):
        """Watchdog loop: detects timeouts, triggers retries, spawns SynthesisAgent when settled."""
        from tools import write_blackboard  # local import to avoid circular deps

        while True:
            time.sleep(10)

            with self._lock:
                peers_snapshot = dict(self._peers)

            now = time.monotonic()
            for agent_id, info in peers_snapshot.items():
                if info["watchdog_triggered"]:
                    continue

                thread_alive = info["thread"].is_alive()
                elapsed = now - info["start_time"]

                if thread_alive and elapsed >= _SWARM_AGENT_TIMEOUT:
                    # Watchdog fires — thread has been running too long
                    with self._lock:
                        self._peers[agent_id]["watchdog_triggered"] = True
                    _update_registry(agent_id, "timeout",
                                     f"Watchdog: timed out after {_SWARM_AGENT_TIMEOUT}s")
                    self._handle_agent_failure(agent_id, info, "timeout", write_blackboard)

                elif not thread_alive:
                    # Thread finished — check if it failed (no GOAL_COMPLETE)
                    with _registry_lock:
                        reg = _registry.get(agent_id, {})
                    reg_status = reg.get("status", "")
                    if reg_status in ("failed", "timeout") and info["retry_key"] not in self._permanently_failed:
                        with self._lock:
                            self._peers[agent_id]["watchdog_triggered"] = True
                        self._handle_agent_failure(agent_id, info, reg_status, write_blackboard)

            if self._all_settled():
                self._synthesize()
                break

    def _handle_agent_failure(self, agent_id: str, info: dict,
                               reason: str, write_blackboard_fn) -> None:
        """Retry a failed/timed-out peer, or mark it permanently failed."""
        retry_key = info["retry_key"]
        retry_count = info["retry_count"] + 1

        # Write SYSTEM_RETRY signal to blackboard
        signal = (
            f"[SYSTEM_RETRY] Agent {info['agent'].agent_name} herstart vanwege {reason}. "
            f"Analyse van blokkade gestart. (Poging {retry_count}/{_SWARM_MAX_RETRIES})"
        )
        try:
            write_blackboard_fn(self.project_id, signal, label="SYSTEM_RETRY")
        except Exception:
            logger.exception("SwarmCoordinator: failed to write SYSTEM_RETRY to blackboard")

        if retry_count > _SWARM_MAX_RETRIES:
            logger.warning(
                "SwarmCoordinator: agent %s exhausted %d retries — marking permanently failed",
                info["agent"].agent_name, _SWARM_MAX_RETRIES,
            )
            with self._lock:
                self._permanently_failed.add(retry_key)
            return

        logger.info(
            "SwarmCoordinator: retrying %s (attempt %d/%d)",
            info["agent"].agent_name, retry_count, _SWARM_MAX_RETRIES,
        )
        retry_goal = f"{_RETRY_CONTEXT_PREFIX}\n\nOrigineel doel: {info['goal']}"
        self.add_peer(retry_goal, info["role"],
                      retry_key=retry_key, retry_count=retry_count)

    def _all_settled(self) -> bool:
        """True when every peer slot is either completed or permanently failed."""
        with self._lock:
            # Group peers by retry_key and find the latest attempt for each slot
            slots: dict[str, list] = {}
            for info in self._peers.values():
                slots.setdefault(info["retry_key"], []).append(info)

            for retry_key, attempts in slots.items():
                if retry_key in self._permanently_failed:
                    continue  # this slot is done (exhausted retries)
                # Find the most recent attempt (highest retry_count)
                latest = max(attempts, key=lambda x: x["retry_count"])
                if latest["thread"].is_alive():
                    return False  # still running
                # Thread is done — if it succeeded we're good; if failed, retry was queued
                with _registry_lock:
                    reg = _registry.get(latest["agent"].agent_name, {})
                if reg.get("status") not in ("completed",):
                    # Failed and not permanently failed — retry should have been queued,
                    # but if retry_key not in permanently_failed the retry peer should exist
                    if not any(
                        i["retry_key"] == retry_key and i["thread"].is_alive()
                        for i in self._peers.values()
                    ):
                        # No active retry thread and not permanently failed yet —
                        # the failure handler hasn't run yet; not settled
                        return False

            return True

    def _synthesize(self):
        with self._lock:
            failed_roles = list(self._permanently_failed)

        failure_note = ""
        if failed_roles:
            for role in failed_roles:
                failure_note += (
                    f" Agent {role} is na {_SWARM_MAX_RETRIES} pogingen gefaald; "
                    f"resultaten zijn gebaseerd op de resterende peers."
                )

        swarms_dir = os.path.join(PROJECT_LOGS_PATH, "swarms")
        os.makedirs(swarms_dir, exist_ok=True)
        synthesis_path = os.path.join(swarms_dir, f"{self.project_id}_synthesis.md")

        synthesis_goal = (
            f"Read the full blackboard for project '{self.project_id}' using "
            f"read_blackboard with project_id='{self.project_id}'. "
            f"IMPORTANT — Synthesis Validation Protocol:\n"
            f"1. Als het blackboard leeg is ('Blackboard is empty' of geen fragments), "
            f"rapporteer dit NIET als 'geen bevindingen'. Schrijf in plaats daarvan een "
            f"'PROTOCOL FAILURE'-rapport: vermeld dat peer-agents als voltooid zijn gemarkeerd "
            f"maar geen blackboard-entries hebben aangemaakt, wat duidt op een 'Silent Execution' "
            f"fout (vault-acties zonder blackboard-bewijs). Lijst alle peers op die GOAL_COMPLETE "
            f"hebben gemeld zonder begeleidende blackboard-entries.\n"
            f"2. Als het blackboard WEL entries heeft, controleer dan of alle vault-acties "
            f"(write_vault/move_file/delete_file) zijn gedocumenteerd in het formaat "
            f"'[ACTIE] | [BESTAND] | [RESULTAAT]'. Maak een exacte lijst van alle vault-wijzigingen.\n"
            f"3. Synthesiseer alle peer-bevindingen tot een coherente conclusie, los "
            f"tegenspraken op, identificeer emergente patronen, en schrijf de eindrapportage "
            f"met write_temp via file_name='swarm_synthesis/{self.project_id}_synthesis.md'. "
            f"De rapportage moet bevatten: (a) Vault-wijzigingenlijst, (b) Inhoudelijke synthese, "
            f"(c) Protocol-status (OK of FAILURE met uitleg)."
            + (f"\nBELANGRIJKE NOOT: {failure_note}" if failure_note else "")
            + "\nDeclare GOAL_COMPLETE na het schrijven van het syntheserapport."
        )
        agent = BackgroundAgent(
            goal=synthesis_goal,
            agent_name="SynthesisAgent",
            context_injector=self.context_injector,
            llm_trigger=self.llm_trigger,
        )
        result = agent.run()

        # Copy the synthesis from agent_temp to logs/swarms/ for permanent process record
        try:
            import shutil
            temp_synthesis = os.path.join(AGENT_TEMP_PATH, "swarm_synthesis",
                                          f"{self.project_id}_synthesis.md")
            if os.path.exists(temp_synthesis):
                shutil.copy2(temp_synthesis, synthesis_path)
                logger.info("SwarmCoordinator: synthesis copied to %s", synthesis_path)
        except Exception:
            logger.exception("SwarmCoordinator: failed to copy synthesis to logs/swarms/")


# ------------------------------------------------------------------
# Registry helpers
# ------------------------------------------------------------------

def _register(
    agent_id: str, name: str, goal: str, log_file: str,
    swarm_id: str | None = None, role: str | None = None,
) -> None:
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
            "swarm_id": swarm_id,
            "role": role,
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
    """Return a summary of all agents launched this session, grouped by swarm."""
    with _registry_lock:
        entries = list(_registry.values())

    if not entries:
        return "No agents have been launched this session."

    lines = ["**Background Agents — this session:**\n"]

    swarms: dict[str, list] = {}
    standalone: list = []
    for e in entries:
        sid = e.get("swarm_id")
        if sid:
            swarms.setdefault(sid, []).append(e)
        else:
            standalone.append(e)

    for e in standalone:
        lines.append(
            f"- `{e['id']}` — **{e['status']}** — {e['goal'][:80]}"
            + ("…" if len(e["goal"]) > 80 else "")
        )

    for sid, peers in swarms.items():
        lines.append(f"\n**Swarm `{sid}`:**")
        for e in peers:
            role = e.get("role", "")
            role_label = f" [{role}]" if role else ""
            lines.append(
                f"  - `{e['id']}`{role_label} — **{e['status']}** — {e['goal'][:70]}"
                + ("…" if len(e["goal"]) > 70 else "")
            )

    return "\n".join(lines)


def _get_swarm_coordinator(swarm_id: str) -> "SwarmCoordinator | None":
    return _swarm_registry.get(swarm_id)


def launch_swarm(
    goal: str,
    project_id: str,
    roles: list[str] | None = None,
    swarm_size: int = _AGENT_SWARM_SIZE,
    context_injector=None,
    llm_trigger=None,
) -> str:
    """
    Start a rhizomatic peer swarm on a shared Blackboard.
    Returns swarm_id immediately. SynthesisAgent spawns automatically when all peers complete.

    roles: list of role names for initial peers, e.g. ["Researcher", "Skeptic", "Synthesist"]
    Default: ["Researcher", "Skeptic", "Synthesist"]
    """
    if roles is None:
        roles = ["Researcher", "Skeptic", "Synthesist"]

    coordinator = SwarmCoordinator(project_id=project_id, swarm_size=swarm_size)
    coordinator.context_injector = context_injector
    coordinator.llm_trigger = llm_trigger

    _swarm_registry[coordinator._swarm_id] = coordinator

    initial_peers = [{"goal": f"[{role}] {goal}", "role": role} for role in roles]
    swarm_id = coordinator.launch(initial_peers)

    return (
        f"Swarm `{swarm_id}` started with {len(roles)} peer(s): {', '.join(roles)}.\n"
        f"Blackboard: `.agent_temp/blackboard/{project_id}/` (transient)\n"
        f"SynthesisAgent will spawn automatically when all peers complete.\n"
        f"Use `list_agents()` to check progress."
    )


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
