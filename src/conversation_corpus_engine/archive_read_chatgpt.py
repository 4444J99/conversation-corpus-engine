from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from .archive_model import (
    Attachment,
    Conversation,
    Message,
    SourceTracker,
    channel_for,
    clean_text,
    normalize_role,
    to_iso,
)

PROVIDER = "chatgpt"
READER = "archive_read_chatgpt@3"
SHARD_RE = re.compile(r"conversations(-\d+)?\.json")
ASSET_MAP_NAME = "conversation_asset_file_names.json"
UNREAD_CHANNELS = (
    ("codex.json", "ChatGPT Codex task threads"),
    ("group_chats.json", "group chats"),
    ("shared_conversations.json", "share-link metadata"),
    ("library_files.json", "file library index"),
    ("pages", "ChatGPT Pages documents"),
    ("sites", "ChatGPT Sites"),
    ("spaces", "ChatGPT Spaces"),
    ("user.json", "account identity (deliberately excluded)"),
    ("user_settings.json", "account settings (deliberately excluded)"),
)


def shard_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_file() and SHARD_RE.fullmatch(p.name))


def detect(root: Path) -> bool:
    return bool(shard_files(root))


def channels(root: Path) -> dict[str, list[str]]:
    read = [f"{len(shard_files(root))} conversation shard file(s)"]
    not_read = [f"{name} — {label}" for name, label in UNREAD_CHANNELS if (root / name).exists()]
    assets = sum(1 for p in root.glob("file*.dat")) if root.is_dir() else 0
    if assets:
        not_read.append(f"{assets} attachment blobs (*.dat) — names referenced, bytes not ingested")
    return {"read": read, "not_read": not_read}


def _asset_name(pointer: str, names: dict[str, str]) -> tuple[str, str]:
    asset_id = pointer.split("://", 1)[-1]
    ref = f"{asset_id}.dat"
    return names.get(ref) or asset_id, ref


def _part_text(part: Any, attachments: list[Attachment], names: dict[str, str]) -> str:
    if isinstance(part, str):
        return part
    if not isinstance(part, dict):
        return ""
    ctype = part.get("content_type", "")
    if ctype == "image_asset_pointer":
        name, ref = _asset_name(str(part.get("asset_pointer", "")), names)
        attachments.append(
            Attachment(name=name, kind="image", size=part.get("size_bytes"), ref=ref)
        )
        return "[image]"
    if ctype == "audio_transcription":
        return str(part.get("text", ""))
    if "asset_pointer" in ctype or "audio" in ctype or "video" in ctype:
        return ""
    return str(part.get("text", "")) if "text" in part else ""


def _content(
    content: dict[str, Any], attachments: list[Attachment], names: dict[str, str]
) -> tuple[str, str]:
    ctype = content.get("content_type") or "text"
    if ctype in {"text", "multimodal_text"}:
        parts = content.get("parts") or []
        pieces = (_part_text(part, attachments, names) for part in parts)
        return "\n\n".join(piece for piece in pieces if piece), "text"
    if ctype == "code":
        body = content.get("text") or ""
        language = content.get("language") or ""
        language = "" if language == "unknown" else language
        return (f"```{language}\n{body}\n```" if body else ""), "code"
    if ctype == "execution_output":
        return str(content.get("text") or ""), "tool"
    if ctype == "thoughts":
        thoughts = content.get("thoughts") or []
        summaries = [
            "\n".join(filter(None, [str(t.get("summary") or ""), str(t.get("content") or "")]))
            for t in thoughts
            if isinstance(t, dict)
        ]
        return "\n\n".join(piece for piece in summaries if piece), "reasoning"
    if ctype == "reasoning_recap":
        return str(content.get("content") or ""), "reasoning"
    if ctype == "tether_quote":
        fields = [str(content.get(key) or "") for key in ("title", "text", "url")]
        return "\n".join(piece for piece in fields if piece), "tool"
    if ctype == "tether_browsing_display":
        return str(content.get("result") or content.get("summary") or ""), "tool"
    if ctype == "user_editable_context":
        return "", "skip"
    text = content.get("text")
    if isinstance(text, str):
        return text, "text"
    parts = content.get("parts") or []
    return "\n\n".join(p for p in (_part_text(x, attachments, names) for x in parts) if p), "text"


def _chain(mapping: dict[str, Any], current: str | None) -> list[dict[str, Any]]:
    if not current or current not in mapping:
        leaves = [
            node for node in mapping.values() if not node.get("children") and node.get("message")
        ]
        leaves.sort(key=lambda node: (node["message"].get("create_time") or 0, node.get("id", "")))
        current = leaves[-1].get("id") if leaves else None
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    while current and current in mapping and current not in seen:
        seen.add(current)
        chain.append(mapping[current])
        current = mapping[current].get("parent")
    chain.reverse()
    return chain


HIDDEN = "hidden"
DROPPED = "dropped"
KEPT = "kept"


def _node_message(node: dict[str, Any], names: dict[str, str]) -> tuple[Message | None, str]:
    """Turn one mapping node into a Message; the second item says what became of the node."""
    message = node["message"]
    meta = message.get("metadata") or {}
    if meta.get("is_visually_hidden_from_conversation"):
        return None, HIDDEN
    author = message.get("author") or {}
    attachments: list[Attachment] = []
    text, kind = _content(message.get("content") or {}, attachments, names)
    for item in meta.get("attachments") or []:
        attachments.append(
            Attachment(
                name=str(item.get("name") or item.get("id") or ""),
                kind=str(item.get("mime_type") or "file"),
                size=item.get("size"),
                ref=str(item.get("id") or ""),
            )
        )
    if kind == "skip" or (not text.strip() and not attachments):
        return None, DROPPED
    entry: dict[str, Any] = {"kind": kind}
    slug = meta.get("model_slug")
    if slug:
        entry["model"] = str(slug)
    if author.get("name"):
        entry["tool"] = str(author["name"])
    return (
        Message(
            role=normalize_role(author.get("role")),
            text=clean_text(text),
            ts=to_iso(message.get("create_time")),
            attachments=attachments,
            meta=entry,
            id=str(node.get("id") or message.get("id") or ""),
        ),
        KEPT,
    )


def _alternates(
    mapping: dict[str, Any], on_chain: set[Any], names: dict[str, str]
) -> list[dict[str, Any]]:
    """Visible messages on branches the person abandoned (regenerations, edits).

    The conversation's own message list stays the `current_node` chain; these are kept beside it,
    each with its own id, the id of the node it hangs from and a `branch` flag.
    """
    nodes = [
        node for node in mapping.values() if node.get("message") and node.get("id") not in on_chain
    ]
    nodes.sort(key=lambda node: (node["message"].get("create_time") or 0, str(node.get("id", ""))))
    rows: list[dict[str, Any]] = []
    for node in nodes:
        message, state = _node_message(node, names)
        if message is None or state != KEPT:
            continue
        message.channel = channel_for(message, "chat")
        row = message.to_dict()
        row["branch"] = "alternate"
        row["parent_id"] = str(node.get("parent") or "")
        rows.append(row)
    return rows


def _conversation(
    raw: dict[str, Any], names: dict[str, str], tracker: SourceTracker, source: Path
) -> Conversation:
    mapping = raw.get("mapping") or {}
    chain = _chain(mapping, raw.get("current_node"))
    on_chain = {node.get("id") for node in chain}
    messages: list[Message] = []
    models: Counter[str] = Counter()
    hidden = dropped = 0
    for node in chain:
        if not node.get("message"):
            continue
        message, state = _node_message(node, names)
        if state == HIDDEN:
            hidden += 1
        elif state == DROPPED:
            dropped += 1
        elif message is not None:
            messages.append(message)
            if message.role == "assistant" and message.meta.get("model"):
                models[message.meta["model"]] += 1
    alternates = _alternates(mapping, on_chain, names)
    leaves = sum(1 for node in mapping.values() if not node.get("children"))
    off_chain = sum(
        1 for node in mapping.values() if node.get("message") and node.get("id") not in on_chain
    )
    model = models.most_common(1)[0][0] if models else str(raw.get("default_model_slug") or "")
    tracker.note("message_nodes", sum(1 for node in mapping.values() if node.get("message")))
    tracker.note("messages_kept", len(messages))
    tracker.note("messages_hidden", hidden)
    tracker.note("messages_dropped_empty_or_context", dropped)
    tracker.note("messages_off_branch", off_chain)
    tracker.note("alternate_messages_kept", len(alternates))
    return Conversation(
        provider=PROVIDER,
        source_id=str(raw.get("conversation_id") or raw.get("id") or ""),
        title=str(raw.get("title") or "").strip() or "Untitled",
        created=to_iso(raw.get("create_time")),
        updated=to_iso(raw.get("update_time")),
        kind="chat",
        collection=str(raw.get("gizmo_id") or ""),
        model=model,
        archived=bool(raw.get("is_archived")),
        messages=messages,
        provenance=tracker.provenance(source),
        extra={
            "alternate_messages": alternates,
            "branches": leaves,
            "hidden_messages": hidden,
            "mapping_nodes": len(mapping),
            "memory_scope": str(raw.get("memory_scope") or ""),
            "off_branch_messages": off_chain,
            "starred": bool(raw.get("is_starred")),
        },
    )


def _asset_names(root: Path) -> dict[str, str]:
    path = root / ASSET_MAP_NAME
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {str(k): str(v) for k, v in payload.items()} if isinstance(payload, dict) else {}


def iter_conversations(
    root: Path,
    tracker: SourceTracker,
    *,
    extra_files: Iterable[Path] = (),
) -> Iterator[Conversation]:
    """Yield every conversation; `extra_files` (live deltas) are read first so they win."""
    names = _asset_names(root)
    for source in [*(Path(p) for p in extra_files), *shard_files(root)]:
        payload = json.loads(source.read_text(encoding="utf-8"))
        records = payload if isinstance(payload, list) else payload.get("conversations", [])
        for raw in records:
            yield _conversation(raw, names, tracker, source)
