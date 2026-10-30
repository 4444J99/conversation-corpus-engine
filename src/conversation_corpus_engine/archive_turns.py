"""The turn store: every message of the archive as one row, in two containers by author.

The atom is the turn (one message). Each turn has an author (see `archive_author`) and lives in
exactly one of two containers, `authored` (what the person typed) and `machine` (everything
else). A container is a folder; inside it every provider has a folder of SQLite shards, one per
provider-month, mirroring the normalized shards one for one. A shard holds the conversation rows
and the turn rows of its author plus two full-text indexes, so any slice of the archive is a
single small file that opens on its own, and a question about what the person wrote never
touches a byte of machine text.

    database-all-turns/
      manifest-all-turns.json
      container-all-authored/provider-chatgpt/shard-chatgpt-202506.sqlite
      container-all-machine/provider-chatgpt/shard-chatgpt-202506.sqlite

Search opens only the shards a question can touch (the authors, providers and months it names)
and merges their ranked hits. BM25 ranks are comparable inside a shard and approximately so
across shards, which is enough to order the best hit of each conversation.
"""

from __future__ import annotations

import heapq
import json
import shutil
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import archive_layout as layout
from .archive_author import AUTHOR_BY_CONTAINER, AUTHORS, CONTAINER_BY_AUTHOR, HUMAN, MACHINE
from .archive_database import fts_query
from .archive_ingest import ArchivePaths, load_manifest, require_inside
from .archive_model import Conversation, iter_conversations, sha256_file, write_json_atomic
from .archive_shard import UNDATED, parse_shard_subject, shard_sort_key
from .archive_topics import load_enrichment
from .corpus_store import CorpusWriteAuthorization

TURNS_SCHEMA = "archive-turns.v2"
BATCH = 500
SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE conversations (
    uid TEXT PRIMARY KEY, provider TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
    created TEXT NOT NULL, updated TEXT NOT NULL, last_activity TEXT NOT NULL,
    collection TEXT NOT NULL, model TEXT NOT NULL, archived INTEGER NOT NULL,
    message_count INTEGER NOT NULL, word_count INTEGER NOT NULL,
    attachment_count INTEGER NOT NULL, snippet TEXT NOT NULL, source_file TEXT NOT NULL,
    topics TEXT NOT NULL
);
CREATE TABLE turns (
    id INTEGER PRIMARY KEY, uid TEXT NOT NULL REFERENCES conversations(uid),
    cseq INTEGER NOT NULL, ordinal INTEGER NOT NULL, turn_id TEXT NOT NULL,
    author TEXT NOT NULL, role TEXT NOT NULL,
    channel TEXT NOT NULL, ts TEXT NOT NULL, mkind TEXT NOT NULL, typed_chars INTEGER NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX turns_uid ON turns(uid, ordinal);
CREATE VIRTUAL TABLE turn_fts USING fts5(
    text, content='turns', content_rowid='id', tokenize='porter unicode61'
);
CREATE VIRTUAL TABLE title_fts USING fts5(
    title, snippet, content='conversations', content_rowid='rowid', tokenize='porter unicode61'
);
"""
BUILD_DIRNAME = ".database-all-turns.build"
OLD_DIRNAME = ".database-all-turns.old"


class TurnStoreError(ValueError):
    """The turn store is missing, or disagrees with the archive manifest."""


def has_turn_store(paths: ArchivePaths) -> bool:
    return paths.turns_manifest.is_file()


def _row(conversation: Conversation, topics: list[str]) -> tuple[Any, ...]:
    row = conversation.index_row()
    return (
        row["uid"],
        row["provider"],
        row["kind"],
        row["title"],
        row["created"],
        row["updated"],
        row["last_activity"],
        row["collection"],
        row["model"],
        int(row["archived"]),
        row["message_count"],
        row["word_count"],
        row["attachment_count"],
        row["snippet"],
        row["source_file"],
        " ".join(topics),
    )


class _ShardBuild:
    """One author's SQLite shard while it is being filled."""

    def __init__(self, path: Path, meta: dict[str, str]) -> None:
        self.path = path
        self.meta = meta
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=OFF")
        self.connection.execute("PRAGMA synchronous=OFF")
        self.connection.executescript(SCHEMA)
        self.conversations: list[tuple[Any, ...]] = []
        self.turns: list[tuple[Any, ...]] = []
        self.conversation_count = 0
        self.turn_count = 0

    def add_conversation(self, row: tuple[Any, ...]) -> None:
        self.conversations.append(row)
        self.conversation_count += 1

    def add_turn(self, row: tuple[Any, ...]) -> None:
        self.turns.append(row)
        self.turn_count += 1
        if len(self.turns) >= BATCH:
            self.flush()

    def flush(self) -> None:
        self.connection.executemany(
            "INSERT INTO conversations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            self.conversations,
        )
        self.connection.executemany(
            "INSERT INTO turns(uid, cseq, ordinal, turn_id, author, role, channel, ts, mkind, "
            "typed_chars, text) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            self.turns,
        )
        self.conversations.clear()
        self.turns.clear()

    def discard(self) -> None:
        """Drop a shard that received neither a turn nor a conversation: it would be clutter."""
        self.connection.close()
        self.path.unlink(missing_ok=True)

    def finish(self) -> None:
        self.flush()
        c = self.connection
        c.execute("INSERT INTO turn_fts(rowid, text) SELECT id, text FROM turns")
        c.execute(
            "INSERT INTO title_fts(rowid, title, snippet) "
            "SELECT rowid, title, snippet FROM conversations"
        )
        c.execute("INSERT INTO turn_fts(turn_fts) VALUES ('optimize')")
        meta = {
            **self.meta,
            "conversations": str(self.conversation_count),
            "turns": str(self.turn_count),
        }
        c.executemany("INSERT INTO meta(key, value) VALUES (?, ?)", sorted(meta.items()))
        c.commit()
        c.close()


def _shard_subject(shard_file: Path, provider: str) -> str:
    stem = shard_file.name[: -len(".jsonl")]
    return stem[len(f"shard-{provider}-") :]


def build_turn_store(
    paths: ArchivePaths, authorization: CorpusWriteAuthorization
) -> dict[str, Any]:
    """Build both containers from the normalized shards, one SQLite shard per author per shard."""
    require_inside(authorization, paths.turns)
    manifest = load_manifest(paths)
    enrichment = load_enrichment(paths)
    root = paths.turns
    build = root.with_name(BUILD_DIRNAME)
    shutil.rmtree(build, ignore_errors=True)
    entries: dict[str, list[dict[str, Any]]] = {name: [] for name in CONTAINER_BY_AUTHOR.values()}
    totals = {HUMAN: 0, MACHINE: 0}
    per_provider: dict[str, dict[str, int]] = {}
    conversations = 0
    try:
        for provider in paths.providers_present():
            counts = per_provider.setdefault(provider, {HUMAN: 0, MACHINE: 0})
            for shard_file in paths.shard_files(provider):
                subject = _shard_subject(shard_file, provider)
                builds: dict[str, _ShardBuild] = {}
                for author in AUTHORS:
                    container = CONTAINER_BY_AUTHOR[author]
                    target = (
                        build
                        / layout.container_dirname(container)
                        / layout.provider_dirname(provider)
                        / layout.turn_shard_filename(provider, subject)
                    )
                    builds[author] = _ShardBuild(
                        target,
                        {
                            "source_fingerprint": manifest["source_fingerprint"],
                            "provider": provider,
                            "subject": subject,
                            "author": author,
                            "container": container,
                            "schema": TURNS_SCHEMA,
                        },
                    )
                for cseq, conversation in enumerate(iter_conversations(shard_file)):
                    conversations += 1
                    topics = enrichment.get(conversation.uid, {}).get("topics", [])
                    row = _row(conversation, topics)
                    present: set[str] = set()
                    if not conversation.messages:
                        # No turn to hold it, but the conversation stays findable by title; with
                        # nothing typed in it, its metadata belongs to the machine container.
                        builds[MACHINE].add_conversation(row)
                    for ordinal, message in enumerate(conversation.messages):
                        author = message.author
                        if author not in present:
                            builds[author].add_conversation(row)
                            present.add(author)
                        builds[author].add_turn(
                            (
                                conversation.uid,
                                cseq,
                                ordinal,
                                message.id,
                                author,
                                message.role,
                                message.channel,
                                message.ts,
                                str(message.meta.get("kind") or "text"),
                                int(message.typed_chars or 0),
                                message.text,
                            )
                        )
                        counts[author] += 1
                        totals[author] += 1
                for author, shard in builds.items():
                    if shard.turn_count == 0 and shard.conversation_count == 0:
                        shard.discard()
                        continue
                    shard.finish()
                    container = CONTAINER_BY_AUTHOR[author]
                    entries[container].append(
                        {
                            "provider": provider,
                            "subject": subject,
                            "conversations": shard.conversation_count,
                            "turns": shard.turn_count,
                            "bytes": shard.path.stat().st_size,
                            "sha256": sha256_file(shard.path),
                            "path": shard.path.relative_to(build).as_posix(),
                        }
                    )
        document = {
            "schema": TURNS_SCHEMA,
            "source_fingerprint": manifest["source_fingerprint"],
            "containers": {
                container: {
                    "author": AUTHOR_BY_CONTAINER[container],
                    "turns": sum(item["turns"] for item in items),
                    "shards": items,
                }
                for container, items in entries.items()
            },
            "totals": {"turns": sum(totals.values()), "authors": dict(totals)},
            "providers": per_provider,
        }
        write_json_atomic(build / layout.TURNS_MANIFEST_FILENAME, document)
        old = root.with_name(OLD_DIRNAME)
        shutil.rmtree(old, ignore_errors=True)
        if root.exists():
            root.replace(old)
        build.replace(root)
        shutil.rmtree(old, ignore_errors=True)
    except BaseException:
        shutil.rmtree(build, ignore_errors=True)
        raise
    shards = sum(len(items) for items in entries.values())
    return {
        "conversations": conversations,
        "turns": document["totals"]["turns"],
        "authors": document["totals"]["authors"],
        "shards": shards,
        "bytes": sum(item["bytes"] for items in entries.values() for item in items),
    }


def load_turn_manifest(paths: ArchivePaths) -> dict[str, Any]:
    if not has_turn_store(paths):
        raise FileNotFoundError(f"No turn store at {paths.turns}; run `cce archive database`")
    return json.loads(paths.turns_manifest.read_text(encoding="utf-8"))


def verify_turn_store(paths: ArchivePaths) -> list[str]:
    """Mismatches between the archive manifest, the turn manifest and the shard files."""
    manifest = load_manifest(paths)
    try:
        turns = load_turn_manifest(paths)
    except FileNotFoundError as exc:
        return [str(exc)]
    problems: list[str] = []
    if turns.get("schema") != TURNS_SCHEMA:
        problems.append(f"turn store schema {turns.get('schema')!r}, expected {TURNS_SCHEMA!r}")
    if turns.get("source_fingerprint") != manifest["source_fingerprint"]:
        problems.append("turn store was built from a different source fingerprint")
    expected = {
        name: info.get("authors", {})
        for name, info in manifest["providers"].items()
        if info.get("status") == "ok"
    }
    found: dict[str, dict[str, int]] = {}
    for container, info in turns.get("containers", {}).items():
        author = AUTHOR_BY_CONTAINER.get(container)
        if author is None:
            problems.append(f"unknown container {container!r}")
            continue
        for item in info.get("shards", []):
            path = paths.turns / item["path"]
            if not path.is_file():
                problems.append(f"missing shard file: {item['path']}")
                continue
            if path.stat().st_size != item["bytes"]:
                problems.append(f"shard size differs from the turn manifest: {item['path']}")
            counted = _count_turns(path)
            if counted != item["turns"]:
                problems.append(
                    f"{item['path']}: {counted} turn row(s), manifest says {item['turns']}"
                )
            found.setdefault(item["provider"], {HUMAN: 0, MACHINE: 0})[author] += counted
    for provider, authors in expected.items():
        for author in AUTHORS:
            have = found.get(provider, {}).get(author, 0)
            want = authors.get(author, 0)
            if have != want:
                problems.append(f"{provider}: {author} turns {have} != manifest {want}")
    for provider in found:
        if provider not in expected:
            problems.append(f"turn store holds {provider}, which the manifest does not list as ok")
    return problems


def _count_turns(path: Path) -> int:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0])
    finally:
        connection.close()


# --- search -----------------------------------------------------------------------------------


def _month_matches(subject: str, months: list[str] | None) -> bool:
    if not months:
        return True
    month, _ = parse_shard_subject(subject)
    return month in months or (UNDATED in months and month == UNDATED)


def select_shards(
    paths: ArchivePaths,
    *,
    authors: Iterable[str] | None = None,
    providers: Iterable[str] | None = None,
    months: list[str] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """The shards a question can touch: (author, manifest entry) pairs, in reading order."""
    turns = load_turn_manifest(paths)
    wanted_authors = set(authors) if authors else set(AUTHORS)
    wanted_providers = set(providers) if providers else None
    picked: list[tuple[str, dict[str, Any]]] = []
    for container in CONTAINER_BY_AUTHOR.values():
        author = AUTHOR_BY_CONTAINER[container]
        if author not in wanted_authors:
            continue
        for item in turns["containers"].get(container, {}).get("shards", []):
            if wanted_providers is not None and item["provider"] not in wanted_providers:
                continue
            if not _month_matches(item["subject"], months):
                continue
            picked.append((author, item))
    return picked


def _open(paths: ArchivePaths, item: dict[str, Any]) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{paths.turns / item['path']}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def search_turns(
    paths: ArchivePaths,
    query: str,
    *,
    authors: list[str] | None = None,
    providers: list[str] | None = None,
    kinds: list[str] | None = None,
    months: list[str] | None = None,
    limit: int = 20,
    include_reasoning: bool = False,
) -> list[dict[str, Any]]:
    """Full-text search across the shards a question names; the best turn of each conversation."""
    match = fts_query(query)
    if not match:
        return []
    clauses = ["turn_fts MATCH ?"]
    params: list[Any] = [match]
    if not include_reasoning:
        clauses.append("t.mkind NOT IN ('reasoning', 'tool')")
    if kinds:
        clauses.append(f"c.kind IN ({','.join('?' * len(kinds))})")
        params.extend(kinds)
    sql = (
        "SELECT c.uid, c.provider, c.kind, c.title, c.created, c.message_count, "
        "t.ordinal AS idx, t.author, t.role, t.channel, t.ts, "
        "snippet(turn_fts, 0, '[', ']', '…', 24) AS excerpt, bm25(turn_fts) AS rank "
        "FROM turn_fts JOIN turns t ON t.id = turn_fts.rowid "
        "JOIN conversations c ON c.uid = t.uid "
        f"WHERE {' AND '.join(clauses)} ORDER BY rank LIMIT ?"
    )
    per_shard = max(limit * 8, 40)
    hits: list[dict[str, Any]] = []
    for _author, item in select_shards(paths, authors=authors, providers=providers, months=months):
        connection = _open(paths, item)
        try:
            hits.extend(dict(row) for row in connection.execute(sql, [*params, per_shard]))
        finally:
            connection.close()
    hits.sort(key=lambda hit: (hit["rank"], hit["uid"], hit["idx"]))
    seen: set[str] = set()
    results: list[dict[str, Any]] = []
    for hit in hits:
        if hit["uid"] in seen:
            continue
        seen.add(hit["uid"])
        results.append(hit)
        if len(results) >= limit:
            break
    return results


def search_turn_titles(
    paths: ArchivePaths,
    query: str,
    *,
    providers: list[str] | None = None,
    months: list[str] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Title and snippet search; a conversation present in both containers is listed once."""
    match = fts_query(query)
    if not match:
        return []
    sql = (
        "SELECT c.uid, c.provider, c.kind, c.title, c.created, c.message_count, "
        "bm25(title_fts) AS rank FROM title_fts JOIN conversations c ON c.rowid = title_fts.rowid "
        "WHERE title_fts MATCH ? ORDER BY rank LIMIT ?"
    )
    hits: dict[str, dict[str, Any]] = {}
    for _author, item in select_shards(paths, providers=providers, months=months):
        connection = _open(paths, item)
        try:
            for row in connection.execute(sql, (match, limit)):
                hit = dict(row)
                if hit["uid"] not in hits or hit["rank"] < hits[hit["uid"]]["rank"]:
                    hits[hit["uid"]] = hit
        finally:
            connection.close()
    ordered = sorted(hits.values(), key=lambda hit: (hit["rank"], hit["uid"]))
    return [{key: value for key, value in hit.items() if key != "rank"} for hit in ordered[:limit]]


def list_turns(
    paths: ArchivePaths,
    *,
    author: str | None = None,
    provider: str | None = None,
    month: str | None = None,
    uid: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Turns of the named slice in reading order (shard, conversation, ordinal), paged.

    A provider-month holds one shard per author. Without an author filter their rows are merged
    by conversation and ordinal, so a conversation reads user, assistant, user, assistant, not
    every typed turn first.
    """
    clauses = ["1=1"]
    params: list[Any] = []
    if uid:
        clauses.append("t.uid = ?")
        params.append(uid)
    sql = (
        "SELECT t.uid, t.cseq, t.ordinal, t.turn_id, t.author, t.role, t.channel, t.ts, "
        "t.typed_chars, t.text, c.provider, c.title, c.created "
        "FROM turns t JOIN conversations c ON c.uid = t.uid "
        f"WHERE {' AND '.join(clauses)} ORDER BY t.id LIMIT ?"
    )
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for _author, item in select_shards(
        paths,
        authors=[author] if author else None,
        providers=[provider] if provider else None,
        months=[month] if month else None,
    ):
        groups.setdefault((item["provider"], item["subject"]), []).append(item)
    provider_rank = {name: rank for rank, name in enumerate(load_turn_manifest(paths)["providers"])}
    rows: list[dict[str, Any]] = []
    skip = offset
    for key in sorted(
        groups, key=lambda k: (provider_rank.get(k[0], len(provider_rank)), shard_sort_key(k[1]))
    ):
        connections = [_open(paths, item) for item in groups[key]]
        try:
            streams = [
                (dict(row) for row in connection.execute(sql, [*params, limit + skip]))
                for connection in connections
            ]
            for row in heapq.merge(*streams, key=lambda r: (r["cseq"], r["ordinal"])):
                if skip:
                    skip -= 1
                    continue
                del row["cseq"]
                rows.append(row)
                if len(rows) >= limit:
                    return rows
        finally:
            for connection in connections:
                connection.close()
    return rows
