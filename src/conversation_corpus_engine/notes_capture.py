"""Apple Notes and Google Drive documents: a read-only capture into notes a documents root reads.

This is the capture home for the two sources the library reads as plain documents. It reads local
application data and nothing else; it never opens an application, never runs a script against
one, never signs in anywhere and never writes to what it reads.

Apple Notes. The Notes database (`NoteStore.sqlite`, with its write-ahead log and shared-memory
file) is copied byte for byte into a private temporary directory, the copy is opened read only,
and the copy is deleted afterwards. The text of a note is stored as a gzip-compressed protocol
buffer; it is decoded here with a small bounded reader, not an application. One note becomes one
file `notes/<xx>/<identifier>.md` whose bytes are the note's own text and nothing else (no header,
no date, no folder), so the authorship rules read only what the note says. What is not the body
(title, folder path, dates, attachment count, a flag for text that looks pasted) goes to a
sidecar `notes-index.jsonl` that no documents root reads. The file name is the note's own stable
identifier, so a refresh rewrites the same file and an edited note is a new revision of one
record, never a second record.

Left out and counted, never silent: notes in Recently Deleted, notes the owner locked with a
password (their body is encrypted and is not read), and notes with no readable body.

Authorship. The text of an Apple Note is mostly the owner's own writing, with some pasted chat or
agent text; the note files are therefore declared in the `authored` wing, `writing` lane, where
the authorship rules keep their markers and give no automatic credit to text that carries an AI
mark. The sidecar flags a note whose text looks pasted from a chat so a reviewer can find it;
the flag is a hint, not a verdict, and never changes a note's bytes.

Google Drive documents. A Google Doc, Sheet or Slides file in the Drive for desktop folder is a
small shortcut file that holds an id and not the document's text; the text lives on Google's
servers and is not read here. Each shortcut becomes a metadata-only note (title, kind, address,
and the folder it sits in), declared in the `ai` wing, `support` lane because the note is a
generated layout. The ordinary files in the same Drive folder (Markdown, text, PDF, Word) are read
by the existing documents reader through a manifest entry this receipt supplies.

A refresh never deletes. A note whose source left the store stays on disk and is counted as
retained, because removing a record's source is a decision the owner makes.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import tempfile
import zlib
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .corpus_store import require_git_private
from .library_paths import _absolute, _no_symlinks

SCHEMA = "cce.notes-docs-capture.v1"
RECEIPT_NAME = "notes-docs-capture-receipt.json"
INDEX_NAME = "notes-index.jsonl"
GDOCS_FOLDER = "gdocs"
GDOCS_INDEX_NAME = "gdocs-index.jsonl"
MAX_GDOCS = 200_000
GDOC_SUFFIXES = (".md", ".txt")
MAX_ID_LIST_BYTES = 64 * 1024 * 1024
MAX_INDEX_BYTES = 64 * 1024 * 1024
_DRIVE_ID = re.compile(r"[A-Za-z0-9_-]{10,200}")
NOTES_FOLDER = "notes"
DOCS_FOLDER = "docs"
DEFAULT_NOTES_STORE = (
    Path.home() / "Library" / "Group Containers" / "group.com.apple.notes" / "NoteStore.sqlite"
)
MAX_NOTE_BYTES = 8 * 1024 * 1024
MAX_COMPRESSED_BYTES = 8 * 1024 * 1024
MAX_PROTO_DEPTH = 4
MAX_FINDINGS = 1000
MAX_DRIVE_ENTRIES = 2_000_000
MAX_STUB_BYTES = 64 * 1024
MAX_FOLDER_DEPTH = 32
COPY_ATTEMPTS = 3
# Core Data counts seconds from 2001-01-01 UTC.
_CORE_DATA_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
_ATTACHMENT_MARK = "￼"
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACE = re.compile(r"\s+")
_DOC_ID = re.compile(r"[A-Za-z0-9_-]{10,200}")
_IDENTIFIER = re.compile(r"[A-Za-z0-9-]{8,64}")
# Text that reads like a pasted chat or agent turn. A hint for a reviewer, never a verdict.
_PASTED_HINTS = (
    re.compile(
        r"^\s*(?:chatgpt|claude|gemini|copilot|assistant|grok)\s*(?:said)?\s*:", re.I | re.M
    ),
    re.compile(r"^\s*(?:you said|chatgpt said|claude said)\s*:?\s*$", re.I | re.M),
    re.compile(r"\b(?:as an ai (?:language )?model|i'?m sorry, but i can(?:no|')t)\b", re.I),
    re.compile(r"^```", re.M),
)
STUB_KINDS: dict[str, tuple[str, str]] = {
    ".gdoc": ("Google Doc", "document"),
    ".gsheet": ("Google Sheet", "spreadsheets"),
    ".gslides": ("Google Slides", "presentation"),
    ".gdraw": ("Google Drawing", "drawings"),
    ".gform": ("Google Form", "forms"),
}
DRIVE_FILE_GLOBS = ["**/*.md", "**/*.markdown", "**/*.txt", "**/*.pdf", "**/*.docx"]
DRIVE_EXCLUDE_GLOBS = ["**/.tmp.drivedownload/**", "**/.tmp.driveupload/**", "**/.git/**"]


_NOTE_COUNTS = (
    "notes_seen",
    "notes_written",
    "notes_unchanged",
    "note_attachments",
    "notes_flagged_pasted",
    "note_characters",
    "skipped_deleted",
    "skipped_password_protected",
    "skipped_no_body",
    "skipped_empty",
    "skipped_undecodable",
)
_GDOC_COUNTS = (
    "gdocs_seen",
    "gdocs_written",
    "gdocs_unchanged",
    "gdocs_skipped_empty",
    "gdocs_skipped_too_large",
    "gdocs_skipped_not_utf8",
    "gdocs_skipped_symlink",
    "gdocs_skipped_duplicate_path",
    "gdocs_with_drive_id",
    "gdocs_ids_ambiguous",
    "gdocs_ids_refused",
    "gdocs_characters",
)
_DRIVE_COUNTS = (
    "drive_roots_read",
    "drive_shortcuts",
    "drive_shortcuts_refused",
    "drive_documents",
    "drive_other_files",
    "drive_symlinks_skipped",
    "shortcuts_written",
    "shortcuts_unchanged",
)


class CaptureRefused(ValueError):
    """A source that cannot be captured; the message is the reason and never holds its text."""


# ---------------------------------------------------------------------------------------------
# Protocol buffer reading (just enough for an Apple Notes body)
# ---------------------------------------------------------------------------------------------


def _varint(data: bytes, index: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if index >= len(data) or shift > 63:
            raise CaptureRefused("protocol buffer is truncated or malformed")
        byte = data[index]
        index += 1
        result |= (byte & 0x7F) << shift
        if byte < 0x80:
            return result, index
        shift += 7


def _fields(data: bytes) -> Iterator[tuple[int, int, Any]]:
    """(field number, wire type, value) of each field of one message, one at a time."""
    index = 0
    while index < len(data):
        key, index = _varint(data, index)
        number, wire = key >> 3, key & 7
        value: Any
        if wire == 0:
            value, index = _varint(data, index)
        elif wire == 2:
            length, index = _varint(data, index)
            if index + length > len(data):
                raise CaptureRefused("protocol buffer field runs past the end")
            value = data[index : index + length]
            index += length
        elif wire == 1:
            value, index = data[index : index + 8], index + 8
        elif wire == 5:
            value, index = data[index : index + 4], index + 4
        else:
            raise CaptureRefused("protocol buffer has an unsupported wire type")
        yield number, wire, value


def _first_bytes(data: bytes, number: int) -> bytes | None:
    for field_number, wire, value in _fields(data):
        if field_number == number and wire == 2:
            return value
    return None


def decode_note_text(blob: bytes) -> str:
    """The text of a note from its stored (gzip, protocol buffer) body.

    Layout: the outer message's field 2 is the document; its field 3 is the note; the note's
    field 2 is the text. Decompression is bounded, so a hostile body cannot fill memory.
    """
    if len(blob) > MAX_COMPRESSED_BYTES:
        raise CaptureRefused("note body is larger than the size bound")
    try:
        inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
        raw = inflater.decompress(blob, MAX_NOTE_BYTES + 1)
    except zlib.error:
        raise CaptureRefused("note body is not gzip data") from None
    if len(raw) > MAX_NOTE_BYTES:
        raise CaptureRefused("note text is larger than the size bound")
    if not inflater.eof:
        raise CaptureRefused("note body is a truncated gzip stream")
    document = _first_bytes(raw, 2)
    note = _first_bytes(document, 3) if document is not None else None
    text = _first_bytes(note, 2) if note is not None else None
    if text is None:
        raise CaptureRefused("note body holds no text field")
    try:
        return text.decode("utf-8")
    except UnicodeDecodeError:
        raise CaptureRefused("note text is not valid UTF-8") from None


def clean_note_text(text: str) -> tuple[str, int]:
    """The note's text with attachment placeholders removed, and how many were removed.

    Line endings become `\\n`; control characters other than tab and newline are dropped; a single
    final newline ends the file. Nothing else is touched, so the file is the note's own words.
    """
    attachments = text.count(_ATTACHMENT_MARK)
    text = text.replace(_ATTACHMENT_MARK, "").replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL.sub("", text).replace(" ", "\n").replace(" ", "\n\n")
    text = text.strip("\n")
    return (text + "\n" if text.strip() else ""), attachments


def looks_pasted(text: str) -> bool:
    return any(pattern.search(text) for pattern in _PASTED_HINTS)


# ---------------------------------------------------------------------------------------------
# Read-only copy of the Notes database
# ---------------------------------------------------------------------------------------------


def _signature(path: Path) -> tuple[int, int] | None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    return info.st_size, info.st_mtime_ns


def _copy_regular(source: Path, target: Path) -> str:
    """Copy one file through a read-only descriptor; return its SHA-256."""
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    digest = hashlib.sha256()
    with os.fdopen(descriptor, "rb") as reader:
        if not stat.S_ISREG(os.fstat(reader.fileno()).st_mode):
            raise CaptureRefused(f"{source.name} is not a regular file")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        with os.fdopen(os.open(target, flags, 0o600), "wb") as writer:
            while chunk := reader.read(1024 * 1024):
                digest.update(chunk)
                writer.write(chunk)
    return digest.hexdigest()


def copy_notes_store(store: Path, directory: Path) -> dict[str, Any]:
    """Copy the store and its journal files into ``directory``; return what was copied.

    The files are copied one after another while the application may be writing, so the sizes and
    modification times are compared before and after, and the copy repeats (at most three times)
    until they agree. A copy that never settles is a finding, not a silent success.
    """
    names = ("", "-wal", "-shm")
    last: dict[str, Any] = {}
    for attempt in range(1, COPY_ATTEMPTS + 1):
        sources = {name: store.with_name(store.name + name) for name in names}
        before = {name: _signature(path) for name, path in sources.items()}
        if before[""] is None:
            raise CaptureRefused("Notes database file does not exist")
        for name in names:
            (directory / (store.name + name)).unlink(missing_ok=True)
        digests: dict[str, str] = {}
        try:
            for name, path in sources.items():
                if before[name] is not None:
                    digests[name or "database"] = _copy_regular(path, directory / path.name)
        except FileNotFoundError:
            # A journal file was folded into the database (a checkpoint) while it was copied: the
            # set of files changed under the copy, so this attempt is unsettled and repeats.
            last = {"attempts": attempt, "settled": False, "sha256": digests, "bytes": {}}
            continue
        after = {name: _signature(path) for name, path in sources.items()}
        last = {
            "attempts": attempt,
            "settled": before == after,
            "sha256": digests,
            "bytes": {name or "database": (before[name] or (0, 0))[0] for name in names},
        }
        if before == after:
            break
    if "database" not in last.get("sha256", {}):
        raise CaptureRefused("Notes database could not be copied")
    return last


def _core_data_time(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or math.isnan(value):
        return ""
    try:
        return (_CORE_DATA_EPOCH + timedelta(seconds=value)).isoformat()
    except (OverflowError, ValueError):
        return ""


def _clean_label(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    # A lone surrogate cannot be stored: it becomes "?".
    value = value.encode("utf-8", "replace").decode("utf-8")
    return _SPACE.sub(" ", _CONTROL.sub(" ", value)).strip()[:limit]


@dataclass
class _NoteRow:
    identifier: str
    source_id: str
    title: str
    folder: str
    created: str
    modified: str
    pinned: bool
    text: str = ""
    attachments: int = 0
    pasted: bool = False


@dataclass
class _NotesRead:
    notes: list[_NoteRow] = field(default_factory=list)
    counts: Counter[str] = field(default_factory=Counter)
    findings: list[dict[str, Any]] = field(default_factory=list)


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _find(findings: list[dict[str, Any]], code: str, **detail: Any) -> None:
    if len(findings) < MAX_FINDINGS:
        findings.append({"code": code, **detail})


def read_notes(database: Path) -> _NotesRead:
    """Every readable note of a copied Notes database. The copy is opened read only."""
    result = _NotesRead()
    try:
        return _read_notes(database, result)
    except sqlite3.Error:
        raise CaptureRefused("Notes database copy could not be read") from None


def _read_notes(database: Path, result: _NotesRead) -> _NotesRead:
    uri = f"file:{quote(str(database))}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        check = connection.execute("PRAGMA quick_check").fetchone()
        if not check or check[0] != "ok":
            _find(result.findings, "quick_check_failed")
        objects = _columns(connection, "ZICCLOUDSYNCINGOBJECT")
        required = {"Z_PK", "Z_ENT", "ZTITLE1", "ZIDENTIFIER", "ZFOLDER", "ZNOTEDATA"}
        if not required <= objects or not {"ZNOTE", "ZDATA"} <= _columns(connection, "ZICNOTEDATA"):
            raise CaptureRefused("Notes database does not have the expected tables and columns")
        note_entity = connection.execute(
            "SELECT Z_ENT FROM Z_PRIMARYKEY WHERE Z_NAME = 'ICNote'"
        ).fetchone()
        folder_entity = connection.execute(
            "SELECT Z_ENT FROM Z_PRIMARYKEY WHERE Z_NAME = 'ICFolder'"
        ).fetchone()
        if note_entity is None or folder_entity is None:
            raise CaptureRefused("Notes database names no note or folder entity")
        optional = {
            name: (name in objects)
            for name in (
                "ZMARKEDFORDELETION",
                "ZISPASSWORDPROTECTED",
                "ZCREATIONDATE1",
                "ZMODIFICATIONDATE1",
                "ZISPINNED",
                "ZFOLDERTYPE",
                "ZTITLE2",
                "ZPARENT",
            )
        }

        def column(name: str) -> str:
            return name if optional[name] else "NULL"

        folders: dict[int, tuple[str, int | None, int | None]] = {}
        for pk, title, parent, kind in connection.execute(
            f"SELECT Z_PK, {column('ZTITLE2')}, {column('ZPARENT')}, {column('ZFOLDERTYPE')} "
            "FROM ZICCLOUDSYNCINGOBJECT WHERE Z_ENT = ?",
            (folder_entity[0],),
        ):
            folders[pk] = (_clean_label(title, 200), parent, kind)

        def folder_path(pk: Any) -> tuple[str, bool]:
            names: list[str] = []
            trash = False
            seen: set[int] = set()
            while isinstance(pk, int) and pk in folders and pk not in seen:
                seen.add(pk)
                name, parent, kind = folders[pk]
                trash = trash or kind == 1
                if name:
                    names.append(name)
                if len(seen) > MAX_FOLDER_DEPTH:
                    break
                pk = parent
            return " / ".join(reversed(names)), trash

        query = (
            "SELECT o.Z_PK, o.ZIDENTIFIER, o.ZTITLE1, o.ZFOLDER, "
            f"{column('ZMARKEDFORDELETION')}, {column('ZISPASSWORDPROTECTED')}, "
            f"{column('ZCREATIONDATE1')}, {column('ZMODIFICATIONDATE1')}, {column('ZISPINNED')}, "
            "d.ZDATA FROM ZICCLOUDSYNCINGOBJECT o "
            "LEFT JOIN ZICNOTEDATA d ON d.Z_PK = o.ZNOTEDATA WHERE o.Z_ENT = ? ORDER BY o.Z_PK"
        )
        seen_identifiers: set[str] = set()
        for (
            pk,
            identifier,
            title,
            folder,
            deleted,
            locked,
            created,
            modified,
            pinned,
            blob,
        ) in connection.execute(query, (note_entity[0],)):
            result.counts["notes_seen"] += 1
            path, in_trash = folder_path(folder)
            if deleted == 1 or in_trash:
                result.counts["skipped_deleted"] += 1
                continue
            if locked == 1:
                result.counts["skipped_password_protected"] += 1
                continue
            if not isinstance(identifier, str) or not _IDENTIFIER.fullmatch(identifier):
                result.counts["skipped_bad_identifier"] += 1
                _find(result.findings, "bad_identifier", row=pk)
                continue
            key = identifier.lower()
            if key in seen_identifiers:
                result.counts["skipped_duplicate_identifier"] += 1
                _find(result.findings, "duplicate_identifier", row=pk)
                continue
            seen_identifiers.add(key)
            if not isinstance(blob, (bytes, bytearray)) or not blob:
                result.counts["skipped_no_body"] += 1
                continue
            try:
                text, attachments = clean_note_text(decode_note_text(bytes(blob)))
            except CaptureRefused as error:
                result.counts["skipped_undecodable"] += 1
                _find(result.findings, "undecodable", row=pk, reason=str(error))
                continue
            if not text:
                result.counts["skipped_empty"] += 1
                continue
            result.notes.append(
                _NoteRow(
                    identifier=key,
                    source_id=identifier,
                    title=_clean_label(title, 300),
                    folder=path,
                    created=_core_data_time(created),
                    modified=_core_data_time(modified),
                    pinned=pinned == 1,
                    text=text,
                    attachments=attachments,
                    pasted=looks_pasted(text),
                )
            )
    finally:
        connection.close()
    return result


# ---------------------------------------------------------------------------------------------
# Google Drive for desktop shortcuts
# ---------------------------------------------------------------------------------------------


@dataclass
class _Stub:
    doc_id: str
    kind: str
    title: str
    places: set[str] = field(default_factory=set)


def _read_stub(path: Path) -> dict[str, Any]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise CaptureRefused("shortcut is not a regular file")
        raw = source.read(MAX_STUB_BYTES + 1)
    if len(raw) > MAX_STUB_BYTES:
        raise CaptureRefused("shortcut is larger than the size bound")
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        raise CaptureRefused("shortcut is not valid JSON") from None
    if not isinstance(value, dict):
        raise CaptureRefused("shortcut is not a JSON object")
    return value


def collect_stubs(roots: list[Path], report: dict[str, Any]) -> dict[str, _Stub]:
    """Every Google shortcut under the Drive folders, merged by document id. No link is followed
    and no file but the shortcut itself is opened."""
    counts: Counter[str] = report.setdefault("counts", Counter())
    findings: list[dict[str, Any]] = report.setdefault("findings", [])
    stubs: dict[str, _Stub] = {}
    visited = 0
    for root in roots:
        if root.is_symlink() or not root.is_dir():
            _find(findings, "drive_root_missing")
            counts["drive_roots_missing"] += 1
            continue
        counts["drive_roots_read"] += 1
        pending = [root]
        while pending:
            directory = pending.pop()
            try:
                entries = sorted(os.scandir(directory), key=lambda item: item.name)
            except OSError:
                counts["drive_unreadable_folders"] += 1
                continue
            for entry in entries:
                visited += 1
                if visited > MAX_DRIVE_ENTRIES:
                    raise CaptureRefused("Drive folder has more entries than the bound")
                if entry.is_symlink():
                    counts["drive_symlinks_skipped"] += 1
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                    continue
                suffix = Path(entry.name).suffix.lower()
                if suffix not in STUB_KINDS or not entry.is_file(follow_symlinks=False):
                    counts["drive_other_files"] += 1
                    continue
                counts["drive_shortcuts"] += 1
                try:
                    value = _read_stub(Path(entry.path))
                    doc_id = value.get("doc_id")
                    if not isinstance(doc_id, str) or not _DOC_ID.fullmatch(doc_id):
                        raise CaptureRefused("shortcut holds no valid document id")
                except CaptureRefused as error:
                    counts["drive_shortcuts_refused"] += 1
                    _find(findings, "shortcut_refused", reason=str(error))
                    continue
                relative = Path(entry.path).relative_to(root).parent
                stub = stubs.setdefault(
                    doc_id,
                    _Stub(
                        doc_id=doc_id,
                        kind=suffix,
                        title=_clean_label(Path(entry.name).stem, 300),
                    ),
                )
                stub.places.add(_clean_label(str(relative) if str(relative) != "." else "", 500))
    counts["drive_documents"] = len(stubs)
    return stubs


def stub_address(stub: _Stub) -> str:
    segment = STUB_KINDS[stub.kind][1]
    return f"https://docs.google.com/{segment}/d/{stub.doc_id}"


def render_stub(stub: _Stub) -> str:
    label = STUB_KINDS[stub.kind][0]
    lines = [f"# {stub.title or label}", "", f"Kind: {label}", f"Link: {stub_address(stub)}"]
    places = sorted(place for place in stub.places if place)
    if places:
        lines.append("Drive folders: " + "; ".join(places))
    lines += [
        "",
        "This is a Google Drive shortcut. The document's text is stored by Google, not on this"
        " computer, so only its title, kind, address and folder are captured here.",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------------------------


def _write_atomic(path: Path, data: bytes, *, mtime: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".capture-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mtime is not None:
            os.utime(name, (mtime, mtime))
        Path(name).replace(path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def _existing(path: Path) -> bytes | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("an exported note path is not a regular file")
        return source.read(MAX_NOTE_BYTES + 1)


def _stamp(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value).timestamp() if value else None
    except ValueError:
        return None


def notes_entry(destination: Path) -> dict[str, Any]:
    return {
        "id": "apple-notes",
        "root": str(destination),
        "include": [f"{NOTES_FOLDER}/**/*.md"],
        "identities": INDEX_NAME,
        # The owner's own writing is the default for a note; pasted text is flagged in the
        # sidecar and the authorship rules still withhold credit from marked AI text.
        "wing": "authored",
        "lane": "notes",
        "kind": "document",
    }


def stubs_entry(destination: Path) -> dict[str, Any]:
    return {
        "id": "drive-shortcuts",
        "root": str(destination),
        "include": [f"{DOCS_FOLDER}/**/*.md"],
        # A shortcut note is a generated layout holding a title and an address, not prose.
        "wing": "ai",
        "lane": "support",
        "kind": "document",
    }


def drive_files_entry(root: Path, index: int) -> dict[str, Any]:
    return {
        "id": f"drive-files-{index}",
        "root": str(root),
        "include": list(DRIVE_FILE_GLOBS),
        "exclude": list(DRIVE_EXCLUDE_GLOBS),
        "wing": "authored",
        "lane": "writing",
        "kind": "document",
    }


def refuse_overlap(destination: Path, *source_groups: list[Path]) -> None:
    """Refuse a destination that contains a source or sits inside one: the capture would read
    its own output or write into an application's data."""
    for group in source_groups:
        for entry in group:
            source = _absolute(entry)
            if destination.is_relative_to(source) or source.is_relative_to(destination):
                raise ValueError("destination overlaps a source folder; choose a separate one")


def collect_google_docs(
    roots: list[Path],
    counts: Counter[str],
    findings: list[dict[str, Any]],
    ambiguous: set[str] | None = None,
):
    """The text of every exported Google Doc under ``roots``: (relative path, cleaned text).

    The folders hold Docs exported as Markdown or plain text by a Drive export tool (such as
    `rclone copy REMOTE: DIR --drive-export-formats md`). Only regular `.md` and `.txt` files are
    opened, each through a no-follow descriptor, and each is read at most ``MAX_NOTE_BYTES``.
    """
    found: dict[str, tuple[str, str]] = {}
    for root in roots:
        if root.is_symlink() or not root.is_dir():
            _find(findings, "google_docs_root_missing")
            continue
        pending = [root]
        while pending:
            directory = pending.pop()
            try:
                entries = sorted(os.scandir(directory), key=lambda item: item.name)
            except OSError:
                _find(findings, "google_docs_folder_unreadable")
                continue
            for entry in entries:
                if entry.is_symlink():
                    counts["gdocs_skipped_symlink"] += 1
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False) or not entry.name.lower().endswith(
                    GDOC_SUFFIXES
                ):
                    continue
                counts["gdocs_seen"] += 1
                if counts["gdocs_seen"] > MAX_GDOCS:
                    raise CaptureRefused("more exported documents than the bound")
                relative = str(Path(entry.path).relative_to(root))
                try:
                    descriptor = os.open(entry.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(descriptor, "rb") as source:
                        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                            continue
                        raw = source.read(MAX_NOTE_BYTES + 1)
                except OSError:
                    _find(findings, "google_doc_unreadable")
                    continue
                if len(raw) > MAX_NOTE_BYTES:
                    counts["gdocs_skipped_too_large"] += 1
                    continue
                try:
                    text, _ = clean_note_text(raw.decode("utf-8-sig"))
                except UnicodeDecodeError:
                    counts["gdocs_skipped_not_utf8"] += 1
                    continue
                if not text:
                    counts["gdocs_skipped_empty"] += 1
                    continue
                if relative in found:
                    # Two export folders hold the same relative path: the first one is kept, the
                    # second is counted and named by code, never dropped without a trace.
                    counts["gdocs_skipped_duplicate_path"] += 1
                    if ambiguous is not None:
                        # The kept text is one root's, an id list may be another's: no id.
                        ambiguous.add(relative)
                    _find(findings, "google_doc_duplicate_path")
                    continue
                found[relative] = (Path(entry.name).stem, text)
    return found


def read_drive_ids(path: Path, counts: Counter[str], findings: list[dict[str, Any]]):
    """Drive file ids by export-relative path, from the JSON list `rclone lsjson -R` prints.

    The list is read through a no-follow descriptor within a size bound. A path that two Drive
    files share is ambiguous, so it gets no id and is counted; the Doc still gets its title.
    """
    try:
        descriptor = os.open(_absolute(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise CaptureRefused("Drive id list is not a regular file")
            raw = source.read(MAX_ID_LIST_BYTES + 1)
    except OSError:
        raise CaptureRefused("Drive id list could not be read") from None
    if len(raw) > MAX_ID_LIST_BYTES:
        raise CaptureRefused("Drive id list is larger than the size bound")
    try:
        rows = json.loads(raw)
    except (ValueError, RecursionError):
        raise CaptureRefused("Drive id list is not valid JSON") from None
    if not isinstance(rows, list):
        raise CaptureRefused("Drive id list is not a JSON list")
    ids: dict[str, str] = {}
    ambiguous: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("IsDir"):
            continue
        name, identity = row.get("Path"), row.get("ID")
        if (
            not isinstance(name, str)
            or not isinstance(identity, str)
            or not _DRIVE_ID.fullmatch(identity)
        ):
            counts["gdocs_ids_refused"] += 1
            continue
        if name in ids or name in ambiguous:
            ids.pop(name, None)
            ambiguous.add(name)
            continue
        ids[name] = identity
    counts["gdocs_ids_ambiguous"] += len(ambiguous)
    return ids


def _write_google_docs(
    found: dict[str, tuple[str, str]],
    destination: Path,
    counts: Counter[str],
    ids: dict[str, str] | None = None,
) -> tuple[set[str], list[dict[str, Any]]]:
    current: set[str] = set()
    index: list[dict[str, Any]] = []
    checked: set[Path] = set()
    for relative in sorted(found):
        title, text = found[relative]
        name = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:24]
        path = destination / GDOCS_FOLDER / name[:2] / f"gdoc_{name}.md"
        if path.parent not in checked:
            _no_symlinks(path.parent)
            require_git_private(path.parent)
            checked.add(path.parent)
        data = text.encode("utf-8")
        current.add(str(path.relative_to(destination)))
        if _existing(path) == data:
            counts["gdocs_unchanged"] += 1
        else:
            _write_atomic(path, data)
            counts["gdocs_written"] += 1
        counts["gdocs_characters"] += len(text)
        identity = (ids or {}).get(relative)
        counts["gdocs_with_drive_id"] += int(identity is not None)
        index.append(
            {
                "identifier": name,
                # The identity a Drive document has everywhere else in the library, so a Doc
                # read again is a new revision of that record, never a second record.
                **({"provider": "googledrive", "source_id": identity} if identity else {}),
                "path": str(path.relative_to(destination)),
                "title": _clean_label(title, 300),
                "folder": _clean_label(
                    "" if Path(relative).parent == Path() else str(Path(relative).parent), 500
                ),
                "characters": len(text),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return current, index


def google_docs_entry(destination: Path) -> dict[str, Any]:
    return {
        "id": "google-docs",
        "root": str(destination),
        "include": [f"{GDOCS_FOLDER}/**/*.md"],
        "identities": GDOCS_INDEX_NAME,
        # The owner's Google Docs are mostly their own writing, as Notes are; the authorship
        # rules still withhold credit from text that carries an AI mark.
        "wing": "authored",
        "lane": "writing",
        "kind": "document",
    }


def _prepare_destination(destination: Path) -> Path:
    _no_symlinks(destination)
    require_git_private(destination)
    receipt_path = destination / RECEIPT_NAME
    if destination.exists():
        if not destination.is_dir():
            raise ValueError("destination is not a directory")
        if any(destination.iterdir()) and not receipt_path.is_file():
            raise ValueError("destination is not empty and holds no earlier capture")
    return receipt_path


def _write_notes(
    notes: list[_NoteRow], destination: Path, counts: Counter[str]
) -> tuple[set[str], list[dict[str, Any]]]:
    current: set[str] = set()
    index: list[dict[str, Any]] = []
    checked: set[Path] = set()
    for note in sorted(notes, key=lambda item: item.identifier):
        path = destination / NOTES_FOLDER / note.identifier[:2] / f"{note.identifier}.md"
        if path.parent not in checked:
            _no_symlinks(path.parent)
            require_git_private(path.parent)
            checked.add(path.parent)
        data = note.text.encode("utf-8")
        current.add(str(path.relative_to(destination)))
        if _existing(path) == data:
            counts["notes_unchanged"] += 1
        else:
            _write_atomic(path, data, mtime=_stamp(note.modified))
            counts["notes_written"] += 1
        counts["note_attachments"] += note.attachments
        counts["notes_flagged_pasted"] += int(note.pasted)
        counts["note_characters"] += len(note.text)
        index.append(
            {
                "identifier": note.identifier,
                # The identity an Apple Notes record has everywhere else in the library, so a
                # re-read note is a new revision of that record, never a second record.
                "provider": "applenotes",
                "source_id": note.source_id,
                "path": str(path.relative_to(destination)),
                "title": note.title or _first_line(note.text),
                "folder": note.folder,
                "created": note.created,
                "updated": note.modified,
                "modified": note.modified,
                "pinned": note.pinned,
                "attachments": note.attachments,
                "pasted_text_hint": note.pasted,
                "characters": len(note.text),
            }
        )
    return current, index


def _first_line(text: str) -> str:
    """The first non-empty line, cleaned: the title Apple Notes itself shows for an untitled note."""
    for line in text.splitlines():
        label = _clean_label(line, 300)
        if label:
            return label
    return ""


def _write_stubs(stubs: dict[str, _Stub], destination: Path, counts: Counter[str]) -> set[str]:
    current: set[str] = set()
    checked: set[Path] = set()
    for doc_id in sorted(stubs):
        stub = stubs[doc_id]
        name = hashlib.sha256(doc_id.encode("utf-8")).hexdigest()[:24]
        path = destination / DOCS_FOLDER / name[:2] / f"gd_{name}.md"
        if path.parent not in checked:
            _no_symlinks(path.parent)
            require_git_private(path.parent)
            checked.add(path.parent)
        data = render_stub(stub).encode("utf-8")
        current.add(str(path.relative_to(destination)))
        if _existing(path) == data:
            counts["shortcuts_unchanged"] += 1
        else:
            _write_atomic(path, data)
            counts["shortcuts_written"] += 1
    return current


def _merged_index(path: Path, rows: list[dict[str, Any]]) -> bytes:
    """The sidecar index: the current rows, plus the earlier row of every file that is retained.

    A retained file stays selected by the manifest, so it must keep the title and identity it
    had; an earlier row is kept only for a path no current row names.
    """
    current = {row["path"] for row in rows}
    merged = list(rows)
    try:
        previous = _existing(path)
    except (OSError, ValueError):
        previous = None
    if previous is not None and len(previous) <= MAX_INDEX_BYTES:
        for line in previous.splitlines():
            try:
                row = json.loads(line)
            except (ValueError, RecursionError):
                continue
            name = row.get("path") if isinstance(row, dict) else None
            if (
                isinstance(name, str)
                and not name.startswith("/")
                and ".." not in name.split("/")
                and name not in current
                and (path.parent / name).is_file()
            ):
                merged.append(row)
    return "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
        for row in sorted(merged, key=lambda item: str(item.get("identifier", "")))
    ).encode("utf-8")


def _retained(destination: Path, folder: str, current: set[str]) -> int:
    root = destination / folder
    _no_symlinks(root)
    if not root.is_dir():
        return 0
    return sum(
        1 for item in root.rglob("*.md") if str(item.relative_to(destination)) not in current
    )


def capture_notes_and_docs(
    destination: Path,
    *,
    notes_store: Path | None = None,
    drive_roots: list[Path] | None = None,
    google_docs_exports: list[Path] | None = None,
    google_docs_ids: Path | None = None,
) -> dict[str, Any]:
    """Capture Apple Notes and Google documents under ``destination``; return the receipt.

    ``notes_store`` is the Notes database (None skips Notes); ``drive_roots`` are Drive for
    desktop folders (empty skips Drive); ``google_docs_exports`` are folders of Google Docs
    already exported as Markdown or text (empty skips them). The destination is private (outside every Git work tree,
    or ignored by it), has no symlink in its path, and is new or an earlier capture. Nothing
    already there is deleted, and no source is written to.
    """
    destination = _absolute(destination)
    if notes_store is None and not drive_roots and not google_docs_exports:
        raise ValueError(
            "choose at least one source: a Notes database, a Drive folder or an exported Docs folder"
        )
    refuse_overlap(
        destination,
        [_absolute(root) for root in drive_roots or []],
        [_absolute(root) for root in google_docs_exports or []],
    )
    if notes_store is not None:
        # Not inside the Notes application's own container, and not holding the database it reads.
        container = _absolute(DEFAULT_NOTES_STORE.parent)
        if destination.is_relative_to(container) or _absolute(notes_store).is_relative_to(
            destination
        ):
            raise ValueError("destination overlaps the Notes data; choose a separate folder")
    receipt_path = _prepare_destination(destination)
    counts: Counter[str] = Counter()
    findings: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    index: list[dict[str, Any]] = []
    current_notes: set[str] = set()
    current_stubs: set[str] = set()

    if notes_store is not None:
        counts.update(dict.fromkeys(_NOTE_COUNTS, 0))
        store = _absolute(notes_store)
        with tempfile.TemporaryDirectory(prefix="cce-notes-copy-") as scratch:
            Path(scratch).chmod(0o700)
            copied = copy_notes_store(store, Path(scratch))
            sources.append({"source": "apple-notes", **copied})
            if not copied["settled"]:
                _find(findings, "notes_store_changed_during_copy")
            read = read_notes(Path(scratch) / store.name)
        counts.update(read.counts)
        findings.extend(read.findings[: MAX_FINDINGS - len(findings)])
        current_notes, index = _write_notes(read.notes, destination, counts)
        entries.append(notes_entry(destination))

    roots = [_absolute(root) for root in drive_roots or []]
    if roots:
        counts.update(dict.fromkeys(_DRIVE_COUNTS, 0))
        drive: dict[str, Any] = {"counts": Counter(), "findings": []}
        stubs = collect_stubs(roots, drive)
        sources.append({"source": "google-drive", "roots": len(roots)})
        counts.update(drive["counts"])
        findings.extend(drive["findings"][: MAX_FINDINGS - len(findings)])
        current_stubs = _write_stubs(stubs, destination, counts)
        entries.append(stubs_entry(destination))
        entries.extend(drive_files_entry(root, number) for number, root in enumerate(roots, 1))

    exports = [_absolute(root) for root in google_docs_exports or []]
    current_gdocs: set[str] = set()
    gdoc_index: list[dict[str, Any]] = []
    if exports:
        counts.update(dict.fromkeys(_GDOC_COUNTS, 0))
        ambiguous_paths: set[str] = set()
        found = collect_google_docs(exports, counts, findings, ambiguous_paths)
        sources.append({"source": "google-docs-export", "roots": len(exports)})
        ids = read_drive_ids(google_docs_ids, counts, findings) if google_docs_ids else None
        if ids:
            ids = {name: value for name, value in ids.items() if name not in ambiguous_paths}
        current_gdocs, gdoc_index = _write_google_docs(found, destination, counts, ids)
        entries.append(google_docs_entry(destination))

    destination.mkdir(parents=True, exist_ok=True)
    if exports:
        _write_atomic(
            destination / GDOCS_INDEX_NAME,
            _merged_index(destination / GDOCS_INDEX_NAME, gdoc_index),
        )
        counts["gdocs_retained_not_in_source"] = _retained(destination, GDOCS_FOLDER, current_gdocs)
    if notes_store is not None:
        _write_atomic(
            destination / INDEX_NAME,
            _merged_index(destination / INDEX_NAME, index),
        )
        counts["notes_retained_not_in_source"] = _retained(destination, NOTES_FOLDER, current_notes)
    if roots:
        counts["shortcuts_retained_not_in_source"] = _retained(
            destination, DOCS_FOLDER, current_stubs
        )
    receipt = {
        "schema": SCHEMA,
        "sources": sources,
        "counts": dict(sorted(counts.items())),
        "findings": findings,
        "manifest_entries": entries,
    }
    _write_atomic(
        receipt_path,
        (json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return receipt
