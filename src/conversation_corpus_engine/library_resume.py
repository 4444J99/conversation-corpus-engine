"""Read-only qualification of an ingested, failed library candidate for reuse."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from .library_store import Library, canonical_json, digest_json


def _safe(path: Path) -> None:
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("resume source contains a symlink")


def _count(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"invalid resume source count: {label}")
    return value


def _read(path: Path) -> tuple[dict[str, Any], bytes]:
    _safe(path)
    if path.stat().st_size > 128 * 1024**2:
        raise ValueError("resume receipt exceeds 128 MiB")
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("resume receipt must be an object")
    return value, raw


def prepare_resume(
    generation: Path,
    store_root: Path,
    current_pointer: dict[str, Any] | None,
    max_database_bytes: int,
    *,
    check_callback: Any = None,
    resume_mode: str = "ingested",
) -> dict[str, Any]:
    """Validate an immutable failed seed; the caller owns copying and new budgets.

    A legacy report's final documents entry is evidence that the old builder
    exhausted source collection, not an explicit phase checkpoint. The receipt
    distinguishes that weaker proof. This never upgrades coverage or completion.
    """
    if resume_mode not in {"ingested", "export", "indexed"}:
        raise ValueError("unknown library resume mode")
    _count(max_database_bytes, "max_database_bytes")
    if not max_database_bytes:
        raise ValueError("resume database byte limit must be positive")
    generation, store_root = Path(generation).absolute(), Path(store_root).absolute()
    _safe(generation)
    _safe(store_root)
    parent = store_root / "library-all" / "generations"
    if generation.parent != parent or not generation.is_dir():
        raise ValueError("resume generation must be a direct child of library-all/generations")
    check = check_callback or (lambda: None)
    check()
    state_path = generation / "build-state.json"
    state, state_raw = _read(state_path)
    report = state.get("report")
    if (
        state.get("state") != "failed"
        or not isinstance(report, dict)
        or report.get("complete") is not False
    ):
        raise ValueError(
            "resume requires a failed, incomplete candidate; active builds cannot resume"
        )
    if report.get("schema") != "cce.library-build.v1" or report.get("generation") != str(
        generation
    ):
        raise ValueError("resume report does not identify this generation")
    pointer_path = store_root / "library-all" / "current.json"

    def pointer() -> dict[str, Any] | None:
        _safe(pointer_path)
        return _read(pointer_path)[0] if pointer_path.exists() else None

    if pointer() != current_pointer or report.get("base_pointer") != current_pointer:
        raise ValueError("resume base pointer differs from current publication")
    manifest, manifest_raw = _read(generation / "sources.json")
    if manifest.get("schema") != "cce.library-sources.v1" or digest_json(manifest) != report.get(
        "source_manifest_hash"
    ):
        raise ValueError("resume source manifest hash mismatch")
    sources = report.get("sources")
    if not isinstance(sources, list) or not sources or sources[-1].get("kind") != "documents":
        raise ValueError("resume source collection did not complete")
    total = 0
    inherited = report.get("resume_source")
    if inherited is not None:
        if not isinstance(inherited, dict):
            raise ValueError("invalid inherited resume evidence")
        inherited_receipt, inherited_report = inherited.get("receipt"), inherited.get("report")
        if not isinstance(inherited_receipt, dict) or not isinstance(inherited_report, dict):
            raise ValueError("missing inherited resume receipt or report")
        if (
            digest_json(inherited_receipt) != inherited.get("receipt_sha256")
            or digest_json(inherited_report) != inherited.get("report_sha256")
            or inherited_receipt.get("schema") != "cce.library-resume.v1"
            or inherited_receipt.get("generation") != inherited_report.get("generation")
        ):
            raise ValueError("inherited resume evidence hash mismatch")
        total = _count(inherited_report.get("accepted_observations"), "inherited observations")
        if (
            inherited_receipt.get("accepted_observations") != total
            or report.get("baseline_accepted_observations") != total
        ):
            raise ValueError("inherited resume baseline count mismatch")
    native_paths: list[str] = []
    for index, source in enumerate(sources):
        check()
        if not isinstance(source, dict):
            raise ValueError("invalid resume source receipt")
        kind = source.get("kind")
        if kind in {"normalized_archive", "snapshot_archive"}:
            total += _count(source.get("records"), kind)
        elif kind == "native_estate":
            native = source.get("report", {})
            total += _count(native.get("records", 0), kind)
            native_paths.append(source.get("path"))
        elif kind == "documents" and index == len(sources) - 1:
            counts = source.get("report", {}).get("counts", {})
            total += _count(counts.get("ingested", 0), "documents ingested")
            total += _count(counts.get("metadata_only", 0), "documents metadata_only")
        else:
            raise ValueError("invalid or incomplete resume source sequence")
    if native_paths != manifest.get("estate_databases", []):
        raise ValueError("resume native source collection differs from declared sources")
    if total != _count(report.get("accepted_observations"), "accepted_observations"):
        raise ValueError("resume accepted observations differ from source counts")
    phase = state.get("phase", report.get("phase"))
    if phase is not None and phase not in {
        "ingested",
        "indexing",
        "indexed",
        "exporting",
        "verifying",
    }:
        raise ValueError("resume phase does not prove completed ingestion")
    database = generation / "library.sqlite"
    _safe(database)
    before = database.stat()
    if not database.is_file() or before.st_size > max_database_bytes:
        raise ValueError("resume database byte limit exceeded")
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = database.with_name(database.name + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            raise ValueError("resume source has a SQLite journal sidecar; cannot prove quiescence")
    with Library(database) as library, library.sql_progress(check):
        problems = library.verify(full=True, check_callback=check)
        if problems:
            raise ValueError("resume database verification failed: " + "; ".join(problems[:10]))
        if resume_mode in {"export", "indexed"}:
            if phase not in {"exporting", "verifying"}:
                raise ValueError("resume mode requires a proven completed index")
            status = library.status(check_callback=check)
            if status != report.get("status"):
                raise ValueError("resume completed index status differs from database")
            record_fingerprint = digest_json(
                [
                    (row["uid"], row["current_digest"], row["current_version"])
                    for row in library.rows()
                ]
            )
            if record_fingerprint != report.get("record_fingerprint"):
                raise ValueError("resume record fingerprint differs from database")
            evidence_fingerprint = digest_json(
                {
                    "library": library.evidence_fingerprint(),
                    "sources": report["sources"],
                    "findings": report["findings"],
                    "manifest": report["source_manifest_hash"],
                    "resume_source": report.get("resume_source"),
                }
            )
            if evidence_fingerprint != report.get("evidence_fingerprint"):
                raise ValueError("resume evidence fingerprint differs from database")
    digest = hashlib.sha256()
    with database.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024**2), b""):
            check()
            digest.update(chunk)
    after = database.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ValueError("resume database changed during qualification")
    if (
        _read(state_path)[1] != state_raw
        or _read(generation / "sources.json")[1] != manifest_raw
        or pointer() != current_pointer
    ):
        raise ValueError("resume receipts or current pointer changed during qualification")
    receipt = {
        "schema": "cce.library-resume.v1",
        "resume_mode": resume_mode,
        "generation": str(generation),
        "database_sha256": digest.hexdigest(),
        "database_bytes": after.st_size,
        "build_state_sha256": hashlib.sha256(state_raw).hexdigest(),
        "sources_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "base_pointer": current_pointer,
        "accepted_observations": total,
        "ingestion_proof": "explicit_phase" if phase else "legacy_completed_source_collection",
        "limitation": "Legacy source ordering and counts are weaker than an explicit ingested phase checkpoint."
        if phase is None
        else "",
        "verification": "schema, SQLite integrity, foreign keys, search identity coverage and all normalized object digests",
    }
    serialized = canonical_json(receipt)
    return {
        "database": database,
        "report": json.loads(canonical_json(report)),
        "receipt": receipt,
        "receipt_json": serialized,
        "receipt_sha256": hashlib.sha256(serialized.encode()).hexdigest(),
    }


DISK_FLOOR = 1024**3


def _clone_file(source: Path, destination: Path) -> None:
    """Use the filesystem's exclusive native clone operation; never shell out."""
    if sys.platform != "darwin":
        raise OSError(errno.ENOTSUP, "native clone unavailable on this platform")
    libc = ctypes.CDLL(None, use_errno=True)
    clone = getattr(libc, "clonefile", None)
    if clone is None:
        raise OSError(errno.ENOSYS, "native clone unavailable")
    clone.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
    clone.restype = ctypes.c_int
    if clone(os.fsencode(source), os.fsencode(destination), 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def copy_qualified_database(
    source: Path,
    destination: Path,
    *,
    max_output_bytes: int,
    mutation_reserve: int = 0,
    check_callback: Any = None,
) -> dict[str, Any]:
    """Clone into a fresh candidate, or copy only with full measured capacity.

    Qualification and before/after source hashes belong to the caller. A COW
    clone is accepted only when remaining logical outputs, mutation reserve and
    a 1 GiB free-space floor fit. Fallback never assumes clone savings.
    """
    check = check_callback or (lambda: None)
    _safe(source)
    _safe(destination)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    size = source.stat().st_size
    free_before = shutil.disk_usage(destination.parent).free
    required = max(0, max_output_bytes - size) + mutation_reserve + DISK_FLOOR
    if free_before < required:
        raise ValueError(
            "available storage does not cover COW remaining output budget and reserves"
        )
    check()
    try:
        _clone_file(source, destination)
        method = "copy_on_write"
    except OSError as error:
        if error.errno not in {errno.ENOTSUP, errno.ENOSYS, errno.EXDEV, errno.EINVAL}:
            raise
        if destination.exists() or destination.is_symlink():
            raise ValueError(
                "failed native clone left a destination; refusing replacement"
            ) from error
        if shutil.disk_usage(destination.parent).free < max_output_bytes + DISK_FLOOR:
            raise ValueError("ordinary copy requires full output budget plus 1 GiB") from error
        with source.open("rb") as original, destination.open("xb") as copied:
            destination.chmod(0o600)
            while chunk := original.read(1024**2):
                check()
                if shutil.disk_usage(destination.parent).free < len(chunk) + DISK_FLOOR:
                    raise ValueError("ordinary copy reached free-space floor") from error
                copied.write(chunk)
            copied.flush()
            os.fsync(copied.fileno())
        method = "bounded_copy"
    destination.chmod(0o600)
    free_after = shutil.disk_usage(destination.parent).free
    if free_after < DISK_FLOOR:
        raise ValueError("database copy reached free-space floor")
    check()
    stat = destination.stat()
    if stat.st_size != size:
        raise ValueError("copied database size differs from qualified source")
    return {
        "method": method,
        "database_bytes": size,
        "filesystem_allocated_bytes": stat.st_blocks * 512,
        "free_bytes_before": free_before,
        "free_bytes_after": free_after,
        "required_free_bytes": required,
        "mutation_reserve_bytes": mutation_reserve,
        "free_space_floor_bytes": DISK_FLOOR,
    }
