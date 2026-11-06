"""Producer-contract and bounded search regressions for the integrated C13 reader."""

from __future__ import annotations

import copy
import errno
import hashlib
import json
from pathlib import Path

import pytest
from test_web_capture import make_row, manifest_for, page_rows, setup, write_shard

from conversation_corpus_engine import library_mcp, web_capture
from conversation_corpus_engine.attribution_rules import attribute_message, record_info
from conversation_corpus_engine.library_build import build_library
from conversation_corpus_engine.library_mcp import SEARCH_TOOL, call_tool
from conversation_corpus_engine.library_store import Library


def wiki_row():
    text = (
        "Opaline currents trace hidden patterns. "
        + "A synthetic encyclopedia paragraph supplies source material for exact quotation. " * 3
    )
    key = "wikipedia:en:Synthetic Article"
    return {
        "schema": "wikipedia-article.v1",
        "id": "pg_" + hashlib.sha256(key.encode()).hexdigest()[:24],
        "key": key,
        "status": "stored",
        "reason": None,
        "requested_url": "https://en.wikipedia.org/wiki/Synthetic_Article",
        "requested_title": "Synthetic Article",
        "lang": "en",
        "api_host": "en.wikipedia.org",
        "resolved_title": "Synthetic Article",
        "redirected": False,
        "redirect_chain": [],
        "page_id": 123,
        "revision_id": 456,
        "revision_timestamp": "2026-11-05T10:00:00Z",
        "fetched_at": "2026-11-06T12:00:00Z",
        "content_sha256": "sha256:" + hashlib.sha256(text.encode()).hexdigest(),
        "text": text,
        "text_chars": len(text),
        "categories": ["Category:Synthetic"],
        "is_disambiguation": False,
        "disambiguation_evidence": [],
        "license_note": "Wikipedia text is CC BY-SA 4.0; third-party text, never owner-authored",
        "worker": {"name": "wikipedia_worker", "version": "1.0.0"},
    }


@pytest.mark.parametrize(
    "field,value", [("status", []), ("status", {}), ("tier", []), ("tier", {})]
)
def test_unhashable_fields_are_refused_as_findings_and_valid_rows_survive(tmp_path, field, value):
    row = page_rows()[0]
    row[field] = value
    shard = write_shard(tmp_path / "rows.jsonl", [row, page_rows()[1]])
    report = {}
    items = list(web_capture.iter_web_captures([str(shard)], report=report))
    assert len(items) == 1 and items[0].title == "Kettle guide"
    assert report["counts"]["refused"] == 1


def test_requested_url_must_be_bound_to_its_key_and_id(tmp_path):
    row = page_rows()[0]
    row["requested_url"] = "https://different.example.test/substituted"
    shard = write_shard(tmp_path / "rows.jsonl", [row])
    report = {}
    assert list(web_capture.iter_web_captures([str(shard)], report=report)) == []
    assert report["counts"]["refused"] == 1


def test_credential_url_is_refused_without_echoing_it_in_findings(tmp_path):
    row = make_row(
        "https://fixture:secret@example.test/page", "Synthetic text", "2026-11-06T12:00:00Z"
    )
    shard = write_shard(tmp_path / "rows.jsonl", [row])
    report = {}
    assert list(web_capture.iter_web_captures([str(shard)], report=report)) == []
    assert "secret" not in json.dumps(report["findings"])


def test_producer_url_normalization_preserves_requested_and_redirected_urls(tmp_path):
    row = make_row(
        "HTTPS://WWW.Example.Test/Saved/?z=9&utm_source=fixture&b=Two+Words#fragment",
        "Amber memories",
        "2026-11-06T12:00:00Z",
        key="https://example.test/saved?b=two+words&z=9",
        final_url="https://example.test/redirected",
    )
    shard = write_shard(tmp_path / "rows.jsonl", [row])
    item = list(web_capture.iter_web_captures([str(shard)]))[0]
    assert item.provenance["requested_url"] == row["requested_url"]
    assert item.provenance["source_path"] == row["final_url"]


def test_duplicate_metadata_ties_do_not_depend_on_declared_source_order(tmp_path):
    first = page_rows()[0]
    second = copy.deepcopy(first)
    second["title"] = "An alternative title for the same captured text"
    a = write_shard(tmp_path / "a.jsonl", [first])
    b = write_shard(tmp_path / "b.jsonl", [second])
    forwards = [item.to_dict() for item in web_capture.iter_web_captures([str(a), str(b)])]
    backwards = [item.to_dict() for item in web_capture.iter_web_captures([str(b), str(a)])]
    assert forwards == backwards


def test_search_provenance_never_decodes_an_entire_long_page(tmp_path, monkeypatch):
    row = page_rows()[0]
    row["text"] += (
        " This paragraph has enough context to qualify as an eligible passage in the deduplicated index."
    )
    row["text"] += "\n\n" + "Synthetic surrounding page text. " * 30000
    row["content_digest"] = web_capture.text_digest(row["text"])
    database = tmp_path / "library.sqlite"
    with Library.create(database) as library:
        library.put(web_capture.conversation_from_row(web_capture.check_row(row), "fixture.jsonl"))
        library.index_blocks()

        def do_not_decode(*args, **kwargs):
            raise AssertionError(
                "search must use bounded stored provenance, not decode a whole page"
            )

        monkeypatch.setattr(library, "record", do_not_decode)
        hit = library.search('"orchard lattice"', unit="passage", max_seconds=1)["hits"][0]
        assert hit["capture"]["captured_at"] == row["captured_at"]
        assert hit["capture"]["content_digest"] == row["content_digest"]


def test_wikipedia_body_and_revision_provenance_are_searchable_and_third_party(
    tmp_path, monkeypatch
):
    project, store, shards = setup(tmp_path)
    row = wiki_row()
    write_shard(shards / "articles.en.jsonl", [row])
    result = build_library(manifest_for(shards), project_root=project, store_root=store)
    assert result["status"]["records"] == 1 and result["coverage_complete"]
    generation = Path(result["generation"])
    with Library(generation / "library.sqlite") as library:
        hit = library.search('"opaline currents"', unit="passage")["hits"][0]
        payload = library.record(hit["uid"])
        assert payload["messages"][0]["text"] == row["text"]
        assert hit["capture"]["captured_at"] == row["fetched_at"]
        assert payload["provenance"]["wikipedia"]["revision_id"] == 456
        assert payload["provenance"]["capture_schema"] == "wikipedia-article.v1"
        spans = attribute_message(
            row["text"], role="tool", channel="tool", typed_chars=0, record=record_info(payload)
        )
        assert {span.author for span in spans} == {"third_party"}
    monkeypatch.setattr(
        library_mcp,
        "open_library",
        lambda root: (generation, Library(generation / "library.sqlite")),
    )
    compact = call_tool(store, SEARCH_TOOL, {"query": "opaline currents", "compact": True})
    assert compact["h"][0]["cp"]["ca"] == row["fetched_at"]


def test_wikipedia_case_sensitive_title_and_expanding_uppercase_follow_the_producer(tmp_path):
    row = wiki_row()
    row.update(
        requested_url="https://de.m.wikipedia.org/w/index.php?title=%C3%9Fynthetic_Article",
        requested_title="ßynthetic Article",
        resolved_title="ßynthetic Article",
        lang="de",
        api_host="de.wikipedia.org",
        key="wikipedia:de:ßynthetic Article",
    )
    row["id"] = "pg_" + hashlib.sha256(row["key"].encode()).hexdigest()[:24]
    shard = write_shard(tmp_path / "articles.de.jsonl", [row])
    item = list(web_capture.iter_web_captures([str(shard)]))
    assert len(item) == 1 and item[0].title == "ßynthetic Article"


def test_wikipedia_missing_and_refused_rows_are_counted_without_article_text(tmp_path):
    missing = wiki_row()
    missing.update(
        status="missing", reason="missing_page", text="", text_chars=0, content_sha256=None
    )
    key = "refused:sha256:" + "0" * 64
    refused = {
        "schema": "wikipedia-article.v1",
        "id": "pg_" + hashlib.sha256(key.encode()).hexdigest()[:24],
        "key": key,
        "status": "refused",
        "reason": "not_wikipedia_host",
        "detail": "host refused",
        "requested_url": "[redacted URL]",
        "recorded_at": "2026-11-06T12:00:00Z",
        "worker": {"name": "wikipedia_worker", "version": "1.0.0"},
    }
    shard = write_shard(tmp_path / "wiki.jsonl", [missing, refused])
    report = {}
    items = list(web_capture.iter_web_captures([str(shard)], report=report))
    assert len(items) == 1 and items[0].messages == []
    assert items[0].extra["body_status"] == "metadata_only"
    assert report["counts"]["captured"] == 0 and report["counts"]["failed"] == 1
    assert report["counts"]["refused"] == 1


@pytest.mark.parametrize("page_id", [-1, 0])
def test_wikipedia_missing_placeholders_preserve_nonpositive_source_ids(tmp_path, page_id):
    row = wiki_row()
    row.update(
        status="missing",
        reason="missing_page",
        text="",
        text_chars=0,
        content_sha256=None,
        page_id=page_id,
        revision_id=None,
        revision_timestamp=None,
    )
    shard = write_shard(tmp_path / "articles.en.jsonl", [row])
    items = list(web_capture.iter_web_captures([str(shard)]))
    assert len(items) == 1 and items[0].messages == []
    assert items[0].provenance["wikipedia"]["page_id"] == page_id
    assert items[0].extra["body_status"] == "metadata_only"


def test_distinct_candidate_budget_applies_during_first_pass(tmp_path):
    shard = write_shard(tmp_path / "rows.jsonl", page_rows())
    with pytest.raises(ValueError, match="record limit"):
        list(web_capture.iter_web_captures([str(shard)], max_records=2))


def test_resume_binds_web_capture_source_receipts_to_the_manifest(tmp_path, monkeypatch):
    project, store, shards = setup(tmp_path)
    write_shard(shards / "one.jsonl", page_rows())

    def failed(self, **kwargs):
        raise ValueError("synthetic ingestion checkpoint")

    monkeypatch.setattr(Library, "index_blocks", failed)
    with pytest.raises(ValueError, match="synthetic ingestion checkpoint"):
        build_library(manifest_for(shards), project_root=project, store_root=store)
    seed = next((store / "library-all" / "generations").iterdir())
    state_path = seed / "build-state.json"
    state = json.loads(state_path.read_text())
    state["report"]["sources"][0]["report"]["sources"][0]["path"] = str(tmp_path / "different")
    state_path.write_text(json.dumps(state))
    with pytest.raises(ValueError, match="web capture source collection"):
        build_library(
            {"schema": "cce.library-sources.v1", "resume_generation": str(seed)},
            project_root=project,
            store_root=store,
        )


def test_provenance_projection_is_bound_to_current_revision_and_full_verification(tmp_path):
    row = page_rows()[0]
    with Library.create(tmp_path / "library.sqlite") as library:
        item = web_capture.conversation_from_row(row, "fixture.jsonl")
        library.put(item)
        row["text"] += " A later version records revised evidence."
        row["content_digest"] = web_capture.text_digest(row["text"])
        row["captured_at"] = "2026-11-06T12:00:00Z"
        changed = library.put(web_capture.conversation_from_row(row, "fixture.jsonl"))
        library.index_blocks()
        hit = library.search('"orchard lattice"')["hits"][0]
        assert hit["capture"]["captured_at"] == "2026-11-06T12:00:00Z"
        assert hit["capture"]["content_digest"] == row["content_digest"]
        assert library.verify() == []
        library.connection.execute(
            "UPDATE meta SET value='{}' WHERE key=?", ("web_capture:" + changed["digest"],)
        )
        assert any("provenance differs from source" in problem for problem in library.verify())


@pytest.mark.parametrize(
    "field,value",
    [
        ("content_sha256", "sha256:" + "0" * 64),
        ("text_chars", 1),
        ("revision_id", True),
        ("requested_url", "https://en.wikipedia.org/wiki/Other_Article"),
    ],
)
def test_wikipedia_integrity_failures_remain_findings(tmp_path, field, value):
    row = wiki_row()
    row[field] = value
    report = {}
    shard = write_shard(tmp_path / "articles.en.jsonl", [row])
    assert list(web_capture.iter_web_captures([str(shard)], report=report)) == []
    assert report["counts"]["refused"] == 1


def test_many_invalid_lines_keep_bounded_finding_details(tmp_path, monkeypatch):
    monkeypatch.setattr(web_capture, "MAX_FINDINGS", 2, raising=False)
    report = {}
    shard = write_shard(tmp_path / "rows.jsonl", [], raw=["not json"] * 5)
    assert list(web_capture.iter_web_captures([str(shard)], report=report)) == []
    assert report["counts"]["refused"] == 5
    assert len(report["findings"]) == 2 and report["findings_omitted"] == 3


def test_deadline_is_checked_while_draining_an_oversized_unterminated_line(tmp_path, monkeypatch):
    shard = tmp_path / "oversized.jsonl"
    shard.write_bytes(b"x" * (web_capture.CHUNK * 20))
    opened = []
    positions = []
    original = web_capture._open

    def observe(path):
        handle, info = original(path)
        opened.append(handle)
        return handle, info

    def deadline():
        if opened and not opened[-1].closed:
            positions.append(opened[-1].tell())
            if positions[-1] > 16:
                raise TimeoutError("synthetic draining deadline")

    monkeypatch.setattr(web_capture, "_open", observe)
    with pytest.raises(TimeoutError, match="synthetic draining deadline"):
        list(web_capture.iter_web_captures([str(shard)], max_row_bytes=16, check_callback=deadline))
    assert max(positions) < shard.stat().st_size


def test_deadline_is_checked_during_discovery_before_opening_shards(tmp_path, monkeypatch):
    for index in range(20):
        write_shard(tmp_path / f"shard-{index}.jsonl", [])
    calls = 0

    def deadline():
        nonlocal calls
        calls += 1
        if calls > 5:
            raise TimeoutError("synthetic discovery deadline")

    def no_open(path):
        raise AssertionError("discovery exhausted its budget before any shard should open")

    monkeypatch.setattr(web_capture, "_open", no_open)
    with pytest.raises(TimeoutError, match="synthetic discovery deadline"):
        list(web_capture.iter_web_captures([str(tmp_path)], check_callback=deadline))


def test_unreadable_directories_are_explicit_coverage_findings(tmp_path, monkeypatch):
    def unavailable_walk(root, **kwargs):
        error = PermissionError(errno.EACCES, "synthetic access denied", str(root / "unavailable"))
        if kwargs.get("onerror"):
            kwargs["onerror"](error)
        return iter(())

    monkeypatch.setattr(web_capture.os, "walk", unavailable_walk)
    report = {}
    assert list(web_capture.iter_web_captures([str(tmp_path)], report=report)) == []
    assert [(finding["code"], finding["path"]) for finding in report["findings"]] == [
        ("unreadable_directory", "unavailable")
    ]
