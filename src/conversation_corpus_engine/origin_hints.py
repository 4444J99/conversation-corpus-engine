"""Git-history origin hints (`cce.origin-hints.v1`) for document records.

A hint says who wrote a file of a software repository, as far as its git history can tell:
`agent` (every commit touching it is agent-signed), `mixed` (some are) or `human` (none is). The
rows come from `scripts/git_origin_hints.py`; this module loads them and matches them to library
records. It only ever SUBTRACTS owner credit: see `attribution_rules.record_info`.

Matching rule. A record names its file by `provenance["source_file"]`, `["source_path"]` or
`["source_ref"]`. Each is read as a path: backslashes become `/`, empty and `.` components are
dropped, everything is NFC-normalised, a `file://` prefix is removed, and for `source_ref` a
`#fragment` or `?query` is cut. A hint names its file by the components of `repo` followed by the
components of `path`. A hint matches a record when the hint's components are the LAST components
of the record's path, compared whole and case-sensitively (git paths are case-sensitive). So
`<anything>/<repo>/<path>` and plain `<repo>/<path>` match; `<path>` alone, without the repository
name, never does, and neither does a partial component (`xrepo/a.md` for `repo/a.md`). Of all the
hints that match, the one with the most components wins. If two matches of the same length
disagree about the origin (two hint rows for one file, or two of the record's own paths), the
record is `ambiguous`: unknown, never owner. Equal-length matches that agree are not ambiguous.
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = "cce.origin-hints.v1"
BASIS = "git-log"
AGENT = "agent"
MIXED = "mixed"
HUMAN = "human"
ORIGINS = (AGENT, MIXED, HUMAN)
AMBIGUOUS = "ambiguous"
LOCATOR_KEYS = ("source_file", "source_path", "source_ref")
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_ROWS = 2_000_000
MAX_LINE_BYTES = 64 * 1024
MAX_LISTED_FINDINGS = 20


class OriginHintsError(ValueError):
    """A hints file that cannot be used at all (unreadable, oversize) or that has findings."""


def components(value: Any, *, reference: bool = False) -> tuple[str, ...]:
    """The path components of a locator, as the matching rule reads them."""
    if not isinstance(value, str) or not value.strip():
        return ()
    text = unicodedata.normalize("NFC", value.strip()).replace("\\", "/")
    if text.casefold().startswith("file://"):
        text = text[len("file://") :]
    if reference:
        text = text.split("#", 1)[0].split("?", 1)[0]
    return tuple(part for part in text.split("/") if part not in ("", "."))


def _key(parts: tuple[str, ...]) -> str:
    return "/".join(parts)


def _check_row(row: Any) -> tuple[str, str] | str:
    """(key, origin) of a valid row, else the reason it is not valid (never row content)."""
    if not isinstance(row, dict):
        return "not an object"
    if row.get("schema") != SCHEMA:
        return "wrong or missing schema"
    for field_name in ("repo", "path"):
        value = row.get(field_name)
        if not isinstance(value, str) or not components(value):
            return f"{field_name} must be a non-empty string"
    path_parts = components(row["path"])
    if ".." in path_parts or ".." in components(row["repo"]):
        return "a path must not hold .."
    origin = row.get("origin")
    if origin not in ORIGINS:
        return "origin must be agent, mixed or human"
    commits = row.get("commits")
    if not isinstance(commits, dict):
        return "commits must be an object"
    counts = [commits.get(name) for name in ("total", "agent", "human")]
    if any(type(count) is not int or count < 0 for count in counts):
        return "commit counts must be non-negative integers"
    total, agent, human = counts
    if total < 1 or agent + human != total:
        return "commit counts do not add up"
    if (origin == AGENT) != (agent == total) or (origin == HUMAN) != (agent == 0):
        return "origin does not match the commit counts"
    signals = row.get("signals")
    if not isinstance(signals, list) or not all(isinstance(item, str) for item in signals):
        return "signals must be a list of strings"
    if row.get("basis") != BASIS:
        return "wrong or missing basis"
    return _key(components(row["repo"]) + path_parts), origin


@dataclass
class OriginHints:
    """A loaded hints file: the index plus what was wrong with it."""

    index: dict[str, str] = field(default_factory=dict)
    rows: int = 0
    finding_count: int = 0
    findings: list[str] = field(default_factory=list)

    def note(self, reason: str, line: int | None = None) -> None:
        """Record a finding: a row that could not be used (never any of its content)."""
        self.finding_count += 1
        if len(self.findings) < MAX_LISTED_FINDINGS:
            self.findings.append((f"line {line}: " if line is not None else "") + reason)

    def add(self, row: Any, line: int | None = None) -> bool:
        """Index one row; a row that is not valid is recorded as a finding instead."""
        checked = _check_row(row)
        if isinstance(checked, str):
            self.note(checked, line)
            return False
        key, origin = checked
        self.rows += 1
        known = self.index.get(key)
        self.index[key] = origin if known in (None, origin) else AMBIGUOUS
        return True

    @classmethod
    def from_rows(cls, rows: Iterable[Any]) -> OriginHints:
        hints = cls()
        for number, row in enumerate(rows, 1):
            hints.add(row, number)
        return hints

    def refuse_findings(self) -> None:
        """Fail closed: a row that could not be read would silently give back owner credit."""
        if self.finding_count:
            shown = "; ".join(self.findings)
            more = self.finding_count - len(self.findings)
            raise OriginHintsError(
                f"{self.finding_count} malformed origin-hint row(s): {shown}"
                + (f"; and {more} more" if more > 0 else "")
            )

    def _lookup(self, parts: tuple[str, ...]) -> tuple[int, str] | None:
        for length in range(len(parts), 1, -1):
            origin = self.index.get(_key(parts[-length:]))
            if origin is not None:
                return length, origin
        return None

    def match(self, provenance: Mapping[str, Any] | None) -> str | None:
        """`agent`, `mixed`, `human`, `ambiguous`, or None when no hint matches the record."""
        if not isinstance(provenance, Mapping):
            return None
        best: tuple[int, str] | None = None
        for name in LOCATOR_KEYS:
            parts = components(provenance.get(name), reference=name == "source_ref")
            found = self._lookup(parts) if parts else None
            if found is None:
                continue
            if best is None or found[0] > best[0]:
                best = found
            elif found[0] == best[0] and found[1] != best[1]:
                best = (best[0], AMBIGUOUS)
        return None if best is None else best[1]


def load_origin_hints(
    path: Path, *, max_bytes: int = MAX_FILE_BYTES, max_rows: int = MAX_ROWS
) -> OriginHints:
    """Read a hints JSONL file. An oversize or unreadable file is refused with OriginHintsError;
    a malformed row is skipped and listed in `findings` (the commands treat any finding as a
    refusal). Memory is bounded by the row cap: one short string per distinct file."""
    try:
        size = path.stat().st_size
    except OSError as error:
        raise OriginHintsError(f"cannot read the origin hints: {error.strerror}") from None
    if size > max_bytes:
        raise OriginHintsError(
            f"the origin hints file is {size} bytes, over the {max_bytes} byte limit"
        )
    hints = OriginHints()
    try:
        with path.open("rb") as handle:
            number = 0
            while True:
                raw = handle.readline(MAX_LINE_BYTES + 1)
                if not raw:
                    break
                number += 1
                if len(raw) > MAX_LINE_BYTES and not raw.endswith(b"\n"):
                    while (rest := handle.readline(MAX_LINE_BYTES)) and not rest.endswith(b"\n"):
                        continue
                    hints.note("line is too long", number)
                    continue
                if not raw.strip():
                    continue
                if hints.rows + hints.finding_count >= max_rows:
                    raise OriginHintsError(
                        f"the origin hints file has more than {max_rows} rows, over the limit"
                    )
                try:
                    row = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError, RecursionError):
                    hints.note("not valid JSON", number)
                    continue
                hints.add(row, number)
    except OSError as error:
        raise OriginHintsError(f"cannot read the origin hints: {error.strerror}") from None
    return hints
