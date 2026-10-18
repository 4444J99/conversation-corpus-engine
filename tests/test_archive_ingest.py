from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from archive_support import conv, gpt_conversation, topical_corpus, write_store

from conversation_corpus_engine.archive_ingest import (
    ArchivePaths,
    authorize_archive,
    failed_providers,
    ingest,
    iter_store,
    load_index,
    load_manifest,
    require_inside,
    resolve_sources,
)
from conversation_corpus_engine.archive_topics import build_analysis, load_enrichment
from conversation_corpus_engine.corpus_store import CorpusStoreError, register_corpus_store


def test_ingest_builds_store_manifest_and_denominators(archive_env) -> None:
    manifest = ingest(
        export_root=archive_env.export, paths=archive_env.paths, authorization=archive_env.auth
    )
    providers = manifest["providers"]
    assert {n: i["status"] for n, i in providers.items()} == dict.fromkeys(
        ["chatgpt", "claude", "gemini", "notebooklm", "grok", "perplexity"], "ok"
    )
    assert providers["chatgpt"]["conversations"] == 3
    assert providers["chatgpt"]["empty_conversations"] == 1
    assert providers["claude"]["kinds"] == {"chat": 3, "memory": 1, "notebook": 1}
    assert providers["gemini"]["reader_counters"]["skipped_deleted_turns"] == 1
    assert providers["notebooklm"]["conversations"] == 1
    assert manifest["totals"]["conversations"] == sum(
        i["conversations"] for i in providers.values()
    )
    assert providers["chatgpt"]["source_files"]["conversations-000.json"]
    assert providers["gemini"]["channels"]["not_read"]
    assert load_manifest(archive_env.paths) == manifest
    assert len(load_index(archive_env.paths)) == manifest["totals"]["conversations"]
    assert sum(1 for _ in iter_store(archive_env.paths)) == manifest["totals"]["conversations"]


def test_ingest_is_byte_identical_on_rerun(archive_env) -> None:
    kwargs = {
        "export_root": archive_env.export,
        "paths": archive_env.paths,
        "authorization": archive_env.auth,
    }
    ingest(**kwargs)
    snapshot = {
        p.relative_to(archive_env.paths.root): p.read_bytes()
        for p in archive_env.paths.root.rglob("*")
        if p.is_file() and p.name != "run.json"
    }
    ingest(**kwargs)
    again = {
        p.relative_to(archive_env.paths.root): p.read_bytes()
        for p in archive_env.paths.root.rglob("*")
        if p.is_file() and p.name != "run.json"
    }
    assert snapshot == again


def test_ingest_isolates_a_failing_provider(archive_env) -> None:
    bad = archive_env.export / "perplexity" / "conversations-20250101.json"
    bad.write_text("{not json", encoding="utf-8")
    manifest = ingest(
        export_root=archive_env.export, paths=archive_env.paths, authorization=archive_env.auth
    )
    assert failed_providers(manifest) == ["perplexity"]
    assert manifest["providers"]["perplexity"]["error"].startswith("JSONDecodeError")
    assert manifest["providers"]["chatgpt"]["status"] == "ok"
    assert not archive_env.paths.conversations("perplexity").exists()
    assert not list(archive_env.paths.root.rglob(".*.tmp"))


def test_ingest_reports_absent_providers_without_inventing_them(
    archive_env, tmp_path: Path
) -> None:
    empty = tmp_path / "bare-export"
    empty.mkdir()
    manifest = ingest(
        export_root=empty,
        paths=archive_env.paths,
        authorization=archive_env.auth,
        providers=["grok", "chatgpt"],
    )
    assert {n: i["status"] for n, i in manifest["providers"].items()} == {
        "grok": "absent",
        "chatgpt": "absent",
    }
    assert manifest["totals"]["conversations"] == 0


def test_ingest_dedupes_by_uid_keeping_first(archive_env, tmp_path: Path) -> None:
    shutil.copytree(archive_env.export / "chatgpt", tmp_path / "gpt")
    (tmp_path / "gpt" / "conversations-001.json").write_text(
        json.dumps([gpt_conversation("aaaa1111", "Dup", [("user", "dup")])]), encoding="utf-8"
    )
    manifest = ingest(
        export_root=tmp_path,
        paths=archive_env.paths,
        authorization=archive_env.auth,
        providers=["chatgpt"],
        overrides={"chatgpt": tmp_path / "gpt"},
    )
    assert manifest["providers"]["chatgpt"]["duplicates_skipped"] == 1
    assert manifest["providers"]["chatgpt"]["conversations"] == 3


def test_resolve_sources_rejects_unknown_provider(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        resolve_sources(tmp_path, ["nope"])
    sources = resolve_sources(tmp_path, ["notebooklm"])
    assert sources["notebooklm"] == (tmp_path / "gemini" / "NotebookLM").resolve()


def test_custody_refuses_unregistered_overlapping_and_escaping_destinations(
    archive_env, tmp_path: Path
) -> None:
    with pytest.raises(CorpusStoreError):
        authorize_archive(
            project_root=archive_env.project,
            archive_root=archive_env.store.parent / "elsewhere",
            corpus_store_root=archive_env.store,
        )
    with pytest.raises(CorpusStoreError):
        authorize_archive(
            project_root=archive_env.project,
            archive_root=archive_env.store / "_archive",
            corpus_store_root=archive_env.store,
            source_roots=(archive_env.store / "_archive" / "inside",),
        )
    other = tmp_path / "other-store"
    other.mkdir()
    with pytest.raises(CorpusStoreError):
        authorize_archive(
            project_root=archive_env.project,
            archive_root=other / "_archive",
            corpus_store_root=other,
        )
    with pytest.raises(CorpusStoreError):
        require_inside(archive_env.auth, archive_env.store / "sibling")
    assert require_inside(archive_env.auth, archive_env.paths.vault)


def test_custody_refuses_git_visible_store(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(CorpusStoreError):
        register_corpus_store(project, repo)


def test_topics_connect_documents_across_providers(archive_env) -> None:
    write_store(archive_env.paths, topical_corpus())
    summary = build_analysis(archive_env.paths)
    assert summary["documents"] == 12
    topics = json.loads((archive_env.paths.analysis / "topics.json").read_text(encoding="utf-8"))
    hubs = topics["hubs"]
    assert hubs["kubernetes"]["documents"] == 4
    assert set(hubs["kubernetes"]["providers"]) == {"chatgpt", "claude", "gemini", "grok"}
    assert hubs["sourdough"]["documents"] == 3
    enrichment = load_enrichment(archive_env.paths)
    assert "kubernetes" in enrichment["chatgpt-k0"]["topics"]
    similar = {s["uid"] for s in enrichment["chatgpt-k0"]["similar"]}
    assert similar == {"claude-k1", "gemini-k2", "grok-k3"}
    assert all(s["score"] >= 0.3 for s in enrichment["chatgpt-k0"]["similar"])
    assert enrichment["chatgpt-o0"]["similar"] == []


def test_topics_are_deterministic(archive_env) -> None:
    write_store(archive_env.paths, topical_corpus())
    build_analysis(archive_env.paths)
    first = (archive_env.paths.analysis / "enrichment.jsonl").read_bytes()
    build_analysis(archive_env.paths)
    assert (archive_env.paths.analysis / "enrichment.jsonl").read_bytes() == first


def test_topics_handle_an_empty_store(archive_env) -> None:
    summary = build_analysis(ArchivePaths(archive_env.paths.root))
    assert summary["documents"] == 0 and summary["hubs"] == 0
    write_store(archive_env.paths, [conv("grok", "solo", "Solo")])
    assert build_analysis(archive_env.paths)["documents"] == 1
