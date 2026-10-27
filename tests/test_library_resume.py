import hashlib
import json
from pathlib import Path

import pytest

from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.library_resume import prepare_resume
from conversation_corpus_engine.library_store import Library, digest_json


def candidate(tmp_path: Path):
    generation = tmp_path / "library-all/generations/generation-failed"
    generation.mkdir(parents=True)
    with Library.create(generation / "library.sqlite") as library:
        library.put(
            Conversation(
                provider="chatgpt",
                source_id="one",
                title="Preserved",
                messages=[Message(role="user", text="Original evidence")],
            )
        )
    manifest = {
        "schema": "cce.library-sources.v1",
        "estate_databases": ["/native/inventory.sqlite"],
    }
    (generation / "sources.json").write_text(json.dumps(manifest))
    report = {
        "schema": "cce.library-build.v1",
        "generation": str(generation),
        "base_pointer": None,
        "complete": False,
        "source_manifest_hash": digest_json(manifest),
        "accepted_observations": 3,
        "sources": [
            {"kind": "normalized_archive", "records": 1},
            {"kind": "native_estate", "path": "/native/inventory.sqlite", "report": {"records": 1}},
            {"kind": "documents", "report": {"counts": {"ingested": 0, "metadata_only": 1}}},
        ],
    }
    state = {"state": "failed", "error": "budget exceeded during indexing", "report": report}
    (generation / "build-state.json").write_text(json.dumps(state))
    return generation, state


def rewrite(generation, state):
    (generation / "build-state.json").write_text(json.dumps(state))


def test_qualified_seed_is_read_only_and_receipt_is_hash_bound(tmp_path):
    generation, state = candidate(tmp_path)
    before = {p.name: p.read_bytes() for p in generation.iterdir()}
    result = prepare_resume(generation, tmp_path, None, 1024**2)
    assert result["database"] == generation / "library.sqlite"
    assert result["report"] == state["report"]
    assert result["receipt"]["ingestion_proof"] == "legacy_completed_source_collection"
    assert hashlib.sha256(result["receipt_json"].encode()).hexdigest() == result["receipt_sha256"]
    assert before == {p.name: p.read_bytes() for p in generation.iterdir()}
    state["report"]["phase"] = "ingested"
    rewrite(generation, state)
    assert (
        prepare_resume(generation, tmp_path, None, 1024**2)["receipt"]["ingestion_proof"]
        == "explicit_phase"
    )


@pytest.mark.parametrize(
    "mutation", ["building", "counts", "documents", "phase", "base", "complete"]
)
def test_incomplete_active_or_rebased_candidates_are_rejected(tmp_path, mutation):
    generation, state = candidate(tmp_path)
    if mutation == "building":
        state["state"] = "building"
    if mutation == "counts":
        state["report"]["accepted_observations"] = 4
    if mutation == "documents":
        state["report"]["sources"].pop()
    if mutation == "phase":
        state["report"]["phase"] = "ingesting"
    if mutation == "base":
        state["report"]["base_pointer"] = {"generation": "other"}
    if mutation == "complete":
        state["report"]["complete"] = True
    rewrite(generation, state)
    with pytest.raises(ValueError):
        prepare_resume(generation, tmp_path, None, 1024**2)


def test_changed_manifest_current_pointer_and_sidecars_are_rejected(tmp_path):
    generation, state = candidate(tmp_path)
    (generation / "sources.json").write_text("{}")
    with pytest.raises(ValueError, match="manifest"):
        prepare_resume(generation, tmp_path, None, 1024**2)


def test_path_budget_and_callback_guards(tmp_path):
    generation, _ = candidate(tmp_path)
    alias = generation.parent / "alias"
    alias.symlink_to(generation, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        prepare_resume(alias, tmp_path, None, 1024**2)
    with pytest.raises(ValueError, match="byte limit"):
        prepare_resume(generation, tmp_path, None, 1)

    def stop():
        raise ValueError("deadline exceeded")

    with pytest.raises(ValueError, match="deadline"):
        prepare_resume(generation, tmp_path, None, 1024**2, check_callback=stop)
    (generation / "library.sqlite-wal").write_bytes(b"unfinished")
    with pytest.raises(ValueError, match="sidecar"):
        prepare_resume(generation, tmp_path, None, 1024**2)


def test_corrupt_normalized_object_cannot_be_resumed(tmp_path):
    generation, _ = candidate(tmp_path)
    with Library(generation / "library.sqlite", writable=True) as library:
        library.connection.execute("UPDATE objects SET payload=?", (b"broken",))
        library.connection.commit()
    with pytest.raises(ValueError, match="verification"):
        prepare_resume(generation, tmp_path, None, 1024**2)
