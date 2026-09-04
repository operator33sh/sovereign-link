"""
link_injector.moc_builder — MOC-bestanden aanmaken en spoke-links injecteren.
"""

import os
import re
from datetime import datetime

from link_injector.scanner import VAULT_PATH, MOC_DIR, Progress, info, warn
from link_injector.injector import _do_inject, _insert_before_timestamps
from link_injector.analyzer import analyze_for_sub_mocs


# ── MOC templates ─────────────────────────────────────────────────────────────

def _moc_timestamp() -> str:
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


def _top_moc_template(moc_name: str, moc_title: str, sub_mocs: list[dict]) -> str:
    sub_links = '\n'.join(
        f"- [[{sc['sub_moc_name']}]] — {sc.get('sub_moc_title', sc['sub_moc_name'])}"
        for sc in sub_mocs
    )
    return (
        f"# {moc_name}\n\n"
        f"## 👑 Centraal Dashboard: {moc_title}\n\n"
        f"Dit dashboard fungeert als het centrale punt voor {moc_title}.\n\n"
        f"### 🗂️ Sub-thema's\n\n"
        f"{sub_links}\n\n"
        f"{_moc_timestamp()}\n"
    )


def _sub_moc_template(sub_moc_name: str, sub_moc_title: str, parent_name: str, spokes: list[str]) -> str:
    spoke_links = '\n'.join(
        f'- [[{os.path.splitext(os.path.basename(s))[0]}]]'
        for s in spokes
    )
    return (
        f"# {sub_moc_name}\n\n"
        f"Onderdeel van [[{parent_name}]] — {sub_moc_title}\n\n"
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
    """Wijs bestanden zonder MOC toe aan de beste cluster op basis van tag-overlap."""
    covered = {s for c in clusters for s in c.get('spokes', [])}

    cluster_tags: list[set[str]] = []
    entry_map = {e['file']: e for e in all_entries}
    for c in clusters:
        tags: set[str] = set()
        for spoke in c.get('spokes', []):
            if spoke in entry_map:
                tags.update(t.lower() for t in entry_map[spoke].get('tags', []))
        cluster_tags.append(tags)

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

        file_tags  = set(t.lower() for t in entry.get('tags', []))
        file_words = set(entry.get('title', '').lower().split())

        best_score = -1.0
        best_idx   = 0
        for i, (ctags, ckw) in enumerate(zip(cluster_tags, cluster_keywords)):
            union     = file_tags | ctags
            tag_score = len(file_tags & ctags) / len(union) if union else 0.0
            kw_score  = len(file_words & ckw) / max(len(ckw), 1)
            score     = tag_score * 2 + kw_score
            if score > best_score:
                best_score = score
                best_idx   = i

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
    moc_depth: int = 1,
    sub_count: int = 3,
    max_retries: int = 3,
    all_entries: list[dict] | None = None,
) -> tuple[int, int, int, int, list[dict]]:
    """Maak MOC-bestanden aan (of update ze) en injecteer bidirectionele links.

    Returns: (mocs_created, mocs_updated, spoke_links_injected, errors, dry_log)
    """
    real_vault  = os.path.realpath(VAULT_PATH)
    moc_dir_abs = os.path.join(VAULT_PATH, MOC_DIR)

    mocs_created   = 0
    mocs_updated   = 0
    spoke_injected = 0
    errors         = 0
    dry_log: list[dict] = []

    if not dry_run:
        os.makedirs(moc_dir_abs, exist_ok=True)

    total_ops = sum(1 + len(c.get('spokes', [])) for c in clusters)
    prog      = Progress(total_ops, "MOC injectie ")

    entries_by_file: dict[str, dict] = {e['file']: e for e in (all_entries or [])}

    for cluster in clusters:
        moc_name  = (cluster.get('moc_name') or '').strip()
        moc_title = (cluster.get('moc_title') or moc_name).strip()
        spokes    = [s for s in cluster.get('spokes', []) if s]

        if not moc_name or not moc_name.startswith('MOC_'):
            errors += 1
            prog.update(1, f"ongeldige MOC-naam: {moc_name}")
            continue

        moc_file = f"{moc_name}.md"
        moc_path = os.path.join(moc_dir_abs, moc_file)

        prog.peek(f"{moc_name} ({len(spokes)} spokes)")

        # ── Depth-2: sub-MOC hierarchy ────────────────────────────────────────
        if moc_depth >= 2:
            spoke_entries = [entries_by_file[s] for s in spokes if s in entries_by_file]
            sub_clusters  = (
                analyze_for_sub_mocs(moc_name, moc_title, spoke_entries,
                                     max_retries=max_retries, sub_count=sub_count)
                if spoke_entries else []
            )

            if sub_clusters:
                covered   = {s for sc in sub_clusters for s in sc.get('spokes', [])}
                uncovered = [s for s in spokes if s not in covered]
                if uncovered:
                    sub_clusters[0].setdefault('spokes', []).extend(uncovered)

                if dry_run:
                    dry_log.append({
                        'action':   'create_top_moc',
                        'moc':      os.path.join(MOC_DIR, moc_file),
                        'sub_mocs': len(sub_clusters),
                    })
                    mocs_created += 1
                    prog.update(1, f"[DRY] {moc_name} (top)")
                else:
                    try:
                        top_content = _top_moc_template(moc_name, moc_title, sub_clusters)
                        if not os.path.isfile(moc_path):
                            with open(moc_path, 'w', encoding='utf-8') as f:
                                f.write(top_content)
                            mocs_created += 1
                            prog.update(1, f"✚ {moc_name} (top)")
                        else:
                            with open(moc_path, 'r', encoding='utf-8') as f:
                                existing = f.read()
                            if top_content != existing:
                                with open(moc_path, 'w', encoding='utf-8') as f:
                                    f.write(top_content)
                                mocs_updated += 1
                                prog.update(1, f"↑ {moc_name} (top)")
                            else:
                                prog.update(1, f"= {moc_name} (ongewijzigd)")
                    except Exception as e:
                        errors += 1
                        prog.update(1, f"MOC-schrijffout: {e}")
                        continue

                for sc in sub_clusters:
                    sub_name   = (sc.get('sub_moc_name') or '').strip()
                    sub_title  = (sc.get('sub_moc_title') or sub_name).strip()
                    sub_spokes = [s for s in sc.get('spokes', []) if s]
                    if not sub_name:
                        continue

                    sub_file     = f"{sub_name}.md"
                    sub_path     = os.path.join(moc_dir_abs, sub_file)
                    sub_wikilink = f'[[{sub_name}]]'

                    if dry_run:
                        dry_log.append({
                            'action': 'create_sub_moc',
                            'moc':    os.path.join(MOC_DIR, sub_file),
                            'spokes': len(sub_spokes),
                        })
                        mocs_created += 1
                    else:
                        try:
                            if not os.path.isfile(sub_path):
                                with open(sub_path, 'w', encoding='utf-8') as f:
                                    f.write(_sub_moc_template(sub_name, sub_title, moc_name, sub_spokes))
                                mocs_created += 1
                            else:
                                with open(sub_path, 'r', encoding='utf-8') as f:
                                    sub_content = f.read()
                                new_links = [
                                    f'- [[{os.path.splitext(os.path.basename(s))[0]}]]'
                                    for s in sub_spokes
                                ]
                                updated = _update_moc_kernconcepten(sub_content, new_links)
                                if updated != sub_content:
                                    with open(sub_path, 'w', encoding='utf-8') as f:
                                        f.write(updated)
                                    mocs_updated += 1
                        except Exception as e:
                            errors += 1
                            continue

                    for spoke in sub_spokes:
                        src_path = os.path.join(VAULT_PATH, spoke)
                        if not os.path.realpath(src_path).startswith(real_vault):
                            errors += 1
                            prog.update(1, "path traversal")
                            continue
                        if not os.path.isfile(src_path):
                            prog.update(1, f"spoke geskipt: {spoke}")
                            continue
                        try:
                            with open(src_path, 'r', encoding='utf-8') as f:
                                content = f.read()
                        except Exception as e:
                            errors += 1
                            prog.update(1, f"leesfout: {e}")
                            continue
                        if sub_wikilink in content:
                            prog.update(1, "dup")
                            continue
                        new_content = _do_inject(content, sub_wikilink, 'Gerelateerd')
                        if dry_run:
                            dry_log.append({'action': 'spoke_link', 'source': spoke, 'wikilink': sub_wikilink})
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

                continue  # next cluster — depth-2 done

            info(f"  Sub-clustering voor {moc_name} leeg — terugval naar depth-1")

        # ── Depth-1: flat MOC ─────────────────────────────────────────────────
        moc_wikilink   = f'[[{moc_name}]]'
        spoke_wikilinks = [
            f'- [[{os.path.splitext(os.path.basename(s))[0]}]]'
            for s in spokes
        ]

        if dry_run:
            exists = os.path.isfile(moc_path)
            dry_log.append({
                'action': 'update_moc' if exists else 'create_moc',
                'moc':    os.path.join(MOC_DIR, moc_file),
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
    moc_dir_abs = os.path.join(VAULT_PATH, MOC_DIR)
    home_path   = os.path.join(moc_dir_abs, "MOC_Home.md")

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
            new_links = []
            for c in clusters:
                name = c.get('moc_name', '')
                if name.startswith('MOC_') and f'[[{name}]]' not in existing:
                    new_links.append(f"- [[{name}]] — {c.get('moc_title', name)}")
            if new_links:
                insert = '\n'.join(new_links)
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
