import os
import random
import subprocess
from urllib.parse import urlparse

import httpx
import trafilatura

from vector import index_file, search_vault_semantic as _search_vault_semantic

VAULT_PATH = os.environ.get("VAULT_PATH", "/home/wouter/Documents/fractalisme-vault")


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


def write_vault(file_name: str, content: str) -> str:
    path = os.path.join(VAULT_PATH, file_name)
    if not os.path.realpath(path).startswith(os.path.realpath(VAULT_PATH)):
        return "Error: path traversal not allowed"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        index_file(file_name, content)
        return f"Written successfully to '{file_name}'"
    except Exception as e:
        return f"Error writing file: {e}"


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
            "name": "search_vault_semantic",
            "description": (
                "Semantic search across the entire fractalisme vault using vector embeddings. "
                "Use this to find relevant notes by meaning and context rather than exact filenames. "
                "Returns the top 5 most relevant text fragments."
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
}
