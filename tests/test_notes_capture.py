"""Apple Notes and Drive capture over synthetic stores only: no real Notes data, no application."""

from __future__ import annotations

import gzip
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from conversation_corpus_engine import notes_capture
from conversation_corpus_engine.attribution_rules import attribute_message, record_info
from conversation_corpus_engine.library_documents import iter_documents

NOTE_ENTITY, FOLDER_ENTITY = 12, 15


def _varint(number: int) -> bytes:
    out = bytearray()
    while True:
        byte = number & 0x7F
        number >>= 7
        out.append(byte | (0x80 if number else 0))
        if not number:
            return bytes(out)


def _field(number: int, payload: bytes) -> bytes:
    return _varint((number << 3) | 2) + _varint(len(payload)) + payload


def note_blob(text: str) -> bytes:
    note = _field(2, text.encode("utf-8")) + _varint((9 << 3) | 0) + _varint(1)
    document = _varint((1 << 3) | 0) + _varint(0) + _field(3, note)
    return gzip.compress(_field(2, document))


def make_store(path: Path, rows: list[dict]) -> Path:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER PRIMARY KEY, Z_NAME VARCHAR);
        CREATE TABLE ZICCLOUDSYNCINGOBJECT (
            Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, ZTITLE1 VARCHAR, ZTITLE2 VARCHAR,
            ZIDENTIFIER VARCHAR, ZFOLDER INTEGER, ZPARENT INTEGER, ZFOLDERTYPE INTEGER,
            ZNOTEDATA INTEGER, ZMARKEDFORDELETION INTEGER, ZISPASSWORDPROTECTED INTEGER,
            ZCREATIONDATE1 REAL, ZMODIFICATIONDATE1 REAL, ZISPINNED INTEGER);
        CREATE TABLE ZICNOTEDATA (Z_PK INTEGER PRIMARY KEY, ZNOTE INTEGER, ZDATA BLOB);
        INSERT INTO Z_PRIMARYKEY VALUES (12, 'ICNote'), (15, 'ICFolder');
        """
    )
    # folders: 1 Notes, 2 Journal (inside Notes), 3 Recently Deleted
    connection.executemany(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, Z_ENT, ZTITLE2, ZPARENT, ZFOLDERTYPE) "
        "VALUES (?, 15, ?, ?, ?)",
        [(1, "Notes", None, 0), (2, "Journal", 1, 0), (3, "Recently Deleted", None, 1)],
    )
    for number, row in enumerate(rows, start=10):
        blob = row.get("blob", note_blob(row["text"]) if "text" in row else None)
        data_pk = None
        if blob is not None:
            data_pk = number
            connection.execute("INSERT INTO ZICNOTEDATA VALUES (?, ?, ?)", (number, number, blob))
        connection.execute(
            "INSERT INTO ZICCLOUDSYNCINGOBJECT VALUES (?, 12, ?, NULL, ?, ?, NULL, NULL, ?, ?, ?, "
            "?, ?, ?)",
            (
                number,
                row.get("title", "T"),
                row.get("identifier", f"AAAAAAAA-0000-0000-0000-{number:012d}"),
                row.get("folder", 2),
                data_pk,
                row.get("deleted", 0),
                row.get("locked", 0),
                row.get("created", 700000000.0),
                row.get("modified", 700000100.0),
                row.get("pinned", 0),
            ),
        )
    connection.commit()
    connection.close()
    return path


@pytest.fixture
def store(tmp_path):
    return make_store(
        tmp_path / "NoteStore.sqlite",
        [
            {"title": "Tide", "text": "Tide diary\nWent out at dawn.\n"},
            {"title": "Pasted", "text": "Chat log\nChatGPT said:\nHere is a plan.\n"},
            {"title": "Attach", "text": "Photo note\n￼\nafter the picture\r\n"},
            {"title": "Trash", "text": "gone", "folder": 3},
            {"title": "Flagged", "text": "gone too", "deleted": 1},
            {"title": "Locked", "text": "secret", "locked": 1},
            {"title": "Nobody", "blob": None},
            {"title": "Empty", "text": "\n \n"},
            {"title": "Bad", "blob": b"not gzip at all"},
        ],
    )


def test_protobuf_round_trip_and_hostile_bodies():
    assert notes_capture.decode_note_text(note_blob("héllo\nworld")) == "héllo\nworld"
    with pytest.raises(notes_capture.CaptureRefused):
        notes_capture.decode_note_text(gzip.compress(b"\x12\xff\xff\xff\xff\x0f"))
    with pytest.raises(notes_capture.CaptureRefused, match="size bound"):
        notes_capture.decode_note_text(gzip.compress(b"\x12" + b"\x80" * 5 + b"0" * 9_000_000))


def test_clean_text_keeps_only_the_notes_own_words():
    text, attachments = notes_capture.clean_note_text("a￼\r\nb\x00\r\n\n\n")
    assert (text, attachments) == ("a\nb\n", 1)
    assert notes_capture.clean_note_text("\n \n") == ("", 0)


def test_one_file_per_readable_note_with_exact_bytes(store, tmp_path):
    out = tmp_path / "out"
    receipt = notes_capture.capture_notes_and_docs(out, notes_store=store)
    files = sorted((out / "notes").rglob("*.md"))
    assert len(files) == 3
    by_name = {file.stem: file.read_text(encoding="utf-8") for file in files}
    assert by_name["aaaaaaaa-0000-0000-0000-000000000010"] == "Tide diary\nWent out at dawn.\n"
    assert by_name["aaaaaaaa-0000-0000-0000-000000000012"] == "Photo note\n\nafter the picture\n"
    counts = receipt["counts"]
    assert counts["notes_seen"] == 9 and counts["notes_written"] == 3
    assert counts["skipped_deleted"] == 2
    assert counts["skipped_password_protected"] == 1
    assert counts["skipped_no_body"] == 1 and counts["skipped_empty"] == 1
    assert counts["skipped_undecodable"] == 1
    assert counts["note_attachments"] == 1 and counts["notes_flagged_pasted"] == 1
    # the secrets and the trash never reach disk
    assert "secret" not in "".join(by_name.values()) and "gone" not in "".join(by_name.values())
    assert "secret" not in json.dumps(receipt) and "Tide" not in json.dumps(receipt)


def test_sidecar_index_holds_the_metadata_not_the_note(store, tmp_path):
    out = tmp_path / "out"
    notes_capture.capture_notes_and_docs(out, notes_store=store)
    rows = [json.loads(line) for line in (out / "notes-index.jsonl").read_text().splitlines()]
    assert len(rows) == 3
    tide = next(row for row in rows if row["title"] == "Tide")
    assert tide["folder"] == "Notes / Journal"
    assert tide["created"].startswith("2023-") and tide["pasted_text_hint"] is False
    assert next(row for row in rows if row["title"] == "Pasted")["pasted_text_hint"] is True
    assert (out / tide["path"]).read_text() == "Tide diary\nWent out at dawn.\n"
    # Core Data 700000100 s after 2001-01-01 is the note's modified time, kept as the file time.
    assert abs((out / tide["path"]).stat().st_mtime - (700_000_100 + 978_307_200)) < 2


def test_refresh_writes_only_changes_and_deletes_nothing(store, tmp_path):
    out = tmp_path / "out"
    first = notes_capture.capture_notes_and_docs(out, notes_store=store)
    again = notes_capture.capture_notes_and_docs(out, notes_store=store)
    assert first["counts"]["notes_written"] == 3
    assert again["counts"]["notes_written"] == 0 and again["counts"]["notes_unchanged"] == 3
    connection = sqlite3.connect(store)
    connection.execute("UPDATE ZICCLOUDSYNCINGOBJECT SET ZMARKEDFORDELETION = 1 WHERE Z_PK = 10")
    connection.commit()
    connection.close()
    third = notes_capture.capture_notes_and_docs(out, notes_store=store)
    assert third["counts"]["notes_retained_not_in_source"] == 1
    assert len(list((out / "notes").rglob("*.md"))) == 3


def test_the_source_is_never_written(store, tmp_path):
    before = (store.read_bytes(), store.stat().st_mtime_ns)
    notes_capture.capture_notes_and_docs(tmp_path / "out", notes_store=store)
    assert (store.read_bytes(), store.stat().st_mtime_ns) == before
    assert not list(tmp_path.glob("NoteStore.sqlite-*"))


def test_wal_pending_writes_are_read_from_the_copy(tmp_path):
    path = make_store(tmp_path / "NoteStore.sqlite", [{"title": "A", "text": "first\n"}])
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    data = note_blob("added in the log\n")
    connection.execute("INSERT INTO ZICNOTEDATA VALUES (99, 99, ?)", (data,))
    connection.execute(
        "INSERT INTO ZICCLOUDSYNCINGOBJECT VALUES (99, 12, 'B', NULL, 'BBBBBBBB-1', 2, NULL, NULL, "
        "99, 0, 0, 1.0, 2.0, 0)"
    )
    connection.commit()
    assert (tmp_path / "NoteStore.sqlite-wal").stat().st_size > 0
    receipt = notes_capture.capture_notes_and_docs(tmp_path / "out", notes_store=path)
    connection.close()
    assert receipt["counts"]["notes_written"] == 2
    assert receipt["sources"][0]["settled"] is True


def test_a_changed_schema_is_refused_not_guessed(tmp_path):
    path = tmp_path / "NoteStore.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER, Z_NAME VARCHAR)")
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="expected tables"):
        notes_capture.capture_notes_and_docs(tmp_path / "out", notes_store=path)
    with pytest.raises(ValueError, match="does not exist"):
        notes_capture.capture_notes_and_docs(
            tmp_path / "out2", notes_store=tmp_path / "missing.sqlite"
        )


def test_note_files_carry_no_agent_credit_and_stay_in_the_authored_wing(store, tmp_path):
    out = tmp_path / "out"
    receipt = notes_capture.capture_notes_and_docs(out, notes_store=store)
    entry = next(e for e in receipt["manifest_entries"] if e["id"] == "apple-notes")
    assert (entry["wing"], entry["lane"]) == ("authored", "writing")
    records = list(iter_documents([entry], report={}))
    assert len(records) == 3, "the documents reader reads the exported notes"

    def authors(fragment: str) -> set[str]:
        record = next(r for r in records if fragment in json.dumps(r.to_dict()))
        info = record_info(record.to_dict())
        return {
            span.author
            for message in record.messages
            for span in attribute_message(message.text, role=message.role, record=info)
        }

    assert "owner" in authors("Went out at dawn")
    assert "owner" not in authors("ChatGPT said")  # a pasted chat turn gets no owner credit


def _drive(tmp_path: Path) -> Path:
    root = tmp_path / "My Drive"
    (root / "Sub").mkdir(parents=True)
    stub = {"doc_id": "1AbCdEfGhIjKlMnOp", "email": "someone@example.com", "resource_key": ""}
    (root / "Sub" / "Plan.gdoc").write_text(json.dumps(stub))
    (root / "Plan copy.gdoc").write_text(json.dumps(stub))
    (root / "Budget.gsheet").write_text(json.dumps({**stub, "doc_id": "2ZyXwVuTsRqPoNmLk"}))
    (root / "Broken.gdoc").write_text("{not json")
    (root / "NoId.gdoc").write_text(json.dumps({"url": "https://x"}))
    (root / "README.md").write_text("plain file\n")
    (root / "link.gdoc").symlink_to(root / "Plan copy.gdoc")
    return root


def test_drive_shortcuts_become_metadata_only_notes(tmp_path):
    root = _drive(tmp_path)
    out = tmp_path / "out"
    receipt = notes_capture.capture_notes_and_docs(out, drive_roots=[root])
    counts = receipt["counts"]
    assert counts["drive_shortcuts"] == 5 and counts["drive_other_files"] == 1
    assert counts["drive_documents"] == 2
    assert counts["drive_shortcuts_refused"] == 2
    assert counts["drive_symlinks_skipped"] == 1
    assert counts["shortcuts_written"] == 2
    texts = [file.read_text() for file in (out / "docs").rglob("*.md")]
    plan = next(text for text in texts if text.startswith("# Plan"))
    assert "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOp" in plan
    assert "Drive folders: Sub" in plan
    assert "not on this computer" in plan
    assert "someone@example.com" not in "".join(texts) + json.dumps(receipt)
    ids = {e["id"]: e for e in receipt["manifest_entries"]}
    assert (ids["drive-shortcuts"]["wing"], ids["drive-shortcuts"]["lane"]) == ("ai", "support")
    assert ids["drive-files-1"]["root"] == str(root)


def test_sources_must_be_chosen_and_destination_private(store, tmp_path):
    with pytest.raises(ValueError, match="at least one source"):
        notes_capture.capture_notes_and_docs(tmp_path / "out")
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "stranger.txt").write_text("x")
    with pytest.raises(ValueError, match="not empty"):
        notes_capture.capture_notes_and_docs(occupied, notes_store=store)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    with pytest.raises(Exception, match="(?i)private|git"):
        notes_capture.capture_notes_and_docs(repo / "capture-dest", notes_store=store)
    assert not (repo / "capture-dest").exists()


def test_the_command_writes_the_receipt(store, tmp_path):
    out = tmp_path / "cli-out"
    environment = {**os.environ, "PYTHONPATH": str(Path(notes_capture.__file__).parents[1])}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "conversation_corpus_engine.cli",
            "library",
            "capture-notes-docs",
            "--out",
            str(out),
            "--notes-store",
            str(store),
        ],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["counts"]["notes_written"] == 3
    assert (out / notes_capture.RECEIPT_NAME).is_file()


def test_truncated_gzip_is_refused_not_partially_captured():
    blob = note_blob("a long enough note body to cut " * 20)
    with pytest.raises(notes_capture.CaptureRefused, match="truncated"):
        notes_capture.decode_note_text(blob[:-8])  # CRC and length trailer cut off
    with pytest.raises(notes_capture.CaptureRefused):
        notes_capture.decode_note_text(blob[: len(blob) // 2])


def test_many_tiny_fields_are_read_lazily():
    body = (
        _field(2, _varint((1 << 3) | 0) + _varint(0) + _field(3, _field(2, b"x")))
        + b"\x08\x01" * 1000
    )
    assert notes_capture.decode_note_text(gzip.compress(body)) == "x"


def test_a_corrupt_database_copy_is_a_refusal_not_a_traceback(tmp_path):
    bad = tmp_path / "NoteStore.sqlite"
    bad.write_bytes(b"this is not a sqlite database" * 100)
    with pytest.raises(ValueError, match="could not be read"):
        notes_capture.capture_notes_and_docs(tmp_path / "out", notes_store=bad)


def test_a_journal_file_vanishing_mid_copy_repeats_the_copy(store, tmp_path, monkeypatch):
    (tmp_path / "NoteStore.sqlite-wal").write_bytes(b"")
    real = notes_capture._copy_regular
    calls = {"count": 0}

    def flaky(source, target):
        calls["count"] += 1
        if calls["count"] == 2:  # the journal, on the first attempt only
            raise FileNotFoundError(source)
        return real(source, target)

    monkeypatch.setattr(notes_capture, "_copy_regular", flaky)
    receipt = notes_capture.capture_notes_and_docs(tmp_path / "out", notes_store=store)
    assert receipt["sources"][0]["attempts"] == 2 and receipt["sources"][0]["settled"] is True
    assert receipt["counts"]["notes_written"] == 3


def test_a_store_that_never_settles_is_reported(store, tmp_path, monkeypatch):
    sizes = iter(range(1, 100))
    real = notes_capture._signature
    monkeypatch.setattr(
        notes_capture,
        "_signature",
        lambda path: (next(sizes), 0) if path.name == store.name else real(path),
    )
    receipt = notes_capture.capture_notes_and_docs(tmp_path / "out", notes_store=store)
    assert receipt["sources"][0]["settled"] is False and receipt["sources"][0]["attempts"] == 3
    assert {"code": "notes_store_changed_during_copy"} in receipt["findings"]


def test_the_temporary_copy_is_removed_even_when_reading_fails(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    monkeypatch.setattr("tempfile.tempdir", None)
    bad = tmp_path / "NoteStore.sqlite"
    bad.write_bytes(b"junk" * 1000)
    with pytest.raises(ValueError):
        notes_capture.capture_notes_and_docs(tmp_path / "out", notes_store=bad)
    assert list(scratch.iterdir()) == []


def test_the_destination_may_not_overlap_a_source(store, tmp_path, monkeypatch):
    container = tmp_path / "fake-notes-container"
    container.mkdir()
    monkeypatch.setattr(notes_capture, "DEFAULT_NOTES_STORE", container / "NoteStore.sqlite")
    drive = _drive(tmp_path)
    with pytest.raises(ValueError, match="overlaps"):
        notes_capture.capture_notes_and_docs(drive / "capture", drive_roots=[drive])
    with pytest.raises(ValueError, match="overlaps"):
        notes_capture.capture_notes_and_docs(tmp_path, drive_roots=[drive])
    with pytest.raises(ValueError, match="overlaps"):  # the destination would hold the database
        notes_capture.capture_notes_and_docs(tmp_path, notes_store=store)
    inside_notes = container / "capture-dest"
    with pytest.raises(ValueError, match="overlaps"):
        notes_capture.capture_notes_and_docs(inside_notes, notes_store=store)
    assert not inside_notes.exists()


def _gdocs(tmp_path: Path) -> Path:
    root = tmp_path / "gdocs-export"
    (root / "Work").mkdir(parents=True)
    (root / "Plan.md").write_text("﻿# Plan\r\nfirst line\r\n", encoding="utf-8")
    (root / "Work" / "Plan.md").write_text("same title, other folder\n")
    (root / "Work" / "Notes.txt").write_text("plain text doc\n")
    (root / "Empty.md").write_text("\n \n")
    (root / "Binary.md").write_bytes(b"\xff\xfe\x00bad")
    (root / "Skip.pdf").write_text("not a doc")
    (root / "link.md").symlink_to(root / "Plan.md")
    return root


def test_exported_google_docs_become_one_note_each(tmp_path):
    root = _gdocs(tmp_path)
    out = tmp_path / "out"
    receipt = notes_capture.capture_notes_and_docs(out, google_docs_exports=[root])
    counts = receipt["counts"]
    assert counts["gdocs_seen"] == 5 and counts["gdocs_written"] == 3
    assert counts["gdocs_skipped_empty"] == 1 and counts["gdocs_skipped_not_utf8"] == 1
    assert counts["gdocs_skipped_symlink"] == 1
    files = sorted((out / "gdocs").rglob("*.md"))
    assert len(files) == 3  # same title in two folders is two documents
    assert "# Plan\nfirst line\n" in {f.read_text() for f in files}
    rows = [json.loads(x) for x in (out / "gdocs-index.jsonl").read_text().splitlines()]
    assert {row["folder"] for row in rows} == {"", "Work"}
    assert "Plan" in {row["title"] for row in rows}
    entry = next(e for e in receipt["manifest_entries"] if e["id"] == "google-docs")
    assert (entry["wing"], entry["lane"]) == ("authored", "writing")
    assert len(list(iter_documents([entry], report={}))) == 3
    again = notes_capture.capture_notes_and_docs(out, google_docs_exports=[root])
    assert again["counts"]["gdocs_written"] == 0 and again["counts"]["gdocs_unchanged"] == 3
    assert "Plan" not in json.dumps(receipt)


def test_google_docs_export_may_not_overlap_the_destination(tmp_path):
    root = _gdocs(tmp_path)
    with pytest.raises(ValueError, match="overlaps"):
        notes_capture.capture_notes_and_docs(root / "capture", google_docs_exports=[root])


def test_a_relative_path_in_two_export_roots_is_counted_not_dropped(tmp_path):
    first, second = tmp_path / "one", tmp_path / "two"
    for root, body in ((first, "from one\n"), (second, "from two\n")):
        root.mkdir()
        (root / "Plan.md").write_text(body)
        (root / ".hidden").mkdir()
    (second / ".hidden" / "Other.md").write_text("hidden folder\n")
    out = tmp_path / "out"
    receipt = notes_capture.capture_notes_and_docs(out, google_docs_exports=[first, second])
    assert receipt["counts"]["gdocs_seen"] == 3 and receipt["counts"]["gdocs_written"] == 2
    assert receipt["counts"]["gdocs_skipped_duplicate_path"] == 1
    assert {"code": "google_doc_duplicate_path"} in receipt["findings"]
    texts = {f.read_text() for f in (out / "gdocs").rglob("*.md")}
    assert texts == {"from one\n", "hidden folder\n"}
    rows = [json.loads(x) for x in (out / "gdocs-index.jsonl").read_text().splitlines()]
    assert ".hidden" in {row["folder"] for row in rows}


def test_google_docs_source_is_untouched_and_oversize_is_skipped(tmp_path, monkeypatch):
    root = _gdocs(tmp_path)
    monkeypatch.setattr(notes_capture, "MAX_NOTE_BYTES", 1000)
    (root / "Huge.md").write_text("x" * 1001)
    before = sorted((p.name, p.stat().st_mtime_ns, p.read_bytes()) for p in root.rglob("*.md"))
    receipt = notes_capture.capture_notes_and_docs(tmp_path / "out", google_docs_exports=[root])
    after = sorted((p.name, p.stat().st_mtime_ns, p.read_bytes()) for p in root.rglob("*.md"))
    assert before == after
    assert receipt["counts"]["gdocs_skipped_too_large"] == 1
    assert "x" * 50 not in json.dumps(receipt)
