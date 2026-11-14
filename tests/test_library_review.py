"""Independent regressions for publication, source coverage and revision review findings."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from importlib import import_module
from pathlib import Path

import pytest

from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.corpus_store import register_corpus_store
from conversation_corpus_engine.library_build import (
    build_library,
    current_generation,
    publish_library,
)
from conversation_corpus_engine.library_commands import read_record
from conversation_corpus_engine.library_store import Library


def observation(text: str, *, status: str = "full", source: str = "original") -> Conversation:
    return Conversation(
        provider="codex",
        source_id="session-one",
        title="Preserved session",
        messages=[Message(role="user", text=text, id="message-one", channel="typed")]
        if text
        else [],
        provenance={"source_file": f"{source}.jsonl", "source_sha256": source},
        extra={"wing": "ai", "lane": "coding", "body_status": status},
    )


def write_archive(path: Path, records: list[Conversation]) -> None:
    path.write_text(
        "".join(json.dumps(item.to_dict(), ensure_ascii=False) + "\n" for item in records),
        encoding="utf-8",
    )


def source_fixture(tmp_path: Path) -> tuple[Path, Path, Path, dict]:
    project, store = tmp_path / "project", tmp_path / "store"
    source = tmp_path / "archive" / "provider-codex" / "collection-codex-conversations.jsonl"
    project.mkdir()
    store.mkdir()
    source.parent.mkdir(parents=True)
    register_corpus_store(project, store)
    write_archive(source, [observation("Original recoverable heliotrope body.")])
    manifest = {
        "schema": "cce.library-sources.v1",
        "archives": [str(source.parent.parent)],
        "documents": [],
        "limits": {"max_records": 1000, "max_seconds": 30, "max_output_bytes": 8 * 1024**2},
    }
    return project, store, source, manifest


def test_snapshot_replay_does_not_manufacture_revisions(tmp_path: Path) -> None:
    earlier = observation("Earlier heliotrope discussion.", source="snapshot-a")
    later = observation("Later heliotrope resolution.", source="snapshot-b")
    with Library.create(tmp_path / "library.sqlite") as library:
        for item in (earlier, later):
            library.put(item, reconcile=True)
        before = library.history(earlier.uid)
        for item in (earlier, later, earlier, later):
            assert library.put(item, reconcile=True)["changed"] is False
        assert library.history(earlier.uid) == before
        assert len(before) == 2
        assert library.record(later.uid)["messages"][0]["text"] == later.messages[0].text
        assert len(library.snapshots(later.uid)) == 2
        assert library.verify() == []


def test_explicit_revert_still_records_a_new_revision(tmp_path: Path) -> None:
    earlier = observation("Original heliotrope content.")
    later = observation("Updated heliotrope content.")
    with Library.create(tmp_path / "library.sqlite") as library:
        library.put(earlier)
        library.put(later)
        result = library.put(earlier)
        assert result["version"] == "1.2.0"
        assert library.record(earlier.uid)["messages"][0]["text"] == earlier.messages[0].text
        assert len(library.history(earlier.uid)) == 3


@pytest.mark.parametrize("status", ["full", "partial", "metadata_only"])
def test_digest_read_recovers_every_retained_snapshot(tmp_path: Path, status: str) -> None:
    original = observation("Original complete heliotrope record.")
    snapshot = observation(
        "A distinct retained snapshot." if status != "metadata_only" else "",
        status=status,
        source=f"snapshot-{status}",
    )
    with Library.create(tmp_path / "library.sqlite") as library:
        library.put(original, reconcile=True)
        result = library.put(snapshot, reconcile=True)
        assert library.record(original.uid, digest=result["digest"]) == snapshot.to_dict()
        assert library.record(original.uid, version="1.0.0") == original.to_dict()
        page = read_record(library, original.uid, digest=result["digest"], limit=8)
        assert page["snapshot_digest"] == result["digest"]
        assert page["version"] is None
        assert page["body_status"] == status
        expected = snapshot.messages[0].text if snapshot.messages else ""
        assert page["text"] == expected[:8]
        assert page["next_offset"] == (8 if len(expected) > 8 else None)


def test_digest_read_cannot_cross_identity_or_combine_selectors(tmp_path: Path) -> None:
    original = observation("First identity's retained content.")
    other = observation("Second identity's unrelated content.")
    other.source_id = "session-two"
    with Library.create(tmp_path / "library.sqlite") as library:
        own = library.put(original)
        foreign = library.put(other)
        assert own["digest"] != foreign["digest"]
        for retrieve in (library.record, lambda uid, **kwargs: read_record(library, uid, **kwargs)):
            with pytest.raises(KeyError):
                retrieve(original.uid, digest=foreign["digest"])
            with pytest.raises(KeyError):
                retrieve(original.uid, digest="f" * 64)
            with pytest.raises(ValueError, match="version|digest"):
                retrieve(original.uid, version="1.0.0", digest=own["digest"])


def test_metadata_only_read_bounds_large_source_metadata(tmp_path: Path) -> None:
    item = observation("", status="metadata_only")
    item.title = "large source title " * 30000
    item.provenance = {"long-key" * 20000: "long-value" * 20000}
    item.extra["findings"] = ["large source finding " * 20000 for _ in range(25)]
    with Library.create(tmp_path / "library.sqlite") as library:
        library.put(item)
        result = read_record(library, item.uid, limit=17)
        assert result["text"] == ""
        assert result["body_status"] == "metadata_only"
        assert len(json.dumps(result, ensure_ascii=False)) < 100000


def test_search_bounds_source_supplied_metadata(tmp_path: Path) -> None:
    item = observation("A searchable heliotrope body.")
    item.title = "large source title " * 30000
    item.kind = "large source kind " * 20000
    item.extra["lane"] = "large source lane " * 20000
    item.provenance["source_file"] = "long-source-reference" * 20000
    with Library.create(tmp_path / "library.sqlite") as library:
        library.put(item)
        result = library.search("heliotrope", limit=1)
        assert result["total"] == 1
        assert result["hits"][0]["uid"] == item.uid
        assert len(json.dumps(result, ensure_ascii=False)) < 100000


@pytest.mark.parametrize("status,text", [("partial", "A shorter fragment."), ("metadata_only", "")])
def test_lower_body_coverage_keeps_current_text_and_new_evidence(
    tmp_path: Path, status: str, text: str
) -> None:
    original = observation("Original recoverable heliotrope body.")
    lower = observation(text, status=status, source="unavailable-later-source")
    with Library.create(tmp_path / "library.sqlite") as library:
        library.put(original, reconcile=True)
        history = library.history(original.uid)
        library.put(lower, reconcile=True)
        assert library.record(original.uid)["messages"][0]["text"] == original.messages[0].text
        assert library.search("heliotrope")["total"] == 1
        assert library.status()["by_body_status"] == {"full": 1}
        assert library.history(original.uid) == history
        assert len(library.snapshots(original.uid)) == 2
        assert library.status()["observations"] == 2


@pytest.mark.parametrize("provider", ["../escape", "a/b", "a\\b", "https:evil", "bad\nname"])
def test_unsafe_provider_cannot_create_an_export_path(tmp_path: Path, provider: str) -> None:
    item = observation("Content with an unsafe provider identity.")
    item.provider = provider
    with Library.create(tmp_path / "library.sqlite") as library:
        with pytest.raises(ValueError, match="identifier|provider|UID"):
            library.put(item)
        assert library.status()["records"] == 0


def test_current_generation_refuses_a_changed_published_report(tmp_path: Path) -> None:
    project, store, _, manifest = source_fixture(tmp_path)
    report = build_library(manifest, project_root=project, store_root=store)
    generation = Path(report["generation"])
    publish_library(generation, project_root=project, store_root=store)
    receipt = generation / "report.json"
    altered = json.loads(receipt.read_text())
    altered["coverage_complete"] = not altered["coverage_complete"]
    receipt.write_text(json.dumps(altered), encoding="utf-8")
    with pytest.raises(ValueError, match="report|hash|integrity"):
        current_generation(store)


def test_stale_candidate_cannot_replace_a_newer_publication(tmp_path: Path) -> None:
    project, store, source, manifest = source_fixture(tmp_path)
    initial = build_library(manifest, project_root=project, store_root=store)
    publish_library(Path(initial["generation"]), project_root=project, store_root=store)
    write_archive(source, [observation("Second heliotrope version.", source="second")])
    second = build_library(manifest, project_root=project, store_root=store)
    write_archive(source, [observation("Third heliotrope version.", source="third")])
    concurrent = build_library(manifest, project_root=project, store_root=store)
    publish_library(Path(second["generation"]), project_root=project, store_root=store)
    with pytest.raises(ValueError, match="changed|stale|parent"):
        publish_library(Path(concurrent["generation"]), project_root=project, store_root=store)
    assert current_generation(store) == Path(second["generation"])


def test_new_observation_is_published_without_downgrading_the_current_body(tmp_path: Path) -> None:
    project, store, source, manifest = source_fixture(tmp_path)
    initial = build_library(manifest, project_root=project, store_root=store)
    publish_library(Path(initial["generation"]), project_root=project, store_root=store)
    write_archive(
        source,
        [
            observation("Original recoverable heliotrope body."),
            observation("", status="metadata_only", source="additional-metadata-observation"),
        ],
    )
    candidate = build_library(manifest, project_root=project, store_root=store)
    generation = Path(candidate["generation"])
    with Library(generation / "library.sqlite") as library:
        assert library.status()["records"] == 1
        assert library.status()["observations"] == 2
        assert len(library.history("codex-session-one")) == 1
        assert len(library.snapshots("codex-session-one")) == 2
        assert library.search("heliotrope")["total"] == 1
    assert publish_library(generation, project_root=project, store_root=store)["changed"] is True
    assert current_generation(store) == generation
    identical = build_library(manifest, project_root=project, store_root=store)
    assert (
        publish_library(Path(identical["generation"]), project_root=project, store_root=store)[
            "changed"
        ]
        is False
    )
    assert current_generation(store) == generation


def test_source_rewritten_during_read_cannot_receive_a_verified_binding(tmp_path: Path) -> None:
    project, store, source, manifest = source_fixture(tmp_path)
    records = []
    for index in range(100):
        item = observation(f"Source record {index} and its heliotrope context.")
        item.source_id = f"record-{index}"
        records.append(item)
    write_archive(source, records)
    changed = False

    def change_source(progress: dict) -> None:
        nonlocal changed
        if progress.get("accepted") == 100 and not changed:
            source.write_text("", encoding="utf-8")
            changed = True

    with pytest.raises(ValueError, match="source.*changed|changed.*source"):
        build_library(manifest, project_root=project, store_root=store, progress=change_source)
    assert changed
    assert current_generation(store) is None


def test_export_budget_callback_stops_before_publication(tmp_path: Path, monkeypatch) -> None:
    library_export = import_module("conversation_corpus_engine.library_export")

    project, store, _, manifest = source_fixture(tmp_path)
    callback_called = False

    def over_budget(*args, check_callback=None, **kwargs):
        nonlocal callback_called
        assert check_callback is not None, "builder must propagate its budget to the exporter"
        callback_called = True
        check_callback(bytes_written=manifest["limits"]["max_output_bytes"] + 1)
        pytest.fail("exporter exceeded the byte budget without interruption")

    monkeypatch.setattr(library_export, "export_library", over_budget)
    with pytest.raises(ValueError, match="byte|budget|output"):
        build_library(manifest, project_root=project, store_root=store)
    assert callback_called
    assert current_generation(store) is None


def test_prior_database_over_budget_is_rejected_before_copying(tmp_path: Path) -> None:
    project, store, _, manifest = source_fixture(tmp_path)
    initial = build_library(manifest, project_root=project, store_root=store)
    generation = Path(initial["generation"])
    publish_library(generation, project_root=project, store_root=store)
    before = set(generation.parent.iterdir())
    manifest["limits"]["max_output_bytes"] = (generation / "library.sqlite").stat().st_size - 1
    with pytest.raises(ValueError, match="budget|byte|database"):
        build_library(manifest, project_root=project, store_root=store)
    assert set(generation.parent.iterdir()) == before
    assert current_generation(store) == generation


def test_database_page_growth_stops_at_declared_output_budget(tmp_path: Path) -> None:
    project, store, source, manifest = source_fixture(tmp_path)
    budget = 256 * 1024
    manifest["limits"]["max_output_bytes"] = budget
    text = " ".join(hashlib.sha256(str(index).encode()).hexdigest() for index in range(20000))
    write_archive(source, [observation(text)])
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        build_library(manifest, project_root=project, store_root=store)
    databases = list((store / "library-all/generations").glob("*/library.sqlite"))
    assert len(databases) == 1
    assert databases[0].stat().st_size <= budget
    assert current_generation(store) is None


def test_block_index_and_revision_verification_honor_cooperative_checks(tmp_path: Path) -> None:
    class DeadlineReached(RuntimeError):
        pass

    def stop(**kwargs) -> None:
        raise DeadlineReached("review deadline")

    with Library.create(tmp_path / "library.sqlite") as library:
        library.put(observation("A heliotrope message suitable for budget checks."))
        library.index_blocks()
        before = library.status()["occurrences"]
        with pytest.raises(DeadlineReached):
            library.index_blocks(check_callback=stop)
        assert library.status()["occurrences"] == before
        with pytest.raises(DeadlineReached):
            library.verify(check_callback=stop)
