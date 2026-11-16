#!/usr/bin/env python3
"""Plan, and only on explicit confirmation run, batched hydration of cloud-only documents.

``--plan`` (the default) enumerates document-type files under each explicit ``--root`` with
``lstat`` only, then prints what WOULD be hydrated in batches of at most 5 GiB. Each batch
needs free space of twice its size, because the downloaded copy and the library build both
occupy disk. Nothing is downloaded in plan mode.

``--execute`` hydrates through ``brctl download`` and is refused unless ``--confirm-hydrate``
is also given. No root is ever implied: every folder must be named with ``--root``.
Hydrated files are left in place; this tool never evicts or deletes anything.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from conversation_corpus_engine.library_documents import iter_document_files

GIB = 1024**3
MAX_BATCH_BYTES = 5 * GIB
FREE_SPACE_FACTOR = 2
DATALESS_FLAG = getattr(stat, "SF_DATALESS", 0x40000000)
HYDRATE_TIMEOUT_SECONDS = 1800
POLL_SECONDS = 2.0

Enumerator = Callable[[Path], list[dict[str, Any]]]
DiskUsage = Callable[[Path], Any]
Hydrator = Callable[[Path], None]


def default_enumerator(root: Path) -> list[dict[str, Any]]:
    return list(iter_document_files(root))


def plan_batches(
    entries: list[dict[str, Any]], batch_bytes: int
) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]]]:
    """Group files into path-ordered batches of at most ``batch_bytes``.

    A file larger than a whole batch is never split or hydrated silently: it is returned
    separately so the owner decides.
    """
    batches: list[list[dict[str, Any]]] = []
    oversize: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    used = 0
    for entry in sorted(entries, key=lambda item: item["path"]):
        size = int(entry["size_bytes"])
        if size > batch_bytes:
            oversize.append(entry)
            continue
        if current and used + size > batch_bytes:
            batches.append(current)
            current, used = [], 0
        current.append(entry)
        used += size
    if current:
        batches.append(current)
    return batches, oversize


def build_plan(
    roots: list[Path],
    batch_bytes: int,
    enumerator: Enumerator,
    disk_usage: DiskUsage,
) -> dict[str, Any]:
    batches_out: list[dict[str, Any]] = []
    oversize_out: list[dict[str, Any]] = []
    free_values: list[int] = []
    dataless_files = dataless_bytes = 0
    for root in roots:
        dataless = [entry for entry in enumerator(root) if entry["state"] == "dataless"]
        dataless_files += len(dataless)
        dataless_bytes += sum(int(entry["size_bytes"]) for entry in dataless)
        free = int(disk_usage(root).free)
        free_values.append(free)
        batches, oversize = plan_batches(dataless, batch_bytes)
        oversize_out.extend(
            {"root": str(root), "path": item["path"], "size_bytes": int(item["size_bytes"])}
            for item in oversize
        )
        for batch in batches:
            size = sum(int(item["size_bytes"]) for item in batch)
            required = FREE_SPACE_FACTOR * size
            batches_out.append(
                {
                    "index": len(batches_out) + 1,
                    "root": str(root),
                    "bytes": size,
                    "required_free_bytes": required,
                    "free_bytes": free,
                    "sufficient_free_space": free >= required,
                    "files": [
                        {"path": item["path"], "size_bytes": int(item["size_bytes"])}
                        for item in batch
                    ],
                }
            )
    return {
        "mode": "plan",
        "batch_limit_bytes": batch_bytes,
        "free_space_factor": FREE_SPACE_FACTOR,
        "free_bytes": min(free_values) if free_values else 0,
        "batches": batches_out,
        "oversize": oversize_out,
        "totals": {
            "dataless_files": dataless_files,
            "dataless_bytes": dataless_bytes,
            "batches": len(batches_out),
            "oversize_files": len(oversize_out),
        },
    }


def _gib(value: int) -> str:
    return f"{value / GIB:.2f} GiB"


def render_plan(plan: dict[str, Any]) -> str:
    totals = plan["totals"]
    lines = [
        "iCloud hydration plan: nothing is downloaded in plan mode.",
        f"cloud-only files: {totals['dataless_files']} ({_gib(totals['dataless_bytes'])}); "
        f"batch limit {_gib(plan['batch_limit_bytes'])}; "
        f"each batch needs {plan['free_space_factor']}x its size free.",
    ]
    for batch in plan["batches"]:
        state = "ok" if batch["sufficient_free_space"] else "INSUFFICIENT FREE SPACE"
        lines.append(
            f"batch {batch['index']} [{batch['root']}]: {len(batch['files'])} files, "
            f"{_gib(batch['bytes'])}, needs {_gib(batch['required_free_bytes'])} free "
            f"(have {_gib(batch['free_bytes'])}: {state})"
        )
        lines.extend(f"  WOULD hydrate {item['path']}" for item in batch["files"])
    for item in plan["oversize"]:
        lines.append(
            f"oversize, not batched [{item['root']}]: {item['path']} ({_gib(item['size_bytes'])})"
        )
    return "\n".join(lines)


def _still_dataless(path: Path) -> bool:
    info = path.lstat()
    if stat.S_ISDIR(info.st_mode):
        for directory, _dirs, files in os.walk(path, followlinks=False):
            for name in files:
                if (Path(directory) / name).lstat().st_flags & DATALESS_FLAG:
                    return True
        return False
    return bool(getattr(info, "st_flags", 0) & DATALESS_FLAG)


def brctl_hydrator(path: Path) -> None:
    """Ask iCloud to download one item and wait until it is no longer a placeholder."""
    executable = shutil.which("brctl")
    if executable is None:
        raise RuntimeError("brctl is not available on this host")
    subprocess.run([executable, "download", str(path)], check=True, timeout=120)
    deadline = time.monotonic() + HYDRATE_TIMEOUT_SECONDS
    while _still_dataless(path):
        if time.monotonic() > deadline:
            raise TimeoutError(f"still a placeholder after {HYDRATE_TIMEOUT_SECONDS}s")
        time.sleep(POLL_SECONDS)


def execute_plan(plan: dict[str, Any], disk_usage: DiskUsage, hydrator: Hydrator) -> dict[str, Any]:
    """Hydrate batch by batch, re-checking free space before each batch."""
    hydrated: list[str] = []
    failed: list[dict[str, str]] = []
    stopped = ""
    for batch in plan["batches"]:
        free = int(disk_usage(Path(batch["root"])).free)
        if free < batch["required_free_bytes"]:
            stopped = f"batch {batch['index']}: free space below {FREE_SPACE_FACTOR}x batch size"
            break
        for item in batch["files"]:
            target = Path(batch["root"]) / item["path"]
            try:
                hydrator(target)
                hydrated.append(item["path"])
            except (OSError, RuntimeError, TimeoutError, subprocess.SubprocessError) as error:
                failed.append({"path": item["path"], "error": type(error).__name__})
    return {"mode": "execute", "hydrated": len(hydrated), "failed": failed, "stopped": stopped}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", maxsplit=1)[0])
    parser.add_argument(
        "--root",
        action="append",
        required=True,
        type=Path,
        help="folder to enumerate; repeat for several. Never implied.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="print the plan (default)")
    mode.add_argument("--execute", action="store_true", help="hydrate; needs --confirm-hydrate")
    parser.add_argument(
        "--confirm-hydrate",
        action="store_true",
        help="required with --execute: acknowledges that files will be downloaded",
    )
    parser.add_argument(
        "--batch-gib", type=float, default=5.0, help="batch size in GiB (capped at 5)"
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    enumerator: Enumerator | None = None,
    disk_usage: DiskUsage | None = None,
    hydrator: Hydrator | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    if args.execute and not args.confirm_hydrate:
        print(
            "icloud_hydrate: --execute downloads files and requires --confirm-hydrate",
            file=sys.stderr,
        )
        return 2
    if args.batch_gib <= 0:
        print("icloud_hydrate: --batch-gib must be positive", file=sys.stderr)
        return 2
    batch_bytes = min(int(args.batch_gib * GIB), MAX_BATCH_BYTES)
    try:
        plan = build_plan(
            args.root,
            batch_bytes,
            enumerator or default_enumerator,
            disk_usage or shutil.disk_usage,
        )
    except (OSError, ValueError) as error:
        print(f"icloud_hydrate: {error}", file=sys.stderr)
        return 2
    result = plan
    code = 0
    if args.execute:
        result = {
            **plan,
            **execute_plan(
                plan,
                disk_usage or shutil.disk_usage,
                hydrator or brctl_hydrator,
            ),
        }
        code = 1 if result["failed"] or result["stopped"] else 0
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(render_plan(plan))
        if args.execute:
            print(
                f"hydrated {result['hydrated']}, failed {len(result['failed'])}"
                + (f", stopped: {result['stopped']}" if result["stopped"] else "")
            )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
