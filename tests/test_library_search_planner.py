"""The library search planner, its time budget and its capped count, on synthetic libraries only.

The planner runs an unquoted multi-word query as an adjacent phrase, then as the AND of its words,
then as the OR of its words, and stops at the first plan that matches anything. The pre-planner
search (which AND-ed the words, exactly like ``plan="and"``) is kept below as a parity oracle.
"""

import math
import sqlite3

import pytest

from conversation_corpus_engine import library_store
from conversation_corpus_engine.archive_database import PHRASE_RE, WORD_RE, fts_query, fts_terms
from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.library_store import (
    BROAD_QUERY_HINT,
    DEFAULT_COUNT_CAP,
    MAX_SEARCH_SECONDS,
    SEARCH_PLANS,
    Library,
    plan_match,
    validate_max_seconds,
)

ADDITIVE_KEYS = {"plan", "partial", "ranked", "elapsed_s", "total_exact", "total_label"}


def legacy_fts_query(text: str) -> str:
    """The pre-planner ``fts_query``, verbatim."""
    phrases = [f'"{p.strip()}"' for p in PHRASE_RE.findall(text) if WORD_RE.search(p)]
    remainder = PHRASE_RE.sub(" ", text)
    words = [f'"{w}"' for w in WORD_RE.findall(remainder)]
    return " ".join([*phrases, *words])


def legacy_search(
    library: Library,
    query: str,
    *,
    limit: int = 20,
    offset: int = 0,
    wing: str | None = None,
    lane: str | None = None,
    providers: list[str] | None = None,
    kinds: list[str] | None = None,
) -> dict:
    """``Library.search`` as it was before the planner, verbatim: the AND of the query terms."""
    match = legacy_fts_query(query)
    empty = {
        "query": query,
        "total": 0,
        "offset": offset,
        "limit": limit,
        "next_offset": None,
        "hits": [],
    }
    if not match:
        return empty
    clauses, params = ["record_fts MATCH ?"], [match]
    for column, value in (("wing", wing), ("lane", lane)):
        if value:
            clauses.append(f"r.{column}=?")
            params.append(value)
    for column, values in (("provider", providers), ("kind", kinds)):
        if values:
            clauses.append(f"r.{column} IN ({','.join('?' for _ in values)})")
            params.extend(values)
    source = "FROM record_fts JOIN records r ON r.uid=record_fts.uid WHERE " + " AND ".join(clauses)
    total = library.connection.execute("SELECT COUNT(*) " + source, params).fetchone()[0]
    results = library.connection.execute(
        "SELECT r.uid,r.title,r.provider,r.kind,r.wing,r.lane,r.current_version AS version,"
        "r.body_status,r.message_count,r.source_ref,snippet(record_fts,-1,'[',']','...',28) AS excerpt "
        + source
        + " ORDER BY bm25(record_fts,0,5,1),r.uid LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )
    hits = []
    for row in results:
        hit = dict(row)
        hit["title"] = hit["title"][:512]
        hit["source_ref"] = hit["source_ref"][:2048]
        for key in ("kind", "lane"):
            hit[key] = hit[key][:128]
        hit["excerpt"] = hit["excerpt"][:360]
        hit["path"] = f"vault/records/{hit['uid']}.md"
        hits.append(hit)
    return {
        **empty,
        "total": total,
        "hits": hits,
        "next_offset": offset + len(hits) if offset + len(hits) < total else None,
    }


def without_additive(result: dict) -> dict:
    return {key: value for key, value in result.items() if key not in ADDITIVE_KEYS}


def conv(
    identity: str, text: str, *, wing: str = "ai", lane: str = "conversations"
) -> Conversation:
    return Conversation(
        provider="chatgpt",
        source_id=identity,
        title=f"Note {identity}",
        messages=[Message(role="user", text=text)],
        extra={"wing": wing, "lane": lane, "body_status": "full"},
    )


def uids(result: dict) -> list[str]:
    return [hit["uid"] for hit in result["hits"]]


@pytest.fixture
def small(tmp_path):
    """Four records: one holds the three words adjacent, one apart, one two, one none."""
    path = tmp_path / "small.sqlite"
    with Library.create(path) as library:
        library.put(
            conv(
                "together",
                "alpha beta gamma together in one phrase",
                wing="authored",
                lane="research",
            )
        )
        library.put(conv("apart", "alpha then beta then gamma apart"))
        library.put(conv("pair", "alpha only plus delta"))
        library.put(conv("other", "epsilon nothing relevant"))
    with Library(path) as library:
        yield library


def small_uid(identity: str) -> str:
    return conv(identity, "x").uid


# --- plan selection and fallback ---------------------------------------------------------------


def test_phrase_plan_answers_first_and_the_search_stops_there(small):
    result = small.search("alpha beta gamma")
    assert result["plan"] == "phrase"
    assert uids(result) == [small_uid("together")]
    # The AND plan would also match the record where the words are apart: the stop rule excludes it.
    assert result["total"] == 1 and library_total(small, "alpha beta gamma", "and") == 2


def library_total(library: Library, query: str, plan: str) -> int:
    return library.search(query, plan=plan)["total"]


def test_and_plan_answers_when_the_phrase_misses(small):
    result = small.search("alpha gamma beta")
    assert result["plan"] == "and" and result["total"] == 2
    assert set(uids(result)) == {small_uid("together"), small_uid("apart")}


def test_or_plan_answers_only_when_and_finds_nothing(small):
    result = small.search("alpha epsilon")
    assert result["plan"] == "or" and result["total"] == 4
    assert set(uids(result)) == {small_uid(name) for name in ("together", "apart", "pair", "other")}


def test_a_query_that_matches_nothing_reports_the_last_plan_tried(small):
    result = small.search("zzzz yyyy")
    assert result["plan"] == "or" and result["total"] == 0 and result["hits"] == []
    assert result["next_offset"] is None
    assert result["total_exact"] is True and result["total_label"] == "0"
    assert result["partial"] is False and result["ranked"] is True


def test_the_stop_rule_looks_at_the_filtered_match_set_not_at_the_page(small):
    # Offset past the only phrase hit: the page is empty but the phrase plan still answered.
    beyond = small.search("alpha beta gamma", offset=5)
    assert beyond["plan"] == "phrase" and beyond["total"] == 1 and beyond["hits"] == []
    # A filter removes the phrase record, so the planner moves on to AND within the filter.
    filtered = small.search("alpha beta gamma", wing="ai")
    assert filtered["plan"] == "and" and uids(filtered) == [small_uid("apart")]
    # The same filter keeps a phrase hit when it belongs to the filtered set.
    kept = small.search("alpha beta gamma", wing="authored")
    assert kept["plan"] == "phrase" and uids(kept) == [small_uid("together")]


def test_pages_of_one_query_stay_on_one_plan(small):
    first = small.search("alpha gamma beta", limit=1)
    second = small.search("alpha gamma beta", limit=1, offset=first["next_offset"])
    assert first["plan"] == second["plan"] == "and" and first["total"] == second["total"] == 2
    assert first["next_offset"] == 1 and second["next_offset"] is None
    assert set(uids(first) + uids(second)) == {small_uid("together"), small_uid("apart")}


def test_forced_plans_run_only_that_plan(small):
    assert small.search("alpha gamma beta", plan="phrase")["total"] == 0
    assert small.search("alpha gamma beta", plan="phrase")["plan"] == "phrase"
    forced_and = small.search("alpha beta gamma", plan="and")
    assert forced_and["plan"] == "and" and forced_and["total"] == 2
    forced_or = small.search("alpha beta gamma", plan="or")
    assert forced_or["plan"] == "or" and forced_or["total"] == 3
    assert small.search("alpha beta gamma", plan="phrase")["total"] == 1


def test_plan_or_is_a_true_or_where_the_pre_planner_search_was_an_and(small):
    assert legacy_search(small, "alpha epsilon")["total"] == 0
    assert small.search("alpha epsilon", plan="or")["total"] == 4
    assert small.search("alpha epsilon", plan="and")["total"] == 0


def test_plan_match_builds_each_expression():
    assert plan_match("alpha beta gamma", "phrase") == '"alpha beta gamma"'
    assert plan_match("alpha beta gamma", "and") == fts_query("alpha beta gamma")
    assert plan_match("alpha beta gamma", "or") == '"alpha" OR "beta" OR "gamma"'
    assert plan_match('"alpha beta" gamma', "or") == '"alpha beta" OR "gamma"'
    assert plan_match('"alpha beta" gamma', "phrase") == '"alpha beta gamma"'
    assert plan_match("", "or") == "" and plan_match("   ", "phrase") == ""
    with pytest.raises(ValueError, match="plan"):
        plan_match("alpha", "auto")


# --- unplanned queries and parity with the pre-planner search ----------------------------------


@pytest.mark.parametrize(
    "query", ["alpha", '"alpha beta gamma"', '"alpha beta" gamma', '"zzz yyy"']
)
def test_single_term_and_quoted_queries_run_as_written_and_report_and(small, query):
    result = small.search(query)
    assert result["plan"] == "and"
    assert without_additive(result) == legacy_search(small, query)


QUERIES = [
    "alpha",
    "alpha beta gamma",
    "alpha gamma beta",
    "alpha delta",
    "alpha epsilon",
    '"alpha beta"',
    '"alpha beta" gamma',
    "beta gamma",
    "zzzz yyyy",
    "",
    "   ",
    '" OR *',
]
FILTERS = [
    {},
    {"wing": "ai"},
    {"wing": "authored", "lane": "research"},
    {"providers": ["claude"]},
    {"providers": ["chatgpt"], "kinds": ["conversation"]},
]


@pytest.mark.parametrize("filters", FILTERS)
@pytest.mark.parametrize(("limit", "offset"), [(20, 0), (1, 0), (1, 1), (2, 1), (5, 4)])
@pytest.mark.parametrize("query", QUERIES)
def test_plan_and_is_byte_identical_to_the_pre_planner_search(small, query, limit, offset, filters):
    new = small.search(query, limit=limit, offset=offset, plan="and", **filters)
    assert new["plan"] == "and"
    assert without_additive(new) == legacy_search(
        small, query, limit=limit, offset=offset, **filters
    )


def test_auto_on_unplanned_queries_is_byte_identical_to_the_pre_planner_search(small):
    for query in ("alpha", '"alpha beta gamma"', '"alpha beta" gamma', "", '" OR *'):
        for limit, offset in ((20, 0), (1, 1)):
            new = small.search(query, limit=limit, offset=offset)
            assert without_additive(new) == legacy_search(small, query, limit=limit, offset=offset)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "alpha",
        "alpha beta  gamma",
        '"alpha beta"',
        '"alpha beta" gamma "delta"',
        '"" alpha',
        '"---" alpha',
        'it\'s a state-of-the-art "x y" thing',
        '" OR *',
        'unterminated "quote words',
    ],
)
def test_fts_query_is_unchanged_by_exposing_its_terms(text):
    assert fts_query(text) == legacy_fts_query(text)
    phrases, words = fts_terms(text)
    rebuilt = " ".join([*(f'"{p}"' for p in phrases), *(f'"{w}"' for w in words)])
    assert rebuilt == legacy_fts_query(text)


# --- validation --------------------------------------------------------------------------------


@pytest.mark.parametrize("plan", ["fast", "", "AND", None, 1, ["and"]])
def test_unknown_plans_are_rejected(small, plan):
    with pytest.raises(ValueError, match="plan must be one of"):
        small.search("alpha beta", plan=plan)


@pytest.mark.parametrize(
    "seconds",
    [0, -1, 0.0, MAX_SEARCH_SECONDS + 1, 601, math.nan, math.inf, -math.inf, True, "5", [1]],
)
def test_max_seconds_must_be_a_positive_number_of_at_most_600(small, seconds):
    with pytest.raises(ValueError, match="max_seconds"):
        small.search("alpha beta", max_seconds=seconds)
    with pytest.raises(ValueError, match="max_seconds"):
        validate_max_seconds(seconds)


def test_max_seconds_accepts_integers_floats_and_none():
    assert validate_max_seconds(None) is None
    assert validate_max_seconds(15) == 15.0 and validate_max_seconds(0.25) == 0.25
    assert validate_max_seconds(600) == 600.0
    assert SEARCH_PLANS == ("auto", "phrase", "and", "or")


@pytest.mark.parametrize("cap", [0, -1, True, "10", 1.5, library_store.MAX_COUNT_CAP + 1])
def test_count_cap_must_be_a_positive_integer_or_none(small, cap):
    with pytest.raises(ValueError, match="count_cap"):
        small.search("alpha", count_cap=cap)


# --- the capped count --------------------------------------------------------------------------


@pytest.fixture
def crowd(tmp_path):
    """Thirty records that all match ``crowd``; ten of them also hold ``rare``."""
    path = tmp_path / "crowd.sqlite"
    with Library.create(path) as library:
        for number in range(30):
            extra = " rare" if number % 3 == 0 else ""
            library.put(conv(f"c{number:02d}", f"crowd record number {number}{extra}"))
    with Library(path) as library:
        yield library


def test_the_default_cap_is_one_thousand_and_small_totals_stay_exact(crowd):
    assert DEFAULT_COUNT_CAP == 1000
    result = crowd.search("crowd")
    assert result["total"] == 30 and result["total_exact"] is True
    assert result["total_label"] == "30"


def test_a_total_above_the_cap_is_reported_as_the_cap_with_a_plus_label(crowd):
    result = crowd.search("crowd", count_cap=10, limit=4)
    assert result["total"] == 10 and result["total_exact"] is False
    assert result["total_label"] == "10+"
    assert len(result["hits"]) == 4 and result["next_offset"] == 4


def test_a_total_equal_to_the_cap_is_still_exact(crowd):
    result = crowd.search("crowd", count_cap=30)
    assert result["total"] == 30 and result["total_exact"] is True
    assert result["total_label"] == "30"
    one_above = crowd.search("crowd", count_cap=29)
    assert one_above["total"] == 29 and one_above["total_exact"] is False
    assert one_above["total_label"] == "29+"


def test_pagination_continues_past_a_capped_total(crowd):
    seen, offset, pages = [], 0, 0
    while offset is not None:
        page = crowd.search("crowd", count_cap=10, limit=7, offset=offset)
        assert page["total"] == 10 and page["total_exact"] is False
        seen.extend(uids(page))
        offset, pages = page["next_offset"], pages + 1
        assert pages <= 6
    assert len(seen) == 30 == len(set(seen)) and pages == 5
    full = crowd.search("crowd", limit=30)
    assert seen == uids(full)


def test_a_full_last_page_with_nothing_after_it_ends_pagination(crowd):
    page = crowd.search("crowd", count_cap=10, limit=10, offset=20)
    assert len(page["hits"]) == 10 and page["next_offset"] is None


def test_count_cap_none_counts_exactly(crowd):
    result = crowd.search("crowd", count_cap=None)
    assert result["total"] == 30 and result["total_exact"] is True
    capped = crowd.search("crowd", count_cap=5)
    assert uids(capped) == uids(result)[: len(capped["hits"])]


def test_the_capped_count_does_not_change_which_hits_are_returned(crowd):
    for plan in ("and", "or"):
        exact = crowd.search("crowd rare", plan=plan, count_cap=None, limit=10)
        capped = crowd.search("crowd rare", plan=plan, count_cap=3, limit=10)
        assert uids(exact) == uids(capped)


def test_an_or_plan_over_the_cap_carries_the_quote_a_phrase_hint(crowd):
    over = crowd.search("crowd rare", plan="or", count_cap=10)
    assert over["total_exact"] is False and over["hint"] == BROAD_QUERY_HINT
    assert BROAD_QUERY_HINT == "quote a phrase or add terms for a faster, more precise search"
    assert "hint" not in crowd.search("crowd rare", plan="or")
    assert "hint" not in crowd.search("crowd rare", plan="and", count_cap=3)
    assert "hint" not in crowd.search("crowd", count_cap=3)


# --- the time budget ---------------------------------------------------------------------------


class FakeClock:
    """A settable clock: reads return ``now``, which a test moves to simulate elapsed time."""

    def __init__(self) -> None:
        self.now = 0.0
        self.reads = 0

    def __call__(self) -> float:
        self.reads += 1
        return self.now


def jump_when_ranking_starts(library: Library, clock: FakeClock, to: float = 100.0) -> None:
    """Move the clock the moment the ranking statement (the only one that sorts by bm25) begins."""

    def trace(statement: str) -> None:
        if "bm25" in statement:
            clock.now = to

    library.connection.set_trace_callback(trace)


@pytest.fixture
def many(tmp_path, monkeypatch):
    """Three hundred records that all hold ``alpha`` and ``beta``, never adjacent."""
    path = tmp_path / "many.sqlite"
    with Library.create(path) as library:
        for number in range(300):
            library.put(conv(f"m{number:03d}", f"alpha filler {number} beta more filler {number}"))
    # A tiny instruction interval makes the progress handler fire inside even this small ranking.
    monkeypatch.setattr(library_store, "SEARCH_PROGRESS_INSTRUCTIONS", 10)
    with Library(path) as library:
        yield library


def test_an_expired_ranking_returns_a_partial_unranked_page_without_raising(many, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(library_store, "_clock", clock)
    jump_when_ranking_starts(many, clock)
    result = many.search("alpha beta", limit=5, max_seconds=1.0)
    assert result["partial"] is True and result["ranked"] is False
    assert result["plan"] == "and" and isinstance(result["elapsed_s"], float)
    # Index order: the first matching records in the order they were added.
    assert uids(result) == [conv(f"m{n:03d}", "x").uid for n in range(5)]
    assert result["total"] == 300 and result["total_exact"] is True
    assert result["next_offset"] == 5
    assert "index order" in result["notice"] and "not by relevance" in result["notice"]
    assert result["hint"] == BROAD_QUERY_HINT


def test_an_unranked_page_pages_on_in_index_order(many, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(library_store, "_clock", clock)
    jump_when_ranking_starts(many, clock)
    second = many.search("alpha beta", limit=5, offset=5, max_seconds=1.0)
    assert second["partial"] is True
    assert uids(second) == [conv(f"m{n:03d}", "x").uid for n in range(5, 10)]


def test_a_search_that_finishes_in_time_is_complete_and_ranked(many, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(library_store, "_clock", clock)
    budgeted = many.search("alpha beta", limit=5, max_seconds=1.0)
    unbudgeted = many.search("alpha beta", limit=5)
    assert budgeted["partial"] is False and budgeted["ranked"] is True
    assert budgeted["elapsed_s"] == 0.0 and "notice" not in budgeted and "hint" not in budgeted
    assert budgeted["hits"] == unbudgeted["hits"]
    assert unbudgeted["partial"] is False and unbudgeted["ranked"] is True


def test_without_a_budget_a_slow_search_is_never_cut_short(many, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(library_store, "_clock", clock)
    jump_when_ranking_starts(many, clock)
    result = many.search("alpha beta", limit=5)
    assert result["partial"] is False and result["ranked"] is True
    assert result["elapsed_s"] == 100.0 and "notice" not in result


def test_a_budget_already_spent_before_the_first_plan_still_returns_a_page(many, monkeypatch):
    class Spent(FakeClock):
        def __call__(self) -> float:
            self.reads += 1
            return 0.0 if self.reads == 1 else self.now

    clock = Spent()
    clock.now = 100.0
    monkeypatch.setattr(library_store, "_clock", clock)
    result = many.search("alpha beta", limit=3, max_seconds=1.0)
    assert result["partial"] is True and result["ranked"] is False
    assert result["plan"] == "phrase"
    assert result["hits"] == [] and result["total"] == 0 and result["total_exact"] is False
    assert "phrase plan" in result["notice"]


def test_an_expired_budget_in_a_later_plan_names_that_plan(many, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(library_store, "_clock", clock)

    def trace(statement: str) -> None:
        # The AND plan is the first statement that names both words without the phrase quotes.
        if '"alpha" "beta"' in statement:
            clock.now = 100.0

    many.connection.set_trace_callback(trace)
    result = many.search("alpha beta", limit=2, max_seconds=1.0)
    assert result["plan"] == "and" and result["partial"] is True and result["ranked"] is False


def test_the_budget_composes_with_an_enclosing_progress_scope(many):
    class Stop(Exception):
        pass

    calls: list[int] = []

    def outer() -> None:
        calls.append(1)
        if len(calls) >= 3:
            raise Stop

    with pytest.raises(Stop), many.sql_progress(outer, instructions=20):
        many.search("alpha beta", max_seconds=60.0)
    assert len(calls) >= 3
    # The enclosing scope is restored: it still sees later statements of its own.
    seen: list[int] = []
    with many.sql_progress(lambda: seen.append(1), instructions=20):
        many.search("alpha beta", max_seconds=60.0)
        before = len(seen)
        many.connection.execute("SELECT COUNT(*) FROM record_fts").fetchone()
    assert before > 0


def test_an_enclosing_scope_still_counts_steps_when_the_search_has_no_budget(many):
    steps: list[int] = []
    with many.sql_progress(lambda: steps.append(1), instructions=20):
        many.search("alpha beta")
    assert len(steps) > 0


def test_a_database_error_is_not_mistaken_for_the_budget(many):
    """Only the budget's own expiry is turned into a partial page; any other failure surfaces."""

    class Failing:
        def __init__(self, connection):
            self.inner = connection

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def execute(self, sql, *args):
            if "COUNT(*)" in sql:
                raise sqlite3.OperationalError("disk I/O error")
            return self.inner.execute(sql, *args)

    many.connection = Failing(many.connection)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        many.search("alpha beta", max_seconds=30.0)


# --- scale: three very common words across thousands of records ---------------------------------

SCALE_RECORDS = 5000


@pytest.fixture(scope="module")
def scale(tmp_path_factory):
    path = tmp_path_factory.mktemp("scale") / "library.sqlite"
    with Library.create(path) as library:
        # Durability is irrelevant to a throwaway synthetic fixture and ~5,000 commits are slow.
        library.connection.execute("PRAGMA synchronous=OFF")
        library.connection.execute("PRAGMA journal_mode=MEMORY")
        for number in range(SCALE_RECORDS):
            body = " ".join(
                f"alpha stream {number} beta ledger {number} gamma marker {number}."
                for _ in range(4)
            )
            library.put(conv(f"s{number:04d}", body))
        needle = conv("needle", "the alpha beta gamma sequence appears exactly once in this record")
        library.put(needle)
    with Library(path) as library:
        yield library, needle.uid


def counted(library: Library, query: str, **kwargs) -> tuple[int, dict]:
    """Search under an enclosing progress scope that counts SQLite progress-handler steps."""
    steps = [0]

    def tick() -> None:
        steps[0] += 1

    with library.sql_progress(tick, instructions=50):
        result = library.search(query, limit=5, **kwargs)
    return steps[0], result


def test_three_common_words_are_answered_by_the_phrase_plan_with_far_less_work(scale):
    library, needle = scale
    auto_steps, auto = counted(library, "alpha beta gamma")
    assert auto["plan"] == "phrase" and auto["total"] == 1 and uids(auto) == [needle]
    assert auto["partial"] is False and auto["ranked"] is True
    and_steps, forced_and = counted(library, "alpha beta gamma", plan="and")
    or_steps, forced_or = counted(library, "alpha beta gamma", plan="or")
    assert forced_and["total"] == forced_or["total"] == DEFAULT_COUNT_CAP
    assert forced_and["total_exact"] is False and forced_and["total_label"] == "1000+"
    assert auto_steps * 10 < and_steps and auto_steps * 10 < or_steps
    assert forced_or["hint"] == BROAD_QUERY_HINT and "hint" not in forced_and


def test_a_phrase_miss_falls_to_the_and_plan_and_caps_its_count(scale):
    library, _needle = scale
    steps, result = counted(library, "alpha gamma beta")
    assert result["plan"] == "and" and result["ranked"] is True and result["partial"] is False
    assert result["total"] == DEFAULT_COUNT_CAP and result["total_label"] == "1000+"
    assert len(result["hits"]) == 5 and result["next_offset"] == 5
    exact = library.search("alpha gamma beta", limit=1, count_cap=None)
    assert exact["total"] == SCALE_RECORDS + 1 and exact["total_exact"] is True
    assert steps > 0
