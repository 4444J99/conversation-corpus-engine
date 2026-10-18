from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from archive_support import (
    add_idless_chatgpt_shard,
    conv,
    gpt_conversation,
    topical_corpus,
    write_store,
)

from conversation_corpus_engine.archive_ingest import (
    DEFAULT_ARCHIVE_DIRNAME,
    PROVIDER_SPECS,
    ArchivePaths,
    authorize_archive,
    failed_providers,
    ingest,
    iter_store,
    load_index,
    load_manifest,
    require_inside,
    resolve_sources,
    source_fingerprint,
)
from conversation_corpus_engine.archive_naming import (
    NAMING_VERSION,
    conversation_name,
    validate_name,
)
from conversation_corpus_engine.archive_topics import build_analysis, load_enrichment
from conversation_corpus_engine.corpus_store import CorpusStoreError, register_corpus_store


def test_ingest_builds_store_manifest_and_denominators(archive_env) -> None:
    manifest = ingest(
        export_root=archive_env.export, paths=archive_env.paths, authorization=archive_env.auth
    )
    providers = manifest["providers"]
    assert {n: i["status"] for n, i in providers.items()} == {
        **dict.fromkeys(["chatgpt", "claude", "gemini", "notebooklm", "grok", "perplexity"], "ok"),
        "copilot": "absent",
    }
    assert providers["chatgpt"]["conversations"] == 3
    assert providers["chatgpt"]["empty_conversations"] == 1
    assert providers["claude"]["kinds"] == {"chat": 3, "memory": 1, "notebook": 1}
    assert providers["gemini"]["reader_counters"]["skipped_deleted_turns"] == 1
    assert providers["notebooklm"]["conversations"] == 1
    assert manifest["totals"]["conversations"] == sum(
        i.get("conversations", 0) for i in providers.values()
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
        if p.is_file()
    }
    ingest(**kwargs)
    again = {
        p.relative_to(archive_env.paths.root): p.read_bytes()
        for p in archive_env.paths.root.rglob("*")
        if p.is_file()
    }
    assert snapshot == again
    assert snapshot, "the second run compared something"


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
    info = manifest["providers"]["chatgpt"]
    assert info["duplicates_folded"] == 1 and info["conversations"] == 3
    # the repeat holds different content, and the first occurrence still wins
    assert (info["duplicates_folded_identical"], info["duplicates_folded_differing"]) == (0, 1)
    assert "duplicates_skipped" not in info
    kept = next(c for c in iter_store(archive_env.paths) if c.source_id == "aaaa1111")
    assert kept.title == "Kubernetes rollout planning"


def test_duplicates_are_classified_as_identical_or_differing(archive_env, tmp_path: Path) -> None:
    shard = tmp_path / "gpt"
    shard.mkdir()
    original = gpt_conversation("dup-a", "Original", [("user", "first"), ("assistant", "ok")])
    changed = gpt_conversation("dup-a", "Original", [("user", "edited"), ("assistant", "ok")])
    other = gpt_conversation("dup-b", "Other", [("user", "other ask")])
    (shard / "conversations-000.json").write_text(
        json.dumps([original, original, changed, other, other]), encoding="utf-8"
    )
    manifest = ingest(
        export_root=tmp_path,
        paths=archive_env.paths,
        authorization=archive_env.auth,
        providers=["chatgpt"],
        overrides={"chatgpt": shard},
    )
    info = manifest["providers"]["chatgpt"]
    assert info["conversations"] == 2
    assert (info["duplicates_folded_identical"], info["duplicates_folded_differing"]) == (2, 1)
    assert info["duplicates_folded"] == 3


def test_manifest_records_versions_and_per_provider_counters(archive_env) -> None:
    manifest = ingest(
        export_root=archive_env.export, paths=archive_env.paths, authorization=archive_env.auth
    )
    assert manifest["schema"] == "archive-manifest.v2"
    assert manifest["conversation_schema"] == "archive-conversation.v2"
    assert manifest["schema_version"] == 2
    assert manifest["naming_version"] == NAMING_VERSION
    assert manifest["reader_versions"] == {
        name: spec.module.READER for name, spec in PROVIDER_SPECS.items()
    }
    for info in manifest["providers"].values():
        if info["status"] == "absent":  # the shared export has no copilot/ folder
            continue
        assert info["derived_ids"] == 0 and info["duplicates_folded"] == 0
        assert info["unnameable_ids"] == 0
    assert len(manifest["source_fingerprint"]) == 64


def test_id_less_conversations_each_get_their_own_derived_id(archive_env) -> None:
    add_idless_chatgpt_shard(archive_env.export)
    manifest = ingest(
        export_root=archive_env.export,
        paths=archive_env.paths,
        authorization=archive_env.auth,
        providers=["chatgpt"],
    )
    info = manifest["providers"]["chatgpt"]
    assert info["conversations"] == 5
    assert info["derived_ids"] == 2 and info["duplicates_folded"] == 0
    derived = [c for c in iter_store(archive_env.paths) if c.source_id.startswith("derived-")]
    assert len(derived) == 2
    assert len({c.uid for c in derived}) == 2
    for conversation in derived:
        assert re.fullmatch(r"derived-[0-9a-f]{16}", conversation.source_id)
        assert conversation.extra["source_id_derived"] == "empty"
        assert conversation.messages and all(m.id for m in conversation.messages)
        assert not validate_name(conversation_name("chatgpt", conversation.uid))


def test_an_unnameable_vendor_id_is_replaced_and_counted_apart_from_empty_ones(
    archive_env, tmp_path: Path
) -> None:
    shard = tmp_path / "gpt"
    shard.mkdir()
    conversations = [
        gpt_conversation("a--b", "Doubled hyphen", [("user", "first ask")]),
        gpt_conversation("", "No id", [("user", "second ask")]),
        gpt_conversation("fine-id", "Fine", [("user", "third ask")]),
    ]
    (shard / "conversations-000.json").write_text(json.dumps(conversations), encoding="utf-8")
    manifest = ingest(
        export_root=tmp_path,
        paths=archive_env.paths,
        authorization=archive_env.auth,
        providers=["chatgpt"],
        overrides={"chatgpt": shard},
    )
    info = manifest["providers"]["chatgpt"]
    assert (info["conversations"], info["derived_ids"], info["unnameable_ids"]) == (3, 2, 1)
    stored = {c.title: c for c in iter_store(archive_env.paths)}
    assert stored["Doubled hyphen"].extra["vendor_id"] == "a--b"
    assert stored["Doubled hyphen"].extra["source_id_derived"] == "unnameable"
    assert stored["No id"].extra["source_id_derived"] == "empty"
    assert stored["Fine"].source_id == "fine-id"


def test_identical_id_less_conversations_fold_into_one(archive_env, tmp_path: Path) -> None:
    twin = gpt_conversation("", "Same", [("user", "same question"), ("assistant", "same")])
    shard = tmp_path / "gpt"
    shard.mkdir()
    (shard / "conversations-000.json").write_text(json.dumps([twin, twin]), encoding="utf-8")
    manifest = ingest(
        export_root=tmp_path,
        paths=archive_env.paths,
        authorization=archive_env.auth,
        providers=["chatgpt"],
        overrides={"chatgpt": shard},
    )
    info = manifest["providers"]["chatgpt"]
    assert (info["conversations"], info["derived_ids"], info["duplicates_folded"]) == (1, 1, 1)
    assert info["duplicates_folded_identical"] == 1 and info["duplicates_folded_differing"] == 0


def test_fingerprint_is_the_same_on_two_hosts_and_moves_with_the_source(
    archive_env, tmp_path: Path
) -> None:
    first = ingest(
        export_root=archive_env.export, paths=archive_env.paths, authorization=archive_env.auth
    )
    elsewhere = tmp_path / "second-host" / "deeper" / "export"
    shutil.copytree(archive_env.export, elsewhere)
    second = ingest(export_root=elsewhere, paths=archive_env.paths, authorization=archive_env.auth)
    assert (
        first["providers"]["chatgpt"]["source_root"]
        != second["providers"]["chatgpt"]["source_root"]
    )
    assert first["source_fingerprint"] == second["source_fingerprint"]
    (elsewhere / "perplexity" / "conversations-20250101.json").write_text("{}", encoding="utf-8")
    third = ingest(export_root=elsewhere, paths=archive_env.paths, authorization=archive_env.auth)
    assert third["source_fingerprint"] != first["source_fingerprint"]


def test_fingerprint_ignores_paths_and_errors_but_not_versions() -> None:
    providers = {"grok": {"status": "ok", "conversations": 1, "source_root": "/a/b"}}
    moved = {"grok": {"status": "ok", "conversations": 1, "source_root": "/other/place"}}
    versions = {"grok": "archive_read_grok@1"}
    base = source_fingerprint(providers, reader_versions=versions, naming_version=1)
    assert base == source_fingerprint(moved, reader_versions=versions, naming_version=1)
    failed = {"grok": {"status": "ok", "conversations": 1, "error": "x at /home/someone"}}
    assert base == source_fingerprint(failed, reader_versions=versions, naming_version=1)
    assert base != source_fingerprint(
        providers, reader_versions={"grok": "archive_read_grok@2"}, naming_version=1
    )
    assert base != source_fingerprint(providers, reader_versions=versions, naming_version=2)


def test_manifest_accepts_the_previous_schema_and_refuses_unknown_ones(archive_env) -> None:
    ingest(export_root=archive_env.export, paths=archive_env.paths, authorization=archive_env.auth)
    manifest = json.loads(archive_env.paths.manifest.read_text(encoding="utf-8"))
    manifest["schema"] = "cce.archive.manifest.v1"
    archive_env.paths.manifest.write_text(json.dumps(manifest), encoding="utf-8")
    assert load_manifest(archive_env.paths)["schema"] == "cce.archive.manifest.v1"
    manifest["schema"] = "archive-manifest.v9"
    archive_env.paths.manifest.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported archive manifest schema"):
        load_manifest(archive_env.paths)


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
            archive_root=archive_env.store / DEFAULT_ARCHIVE_DIRNAME,
            corpus_store_root=archive_env.store,
            source_roots=(archive_env.store / DEFAULT_ARCHIVE_DIRNAME / "inside",),
        )
    other = tmp_path / "other-store"
    other.mkdir()
    with pytest.raises(CorpusStoreError):
        authorize_archive(
            project_root=archive_env.project,
            archive_root=other / DEFAULT_ARCHIVE_DIRNAME,
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
    topics = json.loads(archive_env.paths.topics.read_text(encoding="utf-8"))
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
    first = archive_env.paths.enrichment.read_bytes()
    build_analysis(archive_env.paths)
    assert archive_env.paths.enrichment.read_bytes() == first


def test_topics_handle_an_empty_store(archive_env) -> None:
    summary = build_analysis(ArchivePaths(archive_env.paths.root))
    assert summary["documents"] == 0 and summary["hubs"] == 0
    write_store(archive_env.paths, [conv("grok", "solo", "Solo")])
    assert build_analysis(archive_env.paths)["documents"] == 1
