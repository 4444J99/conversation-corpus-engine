import hashlib
import json
from pathlib import Path

import pytest
from test_library_build import setup_sources

from conversation_corpus_engine import library_build
from conversation_corpus_engine.library_store import Library


def failed_export(tmp_path, monkeypatch):
    project, store, original = setup_sources(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(
            library_build.library_export,
            "verify_export",
            lambda *a, **kw: ["synthetic export failure"],
        )
        with pytest.raises(ValueError, match="synthetic export failure"):
            library_build.build_library(original, project_root=project, store_root=store)
    seed = next((store / "library-all/generations").iterdir())
    return project, store, original, seed


def snapshot(root):
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def export_manifest(seed):
    return {
        "schema": "cce.library-sources.v1",
        "resume_generation": str(seed),
        "resume_mode": "export",
        "limits": {"max_output_bytes": 16 * 1024**2},
    }


def test_export_resume_preserves_seed_without_ingestion_or_reindex(tmp_path, monkeypatch):
    project, store, original, seed = failed_export(tmp_path, monkeypatch)
    before = snapshot(seed)
    Path(original["archives"][0]).rename(tmp_path / "offline-archive")
    Path(original["documents"][0]["root"]).rename(tmp_path / "offline-writing")

    def forbidden(*args, **kwargs):
        raise AssertionError("export-only recovery must not ingest or index")

    monkeypatch.setattr(Library, "index_blocks", forbidden)
    monkeypatch.setattr(library_build, "iter_documents", forbidden)
    monkeypatch.setattr(library_build, "iter_estate", forbidden)
    result = library_build.build_library(
        export_manifest(seed), project_root=project, store_root=store
    )
    destination = Path(result["generation"])
    assert destination != seed
    assert result["complete"] is True
    assert result["status"]["records"] == 2
    assert result["new_observations"] == 0
    assert result["resume_source"]["receipt"]["resume_mode"] == "export"
    assert result["resume_copy"]["method"] in {"copy_on_write", "bounded_copy"}
    assert (
        result["resume_copy"]["source_hashes_before"]
        == result["resume_copy"]["source_hashes_after"]
    )
    assert result["resume_copy"]["copied_database_sha256"] == before["library.sqlite"]
    assert result["artifacts"]["library.sqlite"]["sha256"] == before["library.sqlite"]
    assert snapshot(seed) == before
    assert library_build.current_generation(store) is None
    assert (
        library_build.publish_library(destination, project_root=project, store_root=store)[
            "changed"
        ]
        is True
    )


@pytest.mark.parametrize(
    "field", ["archives", "snapshot_archives", "estate_databases", "documents"]
)
def test_export_resume_rejects_new_sources(tmp_path, monkeypatch, field):
    project, store, _, seed = failed_export(tmp_path, monkeypatch)
    manifest = export_manifest(seed)
    manifest[field] = [{"root": str(tmp_path)}] if field == "documents" else [str(tmp_path)]
    with pytest.raises(ValueError, match="export.*new source"):
        library_build.build_library(manifest, project_root=project, store_root=store)


@pytest.mark.parametrize("mutation", ["phase", "status", "fingerprint"])
def test_export_resume_requires_proven_completed_index(tmp_path, monkeypatch, mutation):
    project, store, _, seed = failed_export(tmp_path, monkeypatch)
    state = json.loads((seed / "build-state.json").read_text())
    if mutation == "phase":
        state["report"]["phase"] = "indexing"
    elif mutation == "status":
        state["report"]["status"]["blocks"] += 1
    else:
        state["report"]["record_fingerprint"] = "wrong"
    (seed / "build-state.json").write_text(json.dumps(state))
    with pytest.raises(ValueError, match="completed index|fingerprint|status"):
        library_build.build_library(export_manifest(seed), project_root=project, store_root=store)


def test_indexed_resume_ingests_supplement_and_reindexes_only_changed_records(
    tmp_path, monkeypatch
):
    project, store, original, seed = failed_export(tmp_path, monkeypatch)
    before = snapshot(seed)
    archive = tmp_path / "supplement" / "provider-chatgpt"
    archive.mkdir(parents=True)
    old = Path(original["archives"][0]) / "provider-chatgpt/collection-chatgpt-conversations.jsonl"
    item = json.loads(old.read_text())
    item["messages"][0]["text"] = (
        "Recovered body contains previously unavailable evidence for incremental indexing."
    )
    (archive / "collection-recovered-conversations.jsonl").write_text(json.dumps(item) + "\n")
    index_blocks = Library.index_blocks
    calls = []

    def tracked_index(self, **kwargs):
        calls.append(kwargs.get("uids"))
        return index_blocks(self, **kwargs)

    monkeypatch.setattr(Library, "index_blocks", tracked_index)
    manifest = export_manifest(seed)
    manifest.update(resume_mode="indexed", archives=[str(archive.parent)])
    result = library_build.build_library(manifest, project_root=project, store_root=store)
    assert len(calls) == 1 and calls[0] is not None and len(calls[0]) == 1
    assert result["new_observations"] == 1
    assert result["status"]["records"] == 2
    assert result["status"]["revisions"] == 3
    assert result["resume_copy"]["method"] in {"copy_on_write", "bounded_copy"}
    with Library(Path(result["generation"]) / "library.sqlite") as library:
        assert library.search("Recovered body")["total"] == 1
    assert snapshot(seed) == before


def test_export_resume_rechecks_source_after_clone(tmp_path, monkeypatch):
    project, store, _, seed = failed_export(tmp_path, monkeypatch)
    copy_database = library_build.copy_qualified_database

    def mutate_source(source, destination, **kwargs):
        result = copy_database(source, destination, **kwargs)
        with source.open("ab") as stream:
            stream.write(b"changed after clone")
        return result

    monkeypatch.setattr(library_build, "copy_qualified_database", mutate_source)
    with pytest.raises(ValueError, match="seed hash changed"):
        library_build.build_library(export_manifest(seed), project_root=project, store_root=store)
    assert library_build.current_generation(store) is None
