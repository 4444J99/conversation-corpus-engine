from __future__ import annotations

import json
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .archive_model import (
    Attachment,
    Conversation,
    Message,
    SourceTracker,
    clean_text,
    normalize_role,
    to_iso,
)

PROVIDER = "claude"
READER = "archive_read_claude@1"
CONVERSATIONS = "conversations.json"
UNREAD_CHANNELS = (
    ("artifacts", "published artifact version HTML"),
    ("frames-000.zip", "artifact frames archive"),
    ("claude_code_cloud", "Claude Code cloud routines"),
    ("reflections", "periodic reflections"),
    ("light_metadata-000", "account identity / login history (deliberately excluded)"),
    ("feedback-000.zip", "feedback records"),
)


def detect(root: Path) -> bool:
    return (root / CONVERSATIONS).is_file()


def channels(root: Path) -> dict[str, list[str]]:
    read = [CONVERSATIONS]
    for name, label in (
        ("projects-000.zip", "projects (prompt template + docs)"),
        ("memories-000.zip", "memory summary + memory files"),
        ("design_chats-000.zip", "design chats"),
    ):
        if (root / name).is_file():
            read.append(f"{name} — {label}")
    not_read = [f"{n} — {label}" for n, label in UNREAD_CHANNELS if (root / n).exists()]
    return {"read": read, "not_read": not_read}


def _blocks(message: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    meta: dict[str, Any] = {"kind": "text"}
    texts: list[str] = []
    thinking: list[str] = []
    tools: list[str] = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and block.get("text"):
            texts.append(str(block["text"]))
        elif kind == "thinking" and block.get("thinking"):
            thinking.append(str(block["thinking"]))
        elif kind == "tool_use" and block.get("name"):
            tools.append(str(block["name"]))
    text = "\n\n".join(texts) if texts else str(message.get("text") or "")
    if thinking:
        meta["thinking_chars"] = sum(len(item) for item in thinking)
    if tools:
        meta["tools"] = sorted(set(tools))
    return text, meta


def _attachments(message: dict[str, Any], include_text: bool) -> tuple[list[Attachment], str]:
    items: list[Attachment] = []
    extracted: list[str] = []
    for entry in message.get("attachments") or []:
        items.append(
            Attachment(
                name=str(entry.get("file_name") or ""),
                kind=str(entry.get("file_type") or "file"),
                size=entry.get("file_size"),
            )
        )
        if include_text and entry.get("extracted_content"):
            extracted.append(
                f"[attachment: {entry.get('file_name', '')}]\n{entry['extracted_content']}"
            )
    for entry in message.get("files") or []:
        items.append(Attachment(name=str(entry.get("file_name") or ""), kind="file"))
    return items, "\n\n".join(extracted)


def _chat(
    raw: dict[str, Any], tracker: SourceTracker, source: Path, include_text: bool
) -> Conversation:
    messages: list[Message] = []
    parents: dict[str, int] = {}
    for entry in raw.get("chat_messages") or []:
        text, meta = _blocks(entry)
        attachments, extracted = _attachments(entry, include_text)
        if extracted:
            text = f"{text}\n\n{extracted}".strip()
        if not text.strip() and not attachments:
            continue
        parent = entry.get("parent_message_uuid")
        if parent:
            parents[parent] = parents.get(parent, 0) + 1
        messages.append(
            Message(
                role=normalize_role(entry.get("sender")),
                text=clean_text(text),
                ts=to_iso(entry.get("created_at")),
                attachments=attachments,
                meta=meta,
            )
        )
    messages.sort(key=lambda message: message.ts)
    return Conversation(
        provider=PROVIDER,
        source_id=str(raw.get("uuid") or ""),
        title=str(raw.get("name") or "").strip() or "Untitled",
        created=to_iso(raw.get("created_at")),
        updated=to_iso(raw.get("updated_at")),
        kind="chat",
        messages=messages,
        provenance=tracker.provenance(source),
        extra={
            "summary": clean_text(raw.get("summary")),
            "branched": any(count > 1 for count in parents.values()),
        },
    )


def _zip_json(path: Path, prefix: str) -> Iterator[tuple[str, Any]]:
    with zipfile.ZipFile(path) as archive:
        for name in sorted(archive.namelist()):
            if name.startswith(prefix) and name.endswith(".json"):
                yield name, json.loads(archive.read(name))


def _project(
    raw: dict[str, Any], tracker: SourceTracker, source: Path, member: str
) -> Conversation:
    messages: list[Message] = []
    if raw.get("prompt_template"):
        messages.append(
            Message(
                role="system",
                text=clean_text(raw["prompt_template"]),
                ts=to_iso(raw.get("created_at")),
                meta={"kind": "prompt-template"},
            )
        )
    for doc in raw.get("docs") or []:
        messages.append(
            Message(
                role="system",
                text=clean_text(doc.get("content")),
                ts=to_iso(doc.get("created_at")),
                attachments=[Attachment(name=str(doc.get("filename") or ""), kind="doc")],
                meta={"kind": "project-doc", "filename": str(doc.get("filename") or "")},
            )
        )
    return Conversation(
        provider=PROVIDER,
        source_id=f"project-{raw.get('uuid', '')}",
        title=str(raw.get("name") or "").strip() or "Untitled project",
        created=to_iso(raw.get("created_at")),
        updated=to_iso(raw.get("updated_at")),
        kind="notebook",
        collection=str(raw.get("uuid") or ""),
        messages=messages,
        provenance=tracker.provenance(source, member=member),
        extra={"description": clean_text(raw.get("description")), "private": raw.get("is_private")},
    )


def _memories(
    raw: dict[str, Any], tracker: SourceTracker, source: Path, member: str
) -> Conversation:
    messages: list[Message] = []
    if raw.get("conversations_memory"):
        messages.append(
            Message(
                role="system",
                text=clean_text(raw["conversations_memory"]),
                meta={"kind": "summary"},
            )
        )
    for project_id, text in sorted((raw.get("project_memories") or {}).items()):
        messages.append(
            Message(
                role="system",
                text=clean_text(text),
                meta={"kind": "project-memory", "project": str(project_id)},
            )
        )
    for entry in raw.get("memory_files") or []:
        messages.append(
            Message(
                role="system",
                text=clean_text(entry.get("content")),
                ts=to_iso(entry.get("updated_at")),
                attachments=[Attachment(name=str(entry.get("path") or ""), kind="memory-file")],
                meta={"kind": "memory-file", "path": str(entry.get("path") or "")},
            )
        )
    return Conversation(
        provider=PROVIDER,
        source_id="memories",
        title="Claude memory export",
        kind="memory",
        messages=messages,
        provenance=tracker.provenance(source, member=member),
    )


def _design(raw: dict[str, Any], tracker: SourceTracker, source: Path, member: str) -> Conversation:
    messages = [
        Message(
            role=normalize_role(entry.get("role") or entry.get("sender")),
            text=clean_text(entry.get("text") or entry.get("content")),
            ts=to_iso(entry.get("created_at")),
        )
        for entry in raw.get("messages") or []
        if isinstance(entry, dict)
    ]
    project = raw.get("project") or {}
    return Conversation(
        provider=PROVIDER,
        source_id=f"design-{raw.get('uuid', '')}",
        title=str(raw.get("title") or "").strip() or "Untitled design chat",
        created=to_iso(raw.get("created_at")),
        updated=to_iso(raw.get("updated_at")),
        kind="chat",
        collection=str(project.get("uuid") or ""),
        messages=messages,
        provenance=tracker.provenance(source, member=member),
        extra={"project_name": str(project.get("name") or ""), "surface": "design"},
    )


def iter_conversations(
    root: Path, tracker: SourceTracker, *, include_attachment_text: bool = False
) -> Iterator[Conversation]:
    source = root / CONVERSATIONS
    for raw in json.loads(source.read_text(encoding="utf-8")):
        yield _chat(raw, tracker, source, include_attachment_text)
    projects = root / "projects-000.zip"
    if projects.is_file():
        for member, raw in _zip_json(projects, "projects/"):
            yield _project(raw, tracker, projects, member)
    memories = root / "memories-000.zip"
    if memories.is_file():
        for member, raw in _zip_json(memories, "memories/"):
            yield _memories(raw, tracker, memories, member)
    designs = root / "design_chats-000.zip"
    if designs.is_file():
        for member, raw in _zip_json(designs, "design_chats/"):
            yield _design(raw, tracker, designs, member)
