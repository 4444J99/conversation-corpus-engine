"""Bounded candidate builds and atomic publication in the registered CCE store."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import threading
import time
import uuid
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any

from . import library_export
from .archive_model import Conversation, sha256_file
from .corpus_store import authorize_corpus_write
from .library_documents import iter_documents
from .library_native import iter_estate
from .library_resume import DISK_FLOOR, copy_qualified_database, prepare_resume
from .library_store import LIBRARY_SCHEMA, SCHEMA_SQL, Library, digest_json

SOURCES_SCHEMA = "cce.library-sources.v1"
REPORT_SCHEMA = "cce.library-build.v1"
DIRECTORY = "library-all"
POINTER = "current.json"
DEFAULT_LIMITS = {"max_records": 100000, "max_seconds": 1800, "max_output_bytes": 8 * 1024**3}
FAILURE_RESERVE = 4096


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def private_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temp.open("x", encoding="utf-8") as stream:
        temp.chmod(0o600)
        stream.write(_json_bytes(value).decode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _pointer(store_root: Path) -> dict[str, Any] | None:
    path = store_root / DIRECTORY / POINTER
    if path.is_symlink():
        raise ValueError("library pointer cannot be a symlink")
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema") != "cce.library-current.v1":
        raise ValueError("unknown library pointer schema")
    return value


def current_generation(store_root: Path) -> Path | None:
    pointer = _pointer(store_root)
    if pointer is None:
        return None
    relative = Path(pointer["generation"])
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or len(relative.parts) != 2
        or relative.parts[0] != "generations"
    ):
        raise ValueError("library pointer escapes generation store")
    path = store_root / DIRECTORY / relative
    if path.is_symlink() or not path.is_dir():
        raise ValueError("library generation is missing or symlinked")
    if path.resolve().parent != (store_root / DIRECTORY / "generations").resolve():
        raise ValueError("library generation escapes its store")
    report_path = path / "report.json"
    if (
        not report_path.is_file()
        or report_path.is_symlink()
        or sha256_file(report_path) != pointer.get("report_sha256")
    ):
        raise ValueError("current library report hash mismatch")
    return path.resolve()


def _validate_manifest(manifest: dict[str, Any]) -> dict[str, int]:
    if manifest.get("schema") != SOURCES_SCHEMA:
        raise ValueError(f"source manifest must use {SOURCES_SCHEMA}")
    for key in ("archives", "snapshot_archives", "estate_databases"):
        values = manifest.get(key, [])
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not Path(value).is_absolute() for value in values
        ):
            raise ValueError(f"{key} must list explicit absolute source paths")
    if not isinstance(manifest.get("documents", []), list):
        raise ValueError("documents must be a list of explicit source declarations")
    resume_generation = manifest.get("resume_generation")
    if resume_generation is not None and (
        not isinstance(resume_generation, str) or not Path(resume_generation).is_absolute()
    ):
        raise ValueError("resume_generation must name an explicit absolute failed generation")
    resume_mode = manifest.get("resume_mode", "ingested")
    if resume_mode not in {"ingested", "export", "indexed"}:
        raise ValueError("unknown library resume mode")
    if resume_mode != "ingested" and resume_generation is None:
        raise ValueError("indexed/export resume mode requires resume_generation")
    if resume_mode == "export" and any(
        manifest.get(key)
        for key in ("archives", "snapshot_archives", "estate_databases", "documents")
    ):
        raise ValueError("export resume cannot include new source declarations")
    native_limit = manifest.get("native_max_source_bytes", 64 * 1024**2)
    if isinstance(native_limit, bool) or not isinstance(native_limit, int) or native_limit <= 0:
        raise ValueError("native_max_source_bytes must be positive")
    limits = {**DEFAULT_LIMITS, **manifest.get("limits", {})}
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in limits.values()
    ):
        raise ValueError("library limits must be positive integers")
    return limits


def _bounded_build(function: Any) -> Any:
    @wraps(function)
    def run(manifest: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        limits = _validate_manifest(manifest)
        if (
            not hasattr(signal, "setitimer")
            or threading.current_thread() is not threading.main_thread()
        ):
            raise ValueError("bounded builds require a main-thread POSIX process")
        previous_handler = signal.getsignal(signal.SIGALRM)
        previous_timer = signal.getitimer(signal.ITIMER_REAL)
        started = time.monotonic()

        def expired(_signal: int, _frame: Any) -> None:
            raise TimeoutError("library execution deadline exceeded")

        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, limits["max_seconds"])
        try:
            return function(manifest, **kwargs)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
            if previous_timer[0]:
                signal.setitimer(
                    signal.ITIMER_REAL,
                    max(0.001, previous_timer[0] - (time.monotonic() - started)),
                    previous_timer[1],
                )

    return run


@_bounded_build
def build_library(
    manifest: dict[str, Any],
    *,
    project_root: Path,
    store_root: Path,
    progress: Any = None,
) -> dict[str, Any]:
    """Build a private immutable candidate; publication is a separate explicit operation."""
    limits = _validate_manifest(manifest)
    resume_mode = manifest.get("resume_mode", "ingested")
    reuse_index = resume_mode in {"export", "indexed"}
    started = time.monotonic()

    def emit(event: dict[str, Any]) -> None:
        """Report a progress event; consumers get the original keys plus elapsed seconds."""
        if progress:
            progress({**event, "elapsed_seconds": round(time.monotonic() - started, 3)})

    working_budget = limits["max_output_bytes"] - FAILURE_RESERVE
    if working_budget <= 0:
        raise ValueError(
            "library output byte limit cannot reserve failure evidence: "
            f"max_output_bytes={limits['max_output_bytes']} failure_reserve={FAILURE_RESERVE}"
        )
    store_root = store_root.expanduser().absolute()
    previous_pointer = _pointer(store_root)
    previous = current_generation(store_root)
    name = (
        "generation-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12]
    )
    destination = store_root / DIRECTORY / "generations" / name
    sources = [
        Path(path)
        for key in ("archives", "snapshot_archives", "estate_databases")
        for path in manifest.get(key, [])
    ]
    sources.extend(Path(row["root"]).expanduser() for row in manifest.get("documents", []))
    resume_generation = (
        Path(manifest["resume_generation"]) if manifest.get("resume_generation") else None
    )
    if resume_generation is not None:
        sources.append(resume_generation)
    authorization = authorize_corpus_write(
        project_root=project_root,
        corpus_store_root=store_root,
        destination=destination,
        source_roots=sources,
        live_roots=[previous] if previous else [],
    )
    if not reuse_index:
        free = shutil.disk_usage(authorization.store_root).free
        if free < limits["max_output_bytes"] + DISK_FLOOR:
            raise ValueError(
                "available storage does not cover declared library output budget plus 1 GiB: "
                f"free={free} max_output_bytes={limits['max_output_bytes']} floor={DISK_FLOOR}"
            )
    if previous:
        previous_database_bytes = (previous / "library.sqlite").stat().st_size
        if previous_database_bytes > limits["max_output_bytes"]:
            raise ValueError(
                "previous library database exceeds declared output byte limit: "
                f"database={previous_database_bytes} max_output_bytes={limits['max_output_bytes']}"
            )
    resume = None
    if resume_generation is not None:
        emit({"stage": "qualify_resume", "generation": str(resume_generation)})

        def check_resume_deadline() -> None:
            if time.monotonic() - started > limits["max_seconds"]:
                raise ValueError("library execution deadline exceeded")

        resume = prepare_resume(
            resume_generation,
            store_root,
            previous_pointer,
            limits["max_output_bytes"],
            check_callback=check_resume_deadline,
            resume_mode=resume_mode,
        )
        if resume["report"]["accepted_observations"] > limits["max_records"]:
            raise ValueError("resume baseline exceeds declared record limit")
    destination = authorization.destination
    destination.mkdir(parents=True, mode=0o700)
    # Intermediate ancestors are private even with an ambient group-readable umask.
    for parent in (destination.parent, destination.parent.parent):
        parent.chmod(0o700)
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "generation": str(destination),
        "source_manifest_hash": digest_json(manifest),
        "base_pointer": previous_pointer,
        "findings": [],
        "sources": [],
        "accepted_observations": 0,
        "changed_records": 0,
        "limits": limits,
        "complete": False,
        "phase": "ingesting",
    }
    if previous:
        inherited = json.loads((previous / "report.json").read_text(encoding="utf-8"))
        report["inherited_report"] = {
            "path": str(previous / "report.json"),
            "sha256": sha256_file(previous / "report.json"),
            "coverage_complete": inherited.get("coverage_complete", False),
            "notice": "Prior source observations and unresolved coverage remain in this immutable report; absent source declarations do not delete retained records.",
        }
    if resume is not None:
        seed_report = resume["report"]
        report["accepted_observations"] = seed_report["accepted_observations"]
        report["changed_records"] = seed_report.get("changed_records", 0)
        report["baseline_accepted_observations"] = seed_report["accepted_observations"]
        report["new_observations"] = 0
        report["findings"] = json.loads(json.dumps(seed_report.get("findings", [])))
        report["resume_source"] = {
            "generation": str(resume_generation),
            "receipt": resume["receipt"],
            "receipt_sha256": resume["receipt_sha256"],
            "report": seed_report,
            "report_sha256": digest_json(seed_report),
        }
        if "inherited_report" in seed_report:
            report["inherited_report"] = seed_report["inherited_report"]
    planned_output_bytes = 0
    measured_database_bytes = 0
    receipt_bytes = 0
    next_size_check = 0.0
    free_bytes = shutil.disk_usage(authorization.store_root).free
    changed_uids: set[str] = set()

    def write_receipt(filename: str, value: Any, *, failure: bool = False) -> None:
        nonlocal receipt_bytes, measured_database_bytes
        # Checkpoints are infrequent. Measure all actual bytes here, including
        # old receipts retained during the atomic temporary-file replacement.
        current = sum(path.stat().st_size for path in destination.rglob("*") if path.is_file())
        ceiling = limits["max_output_bytes"] if failure else working_budget
        writing = len(_json_bytes(value))
        if current + writing > ceiling:
            raise ValueError(
                "library output byte limit exceeded by receipt: "
                f"current={current} writing={writing} ceiling={ceiling} "
                f"max_output_bytes={limits['max_output_bytes']}"
            )
        private_json(destination / filename, value)
        receipt_bytes = sum(
            path.stat().st_size
            for name in ("sources.json", "build-state.json", "report.json")
            if (path := destination / name).is_file()
        )
        database = destination / "library.sqlite"
        measured_database_bytes = database.stat().st_size if database.is_file() else 0

    def check_bound(*, record: bool = False, bytes_written: int = 0) -> None:
        nonlocal planned_output_bytes, measured_database_bytes, next_size_check, free_bytes
        now = time.monotonic()
        if now - started > limits["max_seconds"]:
            raise ValueError("library execution deadline exceeded")
        if record and report["accepted_observations"] >= limits["max_records"]:
            raise ValueError("library record limit exceeded")
        database = destination / "library.sqlite"
        if record or now >= next_size_check:
            if reuse_index:
                free_bytes = shutil.disk_usage(authorization.store_root).free
            measured_database_bytes = database.stat().st_size if database.exists() else 0
            next_size_check = now + 0.25
        if reuse_index and free_bytes - bytes_written < DISK_FLOOR:
            raise ValueError(
                "library build reached free-space floor: "
                f"free={free_bytes} writing={bytes_written} floor={DISK_FLOOR}"
            )
        if (
            measured_database_bytes + planned_output_bytes + bytes_written + receipt_bytes
            > working_budget
        ):
            raise ValueError(
                "library output byte limit exceeded: "
                f"database={measured_database_bytes} planned={planned_output_bytes} "
                f"writing={bytes_written} receipts={receipt_bytes} "
                f"budget={working_budget} max_output_bytes={limits['max_output_bytes']}"
            )
        planned_output_bytes += bytes_written

    def hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024**2), b""):
                check_bound()
                digest.update(chunk)
        return digest.hexdigest()

    def assert_resume_unchanged() -> dict[str, str]:
        if resume is None:
            return {}
        seed = resume["database"].parent
        observed_hashes = {}
        for filename, key in (
            ("library.sqlite", "database_sha256"),
            ("build-state.json", "build_state_sha256"),
            ("sources.json", "sources_sha256"),
        ):
            path = seed / filename
            if any(part.is_symlink() for part in (path, *path.parents)):
                raise ValueError("resume seed became symlinked before or during copy")
            observed_hashes[filename] = hash_file(path)
            if observed_hashes[filename] != resume["receipt"][key]:
                raise ValueError("resume seed hash changed before or during copy")
        if _pointer(store_root) != previous_pointer:
            raise ValueError("current library pointer changed during resume copy")
        return observed_hashes

    try:
        write_receipt("sources.json", manifest)
        write_receipt("build-state.json", {"state": "building", "report": report})
        source_hashes_before_copy = assert_resume_unchanged()
        if resume is not None:
            emit({"stage": "copy_resume", "generation": str(resume_generation)})
        database_source = (
            resume["database"]
            if resume is not None
            else previous / "library.sqlite"
            if previous
            else None
        )
        if database_source is not None:
            initial_database_bytes = database_source.stat().st_size
        else:
            # Establish the actual empty-schema size without creating an
            # over-budget file before SQLite's page cap can be installed.
            with sqlite3.connect(":memory:") as probe:
                probe.executescript(SCHEMA_SQL)
                probe.execute("INSERT INTO meta VALUES ('schema', ?)", (LIBRARY_SCHEMA,))
                initial_database_bytes = (
                    probe.execute("PRAGMA page_count").fetchone()[0]
                    * probe.execute("PRAGMA page_size").fetchone()[0]
                )
        if initial_database_bytes + receipt_bytes > working_budget:
            raise ValueError(
                "library output byte limit exceeded by database copy or schema: "
                f"database={initial_database_bytes} receipts={receipt_bytes} "
                f"budget={working_budget} max_output_bytes={limits['max_output_bytes']}"
            )
        if reuse_index:
            report["resume_copy"] = copy_qualified_database(
                resume["database"],
                destination / "library.sqlite",
                max_output_bytes=limits["max_output_bytes"],
                mutation_reserve=DISK_FLOOR if resume_mode == "indexed" else 0,
                check_callback=check_bound,
            )
            copied_database_sha256 = hash_file(destination / "library.sqlite")
            if copied_database_sha256 != resume["receipt"]["database_sha256"]:
                raise ValueError("copied database hash differs from qualified seed")
            candidate = Library(destination / "library.sqlite", writable=resume_mode == "indexed")
        else:
            candidate = Library.create(
                destination / "library.sqlite",
                previous=database_source,
                compact_previous=resume is not None,
            )
        with candidate as library, candidate.sql_progress(check_bound):
            source_hashes_after_copy = assert_resume_unchanged()
            if reuse_index:
                report["resume_copy"].update(
                    source_hashes_before=source_hashes_before_copy,
                    source_hashes_after=source_hashes_after_copy,
                    copied_database_sha256=copied_database_sha256,
                )
            page_size = library.connection.execute("PRAGMA page_size").fetchone()[0]
            if resume_mode != "export":
                library.connection.execute(
                    f"PRAGMA max_page_count={max(1, (working_budget - receipt_bytes) // page_size)}"
                )

            if resume_mode == "export":
                report["sources"] = [{"kind": "documents", "report": {"counts": {}}}]
                report["status"] = resume["report"]["status"]
            else:

                def accept(item: Conversation, *, observation_only: bool = False) -> None:
                    check_bound(record=True)
                    change = library.put(item, reconcile=True, observation_only=observation_only)
                    report["accepted_observations"] += 1
                    if resume is not None:
                        report["new_observations"] += 1
                    report["changed_records"] += int(change["changed"])
                    if change["changed"]:
                        changed_uids.add(change["uid"])
                    if report["accepted_observations"] % 100 == 0:
                        emit(
                            {
                                "accepted": report["accepted_observations"],
                                "changed": report["changed_records"],
                            }
                        )

                archive_sources = [(path, False) for path in manifest.get("archives", [])]
                archive_sources.extend(
                    (path, True) for path in manifest.get("snapshot_archives", [])
                )
                for archive_name, observation_only in archive_sources:
                    archive = Path(archive_name)
                    if not archive.is_dir():
                        report["findings"].append({"code": "archive_missing", "path": str(archive)})
                        continue
                    files = sorted(archive.glob("provider-*/collection-*-conversations.jsonl"))
                    if not files:
                        report["findings"].append({"code": "archive_empty", "path": str(archive)})
                    for source in files:
                        if source.is_symlink() or source.parent.is_symlink():
                            raise ValueError("normalized archive source must not be symlinked")
                        before = source.stat()
                        count = 0
                        source_digest = hashlib.sha256()
                        with source.open("rb") as stream:
                            while raw_line := stream.readline(128 * 1024**2 + 1):
                                check_bound()
                                if len(raw_line) > 128 * 1024**2:
                                    raise ValueError("normalized archive row exceeds 128 MiB")
                                source_digest.update(raw_line)
                                if not raw_line.strip():
                                    continue
                                item = Conversation.from_dict(json.loads(raw_line))
                                item.extra.setdefault("wing", "ai")
                                item.extra.setdefault("lane", "conversations")
                                item.extra.setdefault(
                                    "body_status", "full" if item.messages else "metadata_only"
                                )
                                accept(item, observation_only=observation_only)
                                count += 1
                        after = source.stat()
                        if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                            after.st_size,
                            after.st_mtime_ns,
                            after.st_ino,
                        ):
                            raise ValueError("normalized archive source changed during read")
                        report["sources"].append(
                            {
                                "kind": "snapshot_archive"
                                if observation_only
                                else "normalized_archive",
                                "path": str(source),
                                "records": count,
                                "sha256": source_digest.hexdigest(),
                            }
                        )
                for database_name in manifest.get("estate_databases", []):
                    native_report: dict[str, Any] = {}
                    for item in iter_estate(
                        Path(database_name),
                        report=native_report,
                        max_source_bytes=manifest.get("native_max_source_bytes", 64 * 1024**2),
                    ):
                        accept(item)
                    report["sources"].append(
                        {"kind": "native_estate", "path": database_name, "report": native_report}
                    )
                    report["findings"].extend(
                        {
                            "code": code,
                            "count": count,
                            "scope": "native_estate",
                            "path": database_name,
                        }
                        for code, count in native_report.get("findings", {}).items()
                    )
                document_report: dict[str, Any] = {}
                for item in iter_documents(manifest.get("documents", []), report=document_report):
                    accept(item)
                report["sources"].append({"kind": "documents", "report": document_report})
                report["findings"].extend(document_report.get("findings", []))
                check_bound()
                report["phase"] = "ingested"
                write_receipt(
                    "build-state.json",
                    {"state": "building", "phase": "ingested", "report": report},
                )
                library.connection.execute(
                    f"PRAGMA max_page_count={max(1, (working_budget - receipt_bytes) // page_size)}"
                )
                emit({"stage": "index_blocks", "accepted": report["accepted_observations"]})
                report["phase"] = "indexing"
                # index_blocks ends with Library.status(), so the next event also marks
                # the index and the status summary as committed.
                report["status"] = library.index_blocks(
                    check_callback=check_bound,
                    **({"uids": sorted(changed_uids)} if resume_mode == "indexed" else {}),
                )
                check_bound()
            emit({"stage": "fingerprint", "records": report["status"]["records"]})
            report["record_fingerprint"] = digest_json(
                [
                    (row["uid"], row["current_digest"], row["current_version"])
                    for row in library.rows()
                ]
            )
            report["evidence_fingerprint"] = digest_json(
                {
                    "library": library.evidence_fingerprint(),
                    "sources": report["sources"],
                    "findings": report["findings"],
                    "manifest": report["source_manifest_hash"],
                    "resume_source": report.get("resume_source"),
                }
            )
            problems = []
            if resume_mode != "export":
                emit({"stage": "verify", "records": report["status"]["records"]})
                problems = library.verify(check_callback=check_bound)
            if problems:
                raise ValueError("library verification failed: " + "; ".join(problems[:10]))
            emit({"stage": "export", "records": report["status"]["records"]})
            report["phase"] = "exporting"
            report["exports"] = library_export.export_library(
                library, destination, check_callback=check_bound
            )
        check_bound()
        emit(
            {
                "stage": "verify_export",
                "database_bytes": measured_database_bytes,
                "planned_output_bytes": planned_output_bytes,
                "working_budget": working_budget,
            }
        )
        report["phase"] = "verifying"
        problems = library_export.verify_export(destination, check_callback=check_bound)
        if problems:
            raise ValueError("library export verification failed: " + "; ".join(problems[:10]))
        # Hashing every artifact re-reads the whole output; announce it so the run is not
        # mistaken for a stall after the export verification event.
        emit({"stage": "artifacts"})
        artifacts = {}
        for file in sorted(destination.rglob("*")):
            if file.is_symlink():
                raise ValueError("candidate output contains a symlink")
            if file.is_dir():
                file.chmod(0o700)
            elif file.is_file() and file.name != "build-state.json":
                file.chmod(0o600)
                artifacts[file.relative_to(destination).as_posix()] = {
                    "sha256": hash_file(file),
                    "bytes": file.stat().st_size,
                }
        report["artifact_bytes"] = sum(item["bytes"] for item in artifacts.values())
        if report["artifact_bytes"] > limits["max_output_bytes"]:
            raise ValueError(
                "library output byte limit exceeded: "
                f"artifacts={report['artifact_bytes']} "
                f"max_output_bytes={limits['max_output_bytes']}"
            )
        report["artifacts"] = artifacts
        report["coverage_complete"] = (
            not report["findings"]
            and report.get("inherited_report", {}).get("coverage_complete", True)
            and (resume is None or resume["report"].get("coverage_complete", True))
            and not report["status"]["by_body_status"].get("metadata_only", 0)
            and not report["status"]["by_body_status"].get("partial", 0)
        )
        report["complete"] = True
        report["phase"] = "verified"
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        write_receipt(
            "build-state.json",
            {"state": "verified", "record_fingerprint": report["record_fingerprint"]},
        )
        existing_bytes = sum(
            path.stat().st_size for path in destination.rglob("*") if path.is_file()
        )
        report["output_bytes"] = existing_bytes
        while (total := existing_bytes + len(_json_bytes(report))) != report["output_bytes"]:
            report["output_bytes"] = total
        # Write the completed report last. A failed receipt can never leave a
        # publishable report behind without the verified state and byte check.
        write_receipt("report.json", report)
        return report
    except Exception as error:
        report["complete"] = False
        if report["phase"] == "verified":
            report["phase"] = "verifying"
        try:
            write_receipt(
                "build-state.json",
                {"state": "failed", "error": str(error), "report": report},
                failure=True,
            )
        except ValueError:
            # Keep a bounded, explicitly non-resumable receipt when retaining
            # the complete source proof would itself breach the output cap.
            write_receipt(
                "build-state.json",
                {
                    "state": "failed",
                    "resumable": False,
                    "report_omitted": True,
                    "reason": "full failure report exceeds output byte limit",
                    "error_type": type(error).__name__,
                    "error": str(error)[:512],
                    "phase": report["phase"],
                    "source_manifest_hash": report["source_manifest_hash"],
                    "accepted_observations": report["accepted_observations"],
                },
                failure=True,
            )
        raise


def verify_candidate(generation: Path) -> dict[str, Any]:
    report = json.loads((generation / "report.json").read_text(encoding="utf-8"))
    if report.get("schema") != REPORT_SCHEMA or not report.get("complete"):
        raise ValueError("candidate does not have completed verification")
    state = json.loads((generation / "build-state.json").read_text(encoding="utf-8"))
    if state.get("state") != "verified" or state.get("record_fingerprint") != report.get(
        "record_fingerprint"
    ):
        raise ValueError("candidate build state is not verified")
    allowed = set(report["artifacts"]) | {"report.json", "build-state.json"}
    total = 0
    for path in generation.rglob("*"):
        relative = path.relative_to(generation).as_posix()
        if path.is_symlink():
            raise ValueError(f"candidate contains unexpected symlink: {relative}")
        if path.is_file():
            if relative not in allowed:
                raise ValueError(f"candidate contains unexpected file: {relative}")
            total += path.stat().st_size
    if total > report["limits"]["max_output_bytes"]:
        raise ValueError(
            "candidate output byte limit exceeded including receipts: "
            f"total={total} max_output_bytes={report['limits']['max_output_bytes']}"
        )
    for relative, expected in report["artifacts"].items():
        path = generation / relative
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(generation.resolve())
            or not path.is_file()
        ):
            raise ValueError(f"candidate artifact missing or outside generation: {relative}")
        if path.stat().st_size != expected["bytes"] or sha256_file(path) != expected["sha256"]:
            raise ValueError(f"candidate artifact hash mismatch: {relative}")
    with Library(generation / "library.sqlite") as library:
        problems = library.verify(full=False)
        if problems:
            raise ValueError("candidate database verification failed: " + "; ".join(problems))
    return report


def publish_library(generation: Path, *, project_root: Path, store_root: Path) -> dict[str, Any]:
    generation = generation.expanduser().absolute()
    store_root = store_root.expanduser().absolute()
    project_root = project_root.expanduser().absolute()
    expected_parent = (store_root / DIRECTORY / "generations").resolve()
    if generation.parent.resolve() != expected_parent or generation.is_symlink():
        raise ValueError("candidate must be a direct generation of the configured library")
    authorize_corpus_write(
        project_root=project_root, corpus_store_root=store_root, destination=generation
    )
    root = store_root / DIRECTORY
    lock_path = root / "publication.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = verify_candidate(generation)
        report_hash = sha256_file(generation / "report.json")
        current = _pointer(store_root)
        relative = generation.relative_to(root).as_posix()
        if current and current.get("generation") == relative:
            return {"changed": False, **current}
        if current != report["base_pointer"]:
            raise ValueError("library current generation changed after candidate started")
        if current and current.get("evidence_fingerprint") == report["evidence_fingerprint"]:
            return {"changed": False, **current}
        pointer = {
            "schema": "cce.library-current.v1",
            "generation": relative,
            "record_fingerprint": report["record_fingerprint"],
            "evidence_fingerprint": report["evidence_fingerprint"],
            "source_manifest_hash": report["source_manifest_hash"],
            "report_sha256": report_hash,
            "published_at": datetime.now(UTC).isoformat(),
            "coverage_complete": report["coverage_complete"],
        }
        if sha256_file(generation / "report.json") != report_hash:
            raise ValueError("candidate report changed during publication")
        private_json(root / POINTER, pointer)
        return {"changed": True, **pointer}
