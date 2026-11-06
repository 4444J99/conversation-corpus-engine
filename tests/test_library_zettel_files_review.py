"""Regression cases for ownership planning and interrupted Zettel publication."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from conversation_corpus_engine import library_zettel_files as files


def entry(caller: str = "idea-1", text: bytes = b"Synthetic note.\n") -> dict:
    canonical = json.dumps(
        {"id": caller, "schema": "cce.zettel-note-id.v1"},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    identity = hashlib.sha256(canonical.encode()).hexdigest()
    return {
        "id": "20260304-" + identity[:6],
        "caller_digest": identity,
        "data": text,
        "sha256": hashlib.sha256(text).hexdigest(),
    }


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


@pytest.mark.parametrize(
    "name",
    [
        "../escaped.md",
        "../../escaped.md",
        "part/note.md",
        "part\\note.md",
        "line\nbreak.md",
        "x" * 253 + ".md",
    ],
)
def test_direct_writer_rejects_unsafe_filename_before_any_output(tmp_path, name):
    root = tmp_path / "wiki"
    with pytest.raises(ValueError, match="invalid.*filename"):
        files.write_owned_notes(root, {name: entry()}, max_entries=1000)
    assert not root.exists()


def test_direct_writer_rejects_absolute_filename_before_any_output(tmp_path):
    root = tmp_path / "wiki"
    target = tmp_path / "escaped.md"
    with pytest.raises(ValueError, match="invalid.*filename"):
        files.write_owned_notes(root, {str(target): entry()}, max_entries=1000)
    assert not root.exists() and not target.exists()


@pytest.mark.parametrize("retry", ["same", "changed", "omitted"])
def test_pending_manifest_cannot_adopt_matching_foreign_bytes(tmp_path, monkeypatch, retry):
    root = tmp_path / "wiki"
    name, original = "Synthetic.md", entry()
    atomic = files._atomic_write

    def editor_first(path, data, **kwargs):
        if path.name == name:
            path.write_bytes(data)
        return atomic(path, data, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(files, "_atomic_write", editor_first)
        with pytest.raises(ValueError, match="appeared during export"):
            files.write_owned_notes(root, {name: original}, max_entries=1000)
    before = snapshot(root)
    planned = (
        {}
        if retry == "omitted"
        else {name: entry(text=b"Changed idea.\n") if retry == "changed" else original}
    )
    with pytest.raises(ValueError, match="creation proof"):
        files.write_owned_notes(root, planned, max_entries=1000)
    assert snapshot(root) == before


def test_absent_pending_name_stays_unclaimed_when_next_batch_omits_it(tmp_path, monkeypatch):
    root = tmp_path / "wiki"
    name, original = "Synthetic.md", entry()
    atomic = files._atomic_write

    def stopped(path, data, **kwargs):
        if path.name == name:
            raise OSError("stop before creation")
        return atomic(path, data, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(files, "_atomic_write", stopped)
        with pytest.raises(OSError, match="stop before creation"):
            files.write_owned_notes(root, {name: original}, max_entries=1000)
    files.write_owned_notes(root, {"Other.md": entry("idea-2")}, max_entries=1000)
    (root / files.NOTES_FOLDER / name).write_bytes(original["data"])
    before = snapshot(root)
    with pytest.raises(ValueError, match="creation proof"):
        files.write_owned_notes(root, {name: original}, max_entries=1000)
    assert snapshot(root) == before


def test_created_inode_proof_cannot_adopt_a_later_matching_replacement(tmp_path, monkeypatch):
    root = tmp_path / "wiki"
    first, second = entry("idea-1"), entry("idea-2")
    planned = {"First.md": first, "Second.md": second}
    atomic = files._atomic_write

    def stopped(path, data, **kwargs):
        if path.name == "Second.md":
            raise OSError("second note interrupted")
        return atomic(path, data, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(files, "_atomic_write", stopped)
        with pytest.raises(OSError, match="interrupted"):
            files.write_owned_notes(root, planned, max_entries=1000)
    note = root / files.NOTES_FOLDER / "First.md"
    note.rename(tmp_path / "retained-created-note")
    note.write_bytes(first["data"])
    before = snapshot(root)
    with pytest.raises(ValueError, match="creation proof"):
        files.write_owned_notes(root, planned, max_entries=1000)
    assert snapshot(root) == before


def test_link_landed_before_error_without_creation_receipt_remains_ambiguous(tmp_path, monkeypatch):
    root = tmp_path / "wiki"
    name, original = "Synthetic.md", entry()
    link = files.os.link

    def late_error(source, target, **kwargs):
        link(source, target, **kwargs)
        if Path(target).name == name:
            raise OSError("link landed before error")

    with monkeypatch.context() as patch:
        patch.setattr(files.os, "link", late_error)
        with pytest.raises(OSError, match="link landed"):
            files.write_owned_notes(root, {name: original}, max_entries=1000)
    before = snapshot(root)
    with pytest.raises(ValueError, match="creation proof"):
        files.write_owned_notes(root, {name: original}, max_entries=1000)
    assert snapshot(root) == before


def test_full_caller_digest_rejects_a_real_short_id_collision(tmp_path):
    root = tmp_path / "wiki"
    first, second = entry("idea-1474"), entry("idea-3497", b"Different logical idea.\n")
    assert first["id"] == second["id"] and first["caller_digest"] != second["caller_digest"]
    files.write_owned_notes(root, {"Synthetic.md": first}, max_entries=1000)
    before = snapshot(root)
    with pytest.raises(ValueError, match="caller identity"):
        files.write_owned_notes(root, {"Synthetic.md": second}, max_entries=1000)
    assert snapshot(root) == before


def test_legacy_manifest_without_full_caller_identity_refuses_ambiguous_update(tmp_path):
    root = tmp_path / "wiki"
    original = entry()
    files.write_owned_notes(root, {"Synthetic.md": original}, max_entries=1000)
    path = root / files.MANIFEST_NAME
    manifest = json.loads(path.read_text())
    manifest["files"]["Synthetic.md"].pop("caller_digest", None)
    path.write_text(json.dumps(manifest))
    before = snapshot(root)
    with pytest.raises(ValueError, match="caller identity"):
        files.write_owned_notes(root, {"Synthetic.md": original}, max_entries=1000)
    assert snapshot(root) == before


def test_deleted_manifest_reservations_share_the_entry_cap(tmp_path):
    root = tmp_path / "wiki"
    for number in (1, 2):
        name = f"Note {number}.md"
        files.write_owned_notes(root, {name: entry(f"idea-{number}")}, max_entries=2)
        (root / files.NOTES_FOLDER / name).unlink()
    before = snapshot(root)
    with pytest.raises(ValueError, match="3 retained and incoming.*2 folder cap"):
        files.write_owned_notes(root, {"Note 3.md": entry("idea-3")}, max_entries=2)
    assert snapshot(root) == before


@pytest.mark.parametrize("stage", ["pending", "note", "complete"])
def test_directory_sync_failure_never_reports_a_successful_batch(tmp_path, monkeypatch, stage):
    root = tmp_path / "wiki"
    note = root / files.NOTES_FOLDER / "Synthetic.md"
    manifest = root / files.MANIFEST_NAME
    sync = files._sync_directory
    tripped = False

    def failed(path):
        nonlocal tripped
        data = json.loads(manifest.read_text()) if manifest.exists() else {}
        target = (
            stage == "pending"
            and path == root
            and data.get("state") == "writing"
            and not note.exists()
            or stage == "note"
            and path == note.parent
            and note.exists()
            or stage == "complete"
            and path == root
            and data.get("state") == "complete"
        )
        if target and not tripped:
            tripped = True
            raise OSError(f"synthetic {stage} directory sync failure")
        sync(path)

    with monkeypatch.context() as patch:
        patch.setattr(files, "_sync_directory", failed)
        with pytest.raises(OSError, match=f"synthetic {stage}"):
            files.write_owned_notes(root, {"Synthetic.md": entry()}, max_entries=1000)
    assert tripped
    if stage == "pending":
        assert not note.exists()
    else:
        assert note.read_bytes() == entry()["data"]
    if stage == "note":
        before = snapshot(root)
        with pytest.raises(ValueError, match="creation proof"):
            files.write_owned_notes(root, {"Synthetic.md": entry()}, max_entries=1000)
        assert snapshot(root) == before
    else:
        assert files.write_owned_notes(root, {"Synthetic.md": entry()}, max_entries=1000)[
            "written"
        ] == (1 if stage == "pending" else 0)


def test_new_directory_chain_is_synced_before_first_note_publication(tmp_path, monkeypatch):
    root = tmp_path / "one" / "two" / "wiki"
    synced = set()
    sync, atomic = files._sync_directory, files._atomic_write

    def record(path):
        sync(path)
        synced.add(path)

    def verify(path, data, **kwargs):
        if path.suffix == ".md":
            assert {tmp_path, tmp_path / "one", root.parent, root, root / "notes"} <= synced
        return atomic(path, data, **kwargs)

    monkeypatch.setattr(files, "_sync_directory", record)
    monkeypatch.setattr(files, "_atomic_write", verify)
    assert (
        files.write_owned_notes(root, {"Synthetic.md": entry()}, max_entries=1000)["written"] == 1
    )
