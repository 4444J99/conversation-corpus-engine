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
import json
import sqlite3
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .archive_read_library import locate
from .attribution import attributed_spans, text_digest
from .attribution_rules import (
    AUTHOR_BY_RULE,
    DEFAULT_REPEAT_MIN_CHARS,
    DEFAULT_REPEAT_MIN_OCCURRENCES,
    GATE_MIN_PRECISION_PERCENT,
    GATE_MIN_REVIEWED,
    RULES,
    RULES_VERSION,
    SAMPLE_PER_STRATUM,
    SAMPLE_SEED,
    STRATA,
    UNATTRIBUTED,
    UNKNOWN,
    VERDICTS,
    AttributedSpan,
    RecordInfo,
    RepeatCensus,
    ReviewSampler,
    ReviewSummary,
    append_ledger,
    attribute_message,
    eligible_strata,
    ledger_row,
    partition_problems,
    read_ledger,
    record_info,
    review_statistics,
    to_annotation,
    utc_stamp,
)
from .corpus_store import CorpusStoreError
from .library_commands import configured_store, open_library
from .library_store import Library

DEFAULT_DEADLINE_SECONDS = 900.0
LARGEST_CATEGORIES = 5
DEADLINE_CHECK_INTERVAL = 256


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
    role: str
    channel: str
    typed_chars: Any
    meta: Mapping[str, Any]
    record: RecordInfo


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

    def add(self, text: str, spans: list[AttributedSpan]) -> None:
        self.messages += 1
        self.total_characters += len(text)
        for span in spans:
            self.spans[span.rule] += 1
            self.characters[span.rule] += span.end - span.start
            self.stratum_spans[(span.rule, span.stratum)] += 1
            self.stratum_characters[(span.rule, span.stratum)] += span.end - span.start
            self.covered_characters += span.end - span.start
            if span.rule == UNATTRIBUTED and not text[span.start : span.end].strip():
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
    """Attribute every message of the slice; a deadline during this pass ends it, flagged."""
    stats, tally = ScanStats(), Tally()
    try:
        for message in iter_messages(
            library, stats, limit_records=limit_records, deadline=deadline
        ):
            stats.messages += 1
            spans = attribute_message(
                message.text,
                role=message.role,
                channel=message.channel,
                typed_chars=message.typed_chars,
                meta=message.meta,
                record=message.record,
                boilerplate=census.later_occurrences(message.text),
            )
            tally.add(message.text, spans)
            if not message.referenceable:
                stats.unreferenceable += 1
            if visit is not None:
                visit(message, spans)
    except DeadlineReached:
        stats.deadline_reached = True
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


class AnnotationWriter:
    """Streams a JSON list to a temporary file and renames it into place on commit."""

    def __init__(self, out: Path) -> None:
        if out.exists() or out.is_symlink():
            raise ValueError(f"output already exists: {out}")
        self.out = out
        self.temp = out.with_name(out.name + ".partial")
        self.handle = self.temp.open("x", encoding="utf-8")
        self.handle.write("[")
        self.count = 0

    def write(self, entry: dict[str, Any]) -> None:
        separator = "," if self.count else ""
        self.handle.write(separator + "\n" + json.dumps(entry, sort_keys=True, ensure_ascii=True))
        self.count += 1

    def commit(self, *, partial: bool = False) -> Path:
        """Close the JSON list. A complete file takes the requested name; a partial one (the
        scan was cut short) keeps the `.partial` name so it cannot pass for a whole export."""
        self.handle.write("\n]\n")
        self.handle.close()
        if partial:
            return self.temp
        self.temp.replace(self.out)
        return self.out

    def abort(self) -> None:
        self.handle.close()
        self.temp.unlink(missing_ok=True)


def message_entry(
    message: ScannedMessage,
    spans: list[AttributedSpan],
    *,
    owner_id: str | None,
    eligible: frozenset[str],
) -> dict[str, Any] | None:
    """`{source, attribution}` for one message, validated against the existing contract."""
    if not message.referenceable or all(span.author == UNKNOWN for span in spans):
        return None
    annotation = to_annotation(message.text, spans, owner_id=owner_id, eligible=eligible)
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
    if not number > 0:
        raise argparse.ArgumentTypeError("must be a positive number")
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
    apply.add_argument(
        "--owner-id",
        help="author_id of owner spans; a strict profile counts only spans bound to its persona",
    )
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
    record.add_argument("--ledger", type=Path, required=True)
    record.add_argument("--answers", type=Path, required=True, help="The sample, answered")
    record.add_argument("--reviewer", required=True)
    record.add_argument(
        "--reviewed-at", help="UTC time like 2026-01-02T03:04:05Z (default: the current time)"
    )
    record.add_argument("--json", action="store_true", default=argparse.SUPPRESS)


# --- command handlers ------------------------------------------------------------------------


def _open_target(args: argparse.Namespace) -> tuple[Library, Path]:
    if args.library is not None:
        path = args.library.expanduser()
        database = path if path.is_file() else locate(path)[0]
        return Library(database), database
    generation, library = open_library(configured_store(args.store_root))
    return library, generation / "library.sqlite"


def _summary(args: argparse.Namespace) -> tuple[ReviewSummary, Path | None]:
    if args.ledger is None:
        return review_statistics([]), None
    return review_statistics(read_ledger(args.ledger)), args.ledger


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
    summary, ledger = _summary(args)
    deadline = Deadline(args.deadline_seconds)
    library, _ = _open_target(args)
    with library:
        census = _census(library, args, deadline)
        stats, tally = scan(library, census, limit_records=args.limit_records, deadline=deadline)
    report = build_report(
        stats,
        tally,
        summary,
        ledger=ledger,
        min_chars=args.repeat_min_chars,
        min_occurrences=args.repeat_min_occurrences,
    )
    _emit(report, render_report(report), args.json)
    if stats.deadline_reached:
        print("attribute: wall deadline reached; the report is partial", file=sys.stderr)
        return 3
    return 0 if report["partition"]["ok"] else 1


def _inside(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


def _apply(args: argparse.Namespace) -> int:
    if args.owner_id is not None and not args.owner_id.strip():
        raise ValueError("--owner-id must be nonempty")
    summary, ledger = _summary(args)
    eligible = eligible_strata(summary)
    deadline = Deadline(args.deadline_seconds)
    library, database = _open_target(args)
    with library:
        out = args.out.expanduser().absolute()
        if _inside(out.parent.resolve() / out.name, database.parent.resolve()):
            raise ValueError("--out must be outside the library generation (it is read-only)")
        if out.exists() or out.is_symlink():
            raise ValueError(f"output already exists: {out}")
        census = _census(library, args, deadline)
        writer = AnnotationWriter(out)
        counts: Counter[str] = Counter()

        def visit(message: ScannedMessage, spans: list[AttributedSpan]) -> None:
            entry = message_entry(message, spans, owner_id=args.owner_id, eligible=eligible)
            if entry is None:
                counts["messages_without_annotation"] += 1
                return
            writer.write(entry)
            for span in entry["attribution"]["spans"]:
                counts[f"{span['author']}_spans"] += 1
                counts["profile_eligible_spans"] += bool(span["profile_eligible"])

        try:
            stats, tally = scan(
                library, census, limit_records=args.limit_records, deadline=deadline, visit=visit
            )
            written = writer.commit(partial=stats.deadline_reached)
        except BaseException:
            writer.abort()
            raise
    result = {
        "out": str(written),
        "partial": stats.deadline_reached,
        "rules_version": RULES_VERSION,
        "entries": writer.count,
        "messages_scanned": tally.messages,
        "records_scanned": stats.records_scanned,
        "records_total": stats.records_total,
        "complete": stats.complete,
        "limited": stats.limited,
        "deadline_reached": stats.deadline_reached,
        "unreferenceable_messages": stats.unreferenceable,
        "messages_without_annotation": counts["messages_without_annotation"],
        "owner_spans": counts["owner_spans"],
        "profile_eligible_spans": counts["profile_eligible_spans"],
        "eligible_strata": sorted(f"{rule}:{stratum}" for rule, stratum in eligible),
        "ledger": str(ledger) if ledger else None,
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


def _review_sample(args: argparse.Namespace) -> int:
    exclude: set[str] = set()
    if args.ledger is not None:
        exclude = {
            row["span_id"]
            for row in read_ledger(args.ledger)
            if row["rules_version"] == RULES_VERSION
        }
    deadline = Deadline(args.deadline_seconds)
    library, _ = _open_target(args)
    sampler = ReviewSampler(per_stratum=args.per_stratum, seed=args.seed, exclude=exclude)
    with library:
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
    rows = sampler.rows()
    payload = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    summary = {
        "rules_version": RULES_VERSION,
        "sampled": len(rows),
        "per_stratum": dict(Counter(f"{row['rule']}:{row['stratum']}" for row in rows)),
        "complete": stats.complete,
    }
    if args.out is None:
        sys.stdout.write(payload)
        print(json.dumps(summary, sort_keys=True), file=sys.stderr)
    else:
        with args.out.expanduser().open("x", encoding="utf-8") as handle:
            handle.write(payload)
        _emit(
            summary | {"out": str(args.out)},
            f"wrote {len(rows)} sample spans to {args.out}",
            args.json,
        )
    if stats.deadline_reached:
        print("attribute: wall deadline reached; the sample is partial", file=sys.stderr)
        return 3
    return 0


def _review_record(args: argparse.Namespace) -> int:
    # The one place a clock is read, at the command boundary; the ledger helpers take the time.
    reviewed_at = args.reviewed_at or utc_stamp(datetime.now(UTC))
    rows: list[dict[str, Any]] = []
    skipped = 0
    for number, line in enumerate(args.answers.read_text(encoding="utf-8").split("\n"), start=1):
        if not line.strip():
            continue
        try:
            answer = json.loads(line)
        except ValueError as error:
            raise ValueError(f"answers line {number}: not JSON") from error
        if not isinstance(answer, dict):
            raise ValueError(f"answers line {number}: must be an object")
        verdict = answer.get("verdict")
        if verdict in (None, ""):
            skipped += 1
            continue
        verdict = str(verdict).strip().lower()
        if verdict not in VERDICTS:
            raise ValueError(f"answers line {number}: verdict must be one of {list(VERDICTS)}")
        try:
            rows.append(
                ledger_row(
                    str(answer.get("span_id")),
                    str(answer.get("rule")),
                    verdict,
                    args.reviewer,
                    reviewed_at,
                    stratum=str(answer.get("stratum")),
                    rules_version=str(answer.get("rules_version") or RULES_VERSION),
                )
            )
        except ValueError as error:
            raise ValueError(f"answers line {number}: {error}") from error
    appended = append_ledger(args.ledger, rows)
    summary = review_statistics(read_ledger(args.ledger))
    result = {
        "ledger": str(args.ledger),
        "appended": appended,
        "unanswered": skipped,
        "strata": {
            f"{rule}:{stratum}": {
                "reviewed": review.reviewed,
                "confirmed": review.confirmed,
                "eligible": review.eligible,
            }
            for (rule, stratum), review in summary.strata.items()
            if review.reviewed
        },
    }
    _emit(
        result, f"appended {appended} verdicts to {args.ledger} ({skipped} unanswered)", args.json
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
            f"attribute: {error} before the repeated-text census finished; nothing was written",
            file=sys.stderr,
        )
        return 3
    except (ValueError, OSError, KeyError, CorpusStoreError, sqlite3.Error) as error:
        print(f"attribute: {error}", file=sys.stderr)
        return 2
