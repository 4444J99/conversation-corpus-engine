"""The prompt feed: one row per ask the person typed, with a receipt that binds it to the archive.

`cce archive project feed` reads the normalized layer and writes, per provider, a feed file and a
receipt beside it. The feed carries private conversation text, so it is only ever written to a
directory that Git cannot see (see `corpus_store.require_git_private`).

Rules the writer follows, each tested:

* One row per eligible ask: role `user`, channel `typed`, a conversation of kind `chat`, `search`
  or `activity`. Asks on the current branch come first, then the conversation's off-branch asks
  (ChatGPT edits and regenerations), which carry `branch: alternate`.
* Projects, memories and notebooks are excluded, and every excluded message is counted by reason.
  Every message the reader kept is accounted for: `messages_seen == rows + sum(excluded)`.
* The text of an ask is what the person typed. Text a reader appended from an attachment is cut
  off at `typed_chars`.
* The provenance of every row is the module constant `FEED_PROVENANCE`. The writer takes no
  provenance argument, so a caller cannot promote a vendor export to operator-typed input.
* Rows are written in conversation order, then message order, with sorted keys, so two runs give
  identical bytes. A repeated `event_ref` aborts the write and leaves no feed behind.
* The receipt holds counts, a digest of the feed and the manifest's source fingerprint; it holds
  no conversation text and no clock reading.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import archive_layout as layout
from .archive_ingest import ArchivePaths, latest_activity_day, load_manifest
from .archive_model import (
    Conversation,
    Message,
    iter_conversations,
    message_reference,
    write_json_atomic,
)
from .archive_naming import (
    NAMING_VERSION,
    InvalidName,
    parse_name,
    validate_name,
)
from .corpus_store import require_git_private

FEED_RECEIPT_SCHEMA = "archive-feed-receipt.v1"
# The one provenance a vendor export may claim. It is a constant on purpose: nothing that calls
# the writer can choose another.
FEED_PROVENANCE = "unknown_user_input"
FEED_BODY_KIND = "direct"
ELIGIBLE_KINDS = frozenset({"chat", "search", "activity"})
ELIGIBLE_ROLE = "user"
ELIGIBLE_CHANNEL = "typed"
# Every key of a feed row except `branch`, which only off-branch rows carry.
ROW_FIELDS = (
    "body_kind",
    "event_index",
    "event_ref",
    "provenance",
    "session_ref",
    "source",
    "source_locator",
    "text",
    "text_index",
    "timestamp",
)
ALTERNATE_FLAG = "alternate"


class FeedError(ValueError):
    """The feed cannot be written faithfully; nothing was left behind."""


def feed_source(provider: str) -> str:
    """The `source` family every row of a provider's feed carries: `archive-<provider>-messages`."""
    return f"archive-{provider}-messages"


def _ask_text(message: Message) -> tuple[str, bool]:
    """What the person typed, and whether attachment text was cut off the end."""
    text, typed = message.text, message.typed_chars
    if isinstance(typed, int) and 0 <= typed < len(text):
        return text[:typed], True
    return text, False


def _exclusion(message: Message, conversation_kind: str) -> str:
    """Why a message is not an ask, or the empty string when it is a candidate."""
    if conversation_kind not in ELIGIBLE_KINDS:
        return f"kind-{conversation_kind}"
    if message.role != ELIGIBLE_ROLE:
        return f"role-{message.role}"
    if message.channel != ELIGIBLE_CHANNEL:
        return f"channel-{message.channel or 'none'}"
    return ""


def _candidates(conversation: Conversation) -> list[tuple[Message, bool]]:
    """Current-branch messages in order, then the off-branch ones in stored order."""
    rows = [(message, False) for message in conversation.messages]
    alternates = conversation.extra.get("alternate_messages") or []
    if not isinstance(alternates, list):
        raise FeedError("alternate_messages must be a list")
    rows.extend((Message.from_dict(item), True) for item in alternates if isinstance(item, dict))
    return rows


def _line(row: dict[str, Any]) -> bytes:
    return (json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _stale_receipts(out: Path, provider: str, keep: str) -> list[Path]:
    """Receipts of this provider's prompt feed other than `keep`: they describe a feed that no
    longer exists."""
    stale: list[Path] = []
    for entry in sorted(out.iterdir()):
        if entry.suffix != ".json" or entry.name == keep:
            continue
        stem = entry.name.removesuffix(".json")
        if validate_name(stem):
            continue
        parts = parse_name(stem)
        if parts.kind == "receipt" and parts.scope == provider and parts.subject == "prompts":
            stale.append(entry)
    return stale


@dataclass
class _Staged:
    """A feed written to a temporary file, and the receipt that will sit beside it."""

    provider: str
    temp: Path
    feed_path: Path
    receipt_path: Path
    receipt: dict[str, Any]


def _stage_provider(
    paths: ArchivePaths,
    provider: str,
    manifest: dict[str, Any],
    out: Path,
    day: str,
) -> _Staged:
    info = manifest["providers"][provider]
    source = feed_source(provider)
    feed_path = out / layout.feed_filename(provider)
    temp = feed_path.with_name(f".{feed_path.name}.tmp")

    refs: set[str] = set()
    excluded: Counter[str] = Counter()
    kinds: Counter[str] = Counter()
    digest = hashlib.sha256()
    conversations = main_messages = seen = rows = alternate_rows = trimmed = 0

    try:
        with temp.open("wb") as handle:
            for conversation in iter_conversations(paths.conversations(provider)):
                conversations += 1
                kinds[conversation.kind] += 1
                main_messages += len(conversation.messages)
                uid = conversation.uid
                for message, alternate in _candidates(conversation):
                    seen += 1
                    reason = _exclusion(message, conversation.kind)
                    if reason:
                        excluded[reason] += 1
                        continue
                    text, was_trimmed = _ask_text(message)
                    if not text.strip():
                        excluded["empty-text"] += 1
                        continue
                    ref = message_reference(provider, uid, message.id)
                    if ref in refs:
                        raise FeedError(f"repeated event_ref {ref} in the {provider} feed")
                    refs.add(ref)
                    row: dict[str, Any] = {
                        "body_kind": FEED_BODY_KIND,
                        "event_index": rows,
                        "event_ref": ref,
                        "provenance": FEED_PROVENANCE,
                        "session_ref": uid,
                        "source": source,
                        "source_locator": f"{uid}#{ref}",
                        "text": text,
                        "text_index": 0,
                        "timestamp": message.ts,
                    }
                    if alternate:
                        row["branch"] = ALTERNATE_FLAG
                        alternate_rows += 1
                    line = _line(row)
                    handle.write(line)
                    digest.update(line)
                    rows += 1
                    trimmed += was_trimmed
        if conversations != info.get("conversations") or main_messages != info.get("messages"):
            raise FeedError(
                f"the {provider} store disagrees with the manifest "
                f"({conversations} conversations, {main_messages} messages; the manifest says "
                f"{info.get('conversations')} and {info.get('messages')}): run `cce archive ingest`"
            )
        if seen != rows + sum(excluded.values()):  # pragma: no cover - guards the loop above
            raise FeedError(f"the {provider} counts do not reconcile")
    except BaseException:
        temp.unlink(missing_ok=True)
        raise

    fingerprint = manifest["source_fingerprint"]
    receipt = {
        "schema": FEED_RECEIPT_SCHEMA,
        "provider": provider,
        "source": source,
        "provenance": FEED_PROVENANCE,
        "feed": feed_path.name,
        "feed_sha256": digest.hexdigest(),
        "rows": rows,
        "alternate_rows": alternate_rows,
        "attachment_text_trimmed": trimmed,
        "messages_seen": seen,
        "excluded": dict(sorted(excluded.items())),
        "conversations_seen": conversations,
        "conversations_by_kind": dict(sorted(kinds.items())),
        "source_fingerprint": fingerprint,
        "day": day,
        "naming_version": NAMING_VERSION,
        "schema_version": manifest.get("schema_version"),
        "reader_versions": dict(sorted(manifest.get("reader_versions", {}).items())),
    }
    receipt_path = out / layout.receipt_filename(provider, fingerprint, day)
    return _Staged(provider, temp, feed_path, receipt_path, receipt)


def _commit(staged: _Staged, out: Path) -> dict[str, Any]:
    staged.temp.replace(staged.feed_path)
    write_json_atomic(staged.receipt_path, staged.receipt)
    for old in _stale_receipts(out, staged.provider, staged.receipt_path.name):
        old.unlink()
    return {
        "feed": staged.feed_path.name,
        "receipt": staged.receipt_path.name,
        **_summary(staged.receipt),
    }


def _summary(receipt: dict[str, Any]) -> dict[str, Any]:
    return {
        key: receipt[key]
        for key in ("rows", "alternate_rows", "messages_seen", "excluded", "feed_sha256")
    }


def project_feed(
    root: Path, out: Path, providers: list[str] | None = None
) -> dict[str, dict[str, Any]]:
    """Write each provider's feed and receipt into `out`; return a counts-only summary."""
    paths = ArchivePaths(root)
    try:
        manifest = load_manifest(paths)
    except FileNotFoundError as exc:
        raise FeedError(str(exc)) from exc
    if not manifest.get("source_fingerprint"):
        raise FeedError("the manifest has no source fingerprint: run `cce archive ingest` again")
    try:
        day = latest_activity_day(manifest)
    except InvalidName as exc:
        raise FeedError("the manifest holds no activity day to date the receipts") from exc
    ready = [
        name
        for name, info in manifest["providers"].items()
        if info.get("status") == "ok" and paths.conversations(name).is_file()
    ]
    chosen = providers or ready
    unready = [name for name in chosen if name not in ready]
    if unready:
        raise FeedError(f"no readable normalized store for: {', '.join(sorted(unready))}")
    out = require_git_private(out)
    out.mkdir(parents=True, exist_ok=True)
    staged: list[_Staged] = []
    try:
        for name in chosen:
            staged.append(_stage_provider(paths, name, manifest, out, day))
    except BaseException:
        for item in staged:
            item.temp.unlink(missing_ok=True)
        raise
    return {item.provider: _commit(item, out) for item in staged}
