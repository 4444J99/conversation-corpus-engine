"""The size gate for a library reading vault: the Wiki Standard's caps, measured in one walk.

An editor that indexes a vault at start-up cannot open a multi-megabyte note, a hundred-thousand
entry folder or a vault of hundreds of thousands of notes. The standard's policy defaults draw the
lines this module checks:

* a note aims for 4 KiB, warns above 16 KiB and fails above 64 KiB;
* a folder warns above 200 entries and fails above 1,000;
* a vault warns above 10,000 notes or 50 MiB and fails above 20,000 notes or 100 MiB;
* a hub page holds at most 100 entries (a longer hub is paginated).

`measure_vault` reads metadata only (plus the short hub pages, to count their entries) and returns
the numbers with one finding per kind of breach. A `fail` finding means the vault must not be
written or kept; a `warn` finding is reported and never blocks.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .archive_limits import KIB, MIB, Limits, take_census

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


def _hub_pages(root: Path, caps: VaultCaps) -> tuple[int, int, list[tuple[str, int]]]:
    """Hub page count, the most entries on one page, and the pages past the entry cap."""
    pages = widest = 0
    over: list[tuple[str, int]] = []
    hubs = root / HUB_DIRNAME
    if hubs.is_dir() and not hubs.is_symlink():
        for page in sorted(hubs.rglob("*.md")):
            if page.is_symlink() or not page.is_file():
                continue
            pages += 1
            entries = len(_HUB_ENTRY.findall(page.read_text(encoding="utf-8", errors="replace")))
            widest = max(widest, entries)
            if entries > caps.hub_entries:
                over.append((page.relative_to(root).as_posix(), entries))
    return pages, widest, over


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
    above = census["above_target"]
    findings: list[dict[str, Any]] = []

    oversized = census["violations"]["notes"]
    if counts["notes"]:
        findings.append(
            _finding(
                "note_size",
                "fail",
                count=counts["notes"],
                worst=notes["max_bytes"],
                limit=caps.note_fail,
                examples=[item["path"] for item in oversized[:REPORTED_EXAMPLES]],
            )
        )
    if above["notes"] > counts["notes"]:
        findings.append(
            _finding(
                "note_size",
                "warn",
                count=above["notes"] - counts["notes"],
                worst=min(notes["max_bytes"], caps.note_fail),
                limit=caps.note_warn,
            )
        )
    crowded = census["violations"]["folders"]
    if counts["folders"]:
        findings.append(
            _finding(
                "folder_entries",
                "fail",
                count=counts["folders"],
                worst=census["widest_folder"]["entries"],
                limit=caps.folder_fail,
                examples=[item["path"] for item in crowded[:REPORTED_EXAMPLES]],
            )
        )
    if above["folders"] > counts["folders"]:
        findings.append(
            _finding(
                "folder_entries",
                "warn",
                count=above["folders"] - counts["folders"],
                worst=min(census["widest_folder"]["entries"], caps.folder_fail),
                limit=caps.folder_warn,
            )
        )
    findings += _tiered("vault_notes", notes["files"], caps.vault_notes_warn, caps.vault_notes_fail)
    findings += _tiered(
        "vault_bytes", census["bytes"], caps.vault_bytes_warn, caps.vault_bytes_fail
    )
    hub_pages, widest_hub, over_hubs = _hub_pages(Path(root), caps)
    if over_hubs:
        findings.append(
            _finding(
                "hub_entries",
                "fail",
                count=len(over_hubs),
                worst=widest_hub,
                limit=caps.hub_entries,
                examples=[path for path, _ in over_hubs[:REPORTED_EXAMPLES]],
            )
        )
    complete = bool(census["complete"])
    failed = any(item["severity"] == "fail" for item in findings)
    return {
        "notes": notes["files"],
        "bytes": census["bytes"],
        "max_note_bytes": notes["max_bytes"],
        "max_note_path": _largest_note(census),
        "folders": census["directories"],
        "widest_folder": census["widest_folder"],
        "hub_pages": hub_pages,
        "max_hub_entries": widest_hub,
        "findings": findings,
        "complete": complete,
        "ok": complete and not failed,
        "clean": complete and not findings,
        "caps": caps.as_dict(),
    }


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
