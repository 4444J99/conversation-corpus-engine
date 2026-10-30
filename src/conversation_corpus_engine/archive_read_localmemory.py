"""Local-session memory: the folders `cce provider import --mode local-session` left behind.

Each folder (`claude-local-session-memory`, `chatgpt-local-session-memory`) holds a capture of the
desktop app's conversations (`source/`), the corpus the engine derived from it (`corpus/`,
`eval/`) and a few notes. This reader accounts for every file in one of three ways, so the file
count reconciles:

* a conversation payload (`conversations.json`, `conversation-summaries.json`, the
  `conversation-cache/` and `conversation-details/` folders) is *folded*: its conversations are the
  threads the account export already holds under the same ids, and a second copy would only make
  every search answer twice. Folding is a claim, so it is checked: every conversation id found in a
  folded payload must already be a conversation of the owning provider (the folder's name says
  which) in this store. An id that is not is counted as missing, and `verify` fails on it, so a
  captured conversation the export lacks can never be dropped without a trace;
* an account file (users, organizations, bootstrap) is *excluded* on purpose, as the other
  readers exclude account data;
* everything else (the derived corpus, the evaluation scorecards, the project and memory
  documents, the notes) is one machine record: the engine wrote it, or it is a record the app
  kept about the account's own memory.

Hidden files and symbolic links are not read; they are counted (`files_skipped`) so the folder's
file count still adds up.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path

from .archive_model import Conversation, Message, SourceTracker, stable_uid, to_iso

PROVIDER = "localmemory"
READER = "archive_read_localmemory@2"
FOLDED_FILES = frozenset({"conversations.json", "conversation-summaries.json"})
FOLDED_DIRS = frozenset({"conversation-cache", "conversation-details"})
EXCLUDED_FILES = frozenset({"users.json", "user.json", "organizations.json", "bootstrap.json"})
TEXT_SUFFIXES = frozenset({".json", ".md", ".txt", ".yaml", ".yml", ".csv", ".html"})
MAX_BYTES = 20 * 1024 * 1024
MAX_PAYLOAD_BYTES = 256 * 1024 * 1024
# The provider a capture folder belongs to, by the first word of the folder's name.
OWNERS = frozenset({"claude", "chatgpt"})
ID_KEYS = ("uuid", "conversation_id", "id")


def _files(root: Path) -> tuple[list[Path], int]:
    """(files in reading order, how many hidden files and links were skipped)."""
    found: list[Path] = []
    skipped = 0
    if not root.is_dir():
        return found, skipped
    for directory, dirs, names in os.walk(root, followlinks=False):
        kept = sorted(name for name in dirs if not name.startswith("."))
        skipped += len(dirs) - len(kept)
        dirs[:] = kept
        for name in sorted(names):
            path = Path(directory) / name
            if name.startswith(".") or path.is_symlink():
                skipped += 1
            else:
                found.append(path)
    return found, skipped


def _disposition(root: Path, path: Path) -> str:
    relative = path.relative_to(root)
    if path.name in EXCLUDED_FILES:
        return "excluded"
    if path.name in FOLDED_FILES or FOLDED_DIRS & set(relative.parts[:-1]):
        return "folded"
    return "record"


def detect(root: Path) -> bool:
    return bool(_files(root)[0])


def channels(root: Path) -> dict[str, list[str]]:
    files, skipped = _files(root)
    counts: dict[str, int] = {"record": 0, "folded": 0, "excluded": 0}
    for path in files:
        counts[_disposition(root, path)] += 1
    return {
        "read": [f"{counts['record']} file(s), one machine record each"],
        "not_read": [
            f"{counts['folded']} conversation payload file(s) — folded into the owning provider",
            f"{counts['excluded']} account file(s) — deliberately excluded",
            f"{skipped} hidden file(s) or link(s) — not read",
        ],
    }


def _owner(root: Path, path: Path) -> str:
    """The provider a folded payload belongs to, from its capture folder's name ('' if none)."""
    word = path.relative_to(root).parts[0].split("-", 1)[0]
    return word if word in OWNERS else ""


def _payload_ids(path: Path) -> set[str]:
    """The conversation ids a folded payload carries: its list items, or its own file name."""
    if path.name in FOLDED_FILES:
        if path.stat().st_size > MAX_PAYLOAD_BYTES:
            return set()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        if not isinstance(payload, list):
            return set()
        ids = (
            next((str(item[key]) for key in ID_KEYS if item.get(key)), "")
            for item in payload
            if isinstance(item, dict)
        )
        return {item for item in ids if item}
    return {path.stem}


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


def _fold_check(
    owner_ids: dict[str, set[str]],
    tracker: SourceTracker,
    known_uids: Callable[[str], set[str]] | None,
) -> None:
    """Hold every folded conversation id to the owning provider's conversations."""
    for owner, ids in sorted(owner_ids.items()):
        tracker.note("folded_ids", len(ids))
        if not owner:
            tracker.note("folded_ids_unowned", len(ids))
        elif known_uids is None:
            tracker.note("folded_ids_unchecked", len(ids))
        else:
            known = known_uids(owner)
            tracker.note(
                "folded_ids_missing", sum(1 for i in ids if stable_uid(owner, i) not in known)
            )


def iter_conversations(
    root: Path,
    tracker: SourceTracker,
    *,
    known_uids: Callable[[str], set[str]] | None = None,
) -> Iterator[Conversation]:
    files, skipped = _files(root)
    tracker.note("files_skipped", skipped)
    owner_ids: dict[str, set[str]] = {}
    for path in files:
        tracker.note("files_seen")
        disposition = _disposition(root, path)
        if disposition == "excluded":
            tracker.note("files_excluded")
        elif disposition == "folded":
            tracker.note("files_folded")
            tracker.provenance(path)
            ids = _payload_ids(path)
            if path.name == "conversations.json":
                tracker.note("conversations_folded", len(ids))
            owner_ids.setdefault(_owner(root, path), set()).update(ids)
        else:
            yield _record(root, path, tracker)
    _fold_check(owner_ids, tracker, known_uids)
