"""Recover ChatGPT conversations from the desktop app's local HTTP cache.

The ChatGPT/Codex desktop app caches the JSON it fetched from the person's own account. This
module reads those cache files (never the network, cookies or tokens; see ``app_cache_chromium``)
and writes a staging root the existing ChatGPT reader ingests unchanged:

* ``conversations-000.json`` - every conversation whose full ``mapping`` was cached, newest cached
  version per id, in the shape of an export ``conversations-NNN.json`` shard;
* ``cache-index.json`` - one metadata row per conversation id seen anywhere in the cache (list
  pages, metadata-only fetches, full bodies), with ``body_cached`` saying whether a full body exists;
* ``coverage.json`` - counts and date ranges only (no titles), the honest statement of what the
  cache could and could not give.

Message-page fetches (``/messages``) hold only a window of a conversation, so they are counted and
never presented as a conversation. A body that cannot be decoded is counted under its reason.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .app_cache_chromium import (
    BodyUnavailable,
    CacheEntry,
    Skipped,
    brotli_decoder,
    iter_entries,
    read_body,
)

SCHEMA = "cce-chatgpt-app-cache/v1"
SHARD_NAME = "conversations-000.json"
INDEX_NAME = "cache-index.json"
COVERAGE_NAME = "coverage.json"
DEFAULT_ROOT_SUFFIXES = (
    "Default/Cache/Cache_Data",
    "Default/Partitions/codex-browser-app/Cache/Cache_Data",
)
HOSTS = {"chatgpt.com", "chat.openai.com"}
UUIDISH = r"[0-9a-fA-F-]{8,64}"
CONVERSATION = re.compile(rf"^/backend-api/conversations?/({UUIDISH})$")
MESSAGES = re.compile(rf"^/backend-api/conversations?/({UUIDISH})/messages$")
LISTING = re.compile(r"^/backend-api/conversations$")


@dataclass
class IndexRow:
    id: str
    title: str = ""
    create_time: Any = None
    update_time: Any = None
    body_cached: bool = False
    sources: set[str] = field(default_factory=set)

    def merge(self, title: Any, create: Any, update: Any, source: str) -> None:
        if _stamp(update) >= _stamp(self.update_time):
            self.title = str(title or self.title or "")
            self.update_time = update if update is not None else self.update_time
        if self.create_time is None and create is not None:
            self.create_time = create
        self.sources.add(source)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "create_time": self.create_time,
            "update_time": self.update_time,
            "body_cached": self.body_cached,
            "sources": sorted(self.sources),
        }


@dataclass
class ScanResult:
    roots: list[str] = field(default_factory=list)
    conversations: dict[str, dict[str, Any]] = field(default_factory=dict)
    index: dict[str, IndexRow] = field(default_factory=dict)
    message_page_conversations: set[str] = field(default_factory=set)
    message_pages: int = 0
    entries_seen: int = 0
    entries_skipped: Counter[str] = field(default_factory=Counter)
    bodies_unavailable: Counter[str] = field(default_factory=Counter)
    relevant_entries: int = 0


def default_roots(home: Path) -> list[Path]:
    base = home / "Library" / "Caches" / "Codex"
    return [base / suffix for suffix in DEFAULT_ROOT_SUFFIXES]


def _stamp(value: Any) -> float:
    """A cached time is an epoch number in a full body and an ISO string in a list page."""
    if isinstance(value, bool) or value is None:
        return 0.0
    if isinstance(value, int | float):
        return float(value)
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _iso(value: Any) -> str:
    stamp = _stamp(value)
    return datetime.fromtimestamp(stamp, UTC).strftime("%Y-%m-%d") if stamp else ""


def _relevant_path(entry: CacheEntry) -> str | None:
    parts = urlsplit(entry.url)
    if (parts.hostname or "") not in HOSTS:
        return None
    return parts.path


def _json(entry: CacheEntry, brotli: Callable[[bytes], bytes] | None) -> Any:
    return json.loads(read_body(entry, brotli).decode("utf-8"))


AUTO: Any = object()


def scan(roots: Iterable[Path], brotli: Callable[[bytes], bytes] | None | Any = AUTO) -> ScanResult:
    """Read every cache entry under ``roots`` and collect what conversations the cache holds."""
    decoder = brotli_decoder() if brotli is AUTO else brotli
    result = ScanResult()
    for root in roots:
        result.roots.append(root.name if not root.is_dir() else str(root))
        for entry in iter_entries(root):
            result.entries_seen += 1
            if isinstance(entry, Skipped):
                result.entries_skipped[entry.reason] += 1
                continue
            path = _relevant_path(entry)
            if path is None or entry.status != 200 or not entry.body_len:
                continue
            _collect(result, entry, path, decoder)
    return result


def _collect(
    result: ScanResult, entry: CacheEntry, path: str, brotli: Callable[[bytes], bytes] | None
) -> None:
    messages = MESSAGES.match(path)
    conversation = CONVERSATION.match(path)
    if not (messages or conversation or LISTING.match(path)):
        return
    result.relevant_entries += 1
    if messages:
        result.message_pages += 1
        result.message_page_conversations.add(messages.group(1))
        return
    try:
        body = _json(entry, brotli)
    except BodyUnavailable as reason:
        result.bodies_unavailable[str(reason)] += 1
        return
    except (UnicodeDecodeError, ValueError):
        result.bodies_unavailable["not-json"] += 1
        return
    if not isinstance(body, dict):
        return
    if conversation:
        _collect_conversation(result, conversation.group(1), body, path)
    else:
        for item in body.get("items") or []:
            if isinstance(item, dict) and item.get("id"):
                row = result.index.setdefault(str(item["id"]), IndexRow(str(item["id"])))
                row.merge(
                    item.get("title"), item.get("create_time"), item.get("update_time"), "list"
                )


def _collect_conversation(result: ScanResult, cid: str, body: dict[str, Any], path: str) -> None:
    if "title" not in body and "mapping" not in body:
        return
    row = result.index.setdefault(cid, IndexRow(cid))
    full = isinstance(body.get("mapping"), dict) and bool(body["mapping"])
    row.merge(
        body.get("title"),
        body.get("create_time"),
        body.get("update_time"),
        "full" if full else "metadata",
    )
    if not full:
        return
    row.body_cached = True
    held = result.conversations.get(cid)
    if held is None or _stamp(body.get("update_time")) >= _stamp(held.get("update_time")):
        stored = dict(body)
        stored.setdefault("conversation_id", cid)
        stored.setdefault("id", cid)
        result.conversations[cid] = stored


def coverage(result: ScanResult, today: datetime | None = None) -> dict[str, Any]:
    """Counts and date ranges only; no titles, ids or text."""
    full_days = sorted(
        d for d in (_iso(c.get("update_time")) for c in result.conversations.values()) if d
    )
    index_days = Counter(
        _iso(row.update_time)[:7] for row in result.index.values() if _iso(row.update_time)
    )
    metadata_only = [row for row in result.index.values() if not row.body_cached]
    return {
        "schema": SCHEMA,
        "generated_at": (today or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "roots_read": len(result.roots),
        "cache_entries_seen": result.entries_seen,
        "cache_entries_skipped": dict(sorted(result.entries_skipped.items())),
        "conversation_entries_relevant": result.relevant_entries,
        "bodies_unavailable": dict(sorted(result.bodies_unavailable.items())),
        "conversations_with_full_body": len(result.conversations),
        "full_body_updated_first": full_days[0] if full_days else None,
        "full_body_updated_last": full_days[-1] if full_days else None,
        "conversations_known_by_metadata_only": len(metadata_only),
        "conversations_known_total": len(result.index),
        "metadata_updated_by_month": dict(sorted(index_days.items())),
        "message_page_fetches": result.message_pages,
        "conversations_with_message_pages": len(result.message_page_conversations),
        "message_pages_note": "windows of a conversation; not ingested as conversations",
    }


def write_staging(result: ScanResult, out: Path) -> dict[str, Any]:
    """Write the staging root. ``out`` must be new or empty; nothing existing is overwritten."""
    out = out.expanduser().absolute()
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise FileExistsError(f"staging root already exists and is not empty: {out}")
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    ordered = sorted(
        result.conversations.values(), key=lambda c: (_stamp(c.get("update_time")), c["id"])
    )
    (out / SHARD_NAME).write_text(json.dumps(ordered, ensure_ascii=False), encoding="utf-8")
    index = [row.as_dict() for _, row in sorted(result.index.items())]
    (out / INDEX_NAME).write_text(
        json.dumps({"schema": SCHEMA, "conversations": index}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    report = coverage(result)
    (out / COVERAGE_NAME).write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    return report
