"""Synthetic publication fixtures; never read operator stores or device credentials."""

import json
import os

import pytest

from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.archive_sealed_crypto import (
    Identity,
)
from conversation_corpus_engine.attribution import text_digest
from conversation_corpus_engine.knovv_projection import export_bundle, read_bundle, restore_bundle
from conversation_corpus_engine.library_store import Library


def fixture(tmp_path):
    library = Library.create(tmp_path / "library.sqlite")
    item = Conversation(
        provider="chatgpt",
        source_id="synthetic",
        title="Lattice inquiry",
        messages=[
            Message(role="user", text="A lattice remembers its sources.", id="m1", channel="typed")
        ],
        provenance={"source_file": "synthetic.json", "source_sha256": "a" * 64},
    )
    library.put(item)
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "Lattice.md").write_text(
        "---\ntitle: Lattice\nauthor: owner\n---\nA maintained explanation. [[Sources]]\n"
    )
    (wiki / "Sources.md").write_text("# Sources\nReturn to [[Lattice]].\n")
    owner = Identity("knovv", "synthetic-owner", os.urandom(32), os.urandom(32))
    return library, wiki, owner


def test_private_projection_is_source_bound_searchable_and_exactly_restorable(tmp_path):
    library, wiki, owner = fixture(tmp_path)
    original = (tmp_path / "library.sqlite").read_bytes()
    target = tmp_path / "encrypted"
    result = export_bundle(library, wiki, target, owner, [owner.recipient()])
    assert result["documents"] == 3
    head, manifest, secret = read_bundle(  # allow-secret: runtime-only
        target, owner, owner.signing_public()
    )  # allow-secret: runtime-only
    assert manifest["count"] == 3
    assert manifest["search"]
    restored = restore_bundle(target, tmp_path / "restored", owner, owner.signing_public())
    docs = [json.loads(p.read_text()) for p in (tmp_path / "restored").glob("*.json")]
    source = next(d for d in docs if d["kind"] == "source")
    assert source["body"] == "A lattice remembers its sources."
    assert source["sources"][0]["uid"] == "chatgpt-synthetic"
    assert source["sources"][0]["start"] == 0
    assert source["sources"][0]["end"] == len(source["body"])
    assert all(d["links"] for d in docs if d["kind"] == "wiki")
    assert restored["documents"] == 3
    assert (tmp_path / "library.sqlite").read_bytes() == original
    for path in target.rglob("*"):
        if path.is_file():
            assert b"lattice" not in path.read_bytes().lower()
            assert b"synthetic" not in path.read_bytes().lower()
    library.close()


def test_capacity_failure_cannot_publish_a_partial_head_and_originals_remain(tmp_path):
    library, wiki, owner = fixture(tmp_path)
    with pytest.raises(ValueError, match="budget"):
        export_bundle(library, wiki, tmp_path / "small", owner, [owner.recipient()], budget=1)
    assert not (tmp_path / "small" / "HEAD.knovv").exists()
    assert (wiki / "Lattice.md").is_file()
    library.close()


def test_tampered_publication_or_object_and_wrong_recipient_are_refused(tmp_path):
    library, wiki, owner = fixture(tmp_path)
    target = tmp_path / "encrypted"
    export_bundle(library, wiki, target, owner, [owner.recipient()])
    stranger = Identity("knovv", "synthetic-owner", os.urandom(32), os.urandom(32))
    with pytest.raises(ValueError):
        read_bundle(target, stranger, owner.signing_public())
    head = json.loads((target / "HEAD.knovv").read_bytes())
    body = target / "objects" / head["manifest"]
    raw = body.read_bytes()
    body.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
    with pytest.raises(ValueError):
        read_bundle(target, owner, owner.signing_public())
    library.close()


def test_original_source_revisions_remain_available_after_a_source_changes(tmp_path):
    library, wiki, owner = fixture(tmp_path)
    original = library.record("chatgpt-synthetic")
    updated = Conversation.from_dict(original)
    updated.messages[0].text = "A revised explanation keeps its earlier evidence."
    library.put(updated)
    target = tmp_path / "encrypted"
    export_bundle(library, wiki, target, owner, [owner.recipient()])
    restore_bundle(target, tmp_path / "restored", owner, owner.signing_public())
    docs = [json.loads(p.read_text()) for p in (tmp_path / "restored").glob("*.json")]
    sources = [d for d in docs if d["kind"] == "source"]
    assert {d["sources"][0]["version"] for d in sources} == {"1.0.0", "1.1.0"}
    assert any(d["body"] == "A lattice remembers its sources." for d in sources)
    assert all(d["source_history"] for d in sources)
    library.close()


def test_wiki_summaries_do_not_expose_internal_link_addresses(tmp_path):
    library, wiki, owner = fixture(tmp_path)
    (wiki / "Lattice.md").write_text("[Source](Sources.md#evidence)\n")
    export_bundle(library, wiki, tmp_path / "encrypted", owner, [owner.recipient()])
    restore_bundle(tmp_path / "encrypted", tmp_path / "restored", owner, owner.signing_public())
    docs = [json.loads(p.read_text()) for p in (tmp_path / "restored").glob("*.json")]
    assert all("/document/" not in d["summary"] for d in docs if d["kind"] == "wiki")
    lattice = next(d for d in docs if d["title"] == "Lattice")
    assert "#/document/" in lattice["body"] and lattice["links"]
    library.close()


def test_citation_digest_and_coordinates_are_validated_before_publication(tmp_path):
    library, wiki, owner = fixture(tmp_path)
    text = "A lattice remembers its sources."
    note = wiki / "Evidence.md"
    note.write_text(
        f"cce:library-{'a' * 64}/chatgpt-synthetic@1.0.0#m0:2-8 sha256:{text_digest(text)}\n"
    )
    export_bundle(library, wiki, tmp_path / "valid", owner, [owner.recipient()])
    note.write_text(note.read_text().replace("2-8", "2-999"))
    with pytest.raises(ValueError, match="invalid-wiki-citation"):
        export_bundle(library, wiki, tmp_path / "invalid", owner, [owner.recipient()])
    assert not (tmp_path / "invalid" / "HEAD.knovv").exists()
    library.close()


def test_packed_objects_use_bounded_locator_shards_and_reuse_unchanged_ciphertext(tmp_path):
    library, wiki, owner = fixture(tmp_path)
    for number in range(300):
        (wiki / f"Note-{number:04}.md").write_text(
            f"# Idea {number}\nA synthetic source-bound thought.\n"
        )
    first = tmp_path / "first"
    result = export_bundle(library, wiki, first, owner, [owner.recipient()])
    _, manifest, _ = read_bundle(first, owner, owner.signing_public())
    assert "content" not in manifest and manifest["resolve"]
    assert result["objects"] < result["documents"]
    second = tmp_path / "second"
    export_bundle(library, wiki, second, owner, [owner.recipient()], previous=first)
    _, updated, _ = read_bundle(second, owner, owner.signing_public())
    assert manifest["resolve"] == updated["resolve"]
    assert (
        restore_bundle(second, tmp_path / "restored-packed", owner, owner.signing_public())[
            "documents"
        ]
        == 303
    )
    library.close()


def test_explicit_rotation_excludes_a_removed_device_and_retains_historical_epoch_keys(tmp_path):
    library, wiki, owner = fixture(tmp_path)
    removed = Identity("knovv", "old-phone", os.urandom(32), os.urandom(32))
    first = tmp_path / "first"
    export_bundle(library, wiki, first, owner, [owner.recipient(), removed.recipient()])
    _, before, old_key = read_bundle(first, owner, owner.signing_public())
    second = tmp_path / "second"
    export_bundle(library, wiki, second, owner, [owner.recipient()], previous=first, rotate=True)
    head, manifest, new_key = read_bundle(second, owner, owner.signing_public())
    assert head["epoch"] == 2 and new_key != old_key
    assert manifest["id_key"] == before["id_key"]
    assert manifest["epoch_keys"]["1"] == old_key.hex()
    with pytest.raises(ValueError):
        read_bundle(second, removed, owner.signing_public())
    assert (
        restore_bundle(second, tmp_path / "restored-rotation", owner, owner.signing_public())[
            "documents"
        ]
        == 3
    )
    library.close()


def test_export_deadline_retains_sources_without_publishing_an_incomplete_head(tmp_path):
    library, wiki, owner = fixture(tmp_path)
    with pytest.raises(ValueError, match="publication-deadline"):
        export_bundle(
            library,
            wiki,
            tmp_path / "bounded",
            owner,
            [owner.recipient()],
            deadline_seconds=0.000001,
        )
    assert not (tmp_path / "bounded" / "HEAD.knovv").exists()
    assert (wiki / "Lattice.md").exists()
    library.close()
