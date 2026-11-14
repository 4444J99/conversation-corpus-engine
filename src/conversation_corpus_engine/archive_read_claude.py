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
from .archive_provenance import BASIS_KEY

PROVIDER = "claude"
READER = "archive_read_claude@4"
CONVERSATIONS = "conversations.json"
TOOL_TEXT_LIMIT = 2000
TOOL_INPUT_LIMIT = 200
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


def _flatten(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for part in (_flatten(item) for item in value) if part)
    if isinstance(value, dict):
        for key in ("text", "content", "result"):
            if isinstance(value.get(key), (str, list, dict)):
                return _flatten(value[key])
    return ""


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"… [{len(text) - limit} more characters]"


def _entry_messages(entry: dict[str, Any], include_text: bool) -> list[Message]:
    """One raw chat message becomes up to four: reasoning, the turn itself, tool calls, results.

    Only the turn itself carries the vendor message uuid; the other three get minted ids.
    """
    role = normalize_role(entry.get("sender"))
    ts = to_iso(entry.get("created_at"))
    texts: list[str] = []
    thinking: list[str] = []
    calls: list[str] = []
    results: list[str] = []
    names: set[str] = set()
    for block in entry.get("content") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and block.get("text"):
            texts.append(str(block["text"]))
        elif kind == "thinking" and block.get("thinking"):
            thinking.append(str(block["thinking"]))
        elif kind == "tool_use" and block.get("name"):
            names.add(str(block["name"]))
            arguments = block.get("input")
            suffix = (
                " "
                + _cap(json.dumps(arguments, sort_keys=True, ensure_ascii=False), TOOL_INPUT_LIMIT)
                if arguments
                else ""
            )
            calls.append(f"tool call: {block['name']}{suffix}")
        elif kind == "tool_result":
            result = _cap(_flatten(block.get("content")), TOOL_TEXT_LIMIT)
            if result.strip():
                results.append(f"tool result: {result}")
    text = "\n\n".join(texts) if texts else str(entry.get("text") or "")
    attachments, extracted = _attachments(entry, include_text)
    typed_chars: int | None = None
    if extracted:
        typed_chars = len(clean_text(text).lstrip())
        text = f"{text}\n\n{extracted}".strip()
    out: list[Message] = []
    if thinking:
        out.append(
            Message(
                role="assistant",
                text=clean_text("\n\n".join(thinking)),
                ts=ts,
                meta={"kind": "reasoning"},
            )
        )
    if text.strip() or attachments:
        meta: dict[str, Any] = {"kind": "text"}
        if names:
            meta["tools"] = sorted(names)
        out.append(
            Message(
                role=role,
                text=clean_text(text),
                ts=ts,
                attachments=attachments,
                meta=meta,
                id=str(entry.get("uuid") or ""),
                typed_chars=typed_chars,
            )
        )
    for lines in (calls, results):
        if lines:
            out.append(
                Message(
                    role="tool", text=clean_text("\n".join(lines)), ts=ts, meta={"kind": "tool"}
                )
            )
    return out


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
        parent = entry.get("parent_message_uuid")
        if parent:
            parents[parent] = parents.get(parent, 0) + 1
        derived = _entry_messages(entry, include_text)
        if not derived:
            tracker.note("empty_messages_dropped")
        messages.extend(derived)
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
    # The instructions and knowledge documents of a project are text the account holder put into
    # it, so they are the person's (channel `written`); the export cannot say whether a document
    # was typed or saved from elsewhere, and that doubt is recorded in the basis.
    messages: list[Message] = []
    if raw.get("prompt_template"):
        messages.append(
            Message(
                role="user",
                text=clean_text(raw["prompt_template"]),
                ts=to_iso(raw.get("created_at")),
                channel="written",
                meta={"kind": "prompt-template", BASIS_KEY: "user-supplied:project-instructions"},
            )
        )
    for doc in raw.get("docs") or []:
        messages.append(
            Message(
                role="user",
                text=clean_text(doc.get("content")),
                ts=to_iso(doc.get("created_at")),
                attachments=[Attachment(name=str(doc.get("filename") or ""), kind="doc")],
                channel="written",
                meta={
                    "kind": "project-doc",
                    "filename": str(doc.get("filename") or ""),
                    BASIS_KEY: "user-supplied:project-document",
                },
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
