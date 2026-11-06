"""Manifest-owned, recoverable writes of small generated Zettel notes.

The manifest records file identities and exact bytes, including a pending write's
old and new hashes. It never authorizes deletion or adoption of an unowned file.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

from .corpus_store import require_git_private
from .library_export import _absolute, _no_symlinks

NOTES_FOLDER = "notes/zettel"
MANIFEST_NAME = "cce-zettel-notes-manifest.json"
MANIFEST_SCHEMA = "cce.zettel-note-manifest.v1"
LOCK_NAME = ".cce-zettel-notes.lock"
RECOVERY_FOLDER = ".cce-zettel-recovery"
RECOVERY_SCHEMA = "cce.zettel-note-recovery.v1"
RECOVERY_SLOTS = 1000
RECOVERY_BYTES = 64 * 1024 * 1024
RECOVERY_RECEIPT_BYTES = 4096
FILENAME_BYTES = 255
MANIFEST_BYTES = 1024 * 1024
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"\d{8}-[0-9a-f]{6}\Z")


def folded(name: str) -> str:
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", name).casefold())


def authorize_destination(destination: Path) -> Path:
    """Authorize the receipt, notes and retained recovery surfaces before reading text."""
    root = _absolute(destination)
    folder = root / NOTES_FOLDER
    _no_symlinks(folder)
    require_git_private(root)
    require_git_private(folder)
    recovery = root / RECOVERY_FOLDER
    _no_symlinks(recovery)
    require_git_private(recovery)
    return root


def _digest(path: Path) -> str | None:
    if path.is_symlink():
        raise ValueError(f"{path.name} is a symlink; refusing to write through it")
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError(f"{path.name} is not a regular file")
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError(f"{path.name} is not a regular file")
        consumed = 0
        while block := source.read(min(65536, RECOVERY_BYTES - consumed + 1)):
            consumed += len(block)
            if consumed > RECOVERY_BYTES:
                raise ValueError(f"{path.name} exceeds the Zettel recovery byte cap; preserving it")
            digest.update(block)
    return digest.hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key in Zettel ownership manifest")
        result[key] = value
    return result


def _read_manifest(root: Path) -> dict[str, Any]:
    path = root / MANIFEST_NAME
    _no_symlinks(path)
    if not path.exists():
        return {"schema": MANIFEST_SCHEMA, "state": "complete", "files": {}}
    if not path.is_file() or path.stat().st_size > MANIFEST_BYTES:
        raise ValueError("invalid or oversized Zettel ownership manifest")
    data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    if (
        not isinstance(data, dict)
        or data.get("schema") != MANIFEST_SCHEMA
        or data.get("state") not in ("writing", "complete")
        or not isinstance(data.get("files"), dict)
    ):
        raise ValueError("unsupported Zettel ownership manifest")
    identities: set[str] = set()
    names: set[str] = set()
    for name, entry in data["files"].items():
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or not name.endswith(".md")
            or len(name.encode("utf-8")) > FILENAME_BYTES
            or any(char in name for char in ("\\", "\x00", "\n", "\r"))
            or not isinstance(entry, dict)
            or not isinstance(entry.get("id"), str)
            or not _ID.fullmatch(entry["id"])
            or not isinstance(entry.get("sha256"), str)
            or not _DIGEST.fullmatch(entry["sha256"])
        ):
            raise ValueError("invalid file entry in Zettel ownership manifest")
        previous = entry.get("previous_sha256")
        if previous is not None and (
            data["state"] != "writing"
            or not isinstance(previous, str)
            or not _DIGEST.fullmatch(previous)
        ):
            raise ValueError("invalid previous hash in Zettel ownership manifest")
        if entry["id"] in identities or folded(name) in names:
            raise ValueError("duplicate identity or filename in Zettel ownership manifest")
        identities.add(entry["id"])
        names.add(folded(name))
    return data


def _plan(
    root: Path, planned: dict[str, dict[str, Any]], max_entries: int
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str | None], int]:
    folder = root / NOTES_FOLDER
    _no_symlinks(folder)
    previous = _read_manifest(root)
    entries = {path.name: path for path in folder.iterdir()} if folder.is_dir() else {}
    count = len(set(entries) | set(planned))
    if count > max_entries:
        raise ValueError(
            f"{count} retained and incoming entries exceed the {max_entries} folder cap"
        )
    by_id = {entry["id"]: name for name, entry in previous["files"].items()}
    owners = {name: dict(entry) for name, entry in previous["files"].items()}
    if previous["state"] == "writing":
        # A new batch can omit an interrupted update. Retain whichever previously
        # recorded bytes are actually on disk instead of discarding their ownership.
        for name, entry in owners.items():
            if name in planned:
                continue
            actual = _digest(folder / name)
            if actual is not None:
                if actual not in {entry["sha256"], entry.get("previous_sha256")}:
                    raise ValueError(f"{name} was edited during an interrupted export")
                entry["sha256"] = actual
            entry.pop("previous_sha256", None)
    observed: dict[str, str | None] = {}
    for name, entry in planned.items():
        if by_id.get(entry["id"]) not in (None, name):
            raise ValueError(
                f"{entry['id']}: already written as {by_id[entry['id']]}; keep its title"
            )
        reserved = set(entries) | set(previous["files"]) | set(planned)
        collisions = sorted(
            other for other in reserved if folded(other) == folded(name) and other != name
        )
        if collisions:
            raise ValueError(f"{name}: filename conflicts with {collisions[0]}; keep its title")
        found = _digest(folder / name)
        observed[name] = found
        owned = previous["files"].get(name)
        if owned is not None and owned["id"] != entry["id"]:
            raise ValueError(f"{name} already holds a different note")
        if found is not None:
            if owned is None:
                raise ValueError(
                    f"{name} already holds a different note or is not owned by the manifest"
                )
            if found not in {owned["sha256"], owned.get("previous_sha256")}:
                raise ValueError(f"{name} was edited after export; refusing overwrite")
        owners[name] = {key: entry[key] for key in ("id", "sha256")}
        if found is not None and found != entry["sha256"]:
            owners[name]["previous_sha256"] = found
    return previous, owners, observed, count


def _bytes(manifest: dict[str, Any]) -> bytes:
    data = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    if len(data) > MANIFEST_BYTES:
        raise ValueError("Zettel ownership manifest exceeds its byte cap")
    return data


def _atomic_write(path: Path, data: bytes, *, exclusive: bool = False) -> None:
    _no_symlinks(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".cce-zettel-", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            try:
                # Linking the completed temporary file creates the new name atomically
                # and refuses an editor's file that appeared after the ownership check.
                os.link(temporary_path, path, follow_symlinks=False)
            except FileExistsError:
                raise ValueError(
                    f"{path.name} appeared during export; preserving the file"
                ) from None
        else:
            temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _recovery_inventory(root: Path) -> dict[str, int]:
    """Count retained slots and observed bytes without reading or deleting their payloads."""
    recovery = root / RECOVERY_FOLDER
    _no_symlinks(recovery)
    require_git_private(recovery)
    if not recovery.exists():
        return {"retained": 0, "bytes": 0}
    if not recovery.is_dir():
        raise ValueError("Zettel recovery surface is not a directory")
    count = size = 0
    for slot in recovery.iterdir():
        count += 1
        if count > RECOVERY_SLOTS:
            raise ValueError(
                "Zettel recovery slot cap exceeded; retain and review the existing slots"
            )
        _no_symlinks(slot)
        if not slot.is_dir() or not re.fullmatch(r"capture-[a-z0-9_]{8}", slot.name):
            raise ValueError("unrecognized Zettel recovery entry; preserving it")
        require_git_private(slot)
        for path in slot.iterdir():
            _no_symlinks(path)
            if path.name not in {"previous.bin", "receipt.json"} or not path.is_file():
                raise ValueError("unrecognized Zettel recovery payload; preserving it")
            observed = path.stat().st_size
            if path.name == "receipt.json" and observed > RECOVERY_RECEIPT_BYTES:
                raise ValueError("Zettel recovery receipt exceeds its byte cap")
            size += observed
            if size > RECOVERY_BYTES:
                raise ValueError(
                    "Zettel recovery byte cap exceeded; retain and review the existing slots"
                )
    return {"retained": count, "bytes": size}


def _reserve_recovery(root: Path, paths: list[Path]) -> dict[str, int]:
    """Reserve observed payload bytes plus a complete receipt for every update in the batch."""
    inventory = _recovery_inventory(root)
    reserved_bytes = inventory["bytes"]
    for path in paths:
        _no_symlinks(path)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"{path.name} is not a regular file")
        reserved_bytes += info.st_size + RECOVERY_RECEIPT_BYTES
    if inventory["retained"] + len(paths) > RECOVERY_SLOTS or reserved_bytes > RECOVERY_BYTES:
        raise ValueError(
            "Zettel recovery capacity would be exceeded by this batch; "
            "existing notes and recovery slots are preserved"
        )
    return inventory


def _sync_directory(path: Path) -> None:
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW | os.O_NONBLOCK
    )
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise ValueError("Zettel publication directory is no longer a directory")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _restore_captured(captured: Path, target: Path) -> str:
    """Reattach the retained inode only to a free name; never replace an editor's file."""
    try:
        os.link(captured, target, follow_symlinks=False)
    except FileExistsError:
        return "the current note was preserved"
    except OSError:
        return "the prior file remains in recovery"
    try:
        _sync_directory(target.parent)
    except (OSError, ValueError):
        return "the prior file was restored but directory sync failed"
    return "the prior file was restored"


def _update_owned(root: Path, path: Path, entry: dict[str, Any], expected: str) -> str:
    """Capture the displaced inode, then publish exclusively; keep the inode indefinitely.

    An editor can keep writing through a descriptor opened before the capture. Its
    inode remains reachable in recovery even after a successful replacement. A
    receipt hash is only an observation, never authority to discard that inode.
    """
    recovery = root / RECOVERY_FOLDER
    _no_symlinks(recovery)
    require_git_private(recovery)
    recovery.mkdir(mode=0o700, exist_ok=True)
    _sync_directory(root)
    slot = Path(tempfile.mkdtemp(prefix="capture-", dir=recovery))
    _sync_directory(recovery)
    captured = slot / "previous.bin"
    relative = captured.relative_to(root).as_posix()
    receipt = {
        "schema": RECOVERY_SCHEMA,
        "filename": path.name,
        "id": entry["id"],
        "previous_sha256": expected,
        "proposed_sha256": entry["sha256"],
        "captured_sha256": None,
    }
    _atomic_write(slot / "receipt.json", _bytes(receipt), exclusive=True)
    _sync_directory(slot)
    displaced = False
    try:
        # The destination is inside an exclusively created private slot. Moving
        # captures whichever inode is at the note path, including a raced edit.
        path.rename(captured)
        displaced = True
        _sync_directory(slot)
        _sync_directory(path.parent)
        receipt["captured_sha256"] = _digest(captured)
        _atomic_write(slot / "receipt.json", _bytes(receipt))
        _sync_directory(slot)
        if receipt["captured_sha256"] != expected:
            raise ValueError("captured note changed during export")
        _atomic_write(path, entry["data"], exclusive=True)
        _sync_directory(path.parent)
    except BaseException as error:
        # A signal or late filesystem error can arrive after rename landed but
        # before Python records the return. The unique retained path is evidence.
        captured_exists = displaced or captured.exists() or captured.is_symlink()
        outcome = (
            _restore_captured(captured, path) if captured_exists else "the note was not captured"
        )
        message = f"{path.name}: recovery at {relative}; {outcome}"
        if isinstance(error, (ValueError, OSError)):
            raise ValueError(f"{message}; {error}") from error
        error.add_note(message)
        raise
    return relative


def write_owned_notes(
    destination: Path, planned: dict[str, dict[str, Any]], *, max_entries: int
) -> dict[str, Any]:
    """Preflight the whole batch, retain displaced inodes, then publish new names exclusively.

    A pending manifest permits an interrupted write to resume from either recorded hash.
    Files absent from the current batch are retained; no output is deleted.
    """
    root = authorize_destination(destination)
    _previous, _owners, initial, _count = _plan(root, planned, max_entries)
    _reserve_recovery(
        root,
        [
            root / NOTES_FOLDER / name
            for name in planned
            if initial[name] is not None and initial[name] != planned[name]["sha256"]
        ],
    )  # ordinary refusal does not create any output
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = root / LOCK_NAME
    _no_symlinks(lock_path)
    try:
        descriptor = os.open(
            lock_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
        )
    except OSError as error:
        raise ValueError("cannot safely open the Zettel export lock") from error
    with os.fdopen(descriptor, "wb") as lock:
        lock_stat = os.fstat(lock.fileno())
        if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_size:
            raise ValueError("Zettel export lock is not an empty regular file")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("another Zettel export owns this destination") from None
        authorize_destination(root)
        previous, owners, observed, count = _plan(root, planned, max_entries)
        complete = {
            "schema": MANIFEST_SCHEMA,
            "state": "complete",
            "files": {
                name: {key: value for key, value in entry.items() if key != "previous_sha256"}
                for name, entry in owners.items()
            },
        }
        final_bytes = _bytes(complete)
        changed = [name for name in planned if observed[name] != planned[name]["sha256"]]
        retained = _reserve_recovery(
            root, [root / NOTES_FOLDER / name for name in changed if observed[name] is not None]
        )
        captured = []
        if changed:
            pending = {"schema": MANIFEST_SCHEMA, "state": "writing", "files": owners}
            _atomic_write(root / MANIFEST_NAME, _bytes(pending))
            folder = root / NOTES_FOLDER
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            for name in changed:
                previous_digest = observed[name]
                if _digest(folder / name) != previous_digest:
                    raise ValueError(f"{name} changed during export; refusing overwrite")
                if previous_digest is None:
                    _atomic_write(folder / name, planned[name]["data"], exclusive=True)
                else:
                    captured.append(
                        _update_owned(root, folder / name, planned[name], previous_digest)
                    )
        if previous != complete or changed or not (root / MANIFEST_NAME).exists():
            _atomic_write(root / MANIFEST_NAME, final_bytes)
        retained = _recovery_inventory(root) if captured else retained
        return {
            "written": len(changed),
            "unchanged": len(planned) - len(changed),
            "entries": count,
            "recovery": {
                "directory": RECOVERY_FOLDER,
                "created": captured,
                **retained,
                "slot_limit": RECOVERY_SLOTS,
                "byte_limit": RECOVERY_BYTES,
            },
        }
