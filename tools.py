import os
import subprocess

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
}
