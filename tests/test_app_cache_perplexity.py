from __future__ import annotations

import argparse
import base64
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conversation_corpus_engine import (
    app_cache_commands,
    app_cache_perplexity,
    archive_read_perplexity,
)  # noqa: E402
from conversation_corpus_engine.app_cache_sqlite import SnapshotError, snapshot  # noqa: E402
from conversation_corpus_engine.archive_model import SourceTracker  # noqa: E402

SPACE = "0a0a0a0a-1111-2222-3333-444444444444"
THREAD = "bbbbbbbb-1111-2222-3333-444444444444"


def item_id(item: str) -> str:
    raw = json.dumps({"sectionId": f"space:{SPACE}", "itemId": item}).encode()
    return "KCI:" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def graphql(updated: str, text: str) -> str:
    node = {
        "id": item_id("concepts/synthetic"),
        "name": "Synthetic Note",
        "updatedAt": updated,
        "groupId": "concepts",
        "content": {"text": text, "truncated": False},
    }
    categories = [{"items": {"edges": [{"node": node}]}}]
    return json.dumps(
        {"viewer": {"knowledgeContext": {"scopeBySpaceUUID": {"categories": categories}}}}
    )


def sse(*texts: str) -> str:
    event = {"related_query_items": [{"text": t} for t in texts]}
    return f"event: message\ndata: {json.dumps(event)}\n\nevent: end_of_stream\ndata: {{}}\n\n"


@pytest.fixture
def cache(tmp_path: Path) -> tuple[Path, Path]:
    bodies = tmp_path / "fsCachedData"
    bodies.mkdir()
    (bodies / "AAAAAAAA-0000-0000-0000-000000000001").write_text(
        graphql("2026-11-09T20:01:00Z", "# Synthetic Note\nbody")
    )
    db = tmp_path / "Cache.db"
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(
        "CREATE TABLE cfurl_cache_response(entry_ID INTEGER PRIMARY KEY, request_key TEXT, time_stamp TEXT)"
    )
    con.execute(
        "CREATE TABLE cfurl_cache_receiver_data(entry_ID INTEGER PRIMARY KEY, isDataOnFS INTEGER, receiver_data BLOB)"
    )
    rows = [
        (
            1,
            "https://www.perplexity.ai/api/graphql/MobileProjectBrainPages/h?sig=zz",
            "2026-11-09 20:03:13",
            1,
            "AAAAAAAA-0000-0000-0000-000000000001",
        ),
        (
            2,
            f"https://www.perplexity.ai/rest/sse/related-queries/{THREAD}",
            "2026-11-10 21:32:18",
            0,
            sse("one", "two").encode(),
        ),
        (
            3,
            "https://www.perplexity.ai/rest/sse/related-queries/cccccccc-1111",
            "2026-11-10 21:40:00",
            0,
            b"",
        ),
        (
            4,
            "https://www.perplexity.ai/rest/browser/favicon?url=x",
            "2026-11-10 21:41:00",
            0,
            b"icon",
        ),
        (5, "https://example.test/other", "2026-11-10 21:42:00", 0, b"zzz"),
    ]
    for entry, key, stamp, on_fs, data in rows:
        con.execute("INSERT INTO cfurl_cache_response VALUES (?,?,?)", (entry, key, stamp))
        con.execute("INSERT INTO cfurl_cache_receiver_data VALUES (?,?,?)", (entry, on_fs, data))
    con.commit()  # rows stay in the -wal: the connection is left open so no checkpoint runs
    keep.append(con)
    return db, bodies


keep: list[sqlite3.Connection] = []


def test_scan_reads_notes_and_followups_through_a_copy(cache: tuple[Path, Path]) -> None:
    db, bodies = cache
    before = db.stat()
    result = app_cache_perplexity.scan(db, bodies)
    assert db.stat().st_mtime_ns == before.st_mtime_ns
    assert list(result.spaces) == [SPACE]
    assert result.followups[THREAD]["texts"] == ["one", "two"]
    assert result.followups[THREAD]["seen"] == "2026-11-10T21:32:18Z"
    assert result.other_perplexity["/rest/browser/favicon"] == 1
    assert result.unreadable["empty-or-missing-body"] == 1


def test_staging_ingests_with_the_existing_reader_and_tags_generated_text(
    cache: tuple[Path, Path], tmp_path: Path
) -> None:
    db, bodies = cache
    out = tmp_path / "stage"
    report = app_cache_perplexity.write_staging(app_cache_perplexity.scan(db, bodies), out)
    assert report["space_notes"] == 1 and report["threads_with_followup_suggestions"] == 1
    assert "Synthetic Note" not in (out / "coverage.json").read_text()
    found = list(
        archive_read_perplexity.iter_conversations(
            out, SourceTracker(out, archive_read_perplexity.READER)
        )
    )
    assert len(found) == 2
    assert {m.role for c in found for m in c.messages} == {"assistant"}
    followup = next(c for c in found if "follow-up" in c.title)
    assert "- one" in followup.messages[0].text and followup.model == "COMPUTER_FOLLOWUP"


def test_clipboard_history_is_counted_never_read(cache: tuple[Path, Path], tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    (shared / "clipboard-history" / "metadata").mkdir(parents=True)
    (shared / "clipboard-history" / "metadata" / "a.json").write_text("SECRET-LOOKING")
    report = app_cache_perplexity.coverage(app_cache_perplexity.scan(*cache), shared)
    assert report["not_read"]["shared_clipboard_history_files"] == 1
    assert "SECRET-LOOKING" not in json.dumps(report)


def test_snapshot_refuses_a_missing_database(tmp_path: Path) -> None:
    with pytest.raises(SnapshotError), snapshot(tmp_path / "none.db"):
        pass


def test_cli(cache: tuple[Path, Path], tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    parser = argparse.ArgumentParser()
    app_cache_commands.add_app_cache_parser(parser.add_subparsers(dest="group"))
    db, bodies = cache
    out = tmp_path / "cli"
    args = parser.parse_args(
        [
            "app-cache",
            "perplexity",
            "--cache-db",
            str(db),
            "--body-dir",
            str(bodies),
            "--out",
            str(out),
            "--json",
        ]
    )
    assert app_cache_commands.handle_app_cache(args) == 0
    assert json.loads(capsys.readouterr().out)["space_notes"] == 1
    assert app_cache_commands.handle_app_cache(args) == 2
