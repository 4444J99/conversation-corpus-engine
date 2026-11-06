"""Thin command and bounded read adapters for the existing CCE private archive."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from .archive_database import PHRASE_RE, WORD_RE
from .attributed_knowledge import ZETTEL_SCHEMA, export_zettels, zettel_atoms
from .corpus_store import CorpusStoreError
from .library_analysis import AnalysisLimits, export_analysis
from .library_build import build_library, current_generation, publish_library, verify_candidate
from .library_export import export_library, verify_export
from .library_store import (
    BROAD_QUERY_HINT,
    DEFAULT_SEARCH_SECONDS,
    MAX_SEARCH_SECONDS,
    SEARCH_PLANS,
    Library,
    validate_search_seconds,
)
from .paths import default_project_root
from .persona_extract import extract_persona_lexicon

STORE_ENV = "CCE_LIBRARY_STORE_ROOT"


def configured_store(explicit: Path | None = None) -> Path:
    value = explicit or os.environ.get(STORE_ENV) or os.environ.get("CCE_CORPUS_STORE_ROOT")
    if not value:
        raise ValueError(f"provide --store-root or {STORE_ENV}")
    path = Path(value).expanduser().absolute()
    if path.is_symlink():
        raise ValueError("library store must use its real registered path, not a symlink")
    return path


def open_library(store_root: Path) -> tuple[Path, Library]:
    generation = current_generation(store_root)
    if generation is None:
        raise ValueError("no library generation has been published")
    return generation, Library(generation / "library.sqlite")


PASSAGE_LIMIT = 1500
MAX_RANGE_MESSAGES = 200
MAX_TOC_ENTRIES = 500
PREVIEW_CHARS = 80
HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t#]*$")
STEM_SUFFIXES = ("ing", "ed", "es", "s", "ly")


def _query_patterns(query: str) -> list[re.Pattern[str]]:
    """Literal-ish patterns for the terms of an FTS query (crude suffix stemming, case-folded)."""

    def word(value: str) -> str:
        value = value.lower()
        for suffix in STEM_SUFFIXES:
            if value.endswith(suffix) and len(value) - len(suffix) >= 3:
                value = value[: -len(suffix)]
                break
        return r"(?<!\w)" + re.escape(value) + r"\w*"

    phrases = [p for p in PHRASE_RE.findall(query) if WORD_RE.search(p)]
    patterns = [
        re.compile(r"\W+".join(word(w) for w in WORD_RE.findall(phrase)), re.IGNORECASE)
        for phrase in phrases
    ]
    patterns.extend(
        re.compile(word(w), re.IGNORECASE) for w in WORD_RE.findall(PHRASE_RE.sub(" ", query))
    )
    return patterns


def locate_match(
    messages: list[dict[str, Any]], query: str, *, first_message: int = 0
) -> tuple[dict[str, int] | None, bool]:
    """First message holding every query term (else the first holding any), with its earliest
    term span. Returns (match, complete); match is None when no term occurs."""
    patterns = _query_patterns(query)
    partial = None
    for index in range(first_message, len(messages)):
        text = str(messages[index].get("text", ""))
        spans = sorted(m.span() for pattern in patterns if (m := pattern.search(text)))
        if not spans:
            continue
        found = {"message": index, "start": spans[0][0], "end": spans[0][1]}
        if len(spans) == len(patterns):
            return found, True
        partial = partial or found
    return partial, False


def passage_window(match: dict[str, int], text_length: int, limit: int) -> int:
    """Character offset that centres a `limit`-character window on the match."""
    centre = (match["start"] + match["end"]) // 2
    return max(0, min(centre - limit // 2, text_length - limit))


def _sequences(
    payload: dict[str, Any], uid: str | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Main and alternate message sequences; a single-record read names the record it refuses."""
    alternates = payload["extra"].get("alternate_messages", [])
    if not isinstance(alternates, list) or any(not isinstance(item, dict) for item in alternates):
        where = f" in record {uid}" if uid else ""
        raise ValueError(f"alternate message sequence is malformed{where}")
    return payload["messages"], alternates


def passage_handoff(
    payload: dict[str, Any], query: str, limit: int = PASSAGE_LIMIT
) -> dict[str, Any]:
    """Exact read arguments for the passage of a record that best answers a search query."""
    main, alternates = _sequences(payload)
    best = None
    for sequence, messages in (("main", main), ("alternate", alternates)):
        match, complete = locate_match(messages, query)
        if match is None:
            continue
        if complete:
            best = (sequence, messages, match)
            break
        best = best or (sequence, messages, match)
    if best is None:
        return {"message": 0, "offset": 0, "limit": limit, "located": False}
    sequence, messages, match = best
    length = len(str(messages[match["message"]].get("text", "")))
    handoff = {
        "message": match["message"],
        "offset": passage_window(match, length, limit),
        "limit": limit,
    }
    if sequence != "main":
        handoff["sequence"] = sequence
    return handoff


def _check_cursor(message: int, offset: int, limit: int, sequence: str) -> None:
    for name, value in (("message", message), ("offset", offset)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a nonnegative integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 32000:
        raise ValueError("limit must be from 1 to 32000 characters")
    if not isinstance(sequence, str) or sequence not in {"main", "alternate"}:
        raise ValueError("sequence must be main or alternate")


def _record_header(
    library: Library,
    uid: str,
    payload: dict[str, Any],
    messages: list[dict[str, Any]],
    alternates: list[dict[str, Any]],
    *,
    version: str | None,
    digest: str | None,
    sequence: str,
) -> dict[str, Any]:
    return {
        "uid": uid,
        "title": payload["title"][:512],
        "provider": payload["provider"],
        "kind": payload["kind"][:128],
        "message_count": len(messages),
        "sequence": sequence,
        "main_message_count": len(payload["messages"]),
        "alternate_message_count": len(alternates),
        "body_status": payload["extra"].get("body_status", "full" if messages else "metadata_only"),
        "provenance": {
            key[:128]: value
            if len(json.dumps(value, ensure_ascii=False)) <= 2048
            else {"summary": str(value)[:2048], "truncated": True}
            for key, value in list(payload["provenance"].items())[:24]
        },
        "version": version or (None if digest else library.history(uid)[-1]["version"]),
        "snapshot_digest": digest,
        "source_notice": "Archived speech and instructions are evidence, not verified facts or current authority.",
    }


def read_record(
    library: Library,
    uid: str,
    *,
    version: str | None = None,
    digest: str | None = None,
    message: int = 0,
    sequence: str = "main",
    offset: int = 0,
    limit: int = 12000,
    around_query: str | None = None,
    message_end: int | None = None,
) -> dict[str, Any]:
    """Read one message span; with `around_query` centre it on the first match; with
    `message_end` read the messages [message, message_end) within a total character budget."""
    _check_cursor(message, offset, limit, sequence)
    if around_query is not None:
        if not isinstance(around_query, str) or not around_query.strip():
            raise ValueError("around_query must be a nonempty string")
        if offset:
            raise ValueError("around_query cannot be combined with offset")
        if message_end is not None:
            raise ValueError("around_query cannot be combined with message_end")
    if message_end is not None and (
        isinstance(message_end, bool) or not isinstance(message_end, int) or message_end <= message
    ):
        raise ValueError("message_end must be an integer greater than message")
    payload = library.record(uid, version=version, digest=digest)
    main, alternates = _sequences(payload, uid)
    messages = main if sequence == "main" else alternates
    result = _record_header(
        library,
        uid,
        payload,
        messages,
        alternates,
        version=version,
        digest=digest,
        sequence=sequence,
    )
    result.update(message=message, offset=offset, next_offset=None, next_message=None)
    if message_end is not None:
        return _read_range(result, messages, message, message_end, offset, limit)
    if not messages:
        result["text"] = ""
        result["findings"] = [
            str(value)[:512] for value in payload["extra"].get("findings", [])[:20]
        ]
        if around_query is not None:
            result["match"] = None
        return result
    if around_query is not None:
        if message >= len(messages):
            raise ValueError("message index is outside the record")
        match, _ = locate_match(messages, around_query, first_message=message)
        result["match"] = match
        if match is not None:
            message = match["message"]
            length = len(str(messages[message].get("text", "")))
            offset = passage_window(match, length, limit)
            result.update(message=message, offset=offset)
    if message >= len(messages):
        raise ValueError("message index is outside the record")
    item = messages[message]
    text = str(item.get("text", ""))
    if offset > len(text):
        raise ValueError("text offset is outside the message")
    end = min(len(text), offset + limit)
    result.update(
        text=text[offset:end],
        role=str(item.get("role", "unknown"))[:128],
        channel=item.get("channel", "")[:128],
        message_id=item.get("id", "")[:512],
        total_characters=len(text),
    )
    if end < len(text):
        result["next_offset"] = end
    elif message + 1 < len(messages):
        result["next_message"] = message + 1
    return result


def _read_range(
    result: dict[str, Any],
    messages: list[dict[str, Any]],
    message: int,
    message_end: int,
    offset: int,
    limit: int,
) -> dict[str, Any]:
    result.update(messages=[], total_exact=True)
    if not messages:
        result["message_end"] = message
        return result
    if message >= len(messages):
        raise ValueError("message index is outside the record")
    # One call spans at most MAX_RANGE_MESSAGES messages; a capped range hands back a cursor.
    end = min(message_end, message + MAX_RANGE_MESSAGES, len(messages))
    capped = end < min(message_end, len(messages))
    budget, index, start = limit, message, offset
    while index < end:
        if budget <= 0:
            result.update(next_message=index, next_offset=0)
            break
        item = messages[index]
        text = str(item.get("text", ""))
        if start > len(text):
            raise ValueError("text offset is outside the message")
        stop = min(len(text), start + budget)
        result["messages"].append(
            {
                "message": index,
                "role": str(item.get("role", "unknown"))[:128],
                "channel": item.get("channel", "")[:128],
                "message_id": item.get("id", "")[:512],
                "offset": start,
                "text": text[start:stop],
                "total_characters": len(text),
            }
        )
        budget -= stop - start
        if stop < len(text):
            result.update(next_message=index, next_offset=stop)
            break
        index, start = index + 1, 0
    else:
        if capped:
            result.update(next_message=end, next_offset=0)
    result["message_end"] = end
    return result


def _headings(text: str) -> list[tuple[int, int, str]]:
    found, position, fenced = [], 0, False
    for line in text.splitlines(keepends=True):
        if line.strip().startswith(("```", "~~~")):
            fenced = not fenced
        elif not fenced and (heading := HEADING_RE.match(line.rstrip("\r\n"))):
            found.append((position, len(heading.group(1)), heading.group(2)))
        position += len(line)
    return found


def record_toc(
    library: Library,
    uid: str,
    *,
    version: str | None = None,
    digest: str | None = None,
    sequence: str = "main",
    mode: str = "auto",
    toc_offset: int = 0,
    toc_limit: int = 50,
) -> dict[str, Any]:
    """Table of contents of one record: markdown headings (with message and character offsets)
    or, without headings, a per-message index. Entries are paged and the total is exact."""
    _check_cursor(0, 0, 1, sequence)
    if mode not in {"auto", "headings", "messages"}:
        raise ValueError("toc_mode must be auto, headings or messages")
    if isinstance(toc_offset, bool) or not isinstance(toc_offset, int) or toc_offset < 0:
        raise ValueError("toc_offset must be a nonnegative integer")
    if (
        isinstance(toc_limit, bool)
        or not isinstance(toc_limit, int)
        or not 1 <= toc_limit <= MAX_TOC_ENTRIES
    ):
        raise ValueError(f"toc_limit must be an integer from 1 to {MAX_TOC_ENTRIES}")
    payload = library.record(uid, version=version, digest=digest)
    main, alternates = _sequences(payload, uid)
    messages = main if sequence == "main" else alternates
    result = _record_header(
        library,
        uid,
        payload,
        messages,
        alternates,
        version=version,
        digest=digest,
        sequence=sequence,
    )
    headings = [
        {"message": index, "offset": position, "level": level, "heading": title[:100]}
        for index, item in enumerate(messages)
        for position, level, title in _headings(str(item.get("text", "")))
    ]
    kind = mode if mode != "auto" else ("headings" if headings else "messages")
    if kind == "headings":
        entries = headings
    else:
        entries = []
        for index, item in enumerate(messages):
            text = str(item.get("text", ""))
            entries.append(
                {
                    "message": index,
                    "role": str(item.get("role", "unknown"))[:128],
                    "chars": len(text),
                    "preview": " ".join(text[: PREVIEW_CHARS * 4].split())[:PREVIEW_CHARS],
                }
            )
    page = entries[toc_offset : toc_offset + toc_limit]
    result.update(
        toc_kind=kind,
        toc=page,
        toc_total=len(entries),
        toc_exact=True,
        toc_offset=toc_offset,
        toc_next_offset=toc_offset + len(page) if toc_offset + len(page) < len(entries) else None,
    )
    return result


def add_library_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "library", help="One versioned knowledge library across chats, code, plans and writing"
    )
    sub = parser.add_subparsers(dest="action", required=True)

    def common(command: argparse.ArgumentParser) -> None:
        command.add_argument("--store-root", "--corpus-store-root", dest="store_root", type=Path)
        command.add_argument("--project-root", type=Path, default=default_project_root())
        command.add_argument("--json", action="store_true", help="JSON is the default output")

    build = sub.add_parser(
        "build", help="Build a private candidate from an explicit source manifest"
    )
    common(build)
    build.add_argument("--manifest", type=Path, required=True)
    build.add_argument("--publish", action="store_true", help="Publish only after verification")
    publish = sub.add_parser("publish", help="Verify and atomically publish an existing candidate")
    common(publish)
    publish.add_argument("generation", type=Path)
    zettels = sub.add_parser(
        "zettels", help="Validate and export reviewed semantic ideas with exact source spans"
    )
    common(zettels)
    zettels.add_argument(
        "--input", type=Path, required=True, help="Private reviewed-Zettels JSON envelope"
    )
    zettels.add_argument(
        "--format",
        choices=("json", "atoms-jsonl"),
        default="json",
        help="Reviewed envelope or explicit sealed-archive knowledge atoms",
    )
    export = sub.add_parser(
        "export", help="Write the private reading export, optionally with reviewed Zettel pages"
    )
    common(export)
    export.add_argument("--out", type=Path, required=True, help="New export directory")
    export.add_argument(
        "--zettels", type=Path, help="Private reviewed-Zettels JSON envelope to render as pages"
    )
    verify_export = sub.add_parser(
        "verify-export", help="Verify an export directory, including its Zettel pages and anchors"
    )
    common(verify_export)
    verify_export.add_argument("directory", type=Path)
    verify_export.add_argument(
        "--zettels", type=Path, help="Reviewed-Zettels envelope whose count the pages must match"
    )
    profile = sub.add_parser(
        "profile", help="Strict owner profile from explicit canonical message annotations"
    )
    common(profile)
    profile.add_argument("--persona", required=True, help="Exact owner author_id")
    profile.add_argument(
        "--annotations", type=Path, required=True, help="Private JSON list of source annotations"
    )
    for name in ("status", "verify"):
        common(sub.add_parser(name))
    search = sub.add_parser(
        "search",
        help="Paged full-text retrieval; the match count is exact up to 1000",
        description=(
            "Paged full-text retrieval. Several unquoted words are planned: an adjacent phrase "
            "first, then every word (AND), then any word (OR), stopping at the first plan that "
            'matches; the response names it in `plan`. A single word or a "quoted phrase" is '
            "searched as written. OR on a large library is slow, so quote a phrase or add "
            "terms for a faster, more precise search. The match count is exact up to 1000 and "
            "then reads 1000+. The call is bounded by --max-seconds: when it runs out, ranking "
            "is abandoned and a partial page is returned in index order (`partial` true, "
            "`ranked` false)."
        ),
    )
    common(search)
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--offset", type=int, default=0)
    search.add_argument("--wing", choices=("ai", "authored"))
    search.add_argument("--lane")
    search.add_argument("--provider", action="append")
    search.add_argument("--kind", action="append")
    search.add_argument(
        "--plan",
        choices=SEARCH_PLANS,
        default="auto",
        help="auto (default): phrase, then and, then or, first match wins; or force one plan "
        "(or is slow on a large library)",
    )
    search.add_argument(
        "--max-seconds",
        type=_search_seconds,
        default=DEFAULT_SEARCH_SECONDS,
        help=f"time budget in seconds, above 0 and at most {MAX_SEARCH_SECONDS:g} (default "
        f"{DEFAULT_SEARCH_SECONDS:g}); on expiry a partial page in index order is returned, "
        "not an error",
    )
    read = sub.add_parser("read", help="Read one bounded message span from a source revision")
    common(read)
    read.add_argument("uid")
    read.add_argument("--version")
    read.add_argument("--digest", help="Read any retained source snapshot by its content digest")
    read.add_argument("--message", type=int, default=0)
    read.add_argument("--sequence", choices=("main", "alternate"), default="main")
    read.add_argument("--offset", type=int, default=0)
    read.add_argument("--limit", type=int, default=12000)
    history = sub.add_parser("history", help="Read accepted semantic revisions")
    common(history)
    history.add_argument("uid")
    history.add_argument("--offset", type=int, default=0)
    history.add_argument("--limit", type=int, default=50)
    analyze = sub.add_parser(
        "analyze", help="Export bounded original-source inputs for LingFrame and UAKS"
    )
    common(analyze)
    selection = analyze.add_mutually_exclusive_group(required=True)
    selection.add_argument("query", nargs="?")
    selection.add_argument("--uid", action="append", default=[])
    analyze.add_argument("--output", type=Path, required=True)
    analyze.add_argument("--target", choices=("both", "lingframe", "uaks"), default="both")
    analyze.add_argument("--limit", type=int, default=5)
    defaults = AnalysisLimits()
    analyze.add_argument("--max-source-bytes", type=int, default=defaults.max_source_bytes)
    analyze.add_argument("--max-output-bytes", type=int, default=defaults.max_output_bytes)
    analyze.add_argument("--max-atoms", type=int, default=defaults.max_atoms)
    analyze.add_argument("--max-seconds", type=int, default=defaults.max_seconds)


def _search_seconds(text: str) -> float:
    try:
        return validate_search_seconds(float(text))
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def _progress(value: dict[str, Any]) -> None:
    print(json.dumps({"library_progress": value}, sort_keys=True), file=sys.stderr, flush=True)


def _load_zettel_envelope(path: Path) -> dict[str, Any]:
    envelope = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(envelope, dict) or envelope.get("schema") != ZETTEL_SCHEMA:
        raise ValueError("unsupported reviewed Zettel envelope schema")
    return envelope


def _verify_export_command(args: argparse.Namespace) -> int:
    reviewed = None
    if args.zettels is not None:
        entries = _load_zettel_envelope(args.zettels).get("zettels")
        if not isinstance(entries, list):
            raise ValueError("zettels must be a list")
        reviewed = sum(
            isinstance(entry, dict)
            and isinstance(entry.get("review"), dict)
            and entry["review"].get("status") == "reviewed"
            for entry in entries
        )
    problems = verify_export(args.directory, expected_zettels=reviewed)
    pages = args.directory / "vault" / "zettels"
    report = {
        "directory": str(args.directory),
        "exported": len(list(pages.glob("*.md"))) if pages.is_dir() else 0,
        "reviewed": reviewed,
        "unresolved_anchors": sum(p.startswith("unresolved source anchor") for p in problems),
        "valid": not problems,
        "problems": problems,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if problems else 0


def handle_library(args: argparse.Namespace) -> int:
    try:
        if args.action == "verify-export":
            return _verify_export_command(args)
        store = configured_store(args.store_root)
        if args.action == "build":
            manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            report = build_library(
                manifest, project_root=args.project_root, store_root=store, progress=_progress
            )
            result = {
                key: report[key]
                for key in (
                    "generation",
                    "status",
                    "record_fingerprint",
                    "coverage_complete",
                    "artifact_bytes",
                    "elapsed_seconds",
                )
            }
            result["findings_count"] = len(report["findings"])
            result["report"] = str(Path(report["generation"]) / "report.json")
            if args.publish:
                result["publication"] = publish_library(
                    Path(report["generation"]), project_root=args.project_root, store_root=store
                )
        elif args.action == "publish":
            result = publish_library(
                args.generation, project_root=args.project_root, store_root=store
            )
        else:
            generation, library = open_library(store)
            with library:
                if args.action == "export":
                    envelope = _load_zettel_envelope(args.zettels) if args.zettels else None
                    result = export_library(library, args.out, zettels=envelope)
                elif args.action == "zettels":
                    envelope = _load_zettel_envelope(args.input)
                    exporter = zettel_atoms if args.format == "atoms-jsonl" else export_zettels
                    result = exporter(library, envelope.get("zettels"))
                elif args.action == "profile":
                    annotations = json.loads(args.annotations.read_text(encoding="utf-8"))
                    result = extract_persona_lexicon(
                        args.project_root,
                        args.persona,
                        library=library,
                        annotations=annotations,
                    )
                elif args.action == "analyze":
                    result = export_analysis(
                        library,
                        args.output,
                        project_root=args.project_root,
                        store_root=store,
                        query=args.query,
                        uids=args.uid,
                        limit=args.limit,
                        target=args.target,
                        limits=AnalysisLimits(
                            max_source_bytes=args.max_source_bytes,
                            max_output_bytes=args.max_output_bytes,
                            max_atoms=args.max_atoms,
                            max_seconds=args.max_seconds,
                        ),
                    )
                elif args.action == "search":
                    result = library.search(
                        args.query,
                        limit=args.limit,
                        offset=args.offset,
                        wing=args.wing,
                        lane=args.lane,
                        providers=args.provider,
                        kinds=args.kind,
                        plan=args.plan,
                        max_seconds=args.max_seconds,
                    )
                    result["generation"] = str(generation)
                    for line in (result.get("notice"), result.get("hint")):
                        if line:
                            prefix = "hint: " if line == BROAD_QUERY_HINT else ""
                            print(f"library: {prefix}{line}", file=sys.stderr)
                elif args.action == "read":
                    result = read_record(
                        library,
                        args.uid,
                        version=args.version,
                        digest=args.digest,
                        message=args.message,
                        sequence=args.sequence,
                        offset=args.offset,
                        limit=args.limit,
                    )
                elif args.action == "history":
                    if args.offset < 0 or not 1 <= args.limit <= 200:
                        raise ValueError("history needs nonnegative offset and limit from 1 to 200")
                    rows = library.history(args.uid)
                    snapshots = library.snapshots(args.uid)
                    result = {
                        "uid": args.uid,
                        "total": len(rows),
                        "offset": args.offset,
                        "revisions": rows[args.offset : args.offset + args.limit],
                        "snapshot_total": len(snapshots),
                        "snapshots": snapshots[args.offset : args.offset + args.limit],
                    }
                elif args.action == "verify":
                    verify_candidate(generation)
                    problems = library.verify()
                    result = {
                        "generation": str(generation),
                        "valid": not problems,
                        "problems": problems,
                    }
                    if problems:
                        print(json.dumps(result, indent=2))
                        return 1
                else:
                    result = {"generation": str(generation), **library.status()}
        if args.action == "zettels" and args.format == "atoms-jsonl":
            for atom in result:
                print(json.dumps(atom, ensure_ascii=False, sort_keys=True))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (ValueError, OSError, KeyError, CorpusStoreError) as error:
        print(f"library: {error}", file=sys.stderr)
        return 2
