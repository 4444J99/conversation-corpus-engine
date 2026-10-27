"""Read-only qualification of an ingested, failed library candidate for reuse."""

from __future__ import annotations

import hashlib
import json
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
) -> dict[str, Any]:
    """Validate an immutable failed seed; the caller owns copying and new budgets.

    A legacy report's final documents entry is evidence that the old builder
    exhausted source collection, not an explicit phase checkpoint. The receipt
    distinguishes that weaker proof. This never upgrades coverage or completion.
    """
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
    callback_errors: list[Exception] = []

    def progress() -> int:
        try:
            check()
            return 0
        except Exception as error:
            callback_errors.append(error)
            return 1

    with Library(database) as library:
        library.connection.set_progress_handler(progress, 10000)
        try:
            problems = library.verify(full=True, check_callback=check)
        except Exception:
            if callback_errors:
                raise callback_errors[0] from None
            raise
        finally:
            library.connection.set_progress_handler(None, 0)
        if problems:
            raise ValueError("resume database verification failed: " + "; ".join(problems[:10]))
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
