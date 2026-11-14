"""Synthetic source→authorship→reviewed Zettel→independent encrypted Git clone."""

import json
import os
import shutil
import subprocess

import pytest

from conversation_corpus_engine import archive_sealed as sealed
from conversation_corpus_engine.archive_model import Conversation, Message, sha256_file
from conversation_corpus_engine.attributed_knowledge import (
    collect_owner_segments,
    export_zettels,
    resolve_source_span,
    zettel_atoms,
)
from conversation_corpus_engine.attribution import ATTRIBUTION_SCHEMA, text_digest
from conversation_corpus_engine.library_store import Library


def _annotation(text, pieces):
    cursor, spans = 0, []
    for part, author in pieces:
        assert text[cursor : cursor + len(part)] == part
        spans.append(
            {
                "start": cursor,
                "end": cursor + len(part),
                "author": author,
                "author_id": "owner" if author == "owner" else author,
                "evidence": ["synthetic review fixture"],
                "assertion_status": "asserted",
                "profile_eligible": author == "owner",
            }
        )
        cursor += len(part)
    return {"schema": ATTRIBUTION_SCHEMA, "text_digest": text_digest(text), "spans": spans}


def test_six_source_cases_keep_authorship_and_exact_revisions_through_encrypted_clone(tmp_path):
    source = tmp_path / "private-source"
    source.mkdir()
    # Roles and app ownership alone never decide the original author.
    cases = [
        (
            "chatgpt",
            "prompt",
            "user",
            [("My question café. ", "owner"), ("Quoted passage.", "third_party")],
        ),
        (
            "applenotes",
            "note",
            "user",
            [("My note 😀. ", "owner"), ("Saved model output.", "agent")],
        ),
        ("chatgpt", "reply", "assistant", [("An agent response.", "agent")]),
        ("documents", "article", "user", [("A clipped article.", "third_party")]),
        (
            "googledrive",
            "draft",
            "user",
            [("My draft. ", "owner"), ("A collaborator quote.", "third_party")],
        ),
        ("documents", "import", "user", [("Unattributed imported text.", "unknown")]),
    ]
    annotations, zettels = [], []
    with Library.create(source / "library.sqlite") as library:
        for provider, identifier, role, pieces in cases:
            text = "".join(part for part, _ in pieces)
            record = Conversation(
                provider=provider,
                source_id=identifier,
                title=identifier,
                created="2026-01-01T00:00:00Z",
                messages=[
                    Message(
                        role=role,
                        text=text,
                        id="m1",
                        channel="typed" if role == "user" else "reply",
                    )
                ],
            )
            library.put(record)
            revision = library.history(record.uid)[0]
            reference = {
                "uid": record.uid,
                "version": revision["version"],
                "revision_digest": revision["digest"],
                "sequence": "main",
                "message": 0,
                "message_id": "m1",
                "text_digest": text_digest(text),
                "start": 0,
                "end": len(text),
            }
            annotation = _annotation(text, pieces) if identifier != "import" else None
            entry = {"source": reference, "attribution": annotation}
            annotations.append(entry)
            body = f"Reviewed idea from {identifier}."
            zettels.append(
                {
                    "id": f"idea:{identifier}",
                    "title": f"Idea {identifier}",
                    "body": body,
                    "attribution": _annotation(body, [(body, "owner")]),
                    "review": {
                        "status": "reviewed",
                        "reviewer": "synthetic-owner",
                        "evidence": ["fixture review"],
                    },
                    "sources": [entry],
                    "relationships": [{"type": "related", "target": "idea:prompt"}]
                    if identifier != "prompt"
                    else [],
                }
            )
        owner_segments, others = collect_owner_segments(library, annotations, "owner")
        assert owner_segments == ["My question café. ", "My note 😀. ", "My draft. "]
        assert len(others) == 6
        exported = export_zettels(library, zettels)
        atoms = zettel_atoms(library, zettels)
        # Accept a newer source revision; exported citations must continue resolving the old one.
        changed = Conversation.from_dict(library.record(annotations[0]["source"]["uid"]))
        changed.messages[0].text = "A later revision."
        library.put(changed)
        assert resolve_source_span(library, annotations[0]["source"])["text"].startswith(
            "My question café."
        )
    (source / "reviewed.atoms.jsonl").write_text(
        "".join(json.dumps(atom, ensure_ascii=False) + "\n" for atom in atoms), encoding="utf-8"
    )
    (source / "annotations.json").write_text(
        json.dumps(annotations, ensure_ascii=False), encoding="utf-8"
    )
    selection = ["library.sqlite", "reviewed.atoms.jsonl", "annotations.json"]
    expected_hashes = {name: sha256_file(source / name) for name in selection}
    owner = sealed.create_identity(
        tmp_path / "owner.key", "knovv-test", "owner", passphrase=b"synthetic-unlock"
    )
    device = sealed.create_identity(
        tmp_path / "device.key", "knovv-test", "reader", passphrase=b"synthetic-device"
    )
    wrong = sealed.create_identity(
        tmp_path / "wrong.key", "knovv-test", "reader", passphrase=b"synthetic-wrong"
    )
    trust = sealed.create_trust(tmp_path / "trust.json", owner)
    repo = tmp_path / "encrypted"
    recipients = [owner.recipient(), device.recipient()]
    plan = sealed.plan_capacity(source, selection, repo, owner, trust, recipients)
    assert plan["fits"] and not repo.exists()
    assert sealed.publish(source, selection, repo, owner, trust, recipients)["units"] == 6
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Synthetic",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Synthetic",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    for args in [
        ("init", "-b", "main"),
        ("add", "."),
        ("commit", "-m", "chore: update encrypted archive"),
    ]:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)
    clone = tmp_path / "independent-clone"
    subprocess.run(
        ["git", "clone", "--no-local", str(repo), str(clone)], check=True, capture_output=True
    )
    checkpoint = tmp_path / "device-trust.json"
    shutil.copyfile(trust, checkpoint)
    with pytest.raises(sealed.SealedError, match="not-authorized"):
        sealed.verify(clone, wrong, checkpoint)
    assert sealed.verify(clone, device, checkpoint)["units"] == 6
    assert set(sealed.list_units(clone, device, checkpoint)) == {atom["id"] for atom in atoms}
    for atom in atoms:
        assert sealed.read_unit(clone, atom["id"], device, checkpoint) == atom
    restored = tmp_path / "restored-private"
    sealed.restore(clone, restored, device, checkpoint)
    assert {name: sha256_file(restored / name) for name in selection} == expected_hashes
    with Library(restored / "library.sqlite") as library:
        assert export_zettels(library, zettels) == exported
        assert collect_owner_segments(library, annotations, "owner")[0] == owner_segments
    ciphertext = b"".join(
        path.read_bytes()
        for path in clone.rglob("*")
        if path.is_file() and ".git" not in path.parts
    )
    assert b"idea:prompt" not in ciphertext and b"Saved model output" not in ciphertext
