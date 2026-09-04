"""
link_injector.injector — mechanische [[wikilink]] injectie in vault-bestanden.
"""

import os
import re

from link_injector.scanner import VAULT_PATH, Progress, _TS_PATTERN


def _insert_before_timestamps(content: str, text: str) -> str:
    m = _TS_PATTERN.search(content)
    if m:
        return content[:m.start()].rstrip() + f'\n{text}\n\n' + content[m.start():]
    return content.rstrip() + f'\n{text}\n'


def _do_inject(content: str, wikilink: str, context: str) -> str:
    """Voer de daadwerkelijke link-injectie uit op de content-string."""
    inserted    = False
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
                cut         = sm.end() + next_h.start()
                new_content = new_content[:cut].rstrip() + f'\n{wikilink}\n\n' + new_content[cut:]
            else:
                new_content = _insert_before_timestamps(new_content, wikilink)
            inserted = True

    if not inserted:
        gm = re.search(r'^#{1,3}\s+Gerelateerd\s*$', new_content, re.IGNORECASE | re.MULTILINE)
        if gm:
            next_h = re.search(r'^#{1,3}\s', new_content[gm.end():], re.MULTILINE)
            if next_h:
                cut         = gm.end() + next_h.start()
                new_content = new_content[:cut].rstrip() + f'\n{wikilink}\n\n' + new_content[cut:]
            else:
                new_content = _insert_before_timestamps(new_content, wikilink)
        else:
            block       = f'## Gerelateerd\n{wikilink}'
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
    injected   = 0
    skipped    = 0
    errors     = 0
    dry_log: list[dict] = []

    prog = Progress(len(matrix), "Injecteren   ")

    for entry in matrix:
        source  = (entry.get('source') or '').strip()
        target  = (entry.get('target') or '').strip()
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

        target_clean = target.removesuffix('.md')
        wikilink     = f'[[{target_clean}]]'

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
