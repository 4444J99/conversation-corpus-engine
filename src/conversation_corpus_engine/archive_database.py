from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from .archive_ingest import ArchivePaths, iter_store, load_manifest, require_inside
from .archive_topics import load_enrichment
from .corpus_store import CorpusWriteAuthorization

BATCH = 500
WORD_RE = re.compile(r"[\w][\w'-]*", re.UNICODE)
PHRASE_RE = re.compile(r'"([^"]+)"')
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
CREATE TABLE messages (
    id INTEGER PRIMARY KEY, uid TEXT NOT NULL REFERENCES conversations(uid),
    idx INTEGER NOT NULL, role TEXT NOT NULL, ts TEXT NOT NULL, mkind TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX messages_uid ON messages(uid, idx);
CREATE TABLE edges (src TEXT NOT NULL, dst TEXT NOT NULL, kind TEXT NOT NULL, weight REAL);
CREATE INDEX edges_src ON edges(src, kind);
CREATE VIRTUAL TABLE message_fts USING fts5(
    text, content='messages', content_rowid='id', tokenize='porter unicode61'
);
CREATE VIRTUAL TABLE title_fts USING fts5(
    title, snippet, content='conversations', content_rowid='rowid', tokenize='porter unicode61'
);
"""


def database_path(paths: ArchivePaths) -> Path:
    return paths.database


def fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 query: quoted phrases and quoted words, all ANDed."""
    phrases = [f'"{p.strip()}"' for p in PHRASE_RE.findall(text) if WORD_RE.search(p)]
    remainder = PHRASE_RE.sub(" ", text)
    words = [f'"{w}"' for w in WORD_RE.findall(remainder)]
    return " ".join([*phrases, *words])


def build_database(paths: ArchivePaths, authorization: CorpusWriteAuthorization) -> dict[str, Any]:
    require_inside(authorization, paths.database)
    manifest = load_manifest(paths)
    enrichment = load_enrichment(paths)
    target = database_path(paths)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.tmp")
    temp.unlink(missing_ok=True)
    connection = sqlite3.connect(temp)
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.executescript(SCHEMA)
        conversations = messages = edges = 0
        pending_conv: list[tuple[Any, ...]] = []
        pending_msg: list[tuple[Any, ...]] = []
        pending_edge: list[tuple[Any, ...]] = []
        for conversation in iter_store(paths):
            row = conversation.index_row()
            extra = enrichment.get(conversation.uid, {})
            pending_conv.append(
                (
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
                    " ".join(extra.get("topics", [])),
                )
            )
            conversations += 1
            for index, message in enumerate(conversation.messages):
                pending_msg.append(
                    (
                        conversation.uid,
                        index,
                        message.role,
                        message.ts,
                        str(message.meta.get("kind") or "text"),
                        message.text,
                    )
                )
                messages += 1
            for term in extra.get("topics", []):
                pending_edge.append((conversation.uid, term, "topic", None))
            for neighbour in extra.get("similar", []):
                pending_edge.append(
                    (conversation.uid, neighbour["uid"], "similar", neighbour["score"])
                )
            if conversation.collection:
                pending_edge.append(
                    (
                        conversation.uid,
                        f"{conversation.provider}:{conversation.collection}",
                        "collection",
                        None,
                    )
                )
            if len(pending_msg) >= BATCH or len(pending_conv) >= BATCH:
                edges += _flush(connection, pending_conv, pending_msg, pending_edge)
        edges += _flush(connection, pending_conv, pending_msg, pending_edge)
        connection.execute("INSERT INTO message_fts(rowid, text) SELECT id, text FROM messages")
        connection.execute(
            "INSERT INTO title_fts(rowid, title, snippet) SELECT rowid, title, snippet FROM conversations"
        )
        connection.execute("INSERT INTO message_fts(message_fts) VALUES ('optimize')")
        connection.executemany(
            "INSERT INTO meta(key, value) VALUES (?, ?)",
            [
                ("source_fingerprint", manifest["source_fingerprint"]),
                ("conversations", str(conversations)),
                ("messages", str(messages)),
            ],
        )
        connection.commit()
    finally:
        connection.close()
    temp.replace(target)
    return {
        "conversations": conversations,
        "messages": messages,
        "edges": edges,
        "bytes": target.stat().st_size,
    }


def _flush(
    connection: sqlite3.Connection,
    conversations: list[tuple[Any, ...]],
    messages: list[tuple[Any, ...]],
    edges: list[tuple[Any, ...]],
) -> int:
    connection.executemany(
        "INSERT INTO conversations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", conversations
    )
    connection.executemany(
        "INSERT INTO messages(uid, idx, role, ts, mkind, text) VALUES (?,?,?,?,?,?)", messages
    )
    connection.executemany("INSERT INTO edges VALUES (?,?,?,?)", edges)
    count = len(edges)
    conversations.clear()
    messages.clear()
    edges.clear()
    return count


def open_database(paths: ArchivePaths) -> sqlite3.Connection:
    path = database_path(paths)
    if not path.is_file():
        raise FileNotFoundError(f"No search database at {path}; run `cce archive database`")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def search(
    connection: sqlite3.Connection,
    query: str,
    *,
    providers: list[str] | None = None,
    kinds: list[str] | None = None,
    limit: int = 20,
    include_reasoning: bool = False,
) -> list[dict[str, Any]]:
    match = fts_query(query)
    if not match:
        return []
    clauses = ["message_fts MATCH ?"]
    params: list[Any] = [match]
    if not include_reasoning:
        clauses.append("m.mkind NOT IN ('reasoning', 'tool')")
    if providers:
        clauses.append(f"c.provider IN ({','.join('?' * len(providers))})")
        params.extend(providers)
    if kinds:
        clauses.append(f"c.kind IN ({','.join('?' * len(kinds))})")
        params.extend(kinds)
    sql = (
        "SELECT c.uid, c.provider, c.kind, c.title, c.created, c.message_count, m.idx, m.role, "
        "m.ts, snippet(message_fts, 0, '[', ']', '…', 24) AS excerpt, bm25(message_fts) AS rank "
        "FROM message_fts JOIN messages m ON m.id = message_fts.rowid "
        "JOIN conversations c ON c.uid = m.uid "
        f"WHERE {' AND '.join(clauses)} ORDER BY rank LIMIT ?"
    )
    params.append(max(limit * 8, 40))
    seen: set[str] = set()
    results: list[dict[str, Any]] = []
    for row in connection.execute(sql, params):
        if row["uid"] in seen:
            continue
        seen.add(row["uid"])
        results.append(dict(row))
        if len(results) >= limit:
            break
    return results


def search_titles(
    connection: sqlite3.Connection, query: str, *, limit: int = 20
) -> list[dict[str, Any]]:
    match = fts_query(query)
    if not match:
        return []
    sql = (
        "SELECT c.uid, c.provider, c.kind, c.title, c.created, c.message_count "
        "FROM title_fts JOIN conversations c ON c.rowid = title_fts.rowid "
        "WHERE title_fts MATCH ? ORDER BY bm25(title_fts) LIMIT ?"
    )
    return [dict(row) for row in connection.execute(sql, (match, limit))]


def conversation_messages(connection: sqlite3.Connection, uid: str) -> list[dict[str, Any]]:
    sql = "SELECT idx, role, ts, mkind, text FROM messages WHERE uid = ? ORDER BY idx"
    return [dict(row) for row in connection.execute(sql, (uid,))]


def verify_database(paths: ArchivePaths) -> list[str]:
    """Return a list of mismatches between the manifest denominators and the search database."""
    manifest = load_manifest(paths)
    problems: list[str] = []
    connection = open_database(paths)
    try:
        by_provider = dict(
            connection.execute("SELECT provider, COUNT(*) FROM conversations GROUP BY provider")
        )
        messages = dict(
            connection.execute(
                "SELECT c.provider, COUNT(*) FROM messages m JOIN conversations c ON c.uid = m.uid "
                "GROUP BY c.provider"
            )
        )
        stored = dict(connection.execute("SELECT key, value FROM meta"))
        for name, info in manifest["providers"].items():
            if info.get("status") != "ok":
                continue
            if by_provider.get(name, 0) != info["conversations"]:
                problems.append(
                    f"{name}: conversations {by_provider.get(name, 0)} != {info['conversations']}"
                )
            if messages.get(name, 0) != info["messages"]:
                problems.append(f"{name}: messages {messages.get(name, 0)} != {info['messages']}")
        if stored.get("source_fingerprint") != manifest["source_fingerprint"]:
            problems.append("search database was built from a different source fingerprint")
        orphans = connection.execute(
            "SELECT COUNT(*) FROM messages WHERE uid NOT IN (SELECT uid FROM conversations)"
        ).fetchone()[0]
        if orphans:
            problems.append(f"{orphans} orphan message row(s)")
    finally:
        connection.close()
    return problems
