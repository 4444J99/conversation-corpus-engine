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
FILENAME_BYTES = 255
MANIFEST_BYTES = 1024 * 1024
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"\d{8}-[0-9a-f]{6}\Z")


def folded(name: str) -> str:
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", name).casefold())


def authorize_destination(destination: Path) -> Path:
    """Check both the receipt root and the actual note folder before reading text."""
    root = _absolute(destination)
    folder = root / NOTES_FOLDER
    _no_symlinks(folder)
    require_git_private(root)
    require_git_private(folder)
    return root


def _digest(path: Path) -> str | None:
    if path.is_symlink():
        raise ValueError(f"{path.name} is a symlink; refusing to write through it")
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError(f"{path.name} is not a regular file")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(65536), b""):
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
        collisions = [other for other in entries if folded(other) == folded(name) and other != name]
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


def write_owned_notes(
    destination: Path, planned: dict[str, dict[str, Any]], *, max_entries: int
) -> dict[str, int]:
    """Preflight every target, record ownership, then atomically replace only owned bytes.

    A pending manifest permits an interrupted write to resume from either recorded hash.
    Files absent from the current batch are retained; no output is deleted.
    """
    root = authorize_destination(destination)
    _plan(root, planned, max_entries)  # ordinary refusal does not create any output
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
        if changed:
            pending = {"schema": MANIFEST_SCHEMA, "state": "writing", "files": owners}
            _atomic_write(root / MANIFEST_NAME, _bytes(pending))
            folder = root / NOTES_FOLDER
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            for name in changed:
                if _digest(folder / name) != observed[name]:
                    raise ValueError(f"{name} changed during export; refusing overwrite")
                _atomic_write(
                    folder / name, planned[name]["data"], exclusive=observed[name] is None
                )
        if previous != complete or changed or not (root / MANIFEST_NAME).exists():
            _atomic_write(root / MANIFEST_NAME, final_bytes)
        return {"written": len(changed), "unchanged": len(planned) - len(changed), "entries": count}
