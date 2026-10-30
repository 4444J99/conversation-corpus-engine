from __future__ import annotations

import json
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

PROVIDER = "grok"
READER = "archive_read_grok@2"
BACKEND = "prod-grok-backend.json"
TASK_TEXT_KEYS = ("title", "name", "prompt", "description", "instruction", "message", "text")


def backend_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob(BACKEND) if p.is_file())


def detect(root: Path) -> bool:
    return bool(backend_files(root))


def channels(root: Path) -> dict[str, list[str]]:
    read = [
        f"{BACKEND} — conversations, projects, scheduled tasks, media prompts "
        f"({len(backend_files(root))} file(s))"
    ]
    not_read: list[str] = []
    for path in backend_files(root):
        folder = path.parent
        assets = folder / "prod-mc-asset-server"
        if assets.is_dir():
            count = sum(1 for p in assets.rglob("*") if p.is_file())
            not_read.append(f"prod-mc-asset-server — {count} generated/uploaded asset file(s)")
        for name in ("prod-mc-auth-mgmt-api.json", "prod-mc-billing.json"):
            if (folder / name).is_file():
                not_read.append(f"{name} — account/billing data (deliberately excluded)")
    return {"read": read, "not_read": not_read}


def _response_time(response: dict[str, Any]) -> str:
    return to_iso(response.get("create_time"))


def _ordered(responses: list[dict[str, Any]], leaf: str | None) -> tuple[list[dict[str, Any]], int]:
    by_id = {str(item.get("_id")): item for item in responses if item.get("_id")}
    if leaf and leaf in by_id:
        chain: list[dict[str, Any]] = []
        seen: set[str] = set()
        current: str | None = leaf
        while current and current in by_id and current not in seen:
            seen.add(current)
            chain.append(by_id[current])
            current = by_id[current].get("parent_response_id")
        chain.reverse()
        return chain, len(by_id) - len(chain)
    return sorted(responses, key=_response_time), 0


def _chat(item: dict[str, Any], tracker: SourceTracker, source: Path) -> Conversation:
    meta = item.get("conversation") or {}
    responses = [entry.get("response") or {} for entry in item.get("responses") or []]
    ordered, off_branch = _ordered(responses, meta.get("leaf_response_id"))
    messages: list[Message] = []
    models: dict[str, int] = {}
    for response in ordered:
        text = clean_text(response.get("message"))
        if not text.strip():
            continue
        role = normalize_role(response.get("sender"))
        model = str(response.get("model") or "")
        if model and role == "assistant":
            models[model] = models.get(model, 0) + 1
        messages.append(
            Message(
                role=role,
                text=text,
                ts=_response_time(response),
                meta={"kind": "text", **({"model": model} if model else {})},
            )
        )
    return Conversation(
        provider=PROVIDER,
        source_id=str(meta.get("id") or ""),
        title=str(meta.get("title") or "").strip() or "Untitled",
        created=to_iso(meta.get("create_time")),
        updated=to_iso(meta.get("modify_time")),
        kind="chat",
        model=max(models, key=lambda name: (models[name], name)) if models else "",
        messages=messages,
        provenance=tracker.provenance(source),
        extra={
            "off_branch_messages": off_branch,
            "starred": bool(meta.get("starred")),
            "summary": clean_text(meta.get("summary")),
            "system_prompt": str(meta.get("system_prompt_name") or ""),
            "temporary": bool(meta.get("temporary")),
        },
    )


def _project(item: dict[str, Any], tracker: SourceTracker, source: Path) -> Conversation:
    messages = []
    if item.get("custom_personality"):
        # The personality text is what the account holder wrote into the project settings.
        messages.append(
            Message(
                role="user",
                text=clean_text(item["custom_personality"]),
                channel="written",
                meta={"kind": "custom-personality", BASIS_KEY: "user-supplied:custom-personality"},
            )
        )
    for starter in item.get("conversation_starters") or []:
        text = starter if isinstance(starter, str) else json.dumps(starter, sort_keys=True)
        messages.append(Message(role="system", text=clean_text(text), meta={"kind": "starter"}))
    return Conversation(
        provider=PROVIDER,
        source_id=f"project-{item.get('workspace_id', '')}",
        title=str(item.get("name") or "").strip() or "Untitled project",
        created=to_iso(item.get("create_time")),
        updated=to_iso(item.get("last_use_time")),
        kind="notebook",
        collection=str(item.get("workspace_id") or ""),
        model=str(item.get("preferred_model") or ""),
        messages=messages,
        provenance=tracker.provenance(source),
        extra={"conversations_created": item.get("conversations_created_count")},
    )


def _task_lines(payload: Any) -> list[str]:
    if isinstance(payload, str):
        return [payload]
    if isinstance(payload, dict):
        lines = [
            clean_text(payload[key]) for key in TASK_TEXT_KEYS if isinstance(payload.get(key), str)
        ]
        for value in payload.values():
            if isinstance(value, (dict, list)):
                lines.extend(_task_lines(value))
        return [line for line in lines if line.strip()]
    if isinstance(payload, list):
        return [line for entry in payload for line in _task_lines(entry)]
    return []


def _task(item: dict[str, Any], index: int, tracker: SourceTracker, source: Path) -> Conversation:
    task = item.get("task") or {}
    messages = [
        Message(role="user", text=line, meta={"kind": "task"}) for line in _task_lines(task)
    ]
    messages += [
        Message(role="assistant", text=line, meta={"kind": "task-result"})
        for line in _task_lines(item.get("results") or [])
    ]
    identifier = str(task.get("id") or task.get("task_id") or index)
    title = next((line for line in _task_lines(task)), "") or f"Scheduled task {identifier}"
    return Conversation(
        provider=PROVIDER,
        source_id=f"task-{identifier}",
        title=title[:80],
        created=to_iso(task.get("create_time") or task.get("created_at")),
        kind="activity",
        messages=messages,
        provenance=tracker.provenance(source),
        extra={
            "schedules": len(item.get("schedules") or []),
            "results": len(item.get("results") or []),
        },
    )


def _media(
    posts: list[dict[str, Any]], tracker: SourceTracker, source: Path
) -> Iterator[Conversation]:
    months: dict[str, list[dict[str, Any]]] = {}
    for post in posts:
        stamp = to_iso(post.get("create_time"))
        months.setdefault(stamp[:7] or "undated", []).append(post)
    for month in sorted(months):
        ordered = sorted(
            months[month], key=lambda post: (to_iso(post.get("create_time")), str(post.get("id")))
        )
        messages = [
            Message(
                role="user",
                text=clean_text(post.get("original_prompt")),
                ts=to_iso(post.get("create_time")),
                attachments=[
                    Attachment(
                        name=str(post.get("id") or ""),
                        kind=str(post.get("media_type") or "media"),
                        ref=str(post.get("link") or ""),
                    )
                ],
                meta={"kind": "media-prompt", "media_type": str(post.get("media_type") or "")},
            )
            for post in ordered
            if str(post.get("original_prompt") or "").strip()
        ]
        yield Conversation(
            provider=PROVIDER,
            source_id=f"media-{month}",
            title=f"Grok media prompts {month}",
            created=messages[0].ts if messages else "",
            kind="activity",
            collection="media",
            messages=messages,
            provenance=tracker.provenance(source),
            extra={"posts": len(ordered)},
        )


def iter_conversations(root: Path, tracker: SourceTracker) -> Iterator[Conversation]:
    for source in backend_files(root):
        payload = json.loads(source.read_text(encoding="utf-8"))
        for item in payload.get("conversations") or []:
            yield _chat(item, tracker, source)
        for item in payload.get("projects") or []:
            yield _project(item, tracker, source)
        for index, item in enumerate(payload.get("tasks") or []):
            yield _task(item, index, tracker, source)
        yield from _media(payload.get("media_posts") or [], tracker, source)
