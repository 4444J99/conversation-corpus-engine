"""A stand-alone card vault for a reading app, written on demand from a library generation.

``cce library export-cards --out DIR`` makes DIR a small, capped Obsidian-style vault: one card per
record under the note budget (see ``library_vault``). It reads the library and never writes inside
the library store. DIR is the vault folder itself, so it also holds whatever the reading app and
the person add; the exporter therefore touches only what it can prove it wrote:

* an empty DIR is accepted, a non-empty one only when it carries the marker file of an earlier run;
* every run writes a manifest of exactly the files it generated, with their sizes and digests;
* a later run replaces and deletes only files that earlier manifest lists, and refuses, before it
  changes anything, when one of them was edited or when an unlisted file already sits where a card
  would go (an identical file is adopted, which makes an interrupted run safe to repeat);
* the cards, hubs and Home it would write are measured against every hard cap in planning, and a
  breach refuses before any file is written, deleted or listed. What the person keeps in the
  folder is theirs: if it alone pushes the folder past a cap, the run completes and says so (a
  warning on stderr and in the result) instead of failing after the manifest is written.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .library_export import _absolute, _no_symlinks, _safe_relative
from .library_note_text import WINGS, check, safe_uid
from .library_store import Library
from .library_vault import PROFILE_CARDS, plan_vault
from .library_vault_gate import (
    DEFAULT_CAPS,
    VaultCaps,
    describe_findings,
    hard_cap_problems,
    measure_vault,
)

CARDS_SCHEMA = "cce.library-cards.v1"
CARDS_MARKER = ".cce-cards-vault"
CARDS_MANIFEST = ".cce-cards-manifest.json"
_REFUSE_STORE = (
    "refusing to write a card vault inside or around the library store; "
    "choose a separate --out directory"
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str:
    return _sha256(path.read_bytes())


def _refuse_library_store(out: Path, store_root: Path | None) -> None:
    for ancestor in (out, *out.parents):
        if ancestor.parent.name == "generations" and ancestor.parent.parent.name == "library-all":
            raise ValueError(_REFUSE_STORE)
    if store_root is not None:
        store = Path(os.path.realpath(store_root))
        target = Path(os.path.realpath(out))
        if target == store or store in target.parents or target in store.parents:
            raise ValueError(_REFUSE_STORE)


def _previous_run(out: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """The files and folders an earlier run listed ({} when there is nothing known to be ours)."""
    if not out.exists():
        return {}, []
    if not out.is_dir():
        raise FileExistsError(out)
    if not any(out.iterdir()):
        return {}, []
    marker = out / CARDS_MARKER
    if marker.is_symlink() or not marker.is_file():
        raise FileExistsError(
            f"refusing to write into a non-empty directory without the {CARDS_MARKER} marker "
            f"file of a card vault: {out}"
        )
    manifest_path = out / CARDS_MANIFEST
    if not manifest_path.exists():
        return {}, []  # an interrupted first run: no file is known to be ours yet
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError(f"card manifest is not a regular file: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("schema") != CARDS_SCHEMA:
            raise ValueError("unknown card manifest schema")
        files: dict[str, dict[str, Any]] = {}
        for entry in manifest["files"]:
            path = _safe_relative(entry["path"]).as_posix()
            if path in files or not isinstance(entry["sha256"], str):
                raise ValueError("duplicate or malformed manifest entry")
            files[path] = entry
        directories = [_safe_relative(path).as_posix() for path in manifest["directories"]]
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"card manifest is invalid: {error}") from error
    return files, directories


def _rows(library: Library) -> list[dict[str, Any]]:
    rows = []
    for row in library.rows():
        safe_uid(row["uid"])
        if row["wing"] not in WINGS:
            raise ValueError("unknown library wing")
        rows.append(row)
    return rows


def _private_write(target: Path, data: bytes, *, replace: bool) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    path = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp") if replace else target
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    if replace:
        path.replace(target)


def is_card_folder(directory: Path) -> bool:
    """Whether `directory` is what `export_cards` makes (its marker or its manifest is there) and
    not a library export, which has an `export-manifest.json` of its own."""
    directory = Path(directory)
    return not os.path.lexists(directory / "export-manifest.json") and any(
        os.path.lexists(directory / name) for name in (CARDS_MARKER, CARDS_MANIFEST)
    )


def verify_cards(directory: Path, *, caps: VaultCaps | None = None) -> dict[str, Any]:
    """Check a card folder against the manifest of the run that wrote it.

    Every file the manifest lists must be there as a regular file of the listed size and SHA-256
    (a listed file that differs was edited since the export, which the next run would refuse to
    replace). Files the manifest does not list are the person's own and are only counted. The
    whole folder, theirs included, is then measured against the hard caps of the vault standard,
    as `measure_vault` does after an export, so a folder that outgrew them reports it here.
    Returns the problems (empty means valid) with the number of cards listed and of files not.
    """
    caps = caps or DEFAULT_CAPS
    directory = _absolute(Path(directory))
    problems: list[str] = []
    report: dict[str, Any] = {"problems": problems, "cards": 0, "unlisted": 0}
    try:
        _no_symlinks(directory)
    except ValueError as error:
        problems.append(str(error))
        return report
    marker, manifest_path = directory / CARDS_MARKER, directory / CARDS_MANIFEST
    if marker.is_symlink() or not marker.is_file():
        problems.append(f"card vault marker missing: {CARDS_MARKER}")
        return report
    if manifest_path.is_symlink() or not manifest_path.is_file():
        problems.append(f"card manifest missing: {CARDS_MANIFEST}")
        return report
    try:
        files, _ = _previous_run(directory)
    except (OSError, ValueError) as error:
        problems.append(str(error))
        return report
    report["cards"] = len(files)
    for path in sorted(files):
        target = directory / path
        if not os.path.lexists(target):
            problems.append(f"missing card file: {path}")
        elif target.is_symlink() or not target.is_file():
            problems.append(f"card file is not a regular file: {path}")
        else:
            data = target.read_bytes()
            entry = files[path]
            if _sha256(data) != entry["sha256"] or len(data) != entry.get("bytes", len(data)):
                problems.append(f"edited since export: {path}")
    listed = set(files) | {CARDS_MARKER, CARDS_MANIFEST}
    for item in directory.rglob("*"):
        if (
            item.is_file()
            and not item.is_symlink()
            and item.relative_to(directory).as_posix() not in listed
        ):
            report["unlisted"] += 1
    try:
        problems.extend(
            f"vault {line}" for line in hard_cap_problems(measure_vault(directory, caps))
        )
    except OSError as error:
        problems.append(f"vault unreadable: {error}")
    return report


def export_cards(
    library: Library,
    out: Path,
    *,
    store_root: Path | None = None,
    limit_notes: int | None = None,
    caps: VaultCaps | None = None,
    check_callback: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Write (or refresh) the card vault in ``out`` and return what was done."""
    caps = caps or DEFAULT_CAPS
    out = _absolute(Path(out))
    _no_symlinks(out)
    _refuse_library_store(out, store_root)
    previous, previous_directories = _previous_run(out)
    plan = plan_vault(
        PROFILE_CARDS,
        library,
        _rows(library),
        prefix="",
        caps=caps,
        limit_notes=limit_notes,
        check_callback=check_callback,
    )
    planned: dict[str, bytes] = {}
    plan.write(lambda path, text: planned.update({path: text.encode("utf-8")}))
    digests = {path: _sha256(data) for path, data in planned.items()}

    # Decide everything before touching anything.
    edited: list[str] = []
    foreign: list[str] = []
    for path, digest in digests.items():
        target = out / path
        _no_symlinks(target)
        if not os.path.lexists(target):
            continue
        if not target.is_file():
            foreign.append(path)
        elif _file_sha256(target) in {digest, previous.get(path, {}).get("sha256")}:
            continue
        else:
            (edited if path in previous else foreign).append(path)
    stale = sorted(set(previous) - set(planned))
    for path in stale:
        target = out / path
        _no_symlinks(target)
        if os.path.lexists(target) and (
            not target.is_file() or _file_sha256(target) != previous[path]["sha256"]
        ):
            edited.append(path)
    if edited:
        raise ValueError(
            f"{len(edited)} generated file(s) were edited since the last export and would be "
            f"replaced or deleted (for example {sorted(edited)[0]}); move or restore them first"
        )
    if foreign:
        raise FileExistsError(
            f"{len(foreign)} file(s) already exist where a card would be written and were not "
            f"written by this tool (for example {sorted(foreign)[0]}); nothing was changed"
        )

    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = out / CARDS_MARKER
    if not marker.exists():
        _private_write(marker, (CARDS_SCHEMA + "\n").encode("utf-8"), replace=False)
    written = adopted = 0
    for path in sorted(planned):
        check(check_callback, bytes_written=len(planned[path]))
        target = out / path
        exists = os.path.lexists(target)
        if exists and _file_sha256(target) == digests[path]:
            adopted += 1
            continue
        _private_write(target, planned[path], replace=exists)
        written += 1
    removed = 0
    for path in stale:
        target = out / path
        if os.path.lexists(target):
            target.unlink()
            removed += 1
    directories = sorted(
        {parent.as_posix() for path in planned for parent in Path(path).parents} - {"."}
    )
    for directory in sorted(set(previous_directories) - set(directories), key=len, reverse=True):
        try:
            (out / directory).rmdir()
        except OSError:
            continue  # not empty (something of the person's is in it) or already gone
    manifest = {
        "schema": CARDS_SCHEMA,
        "records": plan.records,
        "cards": plan.cards,
        "omitted": plan.omitted,
        "truncated": plan.truncated,
        "limit_notes": plan.limit_notes,
        "files": [
            {"path": path, "bytes": len(planned[path]), "sha256": digests[path]}
            for path in sorted(planned)
        ],
        "directories": directories,
    }
    _private_write(
        out / CARDS_MANIFEST,
        (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
        replace=(out / CARDS_MANIFEST).exists(),
    )
    # The manifest above describes a vault whose generated notes already passed the caps in
    # planning. Whatever else the folder holds is the person's; if it crowds the vault past a cap
    # that is reported, never raised after the writing is done.
    gate = measure_vault(out, caps)
    warnings = []
    if gate["findings"]:
        warnings.append(
            "the card folder as a whole is outside the vault caps "
            f"({describe_findings(gate)}); the cards written by this tool fit them, so files of "
            "your own in the folder account for the rest"
        )
        print(f"library: warning: {warnings[0]}", file=sys.stderr)
    return {
        "out": str(out),
        "records": plan.records,
        "cards": plan.cards,
        "omitted": plan.omitted,
        "truncated": plan.truncated,
        "limit_notes": plan.limit_notes,
        "notes": len(planned),
        "written": written,
        "adopted": adopted,
        "removed": removed,
        "manifest": CARDS_MANIFEST,
        "generated_caps": plan.gate,
        "vault_caps": gate,
        "warnings": warnings,
    }
