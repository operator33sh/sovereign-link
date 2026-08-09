# Sovereign-Link

A private Telegram bot that gives you conversational access to your local [Obsidian](https://obsidian.md/) vault (or any folder of Markdown files). Runs entirely on your own machine — no data leaves your infrastructure.

## Features

- **Chat with your vault** — ask questions, get summaries, or search by meaning across all your notes
- **Semantic search (RAG)** — `nomic-embed-text` embeddings + ChromaDB find relevant fragments by context, not just filenames
- **Read & write notes** — the AI can read existing vault files or create new ones on your behalf
- **Sovereign Memory Engine** — automatically extracts High-Value Insights (HVIs) from conversations and saves them as structured `SovereignLog` files in the vault; relevant past memories are injected into the system prompt at session start
- **Memory continuity** — memory is extracted automatically every 20 messages, on `/clear`, and on bot shutdown so nothing is ever lost
- **Vault watcher** — a background filesystem observer auto-indexes any `.md` file written to the vault outside of the bot (e.g. from Obsidian directly)
- **Voice transcription** — send voice messages or audio files; transcribed locally using [faster-whisper](https://github.com/SYSTRAN/faster-whisper) before being sent to the LLM
- **Image understanding** — send photos with an optional caption; the LLM analyses them inline
- **Website analysis** — share a URL and the bot fetches and extracts the page content (via trafilatura) for summarisation or saving
- **Vault snapshots** — `/vault` summarises the last 5 exchanges and saves a structured note with wikilinks to related files
- **Git sync** — all vault writes are committed and pushed automatically
- **Fully local & private** — LLM, embeddings, transcription, and vector DB all run on your own hardware; optionally point the main LLM at a cloud provider via `OLLAMA_BASE_URL`/`OLLAMA_API_KEY`

## Architecture

```
Telegram ──► bot.py ──► llm.py ──► Ollama-compatible API (LLM)
                   │         └──► tools.py ──► read_vault / write_vault / sync_vault
                   │                      └──► analyze_website (trafilatura)
                   │                      └──► vector.py (semantic search, ChromaDB)
                   │
                   └──► memory_manager.py ──► Sovereign Memory Engine (HVI extraction)
                             └──► vector.py ──► ChromaDB (cosine search)
```

## Requirements

- Python 3.11+
- [Ollama](https://ollama.com/) running locally (or any OpenAI-compatible API endpoint)
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- A vault directory of Markdown files (e.g. an Obsidian vault with git initialised)

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/operator33sh/sovereign-link.git
cd sovereign-link
```

### 2. Create a virtual environment and install dependencies

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 3. Pull the required Ollama models

```bash
ollama pull llama3.1          # or whichever chat model you prefer
ollama pull nomic-embed-text  # for semantic search embeddings
```

### 4. Configure environment variables

Create a `.env` file in the project root:

```env
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
ALLOWED_USER_ID=your_telegram_user_id        # only this user can interact with the bot

VAULT_PATH=/path/to/your/vault               # local folder of .md files

# Main LLM (can be Ollama or any OpenAI-compatible endpoint)
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_API_KEY=                              # leave empty for local Ollama
OLLAMA_MODEL=llama3.1

# Embeddings (always local Ollama)
EMBED_BASE_URL=http://localhost:11434
EMBED_MODEL=nomic-embed-text
CHROMA_PATH=~/.sovereign-link/chroma         # where ChromaDB stores its index

# Vision model — optional, defaults to OLLAMA_MODEL
# VISION_MODEL=llava                         # any vision-capable model (llava, qwen3-vl, etc.)

# Voice transcription — optional, defaults shown
# WHISPER_MODEL=small                        # tiny / base / small / medium / large

# Custom system prompt — optional
# SYSTEM_PROMPT=You are a personal assistant...
```

To find your Telegram user ID, message [@userinfobot](https://t.me/userinfobot).

### 5. Index your vault (first time only)

**ChromaDB semantic index:**
```bash
.venv/bin/python ingest.py
```

Re-run this script if you add many files outside of the bot. Files written via the bot are indexed automatically.

### 6. Run the bot

```bash
.venv/bin/python main.py
```

## Running as a systemd service

A service unit file is included. To install it:

```bash
sudo cp sovereign-link.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable sovereign-link
sudo systemctl start sovereign-link
```

Check logs with:

```bash
journalctl -u sovereign-link -f
```

## Bot commands

| Command | Description |
|---------|-------------|
| `/start` | Check if the bot is online |
| `/clear` | Clear the current session context and save a memory log |
| `/vault` | Summarise the last 5 exchanges as a structured vault note and push to git |
| `/memory` | Manually trigger the Sovereign Memory Engine on the current conversation |
| `/whisper` | Generate a tweet-length insight from a random vault fragment |

Any other text message is handled by the LLM with access to all tools. Voice messages and photos are also supported directly.

## Sovereign Memory Engine

The memory engine runs as part of the bot (no separate process needed). It:

1. Scans the conversation for High-Value Insights — psychological breakthroughs, redefined values, recurring patterns, or architectural decisions
2. Searches the vault for related prior memory logs to build associative `[[wikilinks]]`
3. Writes a structured `SovereignLog` file to `memory/YYYY-MM-DD_TOPIC_SovereignLog.md`
4. Commits and pushes the result to git

Memory is triggered automatically every 20 messages, on `/clear`, and on bot shutdown. At the start of each new session the top 3 most relevant past memory logs are injected into the system prompt.

## Project structure

```
sovereign-link/
├── main.py               # Entry point
├── bot.py                # Telegram handlers and command routing
├── llm.py                # Ollama LLM client, tool call loop, audio transcription
├── context.py            # In-memory conversation history
├── tools.py              # Vault tools: read, write, sync, semantic search, web fetch
├── vector.py             # ChromaDB + Ollama embedding logic + filesystem watcher
├── memory_manager.py     # Sovereign Memory Engine (Extract→Synthesize→Store→Sync)
├── ingest.py             # One-shot ChromaDB vault indexer
├── requirements.txt
└── sovereign-link.service  # systemd unit
```

## License

MIT
