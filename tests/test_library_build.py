import json
from pathlib import Path

import pytest

from conversation_corpus_engine import library_build
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
    assert result["output_bytes"] == sum(
        path.stat().st_size for path in generation.rglob("*") if path.is_file()
    )
    published = publish_library(generation, project_root=project, store_root=store)
    assert published["changed"] is True
    assert current_generation(store) == generation
    with Library(generation / "library.sqlite") as library:
        assert library.search("lattice")["total"] == 2
    assert publish_library(generation, project_root=project, store_root=store)["changed"] is False
    assert (generation.stat().st_mode & 0o777) == 0o700
    assert (generation / "library.sqlite").stat().st_mode & 0o777 == 0o600


def test_build_reuses_completed_index_summary(tmp_path, monkeypatch):
    project, store, manifest = setup_sources(tmp_path)
    original = Library.status
    summaries = []

    def observed_status(self, **kwargs):
        result = original(self, **kwargs)
        summaries.append(result)
        return result

    monkeypatch.setattr(Library, "status", observed_status)
    result = build_library(manifest, project_root=project, store_root=store)
    assert result["status"]["records"] == 2
    assert len(summaries) == 1
    assert result["status"] == summaries[0]


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


@pytest.mark.parametrize("extra", ["file", "symlink", "directory_symlink"])
def test_publication_refuses_unlisted_files_and_symlinks(tmp_path, extra):
    project, store, manifest = setup_sources(tmp_path)
    result = build_library(manifest, project_root=project, store_root=store)
    generation = Path(result["generation"])
    unexpected = generation / "vault" / "unverified.md"
    if extra == "file":
        unexpected.write_text("unverified source")
    else:
        unexpected.symlink_to(
            generation / ("vault" if extra == "directory_symlink" else "sources.json")
        )
    with pytest.raises(ValueError, match="unexpected|symlink"):
        publish_library(generation, project_root=project, store_root=store)
    assert current_generation(store) is None


def test_source_inventory_receipts_cannot_exceed_output_budget(tmp_path):
    project, store, manifest = setup_sources(tmp_path)
    document_root = Path(manifest["documents"][0]["root"])
    manifest["documents"][0]["exclude"] = ["**/excluded-*.md"]
    for index in range(2500):
        (document_root / f"excluded-{index}.md").touch()
    manifest["limits"] = {"max_output_bytes": 262144}
    with pytest.raises(ValueError, match="output byte limit"):
        build_library(manifest, project_root=project, store_root=store)
    assert current_generation(store) is None
    generations = list((store / "library-all/generations").iterdir())
    assert len(generations) == 1
    total = sum(path.stat().st_size for path in generations[0].rglob("*") if path.is_file())
    assert total <= manifest["limits"]["max_output_bytes"]
    failure = json.loads((generations[0] / "build-state.json").read_text())
    assert failure["state"] == "failed"
    assert failure["resumable"] is False and failure["report_omitted"] is True
    assert (generations[0] / "build-state.json").stat().st_size < 4096
    assert not (generations[0] / "report.json").exists()


def test_oversized_initial_manifest_leaves_bounded_failure_evidence(tmp_path):
    project, store, manifest = setup_sources(tmp_path)
    manifest["description"] = "x" * 300000
    manifest["limits"] = {"max_output_bytes": 262144}
    with pytest.raises(ValueError, match="output byte limit"):
        build_library(manifest, project_root=project, store_root=store)
    generation = next((store / "library-all/generations").iterdir())
    assert sum(p.stat().st_size for p in generation.rglob("*") if p.is_file()) <= 262144
    assert json.loads((generation / "build-state.json").read_text())["state"] == "failed"
    assert not (generation / "library.sqlite").exists()


def test_publication_checks_receipts_and_failed_state(tmp_path):
    project, store, manifest = setup_sources(tmp_path)
    result = build_library(manifest, project_root=project, store_root=store)
    generation = Path(result["generation"])
    report = json.loads((generation / "report.json").read_text())
    report["limits"]["max_output_bytes"] = report["artifact_bytes"]
    (generation / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    with pytest.raises(ValueError, match="output byte limit"):
        publish_library(generation, project_root=project, store_root=store)
    report["limits"]["max_output_bytes"] = 8 * 1024**3
    (generation / "report.json").write_text(json.dumps(report))
    (generation / "build-state.json").write_text(json.dumps({"state": "failed"}))
    with pytest.raises(ValueError, match="not verified"):
        publish_library(generation, project_root=project, store_root=store)
    assert current_generation(store) is None


def test_final_report_budget_failure_never_leaves_publishable_candidate(tmp_path, monkeypatch):
    project, store, manifest = setup_sources(tmp_path)
    manifest["limits"] = {"max_output_bytes": 262144}
    original = library_build.library_export.export_library

    def oversized_report(*args, **kwargs):
        return {**original(*args, **kwargs), "diagnostic": "x" * 300000}

    monkeypatch.setattr(library_build.library_export, "export_library", oversized_report)
    with pytest.raises(ValueError, match="output byte limit"):
        build_library(manifest, project_root=project, store_root=store)
    generation = next((store / "library-all/generations").iterdir())
    assert sum(p.stat().st_size for p in generation.rglob("*") if p.is_file()) <= 262144
    state = json.loads((generation / "build-state.json").read_text())
    assert state["state"] == "failed" and state["resumable"] is False
    assert not (generation / "report.json").exists()
    assert current_generation(store) is None


def test_direct_publication_accepts_relative_paths(tmp_path, monkeypatch):
    project, store, manifest = setup_sources(tmp_path)
    result = build_library(manifest, project_root=project, store_root=store)
    monkeypatch.chdir(tmp_path)
    published = publish_library(
        Path(result["generation"]).relative_to(tmp_path),
        project_root=Path("project"),
        store_root=Path("store"),
    )
    assert published["changed"] is True


def test_historical_archive_adds_snapshot_without_replacing_current_body(tmp_path):
    project, store, manifest = setup_sources(tmp_path)
    first = build_library(manifest, project_root=project, store_root=store)
    publish_library(Path(first["generation"]), project_root=project, store_root=store)
    source = (
        Path(manifest["archives"][0])
        / "provider-chatgpt"
        / "collection-chatgpt-conversations.jsonl"
    )
    historical = json.loads(source.read_text())
    historical["messages"][0]["text"] = "An older source variant omitted by a first-wins importer."
    source.write_text(json.dumps(historical) + "\n")
    manifest["snapshot_archives"] = manifest.pop("archives")
    second = build_library(manifest, project_root=project, store_root=store)
    with Library(Path(second["generation"]) / "library.sqlite") as library:
        uid = Conversation.from_dict(historical).uid
        assert "lattice" in library.record(uid)["messages"][0]["text"]
        assert len(library.history(uid)) == 1
        snapshots = library.snapshots(uid)
        assert len(snapshots) == 2
        old = next(row for row in snapshots if row["disposition"] == "historical_snapshot")
        assert (
            "older source variant"
            in library.record(uid, digest=old["digest"])["messages"][0]["text"]
        )
    assert second["inherited_report"]["path"] == str(Path(first["generation"]) / "report.json")
