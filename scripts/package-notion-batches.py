#!/usr/bin/env python3
"""Stream a verified library ZIP into deterministic, bounded Notion batches.

Uses only public zipfile APIs. The first pass measures real recompressed entry
sizes into a counting sink; the second writes and verifies every member. No
uncompressed scratch files or changes to the canonical ZIP are required.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import threading
import time
import zipfile
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import Any

CHUNK = 1024**2
FLOOR = 1024**3
FAILURE_RESERVE = 16384
MAX_ENTRIES = 5000
MAX_BATCH_BYTES = 250 * 1024**2
SCHEMA = "cce.notion-batches.v1"


def compatibility(size: int) -> dict[str, bool]:
    """Notion's documented per-file limits use decimal MB, not MiB."""
    return {"free_plan": size <= 5_000_000, "paid_plan": size <= 50_000_000}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _safe_path(path: Path) -> None:
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("source/output path contains a symlink")


def _member_path(name: str) -> None:
    parts = name.rstrip("/").split("/")
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
        or any(part in {"", ".", ".."} for part in parts)
        or ":" in parts[0]
    ):
        raise ValueError("unsafe source member path")


@contextmanager
def _deadline(seconds: int):
    if (
        not hasattr(signal, "setitimer")
        or threading.current_thread() is not threading.main_thread()
    ):
        raise ValueError("bounded packaging requires a main-thread POSIX process")
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()

    def expired(_signal, _frame):
        raise TimeoutError("Notion packaging execution deadline exceeded")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0]:
            signal.setitimer(
                signal.ITIMER_REAL,
                max(0.001, previous_timer[0] - (time.monotonic() - started)),
                previous_timer[1],
            )


class Budget:
    def __init__(self, output: Path, max_bytes: int, max_seconds: int):
        self.output = output
        self.max_bytes = max_bytes
        self.started = time.monotonic()
        self.max_seconds = max_seconds
        self.written = 0
        self.free = shutil.disk_usage(output.parent).free
        self.next_measurement = 0.0

    def check(self, delta: int = 0) -> None:
        now = time.monotonic()
        if now - self.started > self.max_seconds:
            raise TimeoutError("Notion packaging execution deadline exceeded")
        if now >= self.next_measurement:
            self.free = shutil.disk_usage(self.output.parent).free
            self.next_measurement = now + 0.25
        if self.written + delta > self.max_bytes - FAILURE_RESERVE:
            raise ValueError("Notion packaging output byte budget exceeded")
        if self.free - delta < FLOOR:
            raise ValueError("Notion packaging reached free-space floor")

    def charge(self, delta: int) -> None:
        self.check(delta)
        self.written += delta
        self.free -= delta


class CountingSink:
    """Seekable write-only sink: measure zipfile's exact bytes without storage."""

    def __init__(self):
        self.position = 0
        self.size = 0

    def write(self, value):
        self.position += len(value)
        self.size = max(self.size, self.position)
        return len(value)

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        target = offset + (self.position if whence == 1 else self.size if whence == 2 else 0)
        if target < 0 or whence not in {0, 1, 2}:
            raise ValueError("invalid counting sink seek")
        self.position = target
        return target

    def flush(self):
        return None


class BoundedOutput:
    def __init__(self, stream, budget: Budget):
        self.stream = stream
        self.budget = budget
        self.size = 0

    def write(self, value):
        end = self.stream.tell() + len(value)
        growth = max(0, end - self.size)
        self.budget.charge(growth)
        written = self.stream.write(value)
        self.size = max(self.size, self.stream.tell())
        return written

    def seek(self, *args):
        return self.stream.seek(*args)

    def tell(self):
        return self.stream.tell()

    def flush(self):
        self.stream.flush()


@contextmanager
def _private_binary(path: Path):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        yield stream
        stream.flush()
        os.fsync(stream.fileno())


def _sha(path: Path, budget: Budget) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b""):
            budget.check()
            digest.update(chunk)
    return digest.hexdigest()


def _info(name: str, *, directory: bool = False) -> zipfile.ZipInfo:
    item = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    item.create_system = 3
    item.external_attr = ((stat.S_IFDIR | 0o700) if directory else (stat.S_IFREG | 0o600)) << 16
    if directory:
        item.external_attr |= 0x10
    item.compress_type = zipfile.ZIP_STORED if directory else zipfile.ZIP_DEFLATED
    if not directory and hasattr(item, "compress_level"):
        item.compress_level = 6
    return item


def _copy_member(source, original, target, wrapped, budget):
    digest = hashlib.sha256()
    size = 0
    with (
        source.open(original) as reader,
        target.open(_info(wrapped, directory=original.is_dir()), "w", force_zip64=True) as writer,
    ):
        for chunk in iter(lambda: reader.read(CHUNK), b""):
            budget.check()
            digest.update(chunk)
            size += len(chunk)
            writer.write(chunk)
    _require(size == original.file_size, "source member uncompressed size mismatch")
    return digest.hexdigest(), size


def _measure_member(source, original, wrapped, budget, *, wrapper, base_size):
    sink = CountingSink()
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr(_info(wrapper + "/", directory=True), b"")
        digest, size = _copy_member(source, original, archive, wrapped, budget)
        compressed = archive.getinfo(wrapped).compress_size
    return {
        "sha256": digest,
        "bytes": size,
        "compressed_bytes": compressed,
        "archive_contribution_bytes": sink.size - base_size,
    }


def _wrapper_size(wrapper: str) -> int:
    sink = CountingSink()
    with zipfile.ZipFile(sink, "w") as archive:
        archive.writestr(_info(wrapper + "/", directory=True), b"")
    return sink.size


def _finish_batch(path, planned, members, budget):
    _require(path.stat().st_size == planned["bytes"], "final ZIP size differs from measured plan")
    with zipfile.ZipFile(path) as archive:
        _require(
            archive.namelist()
            == [planned["wrapper"] + "/", *(item["archive_path"] for item in members)],
            "output ZIP member identity/order mismatch",
        )
        for item in members:
            digest = hashlib.sha256()
            size = 0
            with archive.open(item["archive_path"]) as stream:
                for chunk in iter(lambda: stream.read(CHUNK), b""):
                    budget.check()
                    size += len(chunk)
                    digest.update(chunk)
            _require(
                size == item["bytes"] and digest.hexdigest() == item["sha256"],
                "output ZIP member byte hash mismatch",
            )
    return {**planned, "sha256": _sha(path, budget), "all_member_bytes_verified": True}


def _readme(manifest: dict[str, Any]) -> bytes:
    counts = manifest["file_size_compatibility"]
    lines = [
        "# Notion import batches",
        "",
        "These are derived copies of the complete canonical ZIP. Every original member is preserved once, with its original relative path below the common `library/` wrapper (or the explicitly configured wrapper). No source text was truncated or split.",
        "",
        "| Measure | Count |",
        "|---|---:|",
        f"| Original members | {manifest['original_members']:,} |",
        f"| Compliant batch ZIPs | {manifest['compliant_batches']:,} |",
        f"| Separate oversized preservation ZIPs | {manifest['oversized_members']:,} |",
        f"| Files exceeding the Free-plan 5,000,000-byte limit | {counts['over_free_plan_5MB']:,} |",
        f"| Files exceeding the paid-plan 50,000,000-byte limit | {counts['over_paid_plan_50MB']:,} |",
        "",
        "The normal ZIPs contain at most 5,000 entries including their wrapper and at most 250 MiB compressed. Any stricter configured caps appear in `manifest.json`. Oversized preservation ZIPs are explicitly exempt from the batch-size claim and are not represented as ready imports.",
        "",
        "Notion documents a 5 GB ZIP cap and warns that 10,000 or more files can cause failed or partial imports. Its Markdown/CSV per-file caps are 5 MB on Free and 50 MB on paid plans; those are decimal MB. [Official import guidance](https://www.notion.com/help/import-data-into-notion).",
        "",
        "Import batch ZIPs individually through Notion's ZIP importer. Cross-batch links and heading anchors are not guaranteed. Native import and navigation have not been verified. A small compressed ZIP does not make an oversized contained file Free-plan compatible.",
        "",
        "`members.jsonl` records every original path, wrapped path, destination ZIP and uncompressed SHA-256. `manifest.json` records archive SHA-256 values, exact byte counts and compatibility counts. `run-receipt.json` records the completed run; the canonical source hash was checked before and after packaging.",
        "",
        "| ZIP | Disposition | Original members | Compressed bytes |",
        "|---|---|---:|---:|",
    ]
    for item in manifest["archives"]:
        lines.append(
            f"| {item['path']} | {item['disposition']} | {item['original_members']:,} | {item['bytes']:,} |"
        )
    return ("\n".join([*lines, ""])).encode()


def package(
    source: Path,
    output: Path,
    *,
    max_entries: int = MAX_ENTRIES,
    max_batch_bytes: int = MAX_BATCH_BYTES,
    max_output_bytes: int = 8 * 1024**3,
    max_seconds: int = 1800,
    wrapper: str = "library",
    progress=None,
) -> dict[str, Any]:
    """Create exclusive derived outputs; retain all failed attempts and source bytes."""
    if not 2 <= max_entries <= MAX_ENTRIES or not 256 <= max_batch_bytes <= MAX_BATCH_BYTES:
        raise ValueError("batch limits must be 2..5000 entries and 256..250 MiB bytes")
    if max_output_bytes < 4 * FAILURE_RESERVE or max_seconds <= 0:
        raise ValueError("positive time and at least 64 KiB output budget are required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", wrapper):
        raise ValueError("wrapper must be one safe folder name")
    source, output = Path(source).absolute(), Path(output).absolute()
    _safe_path(source)
    _safe_path(output)
    if output.exists():
        raise FileExistsError(output)
    if (
        source.parent.parent.name == "generations"
        and source.parent.parent.parent.name == "library-all"
        and output.is_relative_to(source.parent)
    ):
        raise ValueError("derived output must stay outside the canonical generation")
    budget = Budget(output, max_output_bytes, max_seconds)
    source_before = source.stat()
    with _deadline(max_seconds), zipfile.ZipFile(source) as original:
        infos = sorted(original.infolist(), key=lambda item: item.filename)
        if len(infos) > 500000:
            raise ValueError("source member count exceeds 500000")
        names = set()
        for info in infos:
            _member_path(info.filename)
            if info.filename != info.orig_filename:
                raise ValueError("source member path was truncated")
            if info.filename in names:
                raise ValueError("duplicate source member path")
            names.add(info.filename)
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError("source ZIP contains a symlink member")
            if info.flag_bits & 1:
                raise ValueError("encrypted source members cannot be packaged")
            if info.is_dir() and info.file_size:
                raise ValueError("source directory member contains file data")
        source_hash = _sha(source, budget)
        output.mkdir(mode=0o700)
        try:
            plans = []
            current = None
            batch_number = oversize_number = 0
            base_size = _wrapper_size(wrapper)
            file_counts = {
                "files": 0,
                "directories": 0,
                "over_free_plan_5MB": 0,
                "over_paid_plan_50MB": 0,
            }
            with _private_binary(output / "members.jsonl") as ledger:
                for index, info in enumerate(infos):
                    budget.check()
                    wrapped = wrapper + "/" + info.filename
                    measured = _measure_member(
                        original, info, wrapped, budget, wrapper=wrapper, base_size=base_size
                    )
                    alone = base_size + measured["archive_contribution_bytes"]
                    oversized = alone > max_batch_bytes
                    if (
                        oversized
                        or current is None
                        or current["entries_including_wrapper"] >= max_entries
                        or current["bytes"] + measured["archive_contribution_bytes"]
                        > max_batch_bytes
                    ):
                        if oversized:
                            oversize_number += 1
                            name = f"oversized-{oversize_number:06d}.zip"
                        else:
                            batch_number += 1
                            name = f"batch-{batch_number:06d}.zip"
                        current = {
                            "path": name,
                            "disposition": "oversized_preservation"
                            if oversized
                            else "notion_batch",
                            "entries_including_wrapper": 1,
                            "original_members": 0,
                            "bytes": base_size,
                            "wrapper": wrapper,
                        }
                        plans.append(current)
                    current["entries_including_wrapper"] += 1
                    current["original_members"] += 1
                    current["bytes"] += measured["archive_contribution_bytes"]
                    item = {
                        "original_path": info.filename,
                        "archive_path": wrapped,
                        "archive": current["path"],
                        "directory": info.is_dir(),
                        **measured,
                        "notion_file_size_compatibility": compatibility(info.file_size),
                    }
                    encoded = _json(item)
                    budget.charge(len(encoded))
                    ledger.write(encoded)
                    file_counts["directories" if info.is_dir() else "files"] += 1
                    if not info.is_dir():
                        file_counts["over_free_plan_5MB"] += int(info.file_size > 5_000_000)
                        file_counts["over_paid_plan_50MB"] += int(info.file_size > 50_000_000)
                    if oversized:
                        current = None
                    if progress and (index + 1) % 1000 == 0:
                        progress({"stage": "measure", "members": index + 1, "total": len(infos)})
            planned_archives = sum(item["bytes"] for item in plans)
            metadata_reserve = 65536 + 1024 * len(plans)
            if (
                budget.written + planned_archives + metadata_reserve
                > max_output_bytes - FAILURE_RESERVE
            ):
                raise ValueError("Notion packaging output byte budget cannot cover planned output")
            if shutil.disk_usage(output).free < planned_archives + metadata_reserve + FLOOR:
                raise ValueError(
                    "available storage cannot cover planned output and free-space floor"
                )
            verified = []
            seen_members: set[str] = set()
            with (output / "members.jsonl").open() as ledger:
                for plan in plans:
                    members = [json.loads(next(ledger)) for _ in range(plan["original_members"])]
                    _require(
                        all(item["archive"] == plan["path"] for item in members),
                        "member ledger archive mismatch",
                    )
                    path = output / plan["path"]
                    with _private_binary(path) as stream:
                        bounded = BoundedOutput(stream, budget)
                        with zipfile.ZipFile(
                            bounded, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
                        ) as archive:
                            archive.writestr(_info(wrapper + "/", directory=True), b"")
                            for item in members:
                                _require(
                                    item["original_path"] not in seen_members,
                                    "member ledger duplicates an original path",
                                )
                                seen_members.add(item["original_path"])
                                digest, size = _copy_member(
                                    original,
                                    original.getinfo(item["original_path"]),
                                    archive,
                                    item["archive_path"],
                                    budget,
                                )
                                _require(
                                    digest == item["sha256"] and size == item["bytes"],
                                    "source member changed between measurement and copy",
                                )
                                _require(
                                    archive.getinfo(item["archive_path"]).compress_size
                                    == item["compressed_bytes"],
                                    "recompression differs from exact measured size",
                                )
                    verified.append(_finish_batch(path, plan, members, budget))
                    if progress:
                        progress(
                            {
                                "stage": "write_verify",
                                "archives": len(verified),
                                "total": len(plans),
                            }
                        )
                _require(next(ledger, None) is None, "member ledger contains unplanned entries")
            _require(seen_members == names, "not every original member was preserved exactly once")
            source_after = source.stat()
            if (source_before.st_ino, source_before.st_size, source_before.st_mtime_ns) != (
                source_after.st_ino,
                source_after.st_size,
                source_after.st_mtime_ns,
            ) or _sha(source, budget) != source_hash:
                raise ValueError("source ZIP changed during packaging")
            manifest = {
                "schema": SCHEMA,
                "complete": True,
                "source_zip_sha256": source_hash,
                "source_zip_bytes": source_before.st_size,
                "original_members": len(infos),
                "original_member_bytes": sum(info.file_size for info in infos),
                "member_ledger": {
                    "path": "members.jsonl",
                    "sha256": _sha(output / "members.jsonl", budget),
                    "bytes": (output / "members.jsonl").stat().st_size,
                },
                "limits": {
                    "max_entries_including_wrapper": max_entries,
                    "max_batch_bytes": max_batch_bytes,
                    "max_output_bytes": max_output_bytes,
                    "max_seconds": max_seconds,
                    "free_space_floor_bytes": FLOOR,
                },
                "wrapper": wrapper,
                "archives": verified,
                "compliant_batches": batch_number,
                "oversized_members": oversize_number,
                "all_archives_within_batch_limits": oversize_number == 0,
                "file_size_compatibility": file_counts,
                "compression": {
                    "method": "deflate",
                    "level": 6,
                    "zlib_runtime": zlib.ZLIB_RUNTIME_VERSION,
                },
                "source_bytes_unchanged": True,
                "all_original_member_bytes_preserved_exactly_once": True,
                "cross_batch_links_verified_in_notion": False,
                "native_import_verified": False,
                "documented_limits_source": "https://www.notion.com/help/import-data-into-notion",
            }
            readme = _readme(manifest)
            with _private_binary(output / "README.md") as stream:
                budget.charge(len(readme))
                stream.write(readme)
            manifest["readme"] = {
                "path": "README.md",
                "sha256": hashlib.sha256(readme).hexdigest(),
                "bytes": len(readme),
            }
            encoded = _json(manifest)
            receipt = {
                "schema": "cce.notion-batch-run.v1",
                "complete": True,
                "manifest_sha256": hashlib.sha256(encoded).hexdigest(),
                "elapsed_seconds": round(time.monotonic() - budget.started, 3),
                "free_bytes_after": shutil.disk_usage(output).free,
                "source_sha256_before": source_hash,
                "source_sha256_after": source_hash,
            }
            with _private_binary(output / "run-receipt.json") as stream:
                data = _json(receipt)
                budget.charge(len(data))
                stream.write(data)
            with _private_binary(output / "manifest.json") as stream:
                budget.charge(len(encoded))
                stream.write(encoded)
            return manifest
        except BaseException as error:
            failure = _json(
                {
                    "schema": "cce.notion-batch-run.v1",
                    "complete": False,
                    "error_type": type(error).__name__,
                    "error": str(error)[:2048],
                    "source_zip_sha256": source_hash,
                    "retained_partial_outputs": True,
                    "elapsed_seconds": round(time.monotonic() - budget.started, 3),
                }
            )
            if len(failure) <= FAILURE_RESERVE and not (output / "failure.json").exists():
                with _private_binary(output / "failure.json") as stream:
                    stream.write(failure)
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Published full notion-import.zip (read-only)")
    parser.add_argument(
        "--output", type=Path, required=True, help="New private derived-output directory"
    )
    parser.add_argument("--max-entries", type=int, default=MAX_ENTRIES)
    parser.add_argument("--max-batch-bytes", type=int, default=MAX_BATCH_BYTES)
    parser.add_argument("--max-output-bytes", type=int, default=8 * 1024**3)
    parser.add_argument("--max-seconds", type=int, default=1800)
    parser.add_argument("--wrapper", default="library")
    args = parser.parse_args()
    os.nice(10)
    result = package(
        args.source,
        args.output,
        max_entries=args.max_entries,
        max_batch_bytes=args.max_batch_bytes,
        max_output_bytes=args.max_output_bytes,
        max_seconds=args.max_seconds,
        wrapper=args.wrapper,
        progress=lambda value: print(json.dumps(value), flush=True),
    )
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "complete",
                    "original_members",
                    "compliant_batches",
                    "oversized_members",
                    "file_size_compatibility",
                    "native_import_verified",
                )
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
