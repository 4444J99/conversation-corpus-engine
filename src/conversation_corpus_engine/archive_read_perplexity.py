from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .archive_model import Conversation, Message, SourceTracker, clean_text, to_iso

PROVIDER = "perplexity"
READER = "archive_read_perplexity@1"


def conversation_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob("conversations-*.json") if p.is_file())


def detect(root: Path) -> bool:
    return bool(conversation_files(root))


def channels(root: Path) -> dict[str, list[str]]:
    read = [f"{len(conversation_files(root))} conversations file(s)"]
    not_read = [
        f"{p.name} — account data (deliberately excluded)"
        for p in sorted(root.glob("user-data-*.xlsx"))
    ]
    not_read += [
        f"{p.name} — original export archive (duplicate of the extracted files)"
        for p in sorted(root.glob("*.zip"))
    ]
    return {"read": read, "not_read": not_read}


def _conversation(raw: dict[str, Any], tracker: SourceTracker, source: Path) -> Conversation:
    messages: list[Message] = []
    modes: Counter[str] = Counter()
    for entry in raw.get("entries") or []:
        ts = to_iso(entry.get("created_at"))
        meta = {"kind": "text", "status": str(entry.get("query_status") or "")}
        if entry.get("engine_mode"):
            modes[str(entry["engine_mode"])] += 1
        if entry.get("query"):
            messages.append(Message(role="user", text=clean_text(entry["query"]), ts=ts, meta=meta))
        if entry.get("answer"):
            messages.append(
                Message(role="assistant", text=clean_text(entry["answer"]), ts=ts, meta=meta)
            )
    first_query = next((m.text for m in messages if m.role == "user"), "")
    title = str(raw.get("context_title") or "").strip() or first_query[:80] or "Untitled"
    return Conversation(
        provider=PROVIDER,
        source_id=str(raw.get("context_uuid") or ""),
        title=title,
        created=to_iso(raw.get("created_at")),
        updated=to_iso(raw.get("updated_at")),
        kind="search",
        collection=str(raw.get("collection_uuid") or ""),
        model=modes.most_common(1)[0][0] if modes else "",
        messages=messages,
        provenance=tracker.provenance(source),
        extra={"mode": str(raw.get("mode") or ""), "entries": len(raw.get("entries") or [])},
    )


def iter_conversations(root: Path, tracker: SourceTracker) -> Iterator[Conversation]:
    for source in conversation_files(root):
        payload = json.loads(source.read_text(encoding="utf-8"))
        records = payload.get("conversations", []) if isinstance(payload, dict) else payload
        for raw in records:
            yield _conversation(raw, tracker, source)
