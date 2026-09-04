"""Search tools: semantic vault search and timeline search."""


def search_vault_semantic(query: str) -> str:
    from vector import search_vault_semantic as _search
    return _search(query)


def search_timeline(
    query: str | None = None,
    date: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    n: int = 5,
) -> str:
    from timeline import search_by_date, search_by_range

    if date_from and date_to:
        rows = search_by_range(date_from, date_to, query=query, n=n)
        label = f"{date_from} – {date_to}"
    elif date:
        rows = search_by_date(date, query=query, n=n)
        label = date
    else:
        return "Geef minimaal een 'date' (YYYY-MM-DD) of 'date_from'+'date_to' op."

    if not rows:
        q_info = f" met query '{query}'" if query else ""
        return f"Geen resultaten gevonden voor {label}{q_info}."

    parts = []
    for row in rows:
        fp = row["file_path"]
        ts = row.get("datetime_iso", "")
        header = f"[{fp} | {ts}]" if ts else f"[{fp}]"
        parts.append(f"{header}\n{row.get('snippet', '')}")

    return "\n\n---\n\n".join(parts)


DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "search_vault_semantic",
            "description": (
                "Semantic search across the entire fractalisme vault using vector embeddings. "
                "ALWAYS call this before claiming that information is missing or unknown."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_timeline",
            "description": (
                "Search the SQLite timeline index for session transcripts and memory notes by date and/or keyword. "
                "Use this when the user asks about a specific date or period."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "date": {"type": "string"},
                    "date_from": {"type": "string"},
                    "date_to": {"type": "string"},
                    "n": {"type": "integer"},
                },
                "required": [],
            },
        },
    },
]

HANDLERS = {
    "search_vault_semantic": lambda args: search_vault_semantic(args["query"]),
    "search_timeline": lambda args: search_timeline(
        query=args.get("query"),
        date=args.get("date"),
        date_from=args.get("date_from"),
        date_to=args.get("date_to"),
        n=args.get("n", 5),
    ),
}
