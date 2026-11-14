"""A very long record never runs SQLite's snippet(), which cannot be interrupted and grows with
the text length times the number of matches (a stop word in a multi-megabyte record took over a
minute). Its excerpt is cut from the text around the first query word instead. Synthetic data."""

import pytest

from conversation_corpus_engine import library_store
from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.library_store import Library, _windowed_excerpt, query_terms


def conv(identity: str, text: str) -> Conversation:
    return Conversation(
        provider="chatgpt",
        source_id=identity,
        title=f"Note {identity}",
        messages=[Message(role="user", text=text)],
        extra={"wing": "ai", "lane": "conversations", "body_status": "full"},
    )


LONG = "filler " * 400 + "the needle sits here " + "more filler " * 400


@pytest.fixture
def path(tmp_path):
    target = tmp_path / "long.sqlite"
    with Library.create(target) as library:
        library.put(conv("short", "the needle in a short note"))
        library.put(conv("long", LONG))
        library.put(conv("stemmed", "filler " * 400 + "needles everywhere " + "filler " * 400))
    return target


def by_title(result):
    return {hit["title"]: hit for hit in result["hits"]}


def test_a_long_record_gets_a_windowed_excerpt_and_a_short_one_keeps_its_snippet(path, monkeypatch):
    monkeypatch.setattr(library_store, "SNIPPET_MAX_CHARS", 1000)
    with Library(path) as library:
        result = library.search("needle", limit=10)
    hits = by_title(result)
    assert "[needle]" in hits["Note long"]["excerpt"] and len(hits["Note long"]["excerpt"]) <= 360
    assert hits["Note long"]["excerpt_plain"] is True
    assert "excerpt_plain" not in hits["Note short"] and "[needle]" in hits["Note short"]["excerpt"]
    # The stemmed match ("needles") is not the written word: the start of the text is shown.
    assert hits["Note stemmed"]["excerpt"].startswith("filler filler")
    assert result["plain_excerpts"] == 2 and "very long texts" in result["notice"]
    assert result["ranked"] is True and result["partial"] is False


def test_the_ranking_and_the_total_do_not_depend_on_how_the_excerpt_is_built(path, monkeypatch):
    with Library(path) as library:
        baseline = library.search("needle", limit=10)
    monkeypatch.setattr(library_store, "SNIPPET_MAX_CHARS", 1000)
    with Library(path) as library:
        windowed = library.search("needle", limit=10)
    assert [hit["uid"] for hit in windowed["hits"]] == [hit["uid"] for hit in baseline["hits"]]
    assert windowed["total"] == baseline["total"] == 3
    assert "plain_excerpts" not in baseline and "notice" not in baseline


def test_a_long_record_never_runs_snippet(path, monkeypatch):
    monkeypatch.setattr(library_store, "SNIPPET_MAX_CHARS", 1000)
    statements: list[str] = []
    with Library(path) as library:
        library.connection.set_trace_callback(statements.append)
        library.search("needle", limit=10)
    snippets = [s for s in statements if "snippet(" in s]
    # One statement for the short page rows; none per long record.
    assert len(snippets) == 1 and "rowid IN" in snippets[0]


def test_a_page_of_only_long_records_runs_no_snippet_at_all(path, monkeypatch):
    monkeypatch.setattr(library_store, "SNIPPET_MAX_CHARS", 10)
    statements: list[str] = []
    with Library(path) as library:
        library.connection.set_trace_callback(statements.append)
        result = library.search("needle", limit=10)
    assert not [s for s in statements if "snippet(" in s]
    assert result["total"] == 3 and len(result["hits"]) == 3


def test_the_passage_unit_uses_the_same_excerpt_rule(tmp_path, monkeypatch):
    prose = (
        "The luminous crystal lattice records a detailed account of the observation, its historical "
        "setting, the original evidence and a carefully qualified interpretation."
    )
    target = tmp_path / "passage.sqlite"
    with Library.create(target) as library:
        library.put(conv("p", prose))
        library.index_blocks()
    monkeypatch.setattr(library_store, "SNIPPET_MAX_CHARS", 10)
    with Library(target) as library:
        result = library.search("luminous crystal", limit=10, unit="passage")
    assert result["hits"] and all("passage" in hit for hit in result["hits"])
    assert (
        result["hits"][0]["excerpt_plain"] is True and "[luminous]" in result["hits"][0]["excerpt"]
    )


def test_the_window_marks_every_query_word_and_marks_a_cut_with_ellipses():
    text = "x" * 500 + " alpha then beta " + "y" * 500
    excerpt = _windowed_excerpt(text, query_terms("alpha beta"))
    assert "[alpha]" in excerpt and "[beta]" in excerpt
    assert excerpt.startswith("...") and excerpt.endswith("...")
    assert _windowed_excerpt("alpha at the start", query_terms("alpha")).startswith("[alpha]")
    assert _windowed_excerpt("nothing matches", query_terms("zzz")) == "nothing matches"


def test_a_phrase_query_marks_the_words_of_its_phrase():
    excerpt = _windowed_excerpt("one two three four", query_terms('"two three"'))
    assert "[two]" in excerpt and "[three]" in excerpt
