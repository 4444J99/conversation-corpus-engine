"""The build writes the vault profile it is asked for, and none by default."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_library_build import setup_sources
from test_library_export_resume import export_manifest, failed_export

from conversation_corpus_engine import library_build
from conversation_corpus_engine.library_build import (
    build_library,
    current_generation,
    publish_library,
    verify_candidate,
)
from conversation_corpus_engine.library_export import verify_export

NON_VAULT_FILES = {
    "agent-entry.md",
    "build-state.json",
    "export-manifest.json",
    "graph.jsonl",
    "library-index.jsonl",
    "library.sqlite",
    "records.csv",
    "report.json",
    "sources.json",
}


def test_the_default_build_writes_no_vault_and_still_verifies_and_publishes(
    tmp_path: Path,
) -> None:
    project, store, manifest = setup_sources(tmp_path)
    report = build_library(manifest, project_root=project, store_root=store)
    generation = Path(report["generation"])
    assert report["vault_profile"] == "none"
    assert report["exports"]["profile"] == "none" and report["exports"]["vault"] is None
    assert {path.name for path in generation.iterdir()} == NON_VAULT_FILES
    assert not (generation / "vault").exists()
    assert "vault/Home.md" not in report["artifacts"]
    assert report["output_bytes"] == sum(
        path.stat().st_size for path in generation.rglob("*") if path.is_file()
    )
    assert verify_export(generation) == []
    assert verify_candidate(generation)["vault_profile"] == "none"
    published = publish_library(generation, project_root=project, store_root=store)
    assert published["changed"] is True and current_generation(store) == generation
    receipt = json.loads((generation / "report.json").read_text(encoding="utf-8"))
    assert receipt["vault_profile"] == "none"
    manifest_file = json.loads((generation / "export-manifest.json").read_text(encoding="utf-8"))
    assert manifest_file["profile"] == "none"


@pytest.mark.parametrize("profile", ["cards", "full"])
def test_a_requested_profile_is_written_verified_and_recorded(tmp_path: Path, profile: str) -> None:
    project, store, manifest = setup_sources(tmp_path)
    report = build_library(manifest, project_root=project, store_root=store, vault_profile=profile)
    generation = Path(report["generation"])
    assert report["vault_profile"] == profile and report["exports"]["profile"] == profile
    assert (generation / "vault" / "Home.md").is_file()
    assert (generation / "notion-import.zip").is_file()
    assert not (generation / "vault" / "blocks").exists()
    assert report["exports"]["vault_caps"]["ok"] is True
    assert "vault/Home.md" in report["artifacts"]
    assert verify_export(generation) == []
    assert verify_candidate(generation)["vault_profile"] == profile
    assert publish_library(generation, project_root=project, store_root=store)["changed"] is True
    manifest_file = json.loads((generation / "export-manifest.json").read_text(encoding="utf-8"))
    assert manifest_file["profile"] == profile


def test_an_unknown_profile_is_refused_before_anything_is_created(tmp_path: Path) -> None:
    project, store, manifest = setup_sources(tmp_path)
    with pytest.raises(ValueError, match="none, cards, full"):
        build_library(manifest, project_root=project, store_root=store, vault_profile="legacy")
    assert not (store / "library-all").exists()


def test_a_tampered_vault_file_is_still_refused_at_publication(tmp_path: Path) -> None:
    project, store, manifest = setup_sources(tmp_path)
    report = build_library(manifest, project_root=project, store_root=store, vault_profile="cards")
    generation = Path(report["generation"])
    (generation / "vault" / "Home.md").write_text("tampered")
    with pytest.raises(ValueError, match="hash"):
        publish_library(generation, project_root=project, store_root=store)


@pytest.mark.parametrize("profile", ["none", "cards", "full"])
def test_every_profile_reports_the_export_and_verify_stages(tmp_path: Path, profile: str) -> None:
    project, store, manifest = setup_sources(tmp_path)
    events: list[dict] = []
    build_library(
        manifest,
        project_root=project,
        store_root=store,
        progress=events.append,
        vault_profile=profile,
    )
    stages = [event["stage"] for event in events if "stage" in event]
    assert stages.index("export") < stages.index("verify_export") < stages.index("artifacts")


def test_export_resume_builds_the_requested_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, store, _, seed = failed_export(tmp_path, monkeypatch)
    report = library_build.build_library(
        export_manifest(seed), project_root=project, store_root=store, vault_profile="cards"
    )
    generation = Path(report["generation"])
    assert report["vault_profile"] == "cards" and (generation / "vault" / "cards").is_dir()
    assert verify_export(generation) == []
