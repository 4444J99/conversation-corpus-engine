"""Library search joins its search rows to records by row number, not by the stored ``uid`` text.

Reading the stored ``uid`` column of a full-text row loads the whole stored conversation body of
that row. Joining on it made a common-word search touch every matching body, so a search over a
large library spent its whole time budget. These tests use synthetic libraries only.
"""

import sqlite3

import pytest

from conversation_corpus_engine import library_store
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


@pytest.mark.parametrize("compact", [False, True])
def test_a_next_generation_keeps_search_rows_under_their_record_rows(path, tmp_path, compact):
    """Both ways of inheriting a generation (online backup and VACUUM INTO) must keep the
    row-number alignment the search join relies on, including after a record is revised."""
    following = tmp_path / ("compact.sqlite" if compact else "backup.sqlite")
    with Library.create(following, previous=path, compact_previous=compact) as library:
        library.put(conv("two", "alpha gamma shared and revised"))
        library.put(conv("four", "alpha newcomer shared"))
    with Library(following) as library:
        assert library.verify(full=False) == []
        result = library.search("alpha shared")
        revised = library.search("revised")
    expected = {conv(name, "x").uid for name in ("one", "two", "four")}
    assert {hit["uid"] for hit in result["hits"]} == expected
    assert [hit["uid"] for hit in revised["hits"]] == [conv("two", "x").uid]


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def crowd(tmp_path, monkeypatch):
    """Three hundred records that all hold ``alpha`` and ``beta``, never adjacent."""
    target = tmp_path / "crowd.sqlite"
    with Library.create(target) as library:
        for number in range(300):
            library.put(conv(f"m{number:03d}", f"alpha filler {number} beta more filler {number}"))
    # A tiny instruction interval makes the progress handler fire inside even a small statement.
    monkeypatch.setattr(library_store, "SEARCH_PROGRESS_INSTRUCTIONS", 10)
    return target


def test_a_budget_that_expires_while_the_excerpts_are_read_returns_a_coherent_partial_page(
    crowd, monkeypatch
):
    clock = FakeClock()
    monkeypatch.setattr(library_store, "_clock", clock)
    with Library(crowd) as library:

        def trace(statement: str) -> None:
            # The second ranking step is the only statement that selects an excerpt for a page.
            if "rowid IN" in statement and "snippet(" in statement:
                clock.now = 100.0

        library.connection.set_trace_callback(trace)
        result = library.search("alpha beta", limit=5, max_seconds=1.0)
    assert result["partial"] is True and result["ranked"] is False
    assert result["total"] == 300 and result["total_exact"] is True
    assert [hit["uid"] for hit in result["hits"]] == [
        conv(f"m{number:03d}", "x").uid for number in range(5)
    ]
    assert all("key" not in hit for hit in result["hits"])
    assert result["next_offset"] == 5


def test_both_ranking_steps_read_one_snapshot_that_a_writer_cannot_change_between_them(crowd):
    """A commit between the two statements used to drop a ranked row from the page."""
    outcome: dict[str, str] = {}
    with Library(crowd) as library:
        baseline = library.search("alpha beta", limit=5)

        def trace(statement: str) -> None:
            if "rowid IN" in statement and "snippet(" in statement and "writer" not in outcome:
                writer = sqlite3.connect(crowd, timeout=0)
                try:
                    writer.execute("UPDATE record_fts SET body='nothing here now'")
                    writer.commit()
                    outcome["writer"] = "committed"
                except sqlite3.OperationalError as error:
                    outcome["writer"] = str(error)
                finally:
                    writer.close()

        library.connection.set_trace_callback(trace)
        raced = library.search("alpha beta", limit=5)
        library.connection.set_trace_callback(None)
        assert not library.connection.in_transaction
    # The reader's snapshot holds the write lock off for the length of the page, so the writer
    # cannot commit between the steps and the page is exactly the baseline.
    assert outcome["writer"] == "database is locked"
    assert [hit["uid"] for hit in raced["hits"]] == [hit["uid"] for hit in baseline["hits"]]
    assert len(raced["hits"]) == 5
    # The lock is released afterwards: a writer can commit once the search has returned.
    writer = sqlite3.connect(crowd, timeout=0)
    try:
        writer.execute("UPDATE record_fts SET body='nothing here now' WHERE rowid=1")
        writer.commit()
    finally:
        writer.close()
