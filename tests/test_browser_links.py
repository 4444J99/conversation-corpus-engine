"""Browser links: the canonical browser-state set exported as one note per URL (synthetic rows)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from conversation_corpus_engine import browser_links
from conversation_corpus_engine.library_documents import iter_documents
from conversation_corpus_engine.web_capture import normalized_url_key, page_id


def _write(canonical: Path, name: str, rows: list[object]) -> None:
    canonical.mkdir(parents=True, exist_ok=True)
    lines = [row if isinstance(row, str) else json.dumps(row) for row in rows]
    (canonical / f"{name}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def canonical(tmp_path):
    root = tmp_path / "canonical"
    shared = {
        "title": "Tide tables",
        "folders": ["Bookmarks Bar", "Sea"],
        "sources": ["chrome", "safari"],
        "added": "2020-01-02T03:04:05+00:00",
    }
    _write(
        root,
        "bookmarks",
        [
            {"url": "https://www.example.com/tides/?utm_source=x", **shared},
            {"url": "ftp://example.com/file", "title": "ftp"},
            {"url": "https://user:pw@example.com/secret", "title": "creds"},
            "not json",
            ["not", "an", "object"],
        ],
    )
    _write(
        root,
        "reading-list",
        [
            {
                "url": "https://example.com/tides",
                "title": "Tides",
                "added": "1601-01-01T00:00:00+00:00",
            }
        ],
    )
    _write(
        root,
        "history",
        [
            {
                "url": "https://example.com/tides",
                "visit_count": 9,
                "first_seen": "2026-01-01T00:00:00+00:00",
            },
            {"url": "https://rarely.example.org/a", "title": "Rare", "visit_count": 1},
            {"url": "https://private.test/inbox", "title": "Inbox", "visit_count": 50},
        ],
    )
    return root


def test_one_note_per_normalized_url_merging_lists(canonical, tmp_path):
    receipt = browser_links.export_browser_links(canonical, tmp_path / "out")
    notes = list((tmp_path / "out" / "links").rglob("*.md"))
    assert len(notes) == 1  # history is opt in; bookmark and reading-list rows are one URL
    text = notes[0].read_text()
    key = normalized_url_key("https://example.com/tides")
    assert notes[0].name == f"{page_id(key)}.md"
    assert "Saved in: Bookmark, Reading list" in text
    assert "Folders: Bookmarks Bar; Sea" in text
    assert "Browsers: chrome, safari" in text
    assert "Added: 2020-01-02T03:04:05+00:00" in text  # the 1601 date is an unset date
    assert text.startswith("# Tide tables\n")
    assert receipt["counts"]["links"] == 1 and receipt["counts"]["refused"] == 4


def test_refusals_name_file_and_line_never_the_address(canonical, tmp_path):
    receipt = browser_links.export_browser_links(canonical, tmp_path / "out")
    refused = [f for f in receipt["findings"] if f["code"] == "refused"]
    assert sorted(f["line"] for f in refused) == [2, 3, 4, 5]
    assert "pw@" not in json.dumps(receipt)
    assert "secret" not in json.dumps(receipt)


def test_history_is_opt_in_and_narrowed(canonical, tmp_path):
    receipt = browser_links.export_browser_links(
        canonical,
        tmp_path / "out",
        kinds=("bookmarks", "history"),
        min_visits=5,
        exclude_hosts=["private.test"],
    )
    assert receipt["counts"]["links"] == 1
    assert receipt["counts"]["below_min_visits"] == 1
    assert receipt["counts"]["excluded_host"] == 1
    note = next((tmp_path / "out" / "links").rglob("*.md")).read_text()
    assert "Saved in: Bookmark, History" in note and "Visits: 9" in note


def test_refresh_writes_only_changes_and_deletes_nothing(canonical, tmp_path):
    out = tmp_path / "out"
    first = browser_links.export_browser_links(canonical, out)
    again = browser_links.export_browser_links(canonical, out)
    assert first["counts"]["written"] == 1
    assert again["counts"]["written"] == 0 and again["counts"]["unchanged"] == 1
    _write(canonical, "bookmarks", [{"url": "https://other.example.net/", "title": "Other"}])
    _write(canonical, "reading-list", [])
    third = browser_links.export_browser_links(canonical, out)
    assert third["counts"]["written"] == 1
    assert third["counts"]["retained_not_in_source"] == 1
    assert len(list((out / "links").rglob("*.md"))) == 2


def test_a_missing_list_is_a_finding(tmp_path):
    root = tmp_path / "canonical"
    _write(root, "bookmarks", [{"url": "https://a.example.com/", "title": "A"}])
    receipt = browser_links.export_browser_links(root, tmp_path / "out")
    assert {"file": "reading-list.jsonl", "line": 0, "code": "list_missing"} in receipt["findings"]


def test_destination_must_be_new_or_an_earlier_export(canonical, tmp_path):
    other = tmp_path / "occupied"
    other.mkdir()
    (other / "stranger.txt").write_text("x")
    with pytest.raises(ValueError, match="not empty"):
        browser_links.export_browser_links(canonical, other)
    with pytest.raises(ValueError, match="unknown link list"):
        browser_links.export_browser_links(canonical, tmp_path / "o2", kinds=("nope",))


def test_destination_inside_a_git_work_tree_is_refused(canonical, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    with pytest.raises(Exception, match="(?i)private|git"):
        browser_links.export_browser_links(canonical, repo / "links-out")


def test_the_export_is_readable_as_a_documents_root(canonical, tmp_path):
    receipt = browser_links.export_browser_links(canonical, tmp_path / "out")
    report: dict = {}
    records = list(iter_documents([receipt["manifest_entry"]], report=report))
    assert len(records) == 1
    assert "Tide tables" in json.dumps(records[0].to_dict())
    assert report["counts"]["ingested"] == 1
