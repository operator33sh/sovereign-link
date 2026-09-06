"""Vault read/write/list/move/delete and temp-file helpers."""
import json
import logging
import os
import re
import subprocess
from datetime import datetime

from tools.moc import _auto_link_to_moc

logger = logging.getLogger(__name__)

VAULT_PATH = os.environ.get("VAULT_PATH", "/home/wouter/Documents/fractalisme-vault")
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_TEMP_PATH = os.path.join(PROJECT_ROOT, ".agent_temp")
PROJECT_LOGS_PATH = os.path.join(PROJECT_ROOT, "logs")
RUNTIME_PATH = os.path.join(PROJECT_ROOT, ".runtime")

_VAULT_LOG_KEYWORDS = ("execution", "synthesis")
_TS_PATTERN = re.compile(r'#\d{4}-\d{2}-\d{2}(?:\s+#\d{2})+\s*$', re.MULTILINE)
_TAG_PATTERN = re.compile(r'(?<!\d)#([A-Za-zÀ-ÿ]\w*)')

# Sovereign Truth Protocol — allowed evidentiality tags
STS_VALID_TAGS = frozenset({
    "#ev-direct", "#ev-derived", "#ev-reported", "#ev-assumed", "#ev-luna-hypo"
})


def _inject_ev_tag(content: str, ev_tag: str) -> str:
    """Insert or update the STS ev_tag field in YAML frontmatter."""
    tag_line = f"ev_tag: {ev_tag}"
    if content.startswith("---"):
        end = content.find("\n---", 3)
        if end != -1:
            fm_body = content[3:end]
            lines = [l for l in fm_body.splitlines() if not l.startswith("ev_tag:")]
            lines.append(tag_line)
            rest = content[end + 4:]
            return "---\n" + "\n".join(lines) + "\n---" + rest
    return f"---\n{tag_line}\n---\n{content}"


def _is_log_filename(file_name: str) -> bool:
    if "/" in file_name or os.sep in file_name:
        return False
    stem = os.path.splitext(os.path.basename(file_name))[0].lower()
    return any(kw in stem for kw in _VAULT_LOG_KEYWORDS)


def _require_vault_path(file_name: str) -> str:
    """Return resolved absolute path within vault, raising on traversal."""
    from exceptions import PathTraversalError
    path = os.path.join(VAULT_PATH, file_name)
    if not os.path.realpath(path).startswith(os.path.realpath(VAULT_PATH)):
        raise PathTraversalError(f"Path traversal not allowed: {file_name!r}")
    return path


def generate_time_tag() -> str:
    return datetime.now().strftime("#%Y-%m-%d #%H #%M")


def read_vault(file_name: str) -> str:
    try:
        path = _require_vault_path(file_name)
    except Exception:
        return "Error: path traversal not allowed"
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return f"Error: file '{file_name}' not found in vault"
    except Exception as e:
        return f"Error reading file: {e}"


def write_vault(file_name: str, content: str, timestamp: str | None = None, ev_tag: str | None = None) -> str:
    if _is_log_filename(file_name):
        safe_name = os.path.basename(file_name)
        redirect_path = os.path.join(PROJECT_LOGS_PATH, "redirected", safe_name)
        try:
            os.makedirs(os.path.dirname(redirect_path), exist_ok=True)
            with open(redirect_path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            return f"Error writing redirected log file: {e}"
        logger.info("write_vault: redirected '%s' → logs/redirected/%s", file_name, safe_name)
        return (
            f"[VAULT REDIRECT] '{file_name}' contains a process-log keyword and was "
            f"automatically redirected to 'logs/redirected/{safe_name}' (not written to vault)."
        )

    _RUNTIME_REDIRECTS = {".system/active_briefing.md"}
    if file_name in _RUNTIME_REDIRECTS:
        runtime_path = os.path.join(RUNTIME_PATH, os.path.basename(file_name))
        try:
            os.makedirs(RUNTIME_PATH, exist_ok=True)
            with open(runtime_path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            return f"Error writing runtime file: {e}"
        logger.info("write_vault: ACL redirected to runtime: %s", runtime_path)
        return "Written successfully to '.system/active_briefing.md'"

    try:
        path = _require_vault_path(file_name)
    except Exception:
        return "Error: path traversal not allowed"

    # Sovereign Truth Protocol: validate and inject evidentiality tag
    sts_warning = ""
    if ev_tag is None:
        ev_tag = "#ev-assumed"
        sts_warning = " [STS: no ev_tag provided — defaulted to #ev-assumed]"
    elif ev_tag not in STS_VALID_TAGS:
        return (
            f"Error: invalid ev_tag '{ev_tag}'. "
            f"Must be one of: {', '.join(sorted(STS_VALID_TAGS))}"
        )
    content = _inject_ev_tag(content, ev_tag)

    time_tags = datetime.now().strftime("#%Y-%m-%d #%H #%M")
    tagged_content = content.rstrip() + f"\n\n{time_tags}\n"
    tagged_content = _auto_link_to_moc(file_name, tagged_content)

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(tagged_content)
    except Exception as e:
        return f"Error writing file: {e}"

    try:
        from vector import index_file as _index_file
        _index_file(file_name, tagged_content, timestamp or datetime.now().isoformat())
    except Exception:
        logger.exception("write_vault: failed to index %s", file_name)

    try:
        from timeline import index_file as _timeline_index
        _timeline_index(file_name, tagged_content, timestamp or datetime.now().isoformat())
    except Exception:
        logger.exception("write_vault: timeline indexing failed for %s", file_name)

    return f"Written successfully to '{file_name}'{sts_warning}"


def sync_vault() -> str:
    try:
        result = subprocess.run(
            'git add . && git commit -m "Sovereign-Link Update" && git push',
            shell=True, cwd=VAULT_PATH, capture_output=True, text=True, timeout=30,
        )
        output = result.stdout + result.stderr
        if result.returncode != 0:
            return f"Sync failed:\n{output}"
        return f"Vault synced:\n{output}"
    except subprocess.TimeoutExpired:
        return "Error: git operation timed out"
    except Exception as e:
        return f"Error during sync: {e}"


def write_temp(file_name: str, content: str) -> str:
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
    import shutil
    target = os.path.join(AGENT_TEMP_PATH, agent_id)
    if not os.path.realpath(target).startswith(os.path.realpath(AGENT_TEMP_PATH)):
        return "Error: path traversal not allowed"
    if not os.path.exists(target):
        return f"No transient data found for agent '{agent_id}'."
    try:
        shutil.rmtree(target)
        return f"Transient data for '{agent_id}' purged."
    except Exception as e:
        return f"Error during cleanup: {e}"


def commit_to_vault(temp_file_name: str, vault_file_name: str, ev_tag: str | None = None) -> str:
    temp_path = os.path.join(AGENT_TEMP_PATH, temp_file_name)
    if not os.path.realpath(temp_path).startswith(os.path.realpath(AGENT_TEMP_PATH)):
        return "Error: path traversal not allowed"
    if not os.path.exists(temp_path):
        return f"Error: temp file '{temp_file_name}' not found in .agent_temp/"
    try:
        with open(temp_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return f"Error reading temp file: {e}"

    result = write_vault(vault_file_name, content, ev_tag=ev_tag)
    if result.startswith("Error"):
        return result

    try:
        os.remove(temp_path)
    except Exception:
        pass

    return f"Committed '{temp_file_name}' → vault '{vault_file_name}' (tagged & indexed)"


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


def build_vault_map(directory: str = "", max_preview_words: int = 50) -> str:
    scan_root = os.path.join(VAULT_PATH, directory) if directory else VAULT_PATH
    real_vault = os.path.realpath(VAULT_PATH)
    if not os.path.realpath(scan_root).startswith(real_vault):
        return "Error: path traversal not allowed"
    if not os.path.isdir(scan_root):
        return f"Error: '{directory}' is not a directory in the vault"

    entries = []
    for root, dirs, files in os.walk(scan_root):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.'))
        for fname in sorted(files):
            if not fname.endswith('.md'):
                continue
            full_path = os.path.join(root, fname)
            rel_path = os.path.relpath(full_path, VAULT_PATH)
            try:
                with open(full_path, 'r', encoding='utf-8') as f:
                    raw = f.read()
            except Exception:
                continue

            title = os.path.splitext(fname)[0]
            for line in raw.splitlines():
                if line.startswith('# '):
                    title = line[2:].strip()
                    break

            tags = list(dict.fromkeys('#' + m for m in _TAG_PATTERN.findall(raw)))[:20]

            body_start = 0
            if raw.startswith('---'):
                end = raw.find('\n---', 3)
                if end != -1:
                    body_start = end + 4

            body_words: list[str] = []
            for line in raw[body_start:].splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith('#'):
                    continue
                body_words.extend(stripped.split())
                if len(body_words) >= max_preview_words:
                    break

            entries.append({
                'file': rel_path,
                'title': title,
                'tags': tags,
                'preview': ' '.join(body_words[:max_preview_words]),
            })

    os.makedirs(AGENT_TEMP_PATH, exist_ok=True)
    map_path = os.path.join(AGENT_TEMP_PATH, 'vault_map.json')
    try:
        with open(map_path, 'w', encoding='utf-8') as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"Error writing vault_map.json: {e}"

    return f"Vault map gebouwd: {len(entries)} bestanden → {map_path}"


def inject_links(link_matrix_json: str) -> str:
    try:
        matrix = json.loads(link_matrix_json)
    except json.JSONDecodeError as e:
        return f"Error: ongeldige JSON: {e}"
    if not isinstance(matrix, list):
        return "Error: link_matrix_json moet een JSON-array zijn"

    real_vault = os.path.realpath(VAULT_PATH)
    injected = 0
    skipped = 0
    errors = 0
    modified: set[str] = set()

    for entry in matrix:
        source = (entry.get('source') or '').strip()
        target = (entry.get('target') or '').strip()
        context = (entry.get('context') or '').strip()

        if not source or not target:
            errors += 1
            continue

        src_path = os.path.join(VAULT_PATH, source)
        if not os.path.realpath(src_path).startswith(real_vault):
            errors += 1
            continue
        if not os.path.isfile(src_path):
            errors += 1
            continue

        try:
            with open(src_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception:
            errors += 1
            continue

        wikilink = f'[[{target}]]'
        if wikilink in content:
            skipped += 1
            continue

        inserted = False
        if context:
            section_pat = re.compile(
                r'^#{1,3}\s+' + re.escape(context) + r'\s*$',
                re.IGNORECASE | re.MULTILINE,
            )
            sm = section_pat.search(content)
            if sm:
                next_h = re.search(r'^#{1,3}\s', content[sm.end():], re.MULTILINE)
                if next_h:
                    cut = sm.end() + next_h.start()
                    content = content[:cut].rstrip() + f'\n{wikilink}\n\n' + content[cut:]
                else:
                    m = _TS_PATTERN.search(content)
                    if m:
                        content = content[:m.start()].rstrip() + f'\n{wikilink}\n\n' + content[m.start():]
                    else:
                        content = content.rstrip() + f'\n{wikilink}\n'
                inserted = True

        if not inserted:
            gm = re.search(r'^#{1,3}\s+Gerelateerd\s*$', content, re.IGNORECASE | re.MULTILINE)
            if gm:
                next_h = re.search(r'^#{1,3}\s', content[gm.end():], re.MULTILINE)
                if next_h:
                    cut = gm.end() + next_h.start()
                    content = content[:cut].rstrip() + f'\n{wikilink}\n\n' + content[cut:]
                else:
                    m = _TS_PATTERN.search(content)
                    if m:
                        content = content[:m.start()].rstrip() + f'\n{wikilink}\n\n' + content[m.start():]
                    else:
                        content = content.rstrip() + f'\n{wikilink}\n'
            else:
                block = f'## Gerelateerd\n{wikilink}'
                m = _TS_PATTERN.search(content)
                if m:
                    content = content[:m.start()].rstrip() + f'\n{block}\n\n' + content[m.start():]
                else:
                    content = content.rstrip() + f'\n{block}\n'

        try:
            with open(src_path, 'w', encoding='utf-8') as f:
                f.write(content)
            injected += 1
            modified.add(src_path)
        except Exception:
            errors += 1

    return (
        f"{injected} links geïnjecteerd in {len(modified)} bestanden "
        f"| {skipped} duplicaten geskipt | {errors} fouten"
    )


DEFINITIONS = [
    {"type": "function", "function": {"name": "read_vault", "description": "Read a file from the fractalisme vault.", "parameters": {"type": "object", "properties": {"file_name": {"type": "string", "description": "The file name (or relative path) to read from the vault."}}, "required": ["file_name"]}}},
    {"type": "function", "function": {"name": "write_vault", "description": "Write or overwrite a file in the fractalisme vault. REQUIRED: provide ev_tag (Sovereign Truth Protocol). Allowed values: #ev-direct (firsthand observation), #ev-derived (logical synthesis), #ev-reported (external source), #ev-assumed (hypothesis), #ev-luna-hypo (Luna intuition — must be confirmed before promotion to #ev-direct). Omitting ev_tag defaults to #ev-assumed with a warning.", "parameters": {"type": "object", "properties": {"file_name": {"type": "string"}, "content": {"type": "string"}, "ev_tag": {"type": "string", "enum": ["#ev-direct", "#ev-derived", "#ev-reported", "#ev-assumed", "#ev-luna-hypo"], "description": "STS evidentiality tag — provenance of the information being written."}}, "required": ["file_name", "content"]}}},
    {"type": "function", "function": {"name": "sync_vault", "description": "Commit and push all vault changes to git.", "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {"name": "write_temp", "description": "Write a transient/working-memory file to .agent_temp/. NEVER indexed.", "parameters": {"type": "object", "properties": {"file_name": {"type": "string"}, "content": {"type": "string"}}, "required": ["file_name", "content"]}}},
    {"type": "function", "function": {"name": "read_temp", "description": "Read a transient file from .agent_temp/ (working memory).", "parameters": {"type": "object", "properties": {"file_name": {"type": "string"}}, "required": ["file_name"]}}},
    {"type": "function", "function": {"name": "cleanup_transient_data", "description": "Purge all transient working-memory files for a given agent_id from .agent_temp/.", "parameters": {"type": "object", "properties": {"agent_id": {"type": "string"}}, "required": ["agent_id"]}}},
    {"type": "function", "function": {"name": "commit_to_vault", "description": "Promote a finalized draft from .agent_temp/ to the Sovereign Vault. Provide ev_tag (STS) to stamp provenance on commit.", "parameters": {"type": "object", "properties": {"temp_file_name": {"type": "string"}, "vault_file_name": {"type": "string"}, "ev_tag": {"type": "string", "enum": ["#ev-direct", "#ev-derived", "#ev-reported", "#ev-assumed", "#ev-luna-hypo"], "description": "STS evidentiality tag — provenance of the information being committed."}}, "required": ["temp_file_name", "vault_file_name"]}}},
    {"type": "function", "function": {"name": "list_files", "description": "Recursively list all files in a vault directory.", "parameters": {"type": "object", "properties": {"directory": {"type": "string"}}, "required": []}}},
    {"type": "function", "function": {"name": "move_file", "description": "Move or rename a file within the vault.", "parameters": {"type": "object", "properties": {"source_path": {"type": "string"}, "destination_path": {"type": "string"}}, "required": ["source_path", "destination_path"]}}},
    {"type": "function", "function": {"name": "delete_file", "description": "Permanently delete a file from the vault.", "parameters": {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}}},
    {"type": "function", "function": {"name": "build_vault_map", "description": "Scan de vault en schrijf een compacte metadata-map naar .agent_temp/vault_map.json.", "parameters": {"type": "object", "properties": {"directory": {"type": "string"}, "max_preview_words": {"type": "integer"}}, "required": []}}},
    {"type": "function", "function": {"name": "inject_links", "description": "Injecteer [[wikilinks]] mechanisch in vault-bestanden op basis van een Link Matrix.", "parameters": {"type": "object", "properties": {"link_matrix_json": {"type": "string"}}, "required": ["link_matrix_json"]}}},
]

HANDLERS = {
    "read_vault": lambda args: read_vault(args["file_name"]),
    "write_vault": lambda args: write_vault(args["file_name"], args["content"], ev_tag=args.get("ev_tag")),
    "sync_vault": lambda args: sync_vault(),
    "write_temp": lambda args: write_temp(args["file_name"], args["content"]),
    "read_temp": lambda args: read_temp(args["file_name"]),
    "cleanup_transient_data": lambda args: cleanup_transient_data(args["agent_id"]),
    "commit_to_vault": lambda args: commit_to_vault(args["temp_file_name"], args["vault_file_name"], ev_tag=args.get("ev_tag")),
    "list_files": lambda args: list_files(args.get("directory", "")),
    "move_file": lambda args: move_file(args["source_path"], args["destination_path"]),
    "delete_file": lambda args: delete_file(args["file_path"]),
    "build_vault_map": lambda args: build_vault_map(args.get("directory", ""), args.get("max_preview_words", 50)),
    "inject_links": lambda args: inject_links(args["link_matrix_json"]),
}
