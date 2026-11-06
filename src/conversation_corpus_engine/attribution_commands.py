"""`cce library attribute`: a read-only authorship report, annotation export and owner review.

Every command reads one published knowledge-library generation through the read-only `Library`
API and applies the rules of `attribution_rules.py` to the exact text of each message. Nothing is
written into the library. Work is bounded: `--limit-records` scans only the first N records (in
uid order) and `--deadline-seconds` is a wall-clock budget. Boilerplate detection needs a census
of the scanned slice first, so a scan reads the slice twice; the census, and therefore which copy
of a repeated text counts as the first, depends on the slice that was scanned.

* `--report` prints, per rule, spans, characters, share, review counts, precision and eligibility,
  the partition check (every character covered once) and the five largest categories;
* `apply --out FILE` writes the annotations in the `cce.span-attribution.v1` form that
  `cce library profile --annotations` reads, one `{source, attribution}` entry per message;
* `review-sample` draws the deterministic sample the owner confirms or rejects, and
  `review-record` appends the owner's answers to the append-only review ledger.

No rule is profile-eligible until the ledger holds 100 owner-reviewed spans for it at 95 percent
precision, and the ledger is only ever read here (`review-record` appends to it).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import sys
import time
import types
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from .archive_read_library import locate
from .attribution import attributed_spans, text_digest
from .attribution_rules import (
    AUTHOR_BY_RULE,
    DEFAULT_REPEAT_MIN_CHARS,
    DEFAULT_REPEAT_MIN_OCCURRENCES,
    GATE_MIN_PRECISION_PERCENT,
    GATE_MIN_REVIEWED,
    LEDGER_SCHEMA,
    REJECTED_TEXT_EVIDENCE,
    RULES,
    RULES_VERSION,
    SAMPLE_HEADER_SCHEMA,
    SAMPLE_PER_STRATUM,
    SAMPLE_SEED,
    STRATA,
    UNATTRIBUTED,
    UNKNOWN,
    VERDICTS,
    AssistantEcho,
    AttributedSpan,
    RecordInfo,
    RepeatCensus,
    ReviewSample,
    ReviewSampler,
    ReviewSummary,
    append_rows,
    attribute_message,
    blank_text,
    eligible_strata,
    exclusion_digest,
    ledger_row,
    load_json_line,
    parse_sample,
    partition_problems,
    preview_only_reasons,
    read_ledger,
    record_info,
    review_statistics,
    sample_header,
    sample_text,
    stratum_of,
    to_annotation,
    utc_stamp,
)
from .corpus_store import CorpusStoreError, require_git_private
from .library_build import DIRECTORY, current_generation
from .library_commands import STORE_ENV, configured_store, open_library
from .library_store import Library

DEFAULT_DEADLINE_SECONDS = 900.0
LARGEST_CATEGORIES = 5
DEADLINE_CHECK_INTERVAL = 256
# Consecutive user turns wait to be matched as one stretch up to this many characters.
USER_RUN_CHARACTERS = 2_000_000


class DeadlineReached(Exception):
    """The wall-clock budget ran out."""


class Deadline:
    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.seconds = seconds
        self._clock = clock
        self._start = clock()

    def check(self) -> None:
        if self._clock() - self._start > self.seconds:
            raise DeadlineReached(f"wall deadline of {self.seconds:g} seconds reached")


# --- reading the library ---------------------------------------------------------------------


@dataclass
class ScanStats:
    records_total: int = 0
    records_scanned: int = 0
    messages: int = 0
    unreferenceable: int = 0
    # Messages whose text cannot be encoded (a lone surrogate): counted, treated as unknown.
    unreadable: int = 0
    limited: bool = False
    deadline_reached: bool = False

    @property
    def complete(self) -> bool:
        return not self.limited and not self.deadline_reached


@dataclass(frozen=True)
class ScannedMessage:
    uid: str
    version: str
    revision_digest: str
    sequence: str
    index: int
    message_id: str
    # False when the id is empty or not unique in its sequence: no exact reference can name it.
    referenceable: bool
    text: str
    unreadable: bool
    role: str
    channel: str
    typed_chars: Any
    meta: Mapping[str, Any]
    record: RecordInfo


def _unencodable(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


def iter_messages(
    library: Library, stats: ScanStats, *, limit_records: int | None, deadline: Deadline
) -> Iterator[ScannedMessage]:
    """Every nonempty message of the current revision of each record, in uid order: main
    sequence first, then the alternate sequence, each by message index."""
    stats.records_total = int(
        library.connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    )
    counter = 0
    for row in library.rows():
        if limit_records is not None and stats.records_scanned >= limit_records:
            stats.limited = True
            return
        deadline.check()
        payload = library.record(row["uid"])
        stats.records_scanned += 1
        info = record_info(payload)
        extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
        for sequence, messages in (
            ("main", payload.get("messages")),
            ("alternate", extra.get("alternate_messages")),
        ):
            if not isinstance(messages, list):
                continue
            ids = Counter(
                item["id"]
                for item in messages
                if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]
            )
            for index, item in enumerate(messages):
                text = item.get("text") if isinstance(item, dict) else None
                if not isinstance(text, str) or not text:
                    continue
                counter += 1
                if counter % DEADLINE_CHECK_INTERVAL == 0:
                    deadline.check()
                message_id = item.get("id")
                meta = item.get("meta")
                yield ScannedMessage(
                    uid=row["uid"],
                    version=row["current_version"],
                    revision_digest=row["current_digest"],
                    sequence=sequence,
                    index=index,
                    message_id=message_id if isinstance(message_id, str) else "",
                    referenceable=isinstance(message_id, str)
                    and bool(message_id)
                    and ids[message_id] == 1,
                    text=text,
                    unreadable=not text.isascii() and _unencodable(text),
                    role=str(item.get("role") or ""),
                    channel=str(item.get("channel") or ""),
                    typed_chars=item.get("typed_chars"),
                    meta=meta if isinstance(meta, dict) else {},
                    record=info,
                )


def build_census(
    library: Library,
    *,
    limit_records: int | None,
    min_chars: int,
    min_occurrences: int,
    deadline: Deadline,
) -> RepeatCensus:
    census = RepeatCensus(min_chars=min_chars, min_occurrences=min_occurrences)
    for message in iter_messages(
        library, ScanStats(), limit_records=limit_records, deadline=deadline
    ):
        if not message.unreadable:
            census.observe(message.text)
    return census


# --- tallying a scan -------------------------------------------------------------------------


class Tally:
    """Per-rule spans and characters plus the independent partition check."""

    def __init__(self) -> None:
        self.spans: Counter[str] = Counter()
        self.characters: Counter[str] = Counter()
        self.stratum_spans: Counter[tuple[str, str]] = Counter()
        self.stratum_characters: Counter[tuple[str, str]] = Counter()
        self.messages = 0
        self.total_characters = 0
        self.covered_characters = 0
        self.problems: Counter[str] = Counter()
        self.unattributed_whitespace = 0
        self.unreadable_messages = 0

    def add_unreadable(self, text: str) -> None:
        """A message that cannot be encoded has no digest and no spans: all of it is unknown."""
        length = len(text)
        self.messages += 1
        self.total_characters += length
        self.covered_characters += length
        self.spans[UNATTRIBUTED] += 1
        self.characters[UNATTRIBUTED] += length
        self.stratum_spans[(UNATTRIBUTED, stratum_of(length))] += 1
        self.stratum_characters[(UNATTRIBUTED, stratum_of(length))] += length
        self.unreadable_messages += 1

    def add(self, text: str, spans: list[AttributedSpan]) -> None:
        self.messages += 1
        self.total_characters += len(text)
        for span in spans:
            self.spans[span.rule] += 1
            self.characters[span.rule] += span.end - span.start
            self.stratum_spans[(span.rule, span.stratum)] += 1
            self.stratum_characters[(span.rule, span.stratum)] += span.end - span.start
            self.covered_characters += span.end - span.start
            if span.rule == UNATTRIBUTED and blank_text(text[span.start : span.end]):
                self.unattributed_whitespace += span.end - span.start
        for name, count in partition_problems(text, spans, text_digest(text)).items():
            self.problems[name] += count


def scan(
    library: Library,
    census: RepeatCensus,
    *,
    limit_records: int | None,
    deadline: Deadline,
    visit: Callable[[ScannedMessage, list[AttributedSpan]], None] | None = None,
) -> tuple[ScanStats, Tally]:
    """Attribute every message of the slice; a deadline during this pass ends it, flagged.

    Consecutive user turns of one conversation (no other turn between them) are held back and
    matched against the earlier assistant turns as one stretch, so an answer pasted back in
    several messages is still found; at most `USER_RUN_CHARACTERS` characters wait at a time.
    """
    stats, tally = ScanStats(), Tally()
    echo, echo_key = AssistantEcho(), None
    pending: list[ScannedMessage] = []
    waiting = 0

    def attribute(message: ScannedMessage, pasted: tuple[tuple[int, int], ...]) -> None:
        stats.messages += 1
        spans = attribute_message(
            message.text,
            role=message.role,
            channel=message.channel,
            typed_chars=message.typed_chars,
            meta=message.meta,
            record=message.record,
            boilerplate=census.later_occurrences(message.text),
            pasted_back=pasted,
        )
        tally.add(message.text, spans)
        if not message.referenceable:
            stats.unreferenceable += 1
        if visit is not None:
            visit(message, spans)

    def release() -> None:
        nonlocal waiting
        if pending:
            found = echo.pasted_back_turns([item.text for item in pending])
            for item, pasted in zip(pending, found, strict=True):
                attribute(item, pasted)
            pending.clear()
        waiting = 0

    try:
        for message in iter_messages(
            library, stats, limit_records=limit_records, deadline=deadline
        ):
            if (message.uid, message.sequence) != echo_key:
                release()
                echo, echo_key = AssistantEcho(), (message.uid, message.sequence)
            if message.unreadable:
                release()
                stats.messages += 1
                stats.unreadable += 1
                tally.add_unreadable(message.text)
                continue
            role = message.role.strip().lower()
            if role == "user":
                pending.append(message)
                waiting += len(message.text)
                if waiting > USER_RUN_CHARACTERS:
                    release()
                continue
            release()
            if role in {"assistant", "model"}:
                echo.add_assistant(message.text)
            attribute(message, ())
    except DeadlineReached:
        stats.deadline_reached = True
    release()
    return stats, tally


# --- the report ------------------------------------------------------------------------------


def _share(part: int, whole: int) -> float:
    return round(part / whole, 6) if whole else 0.0


def build_report(
    stats: ScanStats,
    tally: Tally,
    summary: ReviewSummary,
    *,
    ledger: Path | None,
    malformed: int = 0,
    min_chars: int,
    min_occurrences: int,
) -> dict[str, Any]:
    total = tally.total_characters
    rows: list[dict[str, Any]] = []
    for rule in (*RULES, None):
        rule_id = rule.id if rule else UNATTRIBUTED
        strata: dict[str, dict[str, Any]] = {}
        for stratum in STRATA:
            review = summary.strata.get((rule_id, stratum))
            strata[stratum] = {
                "spans": tally.stratum_spans[(rule_id, stratum)],
                "characters": tally.stratum_characters[(rule_id, stratum)],
                "reviewed": review.reviewed if review else 0,
                "confirmed": review.confirmed if review else 0,
                "precision": None
                if review is None or review.precision is None
                else round(review.precision, 6),
                "eligible": bool(review and review.eligible),
            }
        rows.append(
            {
                "rule": rule_id,
                "author": AUTHOR_BY_RULE[rule_id],
                "spans": tally.spans[rule_id],
                "characters": tally.characters[rule_id],
                "share": _share(tally.characters[rule_id], total),
                "strata": strata,
                "eligible_strata": [name for name, item in strata.items() if item["eligible"]],
            }
        )
    authors: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = authors.setdefault(row["author"], {"spans": 0, "characters": 0})
        entry["spans"] += row["spans"]
        entry["characters"] += row["characters"]
    for entry in authors.values():
        entry["share"] = _share(entry["characters"], total)
    largest = sorted(
        (row for row in rows if row["characters"]),
        key=lambda row: (-row["characters"], row["rule"]),
    )[:LARGEST_CATEGORIES]
    problems = {name: tally.problems[name] for name in ("overlaps", "gaps", "uncovered")}
    other = sum(tally.problems[name] for name in ("out_of_range", "digest_mismatch"))
    return {
        "rules_version": RULES_VERSION,
        "scan": {
            "records_total": stats.records_total,
            "records_scanned": stats.records_scanned,
            "messages": tally.messages,
            "characters": total,
            "limited": stats.limited,
            "deadline_reached": stats.deadline_reached,
            "complete": stats.complete,
            "unreferenceable_messages": stats.unreferenceable,
            "unreadable_messages": stats.unreadable,
            "repeat_min_chars": min_chars,
            "repeat_min_occurrences": min_occurrences,
        },
        "partition": {
            "characters": total,
            "covered": tally.covered_characters,
            "coverage": _share(tally.covered_characters, total) if total else 1.0,
            **problems,
            "other_problems": other,
            "ok": tally.covered_characters == total and not any(problems.values()) and not other,
        },
        "rules": rows,
        "authors": authors,
        "largest_categories": [
            {
                "rule": row["rule"],
                "author": row["author"],
                "characters": row["characters"],
                "share": row["share"],
            }
            for row in largest
        ],
        "unattributed_whitespace_characters": tally.unattributed_whitespace,
        "gate": {
            "min_reviewed": GATE_MIN_REVIEWED,
            "min_precision_percent": GATE_MIN_PRECISION_PERCENT,
            "ledger": str(ledger) if ledger else None,
            "ledger_rows": summary.rows,
            "stale_rows": summary.stale_rows,
            "orphan_rows": summary.orphan_rows,
            "text_mismatch_rows": summary.text_mismatch_rows,
            "missing_rows": summary.missing_rows,
            "orphan_negative_rows": summary.orphan_negative_rows,
            "duplicate_text_rows": summary.duplicate_text_rows,
            "non_owner_rows": summary.non_owner_rows,
            "non_owner_reviewers": dict(sorted(summary.non_owner_reviewers.items())),
            "quarantined_rows": summary.quarantined_rows + malformed,
            "superseded_rows": summary.superseded_rows,
        },
        "profile_eligible": sorted(
            f"{rule}:{stratum}" for rule, stratum in eligible_strata(summary)
        ),
    }


def render_report(report: dict[str, Any]) -> str:
    scan_info, partition = report["scan"], report["partition"]
    state = "complete" if scan_info["complete"] else "PARTIAL"
    lines = [
        f"authorship rules {report['rules_version']}",
        f"scanned {scan_info['records_scanned']} of {scan_info['records_total']} records, "
        f"{scan_info['messages']} messages, {scan_info['characters']} characters ({state})",
        f"partition: {partition['covered']} of {partition['characters']} characters covered "
        f"({partition['coverage'] * 100:.2f}%), {partition['overlaps']} overlaps, "
        f"{partition['gaps']} gaps" + ("" if partition["ok"] else "  ** PARTITION CHECK FAILED **"),
        "",
        f"{'rule':<18}{'class':<13}{'stratum':>8}{'spans':>9}{'characters':>13}{'share':>9}"
        f"{'reviewed':>10}{'precision':>11}{'eligible':>10}",
    ]
    total_characters = max(partition["characters"], 1)
    for row in report["rules"]:
        shown = [
            (name, item)
            for name, item in row["strata"].items()
            if item["spans"] or item["reviewed"]
        ] or [("-", None)]
        for name, item in shown:
            if item is None:
                cells = f"{0:>9}{0:>13}{0.0:>8.2f}%{0:>10}{'-':>11}{'no':>10}"
            else:
                precision = "-" if item["precision"] is None else f"{item['precision'] * 100:.1f}%"
                share = item["characters"] / total_characters * 100
                cells = (
                    f"{item['spans']:>9}{item['characters']:>13}{share:>8.2f}%"
                    f"{item['reviewed']:>10}{precision:>11}{'yes' if item['eligible'] else 'no':>10}"
                )
            lines.append(f"{row['rule']:<18}{row['author']:<13}{name:>8}{cells}")
    if scan_info["unreadable_messages"]:
        lines.append(
            f"{scan_info['unreadable_messages']} message(s) could not be encoded and are counted "
            "as unknown"
        )
    lines.append("")
    lines.append(
        "largest categories: "
        + (
            ", ".join(
                f"{item['rule']} ({item['share'] * 100:.1f}%)"
                for item in report["largest_categories"]
            )
            or "none"
        )
    )
    gate = report["gate"]
    eligible = report["profile_eligible"]
    lines.append(
        f"gate: a rule and length stratum (S up to 240 characters, M up to 2000, L more) needs "
        f"{gate['min_reviewed']} owner-reviewed spans at {gate['min_precision_percent']}% "
        "precision; profile-eligible: " + (", ".join(eligible) if eligible else "none")
    )
    return "\n".join(lines)


# --- annotation export -----------------------------------------------------------------------


PARTIAL_SUFFIX = ".partial"


def _partial_path(path: Path) -> Path:
    return path.with_name(path.name + PARTIAL_SUFFIX)


def private_directories(directory: Path) -> None:
    """Create the missing directories of a path, each private (mode 0700). Existing ones are
    left as they are."""
    missing: list[Path] = []
    current = directory
    while not current.exists():
        missing.append(current)
        current = current.parent
    for folder in reversed(missing):
        try:
            folder.mkdir(mode=0o700)
        except FileExistsError:
            continue
        folder.chmod(0o700)


def _open_private(path: Path) -> TextIO:
    """A new text file, created exclusively with mode 0600 and never through a symlink."""
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    os.fchmod(descriptor, 0o600)
    return os.fdopen(descriptor, "w", encoding="utf-8")


class PrivateOutput:
    """A new private file written under its `.partial` name and renamed into place on commit.

    A complete output takes the requested name; one whose scan was cut short (a record limit or
    the deadline) keeps the `.partial` name, so it cannot pass for a whole export.
    """

    def __init__(self, out: Path) -> None:
        private_directories(out.parent)
        self.out = out
        self.temp = _partial_path(out)
        self.handle = _open_private(self.temp)

    def commit(self, *, partial: bool = False) -> Path:
        self.handle.close()
        if partial:
            return self.temp
        # A hard link is created only if nothing is at the output name, unlike a rename, which
        # would silently replace a file that appeared while the run was going on.
        try:
            os.link(self.temp, self.out)
        except FileExistsError:
            raise ValueError(f"output already exists: {self.out}") from None
        self.temp.unlink(missing_ok=True)
        return self.out

    def abort(self) -> None:
        self.handle.close()
        self.temp.unlink(missing_ok=True)


class AnnotationWriter(PrivateOutput):
    """Streams the annotation entries as one JSON list."""

    def __init__(self, out: Path) -> None:
        super().__init__(out)
        self.handle.write("[")
        self.count = 0

    def write(self, entry: dict[str, Any]) -> None:
        separator = "," if self.count else ""
        self.handle.write(separator + "\n" + json.dumps(entry, sort_keys=True, ensure_ascii=True))
        self.count += 1

    def commit(self, *, partial: bool = False) -> Path:
        self.handle.write("\n]\n")
        return super().commit(partial=partial)


def _inside(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


def _identity_of(path: Path, stat: Callable[[Path], Any]) -> tuple[int, int] | None:
    try:
        info = stat(path)
    except OSError:
        return None
    return info.st_dev, info.st_ino


def inside_by_identity(
    path: Path, roots: list[Path], *, stat: Callable[[Path], Any] = os.stat
) -> Path | None:
    """The protected root that `path` sits in or is, found by file identity rather than by
    spelling, or None. The existing ancestors of the path (the nearest one for a path whose tail
    does not exist yet, and every one above it) are compared by (device, inode) with the roots,
    so a different spelling of the same folder, such as another letter case on a case-insensitive
    volume, is recognised. Roots that do not exist protect nothing."""
    protected: dict[tuple[int, int], Path] = {}
    for root in roots:
        identity = _identity_of(root, stat)
        if identity is not None:
            protected.setdefault(identity, root)
    if not protected:
        return None
    for ancestor in (path, *path.parents):
        identity = _identity_of(ancestor, stat)
        if identity in protected:
            return protected[identity]
    return None


def library_roots(args: argparse.Namespace, database: Path) -> list[Path]:
    """Every place a writer must stay out of: the generation, its parent, the store the library
    sits in (found through the builder's `library-all` folder, a store folder given as the
    library, `--store-root` or the configured store)."""
    resolved = database.resolve()
    roots = [resolved.parent, resolved.parent.parent]
    roots.extend(
        ancestor.parent
        for ancestor in resolved.parents
        if ancestor.name.casefold() == DIRECTORY.casefold()
    )
    configured = [
        args.store_root,
        os.environ.get(STORE_ENV),
        os.environ.get("CCE_CORPUS_STORE_ROOT"),
        args.library if args.library is not None and args.library.is_dir() else None,
    ]
    roots.extend(Path(item).expanduser().resolve() for item in configured if item)
    return roots


def _refuse_store(path: Path, roots: list[Path]) -> Path:
    out = path.expanduser().absolute()
    resolved = out.parent.resolve() / out.name
    for root in roots:
        if _inside(resolved, root):
            raise ValueError(f"output must be outside the library store ({root}): {out}")
    for candidate in (out, resolved):
        root = inside_by_identity(candidate, roots)
        if root is not None:
            raise ValueError(f"output must be outside the library store ({root}): {out}")
    if out.is_symlink():
        raise ValueError(f"output must not be a symlink: {out}")
    return out


def check_output(path: Path, roots: list[Path]) -> Path:
    """Authorise a new output file before anything is written or any scan starts: outside the
    library store, not existing, no leftover `.partial` from an earlier run, and in a directory
    Git cannot see."""
    out = _refuse_store(path, roots)
    if out.exists():
        raise ValueError(f"output already exists: {out}")
    leftover = _partial_path(out)
    if leftover.exists() or leftover.is_symlink():
        raise ValueError(
            f"leftover partial output from an earlier run: {leftover} (remove or rename it)"
        )
    require_git_private(out.parent)
    return out


def check_ledger_path(path: Path, roots: list[Path]) -> Path:
    """The same custody for the append-only ledger, which may already exist."""
    ledger = _refuse_store(path, roots)
    require_git_private(ledger.parent)
    return ledger


def _stratum_of_evidence(span: dict[str, Any]) -> tuple[str, str]:
    """The (rule, stratum) an annotation entry names in its evidence."""
    rule = span["evidence"][0].split(":rule:")[1]
    return rule, span["evidence"][1].split(":")[1]


def message_entry(
    message: ScannedMessage,
    spans: list[AttributedSpan],
    *,
    owner_id: str | None,
    eligible: frozenset[tuple[str, str]],
    verdicts: Mapping[str, str] = types.MappingProxyType({}),
    rejected_texts: Collection[str] = frozenset(),
) -> dict[str, Any] | None:
    """`{source, attribution}` for one message, validated against the existing contract."""
    if not message.referenceable or all(span.author == UNKNOWN for span in spans):
        return None
    annotation = to_annotation(
        message.text,
        spans,
        owner_id=owner_id,
        eligible=eligible,
        verdicts=verdicts,
        rejected_texts=rejected_texts,
    )
    attributed_spans(message.text, annotation)
    return {
        "source": {
            "uid": message.uid,
            "version": message.version,
            "revision_digest": message.revision_digest,
            "sequence": message.sequence,
            "message": message.index,
            "message_id": message.message_id,
            "text_digest": annotation["text_digest"],
        },
        "attribution": annotation,
    }


# --- arguments -------------------------------------------------------------------------------


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive number") from None
    if not (number > 0 and math.isfinite(number)):
        raise argparse.ArgumentTypeError("must be a positive, finite number")
    return number


def _scan_arguments(parser: argparse.ArgumentParser, *, nested: bool) -> None:
    """The options every scanning command shares. A nested command must not reset a value the
    parent already parsed, so its defaults are suppressed."""

    def default(value: Any) -> Any:
        return argparse.SUPPRESS if nested else value

    parser.add_argument(
        "--library",
        type=Path,
        default=default(None),
        help="A library.sqlite file, a generation folder or a store folder (default: the store)",
    )
    parser.add_argument(
        "--store-root", "--corpus-store-root", dest="store_root", type=Path, default=default(None)
    )
    parser.add_argument(
        "--limit-records",
        type=_positive_int,
        default=default(None),
        help="Scan only the first N records in uid order",
    )
    parser.add_argument(
        "--deadline-seconds",
        type=_positive_float,
        default=default(DEFAULT_DEADLINE_SECONDS),
        help=f"Wall-clock budget (default {DEFAULT_DEADLINE_SECONDS:g})",
    )
    parser.add_argument(
        "--repeat-min-chars",
        type=_positive_int,
        default=default(DEFAULT_REPEAT_MIN_CHARS),
        help="Shortest repeated text that counts as boilerplate",
    )
    parser.add_argument(
        "--repeat-min-occurrences",
        type=_positive_int,
        default=default(DEFAULT_REPEAT_MIN_OCCURRENCES),
        help="Occurrences in the scanned slice that make a text boilerplate (at least 2)",
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        default=default(None),
        help="Owner-review ledger (JSONL) that decides which rules passed the precision gate",
    )
    parser.add_argument(
        "--owner-id",
        default=default(None),
        help=(
            "The owner identity: the author_id of owner spans, and the only reviewer whose "
            "ledger rows count toward the gate"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=default(False),
        help="Print JSON instead of text",
    )


def add_attribute_parser(library_actions: Any) -> None:
    """Register `cce library attribute` on the subparsers action of the `library` command."""
    attribute = library_actions.add_parser(
        "attribute",
        help="Deterministic authorship rules: report, annotation export and owner review",
    )
    _scan_arguments(attribute, nested=False)
    attribute.add_argument(
        "--report",
        action="store_true",
        help="Per-rule spans, characters, share, review counts, precision and eligibility",
    )
    sub = attribute.add_subparsers(dest="attribute_action", required=False)
    apply = sub.add_parser(
        "apply",
        help="Write cce.span-attribution.v1 annotations for `library profile --annotations`",
    )
    _scan_arguments(apply, nested=True)
    apply.add_argument("--out", type=Path, required=True, help="New annotation file (JSON list)")
    sample = sub.add_parser(
        "review-sample",
        help="Draw the deterministic sample of short spans the owner confirms or rejects",
    )
    _scan_arguments(sample, nested=True)
    sample.add_argument(
        "--per-stratum",
        "--per-rule",
        dest="per_stratum",
        type=_positive_int,
        default=SAMPLE_PER_STRATUM,
        help="Spans drawn for each rule and length stratum (S, M, L)",
    )
    sample.add_argument("--seed", default=SAMPLE_SEED)
    sample.add_argument("--out", type=Path, help="New JSONL file (default: standard output)")
    record = sub.add_parser(
        "review-record", help="Append the owner's verdicts on a sample to the review ledger"
    )
    record.add_argument(
        "--library",
        type=Path,
        default=argparse.SUPPRESS,
        help="The library the sample came from, so the ledger can be kept out of its store",
    )
    record.add_argument(
        "--store-root",
        "--corpus-store-root",
        dest="store_root",
        type=Path,
        default=argparse.SUPPRESS,
    )
    record.add_argument("--ledger", type=Path, required=True)
    record.add_argument(
        "--sample", type=Path, required=True, help="The review-sample file the answers belong to"
    )
    record.add_argument("--answers", type=Path, required=True, help="The sample, answered")
    record.add_argument("--reviewer", required=True)
    record.add_argument(
        "--reviewed-at", help="UTC time like 2026-01-02T03:04:05Z (default: the current time)"
    )
    record.add_argument(
        "--deadline-seconds",
        type=_positive_float,
        default=argparse.SUPPRESS,
        help=(
            "Wall-clock budget for drawing the sample again to check it "
            f"(default {DEFAULT_DEADLINE_SECONDS:g})"
        ),
    )
    record.add_argument("--json", action="store_true", default=argparse.SUPPRESS)


# --- command handlers ------------------------------------------------------------------------


def _database_path(args: argparse.Namespace) -> Path:
    """Where the library database is, without opening it."""
    if args.library is not None:
        path = args.library.expanduser()
        return path if path.is_file() else locate(path)[0]
    generation = current_generation(configured_store(args.store_root))
    if generation is None:
        raise ValueError("no library generation has been published")
    return generation / "library.sqlite"


def _open_target(args: argparse.Namespace) -> tuple[Library, Path]:
    if args.library is not None:
        database = _database_path(args)
        return Library(database), database
    generation, library = open_library(configured_store(args.store_root))
    return library, generation / "library.sqlite"


@dataclass
class LedgerView:
    """The ledger as read for one command, and the span ids the scan must look for."""

    path: Path | None
    rows: list[dict[str, Any]]
    wanted: set[str]
    # Lines of the file that could not be read: quarantined, never counted.
    malformed: int = 0


def _ledger_view(args: argparse.Namespace) -> LedgerView:
    if args.ledger is None:
        return LedgerView(None, [], set())
    contents = read_ledger(args.ledger)
    wanted = {row["span_id"] for row in contents.rows if row["rules_version"] == RULES_VERSION}
    return LedgerView(args.ledger, contents.rows, wanted, len(contents.quarantined))


class MatchRecorder:
    """Notes which ledger span ids exist in the scan, under which rule and stratum and with which
    exact text (the SHA-256 of the span's characters)."""

    def __init__(self, wanted: set[str]) -> None:
        self.wanted = wanted
        self.matched: dict[str, tuple[str, str, str]] = {}

    def visit(self, message: ScannedMessage, spans: list[AttributedSpan]) -> None:
        for span in spans:
            if span.span_id in self.wanted:
                self.matched[span.span_id] = (
                    span.rule,
                    span.stratum,
                    text_digest(message.text[span.start : span.end]),
                )


# Names of AI agents. An owner or reviewer identity is a person's: these bare names are refused.
AGENT_IDENTITIES = frozenset(
    {
        "claude",
        "chatgpt",
        "gpt",
        "gemini",
        "copilot",
        "codex",
        "cursor",
        "opencode",
        "assistant",
        "agent",
        "system",
        "bot",
    }
)


def _identity(value: str, flag: str) -> str:
    """An owner or reviewer identity as given, or ValueError: it must show something (not only
    spaces, zero-width or format characters or lone combining marks) and must not be the bare
    name of an AI agent."""
    if blank_text(value):
        raise ValueError(f"{flag} must not be empty or made only of invisible characters")
    if value.strip().casefold() in AGENT_IDENTITIES:
        raise ValueError(f"{flag} must name a person, not an AI agent ({value.strip()!r})")
    return value


def _owner(args: argparse.Namespace) -> str | None:
    owner = args.owner_id
    return None if owner is None else _identity(owner, "--owner-id")


def _census(library: Library, args: argparse.Namespace, deadline: Deadline) -> RepeatCensus:
    return build_census(
        library,
        limit_records=args.limit_records,
        min_chars=args.repeat_min_chars,
        min_occurrences=args.repeat_min_occurrences,
        deadline=deadline,
    )


def _emit(payload: dict[str, Any], text: str, as_json: bool) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) if as_json else text)


def _report(args: argparse.Namespace) -> int:
    view, owner = _ledger_view(args), _owner(args)
    recorder = MatchRecorder(view.wanted)
    deadline = Deadline(args.deadline_seconds)
    library, _ = _open_target(args)
    with library:
        census = _census(library, args, deadline)
        stats, tally = scan(
            library,
            census,
            limit_records=args.limit_records,
            deadline=deadline,
            visit=recorder.visit if view.wanted else None,
        )
    summary = review_statistics(view.rows, matched=recorder.matched, owner=owner)
    report = build_report(
        stats,
        tally,
        summary,
        ledger=view.path,
        malformed=view.malformed,
        min_chars=args.repeat_min_chars,
        min_occurrences=args.repeat_min_occurrences,
    )
    _emit(report, render_report(report), args.json)
    if stats.deadline_reached:
        print("attribute: wall deadline reached; the report is partial", file=sys.stderr)
        return 3
    return 0 if report["partition"]["ok"] else 1


def _apply(args: argparse.Namespace) -> int:
    view, owner = _ledger_view(args), _owner(args)
    deadline = Deadline(args.deadline_seconds)
    library, database = _open_target(args)
    with library:
        out = check_output(args.out, library_roots(args, database))
        census = _census(library, args, deadline)
        recorder = MatchRecorder(view.wanted)
        if view.wanted:
            # Which reviews exist in this scan decides what is eligible, so find out first.
            gate_stats, _ = scan(
                library,
                census,
                limit_records=args.limit_records,
                deadline=deadline,
                visit=recorder.visit,
            )
            if gate_stats.deadline_reached:
                raise DeadlineReached(
                    f"wall deadline of {args.deadline_seconds:g} seconds reached while "
                    "matching the review ledger"
                )
            census.rewind()
        summary = review_statistics(view.rows, matched=recorder.matched, owner=owner)
        eligible = eligible_strata(summary)
        writer = AnnotationWriter(out)
        counts: Counter[str] = Counter()

        def visit(message: ScannedMessage, spans: list[AttributedSpan]) -> None:
            entry = message_entry(
                message,
                spans,
                owner_id=owner,
                eligible=eligible,
                verdicts=summary.verdicts,
                rejected_texts=summary.rejected_texts,
            )
            if entry is None:
                counts["messages_without_annotation"] += 1
                return
            writer.write(entry)
            for span in entry["attribution"]["spans"]:
                counts[f"{span['author']}_spans"] += 1
                counts["profile_eligible_spans"] += bool(span["profile_eligible"])
                counts["spans_withheld_as_rejected_twin"] += (
                    REJECTED_TEXT_EVIDENCE in span["evidence"] and "owner_verdict" not in span
                )
                counts["spans_withheld_by_verdict"] += (
                    span["author"] == "owner"
                    and span.get("owner_verdict") in ("no", "unsure")
                    and _stratum_of_evidence(span) in eligible
                )

        try:
            stats, tally = scan(
                library, census, limit_records=args.limit_records, deadline=deadline, visit=visit
            )
            written = writer.commit(partial=not stats.complete)
        except BaseException:
            writer.abort()
            raise
    result = {
        "out": str(written),
        "partial": not stats.complete,
        "rules_version": RULES_VERSION,
        "entries": writer.count,
        "messages_scanned": tally.messages,
        "records_scanned": stats.records_scanned,
        "records_total": stats.records_total,
        "complete": stats.complete,
        "limited": stats.limited,
        "deadline_reached": stats.deadline_reached,
        "unreferenceable_messages": stats.unreferenceable,
        "unreadable_messages": stats.unreadable,
        "messages_without_annotation": counts["messages_without_annotation"],
        "owner_spans": counts["owner_spans"],
        "profile_eligible_spans": counts["profile_eligible_spans"],
        "spans_withheld_by_verdict": counts["spans_withheld_by_verdict"],
        "spans_withheld_as_rejected_twin": counts["spans_withheld_as_rejected_twin"],
        "eligible_strata": sorted(f"{rule}:{stratum}" for rule, stratum in eligible),
        "ledger": str(view.path) if view.path else None,
        "ledger_rows": summary.rows,
        "stale_rows": summary.stale_rows,
        "orphan_rows": summary.orphan_rows,
        "text_mismatch_rows": summary.text_mismatch_rows,
        "missing_rows": summary.missing_rows,
        "orphan_negative_rows": summary.orphan_negative_rows,
        "duplicate_text_rows": summary.duplicate_text_rows,
        "non_owner_rows": summary.non_owner_rows,
        "quarantined_rows": summary.quarantined_rows + view.malformed,
        "superseded_rows": summary.superseded_rows,
    }
    text = (
        f"wrote {writer.count} annotations for {tally.messages} messages to {written}"
        f" ({counts['owner_spans']} owner spans, {counts['profile_eligible_spans']} profile-eligible"
        f"; eligible: {', '.join(result['eligible_strata']) or 'none'})"
    )
    _emit(result, text, args.json)
    if stats.deadline_reached:
        print(
            f"attribute: wall deadline reached; the annotation file is partial and stays at {written}",
            file=sys.stderr,
        )
        return 3
    return 0


def library_fingerprint(library: Library) -> str:
    """Names the exact library a sample was drawn from: a digest of every record's uid, current
    version and current revision digest (the whole library, whatever slice was scanned)."""
    digest = hashlib.sha256()
    for row in library.rows():
        digest.update(f"{row['uid']}\t{row['current_version']}\t{row['current_digest']}\n".encode())
    return digest.hexdigest()


def _reviewed_by_owner(
    rows: Iterable[dict[str, Any]], owner: str
) -> tuple[dict[str, tuple[str, str, str]], set[tuple[str, str, str]]]:
    """What the owner already reviewed: the spans (by id, with the exact text they answered) and
    the texts (by rule, stratum and text key, whichever message they sit in and however they are
    spelled). Only current rows of the owner's own identity count (a row of another reviewer, or
    of another rules version or schema, hides nothing)."""
    current = [
        row
        for row in rows
        if row["rules_version"] == RULES_VERSION
        and row["schema"] == LEDGER_SCHEMA
        and row["reviewer"] == owner
    ]
    spans = {row["span_id"]: (row["rule"], row["stratum"], row["text_sha256"]) for row in current}
    texts = {(row["rule"], row["stratum"], row["text_key"]) for row in current}
    return spans, texts


def _review_sample(args: argparse.Namespace) -> int:
    owner = _owner(args)
    if owner is None:
        raise ValueError(
            "review-sample needs --owner-id: a sample is drawn for one owner, and only that "
            "owner's reviews count toward the gate"
        )
    view = _ledger_view(args)
    exclude, exclude_texts = _reviewed_by_owner(view.rows, owner)
    deadline = Deadline(args.deadline_seconds)
    library, database = _open_target(args)
    sampler = ReviewSampler(
        per_stratum=args.per_stratum,
        seed=args.seed,
        exclude=exclude,
        exclude_texts=exclude_texts,
    )
    with library:
        out = check_output(args.out, library_roots(args, database)) if args.out else None
        fingerprint = library_fingerprint(library)
        census = _census(library, args, deadline)
        stats, _ = scan(
            library,
            census,
            limit_records=args.limit_records,
            deadline=deadline,
            visit=lambda message, spans: [
                sampler.offer(span, message.text[span.start : span.end]) for span in spans
            ],
        )
    partial = not stats.complete
    rows = sampler.rows(partial=partial)
    header = sample_header(
        rows,
        library=fingerprint,
        owner_id=owner,
        seed=args.seed,
        per_stratum=args.per_stratum,
        partial=partial,
        repeat_min_chars=args.repeat_min_chars,
        repeat_min_occurrences=args.repeat_min_occurrences,
        exclusion_digest=exclusion_digest(view.rows, owner),
    )
    payload = sample_text(header, rows)
    summary = {
        "rules_version": RULES_VERSION,
        "sampled": len(rows),
        "sample_digest": header["sample_digest"],
        "per_stratum": header["drawn"],
        "complete": stats.complete,
        "partial": partial,
    }
    if out is None:
        sys.stdout.write(payload)
        print(json.dumps(summary, sort_keys=True), file=sys.stderr)
    else:
        writer = PrivateOutput(out)
        try:
            writer.handle.write(payload)
            written = writer.commit(partial=partial)
        except BaseException:
            writer.abort()
            raise
        _emit(
            summary | {"out": str(written)},
            f"wrote {len(rows)} sample spans to {written}",
            args.json,
        )
    if stats.deadline_reached:
        print("attribute: wall deadline reached; the sample is partial", file=sys.stderr)
        return 3
    return 0


def _answers(path: Path, sample: ReviewSample) -> dict[str, str | None]:
    """The owner's answers by span id: each must name a row of the sample (its rule, stratum,
    rules version and, when echoed, its text hash must agree) and carry a verdict or none. The
    answers file may be the sample file itself, filled in: a header line is ignored, but it must
    be this sample's header."""
    by_id = {row["span_id"]: row for row in sample.rows}
    answers: dict[str, str | None] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), start=1):
        if not line.strip():
            continue
        answer = load_json_line(line, "answers", number)
        if not isinstance(answer, dict):
            raise ValueError(f"answers line {number}: must be an object")
        if answer.get("schema") == SAMPLE_HEADER_SCHEMA:
            if answer != sample.header:
                raise ValueError(
                    f"answers line {number}: the header is not the header of this sample"
                )
            continue
        bound = by_id.get(str(answer.get("span_id")))
        if (
            bound is None
            or answer.get("rule") != bound["rule"]
            or answer.get("stratum") != bound["stratum"]
            or answer.get("rules_version", bound["rules_version"]) != bound["rules_version"]
            or answer.get("text_sha256", bound["text_sha256"]) != bound["text_sha256"]
        ):
            raise ValueError(
                f"answers line {number}: the span id, rule and stratum are not those of a row "
                "of the sample"
            )
        if bound["span_id"] in answers:
            raise ValueError(f"answers line {number}: the span is answered twice")
        verdict = answer.get("verdict")
        if verdict in (None, ""):
            answers[bound["span_id"]] = None
            continue
        verdict = str(verdict).strip().lower()
        if verdict not in VERDICTS:
            raise ValueError(
                f"answers line {number}: verdict must be one of {list(VERDICTS)} "
                "(there is no skip: leave a row blank and it is recorded as unsure)"
            )
        answers[bound["span_id"]] = verdict
    return answers


def _row_without_answer(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != "verdict"}


def _why_not_the_draw(shown: list[dict[str, Any]], drawn: list[dict[str, Any]]) -> str:
    """What differs between the rows of a sample and the rows a fresh draw gives."""
    shown_ids = [row["span_id"] for row in shown]
    drawn_ids = [row["span_id"] for row in drawn]
    extra = len(set(shown_ids) - set(drawn_ids))
    lacking = len(set(drawn_ids) - set(shown_ids))
    if extra or lacking:
        return (
            f"it holds {extra} row(s) a fresh draw would not give and lacks {lacking} row(s) "
            f"it would give ({len(shown_ids)} rows against {len(drawn_ids)})"
        )
    if shown_ids != drawn_ids:
        return "its rows are not in the order of the draw"
    return "the content of its rows differs from the draw's"


def _check_the_draw(
    database: Path,
    header: Mapping[str, Any],
    rows: list[dict[str, Any]],
    existing: list[dict[str, Any]],
    seconds: float,
) -> None:
    """Draw again, from the library as it is now and the ledger as it stands, with the header's
    own settings, and refuse the sample unless its rows are exactly that draw (the same span ids
    in the same order, with the same content). The draw is deterministic, so a sample that was
    cut, reordered, padded or hand-written, even under a header with a correct digest, library
    fingerprint and ledger state, is not what `review-sample` gives and is refused."""
    exclude, exclude_texts = _reviewed_by_owner(existing, header["owner_id"])
    sampler = ReviewSampler(
        per_stratum=header["per_stratum"],
        seed=header["seed"],
        exclude=exclude,
        exclude_texts=exclude_texts,
    )
    deadline = Deadline(seconds)
    with Library(database) as library:
        census = build_census(
            library,
            limit_records=None,
            min_chars=header["repeat_min_chars"],
            min_occurrences=header["repeat_min_occurrences"],
            deadline=deadline,
        )
        stats, _ = scan(
            library,
            census,
            limit_records=None,
            deadline=deadline,
            visit=lambda message, spans: [
                sampler.offer(span, message.text[span.start : span.end]) for span in spans
            ],
        )
    if stats.deadline_reached:
        raise DeadlineReached(
            f"wall deadline of {seconds:g} seconds reached while drawing the sample again "
            "to check it"
        )
    shown = [_row_without_answer(row) for row in rows]
    drawn = [_row_without_answer(row) for row in sampler.rows(partial=False)]
    if shown != drawn:
        raise ValueError(
            "this sample is not the draw the rules give for this library and this ledger: "
            f"{_why_not_the_draw(shown, drawn)}. A sample must be exactly what `review-sample` "
            "draws, in the same order; draw it again and answer all of it"
        )


def _review_record(args: argparse.Namespace) -> int:
    # The one place a clock is read, at the command boundary; the ledger helpers take the time.
    reviewed_at = args.reviewed_at or utc_stamp(datetime.now(UTC))
    reviewer = _identity(args.reviewer, "--reviewer")
    database = _database_path(args)
    ledger = check_ledger_path(args.ledger, library_roots(args, database))
    sample = parse_sample(args.sample.read_text(encoding="utf-8").split("\n"))
    header = sample.header
    preview = preview_only_reasons(header)
    if preview:
        raise ValueError(
            f"this sample cannot be recorded: {'; '.join(preview)}. It is a preview; draw the "
            "sample again with the default seed and repeat-census settings over the whole library"
        )
    if reviewer != header["owner_id"]:
        raise ValueError(
            "this sample was drawn for another owner identity; only that owner can record it"
        )
    with Library(database) as library:
        if library_fingerprint(library) != header["library"]:
            raise ValueError(
                "this sample was drawn from a different library than the one named here; "
                "draw a new sample"
            )
    answers = _answers(args.answers, sample)
    # Every row the sample drew is accounted for: a row without a verdict is unsure.
    rows: list[dict[str, Any]] = []
    unanswered = 0
    for row in sample.rows:
        verdict = answers.get(row["span_id"])
        unanswered += verdict is None
        rows.append(
            ledger_row(
                row["span_id"],
                row["rule"],
                verdict or "unsure",
                reviewer,
                reviewed_at,
                stratum=row["stratum"],
                sample_digest=header["sample_digest"],
                text_sha256=row["text_sha256"],
                text_key=row["text_key"],
                drawn=header["drawn"][f"{row['rule']}:{row['stratum']}"],
                rules_version=row["rules_version"],
            )
        )
    private_directories(ledger.parent)

    checked: set[str] = set()

    def drawn_against_this_ledger(existing: list[dict[str, Any]]) -> None:
        state = exclusion_digest(existing, reviewer)
        if state != header["exclusion_digest"]:
            raise ValueError(
                "this sample was drawn against a different ledger state than the one it would be "
                "recorded into; draw again against this ledger (a sample can be recorded once "
                "into a ledger in the state it was drawn against)"
            )
        if state not in checked:
            # While the ledger is locked (a ledger that does not exist yet has nothing to lock,
            # and is checked before it is created, then again if it changed meanwhile).
            _check_the_draw(database, header, sample.rows, existing, args.deadline_seconds)
            checked.add(state)

    appended = append_rows(ledger, rows, precondition=drawn_against_this_ledger)
    counts = Counter(f"{row['rule']}:{row['stratum']}" for row in rows)
    result = {
        "ledger": str(args.ledger),
        "appended": appended.appended,
        "unanswered": unanswered,
        "recorded_as_unsure": unanswered,
        "sample_digest": header["sample_digest"],
        "repaired_torn_line": appended.repaired_torn_line,
        "strata": {key: {"appended": count} for key, count in sorted(counts.items())},
    }
    _emit(
        result,
        f"appended {appended.appended} verdicts to {args.ledger} "
        f"({unanswered} unanswered, recorded as unsure)"
        + (" after repairing a cut-short last line" if appended.repaired_torn_line else ""),
        args.json,
    )
    return 0


def handle_attribute(args: argparse.Namespace) -> int:
    action = getattr(args, "attribute_action", None)
    try:
        if action is None and not args.report:
            raise ValueError("choose --report, apply, review-sample or review-record")
        if action is not None and args.report:
            raise ValueError("--report cannot be combined with a subcommand")
        if action is None:
            return _report(args)
        handler = {
            "apply": _apply,
            "review-sample": _review_sample,
            "review-record": _review_record,
        }[action]
        return handler(args)
    except DeadlineReached as error:
        print(
            f"attribute: {error}; nothing was written",
            file=sys.stderr,
        )
        return 3
    except (ValueError, OSError, KeyError, CorpusStoreError, sqlite3.Error) as error:
        print(f"attribute: {error}", file=sys.stderr)
        return 2
