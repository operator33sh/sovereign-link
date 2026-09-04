"""MOC (Map of Content) clustering helpers — shared by tools/ and link_injector/."""
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

VAULT_PATH = os.environ.get("VAULT_PATH", "/home/wouter/Documents/fractalisme-vault")
_MOC_DIR = os.environ.get("MOC_DIR", "MOCs")
_MOC_CLUSTERS_PATH = os.path.join(VAULT_PATH, ".agent_temp", "moc_clusters.json")
_TS_PAT = re.compile(r'#\d{4}-\d{2}-\d{2}(?:\s+#\d{2})+\s*$', re.MULTILINE)
_TAG_PAT = re.compile(r'(?<!\d)#([A-Za-zÀ-ÿ]\w*)')
_MOC_LINK_RE = re.compile(r'\[\[MOC_')

_moc_clusters_cache: list[dict] | None = None
_moc_clusters_mtime: float = 0.0


def _load_moc_clusters() -> list[dict]:
    global _moc_clusters_cache, _moc_clusters_mtime
    try:
        mtime = os.path.getmtime(_MOC_CLUSTERS_PATH)
        if _moc_clusters_cache is not None and mtime == _moc_clusters_mtime:
            return _moc_clusters_cache
        with open(_MOC_CLUSTERS_PATH, 'r', encoding='utf-8') as f:
            _moc_clusters_cache = json.load(f)
        _moc_clusters_mtime = mtime
        return _moc_clusters_cache
    except Exception:
        return []


def _find_best_moc(content: str, file_name: str) -> str | None:
    clusters = _load_moc_clusters()
    if not clusters:
        return None

    note_tags = set(t.lower() for t in _TAG_PAT.findall(content))
    note_words = set(os.path.splitext(os.path.basename(file_name))[0].lower().split())

    best_score = -1.0
    best_moc = None
    for c in clusters:
        ctags = set(t.lower().lstrip('#') for t in c.get('cluster_tags', []))
        if not ctags:
            ctags = set(c.get('moc_title', '').lower().split())
            ctags.update(c.get('moc_name', '').lower().replace('moc_', '').split('_'))
        union = note_tags | ctags
        tag_score = len(note_tags & ctags) / len(union) if union else 0.0
        ckw = set(c.get('moc_title', '').lower().split())
        ckw.update(c.get('moc_name', '').lower().replace('moc_', '').split('_'))
        kw_score = len(note_words & ckw) / max(len(ckw), 1)
        score = tag_score * 2 + kw_score
        if score > best_score:
            best_score = score
            best_moc = c.get('moc_name')

    return best_moc if best_moc and best_moc.startswith('MOC_') else None


def _insert_moc_link(content: str, moc_wikilink: str) -> str:
    gm = re.search(r'^#{1,3}\s+Gerelateerd\s*$', content, re.IGNORECASE | re.MULTILINE)
    if gm:
        next_h = re.search(r'^#{1,3}\s', content[gm.end():], re.MULTILINE)
        if next_h:
            cut = gm.end() + next_h.start()
            return content[:cut].rstrip() + f'\n{moc_wikilink}\n\n' + content[cut:]
        m = _TS_PAT.search(content)
        if m:
            return content[:m.start()].rstrip() + f'\n{moc_wikilink}\n\n' + content[m.start():]
        return content.rstrip() + f'\n{moc_wikilink}\n'

    block = f'## Gerelateerd\n{moc_wikilink}'
    m = _TS_PAT.search(content)
    if m:
        return content[:m.start()].rstrip() + f'\n{block}\n\n' + content[m.start():]
    return content.rstrip() + f'\n{block}\n'


def _update_moc_spoke(moc_name: str, note_file: str) -> None:
    moc_path = os.path.join(VAULT_PATH, _MOC_DIR, f"{moc_name}.md")
    note_link = f'- [[{os.path.splitext(os.path.basename(note_file))[0]}]]'
    try:
        if not os.path.isfile(moc_path):
            return
        with open(moc_path, 'r', encoding='utf-8') as f:
            moc_content = f.read()
        if note_link in moc_content:
            return
        section_re = re.compile(r'(### 💎 Kernconcepten\n)(.*?)(?=\n## |\n#[^#]|\Z)', re.DOTALL)
        sm = section_re.search(moc_content)
        if sm:
            new_block = sm.group(2).rstrip() + f'\n{note_link}\n'
            updated = moc_content[:sm.start(2)] + new_block + moc_content[sm.end(2):]
        else:
            m = _TS_PAT.search(moc_content)
            block = f'### 💎 Kernconcepten\n\n{note_link}\n\n'
            if m:
                updated = moc_content[:m.start()].rstrip() + f'\n{block}' + moc_content[m.start():]
            else:
                updated = moc_content.rstrip() + f'\n{block}'
        with open(moc_path, 'w', encoding='utf-8') as f:
            f.write(updated)
        logger.info("write_vault: MOC spoke toegevoegd %s → %s", note_file, moc_name)
    except Exception:
        logger.exception("write_vault: MOC spoke update mislukt voor %s", moc_name)


def _auto_link_to_moc(file_name: str, content: str) -> str:
    basename = os.path.basename(file_name)
    if basename.startswith('MOC_') or file_name.startswith('.agent_temp'):
        return content
    if _MOC_LINK_RE.search(content):
        return content
    moc_name = _find_best_moc(content, file_name)
    if not moc_name:
        return content
    moc_wikilink = f'[[{moc_name}]]'
    new_content = _insert_moc_link(content, moc_wikilink)
    _update_moc_spoke(moc_name, file_name)
    logger.info("write_vault: auto MOC-link → %s in %s", moc_name, file_name)
    return new_content
