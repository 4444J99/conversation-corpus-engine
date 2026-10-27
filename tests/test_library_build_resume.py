import hashlib
import json
from pathlib import Path

import pytest
from test_library_build import setup_sources

from conversation_corpus_engine import library_build
from conversation_corpus_engine.library_store import Library


def failed_seed(tmp_path, monkeypatch):
    project, store, manifest = setup_sources(tmp_path)

    def fail_after_ingestion(library, **kwargs):
        checkpoint = json.loads((library.path.parent / "build-state.json").read_text())
        assert checkpoint["phase"] == "ingested"
        assert checkpoint["report"]["accepted_observations"] == 2
        raise ValueError("synthetic index budget failure")

    with monkeypatch.context() as patch:
        patch.setattr(Library, "index_blocks", fail_after_ingestion)
        with pytest.raises(ValueError, match="synthetic index"):
            library_build.build_library(manifest, project_root=project, store_root=store)
    generation = next((store / "library-all/generations").iterdir())
    return project, store, manifest, generation


def hashes(root):
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.iterdir()
        if path.is_file()
    }


@pytest.mark.parametrize("with_finding", [False, True])
def test_resume_compacts_seed_without_reading_original_sources_and_keeps_evidence(
    tmp_path, monkeypatch, with_finding
):
    project, store, original, seed = failed_seed(tmp_path, monkeypatch)
    state = json.loads((seed / "build-state.json").read_text())
    state["report"]["coverage_complete"] = False
    if with_finding:
        state["report"]["findings"].append({"code": "original_unresolved_coverage"})
    (seed / "build-state.json").write_text(json.dumps(state))
    before = hashes(seed)
    Path(original["archives"][0]).rename(tmp_path / "offline-archive")
    Path(original["documents"][0]["root"]).rename(tmp_path / "offline-writing")
    supplement = tmp_path / "supplement"
    supplement.mkdir()
    (supplement / "new.md").write_text("New supplemental evidence")
    manifest = {
        "schema": "cce.library-sources.v1",
        "resume_generation": str(seed),
        "documents": [{"id": "supplement", "root": str(supplement), "include": ["**/*.md"]}],
        "limits": {"max_records": 3, "max_output_bytes": 16 * 1024**3, "max_seconds": 3600},
    }
    create = Library.create
    copied = []

    def tracked_create(cls, path, **kwargs):
        copied.append(kwargs)
        return create(path, **kwargs)

    def no_native_read(*args, **kwargs):
        raise AssertionError("resume must not re-read native sources")

    monkeypatch.setattr(Library, "create", classmethod(tracked_create))
    monkeypatch.setattr(library_build, "iter_estate", no_native_read)
    result = library_build.build_library(manifest, project_root=project, store_root=store)
    assert copied == [{"previous": seed / "library.sqlite", "compact_previous": True}]
    assert result["complete"] is True and result["coverage_complete"] is False
    assert result["accepted_observations"] == 3 and result["new_observations"] == 1
    assert result["baseline_accepted_observations"] == 2
    assert result["resume_source"]["report"] == state["report"]
    assert result["resume_source"]["receipt"]["database_sha256"] == before["library.sqlite"]
    assert result["sources"][0]["kind"] == "documents"
    assert result["sources"][0]["report"]["counts"]["ingested"] == 1
    assert result["status"]["records"] == 3
    assert hashes(seed) == before
    assert library_build.current_generation(store) is None


def test_resume_limits_include_baseline_plus_new_observations(tmp_path, monkeypatch):
    project, store, original, seed = failed_seed(tmp_path, monkeypatch)
    manifest = {
        "schema": "cce.library-sources.v1",
        "resume_generation": str(seed),
        "limits": {"max_records": 1},
    }
    with pytest.raises(ValueError, match="baseline.*record limit"):
        library_build.build_library(manifest, project_root=project, store_root=store)
    manifest["limits"]["max_records"] = 2
    manifest["documents"] = original["documents"]
    with pytest.raises(ValueError, match="record limit"):
        library_build.build_library(manifest, project_root=project, store_root=store)


def test_second_resume_keeps_prior_source_report_and_count_proofs(tmp_path, monkeypatch):
    project, store, _, first = failed_seed(tmp_path, monkeypatch)
    manifest = {"schema": "cce.library-sources.v1", "resume_generation": str(first)}

    def fail_again(*args, **kwargs):
        raise ValueError("second index budget failure")

    with monkeypatch.context() as patch:
        patch.setattr(Library, "index_blocks", fail_again)
        with pytest.raises(ValueError, match="second index"):
            library_build.build_library(manifest, project_root=project, store_root=store)
    second = next(path for path in (store / "library-all/generations").iterdir() if path != first)
    manifest["resume_generation"] = str(second)
    result = library_build.build_library(manifest, project_root=project, store_root=store)
    assert result["accepted_observations"] == 2
    assert result["new_observations"] == 0
    assert result["resume_source"]["report"]["resume_source"]["generation"] == str(first)
    assert result["status"]["records"] == 2


def test_seed_bytes_are_checked_again_after_compact_copy(tmp_path, monkeypatch):
    project, store, _, seed = failed_seed(tmp_path, monkeypatch)
    create = Library.create

    def changed_seed(cls, path, **kwargs):
        result = create(path, **kwargs)
        with (seed / "library.sqlite").open("ab") as stream:
            stream.write(b"changed after copy")
        return result

    monkeypatch.setattr(Library, "create", classmethod(changed_seed))
    manifest = {"schema": "cce.library-sources.v1", "resume_generation": str(seed)}
    with pytest.raises(ValueError, match="seed hash changed"):
        library_build.build_library(manifest, project_root=project, store_root=store)
    assert library_build.current_generation(store) is None


def test_resume_generation_must_be_explicit_absolute_path(tmp_path):
    project, store, _ = setup_sources(tmp_path)
    with pytest.raises(ValueError, match="explicit absolute"):
        library_build.build_library(
            {"schema": "cce.library-sources.v1", "resume_generation": "../seed"},
            project_root=project,
            store_root=store,
        )
