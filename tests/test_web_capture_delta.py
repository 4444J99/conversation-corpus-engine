"""Unique regressions from the preserved PR146 follow-up, using invented sources only."""

from __future__ import annotations

import json

import pytest
from test_web_capture import make_row, manifest_for, page_rows, setup, write_shard
from test_web_capture_integration import wiki_row

from conversation_corpus_engine import web_capture
from conversation_corpus_engine.library_build import build_library


@pytest.mark.parametrize("count", [1, 2])
def test_unknown_field_names_never_enter_diagnostics(tmp_path, count):
    marker = "SYNTHETIC-ROW-CONTENT-NOT-A-DIAGNOSTIC"
    row = {**page_rows()[0], **{f"{marker}-{index}": 1 for index in range(count)}}
    with pytest.raises(web_capture.RowRefused) as refused:
        web_capture.check_row(row)
    assert marker not in str(refused.value)
    assert str(refused.value) == f"row has {count} unexpected field{'s' if count != 1 else ''}"
    project, store, shards = setup(tmp_path)
    write_shard(shards / "rows.jsonl", [row, page_rows()[1]])
    report = build_library(manifest_for(shards), project_root=project, store_root=store)
    assert report["status"]["records"] == 1
    assert marker not in json.dumps(report)


@pytest.mark.parametrize("stamp", ["٢٠٢٦-01-02T03:04:05Z", "２０２６-01-02T03:04:05Z"])
@pytest.mark.parametrize("schema", ["capture", "wikipedia"])
def test_capture_timestamps_require_ascii_digits(stamp, schema):
    row = page_rows()[0] if schema == "capture" else wiki_row()
    row["captured_at" if schema == "capture" else "fetched_at"] = stamp
    with pytest.raises(web_capture.RowRefused, match="UTC"):
        web_capture.check_row(row)


def _change_before_reread(monkeypatch, paths):
    original = web_capture._open
    calls = 0

    def changed(path):
        nonlocal calls
        calls += 1
        if calls == len(paths) + 1:
            paths[-1].unlink()
        return original(path)

    monkeypatch.setattr(web_capture, "_open", changed)


def test_a_rotated_winner_falls_back_to_an_unchanged_duplicate(tmp_path, monkeypatch):
    older = page_rows()[0]
    newer = make_row(older["requested_url"], "A newer synthetic page", "2026-04-01T00:00:00Z")
    a = write_shard(tmp_path / "a.jsonl", [older])
    b = write_shard(tmp_path / "b.jsonl", [newer])
    _change_before_reread(monkeypatch, [a, b])
    report = {}
    items = list(web_capture.iter_web_captures([str(a), str(b)], report=report, max_records=1))
    assert [item.messages[0].text for item in items] == [older["text"]]
    assert (
        report["counts"]["duplicates"],
        report["counts"]["refused"],
        report["counts"]["records"],
    ) == (1, 1, 1)
    assert [(finding["path"], finding["code"]) for finding in report["findings"]] == [
        ("b.jsonl", "malformed_row")
    ]


def test_fallbacks_remain_bounded_and_exhaustion_is_explicit(tmp_path, monkeypatch):
    paths = [
        write_shard(
            tmp_path / f"{index}.jsonl",
            [
                make_row(
                    "https://example.test/same",
                    f"Synthetic revision {index}",
                    f"2026-0{index + 1}-01T00:00:00Z",
                )
            ],
        )
        for index in range(5)
    ]
    original = web_capture._open
    reopened = []
    calls = 0

    def rotated(path):
        nonlocal calls
        calls += 1
        if calls > len(paths):
            reopened.append(path.name)
            raise FileNotFoundError("synthetic shard rotation")
        return original(path)

    monkeypatch.setattr(web_capture, "_open", rotated)
    report = {}
    assert list(web_capture.iter_web_captures([str(path) for path in paths], report=report)) == []
    assert reopened == ["4.jsonl", "3.jsonl", "2.jsonl"]
    assert report["counts"]["refused"] == 3
    assert report["findings"][-1]["code"] == "page_unavailable"


def test_the_deadline_can_abort_between_candidate_fallbacks(tmp_path, monkeypatch):
    first = page_rows()[0]
    second = make_row(first["requested_url"], "New synthetic revision", "2026-04-01T00:00:00Z")
    a = write_shard(tmp_path / "a.jsonl", [first])
    b = write_shard(tmp_path / "b.jsonl", [second])
    original = web_capture._open
    expired = False
    calls = 0

    def unavailable(path):
        nonlocal expired, calls
        calls += 1
        if calls == 3:
            expired = True
            raise FileNotFoundError("synthetic rotation")
        return original(path)

    def check():
        if expired:
            raise TimeoutError("synthetic build deadline")

    monkeypatch.setattr(web_capture, "_open", unavailable)
    with pytest.raises(TimeoutError, match="synthetic build deadline"):
        list(web_capture.iter_web_captures([str(a), str(b)], check_callback=check))
    assert calls == 3


def test_build_reports_the_exact_number_of_omitted_finding_details(tmp_path, monkeypatch):
    monkeypatch.setattr(web_capture, "MAX_FINDINGS", 2)
    project, store, shards = setup(tmp_path)
    write_shard(shards / "rows.jsonl", [page_rows()[0]], raw=["not json"] * 7)
    report = build_library(manifest_for(shards), project_root=project, store_root=store)
    source_report = report["sources"][0]["report"]
    assert source_report["findings_omitted"] == 5
    assert source_report["counts"]["refused"] == 7
    assert report["status"]["records"] == 1 and report["coverage_complete"] is False
    truncations = [item for item in report["findings"] if item["code"] == "findings_truncated"]
    assert [(item["scope"], item["count"]) for item in truncations] == [("web_captures", 5)]
    assert len(report["findings"]) == 3


def test_offline_capture_time_does_not_depend_on_the_readers_wall_clock(tmp_path):
    row = make_row(
        "https://example.test/dated", "Synthetic clock-independent evidence", "2099-01-01T00:00:00Z"
    )
    shard = write_shard(tmp_path / "rows.jsonl", [row])
    first = list(web_capture.iter_web_captures([str(shard)]))[0].to_dict()
    second = list(web_capture.iter_web_captures([str(shard)]))[0].to_dict()
    assert first == second and first["created"] == row["captured_at"]


@pytest.mark.parametrize(
    "key",
    [
        "https://example.test/page?version=2",
        "http://example.test/page?version=1",
        "https://example.test/redirect",
    ],
)
def test_capture_identity_keeps_query_scheme_and_requested_url_binding(key):
    row = make_row(
        "https://example.test/page?version=1",
        "Synthetic identity",
        "2026-01-01T00:00:00Z",
        key=key,
        final_url="https://example.test/redirect",
    )
    with pytest.raises(web_capture.RowRefused, match="key does not follow"):
        web_capture.check_row(row)
