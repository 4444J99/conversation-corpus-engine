from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .answering import shorten
from .archive_model import (
    Attachment,
    Conversation,
    Message,
    SourceTracker,
    clean_text,
    to_iso,
)
from .archive_text import html_to_text

PROVIDER = "gemini"
READER = "archive_read_gemini@1"
ACTIVITY = Path("My Activity") / "Gemini Apps" / "MyActivity.json"
AI_MODE = Path("My Activity") / "AI Mode"
WORKSPACE = Path("Gemini in Workspace") / "Conversation History"
SCHEDULED = Path("Gemini") / "gemini_scheduled_actions_data.html"
APP_RE = re.compile(r"gemini\.google\.com/app/([A-Za-z0-9_-]+)")
PROMPT_RE = re.compile(r"^(?:Live Prompt(?:ed)?|Prompted)\s*(.*)$", re.DOTALL)
CANVAS_PREFIX = "Created Gemini Canvas titled "
SESSION_GAP_SECONDS = 30 * 60
OUT_OF_SCOPE_NOTE = "other My Activity products — not AI-chat data, out of scope"


def detect(root: Path) -> bool:
    return (root / ACTIVITY).is_file() or (root / WORKSPACE).is_dir()


def channels(root: Path) -> dict[str, list[str]]:
    read: list[str] = []
    if (root / ACTIVITY).is_file():
        read.append("My Activity/Gemini Apps/MyActivity.json")
    if (root / AI_MODE).is_dir():
        read.append("My Activity/AI Mode/*.json")
    if (root / WORKSPACE).is_dir():
        read.append(f"Gemini in Workspace — {len(workspace_files(root))} conversation file(s)")
    if (root / SCHEDULED).is_file():
        read.append("Gemini/gemini_scheduled_actions_data.html")
    not_read: list[str] = []
    activity = root / "My Activity"
    if activity.is_dir():
        others = sorted(p.name for p in activity.iterdir() if p.is_dir())
        others = [name for name in others if name not in {"Gemini Apps", "AI Mode"}]
        if others:
            not_read.append(f"{len(others)} {OUT_OF_SCOPE_NOTE}")
    apps = root / "My Activity" / "Gemini Apps"
    if apps.is_dir():
        files = sum(1 for p in apps.iterdir() if p.is_file() and p.name != "MyActivity.json")
        not_read.append(
            f"{files} attachment/export files in Gemini Apps — names only, bytes not read"
        )
    not_read += [f"{p.name} — original Takeout archive" for p in sorted(root.glob("takeout-*.zip"))]
    return {"read": read, "not_read": not_read}


def workspace_files(root: Path) -> list[Path]:
    folder = root / WORKSPACE
    return (
        sorted(p for p in folder.glob("conversation_*.txt") if p.is_file())
        if folder.is_dir()
        else []
    )


def _app_id(item: dict[str, Any]) -> str:
    for detail in item.get("details") or []:
        match = APP_RE.search(str(detail.get("url") or detail.get("name") or ""))
        if match:
            return match.group(1)
    return ""


def _attachments(item: dict[str, Any]) -> list[Attachment]:
    names = [str(name) for name in item.get("attachedFiles") or []]
    if not names and item.get("imageFile"):
        names = [str(item["imageFile"])]
    return [Attachment(name=name, kind="file", ref=name) for name in names]


def _epoch(stamp: str) -> float:
    if not stamp:
        return 0.0
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).replace(tzinfo=UTC).timestamp()


def _turns(item: dict[str, Any], tracker: SourceTracker) -> list[Message]:
    title = str(item.get("title") or "")
    ts = to_iso(item.get("time"))
    prompt = PROMPT_RE.match(title)
    if prompt:
        messages = [
            Message(
                role="user",
                text=clean_text(prompt.group(1)).strip(),
                ts=ts,
                attachments=_attachments(item),
                meta={"kind": "text"},
            )
        ]
        html = ((item.get("safeHtmlItem") or [{}])[0]).get("html") or ""
        if html:
            messages.append(
                Message(role="assistant", text=html_to_text(html), ts=ts, meta={"kind": "text"})
            )
        else:
            tracker.note("prompts_without_response")
        return messages
    if title.startswith(CANVAS_PREFIX):
        body = "\n\n".join(
            str(sub.get("name") or "") for sub in item.get("subtitles") or [] if sub.get("name")
        )
        label = title[len(CANVAS_PREFIX) :].strip()
        return [
            Message(
                role="assistant",
                text=clean_text(body),
                ts=ts,
                meta={"kind": "canvas", "canvas_title": label},
            )
        ]
    tracker.note("skipped_activity:" + " ".join(title.split()[:2]).lower())
    return []


def _build(
    key: str,
    items: list[dict[str, Any]],
    tracker: SourceTracker,
    source: Path,
    *,
    synthetic: bool,
) -> Conversation | None:
    messages: list[Message] = []
    for item in items:
        messages.extend(_turns(item, tracker))
    if not messages:
        return None
    first_user = next((m.text for m in messages if m.role == "user"), "")
    canvas = next(
        (m.meta.get("canvas_title", "") for m in messages if m.meta.get("kind") == "canvas"), ""
    )
    stamps = [m.ts for m in messages if m.ts]
    return Conversation(
        provider=PROVIDER,
        source_id=key,
        title=shorten(first_user, 80) or canvas or "Untitled",
        created=min(stamps) if stamps else "",
        updated=max(stamps) if stamps else "",
        kind="chat",
        messages=messages,
        provenance=tracker.provenance(source),
        extra={"grouping": "session-gap" if synthetic else "app-url", "turns": len(items)},
    )


def _activity(root: Path, tracker: SourceTracker) -> Iterator[Conversation]:
    source = root / ACTIVITY
    items = sorted(json.loads(source.read_text(encoding="utf-8")), key=lambda i: str(i.get("time")))
    by_app: dict[str, list[dict[str, Any]]] = {}
    loose: list[dict[str, Any]] = []
    for item in items:
        app = _app_id(item)
        if app:
            by_app.setdefault(app, []).append(item)
        else:
            loose.append(item)
    for app in sorted(by_app):
        conversation = _build(app, by_app[app], tracker, source, synthetic=False)
        if conversation:
            yield conversation
    session: list[dict[str, Any]] = []
    last = 0.0
    for item in [*loose, None]:
        moment = _epoch(to_iso(item.get("time"))) if item else 0.0
        if session and (item is None or moment - last > SESSION_GAP_SECONDS):
            key = "session-" + to_iso(session[0].get("time")).replace(":", "")
            conversation = _build(key, session, tracker, source, synthetic=True)
            if conversation:
                yield conversation
            session = []
        if item is not None:
            session.append(item)
            last = moment


def _ai_mode(root: Path, tracker: SourceTracker) -> Iterator[Conversation]:
    folder = root / AI_MODE
    for source in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        for item in json.loads(source.read_text(encoding="utf-8")):
            title = str(item.get("title") or "")
            query = title.removeprefix("Searched for ").strip()
            ts = to_iso(item.get("time"))
            html = ((item.get("safeHtmlItem") or [{}])[0]).get("html") or ""
            messages = [Message(role="user", text=query, ts=ts, meta={"kind": "text"})]
            if html:
                messages.append(
                    Message(role="assistant", text=html_to_text(html), ts=ts, meta={"kind": "text"})
                )
            digest = hashlib.sha1(f"{ts}|{title}".encode(), usedforsecurity=False).hexdigest()[:12]
            yield Conversation(
                provider=PROVIDER,
                source_id=f"ai-mode-{digest}",
                title=shorten(query, 80) or "AI Mode search",
                created=ts,
                updated=ts,
                kind="search",
                collection="ai-mode",
                messages=messages,
                provenance=tracker.provenance(source),
                extra={"surface": "AI Mode"},
            )


def _workspace(root: Path, tracker: SourceTracker) -> Iterator[Conversation]:
    for source in workspace_files(root):
        payload = json.loads(source.read_text(encoding="utf-8"))
        messages: list[Message] = []
        for turn in payload.get("conversation_turns") or []:
            user = turn.get("user_turn")
            system = turn.get("system_turn")
            for part, role in ((user, "user"), (system, "assistant")):
                if not part:
                    continue
                if part.get("turn_deleted_time"):
                    tracker.note("skipped_deleted_turns")
                    continue
                text = part.get("prompt") if role == "user" else part.get("text")
                meta: dict[str, Any] = {"kind": "text"}
                if part.get("citations"):
                    meta["citations"] = [
                        {"text": str(c.get("display_text") or ""), "url": str(c.get("url") or "")}
                        for c in part["citations"]
                    ]
                messages.append(
                    Message(
                        role=role,
                        text=clean_text(text),
                        ts=to_iso(part.get("turn_last_modified")),
                        meta=meta,
                    )
                )
        if not messages:
            continue
        stamps = [m.ts for m in messages if m.ts]
        first_user = next((m.text for m in messages if m.role == "user"), "")
        yield Conversation(
            provider=PROVIDER,
            source_id=source.stem,
            title=shorten(first_user, 80) or source.stem,
            created=min(stamps) if stamps else "",
            updated=max(stamps) if stamps else "",
            kind="chat",
            collection="workspace",
            messages=messages,
            provenance=tracker.provenance(source),
            extra={"surface": "Gemini in Workspace"},
        )


def _scheduled(root: Path, tracker: SourceTracker) -> Iterator[Conversation]:
    source = root / SCHEDULED
    if not source.is_file():
        return
    text = html_to_text(source.read_text(encoding="utf-8"))
    if not text.strip():
        return
    yield Conversation(
        provider=PROVIDER,
        source_id="scheduled-actions",
        title="Gemini scheduled actions",
        kind="activity",
        collection="scheduled",
        messages=[Message(role="system", text=text, meta={"kind": "scheduled-actions"})],
        provenance=tracker.provenance(source),
    )


def iter_conversations(root: Path, tracker: SourceTracker) -> Iterator[Conversation]:
    if (root / ACTIVITY).is_file():
        yield from _activity(root, tracker)
    yield from _ai_mode(root, tracker)
    yield from _workspace(root, tracker)
    yield from _scheduled(root, tracker)
