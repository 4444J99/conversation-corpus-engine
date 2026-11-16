"""`cce wiki zettels --wiki DIR --out DIR`: the atomic Zettel view of a wiki."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .wiki_zettels import emit


def add_wiki_parser(subparsers: Any) -> argparse.ArgumentParser:
    parser = subparsers.add_parser("wiki", help="Views over a Wiki Standard v1 wiki")
    sub = parser.add_subparsers(dest="action", required=True)
    zettels = sub.add_parser(
        "zettels", help="Cut every wiki note into one small source-tupled Zettel per section"
    )
    zettels.add_argument("--wiki", type=Path, required=True, help="Wiki root (read only)")
    zettels.add_argument("--out", type=Path, required=True, help="Zettel folder, outside the wiki")
    zettels.add_argument("--json", action="store_true", help="JSON is the default output")
    return parser


def handle_wiki(args: argparse.Namespace) -> int:
    try:
        result = emit(args.wiki, args.out)
    except (ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result.as_dict(), indent=2))
    return 0
