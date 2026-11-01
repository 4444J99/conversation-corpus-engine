"""One search interface over both generations of the archive's full-text store.

A store written by the bounded layout answers from its turn store: SQLite shards by provider and
month inside two containers by author. A store written before it answers from its single
`database-all-search.sqlite`. Callers (the CLI, the local server, the MCP tool) go through here and
neither know nor care which, except that the author and month filters exist only on the new one.

A generation-bound unified FTS5 index can be built once per generation
(`<archive-root>/search-all/unified-<source_fingerprint>.sqlite`). When it exists for the current
generation fingerprint, searches open one database instead of hundreds of shards. The index is
derived entirely from the authoritative archive and contains no data not already present there.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from . import archive_database as legacy
from . import archive_turns as turns
from .archive_ingest import ArchivePaths, has_bounded_layout, load_manifest
from .archive_naming import derive_name
from .archive_turns import AUTHOR_BY_CONTAINER, fts_query, load_turn_manifest
from .corpus_store import CorpusWriteAuthorization


class FilterNotSupported(ValueError):
    """The store predates the turn store, so it cannot filter by author or month."""


UNIFIED_DIRNAME = derive_name("search", scope="all")


def _unified_index_path(paths: ArchivePaths, fingerprint: str) -> Path:
    stem = derive_name("index", scope="all", subject=fingerprint[:32])
    return paths.root / UNIFIED_DIRNAME / f"{stem}.sqlite"


def _has_unified_index(paths: ArchivePaths, fingerprint: str) -> bool:
    return _unified_index_path(paths, fingerprint).is_file()


def build_unified_index(
    paths: ArchivePaths,
    authorization: CorpusWriteAuthorization,
    fingerprint: str,
) -> dict[str, Any]:
    """Build a unified FTS5 index from all turn store shards for this generation.

    Reads every turn shard once and writes a single SQLite database keyed by the generation's
    source fingerprint. Subsequent searches against this generation open one file instead of
    opening every shard.
    """
    dest = _unified_index_path(paths, fingerprint)
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_suffix(".sqlite.build")
    temp.unlink(missing_ok=True)
    manifest = load_manifest(paths)
    turns_manifest = load_turn_manifest(paths)

    conn = sqlite3.connect(temp)
    try:
        conn.executescript(
            """
            CREATE TABLE conversations (
                uid TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                created TEXT NOT NULL,
                message_count INTEGER NOT NULL,
                author TEXT NOT NULL
            );
            CREATE TABLE turns (
                id INTEGER PRIMARY KEY,
                uid TEXT NOT NULL REFERENCES conversations(uid),
                author TEXT NOT NULL,
                role TEXT NOT NULL,
                channel TEXT NOT NULL,
                ts TEXT NOT NULL,
                month TEXT NOT NULL,
                mkind TEXT NOT NULL,
                typed_chars INTEGER NOT NULL,
                text TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE turn_fts USING fts5(
                text, content='turns', content_rowid='id', tokenize='porter unicode61'
            );
            CREATE VIRTUAL TABLE title_fts USING fts5(
                title, content='conversations', content_rowid='rowid', tokenize='porter unicode61'
            );
            """
        )

        turn_id = 0
        for container, info in turns_manifest.get("containers", {}).items():
            author = AUTHOR_BY_CONTAINER.get(container)
            if author is None:
                continue
            for item in info.get("shards", []):
                shard_path = paths.turns / item["path"]
                shard_conn = sqlite3.connect(f"file:{shard_path}?mode=ro", uri=True)
                shard_conn.row_factory = sqlite3.Row
                try:
                    for row in shard_conn.execute("SELECT * FROM conversations"):
                        conn.execute(
                            "INSERT OR IGNORE INTO conversations VALUES (?,?,?,?,?,?,?)",
                            (
                                row["uid"],
                                row["provider"],
                                row["kind"],
                                row["title"],
                                row["created"],
                                row["message_count"],
                                author,
                            ),
                        )
                    for row in shard_conn.execute("SELECT * FROM turns"):
                        turn_id += 1
                        ts = row["ts"]
                        month = f"{ts[0:4]}{ts[5:7]}" if len(ts) >= 7 else "undated"
                        conn.execute(
                            "INSERT INTO turns VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (
                                turn_id,
                                row["uid"],
                                author,
                                row["role"],
                                row["channel"],
                                ts,
                                month,
                                row["mkind"],
                                row["typed_chars"],
                                row["text"],
                            ),
                        )
                        conn.execute(
                            "INSERT INTO turn_fts(rowid, text) VALUES (?, ?)",
                            (turn_id, row["text"]),
                        )
                finally:
                    shard_conn.close()

        # Populate title FTS from all conversations (inserted once per uid).
        conn.execute("INSERT INTO title_fts(rowid, title) SELECT rowid, title FROM conversations")
        conn.execute("INSERT INTO turn_fts(turn_fts) VALUES ('optimize')")
        conn.execute("INSERT INTO title_fts(title_fts) VALUES ('optimize')")
        conn.commit()
    finally:
        conn.close()

    temp.replace(dest)

    return {
        "path": str(dest.relative_to(paths.root)),
        "fingerprint": fingerprint,
        "turns": turn_id,
    }


def search_unified(
    paths: ArchivePaths,
    fingerprint: str,
    query: str,
    *,
    providers: list[str] | None = None,
    kinds: list[str] | None = None,
    authors: list[str] | None = None,
    months: list[str] | None = None,
    limit: int = 20,
    include_reasoning: bool = False,
) -> list[dict[str, Any]]:
    """Search the unified generation-bound index.

    Falls back to per-shard search if the unified index does not exist for this fingerprint.
    """
    if not _has_unified_index(paths, fingerprint):
        return search_shards(
            paths,
            query,
            providers=providers,
            kinds=kinds,
            authors=authors,
            months=months,
            limit=limit,
            include_reasoning=include_reasoning,
        )

    match = fts_query(query)
    if not match:
        return []

    path = _unified_index_path(paths, fingerprint)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        clauses = ["turn_fts MATCH ?"]
        params: list[Any] = [match]
        if not include_reasoning:
            clauses.append("t.mkind NOT IN ('reasoning', 'tool')")
        if kinds:
            clauses.append(f"c.kind IN ({','.join('?' * len(kinds))})")
            params.extend(kinds)
        if authors:
            clauses.append(f"t.author IN ({','.join('?' * len(authors))})")
            params.extend(authors)
        if providers:
            clauses.append(f"c.provider IN ({','.join('?' * len(providers))})")
            params.extend(providers)
        if months:
            month_placeholders = ",".join("?" * len(months))
            clauses.append(f"t.month IN ({month_placeholders})")
            params.extend(months)

        sql = (
            "SELECT c.uid, c.provider, c.kind, c.title, c.created, c.message_count, "
            "t.author, t.role, t.channel, t.ts, "
            "snippet(turn_fts, 0, '[', ']', '…', 24) AS excerpt, bm25(turn_fts) AS rank "
            "FROM turn_fts JOIN turns t ON t.id = turn_fts.rowid "
            "JOIN conversations c ON c.uid = t.uid "
            f"WHERE {' AND '.join(clauses)} ORDER BY rank LIMIT ?"
        )
        params.append(limit)

        rows = conn.execute(sql, params)
        results = []
        seen: set[str] = set()
        for row in rows:
            uid = row["uid"]
            if uid in seen:
                continue
            seen.add(uid)
            results.append(dict(row))
        return results
    finally:
        conn.close()


def search_titles_unified(
    paths: ArchivePaths,
    fingerprint: str,
    query: str,
    *,
    providers: list[str] | None = None,
    months: list[str] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Search titles in the unified generation-bound index."""
    if not _has_unified_index(paths, fingerprint):
        return turns.search_turn_titles(
            paths, query, providers=providers, months=months, limit=limit
        )

    match = fts_query(query)
    if not match:
        return []

    path = _unified_index_path(paths, fingerprint)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        clauses = ["title_fts MATCH ?"]
        params: list[Any] = [match]
        if providers:
            clauses.append(f"c.provider IN ({','.join('?' * len(providers))})")
            params.extend(providers)

        sql = (
            "SELECT c.uid, c.provider, c.kind, c.title, c.created, c.message_count, "
            "snippet(title_fts, 0, '[', ']', '…', 24) AS excerpt, bm25(title_fts) AS rank "
            "FROM title_fts JOIN conversations c ON c.rowid = title_fts.rowid "
            f"WHERE {' AND '.join(clauses)} ORDER BY rank LIMIT ?"
        )
        params.append(limit)

        rows = conn.execute(sql, params)
        results = []
        seen: set[str] = set()
        for row in rows:
            uid = row["uid"]
            if uid in seen:
                continue
            seen.add(uid)
            results.append(dict(row))
        return results
    finally:
        conn.close()


def search_shards(
    paths: ArchivePaths,
    query: str,
    *,
    providers: list[str] | None = None,
    kinds: list[str] | None = None,
    authors: list[str] | None = None,
    months: list[str] | None = None,
    limit: int = 20,
    include_reasoning: bool = False,
) -> list[dict[str, Any]]:
    """Original per-shard search implementation."""
    return turns.search_turns(
        paths,
        query,
        authors=authors,
        providers=providers,
        kinds=kinds,
        months=months,
        limit=limit,
        include_reasoning=include_reasoning,
    )


def build_search_store(
    paths: ArchivePaths, authorization: CorpusWriteAuthorization
) -> dict[str, Any]:
    """Build the full-text store the archive's layout calls for.

    Builds both the per-author shard turn store and a generation-bound unified index so
    subsequent searches open one database instead of every shard.
    """
    if has_bounded_layout(load_manifest(paths)):
        manifest = load_manifest(paths)
        fingerprint = manifest.get("source_fingerprint", "")
        result = turns.build_turn_store(paths, authorization)
        if fingerprint:
            result["unified_index"] = build_unified_index(
                paths, authorization, fingerprint
            )
        return result
    return legacy.build_database(paths, authorization)


def verify_search_store(paths: ArchivePaths) -> list[str]:
    if has_bounded_layout(load_manifest(paths)):
        return turns.verify_turn_store(paths)
    return legacy.verify_database(paths)


def search(
    paths: ArchivePaths,
    query: str,
    *,
    providers: list[str] | None = None,
    kinds: list[str] | None = None,
    authors: list[str] | None = None,
    months: list[str] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Best matching turn of each conversation. Raises FileNotFoundError when nothing is built.

    Uses the unified generation-bound index when available; falls back to per-shard search
    otherwise.
    """
    if turns.has_turn_store(paths):
        manifest = load_manifest(paths)
        fingerprint = manifest.get("source_fingerprint", "")
        if fingerprint:
            return search_unified(
                paths,
                fingerprint,
                query,
                providers=providers,
                kinds=kinds,
                authors=authors,
                months=months,
                limit=limit,
            )
        return turns.search_turns(
            paths,
            query,
            authors=authors,
            providers=providers,
            kinds=kinds,
            months=months,
            limit=limit,
        )
    if authors or months:
        raise FilterNotSupported(
            "this archive was built before the turn store; rebuild it to filter by author or month"
        )
    connection = legacy.open_database(paths)
    try:
        return legacy.search(connection, query, providers=providers, kinds=kinds, limit=limit)
    finally:
        connection.close()


def search_titles(
    paths: ArchivePaths,
    query: str,
    *,
    providers: list[str] | None = None,
    months: list[str] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    if turns.has_turn_store(paths):
        manifest = load_manifest(paths)
        fingerprint = manifest.get("source_fingerprint", "")
        if fingerprint:
            return search_titles_unified(
                paths,
                fingerprint,
                query,
                providers=providers,
                months=months,
                limit=limit,
            )
        return turns.search_turn_titles(
            paths, query, providers=providers, months=months, limit=limit
        )
    if months:
        raise FilterNotSupported("this archive was built before the turn store; rebuild it")
    connection = legacy.open_database(paths)
    try:
        return legacy.search_titles(connection, query, limit=limit)
    finally:
        connection.close()
