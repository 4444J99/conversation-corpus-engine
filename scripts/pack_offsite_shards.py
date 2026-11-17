#!/usr/bin/env python3
"""Measure and pack files into <= max-byte shards for encrypted offsite copy prep.

This pipeline does not encrypt, create keys, or create vault repositories.
It produces a deterministic packing plan (and optionally materialized shard
directories) so owners can review sizes before running `cce sealed ...`.

Default shard cap is 48 MiB to align with sealed storage v2's maximum signed,
padded encrypted shard (see docs/encrypted-packing-v2.md and SHARD_LIMIT).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MAX_BYTES = 48 * 1024 * 1024


@dataclass
class Entry:
    rel: str
    size: int


def load_selection(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or any(not isinstance(item, str) for item in data):
        raise ValueError("selection must be a JSON array of relative path strings")
    return data


def scan_source(source: Path) -> list[str]:
    rels: list[str] = []
    for root, dirs, files in os.walk(source):
        dirs.sort()
        files.sort()
        for name in files:
            full = Path(root) / name
            if full.is_symlink():
                continue
            rels.append(full.relative_to(source).as_posix())
    return rels


def measure(source: Path, rels: list[str]) -> list[Entry]:
    entries: list[Entry] = []
    for rel in rels:
        full = (source / rel).resolve()
        source_resolved = source.resolve()
        try:
            full.relative_to(source_resolved)
        except ValueError as exc:
            raise ValueError(f"path escapes source: {rel}") from exc
        if full.is_symlink() or not full.is_file():
            raise ValueError(f"not a regular file: {rel}")
        entries.append(Entry(rel=rel, size=full.stat().st_size))
    return entries


def pack(entries: list[Entry], max_bytes: int) -> tuple[list[dict], list[dict]]:
    # Deterministic first-fit-decreasing by size, stable by path for ties.
    ordered = sorted(entries, key=lambda e: (-e.size, e.rel))
    shards: list[dict] = []
    for entry in ordered:
        placed = False
        for shard in shards:
            if shard["bytes"] + entry.size <= max_bytes:
                shard["files"].append({"path": entry.rel, "bytes": entry.size})
                shard["bytes"] += entry.size
                placed = True
                break
        if not placed:
            shards.append(
                {
                    "shard": len(shards) + 1,
                    "bytes": entry.size,
                    "files": [{"path": entry.rel, "bytes": entry.size}],
                }
            )
    oversize = [s for s in shards if s["bytes"] > max_bytes or any(f["bytes"] > max_bytes for f in s["files"])]
    # Re-number sequentially
    for i, shard in enumerate(shards, 1):
        shard["shard"] = i
        shard["name"] = f"shard-{i:04d}"
        shard["file_count"] = len(shard["files"])
    return shards, oversize


def materialize(source: Path, out_dir: Path, shards: list[dict], overwrite: bool) -> None:
    if out_dir.exists():
        if not overwrite:
            raise ValueError(f"output exists (use --overwrite): {out_dir}")
        # Only remove known shard dirs/files created by this layout? safer: require empty/new.
        raise ValueError(f"output exists: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=False)
    for shard in shards:
        sdir = out_dir / shard["name"]
        sdir.mkdir(parents=True, exist_ok=False)
        for item in shard["files"]:
            src = source / item["path"]
            dst = sdir / item["path"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Source root directory")
    parser.add_argument(
        "--selection",
        type=Path,
        help="JSON array of relative paths under --source; defaults to scanning all regular files",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        help=f"Maximum bytes per shard (default {DEFAULT_MAX_BYTES}, 48 MiB)",
    )
    parser.add_argument("--plan-out", type=Path, help="Write packing plan JSON to this path")
    parser.add_argument(
        "--materialize",
        type=Path,
        help="Create shard directories with copies of files under this output directory (must not exist)",
    )
    parser.add_argument(
        "--allow-oversize",
        action="store_true",
        help="Allow a shard containing a single file larger than --max-bytes (shard will exceed cap)",
    )
    args = parser.parse_args(argv)

    source = args.source.expanduser().resolve()
    if not source.is_dir():
        print(f"[pack] source is not a directory: {source}", file=sys.stderr)
        return 2
    if args.max_bytes < 1:
        print("[pack] --max-bytes must be >= 1", file=sys.stderr)
        return 2

    try:
        rels = load_selection(args.selection) if args.selection else scan_source(source)
        entries = measure(source, rels)
        shards, oversize = pack(entries, args.max_bytes)
        plan = {
            "schema": "cce.offsite-shard-pack.v1",
            "source": str(source),
            "selection_file": str(args.selection) if args.selection else None,
            "max_shard_bytes": args.max_bytes,
            "file_count": len(entries),
            "total_bytes": sum(e.size for e in entries),
            "shard_count": len(shards),
            "largest_shard_bytes": max((s["bytes"] for s in shards), default=0),
            "all_shards_within_limit": len(oversize) == 0,
            "oversize_shards": [s["name"] for s in oversize],
            "shards": shards,
            "notes": [
                "No encryption performed; no keys or vault repositories created.",
                "Materialized copies are plaintext shard directories for owner-reviewed transport/prep.",
                "For encrypted publication use `cce sealed plan/publish --storage-format v2` (requires credentials, trust, recipients).",
            ],
        }
        if args.plan_out:
            args.plan_out.parent.mkdir(parents=True, exist_ok=True)
            args.plan_out.write_text(json.dumps(plan, indent=2, sort_keys=False) + "\n", encoding="utf-8")
        if args.materialize:
            if oversize and not args.allow_oversize:
                raise ValueError(
                    "cannot materialize: shard(s) exceed max bytes: "
                    + ", ".join(s["name"] for s in oversize)
                )
            materialize(source, args.materialize.resolve(), shards, overwrite=False)
        print(json.dumps({k: v for k, v in plan.items() if k != "shards"}, indent=2))
        if oversize and not args.allow_oversize:
            print(
                "[pack] oversize input: one or more files exceed --max-bytes; see plan oversize_shards",
                file=sys.stderr,
            )
            return 2
        return 0
    except Exception as exc:
        print(f"[pack] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
