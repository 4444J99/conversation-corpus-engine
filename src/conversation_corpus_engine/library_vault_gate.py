"""The size gate for a library reading vault: the Wiki Standard's caps, measured in one walk.

An editor that indexes a vault at start-up cannot open a multi-megabyte note, a hundred-thousand
entry folder or a vault of hundreds of thousands of notes. The standard's policy defaults draw the
lines this module checks:

* a note aims for 4 KiB, warns above 16 KiB and fails above 64 KiB;
* a folder warns above 200 entries and fails above 1,000;
* a vault warns above 10,000 notes or 50 MiB and fails above 20,000 notes or 100 MiB;
* a hub page holds at most 100 entries (a longer hub is paginated).

`measure_vault` reads metadata only (plus the short hub pages, to count their entries) and returns
the numbers with one finding per kind of breach. `measure_planned` gives the same report for a
vault that exists only as a mapping of paths to content, so a writer can refuse before it writes.
A `fail` finding means the vault must not be written or kept; a `warn` finding is reported and
never blocks.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .archive_limits import KIB, MIB, NOTE, Limits, file_class, take_census

HUB_DIRNAME = "hubs"
_HUB_ENTRY = re.compile(r"^- \[", re.MULTILINE)
REPORTED_EXAMPLES = 5


@dataclass(frozen=True)
class VaultCaps:
    """The standard's policy defaults for a reading vault."""

    note_target: int = 4 * KIB
    note_warn: int = 16 * KIB
    note_fail: int = 64 * KIB
    folder_warn: int = 200
    folder_fail: int = 1000
    vault_notes_warn: int = 10_000
    vault_notes_fail: int = 20_000
    vault_bytes_warn: int = 50 * MIB
    vault_bytes_fail: int = 100 * MIB
    hub_entries: int = 100

    def __post_init__(self) -> None:
        problems = []
        if not 0 < self.note_target <= self.note_warn:
            problems.append("note_target must be positive and no larger than note_warn")
        if self.note_warn < 2 * KIB or self.note_fail < self.note_warn:
            problems.append("note_warn must be at least 2048 and no larger than note_fail")
        if self.folder_warn < 2 or self.folder_fail < self.folder_warn:
            problems.append("folder_warn must be at least 2 and no larger than folder_fail")
        if not 0 < self.vault_notes_warn <= self.vault_notes_fail:
            problems.append("vault_notes_warn must be positive and no larger than vault_notes_fail")
        if not 0 < self.vault_bytes_warn <= self.vault_bytes_fail:
            problems.append("vault_bytes_warn must be positive and no larger than vault_bytes_fail")
        if self.hub_entries < 1:
            problems.append("hub_entries must be at least 1")
        if problems:
            raise ValueError("; ".join(problems))

    def as_dict(self) -> dict[str, int]:
        return asdict(self)

    def census_limits(self) -> Limits:
        return Limits(
            note_bytes=self.note_warn,
            note_ceiling=self.note_fail,
            folder_entries=self.folder_warn,
            folder_ceiling=self.folder_fail,
        )


DEFAULT_CAPS = VaultCaps()


def _finding(code: str, severity: str, **fields: Any) -> dict[str, Any]:
    return {"code": code, "severity": severity, **fields}


def _tiered(code: str, value: int, warn: int, fail: int, **fields: Any) -> list[dict[str, Any]]:
    """One finding for a single measured value against a warn and a fail line."""
    if value > fail:
        return [_finding(code, "fail", worst=value, limit=fail, **fields)]
    if value > warn:
        return [_finding(code, "warn", worst=value, limit=warn, **fields)]
    return []


def _assemble(stats: dict[str, Any], caps: VaultCaps) -> dict[str, Any]:
    """The report for measured numbers; the disk walk and a planned set share this verdict."""
    findings: list[dict[str, Any]] = []
    if stats["note_fail_count"]:
        findings.append(
            _finding(
                "note_size",
                "fail",
                count=stats["note_fail_count"],
                worst=stats["max_note_bytes"],
                limit=caps.note_fail,
                examples=stats["note_fail_examples"][:REPORTED_EXAMPLES],
            )
        )
    if stats["note_over_warn"] > stats["note_fail_count"]:
        findings.append(
            _finding(
                "note_size",
                "warn",
                count=stats["note_over_warn"] - stats["note_fail_count"],
                worst=stats["note_warn_worst"],
                limit=caps.note_warn,
            )
        )
    if stats["folder_fail_count"]:
        findings.append(
            _finding(
                "folder_entries",
                "fail",
                count=stats["folder_fail_count"],
                worst=stats["widest_folder"]["entries"],
                limit=caps.folder_fail,
                examples=stats["folder_fail_examples"][:REPORTED_EXAMPLES],
            )
        )
    if stats["folder_over_warn"] > stats["folder_fail_count"]:
        findings.append(
            _finding(
                "folder_entries",
                "warn",
                count=stats["folder_over_warn"] - stats["folder_fail_count"],
                worst=stats["folder_warn_worst"],
                limit=caps.folder_warn,
            )
        )
    findings += _tiered("vault_notes", stats["notes"], caps.vault_notes_warn, caps.vault_notes_fail)
    findings += _tiered("vault_bytes", stats["bytes"], caps.vault_bytes_warn, caps.vault_bytes_fail)
    over_hubs = [
        (path, entries) for path, entries in stats["hub_entries"] if entries > caps.hub_entries
    ]
    if over_hubs:
        findings.append(
            _finding(
                "hub_entries",
                "fail",
                count=len(over_hubs),
                worst=max(entries for _, entries in over_hubs),
                limit=caps.hub_entries,
                examples=[path for path, _ in over_hubs[:REPORTED_EXAMPLES]],
            )
        )
    complete = bool(stats["complete"])
    failed = any(item["severity"] == "fail" for item in findings)
    return {
        "notes": stats["notes"],
        "bytes": stats["bytes"],
        "max_note_bytes": stats["max_note_bytes"],
        "max_note_path": stats["max_note_path"],
        "folders": stats["folders"],
        "widest_folder": stats["widest_folder"],
        "hub_pages": len(stats["hub_entries"]),
        "max_hub_entries": max((entries for _, entries in stats["hub_entries"]), default=0),
        "findings": findings,
        "complete": complete,
        "ok": complete and not failed,
        "clean": complete and not findings,
        "caps": caps.as_dict(),
    }


def _hub_entries(root: Path) -> list[tuple[str, int]]:
    """(path, entry count) of every hub page below ``root``/hubs."""
    pages: list[tuple[str, int]] = []
    hubs = root / HUB_DIRNAME
    if hubs.is_dir() and not hubs.is_symlink():
        for page in sorted(hubs.rglob("*.md")):
            if page.is_symlink() or not page.is_file():
                continue
            text = page.read_text(encoding="utf-8", errors="replace")
            pages.append((page.relative_to(root).as_posix(), len(_HUB_ENTRY.findall(text))))
    return pages


def measure_vault(
    root: Path, caps: VaultCaps | None = None, *, deadline_seconds: float | None = None
) -> dict[str, Any]:
    """Walk a vault directory once and return its numbers and every breach of the caps.

    `ok` is true when the walk finished and nothing failed; `clean` is true when, in addition,
    nothing even warns. A walk cut short by its deadline is neither.
    """
    caps = caps or DEFAULT_CAPS
    census = take_census(root, caps.census_limits(), deadline_seconds=deadline_seconds)
    notes = census["classes"].get("note", {"files": 0, "bytes": 0, "max_bytes": 0})
    counts = census["violations"]["counts"]
    stats = {
        "notes": notes["files"],
        "bytes": census["bytes"],
        "max_note_bytes": notes["max_bytes"],
        "max_note_path": _largest_note(census),
        "folders": census["directories"],
        "widest_folder": census["widest_folder"],
        "note_fail_count": counts["notes"],
        "note_fail_examples": [item["path"] for item in census["violations"]["notes"]],
        "note_over_warn": census["above_target"]["notes"],
        "note_warn_worst": census["worst_within_ceiling"]["notes"],
        "folder_fail_count": counts["folders"],
        "folder_fail_examples": [item["path"] for item in census["violations"]["folders"]],
        "folder_over_warn": census["above_target"]["folders"],
        "folder_warn_worst": census["worst_within_ceiling"]["folders"],
        "hub_entries": _hub_entries(Path(root)),
        "complete": census["complete"],
    }
    return _assemble(stats, caps)


def measure_planned(
    files: Mapping[str, str | bytes], caps: VaultCaps | None = None, *, root: str = ""
) -> dict[str, Any]:
    """The same report for a vault that is only planned: ``files`` maps path to content.

    Paths are relative to one output root; ``root`` (for example ``"vault/"``) is the vault's
    folder below it, and the folders and hubs are measured from there. Nothing is read from disk,
    so a refusal can come before anything is written.
    """
    caps = caps or DEFAULT_CAPS
    children: dict[str, set[str]] = {"": set()}
    sizes: dict[str, int] = {}
    hubs: list[tuple[str, int]] = []
    for path, content in files.items():
        relative = path.removeprefix(root)
        data = content.encode("utf-8") if isinstance(content, str) else content
        sizes[relative] = len(data)
        parts = relative.split("/")
        for depth in range(len(parts)):
            children.setdefault("/".join(parts[:depth]), set()).add(parts[depth])
        if relative.startswith(HUB_DIRNAME + "/") and relative.endswith(".md"):
            text = data.decode("utf-8", errors="replace")
            hubs.append((relative, len(_HUB_ENTRY.findall(text))))
    notes = {path: size for path, size in sizes.items() if file_class(path) == NOTE}
    oversized = sorted(path for path, size in notes.items() if size > caps.note_fail)
    warned = [size for size in notes.values() if caps.note_warn < size <= caps.note_fail]
    entries = {folder: len(names) for folder, names in children.items()}
    crowded = sorted(folder for folder, count in entries.items() if count > caps.folder_fail)
    cautioned = [
        count for count in entries.values() if caps.folder_warn < count <= caps.folder_fail
    ]
    widest_folder = max(entries, key=lambda folder: (entries[folder], folder == ""), default="")
    largest = max(notes, key=lambda path: notes[path], default="")
    stats = {
        "notes": len(notes),
        "bytes": sum(sizes.values()),
        "max_note_bytes": notes.get(largest, 0),
        "max_note_path": largest,
        "folders": len(children),
        "widest_folder": {"path": widest_folder or ".", "entries": entries[widest_folder]},
        "note_fail_count": len(oversized),
        "note_fail_examples": oversized,
        "note_over_warn": len(oversized) + len(warned),
        "note_warn_worst": max(warned, default=0),
        "folder_fail_count": len(crowded),
        "folder_fail_examples": [folder or "." for folder in crowded],
        "folder_over_warn": len(crowded) + len(cautioned),
        "folder_warn_worst": max(cautioned, default=0),
        "hub_entries": sorted(hubs),
        "complete": True,
    }
    return _assemble(stats, caps)


def _largest_note(census: dict[str, Any]) -> str:
    for item in census["largest"]:
        if item["class"] == "note":
            return str(item["path"]).replace("\\", "/")
    return ""


def hard_cap_problems(report: dict[str, Any]) -> list[str]:
    """One line per failing finding (and one for an unfinished walk); empty means within the caps."""
    problems = []
    if not report["complete"]:
        problems.append("vault walk incomplete: it stopped at its deadline")
    for item in report["findings"]:
        if item["severity"] != "fail":
            continue
        line = f"{item['code']}: {item['worst']} exceeds the hard cap of {item['limit']}"
        if item.get("count", 1) > 1:
            line += f" ({item['count']} places)"
        if item.get("examples"):
            line += "; for example " + ", ".join(item["examples"])
        problems.append(line)
    return problems


def describe_findings(report: dict[str, Any]) -> str:
    """The findings of a report on one line, for a warning a person will read."""
    return "; ".join(
        f"{item['severity']} {item['code']} ({item['worst']} past {item['limit']})"
        for item in report["findings"]
    )
