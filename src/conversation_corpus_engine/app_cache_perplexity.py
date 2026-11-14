"""Recover what the Perplexity macOS app cached locally, from cache files alone.

The app's ``Cache.db`` (an NSURLCache SQLite store, with body files in ``fsCachedData``) holds the
responses the app fetched for the person's own account. Two kinds carry conversation-like text:

* Space knowledge notes (the GraphQL ``knowledgeContext`` payload): the note text, its name and
  its update time, owned by a Space;
* follow-up suggestions (``/rest/sse/related-queries/<thread uuid>``): what Perplexity proposed
  next for a thread. These are Perplexity-generated, never the person's own words, and are tagged
  that way so nothing downstream reads them as the person's prompts.

Nothing here makes a network request or reads cookies, tokens or any credential store. The
database is read from a private copy (``app_cache_sqlite``). The output is a ``conversations-000.json``
shard in the Perplexity export shape that ``archive_read_perplexity`` ingests unchanged, plus a
titles-free ``coverage.json``. The shared clipboard history is deliberately not read.
"""

from __future__ import annotations

import base64
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .app_cache_output import prepare_directory, write_private
from .app_cache_sqlite import SnapshotError, snapshot

__all__ = ["SnapshotError"]
SCHEMA = "cce-perplexity-app-cache/v1"
SHARD_NAME = "conversations-000.json"
COVERAGE_NAME = "coverage.json"
CACHE_DB = ("Library", "Caches", "ai.perplexity.macv3", "Cache.db")
SHARED_CONTAINER = ("Library", "Group Containers", "7S8W4W365S.ai.perplexity.macv3.shared")
UUID = r"[0-9a-fA-F-]{8,64}"
RELATED = re.compile(rf"^/rest/sse/related-queries/({UUID})$")
GRAPHQL = re.compile(r"^/api/graphql/")
SSE_DATA = re.compile(r"^data: (.*)$", re.MULTILINE)
SPACE_SECTION = re.compile(r"^space:(" + UUID + r")")
FOLLOWUP_MODE = "COMPUTER_FOLLOWUP"


@dataclass
class ScanResult:
    spaces: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    followups: dict[str, dict[str, Any]] = field(default_factory=dict)
    responses: int = 0
    perplexity_responses: int = 0
    other_perplexity: Counter[str] = field(default_factory=Counter)
    unreadable: Counter[str] = field(default_factory=Counter)
    body_files_missing: int = 0


def default_paths(home: Path) -> tuple[Path, Path]:
    cache = home.joinpath(*CACHE_DB)
    return cache, cache.parent / "fsCachedData"


def _iso(stamp: str) -> str:
    """NSURLCache stamps ``YYYY-MM-DD HH:MM:SS`` in UTC."""
    try:
        return (
            datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
            .replace(tzinfo=UTC)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    except ValueError:
        return ""


def _decode_item_id(item_id: str) -> dict[str, Any]:
    """Item ids are ``KCI:<url-safe base64 of a JSON object>`` naming the section and item."""
    payload = item_id.split(":", 1)[-1]
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        decoded = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _spaces_from_graphql(body: dict[str, Any], result: ScanResult) -> bool:
    scope = ((body.get("viewer") or {}).get("knowledgeContext") or {}).get("scopeBySpaceUUID")
    if not isinstance(scope, dict):
        return False
    for category in scope.get("categories") or []:
        for edge in ((category or {}).get("items") or {}).get("edges") or []:
            node = (edge or {}).get("node") or {}
            ident = _decode_item_id(str(node.get("id") or ""))
            match = SPACE_SECTION.match(str(ident.get("sectionId") or ""))
            text = ((node.get("content") or {}).get("text")) or ""
            if not match or not text.strip():
                continue
            key = str(ident.get("itemId") or node.get("name") or "")
            held = result.spaces.setdefault(match.group(1), {})
            old = held.get(key)
            if old is None or str(node.get("updatedAt") or "") >= str(old.get("updatedAt") or ""):
                held[key] = {
                    "name": str(node.get("name") or key),
                    "group": str(node.get("groupId") or ""),
                    "updatedAt": str(node.get("updatedAt") or ""),
                    "text": text,
                    "truncated": bool((node.get("content") or {}).get("truncated")),
                }
    return True


def _followups_from_sse(raw: str) -> list[str]:
    """Suggestion texts from the first ``data:`` event that carries them."""
    for line in SSE_DATA.findall(raw):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        items = event.get("related_query_items") if isinstance(event, dict) else None
        if isinstance(items, list) and items:
            return [str(i["text"]) for i in items if isinstance(i, dict) and i.get("text")]
        queries = event.get("related_queries") if isinstance(event, dict) else None
        if isinstance(queries, list) and queries:
            return [str(q) for q in queries]
    return []


def _read_body(data: Any, is_file: int, body_dir: Path, result: ScanResult) -> bytes | None:
    if isinstance(data, str):  # SQLite hands back text for a column the app stored as text
        data = data.encode("utf-8")
    if not is_file:
        return bytes(data) if data else None
    name = bytes(data).decode("utf-8", "replace") if data else ""
    if not re.fullmatch(r"[0-9A-Fa-f-]{8,64}", name):
        result.unreadable["bad-body-file-name"] += 1
        return None
    path = body_dir / name
    try:
        return path.read_bytes()
    except OSError:
        result.body_files_missing += 1
        return None


def scan(cache_db: Path, body_dir: Path) -> ScanResult:
    result = ScanResult()
    with snapshot(cache_db) as db:
        rows = db.execute(
            """SELECT r.request_key, r.time_stamp, d.isDataOnFS, d.receiver_data
               FROM cfurl_cache_response r
               LEFT JOIN cfurl_cache_receiver_data d ON d.entry_ID = r.entry_ID
               ORDER BY r.time_stamp, r.entry_ID"""
        ).fetchall()
    for key, stamp, is_file, data in rows:
        result.responses += 1
        parts = urlsplit(str(key))
        if parts.hostname != "www.perplexity.ai":
            continue
        result.perplexity_responses += 1
        related = RELATED.match(parts.path)
        graphql = GRAPHQL.match(parts.path)
        if not (related or graphql):
            result.other_perplexity[re.sub(rf"{UUID}", "<id>", parts.path)[:60]] += 1
            continue
        body = _read_body(data, int(is_file or 0), body_dir, result)
        if not body:
            result.unreadable["empty-or-missing-body"] += 1
            continue
        text = body.decode("utf-8", "replace")
        if related:
            texts = _followups_from_sse(text)
            if texts:
                result.followups[related.group(1)] = {"seen": _iso(str(stamp)), "texts": texts}
            continue
        try:
            payload = json.loads(text)
        except ValueError:
            result.unreadable["graphql-not-json"] += 1
            continue
        if not (isinstance(payload, dict) and _spaces_from_graphql(payload, result)):
            result.other_perplexity["graphql-other"] += 1
    return result


def conversations(result: ScanResult) -> list[dict[str, Any]]:
    """Export-shaped conversations: one per Space (notes) and one per thread (suggestions)."""
    out: list[dict[str, Any]] = []
    for space, notes in sorted(result.spaces.items()):
        ordered = sorted(notes.values(), key=lambda n: (n["updatedAt"], n["name"]))
        entries = [
            {
                "created_at": note["updatedAt"],
                "query_status": "cached-note-truncated" if note["truncated"] else "cached-note",
                "engine_mode": "SPACE_KNOWLEDGE_NOTE",
                "answer": note["text"],
            }
            for note in ordered
        ]
        out.append(
            {
                "context_uuid": f"space-{space}",
                "context_title": f"Space knowledge notes ({space[:8]})",
                "mode": "space-knowledge",
                "collection_uuid": space,
                "created_at": ordered[0]["updatedAt"],
                "updated_at": ordered[-1]["updatedAt"],
                "entries": entries,
            }
        )
    for thread, found in sorted(result.followups.items()):
        out.append(
            {
                "context_uuid": f"followups-{thread}",
                "context_title": f"Perplexity follow-up suggestions ({thread[:8]})",
                "mode": "generated-followups",
                "created_at": found["seen"],
                "updated_at": found["seen"],
                "entries": [
                    {
                        "created_at": found["seen"],
                        "query_status": "cached-suggestions",
                        "engine_mode": FOLLOWUP_MODE,
                        "answer": "Perplexity-generated follow-up suggestions for thread "
                        f"{thread}:\n" + "\n".join(f"- {t}" for t in found["texts"]),
                    }
                ],
            }
        )
    return out


def coverage(result: ScanResult, shared_container: Path | None = None) -> dict[str, Any]:
    note_times = sorted(
        n["updatedAt"][:10] for notes in result.spaces.values() for n in notes.values()
    )
    seen = sorted(f["seen"][:10] for f in result.followups.values() if f["seen"])
    clipboard = 0
    if shared_container is not None:
        folder = shared_container / "clipboard-history"
        clipboard = sum(1 for p in folder.rglob("*") if p.is_file()) if folder.is_dir() else 0
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cache_responses": result.responses,
        "perplexity_responses": result.perplexity_responses,
        "spaces_with_notes": len(result.spaces),
        "space_notes": sum(len(n) for n in result.spaces.values()),
        "space_notes_updated_first": note_times[0] if note_times else None,
        "space_notes_updated_last": note_times[-1] if note_times else None,
        "threads_with_followup_suggestions": len(result.followups),
        "followups_seen_first": seen[0] if seen else None,
        "followups_seen_last": seen[-1] if seen else None,
        "other_perplexity_responses_not_read": dict(result.other_perplexity.most_common(12)),
        "unreadable": dict(result.unreadable),
        "body_files_missing": result.body_files_missing,
        "not_read": {
            "shared_clipboard_history_files": clipboard,
            "note": "clipboard history is the system clipboard, not conversations; never read",
            "owner_typed_prompts": "not stored in these caches; only Perplexity's own export has them",
        },
    }


def write_staging(result: ScanResult, out: Path, shared_container: Path | None = None) -> dict:
    out = prepare_directory(out)
    payload = {"conversations": conversations(result)}
    write_private(out / SHARD_NAME, json.dumps(payload, ensure_ascii=False))
    report = coverage(result, shared_container)
    write_private(out / COVERAGE_NAME, json.dumps(report, indent=1) + "\n")
    return report
