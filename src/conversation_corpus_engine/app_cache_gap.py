"""``cce app-cache gap``: what the staged app caches add to an archive, and what is still missing.

The archive's index shards say, per provider, which conversations it holds and how recent they
are. Each staged directory (from ``cce app-cache chatgpt|perplexity|gemini``) says what the
provider's desktop app still holds locally. Comparing the two answers three questions without
reading any message text:

* which staged conversations are new to the archive or newer than the archived copy, and which of
  those have a full body (can be ingested now) versus metadata only (need the provider's export);
* how far each provider's archive ends and how far the staged data reaches;
* for what is missing, which export the owner has to take.

Read-only against the archive and the staged directories. The per-conversation lists carry ids and
titles, so the report goes only into a new private ``--out`` directory (mode 0700, files 0600);
stdout and the coverage file carry counts and dates only.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .app_cache_output import prepare_directory, write_private
from .archive_ingest import ArchivePaths
from .archive_model import iter_jsonl, stable_uid

SCHEMA = "cce-app-cache-gap/v1"
REPORT_NAME = "gap-report.json"
COVERAGE_NAME = "coverage.json"

OWNER_EXPORT = {
    "chatgpt": (
        "ChatGPT: Settings > Data controls > Export data. The e-mailed zip holds conversations-*.json, "
        "which archive_read_chatgpt ingests. The desktop app's HTTP cache holds full bodies for only "
        "the conversations opened recently."
    ),
    "perplexity": (
        "Perplexity: the account's own data export. The desktop app's cache does not store the "
        "person's typed prompts, so no local file can supply them."
    ),
    "gemini": (
        "Gemini: Google Takeout, product My Activity > Gemini Apps, format JSON "
        "(My Activity/Gemini Apps/MyActivity.json). The app's ChatInfo2.store bodies are an "
        "encrypted container and are not read."
    ),
}


class GapError(ValueError):
    """A staged directory or the archive is not in the shape this comparison needs."""


def _parse(value: Any) -> datetime | None:
    """A timestamp in any shape the stores use (ISO text or epoch seconds), as aware UTC."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), UTC)
    text = str(value).strip()
    try:
        return datetime.fromtimestamp(float(text), UTC)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _day(moment: datetime | None) -> str | None:
    return moment.date().isoformat() if moment else None


def archive_provider(archive_root: Path, provider: str) -> dict[str, Any]:
    """What the archive holds for one provider: uid -> last activity, plus the overall end.

    The cutoff is ``last_activity`` (when the conversation last had a message); ``updated`` is the
    source's metadata stamp and only fills in when no activity time exists.
    """
    paths = ArchivePaths(archive_root.expanduser().absolute())
    held: dict[str, datetime | None] = {}
    try:
        for shard in paths.index_files(provider):
            for row in iter_jsonl(shard):
                held[str(row["uid"])] = _parse(row.get("last_activity")) or _parse(
                    row.get("updated")
                )
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise GapError(f"cannot read the {provider} index of {archive_root}: {error}") from error
    latest = max((t for t in held.values() if t), default=None)
    return {"held": held, "conversations": len(held), "last_activity_max": latest}


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise GapError(f"cannot read staged file {path}: {error}") from error


def _classify(
    entry_time: datetime | None,
    archived: datetime | None,
    present: bool,
    archive_end: datetime | None,
) -> str:
    """``new``: the archive has no such uid. ``newer``: the archive has it and the app saw activity
    after the archive's end. A held conversation whose app time is later than its archived time but
    not past the archive's end is timestamp drift (list pages restamp many conversations at once),
    not new activity, so it counts as covered."""
    if not present:
        return "new"
    if entry_time and archive_end and entry_time > archive_end:
        return "newer"
    return "covered"


def _body_times(staged: Path) -> dict[str, datetime | None]:
    """When each staged full body was last updated. Only ids and times are kept; the message
    mapping is parsed with the file and dropped."""
    path = staged / "conversations-000.json"
    if not path.is_file():
        return {}
    bodies = _read_json(path)
    if not isinstance(bodies, list):
        raise GapError(f"{path} is not a list of conversations")
    return {
        str(b.get("conversation_id") or b.get("id")): _parse(b.get("update_time"))
        for b in bodies
        if isinstance(b, dict)
    }


def chatgpt_gap(archive: dict[str, Any], staged: Path) -> dict[str, Any]:
    """Compare ``cache-index.json`` (every conversation the app knows) with the archive's uids.

    A conversation counts as having a usable body only when a body was staged and that body is
    itself newer than the archive's end (or the conversation is new): a stale body under newer list
    metadata is metadata-only for this purpose.
    """
    index = _read_json(staged / "cache-index.json")
    entries = index.get("conversations") if isinstance(index, dict) else None
    if not isinstance(entries, list):
        raise GapError(f"{staged / 'cache-index.json'} has no conversations list")
    body_times = _body_times(staged)
    held: dict[str, datetime | None] = archive["held"]
    end = archive["last_activity_max"]
    buckets: dict[str, list[dict[str, Any]]] = {
        "new_with_body": [],
        "newer_with_body": [],
        "new_metadata_only": [],
        "newer_metadata_only": [],
    }
    covered = 0
    for entry in entries:
        conversation_id = str(entry.get("id") or "")
        uid = stable_uid("chatgpt", conversation_id)
        updated = _parse(entry.get("update_time"))
        state = _classify(updated, held.get(uid), uid in held, end)
        if state == "covered":
            covered += 1
            continue
        body_time = body_times.get(conversation_id)
        fresh = bool(entry.get("body_cached")) and (
            state == "new" or (body_time is not None and end is not None and body_time > end)
        )
        buckets[f"{state}_{'with_body' if fresh else 'metadata_only'}"].append(
            {
                "id": conversation_id,
                "title": entry.get("title"),
                "updated": _day(updated),
                "archived_through": _day(held.get(uid)),
            }
        )
    return _summary("chatgpt", archive, entries, buckets, covered)


def _summary(
    provider: str,
    archive: dict[str, Any],
    entries: list[dict[str, Any]],
    buckets: dict[str, list[dict[str, Any]]],
    covered: int,
) -> dict[str, Any]:
    updated = sorted(d for d in (b["updated"] for rows in buckets.values() for b in rows) if d)
    needing_export = len(buckets.get("new_metadata_only", [])) + len(
        buckets.get("newer_metadata_only", [])
    )
    return {
        "provider": provider,
        "archive_conversations": archive["conversations"],
        "archive_last_activity": _day(archive["last_activity_max"]),
        "staged_known": len(entries),
        "already_covered": covered,
        "counts": {name: len(rows) for name, rows in buckets.items()},
        "ingestible_now": len(buckets.get("new_with_body", []))
        + len(buckets.get("newer_with_body", [])),
        "needs_owner_export": needing_export,
        "gap_updated_first": updated[0] if updated else None,
        "gap_updated_last": updated[-1] if updated else None,
        "gap_updated_by_month": dict(sorted(Counter(d[:7] for d in updated).items())),
        "owner_export": OWNER_EXPORT[provider],
        "rows": buckets,
    }


def gemini_gap(archive: dict[str, Any], staged: Path) -> dict[str, Any]:
    """Compare the Gemini store inventory (ids and times only) with the archive's gemini uids.

    Ids are matched exactly through the archive's own uid function. A chat the archive lacks is
    new whatever its date; a held chat counts as newer only when the app saw it after the
    archive's end. Neither can be ingested: the bodies are encrypted.
    """
    payload = _read_json(staged / "gemini-store-inventory.json")
    inventory = payload.get("inventory") if isinstance(payload, dict) else None
    if not isinstance(inventory, list):
        raise GapError(f"{staged / 'gemini-store-inventory.json'} has no inventory list")
    held = archive["held"]
    end = archive["last_activity_max"]
    buckets: dict[str, list[dict[str, Any]]] = {"new_metadata_only": [], "newer_metadata_only": []}
    covered = 0
    for chat in inventory:
        uids = {
            stable_uid("gemini", str(chat[key]))
            for key in ("chat_uuid", "robin_conversation_id")
            if chat.get(key)
        }
        present = any(uid in held for uid in uids)
        updated = _parse(chat.get("last_updated"))
        state = _classify(updated, None, present, end)
        if state == "covered":
            covered += 1
            continue
        buckets[f"{state}_metadata_only"].append(
            {
                "id": chat.get("chat_uuid"),
                "updated": _day(updated),
                "cached_messages": chat.get("cached_messages"),
            }
        )
    return _summary("gemini", archive, inventory, buckets, covered)


def perplexity_gap(archive: dict[str, Any], staged: Path) -> dict[str, Any]:
    """Staged Perplexity records are app-generated (notes, follow-ups), never the person's prompts.

    They carry synthetic context ids, so only dates are compared: records dated after the
    archive's end are new material that the reader can ingest; the person's own threads after
    that date exist only in Perplexity's export.
    """
    payload = _read_json(staged / "conversations-000.json")
    records = payload.get("conversations", []) if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise GapError(f"{staged / 'conversations-000.json'} has no conversations list")
    end = archive["last_activity_max"]
    new: list[dict[str, Any]] = []
    for record in records:
        updated = _parse(record.get("updated_at") or record.get("created_at"))
        if updated and (end is None or updated > end):
            new.append(
                {
                    "id": record.get("context_uuid"),
                    "title": record.get("context_title"),
                    "updated": _day(updated),
                }
            )
    covered = len(records) - len(new)
    summary = _summary("perplexity", archive, records, {"new_with_body": new}, covered)
    summary["needs_owner_export"] = None
    summary["owner_typed_prompts_after_archive_end"] = "only in the account export"
    return summary


READERS = {"chatgpt": chatgpt_gap, "perplexity": perplexity_gap, "gemini": gemini_gap}


def build_report(archive_root: Path, staged: dict[str, Path]) -> dict[str, Any]:
    """One comparison per provider whose staged directory was given."""
    if not staged:
        raise GapError("give at least one staged directory (--chatgpt, --perplexity, --gemini)")
    providers: dict[str, Any] = {}
    for provider, directory in staged.items():
        if not directory.is_dir():
            raise GapError(f"staged directory does not exist: {directory}")
        archive = archive_provider(archive_root, provider)
        if not archive["conversations"]:
            raise GapError(f"the archive at {archive_root} holds no {provider} conversations")
        providers[provider] = READERS[provider](archive, directory)
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "providers": providers,
    }


def write_report(report: dict[str, Any], out: Path) -> dict[str, Any]:
    """Write the full report (ids, titles) and a coverage file with counts and dates only."""
    out = prepare_directory(out)
    write_private(out / REPORT_NAME, json.dumps(report, indent=1, ensure_ascii=False))
    coverage = {
        **{k: v for k, v in report.items() if k not in ("providers", "archive")},
        "providers": {
            name: {k: v for k, v in info.items() if k != "rows"}
            for name, info in report["providers"].items()
        },
    }
    write_private(out / COVERAGE_NAME, json.dumps(coverage, indent=1, ensure_ascii=False) + "\n")
    return coverage
