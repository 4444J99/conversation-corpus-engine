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
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .corpus_store import require_git_private
from .library_paths import _absolute, _no_symlinks

NOTES_FOLDER = "notes/zettel"
MANIFEST_NAME = "cce-zettel-notes-manifest.json"
MANIFEST_SCHEMA = "cce.zettel-note-manifest.v1"
CREATION_JOURNAL = "exclusive-create.v1"
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


def _valid_filename(name: Any) -> bool:
    return (
        isinstance(name, str)
        and Path(name).name == name
        and name.endswith(".md")
        and len(name.encode("utf-8")) <= FILENAME_BYTES
        and not any(char in name for char in ("\\", "\x00", "\n", "\r"))
    )


def _valid_digest(value: Any) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _validate_planned(planned: dict[str, dict[str, Any]]) -> None:
    identities: set[str] = set()
    callers: set[str] = set()
    for name, entry in planned.items():
        if not _valid_filename(name):
            raise ValueError("invalid incoming filename for Zettel ownership")
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("id"), str)
            or not _ID.fullmatch(entry["id"])
            or not _valid_digest(entry.get("caller_digest"))
            or not _valid_digest(entry.get("sha256"))
            or not isinstance(entry.get("data"), bytes)
            or hashlib.sha256(entry["data"]).hexdigest() != entry["sha256"]
        ):
            raise ValueError("invalid incoming Zettel identity or bytes")
        if entry["id"] in identities or entry["caller_digest"] in callers:
            raise ValueError("duplicate incoming Zettel identity")
        identities.add(entry["id"])
        callers.add(entry["caller_digest"])


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
    if data["state"] == "writing" and data.get("creation_journal") != CREATION_JOURNAL:
        raise ValueError("interrupted Zettel manifest has no exclusive-creation proof")
    identities: set[str] = set()
    callers: set[str] = set()
    names: set[str] = set()
    for name, entry in data["files"].items():
        if (
            not _valid_filename(name)
            or not isinstance(entry, dict)
            or not isinstance(entry.get("id"), str)
            or not _ID.fullmatch(entry["id"])
            or not _valid_digest(entry.get("sha256"))
            or ("caller_digest" in entry and not _valid_digest(entry["caller_digest"]))
        ):
            raise ValueError("invalid file entry in Zettel ownership manifest")
        previous = entry.get("previous_sha256")
        if previous is not None and (
            data["state"] != "writing"
            or not isinstance(previous, str)
            or not _DIGEST.fullmatch(previous)
        ):
            raise ValueError("invalid previous hash in Zettel ownership manifest")
        if "creation_pending" in entry and (
            data["state"] != "writing" or entry["creation_pending"] is not True
        ):
            raise ValueError("invalid creation state in Zettel ownership manifest")
        created = entry.get("created_inode")
        if created is not None and (
            not entry.get("creation_pending")
            or not isinstance(created, dict)
            or set(created) != {"device", "inode"}
            or any(type(value) is not int or not 0 <= value < 2**128 for value in created.values())
        ):
            raise ValueError("invalid creation proof in Zettel ownership manifest")
        caller = entry.get("caller_digest")
        if (
            entry["id"] in identities
            or folded(name) in names
            or caller is not None
            and caller in callers
        ):
            raise ValueError("duplicate identity or filename in Zettel ownership manifest")
        identities.add(entry["id"])
        if caller is not None:
            callers.add(caller)
        names.add(folded(name))
    return data


def _check_recorded_file(path: Path, found: str, owned: dict[str, Any]) -> None:
    if owned.get("creation_pending") and found != owned.get("previous_sha256"):
        # A planned hash is not evidence of ownership: a foreign file can have
        # identical bytes. Only a successfully journaled exclusive creation can
        # establish the new inode during an interrupted batch.
        info = path.stat(follow_symlinks=False)
        created = {"device": info.st_dev, "inode": info.st_ino}
        if not stat.S_ISREG(info.st_mode) or owned.get("created_inode") != created:
            raise ValueError(
                f"{path.name}: missing or mismatched creation proof; preserving the file"
            )
    if found not in {owned["sha256"], owned.get("previous_sha256")}:
        raise ValueError(f"{path.name} was edited after export; refusing overwrite")


def _plan(
    root: Path, planned: dict[str, dict[str, Any]], max_entries: int
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str | None], int]:
    folder = root / NOTES_FOLDER
    _no_symlinks(folder)
    previous = _read_manifest(root)
    entries = {path.name: path for path in folder.iterdir()} if folder.is_dir() else {}
    count = len(set(entries) | set(previous["files"]) | set(planned))
    if count > max_entries:
        raise ValueError(
            f"{count} retained and incoming entries exceed the {max_entries} folder cap"
        )
    by_id = {entry["id"]: name for name, entry in previous["files"].items()}
    by_caller = {
        entry["caller_digest"]: name
        for name, entry in previous["files"].items()
        if "caller_digest" in entry
    }
    owners = {name: dict(entry) for name, entry in previous["files"].items()}
    if previous["state"] == "writing":
        # A new batch can omit an interrupted update. Retain whichever previously
        # recorded bytes are actually on disk instead of discarding their ownership.
        for name, entry in owners.items():
            if name in planned:
                continue
            actual = _digest(folder / name)
            if actual is not None:
                _check_recorded_file(folder / name, actual, entry)
                entry["sha256"] = actual
                for field in ("previous_sha256", "creation_pending", "created_inode"):
                    entry.pop(field, None)
            # An absent, never-created name stays a pending reservation. Completing
            # a different batch cannot turn that reservation into file ownership.
    observed: dict[str, str | None] = {}
    for name, entry in planned.items():
        if by_id.get(entry["id"]) not in (None, name):
            raise ValueError(
                f"{entry['id']}: already written as {by_id[entry['id']]}; keep its title"
            )
        if by_caller.get(entry["caller_digest"]) not in (None, name):
            raise ValueError(
                f"{name}: caller identity already has a different filename; keep its title"
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
        if owned is not None and owned.get("caller_digest") != entry["caller_digest"]:
            raise ValueError(f"{name}: caller identity differs or is absent from legacy ownership")
        if found is not None:
            if owned is None:
                raise ValueError(
                    f"{name} already holds a different note or is not owned by the manifest"
                )
            _check_recorded_file(folder / name, found, owned)
        owners[name] = {key: entry[key] for key in ("id", "caller_digest", "sha256")}
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


def _atomic_write(
    path: Path,
    data: bytes,
    *,
    exclusive: bool = False,
    on_created: Callable[[dict[str, int]], None] | None = None,
) -> None:
    _no_symlinks(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".cce-zettel-", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            info = os.fstat(stream.fileno())
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
        _sync_directory(path.parent)
        if exclusive and on_created is not None:
            on_created({"device": info.st_dev, "inode": info.st_ino})
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


def _make_directory(path: Path) -> None:
    """Create the requested chain and sync each new name's containing directory."""
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    # A prior attempt may have created this ancestor but failed its parent sync.
    # Repair that boundary before treating the existing name as established.
    _sync_directory(current.parent)
    for directory in reversed(missing):
        _no_symlinks(directory)
        directory.mkdir(mode=0o700, exist_ok=True)
        _sync_directory(directory.parent)


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


def _update_owned(
    root: Path,
    path: Path,
    entry: dict[str, Any],
    expected: str,
    on_created: Callable[[dict[str, int]], None],
) -> str:
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
        "caller_digest": entry["caller_digest"],
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
        _atomic_write(path, entry["data"], exclusive=True, on_created=on_created)
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

    Pending updates retain the old hash; new inodes require exclusive-creation proof.
    Files absent from the current batch are retained; no output is deleted.
    """
    _validate_planned(planned)
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
    _make_directory(root)
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
        complete_files = {
            name: (
                {key: entry[key] for key in ("id", "caller_digest", "sha256")}
                if name in planned
                else dict(entry)
            )
            for name, entry in owners.items()
        }
        still_pending = any(entry.get("creation_pending") for entry in complete_files.values())
        complete = {
            "schema": MANIFEST_SCHEMA,
            "state": "writing" if still_pending else "complete",
            "files": complete_files,
        }
        if still_pending:
            complete["creation_journal"] = CREATION_JOURNAL
        final_bytes = _bytes(complete)
        changed = [name for name in planned if observed[name] != planned[name]["sha256"]]
        retained = _reserve_recovery(
            root, [root / NOTES_FOLDER / name for name in changed if observed[name] is not None]
        )
        captured = []
        if changed:
            for name in changed:
                owners[name]["creation_pending"] = True
            pending = {
                "schema": MANIFEST_SCHEMA,
                "state": "writing",
                "creation_journal": CREATION_JOURNAL,
                "files": owners,
            }
            _atomic_write(root / MANIFEST_NAME, _bytes(pending))
            folder = root / NOTES_FOLDER
            _make_directory(folder)

            def record_creation(identity: dict[str, int]) -> None:
                owners[name]["created_inode"] = identity
                _atomic_write(root / MANIFEST_NAME, _bytes(pending))

            for name in changed:
                previous_digest = observed[name]
                if _digest(folder / name) != previous_digest:
                    raise ValueError(f"{name} changed during export; refusing overwrite")
                if previous_digest is None:
                    _atomic_write(
                        folder / name,
                        planned[name]["data"],
                        exclusive=True,
                        on_created=record_creation,
                    )
                else:
                    captured.append(
                        _update_owned(
                            root, folder / name, planned[name], previous_digest, record_creation
                        )
                    )
        if previous != complete or changed or not (root / MANIFEST_NAME).exists():
            _atomic_write(root / MANIFEST_NAME, final_bytes)
        retained = _recovery_inventory(root) if captured else retained
        # A previous final-manifest replace can be visible even if its directory
        # sync failed. An unchanged retry must finish that step before success.
        _sync_directory(root)
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
