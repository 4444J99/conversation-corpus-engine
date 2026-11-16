"""One search interface over both generations of the archive's full-text store.

A store written by the bounded layout answers from its turn store: SQLite shards by provider and
month inside two containers by author. A store written before it answers from its single
`database-all-search.sqlite`. Callers (the CLI, the local server, the MCP tool) go through here and
neither know nor care which, except that the author and month filters exist only on the new one.
"""

from __future__ import annotations

from typing import Any

from . import archive_database as legacy
from . import archive_turns as turns
from .archive_ingest import ArchivePaths, has_bounded_layout, load_manifest
from .corpus_store import CorpusWriteAuthorization


class FilterNotSupported(ValueError):
    """The store predates the turn store, so it cannot filter by author or month."""


def build_search_store(
    paths: ArchivePaths, authorization: CorpusWriteAuthorization
) -> dict[str, Any]:
    """Build the full-text store the archive's layout calls for."""
    if has_bounded_layout(load_manifest(paths)):
        return turns.build_turn_store(paths, authorization)
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
    """Best matching turn of each conversation. Raises FileNotFoundError when nothing is built."""
    if turns.has_turn_store(paths):
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
