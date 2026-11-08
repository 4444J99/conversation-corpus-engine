"""Encrypted synthetic browser edits return through a source-owned CCE adapter."""

import json
import os
import zlib

import pytest

from conversation_corpus_engine.archive_sealed_crypto import (
    Identity,
    canonical,
    digest,
    encrypt,
    sign,
)
from conversation_corpus_engine.knovv_journal import import_journal, import_snapshot


def fixture(attachment=False, revisions=1):
    owner = Identity("knovv", "owner", os.urandom(32), os.urandom(32))
    signer = Identity("knovv", "browser", os.urandom(32), os.urandom(32))
    key = os.urandom(32)
    public = signer.signing_public()
    device = digest(public.encode())
    grant = sign(
        {
            "schema": "knovv-device.v1",
            "id": device,
            "signing_public": public,
            "generation": 1,
            "revoked": False,
        },
        owner,
    )
    document = {
        "id": "a" * 64,
        "title": "Synthetic idea",
        "body": "A retained, attributed idea.",
        "kind": "zettel",
        "author": "owner",
        "authorship_basis": "declared",
        "sources": [],
        "links": [],
        "status": "draft",
        "created": "2026-01-01",
        "updated": "2026-01-01",
        "source_url": "https://example.invalid/captured-evidence",
        "relationships": [{"type": "contradicts", "target": "d" * 64}],
    }
    objects = {}
    if attachment:
        original = b"\x00\xffretained original bytes"
        cipher = encrypt(key, zlib.compress(original), "knovv-attachment:" + "b" * 64 + ":1")
        oid = digest(cipher)
        objects[oid] = cipher
        document["attachments"] = [
            {
                "object": oid,
                "name": "../../original.bin",
                "media_type": "application/octet-stream",
                "size": len(original),
                "epoch": 1,
            }
        ]
    raw = encrypt(
        key, zlib.compress(canonical(document)), "knovv-revision-object:" + "b" * 64 + ":1"
    )
    revision = sign(
        {
            "schema": "knovv-revision.v1",
            "id": "c" * 64,
            "doc": document["id"],
            "parent": None,
            "object": digest(raw),
            "device": device,
            "epoch": 1,
        },
        signer,
    )
    journal = {
        "schema": "knovv-journal.v1",
        "vault": "b" * 64,
        "epoch": 1,
        "grants": [grant],
        "revisions": [{"seq": 1, "envelope": revision}],
    }
    objects[digest(raw)] = raw
    for number in range(1, revisions):
        document["body"] = f"A retained revision {number}."
        raw = encrypt(
            key, zlib.compress(canonical(document)), "knovv-revision-object:" + "b" * 64 + ":1"
        )
        updated = sign(
            {
                key: value
                for key, value in {
                    **revision,
                    "id": digest(f"revision-{number}".encode()),
                    "parent": revision["id"],
                    "object": digest(raw),
                }.items()
                if key != "signature"
            },
            signer,
        )
        journal["revisions"].append({"seq": number + 1, "envelope": updated})
        objects[digest(raw)] = raw
        revision = updated
    return owner, key, journal, objects


def test_journal_history_resumes_in_bounded_batches_without_a_lifetime_entry_limit(tmp_path):
    owner, key, journal, objects = fixture(revisions=5)
    counts = []
    for _ in range(3):
        result = import_journal(
            journal,
            objects.__getitem__,
            key,
            owner.signing_public(),
            tmp_path / "authoring",
            max_entries=2,
        )
        counts.append(result["imported"])
    assert counts == [2, 2, 1] and result["cursor"] == 5 and result["has_more"] is False
    assert len(list((tmp_path / "authoring" / "history").rglob("*.json"))) == 5
    assert "revision 4" in next((tmp_path / "authoring" / "notes").glob("*.md")).read_text()


def test_attachment_originals_are_restored_and_tampering_cannot_create_partial_inputs(tmp_path):
    owner, key, journal, objects = fixture(attachment=True)
    home = tmp_path / "authoring"
    import_journal(journal, objects.__getitem__, key, owner.signing_public(), home)
    files = list((home / "attachments").glob("*"))
    assert len(files) == 1
    assert files[0].read_bytes() == b"\x00\xffretained original bytes"
    oid = next(iter(objects))
    objects[oid] = b"tampered"
    with pytest.raises(ValueError):
        import_journal(journal, objects.__getitem__, key, owner.signing_public(), tmp_path / "bad")
    assert not (tmp_path / "bad").exists()


def test_rotated_publications_can_import_earlier_accepted_revisions_and_attachments(tmp_path):
    owner, key, journal, objects = fixture(attachment=True)
    journal["epoch"] = 2
    result = import_journal(
        journal,
        objects.__getitem__,
        os.urandom(32),
        owner.signing_public(),
        tmp_path / "authoring",
        epoch_keys={"1": key.hex()},
    )
    assert result["imported"] == 1
    assert (
        next((tmp_path / "authoring" / "attachments").iterdir()).read_bytes()
        == b"\x00\xffretained original bytes"
    )


def test_authenticated_edits_are_idempotent_source_inputs_without_rewriting_originals(tmp_path):
    owner, key, journal, objects = fixture()
    result = import_journal(
        journal, objects.__getitem__, key, owner.signing_public(), tmp_path / "authoring"
    )
    assert result["imported"] == 1
    assert (
        import_journal(
            journal, objects.__getitem__, key, owner.signing_public(), tmp_path / "authoring"
        )["imported"]
        == 0
    )
    notes = list((tmp_path / "authoring" / "notes").glob("*.md"))
    assert len(notes) == 1 and "A retained, attributed idea." in notes[0].read_text()
    manifest = json.loads((tmp_path / "authoring" / "source.json").read_text())
    assert manifest["documents"][0]["id"].startswith("knovv-")
    assert list((tmp_path / "authoring" / "history").rglob("*.json"))
    assert "https://example.invalid/captured-evidence" in notes[0].read_text()
    assert "contradicts" in notes[0].read_text()
    from conversation_corpus_engine.library_documents import iter_documents  # noqa: PLC0415

    ingested = list(iter_documents(manifest["documents"]))
    assert any(
        "https://example.invalid/captured-evidence" in message.text
        and "contradicts" in message.text
        for record in ingested
        for message in record.messages
    )


def test_bad_signature_or_missing_parent_cannot_modify_the_current_note(tmp_path):
    owner, key, journal, objects = fixture()
    home = tmp_path / "authoring"
    import_journal(journal, objects.__getitem__, key, owner.signing_public(), home)
    before = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    journal["revisions"][0]["envelope"]["parent"] = "d" * 64
    with pytest.raises(ValueError):
        import_journal(journal, objects.__getitem__, key, owner.signing_public(), home)
    assert {p: p.read_bytes() for p in home.rglob("*") if p.is_file()} == before


def test_a_browser_cannot_rewrite_an_immutable_source_record(tmp_path):
    owner, key, journal, objects = fixture()
    # Any changed ciphertext is rejected before application, even with matching digest lookup.
    oid = next(iter(objects))
    objects[oid] = objects[oid][:-1] + bytes([objects[oid][-1] ^ 1])
    with pytest.raises(ValueError):
        import_journal(
            journal, objects.__getitem__, key, owner.signing_public(), tmp_path / "authoring"
        )
    assert not (tmp_path / "authoring").exists()


def test_paged_snapshot_import_verifies_pages_and_resumes(tmp_path):
    owner, key, journal, objects = fixture(revisions=5)
    pages = []
    root = tmp_path / "snapshot"
    root.mkdir(mode=0o700)
    for number, start in enumerate(range(0, 5, 2)):
        entries = journal["revisions"][start : start + 2]
        raw = b"".join(canonical(e) + b"\n" for e in entries)
        name = f"journal-pages/{number:08}.jsonl"
        (root / "journal-pages").mkdir(exist_ok=True)
        (root / name).write_bytes(raw)
        pages.append(
            {
                "file": name,
                "sha256": digest(raw),
                "bytes": len(raw),
                "entries": len(entries),
                "first": entries[0]["seq"],
                "last": entries[-1]["seq"],
            }
        )
    manifest = {**journal, "schema": "knovv-journal.v2", "cutoff": 5, "pages": pages}
    manifest.pop("revisions")
    result = import_snapshot(
        manifest,
        root,
        objects.__getitem__,
        key,
        owner.signing_public(),
        tmp_path / "home",
        max_entries=1,
    )
    assert result["imported"] == 5 and result["cursor"] == 5 and result["has_more"] is False
    assert (
        import_snapshot(
            manifest, root, objects.__getitem__, key, owner.signing_public(), tmp_path / "home"
        )["imported"]
        == 0
    )
    (root / pages[0]["file"]).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="journal-page-integrity"):
        import_snapshot(
            manifest, root, objects.__getitem__, key, owner.signing_public(), tmp_path / "bad"
        )
    assert not (tmp_path / "bad").exists()
