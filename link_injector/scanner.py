"""
link_injector.scanner — vault scannen en hulpfuncties voor display.
"""

import os
import re
import shutil
import sys
import time
from datetime import timedelta

# ── .env laden ────────────────────────────────────────────────────────────────

def _load_dotenv() -> None:
    """Laad .env vanuit de scriptdirectory of de werkdirectory."""
    for candidate in (
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"),
        os.path.join(os.getcwd(), ".env"),
    ):
        if os.path.isfile(candidate):
            with open(candidate) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    key = key.strip()
                    val = val.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = val
            break

# ── Configuratie ──────────────────────────────────────────────────────────────

VAULT_PATH      = os.environ.get("VAULT_PATH", "/home/wouter/Documents/fractalisme-vault")
AGENT_TEMP_PATH = os.path.join(VAULT_PATH, ".agent_temp")
MOC_DIR         = os.environ.get("MOC_DIR", "MOCs")
BASE_URL        = os.environ.get("OLLAMA_BASE_URL", "https://ollama.com")
API_KEY         = os.environ.get("OLLAMA_API_KEY", "")
MODEL           = os.environ.get("OLLAMA_MODEL", "llama3.1")
LLM_TIMEOUT     = float(os.environ.get("AGENT_LLM_TIMEOUT", 300.0))

_TS_PATTERN  = re.compile(r'#\d{4}-\d{2}-\d{2}(?:\s+#\d{2})+\s*$', re.MULTILINE)
_TAG_PATTERN = re.compile(r'(?<!\d)#([A-Za-zÀ-ÿ]\w*)')

# ── Progress display ──────────────────────────────────────────────────────────

TERM_WIDTH = min(shutil.get_terminal_size((80, 20)).columns, 120)
BAR_WIDTH  = 35


def _bar(done: int, total: int) -> str:
    pct    = done / total if total else 1.0
    filled = int(BAR_WIDTH * pct)
    return "█" * filled + "░" * (BAR_WIDTH - filled)


class Progress:
    """Simple single-line progress bar with ETA."""

    def __init__(self, total: int, label: str):
        self.total        = total
        self.done         = 0
        self.label        = label
        self._start       = time.monotonic()
        self._last_status = ""

    def _render(self, status: str) -> str:
        elapsed   = time.monotonic() - self._start
        rate      = self.done / elapsed if elapsed > 0 else 0
        remaining = (self.total - self.done) / rate if rate > 0 and self.done > 0 else None
        eta       = f"ETA {timedelta(seconds=int(remaining))}" if remaining is not None else "ETA --:--"
        bar       = _bar(self.done, self.total)
        pct       = f"{100*self.done/self.total:.0f}%" if self.total else "100%"
        line      = f"  {self.label} [{bar}] {self.done}/{self.total} {pct}  {eta}"
        if status:
            line += f"  {status}"
        return line[:TERM_WIDTH]

    def update(self, n: int = 1, status: str = ""):
        self.done += n
        self._last_status = status
        sys.stdout.write("\r" + self._render(status).ljust(TERM_WIDTH))
        sys.stdout.flush()

    def peek(self, status: str):
        sys.stdout.write("\r" + self._render(status).ljust(TERM_WIDTH))
        sys.stdout.flush()

    def finish(self, msg: str = ""):
        elapsed = time.monotonic() - self._start
        sys.stdout.write(
            f"\r  ✓ {self.label} — klaar in {elapsed:.1f}s  {msg}\n".ljust(TERM_WIDTH) + "\n"
        )
        sys.stdout.flush()


def section(title: str):
    print(f"\n{'─'*TERM_WIDTH}")
    print(f"  {title}")
    print(f"{'─'*TERM_WIDTH}")


def info(msg: str):
    print(f"  {msg}")


def warn(msg: str):
    print(f"  ⚠  {msg}")


def error(msg: str):
    print(f"  ✗  {msg}", file=sys.stderr)


# ── Stap 1: Vault scannen ─────────────────────────────────────────────────────

def build_vault_map(
    directory: str = "",
    max_preview_words: int = 50,
    uncovered_only: bool = False,
) -> list[dict]:
    scan_root  = os.path.join(VAULT_PATH, directory) if directory else VAULT_PATH
    real_vault = os.path.realpath(VAULT_PATH)

    if not os.path.realpath(scan_root).startswith(real_vault):
        raise ValueError(f"Pad traversal niet toegestaan: {directory}")
    if not os.path.isdir(scan_root):
        raise FileNotFoundError(f"Map niet gevonden in vault: {directory!r}")

    all_md: list[str] = []
    for root, dirs, files in os.walk(scan_root):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.'))
        for fname in files:
            if fname.endswith('.md'):
                all_md.append(os.path.join(root, fname))

    if uncovered_only:
        _wikilink_re = re.compile(r'\[\[')
        filtered = []
        for p in all_md:
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    if not _wikilink_re.search(f.read()):
                        filtered.append(p)
            except Exception:
                pass
        info(f"--uncovered-only: {len(filtered)} van {len(all_md)} bestanden zonder links")
        all_md = filtered

    prog    = Progress(len(all_md), "Vault scannen")
    entries: list[dict] = []

    for full_path in sorted(all_md):
        rel_path = os.path.relpath(full_path, VAULT_PATH)
        prog.peek(rel_path[-50:])
        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                raw = f.read()
        except Exception:
            prog.update(1, "leesfout geskipt")
            continue

        title = os.path.splitext(os.path.basename(full_path))[0]
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
            if max_preview_words and len(body_words) >= max_preview_words:
                break

        entries.append({
            'file':    rel_path,
            'title':   title,
            'tags':    tags,
            'preview': ' '.join(body_words[:max_preview_words] if max_preview_words else body_words),
        })
        prog.update(1)

    prog.finish(f"— {len(entries)} bestanden")
    return entries


# ── Links strippen ────────────────────────────────────────────────────────────

_WIKILINK_RE         = re.compile(r'\[\[[^\]]*\]\]')
_EMPTY_GERELATEERD_RE = re.compile(
    r'^#{1,3}\s+Gerelateerd\s*\n(?:\s*\n)*(?=#{1,3}|\Z)',
    re.MULTILINE,
)


def _strip_wikilinks(content: str) -> str:
    stripped = _WIKILINK_RE.sub('', content)
    stripped = re.sub(r'^- \s*$', '', stripped, flags=re.MULTILINE)
    stripped = _EMPTY_GERELATEERD_RE.sub('', stripped)
    stripped = re.sub(r'\n{3,}', '\n\n', stripped)
    return stripped


def strip_all_links(dry_run: bool = False, directory: str = "") -> int:
    real_vault = os.path.realpath(VAULT_PATH)
    scan_root  = os.path.join(VAULT_PATH, directory) if directory else VAULT_PATH

    all_md: list[str] = []
    for root, dirs, files in os.walk(scan_root):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.'))
        for fname in files:
            if fname.endswith('.md'):
                all_md.append(os.path.join(root, fname))

    prog     = Progress(len(all_md), "Links strippen")
    modified = 0

    for full_path in sorted(all_md):
        if not os.path.realpath(full_path).startswith(real_vault):
            prog.update(1)
            continue
        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                content = f.read()
            new_content = _strip_wikilinks(content)
            if new_content != content:
                if not dry_run:
                    with open(full_path, 'w', encoding='utf-8') as f:
                        f.write(new_content)
                modified += 1
            prog.update(1)
        except Exception as e:
            prog.update(1, f"fout: {e}")

    prog.finish(f"— {modified} bestanden gewijzigd" + (" [DRY-RUN]" if dry_run else ""))
    return modified
