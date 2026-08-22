"""
timeline.py — SQLite Timeline Index (Sovereign Memory Engine)

Parallel index alongside ChromaDB for reliable temporal queries.
ChromaDB handles semantic/topic search; SQLite handles date/time search.

Schema:
  entries       — one row per vault file (upsert on file_path)
  entries_fts   — FTS5 virtual table for full-text search over content

DB path: ~/.sovereign-link/timeline.db
Indexed dirs: Conversaties/, memory/ (skip .agent_temp/, .system/, system_memory/)
"""

import logging
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

TIMELINE_DB = os.environ.get(
    "TIMELINE_DB",
    os.path.expanduser("~/.sovereign-link/timeline.db"),
)

# Directories to index (relative vault paths must start with one of these)
_INDEXED_PREFIXES = ("Conversaties/", "memory/")

# Directories to always skip
_SKIP_PREFIXES = (".agent_temp/", ".system/", "system_memory/")

# Regex to extract session_id from filenames like Sessie_20260823_143000
_SESSION_RE = re.compile(r"Sessie_(\d{8}_\d{6})")

# Regex to extract YYYY-MM-DD from file path or content timestamp tags
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_TIME_RE = re.compile(r"#(\d{2})\s+#(\d{2})\b")  # #HH #MM tags


def _connect() -> sqlite3.Connection:
    Path(TIMELINE_DB).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(TIMELINE_DB, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS entries (
            id           INTEGER PRIMARY KEY,
            file_path    TEXT NOT NULL,
            date         TEXT NOT NULL,
            time         TEXT NOT NULL,
            datetime_iso TEXT NOT NULL,
            session_id   TEXT,
            content      TEXT NOT NULL,
            indexed_at   TEXT NOT NULL,
            UNIQUE(file_path)
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
            content,
            content='entries',
            content_rowid='id'
        );

        CREATE TRIGGER IF NOT EXISTS entries_ai AFTER INSERT ON entries BEGIN
            INSERT INTO entries_fts(rowid, content) VALUES (new.id, new.content);
        END;

        CREATE TRIGGER IF NOT EXISTS entries_ad AFTER DELETE ON entries BEGIN
            INSERT INTO entries_fts(entries_fts, rowid, content)
            VALUES ('delete', old.id, old.content);
        END;

        CREATE TRIGGER IF NOT EXISTS entries_au AFTER UPDATE ON entries BEGIN
            INSERT INTO entries_fts(entries_fts, rowid, content)
            VALUES ('delete', old.id, old.content);
            INSERT INTO entries_fts(rowid, content) VALUES (new.id, new.content);
        END;
    """)
    conn.commit()


def _should_index(file_path: str) -> bool:
    """Return True if this vault file belongs in the timeline index."""
    for skip in _SKIP_PREFIXES:
        if file_path.startswith(skip):
            return False
    for prefix in _INDEXED_PREFIXES:
        if file_path.startswith(prefix):
            return True
    return False


def _extract_datetime(file_path: str, content: str, timestamp: str) -> tuple[str, str, str]:
    """Extract (date, time, datetime_iso) from path/content/timestamp.

    Priority:
    1. Date from filename (YYYY-MM-DD pattern)
    2. Date from #YYYY-MM-DD tag in content
    3. Date from timestamp argument
    """
    # Try filename first
    path_date = _DATE_RE.search(Path(file_path).stem)
    if path_date:
        date_str = path_date.group(1)
    else:
        # Try content tags
        content_date = _DATE_RE.search(content[:500])
        if content_date:
            date_str = content_date.group(1)
        else:
            # Fall back to timestamp argument
            date_str = timestamp[:10] if len(timestamp) >= 10 else datetime.now().strftime("%Y-%m-%d")

    # Try to extract time from #HH #MM tags in content
    time_match = _TIME_RE.search(content[:500])
    if time_match:
        time_str = f"{time_match.group(1)}:{time_match.group(2)}"
    else:
        # Fall back to timestamp
        if len(timestamp) >= 16:
            time_str = timestamp[11:16]
        else:
            time_str = "00:00"

    datetime_iso = f"{date_str}T{time_str}:00"
    return date_str, time_str, datetime_iso


def _extract_session_id(file_path: str) -> str | None:
    m = _SESSION_RE.search(file_path)
    return m.group(1) if m else None


# Module-level connection (lazy, thread-safe via SQLite WAL)
_conn: sqlite3.Connection | None = None
_conn_lock = __import__("threading").Lock()


def _get_conn() -> sqlite3.Connection:
    global _conn
    with _conn_lock:
        if _conn is None:
            _conn = _connect()
        return _conn


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def index_file(file_path: str, content: str, timestamp: str) -> None:
    """Upsert a vault file into the SQLite timeline index.

    Silently skips files outside the indexed directories.
    Safe to call from write_vault() — exceptions are caught and logged.
    """
    if not _should_index(file_path):
        return

    try:
        date_str, time_str, datetime_iso = _extract_datetime(file_path, content, timestamp)
        session_id = _extract_session_id(file_path)
        indexed_at = datetime.now().isoformat()

        conn = _get_conn()
        conn.execute(
            """
            INSERT INTO entries (file_path, date, time, datetime_iso, session_id, content, indexed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(file_path) DO UPDATE SET
                date=excluded.date,
                time=excluded.time,
                datetime_iso=excluded.datetime_iso,
                session_id=excluded.session_id,
                content=excluded.content,
                indexed_at=excluded.indexed_at
            """,
            (file_path, date_str, time_str, datetime_iso, session_id, content, indexed_at),
        )
        conn.commit()
        logger.debug("timeline: indexed %s (%s %s)", file_path, date_str, time_str)
    except Exception:
        logger.exception("timeline: failed to index %s", file_path)


def search_by_date(date: str, query: str | None = None, n: int = 5) -> list[dict]:
    """All entries on a specific date, optionally filtered by FTS query.

    Args:
        date:  YYYY-MM-DD
        query: Optional full-text search string to rank/filter results
        n:     Max number of results

    Returns list of dicts with keys: file_path, date, time, datetime_iso, session_id, snippet
    """
    conn = _get_conn()
    try:
        if query:
            # FTS + date filter: join entries_fts score with date filter
            rows = conn.execute(
                """
                SELECT e.file_path, e.date, e.time, e.datetime_iso, e.session_id,
                       snippet(entries_fts, 0, '[', ']', '...', 20) AS snippet
                FROM entries_fts
                JOIN entries e ON entries_fts.rowid = e.id
                WHERE entries_fts MATCH ?
                  AND e.date = ?
                ORDER BY rank
                LIMIT ?
                """,
                (query, date, n),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT file_path, date, time, datetime_iso, session_id,
                       substr(content, 1, 300) AS snippet
                FROM entries
                WHERE date = ?
                ORDER BY datetime_iso
                LIMIT ?
                """,
                (date, n),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        logger.exception("timeline: search_by_date failed (date=%s)", date)
        return []


def search_by_range(
    date_from: str, date_to: str, query: str | None = None, n: int = 10
) -> list[dict]:
    """Entries within a date range [date_from, date_to] (inclusive).

    Args:
        date_from: YYYY-MM-DD (start, inclusive)
        date_to:   YYYY-MM-DD (end, inclusive)
        query:     Optional FTS filter
        n:         Max results

    Returns list of dicts with file_path, date, time, datetime_iso, session_id, snippet.
    """
    conn = _get_conn()
    try:
        if query:
            rows = conn.execute(
                """
                SELECT e.file_path, e.date, e.time, e.datetime_iso, e.session_id,
                       snippet(entries_fts, 0, '[', ']', '...', 20) AS snippet
                FROM entries_fts
                JOIN entries e ON entries_fts.rowid = e.id
                WHERE entries_fts MATCH ?
                  AND e.date BETWEEN ? AND ?
                ORDER BY rank
                LIMIT ?
                """,
                (query, date_from, date_to, n),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT file_path, date, time, datetime_iso, session_id,
                       substr(content, 1, 300) AS snippet
                FROM entries
                WHERE date BETWEEN ? AND ?
                ORDER BY datetime_iso
                LIMIT ?
                """,
                (date_from, date_to, n),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        logger.exception("timeline: search_by_range failed (%s – %s)", date_from, date_to)
        return []


def search_by_session(session_id: str) -> list[dict]:
    """All snapshots belonging to a specific session_id.

    Returns list of dicts with file_path, date, time, datetime_iso, snippet.
    """
    conn = _get_conn()
    try:
        rows = conn.execute(
            """
            SELECT file_path, date, time, datetime_iso, session_id,
                   substr(content, 1, 300) AS snippet
            FROM entries
            WHERE session_id = ?
            ORDER BY datetime_iso
            """,
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        logger.exception("timeline: search_by_session failed (session=%s)", session_id)
        return []


def rescan_vault(vault_path: str) -> int:
    """Re-index all eligible .md files in vault_path.

    Returns the number of files indexed.
    Skips empty files and files outside the indexed directories.
    """
    vault = Path(vault_path)
    if not vault.exists():
        logger.error("timeline.rescan_vault: vault not found at %s", vault_path)
        return 0

    count = 0
    for md_file in sorted(vault.rglob("*.md")):
        try:
            rel = str(md_file.relative_to(vault))
            if not _should_index(rel):
                continue
            content = md_file.read_text(encoding="utf-8")
            if not content.strip():
                continue
            mtime = datetime.fromtimestamp(md_file.stat().st_mtime).isoformat()
            index_file(rel, content, mtime)
            count += 1
        except Exception:
            logger.exception("timeline.rescan_vault: failed for %s", md_file)

    logger.info("timeline.rescan_vault: indexed %d files from %s", count, vault_path)
    return count
