from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import archive_layout as layout
from . import archive_read_library, archive_search
from .archive_author import AUTHORS, reconcile
from .archive_corpus import CorpusProjectionError, project_corpus
from .archive_feed import FeedError, project_feed
from .archive_fixture import write_fixture
from .archive_graph import build_graph
from .archive_ingest import (
    DEFAULT_ARCHIVE_DIRNAME,
    PROVIDER_SPECS,
    ArchivePaths,
    authorize_archive,
    failed_providers,
    has_bounded_layout,
    ingest,
    load_manifest,
)
from .archive_limits import Limits, census_problems, take_census
from .archive_naming import names_payload, render_names_text, verify_names
from .archive_serve import serve
from .archive_site import build_site, count_conversation_pages
from .archive_topics import build_analysis
from .archive_turns import list_turns
from .archive_vault import build_vault, count_conversation_notes, verify_vault
from .corpus_store import (
    CorpusStoreError,
    CorpusWriteAuthorization,
    resolve_configured_corpus_store_root,
)
from .paths import default_project_root

STAGES = ("ingest", "analyze", "vault", "database", "graph", "site")


@dataclass(frozen=True)
class ArchiveContext:
    paths: ArchivePaths
    authorization: CorpusWriteAuthorization
    export_root: Path | None
    library_root: Path | None = None


def library_source_folder(library_root: Path) -> Path:
    """The folder custody must treat as the library source: the narrowest one that holds it.

    A library is often built in the same store the archive is written to (`<store>/library-all`
    beside `<store>/archive-all`); handing custody the store itself would make the source an
    ancestor of the destination. So the source is the `library-all` folder (or the generation),
    not the store that contains it.
    """
    try:
        database, pointer = archive_read_library.locate(library_root)
    except (OSError, ValueError, KeyError):
        return library_root
    return pointer.parent if pointer is not None else database.parent


def open_context(
    *,
    project_root: Path,
    corpus_store_root: Path | None,
    archive_root: Path | None,
    export_root: Path | None = None,
    library_root: Path | None = None,
    source_overrides: tuple[Path, ...] = (),
) -> ArchiveContext:
    store = resolve_configured_corpus_store_root(corpus_store_root)
    sources = [
        *([export_root] if export_root else []),
        *([library_source_folder(library_root)] if library_root else []),
    ]
    authorization = authorize_archive(
        project_root=project_root,
        archive_root=archive_root or store / DEFAULT_ARCHIVE_DIRNAME,
        corpus_store_root=store,
        source_roots=tuple(path.resolve() for path in [*sources, *source_overrides]),
    )
    return ArchiveContext(
        ArchivePaths(authorization.destination), authorization, export_root, library_root
    )


def _say(message: str) -> None:
    print(f"[archive] {message}", file=sys.stderr, flush=True)


def oversize_row_files(manifest: dict[str, Any]) -> frozenset[str]:
    """The normalized shards that are one conversation larger than a shard, by relative path.

    The manifest records each one at ingest (`oversize_row`); the census exempts exactly these
    files from the data ceiling and lists them with the reason.
    """
    return frozenset(
        f"{layout.provider_dirname(name)}/{layout.shard_filename(name, shard['subject'])}"
        for name, info in manifest.get("providers", {}).items()
        for shard in info.get("layout", {}).get("shards", [])
        if shard.get("oversize_row")
    )


def reconcile_source(name: str, info: dict[str, Any]) -> list[str]:
    """Hold a provider's counts to its source's own account of itself.

    A library family must have read every record and message the library lists for it, and
    `conversations + folded duplicates` must equal that record count (a ChatGPT page the export
    already held is folded, never lost). Local memory must account for every file as a record,
    a folded conversation payload or a deliberate exclusion.
    """
    counters = info.get("reader_counters", {})
    problems: list[str] = []
    if counters.get("library_unclaimed"):
        problems.append(
            f"{counters['library_unclaimed']} library record(s) belong to no family and were "
            "not read"
        )
    if "library_records" in counters:
        for read, listed in (
            ("bridge_records", "library_records"),
            ("bridge_messages", "library_messages"),
        ):
            if counters.get(read, 0) != counters[listed]:
                problems.append(
                    f"read {counters.get(read, 0)} {read.split('_')[1]} of the "
                    f"{counters[listed]} the library lists"
                )
        if name != "chatgpt":
            held = info["conversations"] + info.get("duplicates_folded", 0)
            if held != counters["library_records"]:
                problems.append(
                    f"{info['conversations']} conversation(s) + {info.get('duplicates_folded', 0)} "
                    f"folded != {counters['library_records']} library record(s)"
                )
    if counters.get("folded_ids_missing"):
        problems.append(
            f"{counters['folded_ids_missing']} folded conversation id(s) are not conversations "
            "of the owning provider"
        )
    if counters.get("folded_ids_unowned"):
        problems.append(
            f"{counters['folded_ids_unowned']} folded conversation id(s) sit in a capture folder "
            "no provider owns"
        )
    if "files_seen" in counters:
        accounted = (
            info["conversations"]
            + counters.get("files_folded", 0)
            + counters.get("files_excluded", 0)
        )
        if accounted != counters["files_seen"]:
            problems.append(
                f"{counters['files_seen']} file(s) seen, {accounted} accounted for as records, "
                "folded payloads or exclusions"
            )
    return problems


def verify_layout(paths: ArchivePaths, manifest: dict[str, Any]) -> list[str]:
    """Check the bounded layout's own promises: shards, author counts, notes, pages and limits."""
    problems: list[str] = []
    for name, info in manifest["providers"].items():
        if info.get("status") != "ok":
            continue
        problems += [
            f"{name}: {item}" for item in reconcile(info.get("authors", {}), info["messages"])
        ]
        problems += [f"{name}: {item}" for item in reconcile_source(name, info)]
        shards = info.get("layout", {}).get("shards", [])
        if sum(shard["conversations"] for shard in shards) != info["conversations"]:
            problems.append(f"{name}: shard conversation counts do not sum to the manifest")
        on_disk = {shard_path.name: shard_path for shard_path in paths.shard_files(name)}
        if len(on_disk) != len(shards):
            problems.append(
                f"{name}: {len(on_disk)} shard file(s) on disk, manifest lists {len(shards)}"
            )
        for shard in shards:
            path = paths.provider_dir(name) / layout.shard_filename(name, shard["subject"])
            if not path.is_file():
                problems.append(f"{name}: shard {shard['subject']} is missing")
            elif path.stat().st_size != shard["bytes"]:
                problems.append(
                    f"{name}: shard {shard['subject']} differs in size from the manifest"
                )
    totals = manifest["totals"]
    problems += [
        f"totals: {item}" for item in reconcile(totals.get("authors", {}), totals["messages"])
    ]
    return problems


def verify_archive(paths: ArchivePaths, limits: Limits | None = None) -> list[str]:
    """Cross-check every surface against the ingest manifest; empty list means consistent."""
    try:
        manifest = load_manifest(paths)
    except FileNotFoundError as exc:
        return [str(exc)]
    problems = [
        f"provider {name} failed: {manifest['providers'][name].get('error', '')}"
        for name in failed_providers(manifest)
    ]
    total = manifest["totals"]["conversations"]
    enrichment = paths.enrichment
    if not enrichment.is_file():
        problems.append("analysis missing: run `cce archive analyze`")
    elif sum(1 for _ in enrichment.open(encoding="utf-8")) != total:
        problems.append("analysis row count differs from the manifest")
    problems += verify_vault(paths)
    notes = count_conversation_notes(paths)
    if notes != total:
        problems.append(f"vault has {notes} conversation note(s), manifest has {total}")
    try:
        problems += archive_search.verify_search_store(paths)
    except FileNotFoundError as exc:
        problems.append(str(exc))
    graph_summary = paths.graph_summary
    if not graph_summary.is_file():
        problems.append("graph missing: run `cce archive graph`")
    elif (
        json.loads(graph_summary.read_text(encoding="utf-8"))["node_kinds"].get("conversation")
        != total
    ):
        problems.append("graph conversation node count differs from the manifest")
    pages = count_conversation_pages(paths)
    if pages != total:
        problems.append(f"site has {pages} conversation page(s), manifest has {total}")
    if has_bounded_layout(manifest):
        problems += verify_layout(paths, manifest)
        problems += census_problems(
            take_census(paths.root, limits, exempt=oversize_row_files(manifest))
        )
    return problems


def limits_from_args(args: argparse.Namespace) -> Limits:
    """The limits a command was asked for; anything left out keeps its default."""
    return Limits.scaled(
        note_bytes=getattr(args, "note_bytes", None),
        folder_entries=getattr(args, "folder_entries", None),
        shard_bytes=getattr(args, "shard_bytes", None),
    )


def run_stages(
    context: ArchiveContext,
    stages: tuple[str, ...],
    *,
    providers: list[str] | None = None,
    overrides: dict[str, Path] | None = None,
    options: dict[str, Any] | None = None,
    limits: Limits | None = None,
    merge: bool = False,
) -> dict[str, Any]:
    paths, authorization = context.paths, context.authorization
    limits = limits or Limits()
    options = {"shard_bytes": limits.shard_bytes, "folder_entries": limits.folder_entries, **(options or {})}
    result: dict[str, Any] = {}
    runners: dict[str, Callable[[], dict[str, Any]]] = {
        "analyze": lambda: build_analysis(paths),
        "vault": lambda: build_vault(paths, authorization, limits=limits),
        "database": lambda: archive_search.build_search_store(paths, authorization),
        "graph": lambda: build_graph(paths, authorization),
        "site": lambda: build_site(paths, authorization, limits=limits),
    }
    for stage in stages:
        _say(f"{stage} …")
        if stage == "ingest":
            if context.export_root is None:
                raise ValueError("ingest needs --export-root")
            manifest = ingest(
                export_root=context.export_root,
                paths=paths,
                authorization=authorization,
                providers=providers,
                overrides=overrides,
                options=options,
                library_root=context.library_root,
                merge=merge,
            )
            result[stage] = {
                "totals": manifest["totals"],
                "providers": {
                    name: {k: info.get(k) for k in ("status", "conversations", "messages", "error")}
                    for name, info in manifest["providers"].items()
                },
            }
        else:
            result[stage] = runners[stage]()
        _say(f"{stage} done: {json.dumps(result[stage], sort_keys=True)[:200]}")
    return result


def add_archive_parser(subparsers: Any) -> None:
    archive = subparsers.add_parser(
        "archive", help="Browsable archive surfaces: vault, wiki, search database, site, graph"
    )
    sub = archive.add_subparsers(dest="action", required=True)

    def common(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--project-root", type=Path, default=default_project_root())
        parser.add_argument("--corpus-store-root", type=Path)
        parser.add_argument("--archive-root", type=Path)
        parser.add_argument("--json", action="store_true")

    def limit_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--note-bytes",
            type=int,
            help="cut a note or page into parts at this many bytes (default 102400)",
        )
        parser.add_argument(
            "--folder-entries",
            type=int,
            help="spill a folder into sub-folders past this many entries (default 1800)",
        )
        parser.add_argument(
            "--shard-bytes",
            type=int,
            help="roll a provider-month shard to a numbered sibling at this size (default 48 MiB)",
        )

    def export_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--export-root", type=Path, required=True)
        parser.add_argument("--provider", action="append", choices=sorted(PROVIDER_SPECS))
        parser.add_argument(
            "--source",
            action="append",
            default=[],
            metavar="PROVIDER=PATH",
            help="override a provider's source folder",
        )
        parser.add_argument(
            "--chatgpt-extra",
            action="append",
            type=Path,
            default=[],
            help="extra ChatGPT conversation JSON (live delta); wins over export shards",
        )
        parser.add_argument("--include-attachment-text", action="store_true")
        parser.add_argument(
            "--library-root",
            type=Path,
            help="a knowledge-library store, `library-all` folder or generation: the source of "
            "the coding-CLI, notes, Drive, document, agent memory and plan families",
        )
        parser.add_argument(
            "--merge",
            action="store_true",
            help="keep the manifest entries of providers this call does not read, so a store "
            "is built one provider at a time",
        )

    ingest_parser = sub.add_parser("ingest", help="Read raw exports into the normalized store")
    common(ingest_parser)
    export_args(ingest_parser)
    limit_args(ingest_parser)
    for name, text in (
        ("analyze", "Topic hubs and similarity"),
        ("vault", "Obsidian vault (the wiki)"),
        ("database", "Turn store: SQLite shards by author, provider and month"),
        ("graph", "Graph as JSONL + GraphML"),
        ("site", "Static archive site"),
        ("verify", "Cross-check every surface against the manifest"),
    ):
        stage_parser = sub.add_parser(name, help=text)
        common(stage_parser)
        if name in {"vault", "site", "verify"}:
            limit_args(stage_parser)
    build = sub.add_parser(
        "build", help="ingest + analyze + vault + database + graph + site + verify"
    )
    common(build)
    export_args(build)
    limit_args(build)

    search_parser = sub.add_parser("search", help="Full-text search the search database")
    common(search_parser)
    search_parser.add_argument("query")
    search_parser.add_argument("--provider", action="append", choices=sorted(PROVIDER_SPECS))
    search_parser.add_argument("--kind", action="append")
    search_parser.add_argument(
        "--author",
        action="append",
        choices=list(AUTHORS),
        help="search only turns by this author (human = typed by the person)",
    )
    search_parser.add_argument(
        "--month", action="append", help="search only this month, yyyymm or undated"
    )
    search_parser.add_argument("--limit", type=int, default=15)
    search_parser.add_argument("--titles", action="store_true", help="search titles only")

    turns_parser = sub.add_parser(
        "turns", help="List the turns of one slice of the turn store, in reading order"
    )
    common(turns_parser)
    turns_parser.add_argument("--author", choices=list(AUTHORS))
    turns_parser.add_argument("--provider", choices=sorted(PROVIDER_SPECS))
    turns_parser.add_argument("--month", help="yyyymm or undated")
    turns_parser.add_argument("--uid", help="only the turns of this conversation")
    turns_parser.add_argument("--limit", type=int, default=20)
    turns_parser.add_argument("--offset", type=int, default=0)

    census_parser = sub.add_parser(
        "census", help="Check a tree against the size and folder limits (exit 1 on a violation)"
    )
    census_parser.add_argument("--root", type=Path, required=True)
    census_parser.add_argument(
        "--deadline", type=float, help="stop the walk after this many seconds"
    )
    census_parser.add_argument("--json", action="store_true")
    limit_args(census_parser)

    serve_parser = sub.add_parser("serve", help="Serve the site with full-text search on loopback")
    common(serve_parser)
    serve_parser.add_argument("--port", type=int, default=8765)

    names_parser = sub.add_parser("names", help="Print the naming contract: kinds, scopes, version")
    names_parser.add_argument("--json", action="store_true")

    verify_names_parser = sub.add_parser(
        "verify-names",
        help="Check every file and directory name under an archive root against the contract",
    )
    verify_names_parser.add_argument("--root", type=Path, required=True)

    fixture_parser = sub.add_parser(
        "fixture",
        help="Write deterministic synthetic exports (two providers sharing one ask) for tests",
    )
    fixture_parser.add_argument("--out", type=Path, required=True)
    fixture_parser.add_argument(
        "--stress",
        nargs="?",
        type=int,
        const=120,
        default=0,
        metavar="N",
        help="also write one very long thread and N short conversations in one month (default 120)",
    )

    project = sub.add_parser("project", help="Project the normalized archive into another shape")
    project_sub = project.add_subparsers(dest="target", required=True)
    feed_parser = project_sub.add_parser(
        "feed", help="Write each provider's prompt feed and its receipt"
    )
    feed_parser.add_argument("--root", type=Path, required=True, help="the archive folder")
    feed_parser.add_argument("--out", type=Path, required=True, help="a Git-invisible folder")
    feed_parser.add_argument("--provider", action="append", choices=sorted(PROVIDER_SPECS))
    corpus_parser = project_sub.add_parser(
        "corpus", help="Write each provider's corpus folder for the federation"
    )
    corpus_parser.add_argument("--root", type=Path, required=True, help="the archive folder")
    corpus_parser.add_argument("--out", type=Path, required=True, help="a Git-invisible folder")
    corpus_parser.add_argument("--provider", action="append", choices=sorted(PROVIDER_SPECS))


def _overrides(raw: list[str]) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for item in raw:
        name, sep, value = item.partition("=")
        if not sep or name not in PROVIDER_SPECS or not value:
            raise ValueError(f"--source must be PROVIDER=PATH with a known provider, got: {item}")
        out[name] = Path(value)
    return out


def _handle_project(args: argparse.Namespace) -> int:
    if args.target not in {"feed", "corpus"}:
        raise ValueError(f"unsupported projection: {args.target}")
    if not args.root.is_dir():
        print(f"archive root not found: {args.root}", file=sys.stderr)
        return 2
    project = project_feed if args.target == "feed" else project_corpus
    try:
        summary = project(args.root, args.out, args.provider)
    except CorpusStoreError as exc:
        print(f"{args.target} refused: {exc}", file=sys.stderr)
        return 2
    except (FeedError, CorpusProjectionError) as exc:
        print(f"{args.target} not written: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def handle_archive(args: argparse.Namespace) -> int:
    action = args.action

    # Naming commands read no store and need no custody, so they run before it is opened.
    if action == "names":
        if args.json:
            print(json.dumps(names_payload(), indent=2, sort_keys=True))
        else:
            print(render_names_text())
        return 0
    if action == "verify-names":
        try:
            report = verify_names(args.root)
        except (NotADirectoryError, FileNotFoundError):
            print(f"archive root not found: {args.root}", file=sys.stderr)
            return 2
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["ok"] else 1
    if action == "fixture":
        print(json.dumps(write_fixture(args.out, stress=args.stress), indent=2, sort_keys=True))
        return 0
    if action == "census":
        exempt: frozenset[str] = frozenset()
        with contextlib.suppress(FileNotFoundError, ValueError):
            exempt = oversize_row_files(load_manifest(ArchivePaths(args.root)))
        try:
            report = take_census(
                args.root, limits_from_args(args), deadline_seconds=args.deadline, exempt=exempt
            )
        except NotADirectoryError:
            print(f"census root not found: {args.root}", file=sys.stderr)
            return 2
        report["problems"] = census_problems(report)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["ok"] else 1
    if action == "project":
        return _handle_project(args)

    export_root = getattr(args, "export_root", None)
    library_root = getattr(args, "library_root", None)
    try:
        context = open_context(
            project_root=args.project_root,
            corpus_store_root=args.corpus_store_root,
            archive_root=args.archive_root,
            export_root=export_root,
            library_root=library_root,
            source_overrides=tuple(_overrides(getattr(args, "source", [])).values()),
        )
    except CorpusStoreError as exc:
        print(f"archive custody refused: {exc}", file=sys.stderr)
        print(
            "Register a private store first: cce corpus-store register --root <dir>",
            file=sys.stderr,
        )
        return 2
    paths = context.paths

    if action in {"ingest", "build"}:
        providers = args.provider or None
        options = {
            "chatgpt_extra_files": args.chatgpt_extra,
            "include_attachment_text": args.include_attachment_text,
        }
        stages = ("ingest",) if action == "ingest" else STAGES
        limits = limits_from_args(args)
        result = run_stages(
            context,
            stages,
            providers=providers,
            overrides=_overrides(args.source),
            options=options,
            limits=limits,
            merge=args.merge,
        )
        problems = verify_archive(paths, limits) if action == "build" else []
        failed = [n for n, i in result["ingest"]["providers"].items() if i["status"] == "failed"]
        payload = {"stages": result, "problems": problems, "ok": not problems and not failed}
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["ok"] else 1

    if action in {"analyze", "vault", "database", "graph", "site"}:
        result = run_stages(context, (action,), limits=limits_from_args(args))
        print(json.dumps(result[action], indent=2, sort_keys=True))
        return 0

    if action == "verify":
        problems = verify_archive(paths, limits_from_args(args))
        print(json.dumps({"ok": not problems, "problems": problems}, indent=2))
        return 0 if not problems else 1

    if action == "turns":
        try:
            rows = list_turns(
                paths,
                author=args.author,
                provider=args.provider,
                month=args.month,
                uid=args.uid,
                limit=args.limit,
                offset=args.offset,
            )
        except FileNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            for row in rows:
                preview = " ".join(row["text"].split())[:100]
                print(f"{row['author']:<7} {row['provider']:<10} {row['ts'][:16]:<16} {preview}")
            print(f"{len(rows)} turn(s)")
        return 0

    if action == "search":
        try:
            if args.titles:
                results = archive_search.search_titles(
                    paths, args.query, providers=args.provider, months=args.month, limit=args.limit
                )
            else:
                results = archive_search.search(
                    paths,
                    args.query,
                    providers=args.provider,
                    kinds=args.kind,
                    authors=args.author,
                    months=args.month,
                    limit=args.limit,
                )
        except FileNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        except archive_search.FilterNotSupported as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(results, indent=2))
        else:
            for hit in results:
                print(f"{hit['provider']:<10} {hit['created'][:10]:<10} {hit['title'][:70]}")
                print(f"           {hit['uid']}  {hit.get('excerpt', '')}")
            print(f"{len(results)} conversation(s)")
        return 0

    if action == "serve":
        serve(paths, port=args.port)
        return 0

    raise ValueError(f"unsupported archive action: {action}")
