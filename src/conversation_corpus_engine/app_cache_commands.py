"""``cce app-cache``: recover a provider's own data from its desktop app's local files.

Every action is read-only against the app's folders (no network, cookies or tokens) and writes
only into a new, empty ``--out`` directory, which should live in the private store.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import app_cache_gemini, app_cache_perplexity
from .app_cache_chatgpt import default_roots, scan, write_staging
from .corpus_store import CorpusStoreError, require_git_private


def add_app_cache_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "app-cache", help="Recover provider conversations from a desktop app's local cache"
    )
    sub = parser.add_subparsers(dest="action", required=True)
    chatgpt = sub.add_parser(
        "chatgpt",
        help="Stage conversations cached by the ChatGPT/Codex desktop app (HTTP cache, read-only)",
    )
    chatgpt.add_argument(
        "--root",
        action="append",
        type=Path,
        help="a Chromium Cache_Data directory (repeatable); default: the app's two cache roots",
    )
    chatgpt.add_argument("--out", type=Path, required=True, help="new empty staging directory")
    chatgpt.add_argument("--json", action="store_true")
    perplexity = sub.add_parser(
        "perplexity",
        help="Stage Space notes and follow-up suggestions cached by the Perplexity macOS app",
    )
    perplexity.add_argument("--cache-db", type=Path, help="default: the app's Cache.db")
    perplexity.add_argument("--body-dir", type=Path, help="default: fsCachedData beside Cache.db")
    perplexity.add_argument("--out", type=Path, required=True, help="new empty staging directory")
    perplexity.add_argument("--json", action="store_true")
    gemini = sub.add_parser(
        "gemini",
        help="Census of the Gemini macOS app's chat store (metadata only; bodies are encrypted)",
    )
    gemini.add_argument("--store", type=Path, help="default: the app's ChatInfo2.store")
    gemini.add_argument("--out", type=Path, required=True, help="new empty output directory")
    gemini.add_argument("--json", action="store_true")
    return parser


def handle_app_cache(args: argparse.Namespace) -> int:
    if args.action == "chatgpt":
        roots = args.root or default_roots(Path.home())
        missing = [str(r) for r in roots if not r.is_dir()]
        if (args.root and missing) or len(missing) == len(roots):
            print(f"error: cache root does not exist: {', '.join(missing)}")
            return 2
        try:
            require_git_private(args.out)
            report = write_staging(scan(roots), args.out)
        except (FileExistsError, CorpusStoreError) as error:
            print(f"error: {error}")
            return 2
        print(json.dumps(report, indent=2) if args.json else _summary(report))
        return 0
    if args.action == "perplexity":
        return _perplexity(args)
    if args.action == "gemini":
        return _gemini(args)
    return 2


def _gemini(args: argparse.Namespace) -> int:
    store = args.store or app_cache_gemini.default_store(Path.home())
    try:
        report = app_cache_gemini.write_report(app_cache_gemini.census(store), args.out)
    except (FileExistsError, app_cache_gemini.SnapshotError) as error:
        print(f"error: {error}")
        return 2
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        export = report["owner_export"]
        print(
            f"chats: {report['chats']} ({report['chats_updated_first']} .. "
            f"{report['chats_updated_last']}); cached messages: {report['cached_messages']}\n"
            f"bodies: encrypted, not read. Owner export: {export['takeout_product']} "
            f"-> {export['file_the_reader_detects']}"
        )
    return 0


def _perplexity(args: argparse.Namespace) -> int:
    default_db, default_bodies = app_cache_perplexity.default_paths(Path.home())
    database = args.cache_db or default_db
    bodies = args.body_dir or (database.parent / "fsCachedData")
    shared = Path.home().joinpath(*app_cache_perplexity.SHARED_CONTAINER)
    try:
        require_git_private(args.out)
        result = app_cache_perplexity.scan(database, bodies)
        report = app_cache_perplexity.write_staging(result, args.out, shared)
    except (FileExistsError, CorpusStoreError, app_cache_perplexity.SnapshotError) as error:
        print(f"error: {error}")
        return 2
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(
            f"space notes: {report['space_notes']} in {report['spaces_with_notes']} space(s)\n"
            f"threads with follow-up suggestions: {report['threads_with_followup_suggestions']}"
        )
    return 0


def _summary(report: dict) -> str:
    return (
        f"conversations with full body: {report['conversations_with_full_body']} "
        f"({report['full_body_updated_first']} .. {report['full_body_updated_last']})\n"
        f"known by metadata only: {report['conversations_known_by_metadata_only']}\n"
        f"message-page fetches (not ingested): {report['message_page_fetches']}\n"
        f"bodies unavailable: {report['bodies_unavailable'] or 'none'}"
    )
