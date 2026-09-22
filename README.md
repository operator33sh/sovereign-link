# 🪬 Sovereign Link

> *Your own AI. On your own machine. With your own memory.*

---

## What is this?

Sovereign Link is a personal AI assistant that runs on your hardware, communicates via Telegram, and remembers everything in a local knowledge vault — your words, your insights, your patterns.

No cloud. No subscription. No company reading along.

**What it does for you:**

| You say or send... | Sovereign Link does... |
|---|---|
| A question | Searches your own notes for context and gives a grounded answer |
| A voice message | Transcribes it, processes it, remembers what mattered |
| A photo | Analyses the content and connects it to what you already know |
| A URL | Reads the page and extracts the essence |
| An emotional thought | Stores it as an insight — and surfaces it when it becomes relevant |

Everything you share is stored as readable Markdown files in a local vault. You own your own memory.

---

## 🪬 The Philosophy

Most people use AI as an emotional airbag.

They vent into it. They let it absorb the shock of their own patterns. And then they move on — unchanged. The conversation disappears. The insight evaporates. The cycle continues.

**This is not that.**

Sovereign Link was built on a different premise: that the most dangerous thing you can do with AI is make it comfortable. Comfort is friction removal. And friction, in the right places, is what forces the brain to restructure.

The system exists to do one thing: **give the observer a structural map of their own destructive patterns** — not to validate them, but to make them visible, nameable, and therefore interruptible.

When memory is yours — stored in *your* vault, on *your* machine, under *your* version control — the infrastructure of self-knowledge belongs to you. Not to a corporate server. Not to a session that expires.

> The observer who maps their own patterns owns the only leverage point that matters: the moment before the next repetition.

The philosophical framework lives at **[Fractalisme.nl](https://fractalisme.nl)**.

---

## What it protects

Sovereign Link has a built-in cognitive brake — not marketing, but technical infrastructure.

If you work too long, too intensively, or the system detects heavy psychological topics, a **Stop Order** activates automatically. Analyses are blocked. Complex tools are locked. Luna asks if you're doing okay.

Only when you confirm you've rested does it open back up.

The thresholds are configurable. They're stricter when you're in a stress phase. And if you signal that you're in flow, they're temporarily raised — so the brake doesn't fire too early.

**Your rest takes priority over every task.**

---

## Navigation

- [For users — what can you do with it?](#what-is-this)
- [Setup](#setup)
- [Bot commands](#bot-commands)
- [Technical architecture](#technical-architecture)
- [Project structure](#project-structure)

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/operator33sh/sovereign-link.git
cd sovereign-link
```

### 2. Install dependencies

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 3. Pull the required models

```bash
ollama pull llama3.1          # or whichever chat model you prefer
ollama pull nomic-embed-text  # for semantic search embeddings
```

### 4. Create a `.env` file

```env
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
ALLOWED_USER_ID=your_telegram_user_id        # only this user can interact with the bot

VAULT_PATH=/path/to/your/vault               # local folder of .md files

# Main LLM (Ollama local or any OpenAI-compatible endpoint)
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_API_KEY=                              # leave empty for local Ollama
OLLAMA_MODEL=llama3.1

# Embeddings (always local Ollama)
EMBED_BASE_URL=http://localhost:11434
EMBED_MODEL=nomic-embed-text

# Proton Mail Bridge (optional — fill in if your Bridge is active)
PROTON_IMAP_USER=
PROTON_IMAP_PASS=
PROTON_IMAP_HOST=127.0.0.1
PROTON_IMAP_PORT=1143
```

Find your Telegram user ID via [@userinfobot](https://t.me/userinfobot).  
Create your Telegram bot token via [@BotFather](https://t.me/BotFather).

### 5. Index your vault (first time only)

```bash
.venv/bin/python ingest.py --rescan
```

### 6. Start the bot

```bash
.venv/bin/python main.py
```

---

## Bot commands

| Command | What it does |
|---------|-------------|
| `/start` | Check if the bot is online |
| `/clear` | Clear the current session and save a memory log |
| `/vault` | Save the last 5 exchanges as a structured vault note |
| `/memory` | Manually trigger memory extraction on the current conversation |
| `/whisper` | Generate a tweet-length insight from a random vault fragment |

Any other message — text, voice, photo, or link — is processed directly by the AI.

---

## Technical Architecture

```
Telegram ──► bot.py ──► llm.py ──► Ollama-compatible API (LLM)
                   │         └──► tools/ ──► read_vault / write_vault / sync_vault
                   │                    └──► analyze_website (trafilatura)
                   │                    └──► search_vault_semantic ──► vector.py (ChromaDB)
                   │                    └──► search_timeline ──► timeline.py (SQLite + FTS5)
                   │                    └──► check_proton_mail / read_proton_email (IMAP)
                   │
                   └──► memory_manager.py ──► Sovereign Memory Engine (HVI extraction)
                   │         └──► vector.py ──► ChromaDB (cosine / semantic search)
                   │         └──► timeline.py ──► SQLite (date / keyword search)
                   │         └──► Obsidian Vault ──► source of proof (plain Markdown)
                   │
                   └──► cognitive_brake.py ──► Stop Order Monitor
```

### Layer 1 — Interface

| Component | Role |
|-----------|------|
| 📱 **Telegram** | Primary command-and-control surface. All interaction, voice, images, and commands flow through here. |
| 🧠 **LLM (AI Model)** | Central processing unit. Reasoning, planning, tool orchestration, and language understanding. |

### Layer 2 — Memory & Persistence

| Component | Role |
|-----------|------|
| 📂 **Obsidian Vault** | The human-readable long-term memory. Everything lives here as navigable, linkable Markdown files. Nothing is ephemeral — every session, insight, and memory log is auditable. |
| **Git / GitHub** | Version control, synchronisation, and recovery. Every vault write is automatically committed and pushed. |

### Layer 3 — Intelligence & Retrieval

| Component | Role |
|-----------|------|
| **RAG** | Grounds the AI in the vault's truth rather than generic training data. |
| 🗄️ **ChromaDB (Semantic Search)** | Searches by meaning and context — not just filename or keyword. Local `nomic-embed-text` embeddings. |
| 🗃️ **SQLite Timeline** | Parallel index for reliable date-based queries. FTS5 full-text search combined with date filters. |
| ⚙️ **Sovereign Memory Engine** | Autonomous background agents that scan conversations for High-Value Insights, write structured `SovereignLog` files, and commit everything to git. |
| **Active Context Layer (ACL)** | A dynamic briefing file (`.system/active_briefing.md`) that steers AI behaviour based on the user's current operational state. |
| 🛑 **Cognitive Brake** | Background monitor (`cognitive_brake.py`) that tracks session duration, tool-call intensity, heavy psychological topics, and fatigue signals. Activates a **Stop Order** that blocks all analytical tools until a confirmed rest period has elapsed. Thresholds are phase-aware and configurable via `.system/cognitive_limits.json`. **Dynamic Override** raises thresholds +50% on positive flow signals. |
| 🧬 **SOUL.md** | Luna's cognitive architecture kernel — injected into every system prompt. Defines identity, behavioural mechanisms, and the Stop Order protocol. Versioned in an Evolution Log within the file itself. |

### Layer 4 — Framework

| Component | Role |
|-----------|------|
| **The Harness** | Psychological guardrails and operational constraints embedded in the system prompt. Defines the AI's behavioural contract. |
| **Fractalism** | The philosophical framework governing how data is organised and interconnected. See [Fractalisme.nl](https://fractalisme.nl). |

---

## Project Structure

```
sovereign-link/
├── main.py                   # Entry point
├── bot.py                    # Telegram handlers and command routing
├── llm.py                    # Ollama LLM client, tool call loop, audio transcription
├── cognitive_brake.py        # Cognitive Brake: session monitor, Stop Order, Dynamic Override
├── context.py                # In-memory conversation history
├── memory_manager.py         # Sovereign Memory Engine (Extract→Synthesize→Store→Sync)
├── vector.py                 # ChromaDB + Ollama embedding + filesystem watcher
├── timeline.py               # SQLite timeline index: date/FTS5 search across vault
├── ingest.py                 # Vault indexer: --rescan (full), --backfill (fix dates)
├── SOUL.md                   # Luna's cognitive architecture kernel
│
├── tools/                    # Tool registry (one submodule per domain)
│   ├── vault.py              # read_vault, write_vault, sync_vault
│   ├── search.py             # search_vault_semantic (ChromaDB), search_timeline (SQLite)
│   ├── http.py               # analyze_website (trafilatura)
│   ├── browser_tools.py      # Playwright browser automation
│   ├── agent_tools.py        # Agent blackboard / subagent coordination
│   ├── scheduler_tools.py    # Scheduled reminders and recurring tasks
│   ├── notification_tools.py # Push notifications + clear_stop_order / set_pause_duration
│   ├── session_status_tools.py # get_session_status: live Cognitive Brake dashboard
│   ├── soul_tools.py         # write_soul_md: update Luna's cognitive architecture
│   ├── proton_mail.py        # check_proton_mail / read_proton_email (IMAP via Bridge)
│   ├── gmail.py              # Gmail integration (OAuth)
│   ├── calendar.py           # Google Calendar integration
│   ├── twitter.py            # X/Twitter integration
│   ├── automation_tools.py   # Vault-based automation rules
│   ├── personality_tools.py  # Luna persona management
│   ├── mental_state_tools.py # get_mental_state: DriftGovernor analysis
│   └── moltbook.py           # Moltbook integration
│
├── agent.py                  # Subagent orchestration
├── automations.py            # Automation rule engine
├── hotline.py                # Voice-to-voice hotline (Twilio → Deepgram → LLM → ElevenLabs)
├── notifications.py          # Push notification dispatcher
├── proactive.py              # Proactive message engine
├── scheduler.py              # Task scheduler
│
├── requirements.txt
├── sovereign-link.service    # systemd unit (bot)
└── hotline.service           # systemd unit (voice hotline)
```

---

## Requirements

- Python 3.11+
- [Ollama](https://ollama.com/) running locally (or any OpenAI-compatible API endpoint)
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- A directory of Markdown files (e.g. an Obsidian vault with git initialised)

---

## Running as a systemd service

```bash
sudo cp sovereign-link.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable sovereign-link
sudo systemctl start sovereign-link
```

View logs:

```bash
journalctl -u sovereign-link -f
```

---

## License

MIT
