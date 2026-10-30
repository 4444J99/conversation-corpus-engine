"""Local-session memory: the folders `cce provider import --mode local-session` left behind.

Each folder (`claude-local-session-memory`, `chatgpt-local-session-memory`) holds a capture of the
desktop app's conversations (`source/`), the corpus the engine derived from it (`corpus/`,
`eval/`) and a few notes. This reader accounts for every file in one of three ways, so the file
count reconciles:

* a conversation payload (`conversations.json`, `conversation-summaries.json`, the
  `conversation-cache/` and `conversation-details/` folders) is *folded*: its conversations are the
  threads the account export already holds under the same ids, and a second copy would only make
  every search answer twice. The reader counts the files and the conversations they carry; the
  ingest's own manifest for the owning provider proves the ids are present;
* an account file (users, organizations, bootstrap) is *excluded* on purpose, as the other
  readers exclude account data;
* everything else (the derived corpus, the evaluation scorecards, the project and memory
  documents, the notes) is one machine record: the engine wrote it, or it is a record the app
  kept about the account's own memory.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from pathlib import Path

from .archive_model import Conversation, Message, SourceTracker, to_iso

PROVIDER = "localmemory"
READER = "archive_read_localmemory@1"
FOLDED_FILES = frozenset({"conversations.json", "conversation-summaries.json"})
FOLDED_DIRS = frozenset({"conversation-cache", "conversation-details"})
EXCLUDED_FILES = frozenset({"users.json", "user.json", "organizations.json", "bootstrap.json"})
TEXT_SUFFIXES = frozenset({".json", ".md", ".txt", ".yaml", ".yml", ".csv", ".html"})
MAX_BYTES = 20 * 1024 * 1024


def _files(root: Path) -> list[Path]:
    found: list[Path] = []
    if not root.is_dir():
        return found
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if not name.startswith("."))
        for name in sorted(names):
            path = Path(directory) / name
            if not name.startswith(".") and not path.is_symlink():
                found.append(path)
    return found


def _disposition(root: Path, path: Path) -> str:
    relative = path.relative_to(root)
    if path.name in EXCLUDED_FILES:
        return "excluded"
    if path.name in FOLDED_FILES or FOLDED_DIRS & set(relative.parts[:-1]):
        return "folded"
    return "record"


def detect(root: Path) -> bool:
    return bool(_files(root))


def channels(root: Path) -> dict[str, list[str]]:
    counts: dict[str, int] = {"record": 0, "folded": 0, "excluded": 0}
    for path in _files(root):
        counts[_disposition(root, path)] += 1
    return {
        "read": [f"{counts['record']} file(s), one machine record each"],
        "not_read": [
            f"{counts['folded']} conversation payload file(s) — folded into the owning provider",
            f"{counts['excluded']} account file(s) — deliberately excluded",
        ],
    }


def _conversation_count(path: Path) -> int:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    return len(payload) if isinstance(payload, list) else 0


def _record(root: Path, path: Path, tracker: SourceTracker) -> Conversation:
    relative = path.relative_to(root).as_posix()
    info = path.stat()
    stamp = to_iso(info.st_mtime)
    text = ""
    if path.suffix.lower() in TEXT_SUFFIXES and info.st_size <= MAX_BYTES:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            text = ""
    if "\x00" in text:
        text = ""
    messages = (
        [
            Message(
                role="system",
                text=text,
                ts=stamp,
                channel="memory",
                typed_chars=0,
                meta={"kind": "memory", "path": relative},
            )
        ]
        if text.strip()
        else []
    )
    if not messages:
        tracker.note("records_without_text")
    return Conversation(
        provider=PROVIDER,
        source_id=f"file-{hashlib.sha256(relative.encode('utf-8')).hexdigest()[:32]}",
        title=relative,
        created=stamp,
        updated=stamp,
        kind="memory",
        collection=relative.split("/", 1)[0],
        messages=messages,
        provenance=tracker.provenance(path),
        extra={"path": relative, "bytes": info.st_size},
    )


def iter_conversations(root: Path, tracker: SourceTracker) -> Iterator[Conversation]:
    for path in _files(root):
        tracker.note("files_seen")
        disposition = _disposition(root, path)
        if disposition == "excluded":
            tracker.note("files_excluded")
        elif disposition == "folded":
            tracker.note("files_folded")
            tracker.provenance(path)
            if path.name == "conversations.json":
                tracker.note("conversations_folded", _conversation_count(path))
        else:
            yield _record(root, path, tracker)
