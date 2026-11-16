"""Deterministic synthetic provider exports, for tests and for the downstream predicate.

`cce archive fixture --out DIR` writes a ChatGPT export and a Claude export whose conversations
share one identical ask, so a recurring ask across two providers can be exercised end to end
without any real conversation text. Every string here is invented. The bytes are the same on every
run: fixed timestamps, sorted keys, and stored (uncompressed) zip members with a fixed date.

The export shapes are the vendors' own (a ChatGPT `mapping` tree, a Claude `conversations.json`
with project and memory archives), so the normal readers parse them: ingest, then project the
feed. Beside the exports a small note (`fixture-all.json`) records the digest of the shared ask
and how many asks each provider should yield, so a consumer can check its counts without
reading any text.

The fixture also drives the projector's exclusion paths: a Claude user turn that is only an
attachment (`empty-text`), a Claude memory archive and a project (`kind-memory`, `kind-notebook`),
assistant and tool turns (`role-assistant`, `role-tool`), and a ChatGPT edit that leaves an
abandoned branch (`alternate_rows`).

`--stress` adds what the bounded layout exists for: one ChatGPT thread whose reply is hundreds of
kilobytes (many lines, a long fenced block and one line of 120,000 characters), so a note must be
split into parts, and a crowd of short conversations in a single month (spread over several days),
so a small folder limit must spill the month into day folders. The default fixture is unchanged.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any

from . import archive_layout as layout

FIXTURE_SCHEMA = "archive-fixture.v1"
STRESS_CONVERSATIONS = 120
STRESS_DAYS = 20
LONG_THREAD_ID = "fixture-long-thread"
SHARED_ASK = "How often should a lantern wick be trimmed to keep the flame steady?"
EDITED_ASK = "How frequently should a lantern wick be trimmed?"
BASE_EPOCH = 1_760_000_000.0
ZIP_DATE = (2025, 10, 10, 0, 0, 0)
DAY_SECONDS = 86_400


def _dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _zip(path: Path, members: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, payload in sorted(members.items()):
            info = zipfile.ZipInfo(name, date_time=ZIP_DATE)
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o644 << 16
            archive.writestr(info, json.dumps(payload, sort_keys=True))


def _node(node_id: str, parent: str, role: str, when: float, text: str) -> dict[str, Any]:
    return {
        "id": node_id,
        "parent": parent,
        "children": [],
        "message": {
            "id": node_id,
            "author": {"role": role},
            "create_time": when,
            "content": {"content_type": "text", "parts": [text]},
            "metadata": {"model_slug": "fixture-model"} if role == "assistant" else {},
        },
    }


def _chatgpt_conversation(
    conversation_id: str,
    title: str,
    day: int,
    turns: list[tuple[str, str]],
    *,
    edited_first_ask: str = "",
) -> dict[str, Any]:
    """A ChatGPT conversation: the current branch is `turns`; `edited_first_ask` adds an
    abandoned branch that hangs from the same parent as the first turn."""
    start = BASE_EPOCH + DAY_SECONDS * day
    root = "root"
    mapping: dict[str, Any] = {root: {"id": root, "message": None, "parent": None, "children": []}}
    parent = root
    last = parent
    for index, (role, text) in enumerate(turns):
        node_id = f"{conversation_id}-{index}"
        mapping[node_id] = _node(node_id, parent, role, start + 60 * (index + 1), text)
        mapping[parent]["children"].append(node_id)
        parent = last = node_id
    if edited_first_ask:
        edit_id = f"{conversation_id}-edit"
        reply_id = f"{conversation_id}-edit-reply"
        mapping[edit_id] = _node(edit_id, root, "user", start + 600, edited_first_ask)
        mapping[reply_id] = _node(
            reply_id, edit_id, "assistant", start + 660, "A different answer."
        )
        mapping[root]["children"].append(edit_id)
        mapping[edit_id]["children"].append(reply_id)
    return {
        "conversation_id": conversation_id,
        "id": conversation_id,
        "title": title,
        "create_time": start,
        "update_time": start + 3600,
        "current_node": last,
        "is_archived": False,
        "default_model_slug": None,
        "gizmo_id": None,
        "mapping": mapping,
    }


def _claude_entry(uuid: str, sender: str, when: str, text: str, **extra: Any) -> dict[str, Any]:
    return {
        "uuid": uuid,
        "sender": sender,
        "created_at": when,
        "text": text,
        "content": extra.pop("content", [{"type": "text", "text": text}]),
        "attachments": extra.pop("attachments", []),
        "files": [],
        "parent_message_uuid": extra.pop("parent", "root"),
    }


def _claude_conversation(
    uuid: str, name: str, created: str, entries: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "uuid": uuid,
        "name": name,
        "summary": "",
        "created_at": created,
        "updated_at": created,
        "chat_messages": entries,
    }


def _long_reply() -> str:
    """An invented reply of several hundred kilobytes: prose lines, a fenced block, one wide line."""
    prose = [
        f"Row {number:05d}: the lantern wick log continues with an invented trimming entry."
        for number in range(3000)
    ]
    code = [
        "```python",
        *(f"step_{number:04d} = {number} * 3  # invented" for number in range(400)),
    ]
    code.append("```")
    return "\n".join([*prose[:1500], *code, *prose[1500:], "wick" * 30_000])


def _stress_chatgpt(count: int) -> list[dict[str, Any]]:
    """The long thread, then `count` short conversations spread over STRESS_DAYS days of one month."""
    stress = [
        _chatgpt_conversation(
            LONG_THREAD_ID,
            "A very long thread",
            2,
            [("user", "Write out the whole wick log."), ("assistant", _long_reply())],
        )
    ]
    for number in range(count):
        stress.append(
            _chatgpt_conversation(
                f"fixture-stress-{number:04d}",
                f"Stress conversation {number:04d}",
                3 + number % STRESS_DAYS,
                [
                    ("user", f"Stress ask {number:04d}: how tight should knot {number} be?"),
                    ("assistant", f"Snug, but not tight enough to cut: answer {number}."),
                ],
            )
        )
    return stress


def _chatgpt_export(stress: int = 0) -> list[dict[str, Any]]:
    base = [
        _chatgpt_conversation(
            "fixture-lantern-1",
            "Lantern care",
            0,
            [
                ("user", SHARED_ASK),
                ("assistant", "Trim it a little each week."),
            ],
            edited_first_ask=EDITED_ASK,
        ),
        _chatgpt_conversation(
            "fixture-hammock-1",
            "Hammock knots",
            1,
            [
                ("user", "What is a good first knot for a hammock?"),
                ("assistant", "A simple bowline works."),
                ("user", "Which knot is easiest to untie after a long load?"),
                ("assistant", "A slipped bowline releases quickly."),
            ],
        ),
    ]
    return base + _stress_chatgpt(stress) if stress else base


def _claude_export() -> list[dict[str, Any]]:
    day_one = "2025-10-10T09:00:00Z"
    day_two = "2025-10-11T09:00:00Z"
    return [
        _claude_conversation(
            "fixture-claude-lantern",
            "Lantern wicks",
            day_one,
            [
                _claude_entry("fixture-c1-ask", "human", day_one, SHARED_ASK),
                _claude_entry(
                    "fixture-c1-reply",
                    "assistant",
                    "2025-10-10T09:01:00Z",
                    "",
                    parent="fixture-c1-ask",
                    content=[
                        {"type": "thinking", "thinking": "Considering wick length."},
                        {"type": "text", "text": "Trim it weekly."},
                        {"type": "tool_use", "name": "calculator"},
                    ],
                ),
            ],
        ),
        _claude_conversation(
            "fixture-claude-tent",
            "Tent packing",
            day_two,
            [
                _claude_entry(
                    "fixture-c2-ask",
                    "human",
                    day_two,
                    "Draft a checklist for packing a small tent.",
                ),
                # A user turn that is only an attachment: the reader keeps it, with no text, so
                # the feed projector counts it as `empty-text`.
                _claude_entry(
                    "fixture-c2-photo",
                    "human",
                    "2025-10-11T09:00:30Z",
                    "",
                    parent="fixture-c2-ask",
                    attachments=[
                        {"file_name": "campsite.png", "file_size": 12, "file_type": "png"}
                    ],
                ),
                _claude_entry(
                    "fixture-c2-reply",
                    "assistant",
                    "2025-10-11T09:01:00Z",
                    "Poles, pegs, fly sheet, groundsheet.",
                    parent="fixture-c2-photo",
                ),
            ],
        ),
    ]


def _claude_project() -> dict[str, Any]:
    return {
        "uuid": "fixture-project-1",
        "name": "Camping handbook",
        "description": "Invented project.",
        "is_private": True,
        "prompt_template": "Answer briefly about camping.",
        "created_at": "2025-10-12T09:00:00Z",
        "updated_at": "2025-10-12T10:00:00Z",
        "docs": [
            {
                "uuid": "fixture-doc-1",
                "filename": "checklist.md",
                "content": "A checklist for a weekend trip.",
                "created_at": "2025-10-12T09:30:00Z",
            }
        ],
    }


def _claude_memory() -> dict[str, Any]:
    return {
        "conversations_memory": "Enjoys camping and lanterns.",
        "project_memories": {"fixture-project-1": "Camping handbook notes."},
        "memory_files": [
            {
                "path": "notes.md",
                "content": "Invented note body.",
                "updated_at": "2025-10-13T09:00:00Z",
            }
        ],
    }


def expected_counts(stress: int = 0) -> dict[str, Any]:
    """What the feed projector must yield from the fixture, derived from the fixture's own data."""
    chatgpt_rows = sum(
        1
        for conversation in _chatgpt_export(stress)
        for node in conversation["mapping"].values()
        if node["message"] and node["message"]["author"]["role"] == "user"
    )
    claude_rows = sum(
        1
        for conversation in _claude_export()
        for entry in conversation["chat_messages"]
        if entry["sender"] == "human" and entry["text"].strip()
    )
    alternates = sum(
        1
        for conversation in _chatgpt_export(stress)
        if f"{conversation['id']}-edit" in conversation["mapping"]
    )
    return {
        "chatgpt": {"asks": chatgpt_rows, "alternate_asks": alternates},
        "claude": {"asks": claude_rows, "alternate_asks": 0},
    }


def write_fixture(out: Path, *, stress: int = 0) -> dict[str, Any]:
    """Write the synthetic exports under `out`; return a payload that lists what was written.

    `stress` is the number of extra short conversations to add beside the one very long thread
    (0 writes the plain fixture)."""
    _dump(out / "chatgpt" / "conversations-000.json", _chatgpt_export(stress))
    claude = out / "claude"
    _dump(claude / "conversations.json", _claude_export())
    _zip(claude / "projects-000.zip", {"projects/project-1.json": _claude_project()})
    _zip(claude / "memories-000.zip", {"memories/memory-1.json": _claude_memory()})
    note = {
        "schema": FIXTURE_SCHEMA,
        "shared_ask_sha256": hashlib.sha256(SHARED_ASK.encode("utf-8")).hexdigest(),
        "shared_ask_providers": ["chatgpt", "claude"],
        "providers": expected_counts(stress),
    }
    if stress:
        note["stress"] = {
            "long_thread_id": LONG_THREAD_ID,
            "short_conversations": stress,
            "days": STRESS_DAYS,
        }
    _dump(out / layout.FIXTURE_FILENAME, note)
    files = sorted(str(path.relative_to(out)) for path in out.rglob("*") if path.is_file())
    return {"out": str(out), "files": files, **note}
