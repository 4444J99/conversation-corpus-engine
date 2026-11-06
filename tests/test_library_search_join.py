"""Library search joins its search rows to records by row number, not by the stored ``uid`` text.

Reading the stored ``uid`` column of a full-text row loads the whole stored conversation body of
that row. Joining on it made a common-word search touch every matching body, so a search over a
large library spent its whole time budget. These tests use synthetic libraries only.
"""

import sqlite3

import pytest

from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.library_store import Library


def conv(identity: str, text: str) -> Conversation:
    return Conversation(
        provider="chatgpt",
        source_id=identity,
        title=f"Note {identity}",
        messages=[Message(role="user", text=text)],
        extra={"wing": "ai", "lane": "conversations", "body_status": "full"},
    )


def tamper(path, *statements):
    """Run raw writes against a library file; ``Library(path)`` itself opens it read-only."""
    connection = sqlite3.connect(path)
    try:
        for sql, params in statements:
            connection.execute(sql, params)
        connection.commit()
    finally:
        connection.close()


@pytest.fixture
def path(tmp_path):
    target = tmp_path / "join.sqlite"
    with Library.create(target) as library:
        library.put(conv("one", "alpha beta shared"))
        library.put(conv("two", "alpha gamma shared"))
        library.put(conv("three", "delta epsilon"))
    return target


def test_search_answers_from_the_record_not_from_the_stored_search_uid(path):
    expected = {conv("one", "x").uid, conv("two", "x").uid}
    with Library(path) as library:
        before = library.search("alpha shared")
    tamper(path, ("UPDATE record_fts SET uid='tampered'", ()))
    with Library(path) as library:
        after = library.search("alpha shared")
    assert {hit["uid"] for hit in before["hits"]} == expected
    # The join no longer reads the stored uid, so tampering with it cannot change an answer.
    assert {hit["uid"] for hit in after["hits"]} == expected
    assert after["total"] == before["total"] == 2


def test_filters_and_excerpts_still_come_from_the_joined_record(path):
    with Library(path) as library:
        result = library.search("alpha", providers=["chatgpt"], kinds=None)
        none = library.search("alpha", providers=["claude"])
    assert result["total"] == 2
    assert all("[alpha]" in hit["excerpt"] for hit in result["hits"])
    assert none["total"] == 0 and none["hits"] == []


def test_verify_accepts_an_aligned_library(path):
    with Library(path) as library:
        assert library.verify() == []


def test_verify_reports_search_rows_stored_under_the_wrong_record_row(path):
    with Library(path) as library:
        rows = library.connection.execute("SELECT rowid,uid FROM record_fts ORDER BY rowid LIMIT 2")
        (first_rowid, first_uid), (second_rowid, second_uid) = [tuple(row) for row in rows]
    # Swap the stored identities: every earlier check still passes (counts equal, no
    # duplicates, nothing absent), but each record now sits under the other's search row.
    tamper(
        path,
        ("UPDATE record_fts SET uid=? WHERE rowid=?", (second_uid, first_rowid)),
        ("UPDATE record_fts SET uid=? WHERE rowid=?", (first_uid, second_rowid)),
    )
    with Library(path) as library:
        problems = library.verify(full=False)
    assert "search identity is not aligned with its record row" in problems
    assert not any("duplicate" in problem or "absent" in problem for problem in problems)


def test_verify_reports_a_record_without_a_search_row_at_its_own_row(path):
    tamper(
        path,
        ("DELETE FROM record_fts WHERE rowid=(SELECT MIN(rowid) FROM record_fts)", ()),
    )
    with Library(path) as library:
        problems = library.verify(full=False)
    assert "search identity is not aligned with its record row" in problems


def test_the_ranking_statement_does_not_build_excerpts_for_every_match(path):
    """SQLite computes every selected column of every match before it sorts, so the statement
    that sorts by relevance must not select the excerpt; a second statement reads just the page."""
    statements: list[str] = []
    with Library(path) as library:
        library.connection.set_trace_callback(statements.append)
        result = library.search("alpha shared", limit=1)
    ranking = [s for s in statements if "bm25" in s]
    excerpts = [s for s in statements if "snippet(" in s]
    assert len(ranking) == 1 and "snippet(" not in ranking[0]
    assert len(excerpts) == 1 and "bm25" not in excerpts[0]
    # Paging still works: one more row than the page decides whether another page follows.
    assert len(result["hits"]) == 1 and result["next_offset"] == 1 and result["total"] == 2


def test_two_pages_of_a_ranked_search_do_not_repeat_or_skip_a_record(path):
    with Library(path) as library:
        first = library.search("alpha shared", limit=1, offset=0)
        second = library.search("alpha shared", limit=1, offset=1)
    seen = [hit["uid"] for hit in first["hits"]] + [hit["uid"] for hit in second["hits"]]
    assert sorted(seen) == sorted({conv("one", "x").uid, conv("two", "x").uid})
    assert second["next_offset"] is None
