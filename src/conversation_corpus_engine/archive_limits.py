"""The size and fan-out limits of the bounded archive, and the census that checks them.

An archive that cannot be opened is as lost as one that cannot be read. The failure comes in two
shapes: a handful of files so large that no editor, browser or indexer survives them, and a
folder with so many entries that a file manager, a vault indexer or a custody walk takes minutes
to list it. These limits are the contract that rules both out:

* a human-facing file (a note, a page, a hub) is cut into numbered parts at `NOTE_BYTES`;
* a folder holds at most `FOLDER_ENTRIES` entries, and a deterministic plan spreads the rest into
  sub-folders;
* a normalized-layer shard rolls over to a numbered sibling at `SHARD_BYTES`;
* SQLite files are the one exemption from the size ceiling: they are opened by an engine that
  pages them, never rendered, and they are sharded by provider and month anyway.

The census walks a tree once and reports every file and folder past the ceilings. It reads
metadata only, never file contents.
"""

from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

KIB = 1024
MIB = 1024 * KIB

NOTE_BYTES = 100 * KIB
NOTE_CEILING = 128 * KIB
DATA_CEILING = 64 * MIB
SHARD_BYTES = 48 * MIB
FOLDER_ENTRIES = 1800
FOLDER_CEILING = 2000

NOTE_SUFFIXES = frozenset({".md", ".html", ".htm"})
SQLITE_SUFFIXES = frozenset({".sqlite", ".sqlite3", ".db"})

NOTE = "note"
DATA = "data"
SQLITE = "sqlite"
REPORTED_VIOLATIONS = 20


@dataclass(frozen=True)
class Limits:
    """The limits a build plans to and a census checks against."""

    note_bytes: int = NOTE_BYTES
    note_ceiling: int = NOTE_CEILING
    data_ceiling: int = DATA_CEILING
    shard_bytes: int = SHARD_BYTES
    folder_entries: int = FOLDER_ENTRIES
    folder_ceiling: int = FOLDER_CEILING

    def __post_init__(self) -> None:
        problems = []
        if self.note_bytes < 2 * KIB:
            problems.append("note_bytes must be at least 2048")
        if self.note_ceiling < self.note_bytes:
            problems.append("note_ceiling may not be below note_bytes")
        if self.folder_entries < 2:
            problems.append("folder_entries must be at least 2")
        if self.folder_ceiling < self.folder_entries:
            problems.append("folder_ceiling may not be below folder_entries")
        if self.shard_bytes < MIB // 16:
            problems.append("shard_bytes must be at least 65536")
        if problems:
            raise ValueError("; ".join(problems))

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def file_class(name: str) -> str:
    """Which ceiling a file answers to, by its suffix."""
    suffix = Path(name).suffix.lower()
    if suffix in NOTE_SUFFIXES:
        return NOTE
    if suffix in SQLITE_SUFFIXES:
        return SQLITE
    return DATA


def _bump(summary: dict[str, dict[str, int]], kind: str, size: int) -> None:
    entry = summary.setdefault(kind, {"files": 0, "bytes": 0, "max_bytes": 0})
    entry["files"] += 1
    entry["bytes"] += size
    entry["max_bytes"] = max(entry["max_bytes"], size)


def take_census(
    root: Path, limits: Limits | None = None, *, deadline_seconds: float | None = None
) -> dict[str, Any]:
    """Walk `root` once and report sizes, fan-out and every violation of the ceilings.

    Only metadata is read. `complete` is false when the deadline stopped the walk early; a
    partial census never claims the tree is within its limits.
    """
    limits = limits or Limits()
    root = Path(root)
    if not root.is_dir():
        raise NotADirectoryError(str(root))
    deadline = None if deadline_seconds is None else time.monotonic() + deadline_seconds
    classes: dict[str, dict[str, int]] = {}
    over_notes: list[dict[str, Any]] = []
    over_data: list[dict[str, Any]] = []
    over_folders: list[dict[str, Any]] = []
    counts = {"notes": 0, "data": 0, "folders": 0}
    largest: list[tuple[int, str]] = []
    widest = (0, "")
    files = directories = total_bytes = 0
    complete = True
    pending = [root]
    while pending:
        if deadline is not None and time.monotonic() > deadline:
            complete = False
            break
        current = pending.pop()
        directories += 1
        entries = 0
        with os.scandir(current) as scan:
            for entry in scan:
                entries += 1
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                    continue
                size = entry.stat(follow_symlinks=False).st_size
                files += 1
                total_bytes += size
                kind = file_class(entry.name)
                _bump(classes, kind, size)
                largest.append((size, entry.path))
                if len(largest) > 64:
                    largest.sort(reverse=True)
                    del largest[8:]
                relative = os.path.relpath(entry.path, root)
                if kind == NOTE and size > limits.note_ceiling:
                    counts["notes"] += 1
                    if len(over_notes) < REPORTED_VIOLATIONS:
                        over_notes.append({"path": relative, "bytes": size})
                elif kind == DATA and size > limits.data_ceiling:
                    counts["data"] += 1
                    if len(over_data) < REPORTED_VIOLATIONS:
                        over_data.append({"path": relative, "bytes": size})
        if entries > widest[0]:
            widest = (entries, os.path.relpath(current, root))
        if entries > limits.folder_ceiling:
            counts["folders"] += 1
            if len(over_folders) < REPORTED_VIOLATIONS:
                over_folders.append({"path": os.path.relpath(current, root), "entries": entries})
    largest.sort(reverse=True)
    return {
        "root": str(root),
        "complete": complete,
        "files": files,
        "directories": directories,
        "bytes": total_bytes,
        "classes": classes,
        "largest": [
            {"path": os.path.relpath(path, root), "bytes": size, "class": file_class(path)}
            for size, path in largest[:5]
        ],
        "widest_folder": {"path": widest[1], "entries": widest[0]},
        "violations": {
            "counts": counts,
            "notes": over_notes,
            "data": over_data,
            "folders": over_folders,
        },
        "limits": limits.as_dict(),
        "ok": complete and not any(counts.values()),
    }


def census_problems(report: dict[str, Any]) -> list[str]:
    """One line per kind of violation in a census report; empty means the tree is within limits."""
    problems: list[str] = []
    limits = report["limits"]
    counts = report["violations"]["counts"]
    if not report["complete"]:
        problems.append("census incomplete: the walk stopped at its deadline")
    if counts["notes"]:
        worst = max(item["bytes"] for item in report["violations"]["notes"])
        problems.append(
            f"{counts['notes']} note/page file(s) above {limits['note_ceiling']} bytes "
            f"(largest reported {worst})"
        )
    if counts["data"]:
        worst = max(item["bytes"] for item in report["violations"]["data"])
        problems.append(
            f"{counts['data']} data file(s) above {limits['data_ceiling']} bytes "
            f"(largest reported {worst})"
        )
    if counts["folders"]:
        worst = max(item["entries"] for item in report["violations"]["folders"])
        problems.append(
            f"{counts['folders']} folder(s) above {limits['folder_ceiling']} entries "
            f"(widest reported {worst})"
        )
    return problems


def verify_limits(
    root: Path, limits: Limits | None = None, *, deadline_seconds: float | None = None
) -> list[str]:
    return census_problems(take_census(root, limits, deadline_seconds=deadline_seconds))
