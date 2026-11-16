"""Derived full-text rows for deduplicated, eligible library prose blocks.

The source objects and occurrence offsets remain authoritative. Offsets are Python character
indices in the original normalized message, never indices in the canonical block or UTF-8 bytes.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from typing import Any

from .library_blocks import split_blocks

PASSAGE_LIMIT = 1500
PASSAGE_SCHEMA_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS passage_fts USING fts5(
    digest UNINDEXED, body, tokenize='porter unicode61'
);
CREATE INDEX IF NOT EXISTS occurrences_passage ON occurrences(
    digest,uid,message_index,block_index
);
"""


def index_passages(
    connection: sqlite3.Connection, *, incremental: bool = False, check_callback: Any = None
) -> None:
    """Synchronize derived rows inside the caller's transaction; never read source imports."""
    eligible = "b.eligible=1 AND EXISTS (SELECT 1 FROM occurrences o WHERE o.digest=b.digest)"
    if incremental:
        connection.execute(
            "DELETE FROM passage_fts WHERE rowid NOT IN (SELECT b.rowid FROM blocks b WHERE "
            + eligible
            + ")"
        )
        eligible += " AND NOT EXISTS (SELECT 1 FROM passage_fts f WHERE f.rowid=b.rowid)"
    else:
        connection.execute("DELETE FROM passage_fts")
    for row in connection.execute("SELECT b.rowid,b.digest,b.text FROM blocks b WHERE " + eligible):
        if check_callback:
            check_callback()
        body = zlib.decompress(row[2]).decode("utf-8") if isinstance(row[2], bytes) else row[2]
        connection.execute(
            "INSERT INTO passage_fts(rowid,digest,body) VALUES (?,?,?)", (row[0], row[1], body)
        )


def verify_passages(
    connection: sqlite3.Connection, *, full: bool, record: Any, check_callback: Any = None
) -> list[str]:
    problems = []
    if connection.execute(
        "SELECT 1 FROM blocks b LEFT JOIN passage_fts f ON f.rowid=b.rowid "
        "WHERE b.eligible=1 AND EXISTS (SELECT 1 FROM occurrences o WHERE o.digest=b.digest) "
        "AND f.digest IS NOT b.digest LIMIT 1"
    ).fetchone():
        problems.append("eligible block absent from passage search or misaligned")
    if connection.execute(
        "SELECT 1 FROM passage_fts f LEFT JOIN blocks b ON b.rowid=f.rowid "
        "WHERE b.digest IS NOT f.digest OR b.eligible!=1 OR NOT EXISTS "
        "(SELECT 1 FROM occurrences o WHERE o.digest=b.digest) LIMIT 1"
    ).fetchone():
        problems.append("passage search has a stale, ineligible or misaligned block")
    if connection.execute(
        "SELECT 1 FROM occurrences WHERE start<0 OR end<=start OR message_index<0 "
        "OR block_index<0 LIMIT 1"
    ).fetchone():
        problems.append("passage occurrence has an invalid character range")
    if full:
        for row in connection.execute(
            "SELECT b.digest,b.text,b.chars,f.body FROM blocks b "
            "JOIN passage_fts f ON f.rowid=b.rowid"
        ):
            if check_callback:
                check_callback()
            try:
                body = (
                    zlib.decompress(row[1]).decode("utf-8") if isinstance(row[1], bytes) else row[1]
                )
                valid = (
                    body == row[3]
                    and len(body) == row[2]
                    and hashlib.sha256(body.encode("utf-8")).hexdigest() == row[0]
                )
            except (zlib.error, UnicodeError):
                valid = False
            if not valid:
                problems.append(f"passage search text differs from its block: {row[0]}")
        problems.extend(_verify_sources(connection, record, check_callback))
    return problems


def _verify_sources(connection: sqlite3.Connection, record: Any, check_callback: Any) -> list[str]:
    """Bind each locator to its original message partition; decode each current record once."""
    problems = []
    current_uid, current_message = None, None
    messages, blocks = [], []
    for row in connection.execute(
        "SELECT uid,message_index,block_index,digest,start,end,normalization "
        "FROM occurrences ORDER BY uid,message_index,block_index"
    ):
        if check_callback:
            check_callback()
        try:
            if row["uid"] != current_uid:
                messages = record(row["uid"])["messages"]
                current_uid, current_message = row["uid"], None
            if row["message_index"] != current_message:
                if not 0 <= row["message_index"] < len(messages):
                    raise IndexError("message is outside the source")
                blocks = split_blocks(messages[row["message_index"]]["text"])
                current_message = row["message_index"]
            if not 0 <= row["block_index"] < len(blocks):
                raise IndexError("block is outside the source")
            block = blocks[row["block_index"]]
            valid = (
                block["eligible"]
                and block["digest"] == row["digest"]
                and block["start"] == row["start"]
                and block["end"] == row["end"]
                and block["normalization"] == json.loads(row["normalization"])
            )
        except (KeyError, IndexError, TypeError, UnicodeError, zlib.error, json.JSONDecodeError):
            valid = False
        if not valid:
            problems.append(
                f"passage occurrence differs from source: {row['uid']} "
                f"message {row['message_index']} block {row['block_index']}"
            )
    return problems
