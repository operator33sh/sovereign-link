"""
link_injector.analyzer — LLM-analyse van vault-bestanden.
"""

import json
import os
import re
import time

import httpx

from link_injector.scanner import (
    API_KEY, BASE_URL, LLM_TIMEOUT, MODEL,
    Progress, info, warn,
)

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

SUB_MOC_SYSTEM_PROMPT = """\
Je bent een vault-architect voor een Obsidian kennisbank in het Nederlands.
Je krijgt een lijst vault-bestanden (JSON) die allemaal onder het thema '{parent_title}' vallen.

Taak: groepeer deze bestanden in {sub_count} sub-thema's. Elk sub-thema krijgt één Sub-MOC.

KRITIEKE REGELS:
- Maak PRECIES {sub_count} sub-clusters (of minder als het thema te klein is voor sub-clustering)
- Gebruik EXACT de 'file'-waarde als spoke-pad — kopieer letterlijk inclusief mapnaam en extensie (.md)
- sub_moc_name: prefix '{parent_name}_' gevolgd door een korte CamelCase naam (bijv. {parent_name}_Patronen)
- sub_moc_title: leesbare Nederlandse naam voor het sub-thema
- Verdeel de bestanden zo evenwichtig mogelijk
- Elk bestand in PRECIES één sub-cluster

Geef UITSLUITEND een JSON-array terug, geen uitleg, geen markdown:
[{{"sub_moc_name": "{parent_name}_SubThema", "sub_moc_title": "Sub Thema", "spokes": ["map/A.md"]}}]

Als het thema te klein is voor sub-clusters: []
"""


def _llm_call(messages: list, max_retries: int) -> tuple[str, int]:
    """OpenAI-compatibele LLM-call met exponentiële retry.
    Returns (content, attempts_used).
    """
    headers = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}
    payload = {"model": MODEL, "messages": messages, "stream": False}

    for attempt in range(1, max_retries + 1):
        try:
            client   = httpx.Client(base_url=BASE_URL, headers=headers, timeout=LLM_TIMEOUT)
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
            raise
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
    chunks     = [entries[i:i + chunk_size] for i in range(0, len(entries), chunk_size)]
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
    condensed = [{'file': e['file'], 'title': e['title'], 'tags': e['tags']} for e in entries]
    condensed = [e for e in condensed if 'MOC_' not in os.path.basename(e['file'])]

    info(f"MOC-analyse: {len(condensed)} bestanden → {moc_count} clusters (één LLM-call)...")
    prompt   = MOC_SYSTEM_PROMPT.format(moc_count=moc_count)
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


def analyze_for_sub_mocs(
    parent_name: str,
    parent_title: str,
    spoke_entries: list[dict],
    max_retries: int,
    sub_count: int = 3,
) -> list[dict]:
    condensed = [{'file': e['file'], 'title': e['title'], 'tags': e['tags']} for e in spoke_entries]
    if not condensed:
        return []

    info(f"  Sub-MOC analyse: {len(condensed)} notes → {sub_count} sub-clusters voor {parent_name}...")
    prompt   = SUB_MOC_SYSTEM_PROMPT.format(
        parent_title=parent_title,
        parent_name=parent_name,
        sub_count=sub_count,
    )
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": json.dumps(condensed, ensure_ascii=False)},
    ]
    try:
        content, attempts = _llm_call(messages, max_retries)
        sub_clusters = _extract_json(content)
        info(f"  → {len(sub_clusters)} sub-clusters (na {attempts} poging(en))")
        return sub_clusters
    except Exception as e:
        warn(f"  Sub-MOC analyse mislukt voor {parent_name}: {e}")
        return []
