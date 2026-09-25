"""Document ingestion tools: read .txt and parse .pdf into Markdown."""
import logging
import os

logger = logging.getLogger(__name__)

# Max characters before chunking kicks in (≈200 kB of text)
_MAX_CHARS = 200_000
# Max chars per chunk when splitting large documents
_CHUNK_SIZE = 50_000


def read_document(file_path: str) -> str:
    """Read a .txt file from an absolute path and return its content."""
    if not os.path.isabs(file_path):
        return "Error: file_path must be an absolute path."
    if not os.path.isfile(file_path):
        return f"Error: file not found: {file_path!r}"
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:
        return f"Error reading file: {e}"

    if len(content) > _MAX_CHARS:
        chunks = _split_text(content)
        header = (
            f"[Document too large — split into {len(chunks)} chunks of ~{_CHUNK_SIZE} chars each. "
            f"Showing chunk 1/{len(chunks)}. "
            f"Call read_document_chunk to retrieve subsequent chunks.]\n\n"
        )
        # Cache chunks in a predictable location so subsequent calls can retrieve them
        _cache_chunks(file_path, chunks)
        return header + chunks[0]

    return content


def read_document_chunk(file_path: str, chunk_index: int) -> str:
    """Return a specific chunk (0-based) of a previously chunked document."""
    cache_path = _chunk_cache_path(file_path)
    if not os.path.isfile(cache_path):
        return f"Error: no chunk cache found for {file_path!r}. Call read_document first."
    try:
        import json
        with open(cache_path, "r", encoding="utf-8") as f:
            chunks = json.load(f)
    except Exception as e:
        return f"Error reading chunk cache: {e}"
    if chunk_index < 0 or chunk_index >= len(chunks):
        return f"Error: chunk_index {chunk_index} out of range (0–{len(chunks) - 1})."
    return f"[Chunk {chunk_index + 1}/{len(chunks)}]\n\n{chunks[chunk_index]}"


def parse_pdf(file_path: str) -> str:
    """
    Extract text from a PDF file and return it as clean Markdown.

    Handles:
    - Encrypted PDFs (reports the issue clearly)
    - Corrupted or unreadable files
    - Large documents (chunked, first chunk returned; use parse_pdf_chunk for the rest)
    """
    if not os.path.isabs(file_path):
        return "Error: file_path must be an absolute path."
    if not os.path.isfile(file_path):
        return f"Error: file not found: {file_path!r}"

    try:
        import pdfplumber
    except ImportError:
        return "Error: pdfplumber is not installed. Run: pip install pdfplumber"

    try:
        with pdfplumber.open(file_path) as pdf:
            if pdf.metadata.get("Encrypted") or _is_encrypted(pdf):
                return (
                    f"Error: '{os.path.basename(file_path)}' is encrypted. "
                    "Provide the decrypted file or a password-free export."
                )

            pages_md: list[str] = []
            for i, page in enumerate(pdf.pages, start=1):
                try:
                    text = page.extract_text() or ""
                except Exception as e:
                    text = f"[Page {i} unreadable: {e}]"
                pages_md.append(_page_to_markdown(i, text))

    except Exception as e:
        err = str(e).lower()
        if "encrypt" in err or "password" in err:
            return (
                f"Error: '{os.path.basename(file_path)}' appears to be encrypted or password-protected."
            )
        return f"Error parsing PDF: {e}"

    full_text = "\n\n".join(pages_md)
    filename_stem = os.path.splitext(os.path.basename(file_path))[0]
    document_md = f"# {filename_stem}\n\n{full_text}"

    if len(document_md) > _MAX_CHARS:
        chunks = _split_text(document_md)
        _cache_chunks(file_path, chunks)
        header = (
            f"[PDF too large — split into {len(chunks)} chunks of ~{_CHUNK_SIZE} chars each. "
            f"Showing chunk 1/{len(chunks)}. "
            f"Call parse_pdf_chunk to retrieve subsequent chunks.]\n\n"
        )
        return header + chunks[0]

    return document_md


def parse_pdf_chunk(file_path: str, chunk_index: int) -> str:
    """Return a specific chunk (0-based) of a previously parsed large PDF."""
    cache_path = _chunk_cache_path(file_path)
    if not os.path.isfile(cache_path):
        return f"Error: no chunk cache found for {file_path!r}. Call parse_pdf first."
    try:
        import json
        with open(cache_path, "r", encoding="utf-8") as f:
            chunks = json.load(f)
    except Exception as e:
        return f"Error reading chunk cache: {e}"
    if chunk_index < 0 or chunk_index >= len(chunks):
        return f"Error: chunk_index {chunk_index} out of range (0–{len(chunks) - 1})."
    return f"[Chunk {chunk_index + 1}/{len(chunks)}]\n\n{chunks[chunk_index]}"


# ── Internal helpers ──────────────────────────────────────────────────────────

def _is_encrypted(pdf) -> bool:
    """Best-effort encryption check beyond metadata."""
    try:
        # pdfplumber wraps pdfminer; if the first page raises, it's likely encrypted
        _ = pdf.pages[0].extract_text()
        return False
    except Exception as e:
        return "encrypt" in str(e).lower() or "password" in str(e).lower()


def _page_to_markdown(page_num: int, text: str) -> str:
    """Convert a single page's raw text to minimal Markdown."""
    if not text.strip():
        return f"*[Page {page_num} — no extractable text]*"
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append("")
            continue
        lines.append(stripped)
    return "\n".join(lines)


def _split_text(text: str) -> list[str]:
    chunks = []
    start = 0
    while start < len(text):
        end = start + _CHUNK_SIZE
        # Try to break on a newline boundary for cleaner splits
        if end < len(text):
            nl = text.rfind("\n", start, end)
            if nl > start:
                end = nl + 1
        chunks.append(text[start:end])
        start = end
    return chunks


def _chunk_cache_path(file_path: str) -> str:
    import hashlib
    h = hashlib.md5(file_path.encode()).hexdigest()[:12]
    cache_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".agent_temp", "doc_chunks")
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, f"{h}.json")


def _cache_chunks(file_path: str, chunks: list[str]) -> None:
    import json
    cache_path = _chunk_cache_path(file_path)
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(chunks, f, ensure_ascii=False)
    except Exception as e:
        logger.warning("document_tools: failed to cache chunks for %s: %s", file_path, e)


# ── Tool registry ─────────────────────────────────────────────────────────────

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "read_document",
            "description": (
                "Read the full text content of a .txt file from an absolute file path. "
                "For large files (>200 kB) only the first chunk is returned; "
                "use read_document_chunk to retrieve subsequent chunks."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the .txt file.",
                    }
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_document_chunk",
            "description": (
                "Retrieve a specific chunk (0-based index) of a large .txt file "
                "that was previously read with read_document."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the original .txt file.",
                    },
                    "chunk_index": {
                        "type": "integer",
                        "description": "Zero-based chunk index.",
                    },
                },
                "required": ["file_path", "chunk_index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "parse_pdf",
            "description": (
                "Extract text from a PDF file at the given absolute path and return it "
                "as clean Markdown. Handles encrypted PDFs and corrupted files gracefully. "
                "For large PDFs only the first chunk is returned; "
                "use parse_pdf_chunk to retrieve subsequent chunks."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the .pdf file.",
                    }
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "parse_pdf_chunk",
            "description": (
                "Retrieve a specific chunk (0-based index) of a large PDF "
                "that was previously parsed with parse_pdf."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the original .pdf file.",
                    },
                    "chunk_index": {
                        "type": "integer",
                        "description": "Zero-based chunk index.",
                    },
                },
                "required": ["file_path", "chunk_index"],
            },
        },
    },
]

HANDLERS = {
    "read_document": lambda args: read_document(args["file_path"]),
    "read_document_chunk": lambda args: read_document_chunk(args["file_path"], int(args["chunk_index"])),
    "parse_pdf": lambda args: parse_pdf(args["file_path"]),
    "parse_pdf_chunk": lambda args: parse_pdf_chunk(args["file_path"], int(args["chunk_index"])),
}
