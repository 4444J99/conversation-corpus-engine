"""``cce app-cache``: recover a provider's own data from its desktop app's local files.

Every action is read-only against the app's folders (no network, cookies or tokens) and writes
only into a new, empty ``--out`` directory, which should live in the private store.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .app_cache_chatgpt import default_roots, scan, write_staging


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
    return parser


def handle_app_cache(args: argparse.Namespace) -> int:
    if args.action == "chatgpt":
        roots = args.root or default_roots(Path.home())
        try:
            report = write_staging(scan(roots), args.out)
        except FileExistsError as error:
            print(f"error: {error}")
            return 2
        print(json.dumps(report, indent=2) if args.json else _summary(report))
        return 0
    return 2


def _summary(report: dict) -> str:
    return (
        f"conversations with full body: {report['conversations_with_full_body']} "
        f"({report['full_body_updated_first']} .. {report['full_body_updated_last']})\n"
        f"known by metadata only: {report['conversations_known_by_metadata_only']}\n"
        f"message-page fetches (not ingested): {report['message_page_fetches']}\n"
        f"bodies unavailable: {report['bodies_unavailable'] or 'none'}"
    )
