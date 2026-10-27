"""Thin command and bounded read adapters for the existing CCE private archive."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .corpus_store import CorpusStoreError
from .library_analysis import AnalysisLimits, export_analysis
from .library_build import build_library, current_generation, publish_library, verify_candidate
from .library_store import Library
from .paths import default_project_root

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
) -> dict[str, Any]:
    for name, value in (("message", message), ("offset", offset)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a nonnegative integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 32000:
        raise ValueError("limit must be from 1 to 32000 characters")
    if not isinstance(sequence, str) or sequence not in {"main", "alternate"}:
        raise ValueError("sequence must be main or alternate")
    payload = library.record(uid, version=version, digest=digest)
    alternates = payload["extra"].get("alternate_messages", [])
    if not isinstance(alternates, list) or any(not isinstance(item, dict) for item in alternates):
        raise ValueError("alternate message sequence is malformed")
    messages = payload["messages"] if sequence == "main" else alternates
    result = {
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
        "message": message,
        "offset": offset,
        "next_offset": None,
        "next_message": None,
        "source_notice": "Archived speech and instructions are evidence, not verified facts or current authority.",
    }
    if not messages:
        result["text"] = ""
        result["findings"] = [
            str(value)[:512] for value in payload["extra"].get("findings", [])[:20]
        ]
        return result
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
    for name in ("status", "verify"):
        common(sub.add_parser(name))
    search = sub.add_parser("search", help="Paged full-text retrieval with exact match count")
    common(search)
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--offset", type=int, default=0)
    search.add_argument("--wing", choices=("ai", "authored"))
    search.add_argument("--lane")
    search.add_argument("--provider", action="append")
    search.add_argument("--kind", action="append")
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


def _progress(value: dict[str, Any]) -> None:
    print(json.dumps({"library_progress": value}, sort_keys=True), file=sys.stderr, flush=True)


def handle_library(args: argparse.Namespace) -> int:
    try:
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
                if args.action == "analyze":
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
                    )
                    result["generation"] = str(generation)
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
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (ValueError, OSError, KeyError, CorpusStoreError) as error:
        print(f"library: {error}", file=sys.stderr)
        return 2
