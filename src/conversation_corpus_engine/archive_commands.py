from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .archive_graph import build_graph
from .archive_ingest import (
    DEFAULT_ARCHIVE_DIRNAME,
    PROVIDER_SPECS,
    ArchivePaths,
    authorize_archive,
    failed_providers,
    ingest,
    load_manifest,
)
from .archive_kb import build_kb, open_kb, search, search_titles, verify_kb
from .archive_serve import serve
from .archive_site import build_site
from .archive_topics import build_analysis
from .archive_vault import build_vault, verify_vault
from .corpus_store import (
    CorpusStoreError,
    CorpusWriteAuthorization,
    resolve_configured_corpus_store_root,
)
from .paths import default_project_root

STAGES = ("ingest", "analyze", "vault", "kb", "graph", "site")


@dataclass(frozen=True)
class ArchiveContext:
    paths: ArchivePaths
    authorization: CorpusWriteAuthorization
    export_root: Path | None


def open_context(
    *,
    project_root: Path,
    corpus_store_root: Path | None,
    archive_root: Path | None,
    export_root: Path | None = None,
) -> ArchiveContext:
    store = resolve_configured_corpus_store_root(corpus_store_root)
    authorization = authorize_archive(
        project_root=project_root,
        archive_root=archive_root or store / DEFAULT_ARCHIVE_DIRNAME,
        corpus_store_root=store,
        source_roots=(export_root.resolve(),) if export_root else (),
    )
    return ArchiveContext(ArchivePaths(authorization.destination), authorization, export_root)


def _say(message: str) -> None:
    print(f"[archive] {message}", file=sys.stderr, flush=True)


def verify_archive(paths: ArchivePaths) -> list[str]:
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
    enrichment = paths.analysis / "enrichment.jsonl"
    if not enrichment.is_file():
        problems.append("analysis missing: run `cce archive analyze`")
    elif sum(1 for _ in enrichment.open(encoding="utf-8")) != total:
        problems.append("analysis row count differs from the manifest")
    problems += verify_vault(paths)
    try:
        problems += verify_kb(paths)
    except FileNotFoundError as exc:
        problems.append(str(exc))
    graph_summary = paths.graph / "summary.json"
    if not graph_summary.is_file():
        problems.append("graph missing: run `cce archive graph`")
    elif (
        json.loads(graph_summary.read_text(encoding="utf-8"))["node_kinds"].get("conversation")
        != total
    ):
        problems.append("graph conversation node count differs from the manifest")
    pages = sum(1 for _ in (paths.site / "c").rglob("*.html")) if (paths.site / "c").is_dir() else 0
    if pages != total:
        problems.append(f"site has {pages} conversation page(s), manifest has {total}")
    return problems


def run_stages(
    context: ArchiveContext,
    stages: tuple[str, ...],
    *,
    providers: list[str] | None = None,
    overrides: dict[str, Path] | None = None,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    paths, authorization = context.paths, context.authorization
    result: dict[str, Any] = {}
    runners: dict[str, Callable[[], dict[str, Any]]] = {
        "analyze": lambda: build_analysis(paths),
        "vault": lambda: build_vault(paths, authorization),
        "kb": lambda: build_kb(paths, authorization),
        "graph": lambda: build_graph(paths, authorization),
        "site": lambda: build_site(paths, authorization),
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
        "archive", help="Browsable archive surfaces: vault, wiki, knowledge base, site, graph"
    )
    sub = archive.add_subparsers(dest="action", required=True)

    def common(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--project-root", type=Path, default=default_project_root())
        parser.add_argument("--corpus-store-root", type=Path)
        parser.add_argument("--archive-root", type=Path)
        parser.add_argument("--json", action="store_true")

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

    ingest_parser = sub.add_parser("ingest", help="Read raw exports into the normalized store")
    common(ingest_parser)
    export_args(ingest_parser)
    for name, text in (
        ("analyze", "Topic hubs and similarity"),
        ("vault", "Obsidian vault (the wiki)"),
        ("kb", "SQLite FTS5 knowledge base"),
        ("graph", "Graph as JSONL + GraphML"),
        ("site", "Static archive site"),
        ("verify", "Cross-check every surface against the manifest"),
    ):
        common(sub.add_parser(name, help=text))
    build = sub.add_parser("build", help="ingest + analyze + vault + kb + graph + site + verify")
    common(build)
    export_args(build)

    search_parser = sub.add_parser("search", help="Full-text search the knowledge base")
    common(search_parser)
    search_parser.add_argument("query")
    search_parser.add_argument("--provider", action="append", choices=sorted(PROVIDER_SPECS))
    search_parser.add_argument("--kind", action="append")
    search_parser.add_argument("--limit", type=int, default=15)
    search_parser.add_argument("--titles", action="store_true", help="search titles only")

    serve_parser = sub.add_parser("serve", help="Serve the site with full-text search on loopback")
    common(serve_parser)
    serve_parser.add_argument("--port", type=int, default=8765)


def _overrides(raw: list[str]) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for item in raw:
        name, sep, value = item.partition("=")
        if not sep or name not in PROVIDER_SPECS or not value:
            raise ValueError(f"--source must be PROVIDER=PATH with a known provider, got: {item}")
        out[name] = Path(value)
    return out


def handle_archive(args: argparse.Namespace) -> int:
    action = args.action
    export_root = getattr(args, "export_root", None)
    try:
        context = open_context(
            project_root=args.project_root,
            corpus_store_root=args.corpus_store_root,
            archive_root=args.archive_root,
            export_root=export_root,
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
        result = run_stages(
            context, stages, providers=providers, overrides=_overrides(args.source), options=options
        )
        problems = verify_archive(paths) if action == "build" else []
        failed = [n for n, i in result["ingest"]["providers"].items() if i["status"] == "failed"]
        payload = {"stages": result, "problems": problems, "ok": not problems and not failed}
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["ok"] else 1

    if action in {"analyze", "vault", "kb", "graph", "site"}:
        result = run_stages(context, (action,))
        print(json.dumps(result[action], indent=2, sort_keys=True))
        return 0

    if action == "verify":
        problems = verify_archive(paths)
        print(json.dumps({"ok": not problems, "problems": problems}, indent=2))
        return 0 if not problems else 1

    if action == "search":
        connection = open_kb(paths)
        try:
            if args.titles:
                results = search_titles(connection, args.query, limit=args.limit)
            else:
                results = search(
                    connection,
                    args.query,
                    providers=args.provider,
                    kinds=args.kind,
                    limit=args.limit,
                )
        finally:
            connection.close()
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
