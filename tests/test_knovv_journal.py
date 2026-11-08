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
from conversation_corpus_engine.knovv_journal import import_journal


def fixture(attachment=False):
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
    return owner, key, journal, objects


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
