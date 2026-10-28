from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .answering import shorten, slugify
from .archive_naming import (
    PROVIDER_SCOPES,
    InvalidName,
    conversation_name,
    derive_name,
    hash_identity,
    parse_name,
    validate_name,
)

SCHEMA = "archive-conversation.v2"
LEGACY_SCHEMA = "cce.archive.conversation.v1"
ACCEPTED_SCHEMAS = frozenset({SCHEMA, LEGACY_SCHEMA})
ROLES = ("user", "assistant", "system", "tool")
KINDS = ("chat", "search", "activity", "notebook", "memory")
CHANNELS = ("typed", "reply", "thinking", "tool", "system", "memory", "project")
SNIPPET_LIMIT = 240
DERIVED_ID_PREFIX = "derived"
_PLAIN_ID_RE = re.compile(r"[a-z0-9-]{1,64}")


@dataclass
class Attachment:
    name: str
    kind: str = "file"
    size: int | None = None
    ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "size": self.size, "ref": self.ref}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Attachment:
        return cls(
            name=str(payload.get("name", "")),
            kind=str(payload.get("kind", "file")),
            size=payload.get("size"),
            ref=str(payload.get("ref", "")),
        )


@dataclass
class Message:
    """One message of a normalized conversation.

    `id` is the vendor message id when the export carries one and a minted
    `message-<provider>-<16hex>` name otherwise (see `assign_message_fields`). `channel` says where
    the message came from (one of CHANNELS). `typed_chars` is the number of characters the person
    typed, before any attachment text a reader appended; it equals `len(text)` unless a reader
    says otherwise.
    """

    role: str
    text: str
    ts: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    id: str = ""
    channel: str = ""
    typed_chars: int | None = None

    def __post_init__(self) -> None:
        if self.typed_chars is None:
            self.typed_chars = len(self.text)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "channel": self.channel,
            "ts": self.ts,
            "text": self.text,
            "typed_chars": self.typed_chars,
            "attachments": [item.to_dict() for item in self.attachments],
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Message:
        typed = payload.get("typed_chars")
        return cls(
            role=str(payload.get("role", "user")),
            text=str(payload.get("text", "")),
            ts=str(payload.get("ts", "")),
            attachments=[Attachment.from_dict(item) for item in payload.get("attachments", [])],
            meta=dict(payload.get("meta", {})),
            id=str(payload.get("id", "")),
            channel=str(payload.get("channel", "")),
            typed_chars=typed if isinstance(typed, int) and not isinstance(typed, bool) else None,
        )


@dataclass
class Conversation:
    provider: str
    source_id: str
    title: str
    created: str = ""
    updated: str = ""
    kind: str = "chat"
    collection: str = ""
    model: str = ""
    archived: bool = False
    messages: list[Message] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def uid(self) -> str:
        return stable_uid(self.provider, self.source_id)

    @property
    def message_count(self) -> int:
        return len(self.messages)

    @property
    def word_count(self) -> int:
        return sum(len(message.text.split()) for message in self.messages)

    @property
    def attachment_count(self) -> int:
        return sum(len(message.attachments) for message in self.messages)

    @property
    def last_activity(self) -> str:
        stamps = [message.ts for message in self.messages if message.ts]
        return max(stamps) if stamps else (self.updated or self.created)

    def first_user_text(self) -> str:
        for message in self.messages:
            if message.role == "user" and message.text.strip():
                return message.text
        return ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "uid": self.uid,
            "provider": self.provider,
            "source_id": self.source_id,
            "title": self.title,
            "created": self.created,
            "updated": self.updated,
            "kind": self.kind,
            "collection": self.collection,
            "model": self.model,
            "archived": self.archived,
            "provenance": self.provenance,
            "extra": self.extra,
            "messages": [message.to_dict() for message in self.messages],
        }

    def index_row(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "provider": self.provider,
            "title": self.title,
            "created": self.created,
            "updated": self.updated,
            "last_activity": self.last_activity,
            "kind": self.kind,
            "collection": self.collection,
            "model": self.model,
            "archived": self.archived,
            "message_count": self.message_count,
            "word_count": self.word_count,
            "attachment_count": self.attachment_count,
            "snippet": shorten(self.first_user_text(), SNIPPET_LIMIT),
            "source_file": self.provenance.get("source_file", ""),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Conversation:
        """Load a normalized conversation.

        A version 2 row is taken as written. A version 1 row (or one with no schema string) is
        upgraded: missing message ids, channels and typed character counts are filled in. Any
        other schema string is refused.
        """
        schema = payload.get("schema")
        if schema is not None and schema not in ACCEPTED_SCHEMAS:
            raise ValueError(f"unsupported conversation schema: {schema!r}")
        conversation = cls(
            provider=str(payload["provider"]),
            source_id=str(payload["source_id"]),
            title=str(payload.get("title", "")),
            created=str(payload.get("created", "")),
            updated=str(payload.get("updated", "")),
            kind=str(payload.get("kind", "chat")),
            collection=str(payload.get("collection", "")),
            model=str(payload.get("model", "")),
            archived=bool(payload.get("archived", False)),
            messages=[Message.from_dict(item) for item in payload.get("messages", [])],
            provenance=dict(payload.get("provenance", {})),
            extra=dict(payload.get("extra", {})),
        )
        if schema != SCHEMA:
            assign_message_fields(conversation)
        return conversation


def stable_uid(provider: str, source_id: str) -> str:
    raw = str(source_id)
    if _PLAIN_ID_RE.fullmatch(raw):
        return f"{provider}-{raw}"
    digest = hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]
    return f"{provider}-{slugify(raw, limit=32)}-{digest}"


def derived_source_id(provider: str, created: str, messages: list[Message]) -> str:
    """Identity for a conversation the export gave no usable id: `derived-<16hex>` over the
    provider, the creation stamp and the text of the first three messages."""
    parts = [provider, created, *(message.text for message in messages[:3])]
    return f"{DERIVED_ID_PREFIX}-{hash_identity(chr(0).join(parts))}"


def resolve_source_id(conversation: Conversation) -> str:
    """Give an id-less or unnameable conversation a derived id. Returns why ("" when untouched).

    "empty": the export carried no vendor id (before this, every id-less conversation collapsed
    into one uid). "unnameable": the vendor id is valid for `stable_uid` but cannot be expressed
    as a contract name (an edge or doubled hyphen, or a name over 80 characters); the original is
    kept in `extra["vendor_id"]`. The frozen `stable_uid` is never changed for any other id.
    """
    if conversation.provider not in PROVIDER_SCOPES:
        raise InvalidName(repr(conversation.provider), ["not a known provider word"])
    reason = ""
    if not str(conversation.source_id).strip():
        reason = "empty"
    else:
        try:
            conversation_name(conversation.provider, conversation.uid)
        except InvalidName:
            reason = "unnameable"
    if not reason:
        return ""
    if reason == "unnameable":
        conversation.extra["vendor_id"] = conversation.source_id
    conversation.source_id = derived_source_id(
        conversation.provider, conversation.created, conversation.messages
    )
    conversation.extra["source_id_derived"] = reason
    return reason


def channel_for(message: Message, conversation_kind: str) -> str:
    """Which of CHANNELS a message belongs to, from its role, kind and conversation kind."""
    if conversation_kind == "memory":
        return "memory"
    if conversation_kind == "notebook":
        return "project"
    kind = str(message.meta.get("kind") or "text")
    if message.role == "tool" or kind == "tool":
        return "tool"
    if kind == "reasoning":
        return "thinking"
    if message.role == "user":
        return "typed"
    if message.role == "assistant":
        return "reply"
    return "system"


def message_id_fallback(
    provider: str, uid: str, role: str, ts: str, text: str, ordinal: int = 0
) -> str:
    """Stable id for a message with no vendor id: `message-<provider>-<16hex>` over provider,
    conversation uid, role, timestamp and text. A repeat of the same role, stamp and text inside
    one conversation carries its ordinal inside the hashed string, so repeats stay distinct."""
    parts = [provider, uid, role, ts, text]
    if ordinal:
        parts.append(str(ordinal))
    return derive_name("message", scope=provider, identity=hash_identity("\0".join(parts)))


def assign_message_fields(conversation: Conversation) -> None:
    """Fill every blank message id, channel and typed character count; keep what readers set.

    A vendor id that repeats inside the conversation keeps its first holder; later holders are
    re-minted rather than failing the whole provider. Idempotent.
    """
    provider, uid = conversation.provider, conversation.uid
    used: set[str] = set()
    repeats: Counter[tuple[str, str, str]] = Counter()
    for message in conversation.messages:
        if message.id and message.id not in used:
            used.add(message.id)
        else:
            key = (message.role, message.ts, message.text)
            minted = message_id_fallback(
                provider, uid, message.role, message.ts, message.text, repeats[key]
            )
            while minted in used:
                repeats[key] += 1
                minted = message_id_fallback(
                    provider, uid, message.role, message.ts, message.text, repeats[key]
                )
            repeats[key] += 1
            message.id = minted
            used.add(minted)
        if not message.channel:
            message.channel = channel_for(message, conversation.kind)
        if message.typed_chars is None:
            message.typed_chars = len(message.text)


def finalize_conversation(conversation: Conversation) -> str:
    """Resolve the conversation id, then every message field. Returns the derived-id reason."""
    reason = resolve_source_id(conversation)
    assign_message_fields(conversation)
    return reason


def message_reference(provider: str, uid: str, message_id: str) -> str:
    """The stable `message-<provider>-<16hex>` reference for a message.

    A minted id is already that name. A vendor id is hashed together with the conversation uid
    (separated by a NUL), so the same vendor id in two conversations stays distinct.
    """
    if not validate_name(message_id):
        parts = parse_name(message_id)
        if parts.kind == "message" and parts.scope == provider:
            return message_id
    return derive_name("message", scope=provider, identity=hash_identity(f"{uid}\0{message_id}"))


def to_iso(value: Any) -> str:
    """Normalize epoch seconds/ms, ISO-8601 strings and Mongo `$date` wrappers to UTC `Z` text."""
    if value is None or value == "" or isinstance(value, bool):
        return ""
    if isinstance(value, dict):
        inner = value.get("$date", value.get("$numberLong"))
        if isinstance(inner, dict):
            inner = inner.get("$numberLong")
        return to_iso(inner)
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e11:
            seconds /= 1000.0
        try:
            moment = datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return ""
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    text = str(value).strip()
    if not text:
        return ""
    if re.fullmatch(r"-?\d+(\.\d+)?", text):
        return to_iso(float(text))
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, OverflowError):
        return ""


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")


def normalize_role(value: Any) -> str:
    role = str(value or "").strip().lower()
    if role in {"human", "user"}:
        return "user"
    if role in {"assistant", "ai", "model", "gpt", "grok", "bot"}:
        return "assistant"
    if role in ROLES:
        return role
    return "assistant" if role else "system"


def sha256_file(path: Path, cache: dict[Path, str] | None = None) -> str:
    resolved = path.resolve()
    if cache is not None and resolved in cache:
        return cache[resolved]
    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    value = digest.hexdigest()
    if cache is not None:
        cache[resolved] = value
    return value


class SourceTracker:
    """Per-export provenance: relative source path plus a lazily cached sha256."""

    def __init__(self, root: Path, reader: str) -> None:
        self.root = root.resolve()
        self.reader = reader
        self._hashes: dict[Path, str] = {}
        self.files_read: list[str] = []
        self.counters: Counter[str] = Counter()

    def note(self, name: str, amount: int = 1) -> None:
        self.counters[name] += amount

    def relative(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.root))
        except ValueError:
            return path.name

    def hashes(self) -> dict[str, str]:
        return {self.relative(path): digest for path, digest in sorted(self._hashes.items())}

    def provenance(self, path: Path, *, member: str = "") -> dict[str, Any]:
        rel = self.relative(path)
        if rel not in self.files_read:
            self.files_read.append(rel)
        info: dict[str, Any] = {
            "source_file": rel,
            "source_sha256": sha256_file(path, self._hashes),
            "reader": self.reader,
        }
        if member:
            info["source_member"] = member
        return info


def write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)


def write_json_atomic(path: Path, payload: Any) -> None:
    write_text_atomic(
        path, json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )


def write_jsonl_atomic(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    count = 0
    with temp.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
            count += 1
    temp.replace(path)
    return count


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            stripped = raw.strip()
            if stripped:
                yield json.loads(stripped)


def iter_conversations(path: Path) -> Iterator[Conversation]:
    for row in iter_jsonl(path):
        yield Conversation.from_dict(row)
