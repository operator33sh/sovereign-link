"""
Scraping tools for full-site knowledge extraction.

Provides four tools:
  crawl_site_structure   — BFS sitemap of a domain → JSON tree
  scrape_page_full       — full HTML → clean Markdown + metadata
  batch_extract_content  — process a URL list → .agent_temp/scraping_buffer/
  generate_knowledge_graph — scan Markdown → concepts.json (nodes + edges)

Dependencies: stdlib only (urllib, html.parser, re, json).
Falls back to browser.fetch_with_browser_fallback for JS-rendered pages.
"""

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from html.parser import HTMLParser

logger = logging.getLogger(__name__)

# ── project paths ──────────────────────────────────────────────────────────────
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AGENT_TEMP = os.path.join(_PROJECT_ROOT, ".agent_temp")

# Buffer lives inside the vault so read_vault() can access scraped files directly.
# Vault path is resolved at import time to avoid circular imports.
from tools.vault import VAULT_PATH as _VAULT_PATH  # noqa: E402
_BUFFER_DIR = os.path.join(_VAULT_PATH, "scraping_buffer")

# ── rate limiting ──────────────────────────────────────────────────────────────
_RATE_DELAY = 1.5   # seconds between requests (polite crawl)
_REQUEST_TIMEOUT = 20

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "nl,en;q=0.9",
}


# ── HTML utilities ─────────────────────────────────────────────────────────────

class _LinkExtractor(HTMLParser):
    """Collect all href links from an HTML document."""

    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs):
        if tag == "a":
            for k, v in attrs:
                if k == "href" and v:
                    abs_url = urllib.parse.urljoin(self.base_url, v.strip())
                    # Strip fragment
                    abs_url = abs_url.split("#")[0]
                    if abs_url:
                        self.links.append(abs_url)


class _MetaExtractor(HTMLParser):
    """Extract title, meta description, headings, body text, and links."""

    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.title = ""
        self.description = ""
        self.headings: list[tuple[int, str]] = []  # (level, text)
        self.links: list[str] = []
        self.body_text: list[str] = []

        self._in_title = False
        self._in_body_tag = False
        self._current_heading: int | None = None
        self._heading_buf: list[str] = []
        self._skip_tags = {"script", "style", "nav", "footer", "header", "aside"}
        self._skip_depth = 0
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs):
        attr_dict = dict(attrs)

        if tag == "title":
            self._in_title = True
            return

        if tag == "meta":
            name = (attr_dict.get("name") or "").lower()
            if name in ("description", "og:description") and attr_dict.get("content"):
                self.description = attr_dict["content"].strip()
            return

        if tag in self._skip_tags:
            self._skip_depth += 1
            return

        if tag == "a":
            href = attr_dict.get("href", "")
            if href:
                abs_url = urllib.parse.urljoin(self.base_url, href.strip()).split("#")[0]
                if abs_url:
                    self.links.append(abs_url)

        m = re.match(r"^h([1-6])$", tag)
        if m:
            self._current_heading = int(m.group(1))
            self._heading_buf = []

    def handle_endtag(self, tag: str):
        if tag == "title":
            self._in_title = False
            return

        if tag in self._skip_tags:
            self._skip_depth = max(0, self._skip_depth - 1)
            return

        m = re.match(r"^h([1-6])$", tag)
        if m and self._current_heading is not None:
            heading_text = " ".join(self._heading_buf).strip()
            if heading_text:
                self.headings.append((self._current_heading, heading_text))
            self._current_heading = None
            self._heading_buf = []

    def handle_data(self, data: str):
        if self._in_title:
            self.title += data
            return

        if self._skip_depth > 0:
            return

        if self._current_heading is not None:
            self._heading_buf.append(data)
            return

        text = data.strip()
        if text:
            self.body_text.append(text)


def _fetch_html(url: str) -> tuple[str, int]:
    """Fetch URL, return (html_text, status_code). Raises on failure."""
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
        charset = "utf-8"
        ct = resp.headers.get("Content-Type", "")
        m = re.search(r"charset=([^\s;]+)", ct)
        if m:
            charset = m.group(1)
        return resp.read().decode(charset, errors="replace"), resp.status


def _html_to_markdown(url: str, html: str) -> dict:
    """Parse HTML and return a metadata+markdown dict."""
    parser = _MetaExtractor(url)
    try:
        parser.feed(html)
    except Exception:
        pass

    title = parser.title.strip() or url
    lines: list[str] = []

    # YAML-style frontmatter
    lines.append("---")
    lines.append(f"title: \"{title}\"")
    lines.append(f"source: \"{url}\"")
    if parser.description:
        lines.append(f"description: \"{parser.description}\"")
    lines.append("---")
    lines.append("")

    # Headings as Markdown
    for level, text in parser.headings:
        lines.append("#" * level + " " + text)
        lines.append("")

    # Body paragraphs (deduplicate adjacent identical chunks)
    seen_para: set[str] = set()
    for chunk in parser.body_text:
        chunk = chunk.strip()
        if not chunk or chunk in seen_para:
            continue
        seen_para.add(chunk)
        lines.append(chunk)
        lines.append("")

    # Internal links section
    domain = urllib.parse.urlparse(url).netloc
    internal = sorted({
        lnk for lnk in parser.links
        if urllib.parse.urlparse(lnk).netloc == domain
    })
    if internal:
        lines.append("## Interne links")
        for lnk in internal[:30]:
            lines.append(f"- {lnk}")
        lines.append("")

    return {
        "url": url,
        "title": title,
        "description": parser.description,
        "headings": [{"level": lvl, "text": txt} for lvl, txt in parser.headings],
        "internal_links": internal,
        "markdown": "\n".join(lines),
    }


def _safe_filename(url: str) -> str:
    """Convert a URL to a safe filename stem."""
    parsed = urllib.parse.urlparse(url)
    path = (parsed.netloc + parsed.path).strip("/").replace("/", "_")
    path = re.sub(r"[^\w\-.]", "_", path)
    return (path or "index")[:120] + ".md"


# ── Tool 1: crawl_site_structure ───────────────────────────────────────────────

def crawl_site_structure(start_url: str, max_pages: int = 80) -> str:
    """
    BFS crawl of start_url's domain. Returns a JSON tree of discovered pages.
    Respects rate limiting and stays within the same domain.
    """
    parsed = urllib.parse.urlparse(start_url)
    if parsed.scheme not in ("http", "https"):
        return "Error: only HTTP/HTTPS URLs are allowed"

    domain = parsed.netloc
    visited: set[str] = set()
    queue: deque[tuple[str, int]] = deque()  # (url, depth)
    queue.append((start_url, 0))
    nodes: list[dict] = []
    errors: list[str] = []

    while queue and len(visited) < max_pages:
        url, depth = queue.popleft()
        # Normalise
        url = url.rstrip("/") or url
        if url in visited:
            continue
        visited.add(url)

        try:
            html, status = _fetch_html(url)
        except urllib.error.HTTPError as e:
            errors.append(f"{url} → HTTP {e.code}")
            continue
        except Exception as exc:
            errors.append(f"{url} → {exc}")
            continue

        # Extract title
        title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        title = title_m.group(1).strip() if title_m else url

        # Extract links
        extractor = _LinkExtractor(url)
        try:
            extractor.feed(html)
        except Exception:
            pass

        child_urls = [
            lnk for lnk in extractor.links
            if urllib.parse.urlparse(lnk).netloc == domain
            and lnk not in visited
            and not re.search(r"\.(pdf|jpg|jpeg|png|gif|svg|ico|css|js|zip|xml)$", lnk, re.I)
        ]

        nodes.append({
            "url": url,
            "title": title,
            "depth": depth,
            "status": status,
            "children": child_urls[:50],
        })

        for child in child_urls:
            if child not in visited:
                queue.append((child, depth + 1))

        time.sleep(_RATE_DELAY)

    result = {
        "domain": domain,
        "start_url": start_url,
        "pages_found": len(nodes),
        "errors": errors,
        "tree": nodes,
    }

    # Save to temp
    out_path = os.path.join(_AGENT_TEMP, "site_structure.json")
    os.makedirs(_AGENT_TEMP, exist_ok=True)
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        return f"Error writing site_structure.json: {exc}"

    return json.dumps({
        "status": "ok",
        "pages_found": len(nodes),
        "errors_count": len(errors),
        "saved_to": ".agent_temp/site_structure.json",
        "summary": [{"url": n["url"], "title": n["title"], "depth": n["depth"]} for n in nodes],
    }, ensure_ascii=False, indent=2)


# ── Tool 2: scrape_page_full ───────────────────────────────────────────────────

def scrape_page_full(url: str) -> str:
    """
    Fetch a single page and return full metadata + Markdown content.
    Falls back to browser (Playwright) for JS-rendered pages when available.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "Error: only HTTP/HTTPS URLs are allowed"

    html = ""
    used_browser = False

    try:
        html, _ = _fetch_html(url)
        # Heuristic: if the page has very little body text, try browser fallback
        text_content = re.sub(r"<[^>]+>", " ", html)
        if len(text_content.strip()) < 500:
            raise ValueError("Page appears to have insufficient text — trying browser")
    except Exception as primary_exc:
        logger.info("scrape_page_full: primary fetch failed (%s), trying browser", primary_exc)
        try:
            from browser import fetch_with_browser_fallback
            html = fetch_with_browser_fallback(url)
            used_browser = True
        except Exception as browser_exc:
            if not html:
                return f"Error: failed to fetch '{url}' — {primary_exc}; browser: {browser_exc}"

    data = _html_to_markdown(url, html)
    data["used_browser"] = used_browser

    # Save to buffer
    os.makedirs(_BUFFER_DIR, exist_ok=True)
    fname = _safe_filename(url)
    file_path = os.path.join(_BUFFER_DIR, fname)
    logger.info("scrape_page_full: writing to %s", file_path)
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(data["markdown"])
            f.flush()
            os.fsync(f.fileno())
    except Exception as exc:
        logger.error("scrape_page_full: write failed for %s — %s", file_path, exc)
        return f"Error writing buffer file: {exc}"

    # Atomic validation: confirm file exists and is non-empty
    if not os.path.exists(file_path):
        msg = f"WRITE FAILED: file not found on disk after write — {file_path}"
        logger.error("scrape_page_full: %s", msg)
        return f"Error: {msg}"
    actual_size = os.path.getsize(file_path)
    if actual_size == 0:
        msg = f"WRITE FAILED: file is empty on disk — {file_path}"
        logger.error("scrape_page_full: %s", msg)
        return f"Error: {msg}"

    logger.info("scrape_page_full: confirmed %s (%d bytes)", file_path, actual_size)

    return json.dumps({
        "status": "ok",
        "url": url,
        "title": data["title"],
        "description": data["description"],
        "headings_count": len(data["headings"]),
        "internal_links_count": len(data["internal_links"]),
        "used_browser": used_browser,
        "saved_to": f"scraping_buffer/{fname}",
        "bytes_written": actual_size,
        "markdown_preview": data["markdown"][:800],
    }, ensure_ascii=False, indent=2)


# ── Tool 3: batch_extract_content ─────────────────────────────────────────────

def batch_extract_content(url_list_json: str) -> str:
    """
    Process a JSON array of URLs. Saves each as Markdown to
    .agent_temp/scraping_buffer/. Returns a summary with per-URL status.
    """
    try:
        url_list = json.loads(url_list_json)
    except json.JSONDecodeError as exc:
        return f"Error: invalid JSON — {exc}"

    if not isinstance(url_list, list):
        return "Error: expected a JSON array of URL strings"

    os.makedirs(_BUFFER_DIR, exist_ok=True)

    results: list[dict] = []
    for i, url in enumerate(url_list):
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            results.append({"url": url, "status": "skipped", "reason": "invalid URL"})
            continue

        logger.info("batch_extract_content [%d/%d]: %s", i + 1, len(url_list), url)

        try:
            html, status = _fetch_html(url)
        except urllib.error.HTTPError as exc:
            results.append({"url": url, "status": "error", "reason": f"HTTP {exc.code}"})
            time.sleep(_RATE_DELAY)
            continue
        except Exception as exc:
            # Browser fallback
            try:
                from browser import fetch_with_browser_fallback
                html = fetch_with_browser_fallback(url)
                status = 200
            except Exception as b_exc:
                results.append({"url": url, "status": "error", "reason": str(b_exc)})
                time.sleep(_RATE_DELAY)
                continue

        data = _html_to_markdown(url, html)
        fname = _safe_filename(url)
        file_path = os.path.join(_BUFFER_DIR, fname)

        logger.info("batch_extract_content [%d/%d]: writing to %s", i + 1, len(url_list), file_path)
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(data["markdown"])
                f.flush()
                os.fsync(f.fileno())
        except Exception as exc:
            logger.error("batch_extract_content: write failed for %s — %s", file_path, exc)
            results.append({"url": url, "status": "error", "reason": f"write failed: {exc}"})
            time.sleep(_RATE_DELAY)
            continue

        # Atomic validation: confirm file exists and is non-empty before reporting ok
        if not os.path.exists(file_path):
            reason = f"file not found on disk after write: {file_path}"
            logger.error("batch_extract_content: %s", reason)
            results.append({"url": url, "status": "error", "reason": reason})
            time.sleep(_RATE_DELAY)
            continue

        actual_size = os.path.getsize(file_path)
        if actual_size == 0:
            reason = f"file is empty on disk after write: {file_path}"
            logger.error("batch_extract_content: %s", reason)
            results.append({"url": url, "status": "error", "reason": reason})
            time.sleep(_RATE_DELAY)
            continue

        logger.info("batch_extract_content: confirmed %s (%d bytes)", fname, actual_size)
        results.append({
            "url": url,
            "status": "ok",
            "title": data["title"],
            "file": f"scraping_buffer/{fname}",
            "bytes_written": actual_size,
        })

        time.sleep(_RATE_DELAY)

    ok_count = sum(1 for r in results if r["status"] == "ok")
    err_count = sum(1 for r in results if r["status"] == "error")

    logger.info("batch_extract_content: finished — %d ok, %d errors out of %d", ok_count, err_count, len(url_list))
    return json.dumps({
        "status": "done",
        "total": len(url_list),
        "ok": ok_count,
        "errors": err_count,
        "buffer_dir": "scraping_buffer/",
        "buffer_dir_absolute": _BUFFER_DIR,
        "results": results,
    }, ensure_ascii=False, indent=2)


# ── Tool 4: generate_knowledge_graph ──────────────────────────────────────────

# Dutch + English stopwords for concept extraction
_STOPWORDS = frozenset({
    "de", "het", "een", "en", "van", "in", "te", "dat", "is", "op", "voor",
    "met", "als", "zijn", "ook", "aan", "er", "maar", "om", "dan", "dit",
    "niet", "door", "die", "ze", "hoe", "wat", "we", "of", "naar",
    "the", "a", "an", "and", "of", "in", "to", "is", "are", "that", "this",
    "it", "for", "on", "with", "as", "be", "by", "or", "from", "at", "not",
    "have", "was", "but", "they", "their", "which", "also", "can", "more",
    "its", "been", "has", "will", "when", "how", "all", "each", "both",
})

_MIN_WORD_LEN = 4
_MIN_FREQUENCY = 2


def generate_knowledge_graph(data_source: str = "") -> str:
    """
    Scan Markdown files in data_source (defaults to .agent_temp/scraping_buffer/)
    and generate a concepts.json with nodes (concepts) and edges (co-occurrences).
    """
    scan_dir = data_source.strip() if data_source.strip() else _BUFFER_DIR
    # Resolve to absolute if relative
    if not os.path.isabs(scan_dir):
        scan_dir = os.path.join(_PROJECT_ROOT, scan_dir)

    if not os.path.isdir(scan_dir):
        return f"Error: directory not found — '{scan_dir}'"

    # Gather all markdown files
    md_files: list[str] = []
    for fname in os.listdir(scan_dir):
        if fname.endswith(".md"):
            md_files.append(os.path.join(scan_dir, fname))

    if not md_files:
        return f"Error: no .md files found in '{scan_dir}'"

    # Word frequency map and co-occurrence map
    word_freq: dict[str, int] = {}
    # co_occur[w1][w2] = count of files where both appear
    co_occur: dict[str, dict[str, int]] = {}
    # page_words: per-file word sets for edge building
    file_word_sets: list[tuple[str, set[str]]] = []

    _word_re = re.compile(r"\b([A-Za-zÀ-ÿ]{" + str(_MIN_WORD_LEN) + r",})\b")
    _frontmatter_re = re.compile(r"^---.*?---\s*", re.DOTALL)

    for fpath in md_files:
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                raw = f.read()
        except Exception:
            continue

        # Strip frontmatter and markdown syntax
        text = _frontmatter_re.sub("", raw)
        text = re.sub(r"#+\s", " ", text)
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)  # links → text
        text = re.sub(r"[`*_~|]", " ", text)

        words = {
            w.lower() for w in _word_re.findall(text)
            if w.lower() not in _STOPWORDS
        }

        for w in words:
            word_freq[w] = word_freq.get(w, 0) + 1

        file_word_sets.append((os.path.basename(fpath), words))

    # Filter to concepts meeting minimum frequency
    concepts = {w for w, freq in word_freq.items() if freq >= _MIN_FREQUENCY}

    # Build co-occurrence edges from file-level word sets
    for _fname, word_set in file_word_sets:
        page_concepts = sorted(concepts & word_set)
        for i, w1 in enumerate(page_concepts):
            for w2 in page_concepts[i + 1:]:
                if w1 not in co_occur:
                    co_occur[w1] = {}
                co_occur[w1][w2] = co_occur[w1].get(w2, 0) + 1

    # Build nodes
    nodes = [
        {"id": w, "label": w, "frequency": word_freq[w]}
        for w in sorted(concepts, key=lambda x: -word_freq[x])
    ]

    # Build edges (co-occurrence ≥ 2 to reduce noise)
    edges = []
    for w1, targets in co_occur.items():
        for w2, weight in targets.items():
            if weight >= 2:
                edges.append({"source": w1, "target": w2, "weight": weight})

    edges.sort(key=lambda e: -e["weight"])

    graph = {
        "meta": {
            "files_scanned": len(md_files),
            "concepts_found": len(nodes),
            "edges_found": len(edges),
            "min_frequency": _MIN_FREQUENCY,
            "source_dir": scan_dir,
        },
        "nodes": nodes,
        "edges": edges,
    }

    # Save to .agent_temp/concepts.json
    out_path = os.path.join(_AGENT_TEMP, "concepts.json")
    os.makedirs(_AGENT_TEMP, exist_ok=True)
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(graph, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        return f"Error writing concepts.json: {exc}"

    return json.dumps({
        "status": "ok",
        "files_scanned": len(md_files),
        "concepts_found": len(nodes),
        "edges_found": len(edges),
        "saved_to": ".agent_temp/concepts.json",
        "top_concepts": [n["id"] for n in nodes[:20]],
    }, ensure_ascii=False, indent=2)


# ── Tool registry ──────────────────────────────────────────────────────────────

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "crawl_site_structure",
            "description": (
                "Crawl an entire website domain using BFS and generate a structured sitemap. "
                "Returns a JSON tree of all discovered pages with titles, depths, and child URLs. "
                "Stays within the same domain as start_url. "
                "Result saved to .agent_temp/site_structure.json. "
                "Use this as the first step before batch_extract_content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_url": {
                        "type": "string",
                        "description": "The starting URL to crawl (e.g. 'https://fractalisme.nl').",
                    },
                    "max_pages": {
                        "type": "integer",
                        "description": "Maximum number of pages to crawl (default: 80, max recommended: 200).",
                    },
                },
                "required": ["start_url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scrape_page_full",
            "description": (
                "Fetch a single webpage and extract full metadata, headings, body text, and "
                "internal links. Returns clean Markdown with YAML frontmatter. "
                "Automatically falls back to a headless browser (Playwright) for JS-rendered pages. "
                "Result saved to .agent_temp/scraping_buffer/<filename>.md and compatible with write_vault."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The full URL of the page to scrape.",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "batch_extract_content",
            "description": (
                "Process a list of URLs in sequence, scraping each page and saving Markdown files "
                "to .agent_temp/scraping_buffer/. Includes built-in rate limiting to avoid blocking. "
                "Use after crawl_site_structure to extract content from discovered pages. "
                "Pass the URL list from site_structure.json's tree[].url field."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url_list_json": {
                        "type": "string",
                        "description": "A JSON array of URL strings to process, e.g. '[\"https://fractalisme.nl/page1\", ...]'.",
                    },
                },
                "required": ["url_list_json"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_knowledge_graph",
            "description": (
                "Scan scraped Markdown files and extract recurring concepts to build a knowledge graph. "
                "Generates concepts.json with Nodes (concepts + frequency) and Edges (co-occurrence weight). "
                "Saved to .agent_temp/concepts.json — ready for MCP server integration or vault storage. "
                "Run this after batch_extract_content has populated the scraping buffer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "data_source": {
                        "type": "string",
                        "description": (
                            "Directory of .md files to scan. "
                            "Defaults to .agent_temp/scraping_buffer/ if omitted."
                        ),
                    },
                },
                "required": [],
            },
        },
    },
]

HANDLERS = {
    "crawl_site_structure": lambda args: crawl_site_structure(
        args["start_url"], args.get("max_pages", 80)
    ),
    "scrape_page_full": lambda args: scrape_page_full(args["url"]),
    "batch_extract_content": lambda args: batch_extract_content(args["url_list_json"]),
    "generate_knowledge_graph": lambda args: generate_knowledge_graph(
        args.get("data_source", "")
    ),
}
