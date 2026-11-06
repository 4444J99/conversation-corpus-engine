"""Hostile and degenerate inputs to the library search planner, on synthetic libraries only."""

import sqlite3
import time

import pytest
from test_library_search_planner import legacy_search

from conversation_corpus_engine import library_store
from conversation_corpus_engine.archive_database import PHRASE_RE, WORD_RE, fts_query, fts_terms
from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.library_store import (
    MAX_QUERY_CHARS,
    MAX_QUERY_TERMS,
    MAX_TERM_CHARS,
    Library,
    plan_locator_query,
    plan_match,
    validate_max_seconds,
    validate_search_seconds,
)


def conv(identity: str, text: str) -> Conversation:
    return Conversation(
        provider="chatgpt",
        source_id=identity,
        title=f"Note {identity}",
        messages=[Message(role="user", text=text)],
        extra={"wing": "ai", "lane": "conversations", "body_status": "full"},
    )


def uids(result: dict) -> list[str]:
    return [hit["uid"] for hit in result["hits"]]


COMMON = 50
WORDS = [f"w{number:02d}" for number in range(40)]


@pytest.fixture(scope="module")
def hardened(tmp_path_factory):
    path = tmp_path_factory.mktemp("hardened") / "library.sqlite"
    with Library.create(path) as library:
        for number in range(COMMON):
            library.put(conv(f"c{number:02d}", f"omega filler common words number {number} alpha"))
        library.put(conv("thirty-two", " ".join(WORDS[:MAX_QUERY_TERMS])))
        library.put(conv("zeta", "zeta stands alone here"))
        library.put(conv("long-token", "q" * MAX_TERM_CHARS))
    with Library(path) as library:
        yield library


# --- bounded queries (every plan) ----------------------------------------------------------------

HUGE = ("alpha " * 17000)[:100000]


@pytest.mark.parametrize("plan", ["auto", "phrase", "and", "or"])
def test_a_hundred_thousand_character_query_is_answered_quickly_by_every_plan(hardened, plan):
    started = time.monotonic()
    result = hardened.search(HUGE, plan=plan, limit=5, max_seconds=2.0)
    assert time.monotonic() - started < 2.0
    assert result["terms_truncated"] is True and "shortened" in result["notice"]
    assert result["partial"] is False
    if plan != "phrase":
        # Only the first thousand characters are examined and the repeats collapse to one term.
        assert result["total"] == COMMON and len(result["hits"]) == 5


def test_the_hard_cap_examines_only_the_first_thousand_characters(hardened):
    query = "zeta" + " " * (MAX_QUERY_CHARS + 50) + "nosuchword"
    result = hardened.search(query, plan="and")
    # "nosuchword" lies past the cap and is ignored, so the lone term "zeta" matches.
    assert result["total"] == 1 and result["terms_truncated"] is True
    assert plan_match(query, "and") == '"zeta"'
    assert hardened.search("zeta nosuchword", plan="and")["total"] == 0


def test_at_most_thirty_two_distinct_terms_are_kept_for_every_plan(hardened):
    query = " ".join(WORDS)  # forty distinct words; the record holds only the first thirty-two
    anded = hardened.search(query, plan="and")
    assert anded["total"] == 1 and anded["terms_truncated"] is True
    ored = hardened.search(query, plan="or")
    assert ored["terms_truncated"] is True and ored["total"] == 1
    phrase = hardened.search(query, plan="phrase")
    assert phrase["total"] == 1 and phrase["terms_truncated"] is True
    assert plan_match(query, "and").count('"') == 2 * MAX_QUERY_TERMS
    assert plan_match(query, "or").count(" OR ") == MAX_QUERY_TERMS - 1
    assert plan_match(query, "phrase") == '"' + " ".join(WORDS[:MAX_QUERY_TERMS]) + '"'


def test_a_query_within_the_limits_reports_nothing_dropped(hardened):
    for query in (" ".join(WORDS[:MAX_QUERY_TERMS]), "alpha omega", "zeta"):
        result = hardened.search(query)
        assert "terms_truncated" not in result and "notice" not in result


def test_a_term_is_cut_to_sixty_four_characters(hardened):
    query = "q" * (MAX_TERM_CHARS + 36)
    result = hardened.search(query, plan="and")
    assert result["total"] == 1 and result["terms_truncated"] is True
    assert plan_match(query, "and") == '"' + "q" * MAX_TERM_CHARS + '"'


def test_repeated_terms_collapse_case_insensitively_without_reporting_a_drop(hardened):
    assert plan_match("Alpha alpha ALPHA omega omega", "and") == '"Alpha" "omega"'
    assert plan_match("Alpha alpha ALPHA omega omega", "or") == '"Alpha" OR "omega"'
    result = hardened.search("Alpha alpha ALPHA omega omega", plan="and")
    assert result["total"] == COMMON and "terms_truncated" not in result
    # The phrase plan keeps repeats: they are positions, not terms.
    assert plan_match("alpha alpha omega", "phrase") == '"alpha alpha omega"'


def test_quoted_phrases_share_the_same_limits(hardened):
    many = " ".join(f'"p{number:02d} x"' for number in range(MAX_QUERY_TERMS + 5))
    match = plan_match(many, "and")
    assert match.count('"') == 2 * MAX_QUERY_TERMS
    long_phrase = '"' + " ".join(["q"] * 100) + '"'
    assert len(plan_match(long_phrase, "and")) <= MAX_TERM_CHARS + 2
    assert hardened.search(many, plan="and")["terms_truncated"] is True


def test_the_passage_locator_query_is_bounded_too(hardened):
    for plan in ("and", "or", "phrase"):
        assert len(plan_locator_query(HUGE, plan)) < 250
    assert plan_locator_query("alpha omega", "and") == "alpha omega"
    assert plan_locator_query('"alpha omega" zeta', "or") == '"alpha omega" zeta'
    assert plan_locator_query("alpha omega", "phrase") == '"alpha omega"'


def test_queries_within_the_limits_behave_exactly_as_before(hardened):
    for query in ("alpha", "omega alpha", "zeta stands", "alpha alpha omega", "OMEGA Alpha"):
        new = hardened.search(query, plan="and", limit=10)
        expected = hardened.connection.execute(
            "SELECT COUNT(*) FROM record_fts WHERE record_fts MATCH ?",
            [" ".join(f'"{word}"' for word in query.split())],
        ).fetchone()[0]
        assert new["total"] == expected


def test_a_clip_that_lands_inside_a_word_drops_that_fragment():
    query = "alpha " * 166 + "omegazzz"  # the thousandth character falls inside "omegazzz"
    assert len(query) > MAX_QUERY_CHARS and not query[MAX_QUERY_CHARS].isspace()
    assert plan_match(query, "and") == '"alpha"'
    # One giant token has no earlier boundary, so its first characters are kept (then cut again).
    assert plan_match("x" * 5000, "and") == '"' + "x" * MAX_TERM_CHARS + '"'


# --- numeric arguments never overflow ------------------------------------------------------------

HUGE_INT = 10**400


@pytest.mark.parametrize("value", [HUGE_INT, -HUGE_INT])
@pytest.mark.parametrize("name", ["max_seconds", "count_cap", "limit", "offset"])
def test_a_huge_integer_is_a_value_error_for_every_numeric_argument(hardened, name, value):
    """float() of such an integer overflows; the validators must report it as invalid input."""
    with pytest.raises(ValueError, match=name):
        hardened.search("alpha", **{name: value})


@pytest.mark.parametrize("value", [HUGE_INT, -HUGE_INT])
def test_the_budget_validators_reject_a_huge_integer_with_their_standard_message(value):
    for validator in (validate_max_seconds, validate_search_seconds):
        with pytest.raises(ValueError, match="max_seconds must be a number"):
            validator(value)


# --- control characters in a query ---------------------------------------------------------------

CONTROLS = ["\x00", "\x01", "\x08", "\x1f", "\x7f", "\t", "\n", "\r"]


def result_core(result: dict) -> tuple:
    return result["total"], uids(result), result["plan"]


@pytest.mark.parametrize("control", CONTROLS)
@pytest.mark.parametrize("plan", ["auto", "phrase", "and", "or"])
def test_a_control_character_inside_a_quoted_phrase_acts_as_a_space(hardened, control, plan):
    """A NUL used to end the FTS5 string early and raise "unterminated string" (also in base)."""
    expected = hardened.search('"zeta stands"', plan=plan)
    assert expected["total"] == 1
    assert result_core(hardened.search(f'"zeta{control}stands"', plan=plan)) == result_core(
        expected
    )
    assert result_core(hardened.search(f'x "zeta{control}stands" y', plan=plan))[0] in (0, 1)
    assert hardened.search(f"zeta{control}stands", plan=plan)["total"] == 1


def test_the_base_search_fails_on_a_nul_and_the_planner_does_not(hardened):
    with pytest.raises(sqlite3.OperationalError, match="unterminated string"):
        legacy_search(hardened, '"zeta\x00stands"')
    assert hardened.search('"zeta\x00stands"')["total"] == 1


def test_fts_query_replaces_control_characters_and_leaves_clean_text_alone():
    assert fts_query('"alpha\x00beta"') == '"alpha beta"'
    assert fts_query('"alpha\x01\x1f\x7fbeta" gamma') == '"alpha   beta" "gamma"'
    assert fts_terms('"a\tb"') == (["a b"], [])
    for text in ('"alpha beta" gamma', "alpha-beta don't", '"a  b"', "", '" OR *'):
        assert fts_query(text) == " ".join(
            [
                *(f'"{p.strip()}"' for p in PHRASE_RE.findall(text) if WORD_RE.search(p)),
                *(f'"{w}"' for w in WORD_RE.findall(PHRASE_RE.sub(" ", text))),
            ]
        )


@pytest.mark.parametrize(
    "query",
    ['"zeta\tstands"', '"zeta\nstands"', '"zeta\r\nstands"', "alpha\tomega", '"omega\n filler"'],
)
def test_queries_the_base_accepted_return_the_same_hits(hardened, query):
    """Whitespace controls were valid before: replacing them with a space changes no result."""
    expected = legacy_search(hardened, query)
    got = hardened.search(query, plan="and")
    assert {k: got[k] for k in expected} == expected


# --- a tiny budget still produces a page ---------------------------------------------------------


@pytest.mark.parametrize("seconds", [5e-324, 1e-300, 1e-9])
def test_a_tiny_budget_still_returns_a_page_from_the_plan_that_matches(
    hardened, monkeypatch, seconds
):
    """The budget is spent before anything runs; the quarter-second floor lets the probes and the
    page finish, so the answer is not an empty "0+" with nowhere to go."""
    monkeypatch.setattr(library_store, "SEARCH_PROGRESS_INSTRUCTIONS", 1)
    result = hardened.search("omega alpha", limit=5, max_seconds=seconds)
    assert result["partial"] is True and result["ranked"] is False
    assert result["plan"] == "and" and len(result["hits"]) == 5
    assert result["total_exact"] is False and result["next_offset"] == 5
    assert result["total"] == 5 and result["total_label"] == "5+"


def test_the_allowance_floor_and_cap_are_documented_constants():
    assert library_store.UNRANKED_GRACE_SECONDS == 2.0
    assert library_store.MIN_UNRANKED_GRACE_SECONDS == 0.25
