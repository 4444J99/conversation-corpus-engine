from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .archive_model import Attachment, Conversation, Message, SourceTracker, clean_text, to_iso
from .archive_provenance import BASIS_KEY
from .archive_text import html_to_text

PROVIDER = "notebooklm"
READER = "archive_read_notebooklm@2"
MEDIA_SUFFIXES = {".mp4", ".wav", ".mp3", ".m4a", ".png", ".jpg", ".jpeg", ".webp"}
# A notebook's sources are what the account holder added to it and its notes are what they saved
# in it, so both are the person's (channel `written`); a note may be a saved model reply, which
# the export cannot tell apart from a typed one, and the basis keeps that doubt visible. The
# transcript, the artifacts and the discovered sources are produced by the service.
USER_KINDS = {"source": "user-supplied:source", "note": "user-saved:note"}


def notebook_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))


def detect(root: Path) -> bool:
    return any(list(p.glob("* metadata.json")) for p in notebook_dirs(root))


def channels(root: Path) -> dict[str, list[str]]:
    dirs = notebook_dirs(root)
    read = [
        f"{len(dirs)} notebooks — metadata, Sources, Notes, Artifacts metadata, Chat History, "
        "Discovered Sources"
    ]
    media = (
        sum(1 for p in root.rglob("*") if p.is_file() and p.suffix.lower() in MEDIA_SUFFIXES)
        if root.is_dir()
        else 0
    )
    not_read = (
        [f"{media} media files (audio/video overviews) — names and sizes only"] if media else []
    )
    return {"read": read, "not_read": not_read}


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _scalars(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    return {k: v for k, v in sorted(payload.items()) if isinstance(v, (str, int, float, bool))}


def _html_messages(folder: Path, kind: str, tracker: SourceTracker) -> list[Message]:
    messages: list[Message] = []
    for page in sorted(folder.glob("*.html")) if folder.is_dir() else []:
        meta: dict[str, Any] = {"kind": kind, "name": page.stem}
        sidecar = page.with_name(f"{page.stem} metadata.json")
        if sidecar.is_file():
            meta["source_meta"] = _scalars(_load(sidecar))
        text = html_to_text(page.read_text(encoding="utf-8", errors="replace"))
        tracker.provenance(page)
        if kind in USER_KINDS:
            meta[BASIS_KEY] = USER_KINDS[kind]
            messages.append(Message(role="user", text=text, channel="written", meta=meta))
        else:
            messages.append(Message(role="system", text=text, meta=meta))
    return messages


def _artifacts(folder: Path, tracker: SourceTracker) -> list[Message]:
    messages: list[Message] = []
    if not folder.is_dir():
        return messages
    for sidecar in sorted(folder.glob("* metadata.json")):
        payload = _load(sidecar)
        stem = sidecar.name.removesuffix(" metadata.json")
        attachments = [
            Attachment(
                name=p.name, kind=p.suffix.lower().lstrip("."), size=p.stat().st_size, ref=p.name
            )
            for p in sorted(folder.glob(f"{stem}.*"))
            if p.suffix.lower() in MEDIA_SUFFIXES
        ]
        tracker.provenance(sidecar)
        label = " · ".join(
            part
            for part in (
                str(payload.get("title") or stem),
                str(payload.get("type") or "").replace("ARTIFACT_TYPE_", "").lower(),
                str(payload.get("status") or "").replace("ARTIFACT_STATUS_", "").lower(),
            )
            if part
        )
        messages.append(
            Message(
                role="system",
                text=label,
                attachments=attachments,
                meta={"kind": "artifact", "source_count": len(payload.get("sources") or [])},
            )
        )
    return messages


def _discovered(folder: Path, tracker: SourceTracker) -> list[Message]:
    messages: list[Message] = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        tracker.provenance(path)
        scalars = _scalars(_load(path))
        text = "\n".join(f"{key}: {value}" for key, value in scalars.items())
        if text:
            messages.append(
                Message(role="system", text=clean_text(text), meta={"kind": "discovered-source"})
            )
    return messages


def iter_conversations(root: Path, tracker: SourceTracker) -> Iterator[Conversation]:
    for folder in notebook_dirs(root):
        sidecars = sorted(folder.glob("* metadata.json"))
        if not sidecars:
            tracker.note("skipped_dirs_without_metadata")
            continue
        header = _load(sidecars[0])
        stamps = header.get("metadata") or {}
        messages = [
            *_html_messages(folder / "Sources", "source", tracker),
            *_html_messages(folder / "Notes", "note", tracker),
            *_artifacts(folder / "Artifacts", tracker),
            *_html_messages(folder / "Chat History", "chat-history", tracker),
            *_discovered(folder / "Discovered Sources", tracker),
        ]
        yield Conversation(
            provider=PROVIDER,
            source_id=str(header.get("id") or folder.name),
            title=str(header.get("title") or folder.name).strip(),
            created=to_iso(stamps.get("createTime")),
            updated=to_iso(stamps.get("lastViewed")),
            kind="notebook",
            collection=folder.name,
            messages=messages,
            provenance=tracker.provenance(sidecars[0]),
            extra={
                "emoji": str(header.get("emoji") or ""),
                "shared": bool(stamps.get("isShared")),
                "sources": sum(1 for m in messages if m.meta.get("kind") == "source"),
                "notes": sum(1 for m in messages if m.meta.get("kind") == "note"),
                "artifacts": sum(1 for m in messages if m.meta.get("kind") == "artifact"),
            },
        )
