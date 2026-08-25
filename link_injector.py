#!/usr/bin/env python3
"""
Mechanical Link Injector — standalone CLI
=========================================
Pipeline:
  1. Scan vault → vault_map.json  (razendsnel, pure Python)
  2. LLM-analyse in chunks        (één LLM-call per chunk, retry-logica ingebouwd)
  3. Mechanische injectie         (directe filesystem-writes, geen LLM per bestand)

Gebruik:
  python link_injector.py [opties]
  python link_injector.py --dry-run
  python link_injector.py --directory 10_Kern --chunk-size 30
  python link_injector.py --from-matrix link_matrix.json  # sla stap 1+2 over

Omgevingsvariabelen (zelfde als agent):
  VAULT_PATH, OLLAMA_BASE_URL, OLLAMA_API_KEY, OLLAMA_MODEL
"""

import argparse
import json
import os
import re
import shutil
import sys
import time
from datetime import timedelta

import httpx

# ── Configuratie ──────────────────────────────────────────────────────────────

VAULT_PATH = os.environ.get("VAULT_PATH", "/home/wouter/Documents/fractalisme-vault")
AGENT_TEMP_PATH = os.path.join(VAULT_PATH, ".agent_temp")
BASE_URL = os.environ.get("OLLAMA_BASE_URL", "https://ollama.com")
API_KEY = os.environ.get("OLLAMA_API_KEY", "")
MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1")
LLM_TIMEOUT = float(os.environ.get("AGENT_LLM_TIMEOUT", 300.0))

_TS_PATTERN = re.compile(r'#\d{4}-\d{2}-\d{2}(?:\s+#\d{2})+\s*$', re.MULTILINE)
_TAG_PATTERN = re.compile(r'(?<!\d)#([A-Za-zÀ-ÿ]\w*)')

LLM_SYSTEM_PROMPT = """\
Je bent een vault-analist voor een Obsidian kennisbank in het Nederlands.
Je krijgt een batch vault-bestanden (JSON) met bestandsnaam, titel, tags en preview.

Taak: genereer [[wikilinks]] die inhoudelijk zinvol zijn — conceptuele verwantschap,
niet oppervlakkige trefwoordovereenkomst.

Strikte regels:
- Gebruik EXACT de 'file'-waarde uit de input als 'source' (pad inclusief map)
- Gebruik EXACT de 'file'-waarde (zonder .md extensie) als 'target'
- 'context' = sectienaam voor de link; gebruik altijd "Gerelateerd" tenzij een andere sectie evident beter is
- Max 3 links per bronbestand
- Geen links naar zichzelf
- Alleen bestanden die IN de input staan als source of target

Geef UITSLUITEND een JSON-array terug, geen uitleg, geen markdown:
[{"source": "map/A.md", "target": "map/B", "context": "Gerelateerd"}]

Als er geen zinvolle links zijn: []
"""

# ── Progress display ──────────────────────────────────────────────────────────

TERM_WIDTH = min(shutil.get_terminal_size((80, 20)).columns, 120)
BAR_WIDTH = 35


def _bar(done: int, total: int) -> str:
    pct = done / total if total else 1.0
    filled = int(BAR_WIDTH * pct)
    return "█" * filled + "░" * (BAR_WIDTH - filled)


class Progress:
    """Simple single-line progress bar with ETA."""

    def __init__(self, total: int, label: str):
        self.total = total
        self.done = 0
        self.label = label
        self._start = time.monotonic()
        self._last_status = ""

    def _render(self, status: str) -> str:
        elapsed = time.monotonic() - self._start
        rate = self.done / elapsed if elapsed > 0 else 0
        remaining = (self.total - self.done) / rate if rate > 0 and self.done > 0 else None
        eta = f"ETA {timedelta(seconds=int(remaining))}" if remaining is not None else "ETA --:--"
        bar = _bar(self.done, self.total)
        pct = f"{100*self.done/self.total:.0f}%" if self.total else "100%"
        line = f"  {self.label} [{bar}] {self.done}/{self.total} {pct}  {eta}"
        if status:
            line += f"  {status}"
        return line[:TERM_WIDTH]

    def update(self, n: int = 1, status: str = ""):
        self.done += n
        self._last_status = status
        sys.stdout.write("\r" + self._render(status).ljust(TERM_WIDTH))
        sys.stdout.flush()

    def peek(self, status: str):
        """Update display without incrementing counter."""
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

def build_vault_map(directory: str = "", max_preview_words: int = 50) -> list[dict]:
    scan_root = os.path.join(VAULT_PATH, directory) if directory else VAULT_PATH
    real_vault = os.path.realpath(VAULT_PATH)

    if not os.path.realpath(scan_root).startswith(real_vault):
        raise ValueError(f"Pad traversal niet toegestaan: {directory}")
    if not os.path.isdir(scan_root):
        raise FileNotFoundError(f"Map niet gevonden in vault: {directory!r}")

    # Eerst tellen voor progress
    all_md: list[str] = []
    for root, dirs, files in os.walk(scan_root):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.'))
        for fname in files:
            if fname.endswith('.md'):
                all_md.append(os.path.join(root, fname))

    prog = Progress(len(all_md), "Vault scannen")
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

        # Titel
        title = os.path.splitext(os.path.basename(full_path))[0]
        for line in raw.splitlines():
            if line.startswith('# '):
                title = line[2:].strip()
                break

        # Tags
        tags = list(dict.fromkeys('#' + m for m in _TAG_PATTERN.findall(raw)))[:20]

        # Preview
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
        prog.update(1)

    prog.finish(f"— {len(entries)} bestanden")
    return entries


# ── Stap 2: LLM-analyse in chunks ────────────────────────────────────────────

def _llm_call(messages: list, max_retries: int) -> tuple[str, int]:
    """OpenAI-compatibele LLM-call met exponentiële retry.
    Returns (content, attempts_used).
    """
    headers = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}
    payload = {"model": MODEL, "messages": messages, "stream": False}

    for attempt in range(1, max_retries + 1):
        try:
            client = httpx.Client(base_url=BASE_URL, headers=headers, timeout=LLM_TIMEOUT)
            response = client.post("/v1/chat/completions", json=payload)
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"], attempt
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            if code in (429, 503) and attempt < max_retries:
                wait = 2 ** attempt
                print(f"\n  ⚠ HTTP {code} — retry {attempt}/{max_retries} in {wait}s...")
                time.sleep(wait)
                continue
            raise  # 401, 404, etc. → meteen stoppen
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError):
            if attempt < max_retries:
                wait = 2 ** attempt
                print(f"\n  ⚠ Timeout — retry {attempt}/{max_retries} in {wait}s...")
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("max_retries bereikt")


def _extract_json(text: str) -> list:
    """Haal een JSON-array op uit LLM-output (ook als die in markdown staat)."""
    text = text.strip()
    for pattern in [
        r'```(?:json)?\s*(\[[\s\S]*?\])\s*```',
        r'(\[[\s\S]*\])',
    ]:
        m = re.search(pattern, text)
        if m:
            try:
                result = json.loads(m.group(1))
                if isinstance(result, list):
                    return result
            except json.JSONDecodeError:
                continue
    # Direct parse als fallback
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass
    return []


def analyze_vault_map(
    entries: list[dict],
    chunk_size: int,
    max_retries: int,
) -> list[dict]:
    chunks = [entries[i:i + chunk_size] for i in range(0, len(entries), chunk_size)]
    all_links: list[dict] = []
    failed_chunks: list[int] = []

    prog = Progress(len(chunks), "LLM-analyse  ")

    for i, chunk in enumerate(chunks):
        prog.peek(f"chunk {i+1}/{len(chunks)} ({len(chunk)} bestanden)...")
        messages = [
            {"role": "system", "content": LLM_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Batch {i+1} van {len(chunks)} "
                    f"({len(chunk)} bestanden):\n\n"
                    + json.dumps(chunk, ensure_ascii=False)
                ),
            },
        ]

        try:
            content, attempts = _llm_call(messages, max_retries=max_retries)
            links = _extract_json(content)
            all_links.extend(links)
            prog.update(1, f"+{len(links)} links (totaal {len(all_links)})")
        except Exception as e:
            failed_chunks.append(i + 1)
            prog.update(1, f"FOUT chunk {i+1}")
            warn(f"Chunk {i+1} mislukt: {e}")

    prog.finish(f"— {len(all_links)} links, {len(failed_chunks)} chunk(s) mislukt")
    if failed_chunks:
        warn(f"Mislukte chunks: {failed_chunks}")
    return all_links


# ── Stap 3: Mechanische injectie ─────────────────────────────────────────────

def _insert_before_timestamps(content: str, text: str) -> str:
    m = _TS_PATTERN.search(content)
    if m:
        return content[:m.start()].rstrip() + f'\n{text}\n\n' + content[m.start():]
    return content.rstrip() + f'\n{text}\n'


def _do_inject(content: str, wikilink: str, context: str) -> str:
    """Voer de daadwerkelijke link-injectie uit op de content-string."""
    inserted = False
    new_content = content

    if context:
        section_pat = re.compile(
            r'^#{1,3}\s+' + re.escape(context) + r'\s*$',
            re.IGNORECASE | re.MULTILINE,
        )
        sm = section_pat.search(new_content)
        if sm:
            next_h = re.search(r'^#{1,3}\s', new_content[sm.end():], re.MULTILINE)
            if next_h:
                cut = sm.end() + next_h.start()
                new_content = new_content[:cut].rstrip() + f'\n{wikilink}\n\n' + new_content[cut:]
            else:
                new_content = _insert_before_timestamps(new_content, wikilink)
            inserted = True

    if not inserted:
        gm = re.search(r'^#{1,3}\s+Gerelateerd\s*$', new_content, re.IGNORECASE | re.MULTILINE)
        if gm:
            next_h = re.search(r'^#{1,3}\s', new_content[gm.end():], re.MULTILINE)
            if next_h:
                cut = gm.end() + next_h.start()
                new_content = new_content[:cut].rstrip() + f'\n{wikilink}\n\n' + new_content[cut:]
            else:
                new_content = _insert_before_timestamps(new_content, wikilink)
        else:
            block = f'## Gerelateerd\n{wikilink}'
            new_content = _insert_before_timestamps(new_content, block)

    return new_content


def inject_links(
    matrix: list[dict],
    dry_run: bool = False,
) -> tuple[int, int, int, list[dict]]:
    """
    Returns: (injected, skipped, errors, dry_run_log)
    dry_run_log is alleen gevuld als dry_run=True.
    """
    real_vault = os.path.realpath(VAULT_PATH)
    injected = 0
    skipped = 0
    errors = 0
    modified: set[str] = set()
    dry_log: list[dict] = []

    prog = Progress(len(matrix), "Injecteren   ")

    for entry in matrix:
        source = (entry.get('source') or '').strip()
        target = (entry.get('target') or '').strip()
        context = (entry.get('context') or '').strip()

        prog.peek(os.path.basename(source)[:40])

        if not source or not target:
            errors += 1
            prog.update(1, "leeg entry")
            continue

        src_path = os.path.join(VAULT_PATH, source)
        if not os.path.realpath(src_path).startswith(real_vault):
            errors += 1
            prog.update(1, "path traversal")
            continue
        if not os.path.isfile(src_path):
            errors += 1
            prog.update(1, f"niet gevonden: {source}")
            continue

        try:
            with open(src_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            errors += 1
            prog.update(1, f"leesfout: {e}")
            continue

        wikilink = f'[[{target}]]'

        # Duplicaat?
        if wikilink in content:
            skipped += 1
            prog.update(1, "dup")
            continue

        new_content = _do_inject(content, wikilink, context)

        if dry_run:
            dry_log.append({'source': source, 'target': target, 'wikilink': wikilink, 'context': context})
            injected += 1
            prog.update(1, f"[DRY] {wikilink}")
        else:
            try:
                with open(src_path, 'w', encoding='utf-8') as f:
                    f.write(new_content)
                modified.add(src_path)
                injected += 1
                prog.update(1)
            except Exception as e:
                errors += 1
                prog.update(1, f"schrijffout: {e}")

    prog.finish(
        f"— {injected} geïnjecteerd, {skipped} geskipt, {errors} fout"
        + (" [DRY-RUN]" if dry_run else "")
    )
    return injected, skipped, errors, dry_log


# ── Samenvatting ─────────────────────────────────────────────────────────────

def print_summary(
    n_files: int,
    n_links: int,
    injected: int,
    skipped: int,
    errors: int,
    matrix_path: str,
    dry_run: bool,
    dry_log: list[dict],
):
    section("SAMENVATTING")
    info(f"Vault-bestanden gescand : {n_files}")
    info(f"Links in matrix         : {n_links}")
    info(f"Links geïnjecteerd      : {injected}" + (" (DRY-RUN, geen wijzigingen)" if dry_run else ""))
    info(f"Duplicaten geskipt      : {skipped}")
    info(f"Fouten                  : {errors}")
    info(f"Link matrix opgeslagen  : {matrix_path}")

    if dry_run and dry_log:
        print()
        info("DRY-RUN preview (eerste 20):")
        for entry in dry_log[:20]:
            info(f"  {entry['source']}  ←  {entry['wikilink']}")
        if len(dry_log) > 20:
            info(f"  ... en {len(dry_log)-20} meer")

    if not dry_run and errors == 0:
        info("\n  ✓ Klaar — vault bijgewerkt.")
    elif not dry_run and errors > 0:
        warn(f"{errors} injecties mislukt. Controleer bovenstaande output.")


# ── CLI entrypoint ────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mechanical Link Injector — scan vault, analyseer met LLM, injecteer [[wikilinks]]",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Voorbeelden:
  python link_injector.py                          # volledige vault
  python link_injector.py --dry-run                # preview, geen schrijven
  python link_injector.py --directory 10_Kern      # alleen submap
  python link_injector.py --chunk-size 30          # kleinere LLM-chunks
  python link_injector.py --from-matrix matrix.json  # hergebruik bestaande matrix
""",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview wat er geïnjecteerd zou worden — schrijft niets naar de vault",
    )
    parser.add_argument(
        "--directory", default="",
        help="Scan alleen deze submap (relatief pad in vault, bijv. '10_Kern')",
    )
    parser.add_argument(
        "--chunk-size", type=int, default=50, metavar="N",
        help="Bestanden per LLM-chunk (standaard: 50)",
    )
    parser.add_argument(
        "--preview-words", type=int, default=50, metavar="N",
        help="Max woorden per bestand in de vault map (standaard: 50)",
    )
    parser.add_argument(
        "--max-retries", type=int, default=3, metavar="N",
        help="Max LLM-retries per chunk bij 429/503/timeout (standaard: 3)",
    )
    parser.add_argument(
        "--from-matrix", metavar="FILE",
        help="Sla scan+LLM over en gebruik een bestaand link_matrix.json bestand",
    )
    parser.add_argument(
        "--save-matrix", metavar="FILE",
        default=os.path.join(AGENT_TEMP_PATH, "link_matrix.json"),
        help="Pad om link matrix op te slaan (standaard: .agent_temp/link_matrix.json)",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    print(f"\n{'═'*TERM_WIDTH}")
    print("  MECHANICAL LINK INJECTOR")
    print(f"  Vault : {VAULT_PATH}")
    print(f"  Model : {MODEL} @ {BASE_URL}")
    if args.dry_run:
        print("  Modus : DRY-RUN (geen wijzigingen in vault)")
    print(f"{'═'*TERM_WIDTH}")

    # ── Stap 1: Scan of laad bestaande matrix ─────────────────────────────────
    if args.from_matrix:
        section("STAP 1+2: Bestaande link matrix laden")
        try:
            with open(args.from_matrix, 'r', encoding='utf-8') as f:
                matrix = json.load(f)
            info(f"Matrix geladen: {len(matrix)} links uit {args.from_matrix}")
            n_files = "?"
        except Exception as e:
            error(f"Kan matrix niet laden: {e}")
            sys.exit(1)
    else:
        section(f"STAP 1: Vault scannen{'  [' + args.directory + ']' if args.directory else ''}")
        try:
            entries = build_vault_map(
                directory=args.directory,
                max_preview_words=args.preview_words,
            )
        except Exception as e:
            error(f"Scan mislukt: {e}")
            sys.exit(1)

        n_files = len(entries)
        n_chunks = (n_files + args.chunk_size - 1) // args.chunk_size
        est_tokens = n_files * args.preview_words * 1.3
        info(f"{n_files} bestanden → {n_chunks} chunks × {args.chunk_size} bestanden")
        info(f"Geschat ~{est_tokens/1000:.0f}k tokens totaal voor LLM-analyse")

        # ── Stap 2: LLM-analyse ───────────────────────────────────────────────
        section("STAP 2: LLM-analyse (chunks)")
        try:
            matrix = analyze_vault_map(
                entries,
                chunk_size=args.chunk_size,
                max_retries=args.max_retries,
            )
        except Exception as e:
            error(f"LLM-analyse afgebroken: {e}")
            sys.exit(1)

        # Matrix opslaan voor hergebruik
        os.makedirs(os.path.dirname(args.save_matrix), exist_ok=True)
        try:
            with open(args.save_matrix, 'w', encoding='utf-8') as f:
                json.dump(matrix, f, ensure_ascii=False, indent=2)
            info(f"Link matrix opgeslagen: {args.save_matrix}")
        except Exception as e:
            warn(f"Opslaan matrix mislukt: {e}")

    # ── Stap 3: Injectie ──────────────────────────────────────────────────────
    section("STAP 3: Mechanische injectie" + (" [DRY-RUN]" if args.dry_run else ""))
    injected, skipped, errors, dry_log = inject_links(
        matrix,
        dry_run=args.dry_run,
    )

    # ── Samenvatting ──────────────────────────────────────────────────────────
    print_summary(
        n_files=n_files,
        n_links=len(matrix),
        injected=injected,
        skipped=skipped,
        errors=errors,
        matrix_path=args.save_matrix if not args.from_matrix else args.from_matrix,
        dry_run=args.dry_run,
        dry_log=dry_log,
    )
    print(f"{'═'*TERM_WIDTH}\n")

    sys.exit(1 if errors > 0 else 0)


if __name__ == "__main__":
    main()
