"""Web captures: the reader's hardening after review (types, ties, fallbacks, stamps, identity, bounds).

Every URL, title and sentence here is invented (example.com style); no real page is involved.
"""

from __future__ import annotations

import json
import os
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from test_web_capture import (
    PAGES,
    failed_row,
    make_row,
    manifest_for,
    page_rows,
    setup,
    write_shard,
)

from conversation_corpus_engine import web_capture
from conversation_corpus_engine.library_build import build_library

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def read(sources: Any, **kwargs: Any) -> tuple[list[Any], dict[str, Any]]:
    report: dict[str, Any] = {}
    sources = sources if isinstance(sources, list) else [sources]
    kwargs.setdefault("clock", lambda: NOW)
    items = list(web_capture.iter_web_captures(sources, report=report, **kwargs))
    return items, report


def comparable(conversation: Any) -> dict[str, Any]:
    """A record without the shard name it came from (that name is the layout)."""
    payload = conversation.to_dict()
    payload["provenance"].pop("source_file")
    return payload


# --- 1. wrong types are refusals, never crashes -----------------------------------------------


@pytest.mark.parametrize("field", ["status", "tier", "failure_class"])
@pytest.mark.parametrize("value", [[], ["A"], {}, {"a": 1}, [[]]])
def test_an_unhashable_value_is_a_refusal_not_a_crash(field, value):
    row = {**make_row(PAGES[0][0], PAGES[0][2], PAGES[0][3]), field: value}
    with pytest.raises(web_capture.RowRefused):
        web_capture.check_row(row, now=NOW)
    with pytest.raises(web_capture.RowRefused):
        web_capture.parse_row(json.dumps(row).encode(), now=NOW)


def test_a_bad_typed_line_becomes_a_finding_and_the_other_pages_survive(tmp_path):
    rows = page_rows()
    bad = {**rows[0], "status": []}
    bad2 = {**rows[1], "tier": ["A"]}
    write_shard(tmp_path / "s.jsonl", [bad, bad2, rows[2]])
    items, report = read(tmp_path)
    assert len(items) == 1
    assert [(f["code"], f["line"]) for f in report["findings"]] == [
        ("malformed_row", 1),
        ("malformed_row", 2),
    ]
    assert report["counts"]["refused"] == 2


# --- 2 and 5. ties have a total, content-derived order ----------------------------------------


def test_tied_failures_pick_the_same_winner_whatever_the_shard_names(tmp_path):
    url = PAGES[0][0]
    timeout = failed_row(url, "timeout", "2026-03-20T08:00:00Z")
    blocked = failed_row(url, "blocked", "2026-03-20T08:00:00Z")
    reasons = set()
    for index, (first, second) in enumerate(((timeout, blocked), (blocked, timeout))):
        folder = tmp_path / f"layout{index}"
        write_shard(folder / "a.jsonl", [first])
        write_shard(folder / "b.jsonl", [second])
        items, _ = read(folder)
        reasons.add(items[0].extra["body_reason"])
    assert len(reasons) == 1


def test_tied_captures_with_different_titles_pick_the_same_winner(tmp_path):
    url, _, text, stamp = PAGES[0]
    rows = [
        make_row(url, text, stamp, title="Title one"),
        make_row(url, text, stamp, title="Title two", final_url=url + "/"),
        make_row(url, text, stamp, title="Title three"),
    ]
    seen = set()
    for index in range(6):
        order = list(rows)
        random.Random(index).shuffle(order)
        sources = []
        for number, row in enumerate(order):
            path = tmp_path / f"run{index}" / f"{random.Random(index + number).random()}.jsonl"
            write_shard(path, [row])
            sources.append(path)
        random.Random(index * 7).shuffle(sources)
        items, report = read(sources)
        assert len(items) == 1 and report["counts"]["duplicates"] == 2
        seen.add(json.dumps(comparable(items[0]), sort_keys=True))
    assert len(seen) == 1


def test_identical_rows_in_two_shards_name_the_first_shard_by_name(tmp_path):
    row = make_row(*PAGES[0][:1], PAGES[0][2], PAGES[0][3], title=PAGES[0][1])
    for order in (("m.jsonl", "c.jsonl"), ("c.jsonl", "m.jsonl")):
        folder = tmp_path / order[0]
        for name in order:
            write_shard(folder / name, [row])
        items, _ = read([folder / order[0], folder / order[1]])
        assert items[0].provenance["source_file"] == "c.jsonl"


# --- 3. a candidate that fails the second read falls back to the next one ---------------------


def appender(monkeypatch, target: Path, after: int) -> None:
    """Append a line to `target` just before the `after`-th open of any shard."""
    real = web_capture._open
    calls = {"n": 0}

    def patched(path: Path):
        calls["n"] += 1
        if calls["n"] == after:
            with target.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"late": True}) + "\n")
        return real(path)

    monkeypatch.setattr(web_capture, "_open", patched)


def test_a_changed_winner_shard_falls_back_to_the_next_best_row(tmp_path, monkeypatch):
    url, title, text, _ = PAGES[0]
    write_shard(tmp_path / "a.jsonl", [make_row(url, text, "2026-01-01T00:00:00Z", title=title)])
    newer = text + " Newer."
    write_shard(tmp_path / "b.jsonl", [make_row(url, newer, "2026-02-01T00:00:00Z", title=title)])
    appender(monkeypatch, tmp_path / "b.jsonl", after=3)
    items, report = read(tmp_path)
    assert [item.messages[0].text for item in items] == [text]
    codes = [(f["path"], f["code"]) for f in report["findings"]]
    assert ("b.jsonl", "malformed_row") in codes
    assert not any(code == "page_unavailable" for _, code in codes)
    assert report["counts"]["records"] == 1


def test_a_page_with_no_readable_candidate_is_named_in_a_finding(tmp_path, monkeypatch):
    url, title, text, stamp = PAGES[0]
    write_shard(tmp_path / "only.jsonl", [make_row(url, text, stamp, title=title)])
    appender(monkeypatch, tmp_path / "only.jsonl", after=2)
    items, report = read(tmp_path)
    assert items == []
    codes = [f["code"] for f in report["findings"]]
    assert "page_unavailable" in codes and "source_changed_during_read" not in codes


# --- 4. only ASCII digits make a stamp --------------------------------------------------------


@pytest.mark.parametrize(
    "stamp",
    [
        "٢٠٢٦-01-02T03:04:05Z",
        "２０２６-01-02T03:04:05Z",
        "2026-01-02T03:04:05०Z",
    ],
)
def test_a_stamp_in_non_ascii_digits_is_refused(stamp):
    row = {**make_row(PAGES[0][0], PAGES[0][2], PAGES[0][3]), "captured_at": stamp}
    with pytest.raises(web_capture.RowRefused, match="captured_at must be a UTC time"):
        web_capture.check_row(row, now=NOW)


def test_the_latest_ascii_stamp_wins_against_a_non_ascii_one(tmp_path):
    url, title, text, _ = PAGES[0]
    odd = make_row(url, text + " odd", "٢٠٢٦-01-02T03:04:05Z", title=title)
    real = make_row(url, text, "2026-01-02T03:04:05Z", title=title)
    write_shard(tmp_path / "s.jsonl", [odd, real])
    items, report = read(tmp_path)
    assert [item.created for item in items] == ["2026-01-02T03:04:05Z"]
    assert report["counts"]["refused"] == 1


# --- 6 is in test_web_capture.py (search never reads the page) --------------------------------

# --- 8. identity must follow the URLs; time cannot be in the future ---------------------------


def test_a_key_of_another_page_is_refused():
    row = make_row("https://example.org/b", "text of b", "2026-03-01T00:00:00Z")
    forged = {
        **row,
        "key": "https://example.com/a",
        "id": web_capture.page_id("https://example.com/a"),
    }
    with pytest.raises(web_capture.RowRefused, match="key does not match"):
        web_capture.check_row(forged, now=NOW)


@pytest.mark.parametrize(
    "key",
    [
        "https://example.com/Articles/Orchard",
        "example.com/articles/orchard",
        "http://EXAMPLE.com:80/articles/orchard/",
        "https://example.com:443/articles/orchard#section",
        "https://example.com/articles/orchard?utm=1",
    ],
)
def test_the_key_may_be_any_normalization_of_the_same_page(key):
    row = make_row("https://example.com/articles/orchard", "text", "2026-03-01T00:00:00Z", key=key)
    assert web_capture.check_row(row, now=NOW) is row


def test_the_key_may_follow_the_final_url():
    row = make_row(
        "https://example.com/short", "text", "2026-03-01T00:00:00Z",
        final_url="https://example.com/long/article", key="https://example.com/long/article",
    )  # fmt: skip
    assert web_capture.check_row(row, now=NOW) is row


def test_a_stamp_from_the_future_is_refused_beyond_a_small_skew():
    base = make_row(*PAGES[0][:1], PAGES[0][2], "2099-01-01T00:00:00Z")
    with pytest.raises(web_capture.RowRefused, match="in the future"):
        web_capture.check_row(base, now=NOW)
    ten_minutes = {**base, "captured_at": "2026-06-01T12:10:00Z"}
    with pytest.raises(web_capture.RowRefused, match="in the future"):
        web_capture.check_row(ten_minutes, now=NOW)
    near = {**base, "captured_at": "2026-06-01T12:01:00Z"}
    assert web_capture.check_row(near, now=NOW) is near


def test_a_future_row_cannot_pin_the_winner(tmp_path):
    url, title, text, _ = PAGES[0]
    pinned = make_row(url, text + " pinned", "2099-01-01T00:00:00Z", title=title)
    real = make_row(url, text, "2026-03-01T00:00:00Z", title=title)
    write_shard(tmp_path / "s.jsonl", [pinned, real])
    items, report = read(tmp_path)
    assert [item.messages[0].text for item in items] == [text]
    assert report["counts"]["refused"] == 1


# --- 9. a folder that cannot be listed is a finding -------------------------------------------


@pytest.mark.skipif(os.geteuid() == 0, reason="root can list any folder")
def test_an_unlistable_subfolder_is_a_finding_and_the_build_is_incomplete(tmp_path):
    project, store, shards = setup(tmp_path)
    write_shard(shards / "captures-001.jsonl", page_rows())
    hidden = shards / "locked"
    write_shard(
        hidden / "inside.jsonl",
        [make_row("https://example.com/x", "x text", "2026-03-01T00:00:00Z")],
    )
    hidden.chmod(0)
    try:
        report = build_library(manifest_for(shards), project_root=project, store_root=store)
    finally:
        hidden.chmod(0o700)
    found = [item for item in report["findings"] if item["code"] == "unreadable_file"]
    assert [(item["scope"], item["path"]) for item in found] == [("web_captures", "locked")]
    assert report["status"]["records"] == 3
    assert report["coverage_complete"] is False


# --- 10. findings are bounded, the overflow is counted exactly --------------------------------


def test_findings_are_capped_and_the_overflow_is_exact(tmp_path):
    total = web_capture.MAX_FINDINGS + 500
    (tmp_path / "junk.jsonl").write_text("x\n" * total, encoding="utf-8")
    items, report = read(tmp_path)
    assert items == []
    assert len(report["findings"]) == web_capture.MAX_FINDINGS
    assert report["findings_overflow"] == 500
    assert report["counts"]["refused"] == total


def test_the_build_report_names_the_truncation(tmp_path):
    project, store, shards = setup(tmp_path)
    write_shard(shards / "a.jsonl", page_rows())
    (shards / "junk.jsonl").write_text("x\n" * (web_capture.MAX_FINDINGS + 7), encoding="utf-8")
    report = build_library(manifest_for(shards), project_root=project, store_root=store)
    truncated = [item for item in report["findings"] if item["code"] == "findings_truncated"]
    assert [(item["scope"], item["count"]) for item in truncated] == [("web_captures", 7)]
    assert len(report["findings"]) == web_capture.MAX_FINDINGS + 1
    assert report["coverage_complete"] is False


# --- 11. a reason never repeats row-supplied text ---------------------------------------------


def test_an_unknown_field_name_is_never_echoed():
    secret = "page text pretending to be a field name"
    row = {**make_row(*PAGES[0][:1], PAGES[0][2], PAGES[0][3]), secret: 1, secret + "2": 2}
    with pytest.raises(web_capture.RowRefused) as refused:
        web_capture.check_row(row, now=NOW)
    assert "page text" not in str(refused.value)
    assert str(refused.value) == "row has 2 unexpected fields"
    single = {**make_row(*PAGES[0][:1], PAGES[0][2], PAGES[0][3]), secret: 1}
    with pytest.raises(web_capture.RowRefused, match="^row has 1 unexpected field$"):
        web_capture.check_row(single, now=NOW)


def test_no_finding_detail_holds_row_supplied_text(tmp_path):
    secret = "kettle thermodynamics"
    row = {**make_row(*PAGES[1][:1], PAGES[1][2], PAGES[1][3]), secret: 1}
    write_shard(tmp_path / "s.jsonl", [row])
    _, report = read(tmp_path)
    assert secret not in json.dumps(report["findings"])


def test_failed_rows_still_tie_break_by_a_stable_hash(tmp_path):
    rows = [
        failed_row(PAGES[0][0], name, "2026-03-20T08:00:00Z")
        for name in web_capture.FAILURE_CLASSES
    ]
    winners = set()
    for seed in range(5):
        order = list(rows)
        random.Random(seed).shuffle(order)
        folder = tmp_path / str(seed)
        for number, row in enumerate(order):
            write_shard(folder / f"{number}.jsonl", [row])
        items, _ = read(folder)
        winners.add(items[0].extra["body_reason"])
    assert len(winners) == 1
