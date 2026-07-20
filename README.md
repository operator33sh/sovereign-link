# Sovereign-Link

A private Telegram bot that gives you conversational access to your local [Obsidian](https://obsidian.md/) vault (or any folder of Markdown files). It runs entirely on your own machine using a local LLM via [Ollama](https://ollama.com/), so no data leaves your infrastructure.

## Features

- **Chat with your vault** — ask questions, get summaries, or search by meaning across all your notes
- **Semantic search (RAG)** — uses `nomic-embed-text` embeddings + ChromaDB to find relevant fragments by context, not just filenames
- **Read & write notes** — the AI can read existing vault files or create new ones on your behalf
- **Vault snapshots** — `/vault` command summarizes the last 5 exchanges and saves a structured note, then pushes it to git
- **Git sync** — all writes are committed and pushed automatically
- **Fully local & private** — LLM runs via Ollama, embeddings run via Ollama, vector DB runs locally with ChromaDB

## Architecture

```
Telegram ──► bot.py ──► llm.py ──► Ollama (local LLM)
                   │
                   └──► tools.py ──► read_vault / write_vault / sync_vault
                              └──► vector.py ──► Ollama (nomic-embed-text)
                                           └──► ChromaDB (local)
```

## Requirements

- Python 3.11+
- [Ollama](https://ollama.com/) running locally
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- A vault directory of Markdown files (e.g. an Obsidian vault with git initialized)

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
ollama pull llama3.1          # or whichever model you prefer
ollama pull nomic-embed-text  # for semantic search embeddings
```

### 4. Configure environment variables

Create a `.env` file in the project root:

```env
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
ALLOWED_USER_ID=your_telegram_user_id        # only this user can interact with the bot
VAULT_PATH=/path/to/your/vault               # local folder of .md files
OLLAMA_BASE_URL=http://localhost:11434        # Ollama API base URL
OLLAMA_MODEL=llama3.1                        # LLM model to use
EMBED_MODEL=nomic-embed-text                 # embedding model for semantic search
CHROMA_PATH=~/.sovereign-link/chroma         # where ChromaDB stores its index
SYSTEM_PROMPT=You are a personal assistant...  # optional: customize the system prompt
```

To find your Telegram user ID, message [@userinfobot](https://t.me/userinfobot).

### 5. Index your vault (first time only)

```bash
.venv/bin/python ingest.py
```

This crawls all `.md` files in your vault, chunks them, and stores embeddings in ChromaDB. Re-run this if you add files outside of the bot (files written via the bot are indexed automatically).

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
| `/clear` | Clear the current conversation context |
| `/vault` | Summarize the last 5 exchanges as a structured vault note and push to git |

Any other message is treated as a chat message to the LLM. The AI can decide to use tools (read files, write files, semantic search) based on your request.

## Project structure

```
sovereign-link/
├── main.py          # Entry point
├── bot.py           # Telegram handlers
├── llm.py           # Ollama LLM client + tool call loop
├── context.py       # In-memory conversation history
├── tools.py         # Vault tools (read, write, sync, semantic search)
├── vector.py        # ChromaDB + Ollama embedding logic
├── ingest.py        # One-shot vault indexing script
├── requirements.txt
└── sovereign-link.service  # systemd unit
```

## License

MIT
