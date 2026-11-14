"""Census of the Gemini macOS app's local chat store, metadata only.

``ChatInfo2.store`` (a SwiftData/SQLite file) lists every chat the app knows and every cached
message. The message and chat bodies are stored in ``ZENCRYPTEDPROTOBYTES`` columns, an encrypted
container (its bytes begin ``2EBAFECA``). This module never reads those columns and does not try
to open them: the connection carries a SQLite authorizer that denies any read of them, so even a
coding mistake cannot return them. What it reports is what the unencrypted columns hold: chat
identifiers, last-updated times, message counts, user-turn counts and byte sizes.

The conversation text for these chats reaches the archive only through Google's own export, which
``archive_read_gemini`` already reads (see ``OWNER_EXPORT``).
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import archive_read_gemini
from .app_cache_sqlite import SnapshotError, snapshot

__all__ = ["SnapshotError"]

SCHEMA = "cce-gemini-app-store/v1"
INVENTORY_NAME = "gemini-store-inventory.json"
COVERAGE_NAME = "coverage.json"
STORE = ("Library", "Caches", "com.google.GeminiMacOS", "Gemini", "user1", "ChatInfo2.store")
ENCRYPTED_COLUMN = "ZENCRYPTEDPROTOBYTES"
CORE_DATA_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)

OWNER_EXPORT = {
    "provider_root": "the gemini provider root of the archive's source library",
    "takeout_product": "Google Takeout: My Activity > Gemini Apps, in JSON format",
    "file_the_reader_detects": str(archive_read_gemini.ACTIVITY),
    "also_read_when_present": [
        str(archive_read_gemini.AI_MODE) + "/*.json",
        str(archive_read_gemini.WORKSPACE),
        str(archive_read_gemini.SCHEDULED),
    ],
    "reader": archive_read_gemini.READER,
    "note": (
        "the reader parses only the files named here; the export's coverage of a given chat is "
        "known only after it is read"
    ),
}


class EncryptedColumnRead(sqlite3.DatabaseError):
    pass


def _deny_encrypted(action: int, arg1: str | None, arg2: str | None, *_: Any) -> int:
    if action == sqlite3.SQLITE_READ and arg2 == ENCRYPTED_COLUMN:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def default_store(home: Path) -> Path:
    return home.joinpath(*STORE)


def _iso(core_data_seconds: float | None) -> str:
    if core_data_seconds is None:
        return ""
    return (CORE_DATA_EPOCH + timedelta(seconds=float(core_data_seconds))).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def census(store: Path) -> dict[str, Any]:
    """Counts and per-chat metadata. The encrypted columns are denied, not merely unselected."""
    with snapshot(store) as db:
        db.set_authorizer(_deny_encrypted)
        chats = db.execute(
            "SELECT ZCHATUUID, ZROBINCONVERSATIONID, ZLASTUPDATEDTIME "
            "FROM ZCHATINFOSTOREDMODEL ORDER BY ZLASTUPDATEDTIME"
        ).fetchall()
        messages = db.execute(
            "SELECT ZCHATUUID, ZMESSAGEINDEX, ZCREATEDTIME, ZISUSERTURN, ZBYTESIZE, "
            "ZHASTASKARTIFACTS FROM ZCHATMESSAGESTOREDMODEL ORDER BY ZCHATUUID, ZMESSAGEINDEX"
        ).fetchall()
        encrypted_rows = db.execute(
            "SELECT (SELECT count(*) FROM ZCHATINFOSTOREDMODEL), "
            "(SELECT count(*) FROM ZCHATMESSAGESTOREDMODEL)"
        ).fetchone()
    per_chat: dict[str, dict[str, Any]] = {}
    for chat, _index, created, is_user, size, _tasks in messages:
        row = per_chat.setdefault(
            chat, {"messages": 0, "user_turns": 0, "bytes": 0, "first": None, "last": None}
        )
        row["messages"] += 1
        row["user_turns"] += 1 if is_user else 0
        row["bytes"] += int(size or 0)
        row["first"] = created if row["first"] is None else min(row["first"], created)
        row["last"] = created if row["last"] is None else max(row["last"], created)
    inventory = [
        {
            "chat_uuid": chat,
            "robin_conversation_id": robin or "",
            "last_updated": _iso(updated),
            "cached_messages": per_chat.get(chat, {}).get("messages", 0),
            "cached_user_turns": per_chat.get(chat, {}).get("user_turns", 0),
            "cached_bytes": per_chat.get(chat, {}).get("bytes", 0),
        }
        for chat, robin, updated in chats
    ]
    known = {chat for chat, _, _ in chats}
    updated_days = sorted(row["last_updated"][:10] for row in inventory if row["last_updated"])
    message_days = sorted(_iso(m[2])[:10] for m in messages if m[2] is not None)
    by_month = Counter(day[:7] for day in updated_days)
    return {
        "schema": SCHEMA,
        "chats": len(chats),
        "chats_updated_first": updated_days[0] if updated_days else None,
        "chats_updated_last": updated_days[-1] if updated_days else None,
        "chats_updated_by_month": dict(sorted(by_month.items())),
        "chats_after_2026_10_16": sum(1 for day in updated_days if day > "2026-10-16"),
        "cached_messages": len(messages),
        "cached_user_turns": sum(1 for m in messages if m[3]),
        "cached_message_bytes": sum(int(m[4] or 0) for m in messages),
        "chats_with_cached_messages": len(per_chat),
        "cached_messages_first": message_days[0] if message_days else None,
        "cached_messages_last": message_days[-1] if message_days else None,
        "cached_messages_without_a_chat_row": sum(1 for chat in per_chat if chat not in known),
        "rows_with_encrypted_body": {"chats": encrypted_rows[0], "messages": encrypted_rows[1]},
        "bodies": "encrypted container (ZENCRYPTEDPROTOBYTES, prefix 2EBAFECA); not read, not opened",
        "owner_export": OWNER_EXPORT,
        "inventory": inventory,
    }


def write_report(report: dict[str, Any], out: Path) -> dict[str, Any]:
    """Write the inventory (private, with ids) and a coverage file without ids."""
    out = out.expanduser().absolute()
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise FileExistsError(f"output directory already exists and is not empty: {out}")
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    (out / INVENTORY_NAME).write_text(json.dumps(report, indent=1), encoding="utf-8")
    public = {k: v for k, v in report.items() if k != "inventory"}
    (out / COVERAGE_NAME).write_text(json.dumps(public, indent=1) + "\n", encoding="utf-8")
    return public
