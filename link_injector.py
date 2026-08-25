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

# ── .env laden (standalone script heeft geen agent-bootstrap) ─────────────────

def _load_dotenv() -> None:
    """Laad .env vanuit de scriptdirectory of de werkdirectory."""
    for candidate in (
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
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

_load_dotenv()

# ── Configuratie ──────────────────────────────────────────────────────────────

VAULT_PATH = os.environ.get("VAULT_PATH", "/home/wouter/Documents/fractalisme-vault")
AGENT_TEMP_PATH = os.path.join(VAULT_PATH, ".agent_temp")
MOC_DIR = os.environ.get("MOC_DIR", "MOCs")  # submap in vault voor MOC-bestanden
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

MOC_SYSTEM_PROMPT = """\
Je bent een vault-architect voor een Obsidian kennisbank in het Nederlands.
Je krijgt een lijst vault-bestanden (JSON) met bestandsnaam ('file'), titel en tags.

Taak: groepeer de bestanden in PRECIES {moc_count} thematische clusters. Elk cluster krijgt één MOC (Map of Content).
Dit vormt een klaverblad-structuur: één centraal Home-MOC linkt naar {moc_count} thematische MOCs, die elk linken naar de bijbehorende notes.

KRITIEKE REGELS:
- Maak PRECIES {moc_count} clusters — niet meer, niet minder
- Gebruik EXACT de 'file'-waarde als spoke-pad — kopieer het letterlijk uit de input, inclusief mapnaam, spaties en extensie (.md). Verzin GEEN bestandsnamen.
- Elk bestand mag in MAX 2 clusters zitten
- MOC-naam: korte CamelCase naam zonder spaties, prefix 'MOC_' (bijv. MOC_Fractalisme)
- moc_title: leesbare Nederlandse naam (bijv. "Fractalisme")
- Sla bestaande MOC-bestanden (die al 'MOC_' in de naam hebben) over als source
- Geen limiet op spokes — probeer zo veel mogelijk bestanden te dekken, verdeel ze evenwichtig

Geef UITSLUITEND een JSON-array terug, geen uitleg, geen markdown:
[{{"moc_name": "MOC_Thema", "moc_title": "Thema", "spokes": ["map/A.md", "B.md"]}}]

Als er geen clusters zijn: []
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

def build_vault_map(
    directory: str = "",
    max_preview_words: int = 50,
    uncovered_only: bool = False,
) -> list[dict]:
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

    # Filter op bestanden zonder bestaande wikilinks
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


def analyze_for_mocs(entries: list[dict], max_retries: int, moc_count: int = 5) -> list[dict]:
    """Één LLM-call: groepeer alle vault-entries in thematische MOC-clusters."""
    condensed = [{'file': e['file'], 'title': e['title'], 'tags': e['tags']} for e in entries]
    condensed = [e for e in condensed if 'MOC_' not in os.path.basename(e['file'])]

    info(f"MOC-analyse: {len(condensed)} bestanden → {moc_count} clusters (één LLM-call)...")
    prompt = MOC_SYSTEM_PROMPT.format(moc_count=moc_count)
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": json.dumps(condensed, ensure_ascii=False)},
    ]
    try:
        content, attempts = _llm_call(messages, max_retries)
        clusters = _extract_json(content)
        info(f"LLM leverde {len(clusters)} clusters (na {attempts} poging(en))")
        return clusters
    except Exception as e:
        warn(f"MOC-analyse mislukt: {e}")
        return []


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

        # Strip .md extensie — Obsidian wikilinks bevatten nooit de extensie
        target_clean = target.removesuffix('.md')
        wikilink = f'[[{target_clean}]]'

        # Duplicaat? Check ook de foute variant met extensie
        if wikilink in content or f'[[{target}]]' in content:
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


# ── MOC-pipeline ─────────────────────────────────────────────────────────────

def _moc_timestamp() -> str:
    from datetime import datetime
    now = datetime.now()
    return f"#MOC #{now.strftime('%Y-%m-%d')} #{now.strftime('%H')} #{now.strftime('%M')}"


def _moc_template(moc_name: str, moc_title: str, spokes: list[str]) -> str:
    spoke_links = '\n'.join(
        f'- [[{os.path.splitext(os.path.basename(s))[0]}]]'
        for s in spokes
    )
    return (
        f"# {moc_name}\n\n"
        f"## 👑 Centraal Dashboard: {moc_title}\n\n"
        f"Dit dashboard fungeert als het centrale punt voor {moc_title}.\n\n"
        f"### 💎 Kernconcepten\n\n"
        f"{spoke_links}\n\n"
        f"{_moc_timestamp()}\n"
    )


def _update_moc_kernconcepten(content: str, new_links: list[str]) -> str:
    """Voeg nieuwe spoke-links toe aan bestaande MOC Kernconcepten sectie. Idempotent."""
    section_re = re.compile(r'(### 💎 Kernconcepten\n)(.*?)(?=\n## |\n#[^#]|\Z)', re.DOTALL)
    m = section_re.search(content)
    if not m:
        block = '### 💎 Kernconcepten\n\n' + '\n'.join(new_links) + '\n\n'
        return _insert_before_timestamps(content, block)
    existing_block = m.group(2)
    to_add = [lnk for lnk in new_links if lnk not in existing_block]
    if not to_add:
        return content
    new_block = existing_block.rstrip() + '\n' + '\n'.join(to_add) + '\n'
    return content[:m.start(2)] + new_block + content[m.end(2):]


def _assign_uncovered_files(clusters: list[dict], all_entries: list[dict]) -> list[dict]:
    """Wijs bestanden zonder MOC toe aan de beste cluster op basis van tag-overlap.
    Mechanisch — geen LLM-call. Muteert clusters in-place.
    """
    covered = {s for c in clusters for s in c.get('spokes', [])}

    # Tag-profiel per cluster (union van alle spoke-tags)
    cluster_tags: list[set[str]] = []
    entry_map = {e['file']: e for e in all_entries}
    for c in clusters:
        tags: set[str] = set()
        for spoke in c.get('spokes', []):
            if spoke in entry_map:
                tags.update(t.lower() for t in entry_map[spoke].get('tags', []))
        cluster_tags.append(tags)

    # Naam-keywords per cluster (moc_title + moc_name)
    cluster_keywords: list[set[str]] = []
    for c in clusters:
        words = set(c.get('moc_title', '').lower().split())
        words.update(c.get('moc_name', '').lower().replace('moc_', '').split('_'))
        cluster_keywords.append(words)

    assigned = 0
    for entry in all_entries:
        f = entry['file']
        if f in covered or 'MOC_' in os.path.basename(f):
            continue

        file_tags = set(t.lower() for t in entry.get('tags', []))
        file_words = set(entry.get('title', '').lower().split())

        best_score = -1.0
        best_idx = 0
        for i, (ctags, ckw) in enumerate(zip(cluster_tags, cluster_keywords)):
            union = file_tags | ctags
            tag_score = len(file_tags & ctags) / len(union) if union else 0.0
            kw_score = len(file_words & ckw) / max(len(ckw), 1)
            score = tag_score * 2 + kw_score
            if score > best_score:
                best_score = score
                best_idx = i

        clusters[best_idx].setdefault('spokes', []).append(f)
        cluster_tags[best_idx].update(file_tags)
        covered.add(f)
        assigned += 1

    if assigned:
        info(f"Fallback: {assigned} ongedekte bestanden toegewezen op basis van tag-overlap")
    return clusters


def inject_moc_links(
    clusters: list[dict],
    dry_run: bool = False,
) -> tuple[int, int, int, int, list[dict]]:
    """Maak MOC-bestanden aan (of update ze) en injecteer bidirectionele links.

    Returns: (mocs_created, mocs_updated, spoke_links_injected, errors, dry_log)
    """
    real_vault = os.path.realpath(VAULT_PATH)
    moc_dir_abs = os.path.join(VAULT_PATH, MOC_DIR)

    mocs_created = 0
    mocs_updated = 0
    spoke_injected = 0
    errors = 0
    dry_log: list[dict] = []

    if not dry_run:
        os.makedirs(moc_dir_abs, exist_ok=True)

    total_ops = sum(1 + len(c.get('spokes', [])) for c in clusters)
    prog = Progress(total_ops, "MOC injectie ")

    for cluster in clusters:
        moc_name = (cluster.get('moc_name') or '').strip()
        moc_title = (cluster.get('moc_title') or moc_name).strip()
        spokes = [s for s in cluster.get('spokes', []) if s]

        if not moc_name or not moc_name.startswith('MOC_'):
            errors += 1
            prog.update(1, f"ongeldige MOC-naam: {moc_name}")
            continue

        moc_file = f"{moc_name}.md"
        moc_path = os.path.join(moc_dir_abs, moc_file)
        moc_wikilink = f'[[{moc_name}]]'

        prog.peek(f"{moc_name} ({len(spokes)} spokes)")

        spoke_wikilinks = [
            f'- [[{os.path.splitext(os.path.basename(s))[0]}]]'
            for s in spokes
        ]

        if dry_run:
            exists = os.path.isfile(moc_path)
            dry_log.append({
                'action': 'update_moc' if exists else 'create_moc',
                'moc': os.path.join(MOC_DIR, moc_file),
                'spokes': len(spokes),
            })
            if exists:
                mocs_updated += 1
            else:
                mocs_created += 1
            prog.update(1, f"[DRY] {moc_name}")
        else:
            try:
                if not os.path.isfile(moc_path):
                    with open(moc_path, 'w', encoding='utf-8') as f:
                        f.write(_moc_template(moc_name, moc_title, spokes))
                    mocs_created += 1
                    prog.update(1, f"✚ {moc_name}")
                else:
                    with open(moc_path, 'r', encoding='utf-8') as f:
                        moc_content = f.read()
                    updated = _update_moc_kernconcepten(moc_content, spoke_wikilinks)
                    if updated != moc_content:
                        with open(moc_path, 'w', encoding='utf-8') as f:
                            f.write(updated)
                        mocs_updated += 1
                        prog.update(1, f"↑ {moc_name}")
                    else:
                        prog.update(1, f"= {moc_name} (geen nieuwe spokes)")
            except Exception as e:
                errors += 1
                prog.update(1, f"MOC-schrijffout: {e}")
                continue

        for spoke in spokes:
            src_path = os.path.join(VAULT_PATH, spoke)
            if not os.path.realpath(src_path).startswith(real_vault):
                errors += 1
                prog.update(1, "path traversal")
                continue
            if not os.path.isfile(src_path):
                prog.update(1, f"spoke geskipt (niet gevonden): {spoke}")
                continue
            try:
                with open(src_path, 'r', encoding='utf-8') as f:
                    content = f.read()
            except Exception as e:
                errors += 1
                prog.update(1, f"leesfout: {e}")
                continue
            if moc_wikilink in content:
                prog.update(1, "dup")
                continue
            new_content = _do_inject(content, moc_wikilink, 'Gerelateerd')
            if dry_run:
                dry_log.append({'action': 'spoke_link', 'source': spoke, 'wikilink': moc_wikilink})
                spoke_injected += 1
                prog.update(1, f"[DRY] {os.path.basename(spoke)}")
            else:
                try:
                    with open(src_path, 'w', encoding='utf-8') as f:
                        f.write(new_content)
                    spoke_injected += 1
                    prog.update(1)
                except Exception as e:
                    errors += 1
                    prog.update(1, f"schrijffout: {e}")

    prog.finish(
        f"— {mocs_created} MOCs aangemaakt, {mocs_updated} bijgewerkt, "
        f"{spoke_injected} spoke-links geïnjecteerd, {errors} fouten"
        + (" [DRY-RUN]" if dry_run else "")
    )
    return mocs_created, mocs_updated, spoke_injected, errors, dry_log


def _create_home_moc(clusters: list[dict], dry_run: bool = False) -> bool:
    """Maak of update de centrale Home MOC die naar alle thematische MOCs linkt.

    De Home MOC vormt het middelpunt van de klaverblad-structuur.
    Returns True als aangemaakt/bijgewerkt, False bij fout.
    """
    moc_dir_abs = os.path.join(VAULT_PATH, MOC_DIR)
    home_path = os.path.join(moc_dir_abs, "MOC_Home.md")

    moc_links = "\n".join(
        f"- [[{c['moc_name']}]] — {c.get('moc_title', c['moc_name'])}"
        for c in clusters
        if c.get('moc_name', '').startswith('MOC_')
    )

    content = f"""\
# 🏠 Home MOC

Centraal overzicht van alle thematische kennisgebieden.

## 🍀 Thematische MOCs

{moc_links}
"""

    if dry_run:
        info(f"[DRY] Home MOC: {len(clusters)} thematische MOCs gelinkt")
        return True

    try:
        os.makedirs(moc_dir_abs, exist_ok=True)
        if os.path.isfile(home_path):
            with open(home_path, 'r', encoding='utf-8') as f:
                existing = f.read()
            # Voeg nieuwe MOC-links toe die nog ontbreken
            new_links = []
            for c in clusters:
                name = c.get('moc_name', '')
                if name.startswith('MOC_') and f'[[{name}]]' not in existing:
                    new_links.append(f"- [[{name}]] — {c.get('moc_title', name)}")
            if new_links:
                insert = '\n'.join(new_links)
                # Voeg in na "## 🍀 Thematische MOCs" sectie
                m = re.search(r'^## 🍀 Thematische MOCs\s*$', existing, re.MULTILINE)
                if m:
                    updated = existing[:m.end()].rstrip() + '\n\n' + insert + '\n' + existing[m.end():]
                else:
                    updated = existing.rstrip() + '\n\n' + insert + '\n'
                with open(home_path, 'w', encoding='utf-8') as f:
                    f.write(updated)
                info(f"Home MOC bijgewerkt: {len(new_links)} nieuwe links toegevoegd")
            else:
                info("Home MOC: geen nieuwe MOC-links")
        else:
            with open(home_path, 'w', encoding='utf-8') as f:
                f.write(content)
            info(f"Home MOC aangemaakt: {home_path}")
        return True
    except Exception as e:
        warn(f"Home MOC aanmaken mislukt: {e}")
        return False


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
        "--uncovered-only", action="store_true",
        help="Analyseer alleen bestanden die nog geen [[wikilinks]] bevatten",
    )
    parser.add_argument(
        "--create-mocs", action="store_true",
        help="MOC-modus: groepeer vault in clusters, maak MOC-bestanden aan, injecteer bidirectionele links",
    )
    parser.add_argument(
        "--moc-count", type=int, default=5, metavar="N",
        help="Aantal thematische MOCs (klaverblad-bladen, standaard: 5)",
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
                uncovered_only=args.uncovered_only,
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

    # ── MOC-pipeline (optioneel) ───────────────────────────────────────────────
    if args.create_mocs:
        section("MOC-PIPELINE: Clusters analyseren")

        # Als --from-matrix gebruikt werd, hebben we geen entries — scan opnieuw
        if args.from_matrix:
            info("Vault opnieuw scannen voor MOC-analyse (geen entries van --from-matrix)...")
            try:
                entries = build_vault_map(max_preview_words=args.preview_words)
            except Exception as e:
                error(f"Scan mislukt: {e}")
                sys.exit(1)

        clusters = analyze_for_mocs(entries, max_retries=args.max_retries, moc_count=args.moc_count)
        clusters = _assign_uncovered_files(clusters, entries)

        if not clusters:
            warn("Geen MOC-clusters gevonden — MOC-pipeline overgeslagen")
        else:
            moc_matrix_path = os.path.join(AGENT_TEMP_PATH, "moc_clusters.json")
            os.makedirs(AGENT_TEMP_PATH, exist_ok=True)
            try:
                with open(moc_matrix_path, 'w', encoding='utf-8') as f:
                    json.dump(clusters, f, ensure_ascii=False, indent=2)
                info(f"{len(clusters)} clusters opgeslagen: {moc_matrix_path}")
            except Exception as e:
                warn(f"Opslaan clusters mislukt: {e}")

            section(f"MOC-PIPELINE: Aanmaken + linken{'  [DRY-RUN]' if args.dry_run else ''}")
            mocs_created, mocs_updated, spoke_injected, moc_errors, _ = inject_moc_links(
                clusters,
                dry_run=args.dry_run,
            )

            _create_home_moc(clusters, dry_run=args.dry_run)

            section("MOC SAMENVATTING")
            info(f"MOC-bestanden aangemaakt : {mocs_created}")
            info(f"MOC-bestanden bijgewerkt : {mocs_updated}")
            info(f"Spoke-links geïnjecteerd : {spoke_injected}")
            info(f"Fouten                   : {moc_errors}")
            info(f"MOC-map                  : {os.path.join(VAULT_PATH, MOC_DIR)}/")
            info(f"Home MOC                 : {os.path.join(VAULT_PATH, MOC_DIR, 'MOC_Home.md')}")
            if moc_errors > 0:
                warn(f"{moc_errors} fouten in MOC-pipeline.")
            errors += moc_errors

        print(f"{'═'*TERM_WIDTH}\n")

    sys.exit(1 if errors > 0 else 0)


if __name__ == "__main__":
    main()
