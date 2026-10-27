import json
from pathlib import Path

import pytest

from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.corpus_store import register_corpus_store
from conversation_corpus_engine.library_build import (
    build_library,
    current_generation,
    publish_library,
)
from conversation_corpus_engine.library_store import Library


def setup_sources(tmp_path):
    project, store, archive, documents = (
        tmp_path / name for name in ("project", "store", "archive", "writing")
    )
    for path in (project, store, archive, documents):
        path.mkdir()
    register_corpus_store(project, store)
    provider = archive / "provider-chatgpt"
    provider.mkdir()
    conversation = Conversation(
        provider="chatgpt",
        source_id="one",
        title="Lattice conversation",
        messages=[
            Message(role="user", text="A lattice of ideas connects the conversation to research.")
        ],
    )
    (provider / "collection-chatgpt-conversations.jsonl").write_text(
        json.dumps(conversation.to_dict()) + "\n"
    )
    (documents / "paper.md").write_text(
        "# Research\n\nA lattice connects this authored research to the conversation."
    )
    manifest = {
        "schema": "cce.library-sources.v1",
        "archives": [str(archive)],
        "documents": [
            {
                "id": "research",
                "root": str(documents),
                "include": ["**/*.md"],
                "wing": "authored",
                "lane": "research",
                "kind": "document",
            }
        ],
    }
    return project, store, manifest


def test_build_publish_and_reopen_share_both_wings(tmp_path):
    project, store, manifest = setup_sources(tmp_path)
    result = build_library(manifest, project_root=project, store_root=store)
    generation = Path(result["generation"])
    assert current_generation(store) is None
    assert result["status"]["by_wing"] == {"ai": 1, "authored": 1}
    assert (generation / "vault" / "Home.md").is_file()
    assert (generation / "notion-import.zip").is_file()
    assert (generation / "agent-entry.md").stat().st_size < 4096
    published = publish_library(generation, project_root=project, store_root=store)
    assert published["changed"] is True
    assert current_generation(store) == generation
    with Library(generation / "library.sqlite") as library:
        assert library.search("lattice")["total"] == 2
    assert publish_library(generation, project_root=project, store_root=store)["changed"] is False
    assert (generation.stat().st_mode & 0o777) == 0o700
    assert (generation / "library.sqlite").stat().st_mode & 0o777 == 0o600


def test_changed_source_adds_revision_and_retains_missing_source(tmp_path):
    project, store, manifest = setup_sources(tmp_path)
    first = build_library(manifest, project_root=project, store_root=store)
    publish_library(Path(first["generation"]), project_root=project, store_root=store)
    (Path(manifest["documents"][0]["root"]) / "paper.md").write_text("A revised lattice study.")
    second = build_library(manifest, project_root=project, store_root=store)
    with Library(Path(second["generation"]) / "library.sqlite") as library:
        uid = library.search("revised")["hits"][0]["uid"]
        assert len(library.history(uid)) == 2
        assert library.history(uid)[-1]["version"] == "1.1.0"
    publish_library(Path(second["generation"]), project_root=project, store_root=store)
    manifest["archives"] = [str(tmp_path / "missing")]
    third = build_library(manifest, project_root=project, store_root=store)
    assert third["status"]["records"] == 2
    assert any(f["code"] == "archive_missing" for f in third["findings"])
    assert current_generation(store) == Path(second["generation"])


def test_publication_refuses_modified_candidate_and_foreign_parent(tmp_path):
    project, store, manifest = setup_sources(tmp_path)
    first = build_library(manifest, project_root=project, store_root=store)
    generation = Path(first["generation"])
    (generation / "vault" / "Home.md").write_text("tampered")
    with pytest.raises(ValueError, match="hash"):
        publish_library(generation, project_root=project, store_root=store)
    assert current_generation(store) is None


def test_bounded_build_does_not_publish_partial_work(tmp_path):
    project, store, manifest = setup_sources(tmp_path)
    manifest["limits"] = {"max_records": 1}
    with pytest.raises(ValueError, match="record limit"):
        build_library(manifest, project_root=project, store_root=store)
    assert current_generation(store) is None
