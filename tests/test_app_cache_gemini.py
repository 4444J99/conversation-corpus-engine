from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conversation_corpus_engine import app_cache_commands, app_cache_gemini  # noqa: E402
from conversation_corpus_engine.app_cache_sqlite import snapshot  # noqa: E402

SENTINEL = b"\x2e\xba\xfe\xca\x2e\xba\xfe\xca-SYNTHETIC-CIPHERTEXT"
DAY = 86400.0
# Core Data seconds since 2001-01-01: 2026-10-17 is.
OCT_17 = 813_888_000.0  # 2026-10-17T00:00:00Z


@pytest.fixture
def store(tmp_path: Path) -> Path:
    path = tmp_path / "ChatInfo2.store"
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(
        "CREATE TABLE ZCHATINFOSTOREDMODEL (Z_PK INTEGER PRIMARY KEY, ZLASTUPDATEDTIME TIMESTAMP,"
        " ZCHATUUID VARCHAR, ZROBINCONVERSATIONID VARCHAR, ZENCRYPTEDPROTOBYTES BLOB)"
    )
    con.execute(
        "CREATE TABLE ZCHATMESSAGESTOREDMODEL (Z_PK INTEGER PRIMARY KEY, ZBYTESIZE INTEGER,"
        " ZHASTASKARTIFACTS INTEGER, ZISUSERTURN INTEGER, ZMESSAGEINDEX INTEGER,"
        " ZCREATEDTIME TIMESTAMP, ZCHATUUID VARCHAR, ZMESSAGEUUID VARCHAR,"
        " ZENCRYPTEDPROTOBYTES BLOB)"
    )
    con.execute(
        "INSERT INTO ZCHATINFOSTOREDMODEL VALUES (1, ?, 'c_one', 'c_robin', ?)",
        (OCT_17 - DAY, SENTINEL),
    )
    con.execute(
        "INSERT INTO ZCHATINFOSTOREDMODEL VALUES (2, ?, 'c_two', NULL, ?)",
        (OCT_17 + 5 * DAY, SENTINEL),
    )
    for index, (chat, user) in enumerate([("c_two", 1), ("c_two", 0), ("c_orphan", 1)]):
        con.execute(
            "INSERT INTO ZCHATMESSAGESTOREDMODEL VALUES (?, 100, 0, ?, ?, ?, ?, ?, ?)",
            (index, user, index, OCT_17 + 5 * DAY, chat, f"m{index}", SENTINEL),
        )
    con.commit()
    keep.append(con)
    return path


keep: list[sqlite3.Connection] = []


def test_census_counts_metadata_and_reports_the_export_path(store: Path) -> None:
    report = app_cache_gemini.census(store)
    assert report["chats"] == 2 and report["chats_after_2026_10_16"] == 1
    assert report["chats_updated_first"] == "2026-10-16"
    assert report["cached_messages"] == 3 and report["cached_user_turns"] == 2
    assert report["cached_messages_without_a_chat_row"] == 1
    assert report["cached_message_bytes"] == 300
    assert (
        report["owner_export"]["file_the_reader_detects"]
        == "My Activity/Gemini Apps/MyActivity.json"
    )
    assert "SYNTHETIC" not in json.dumps(report)


def test_the_encrypted_column_cannot_be_read_even_deliberately(store: Path) -> None:
    with snapshot(store) as db:
        db.set_authorizer(app_cache_gemini._deny_encrypted)
        with pytest.raises(sqlite3.DatabaseError):
            db.execute("SELECT ZENCRYPTEDPROTOBYTES FROM ZCHATINFOSTOREDMODEL").fetchall()
        assert db.execute("SELECT count(*) FROM ZCHATINFOSTOREDMODEL").fetchone() == (2,)


def test_report_files_keep_ids_out_of_coverage(store: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    public = app_cache_gemini.write_report(app_cache_gemini.census(store), out)
    assert "inventory" not in public
    assert "c_two" in (out / "gemini-store-inventory.json").read_text()
    assert "c_two" not in (out / "coverage.json").read_text()
    assert (out.stat().st_mode & 0o777) == 0o700
    with pytest.raises(FileExistsError):
        app_cache_gemini.write_report(public, out)


def test_cli(store: Path, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    parser = argparse.ArgumentParser()
    app_cache_commands.add_app_cache_parser(parser.add_subparsers(dest="group"))
    args = parser.parse_args(
        ["app-cache", "gemini", "--store", str(store), "--out", str(tmp_path / "c"), "--json"]
    )
    assert app_cache_commands.handle_app_cache(args) == 0
    assert json.loads(capsys.readouterr().out)["chats"] == 2
    assert app_cache_commands.handle_app_cache(args) == 2
