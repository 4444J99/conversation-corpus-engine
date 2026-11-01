"""The turn store: one row per message, in two containers by author, as provider-month shards."""

from __future__ import annotations

import json
import random
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from archive_support import conv, topical_corpus, write_sharded_store, write_store

from conversation_corpus_engine import archive_search, archive_turns
from conversation_corpus_engine.archive_author import HUMAN, MACHINE
from conversation_corpus_engine.archive_database import build_database
from conversation_corpus_engine.archive_ingest import ingest, load_manifest
from conversation_corpus_engine.archive_model import Conversation, Message, write_json_atomic
from conversation_corpus_engine.archive_naming import verify_names
from conversation_corpus_engine.archive_serve import start_in_thread
from conversation_corpus_engine.archive_site import build_site
from conversation_corpus_engine.archive_topics import build_analysis
from conversation_corpus_engine.archive_turns import (
    build_turn_store,
    has_turn_store,
    list_turns,
    load_turn_manifest,
    select_shards,
    verify_turn_store,
)
from conversation_corpus_engine.mcp_server import ARCHIVE_TOOL, CceMcpServer


def _get(server, path: str) -> tuple[int, dict[str, str], bytes]:
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


@pytest.fixture
def built(archive_env):
    ingest(export_root=archive_env.export, paths=archive_env.paths, authorization=archive_env.auth)
    build_analysis(archive_env.paths)
    summary = build_turn_store(archive_env.paths, archive_env.auth)
    return archive_env, summary


def _shard_authors(path: Path) -> set[str]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {row[0] for row in connection.execute("SELECT DISTINCT author FROM turns")}
    finally:
        connection.close()


def test_the_summary_counts_turns_by_author_and_they_reconcile_to_the_manifest(built) -> None:
    env, summary = built
    manifest = load_manifest(env.paths)
    assert summary["authors"] == manifest["totals"]["authors"]
    assert summary["turns"] == manifest["totals"]["messages"]
    assert summary["conversations"] == manifest["totals"]["conversations"]
    assert verify_turn_store(env.paths) == []
    assert verify_names(env.paths.root)["ok"]


def test_each_container_holds_only_its_authors_turns(built) -> None:
    env, _ = built
    turns = load_turn_manifest(env.paths)
    for container, author in (("authored", HUMAN), ("machine", MACHINE)):
        shards = turns["containers"][container]["shards"]
        assert shards
        for item in shards:
            assert _shard_authors(env.paths.turns / item["path"]) == {author}
            assert item["path"].startswith(f"container-all-{container}/provider-")
    assert env.paths.container(HUMAN).name == "container-all-authored"
    assert env.paths.container(MACHINE).name == "container-all-machine"


def test_a_provider_with_no_typed_turns_has_no_authored_shard(built) -> None:
    env, _ = built
    turns = load_turn_manifest(env.paths)
    authored = {item["provider"] for item in turns["containers"]["authored"]["shards"]}
    machine = {item["provider"] for item in turns["containers"]["machine"]["shards"]}
    # A notebook's sources and notes are text the person supplied (channel `written`), so
    # NotebookLM has an authored shard; the Perplexity fixture has no typed or written turn.
    assert "notebooklm" in machine and "notebooklm" in authored
    assert "perplexity" in machine
    if "perplexity" not in authored:
        assert not any(env.paths.container(HUMAN).glob("provider-perplexity/*"))


def test_a_shard_mirrors_a_normalized_shard_one_for_one(built) -> None:
    env, _ = built
    turns = load_turn_manifest(env.paths)
    for provider in env.paths.providers_present():
        subjects = {
            p.name.removeprefix(f"shard-{provider}-").removesuffix(".jsonl")
            for p in env.paths.shard_files(provider)
        }
        for container in turns["containers"].values():
            used = {item["subject"] for item in container["shards"] if item["provider"] == provider}
            assert used <= subjects


def test_verify_names_accepts_every_shard_and_directory(built) -> None:
    env, _ = built
    report = verify_names(env.paths.turns)
    assert report["ok"], report["problems"]
    assert report["checked"] > 5


def test_tampering_with_a_shard_is_detected(built) -> None:
    env, _ = built
    turns = load_turn_manifest(env.paths)
    first = turns["containers"]["authored"]["shards"][0]
    path = env.paths.turns / first["path"]
    connection = sqlite3.connect(path)
    connection.execute("DELETE FROM turns WHERE id = (SELECT MIN(id) FROM turns)")
    connection.commit()
    connection.close()
    problems = verify_turn_store(env.paths)
    assert any("turn row" in p or "differs" in p for p in problems), problems
    path.unlink()
    assert any("missing shard file" in p for p in verify_turn_store(env.paths))


def test_a_shard_that_lost_its_conversation_rows_is_detected(built) -> None:
    env, _ = built
    first = load_turn_manifest(env.paths)["containers"]["authored"]["shards"][0]
    path = env.paths.turns / first["path"]
    connection = sqlite3.connect(path)
    connection.execute("DELETE FROM conversations")
    connection.commit()
    connection.close()
    problems = verify_turn_store(env.paths)
    assert any("conversation row" in p for p in problems), problems
    assert any("no conversation row" in p for p in problems), problems
    assert any("checksum differs" in p for p in problems), problems


def test_a_store_built_from_a_different_fingerprint_is_refused(built) -> None:
    env, _ = built
    document = load_turn_manifest(env.paths)
    document["source_fingerprint"] = "0" * 64
    write_json_atomic(env.paths.turns_manifest, document)
    assert any("different source fingerprint" in p for p in verify_turn_store(env.paths))


def test_a_missing_turn_store_says_how_to_build_it(archive_env) -> None:
    ingest(export_root=archive_env.export, paths=archive_env.paths, authorization=archive_env.auth)
    assert not has_turn_store(archive_env.paths)
    assert "run `cce archive database`" in verify_turn_store(archive_env.paths)[0]
    with pytest.raises(FileNotFoundError):
        select_shards(archive_env.paths)


def test_a_rebuild_replaces_the_store_and_leaves_no_temporary_directory(built) -> None:
    env, first = built
    again = build_turn_store(env.paths, env.auth)
    assert again["turns"] == first["turns"] and again["shards"] == first["shards"]
    assert not [p for p in env.paths.root.iterdir() if p.name.startswith(".database-all-turns")]
    assert verify_turn_store(env.paths) == []


def test_search_filters_by_author_provider_month_and_kind(built) -> None:
    env, _ = built
    paths = env.paths
    every = archive_search.search(paths, "kubernetes cluster", limit=20)
    human = archive_search.search(paths, "kubernetes cluster", authors=["human"], limit=20)
    machine = archive_search.search(paths, "kubernetes cluster", authors=["machine"], limit=20)
    assert every and human and {hit["author"] for hit in human} == {HUMAN}
    assert {hit["author"] for hit in machine} <= {MACHINE}
    assert len(every) >= max(len(human), len(machine))
    assert {
        h["provider"] for h in archive_search.search(paths, "sourdough", providers=["claude"])
    } == {"claude"}
    assert archive_search.search(paths, "kubernetes", months=["190001"]) == []
    chats = archive_search.search(paths, "kubernetes cluster", kinds=["chat"], limit=20)
    assert {h["kind"] for h in chats} == {"chat"}
    assert archive_search.search(paths, "") == [] and archive_search.search(paths, '"') == []


def test_the_best_turn_of_each_conversation_is_returned_once(built) -> None:
    env, _ = built
    hits = archive_search.search(env.paths, "kubernetes", limit=50)
    uids = [hit["uid"] for hit in hits]
    assert len(uids) == len(set(uids))
    assert all("[" in hit["excerpt"] for hit in hits)


def test_list_turns_pages_in_reading_order_across_shards(built) -> None:
    env, _ = built
    manifest = load_manifest(env.paths)
    everything = list_turns(env.paths, limit=1000)
    assert len(everything) == manifest["totals"]["messages"]
    page = list_turns(env.paths, limit=5, offset=3)
    assert [r["turn_id"] for r in page] == [r["turn_id"] for r in everything[3:8]]
    human = list_turns(env.paths, author=HUMAN, limit=1000)
    assert len(human) == manifest["totals"]["authors"][HUMAN]
    assert {row["author"] for row in human} == {HUMAN}
    one = list_turns(env.paths, uid="chatgpt-aaaa1111", limit=50)
    assert [r["ordinal"] for r in one] == sorted(r["ordinal"] for r in one)
    assert {r["uid"] for r in one} == {"chatgpt-aaaa1111"}


def _write_minimal_manifest(env) -> None:
    env.paths.manifest.write_text(
        json.dumps({"source_fingerprint": "e" * 64, "totals": {}, "providers": {}}),
        encoding="utf-8",
    )


def test_an_unfiltered_listing_keeps_each_conversation_in_turn_order(archive_env) -> None:
    first = conv(
        "chatgpt",
        "ord1",
        "Ordered one",
        [("user", "q0"), ("assistant", "a1"), ("user", "q2"), ("assistant", "a3")],
    )
    second = conv("chatgpt", "ord2", "Ordered two", [("user", "r0"), ("assistant", "b1")])
    write_sharded_store(archive_env.paths, [first, second])
    _write_minimal_manifest(archive_env)
    build_turn_store(archive_env.paths, archive_env.auth)

    one = list_turns(archive_env.paths, uid=first.uid, limit=50)
    assert [r["ordinal"] for r in one] == [0, 1, 2, 3]
    assert [r["author"] for r in one] == [HUMAN, MACHINE, HUMAN, MACHINE]
    expected = [(first.uid, n) for n in range(4)] + [(second.uid, 0), (second.uid, 1)]
    everything = list_turns(archive_env.paths, limit=50)
    assert [(r["uid"], r["ordinal"]) for r in everything] == expected
    page = list_turns(archive_env.paths, limit=2, offset=3)
    assert [(r["uid"], r["ordinal"]) for r in page] == expected[3:5]
    assert "cseq" not in everything[0]
    only_human = list_turns(archive_env.paths, author=HUMAN, limit=50)
    assert [(r["uid"], r["ordinal"]) for r in only_human] == [
        (first.uid, 0),
        (first.uid, 2),
        (second.uid, 0),
    ]


def test_a_conversation_with_no_messages_is_still_found_by_title(archive_env) -> None:
    quiet = conv("claude", "e1", "Zeppelin inventory", [("user", "x")])
    quiet.messages.clear()
    alone = conv("claude", "e2", "Quokka register", [("user", "y")], created="2024-03-05T00:00:00Z")
    alone.messages.clear()
    typed = conv("claude", "t1", "Plain talk", [("user", "hello world"), ("assistant", "hi")])
    write_sharded_store(archive_env.paths, [quiet, alone, typed])
    _write_minimal_manifest(archive_env)
    summary = build_turn_store(archive_env.paths, archive_env.auth)

    assert summary["turns"] == 2 and summary["conversations"] == 3
    assert [h["uid"] for h in archive_turns.search_turn_titles(archive_env.paths, "zeppelin")] == [
        quiet.uid
    ]
    assert [h["uid"] for h in archive_turns.search_turn_titles(archive_env.paths, "quokka")] == [
        alone.uid
    ]
    assert len(list_turns(archive_env.paths, limit=50)) == 2
    machine = load_turn_manifest(archive_env.paths)["containers"]["machine"]["shards"]
    assert any(item["subject"] == "202403" and item["turns"] == 0 for item in machine)


def test_reasoning_and_tool_turns_are_hidden_unless_asked_for(archive_env) -> None:
    conversation = conv("chatgpt", "r", "R", [("user", "hello")])
    conversation.messages.append(
        Message(
            role="assistant",
            text="ponder zebra thoughts",
            ts="2025-01-01T00:00:00Z",
            meta={"kind": "reasoning"},
            channel="thinking",
        )
    )
    conversation.messages.append(
        Message(
            role="assistant", text="plain zebra answer", ts="2025-01-01T00:00:01Z", channel="reply"
        )
    )
    write_sharded_store(archive_env.paths, [conversation])
    archive_env.paths.manifest.write_text(
        json.dumps({"source_fingerprint": "e" * 64, "totals": {}, "providers": {}}),
        encoding="utf-8",
    )
    archive_turns.build_turn_store(archive_env.paths, archive_env.auth)
    hits = archive_turns.search_turns(archive_env.paths, "zebra")
    assert [h["role"] for h in hits] == ["assistant"] and "plain" in hits[0]["excerpt"]
    assert hits[0]["author"] == MACHINE and hits[0]["channel"] == "reply"
    deep = archive_turns.search_turns(archive_env.paths, "ponder", include_reasoning=True)
    assert deep and deep[0]["channel"] == "thinking"
    assert archive_turns.search_turns(archive_env.paths, "ponder") == []


# --- a first-generation store -----------------------------------------------------------------


def test_a_store_without_a_turn_store_answers_from_its_single_database(archive_env) -> None:
    write_store(archive_env.paths, topical_corpus())
    archive_env.paths.manifest.write_text(
        json.dumps(
            {
                "source_fingerprint": "a" * 64,
                "totals": {"conversations": 12, "messages": 24, "words": 100},
                "providers": {},
            }
        ),
        encoding="utf-8",
    )
    build_analysis(archive_env.paths)
    build_database(archive_env.paths, archive_env.auth)
    assert not has_turn_store(archive_env.paths)
    hits = archive_search.search(archive_env.paths, "kubernetes cluster", limit=5)
    assert hits and "author" not in hits[0]
    with pytest.raises(archive_search.FilterNotSupported, match="rebuild"):
        archive_search.search(archive_env.paths, "kubernetes", authors=["human"])
    with pytest.raises(archive_search.FilterNotSupported):
        archive_search.search_titles(archive_env.paths, "kubernetes", months=["202501"])
    assert archive_search.search_titles(archive_env.paths, "kubernetes note", limit=3)


def test_nothing_built_is_a_missing_file_not_an_empty_answer(archive_env) -> None:
    with pytest.raises(FileNotFoundError):
        archive_search.search(archive_env.paths, "anything")


# --- the server and the MCP tool ---------------------------------------------------------------


@pytest.fixture
def server(built):
    env, _ = built
    build_site(env.paths, env.auth)
    srv, thread = start_in_thread(env.paths)
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


def test_the_server_filters_by_author_and_month(server) -> None:
    status, _, body = _get(server, "/api/search?q=kubernetes+cluster&author=human&limit=10")
    results = json.loads(body)["results"]
    assert status == 200 and results and {hit["author"] for hit in results} == {HUMAN}
    status, _, body = _get(server, "/api/search?q=kubernetes&month=190001")
    assert status == 200 and json.loads(body)["results"] == []
    status, _, body = _get(server, "/api/search?q=kubernetes&author=machine&provider=claude")
    assert status == 200 and {h["provider"] for h in json.loads(body)["results"]} <= {"claude"}
    assert _get(server, "/api/titles?q=rollout&month=202311")[0] == 200


def test_the_index_data_carries_each_conversations_page_path(built) -> None:
    env, _ = built
    build_site(env.paths, env.auth)
    payload = (env.paths.site / "index-all-conversations.js").read_text(encoding="utf-8")
    data = json.loads(payload.removeprefix("window.CCE = ").rstrip(";\n"))
    assert all(
        row[8].startswith("conversations/") and row[8].endswith(".html") for row in data["index"]
    )
    assert all((env.paths.site / row[8]).is_file() for row in data["index"])


def test_the_mcp_tool_filters_by_author_and_reports_an_old_store(built, tmp_path) -> None:
    env, _ = built
    server = CceMcpServer(default_project_root=tmp_path, archive_root=env.paths.root)

    def call(**arguments):
        return server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": ARCHIVE_TOOL, "arguments": arguments},
            }
        )["result"]

    result = call(query="kubernetes cluster", author=["human"], limit=5)
    assert result["isError"] is False
    assert {hit["author"] for hit in result["structuredContent"]["results"]} == {HUMAN}
    bad = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": ARCHIVE_TOOL, "arguments": {"query": "x", "author": "human"}},
        }
    )
    assert "author must be an array of strings" in json.dumps(bad)


# --- rank-first search is the joined search, answer for answer ----------------------------------

FILLER = ("alpha", "beta", "gamma", "delta", "marble", "lantern", "orchid", "falcon")
RANKED_PROVIDERS = ("chatgpt", "claude", "gemini")
RANKED_MONTHS = ("2025-01", "2025-02")


def _sentence(rng: random.Random, size: int, *terms: str) -> str:
    words = [rng.choice(FILLER) for _ in range(size)]
    for term in terms:
        words.insert(rng.randrange(len(words) + 1), term)
    return " ".join(words)


def _hidden_turn(text: str, kind: str, ts: str) -> Message:
    """A reasoning or tool turn: machine text that a search hides unless it is asked for."""
    channel, role = ("thinking", "assistant") if kind == "reasoning" else ("tool", "tool")
    return Message(role=role, text=text, ts=ts, meta={"kind": kind}, channel=channel)


def _ranked_corpus() -> list[Conversation]:
    """Invented conversations that give a search every awkward shape to get right.

    Ordinary provider-months repeat a few terms at varying frequency and length, so one
    conversation matches many times at many ranks and carries reasoning and tool turns among
    typed and replied ones, in two conversation kinds. One shard holds far more reasoning matches
    than any small cut can see past, with the plain matches ranking behind them. One shard holds
    sixty identical typed turns, so their ranks tie exactly across the per-shard cut.
    """
    rng = random.Random(97)
    items: list[Conversation] = []
    for provider in RANKED_PROVIDERS:
        for month in RANKED_MONTHS:
            for number in range(14):
                texts = []
                for turn in range(rng.randint(6, 14)):
                    terms = rng.choice([(), ("kestrel",), ("kestrel", "orchid"), ("kestrel",) * 2])
                    texts.append(
                        (
                            "user" if turn % 2 == 0 else "assistant",
                            _sentence(rng, rng.randint(2, 40), *terms),
                        )
                    )
                created = f"{month}-{number + 1:02d}T09:00:00Z"
                conversation = conv(
                    provider,
                    f"{provider}-{month}-{number}",
                    f"Talk {number}",
                    texts,
                    created=created,
                    kind="note" if number % 4 == 0 else "chat",
                )
                for kind in ("reasoning", "tool", "reasoning"):
                    if rng.random() < 0.7:
                        conversation.messages.append(
                            _hidden_turn(
                                _sentence(rng, rng.randint(2, 12), "kestrel"), kind, created
                            )
                        )
                items.append(conversation)
    stamp = "2025-04-10T09:00:00Z"
    for number in range(10):
        heavy = conv(
            "grok",
            f"heavy-{number}",
            f"Heavy {number}",
            [("user", "begin"), ("assistant", _sentence(rng, 30, "zebra"))],
            created=stamp,
        )
        heavy.messages.extend(
            _hidden_turn("zebra zebra zebra", "reasoning", stamp) for _ in range(12)
        )
        items.append(heavy)
    for number in range(6):
        items.append(
            conv(
                "grok",
                f"quiet-{number}",
                f"Quiet {number}",
                [("user", "begin"), ("assistant", _sentence(rng, 25, "zebra"))],
                created=stamp,
                kind="note",
            )
        )
    for number in range(60):
        items.append(
            conv(
                "perplexity",
                f"tie-{number}",
                f"Tie {number}",
                [("user", "quartz marble"), ("assistant", "noted")],
                created="2025-05-03T09:00:00Z",
            )
        )
    return items


@pytest.fixture
def ranked_store(archive_env):
    write_sharded_store(archive_env.paths, _ranked_corpus())
    _write_minimal_manifest(archive_env)
    build_turn_store(archive_env.paths, archive_env.auth)
    return archive_env.paths


def _scalar(paths, author: str, provider: str, month: str, sql: str) -> int:
    """One number from the one shard that `author` has for `provider` in `month`."""
    ((_, item),) = select_shards(paths, authors=[author], providers=[provider], months=[month])
    connection = sqlite3.connect(f"file:{paths.turns / item['path']}?mode=ro", uri=True)
    try:
        return int(connection.execute(sql).fetchone()[0])
    finally:
        connection.close()


def test_the_differential_fixture_has_the_shapes_it_claims(ranked_store) -> None:
    per_shard = 40  # max(limit * 8, 40) for every limit up to 5
    count = "SELECT COUNT(*) FROM turn_fts WHERE turn_fts MATCH '\"{}\"'"
    assert _scalar(ranked_store, MACHINE, "grok", "202504", count.format("zebra")) > 2 * per_shard
    tied = _scalar(ranked_store, HUMAN, "perplexity", "202505", count.format("quartz"))
    assert tied == 60 > per_shard
    hits = archive_turns.search_turns_reference(ranked_store, "kestrel", limit=50)
    assert len(hits) >= 20 and len({hit["provider"] for hit in hits}) == 3
    most = (
        "SELECT MAX(n) FROM (SELECT COUNT(*) AS n FROM turns t JOIN turn_fts "
        "ON turn_fts.rowid = t.id WHERE turn_fts MATCH '\"kestrel\"' GROUP BY t.uid)"
    )
    assert _scalar(ranked_store, MACHINE, "claude", "202501", most) >= 3  # many hits, one winner


DIFFERENTIAL_QUERIES = (
    "kestrel",
    "kestrel orchid",
    '"kestrel orchid"',
    "marble lantern",
    "quartz",
    "zebra",
    "unseenterm",
)
DIFFERENTIAL_FILTERS: tuple[dict, ...] = (
    {},
    {"authors": ["human"]},
    {"authors": ["machine"]},
    {"providers": ["claude"]},
    {"providers": ["chatgpt", "gemini"], "authors": ["machine"]},
    {"kinds": ["note"]},
    {"kinds": ["chat", "note"]},
    {"months": ["202502"]},
    {"months": ["202501", "202504"]},
    {"providers": ["claude"], "authors": ["human"], "months": ["202502"], "kinds": ["chat"]},
)


@pytest.mark.parametrize("limit", [0, 1, 5, 20, 50])
def test_rank_first_search_returns_exactly_what_the_joined_search_returns(
    ranked_store, limit: int
) -> None:
    compared = answered = 0
    for query in DIFFERENTIAL_QUERIES:
        for filters in DIFFERENTIAL_FILTERS:
            for include_reasoning in (False, True):
                want = archive_turns.search_turns_reference(
                    ranked_store, query, limit=limit, include_reasoning=include_reasoning, **filters
                )
                got = archive_turns.search_turns(
                    ranked_store, query, limit=limit, include_reasoning=include_reasoning, **filters
                )
                assert got == want, (query, filters, include_reasoning, limit)
                compared += 1
                answered += bool(want)
    assert compared == len(DIFFERENTIAL_QUERIES) * len(DIFFERENTIAL_FILTERS) * 2
    assert answered >= compared // 3  # most of the comparisons are of real answers


def _watch_fallback(monkeypatch) -> list[int]:
    calls: list[int] = []
    real = archive_turns._joined_candidates

    def watched(connection, match, filters, per_shard):
        calls.append(per_shard)
        return real(connection, match, filters, per_shard)

    monkeypatch.setattr(archive_turns, "_joined_candidates", watched)
    return calls


@pytest.mark.parametrize(
    "arguments",
    [
        {"providers": ["grok"]},
        {"providers": ["grok"], "authors": ["machine"]},
        {"providers": ["grok"], "kinds": ["note"], "include_reasoning": True},
    ],
)
def test_an_insufficient_oversample_falls_back_to_the_exact_query(
    ranked_store, monkeypatch, arguments: dict
) -> None:
    calls = _watch_fallback(monkeypatch)
    for limit in (1, 5):
        want = archive_turns.search_turns_reference(ranked_store, "zebra", limit=limit, **arguments)
        calls.clear()
        got = archive_turns.search_turns(ranked_store, "zebra", limit=limit, **arguments)
        assert want and got == want
        assert calls, "the reasoning rows should have filled the oversample"


def test_the_fallback_is_for_a_cut_that_is_too_narrow_to_be_sure(ranked_store, monkeypatch) -> None:
    calls = _watch_fallback(monkeypatch)
    for limit in (1, 5):  # nothing to discard: the first rows ranked are the answer, however few
        archive_turns.search_turns(ranked_store, "kestrel", limit=limit, include_reasoning=True)
    for limit in (20, 50):  # a wide cut sees every match of these small shards, filtered or not
        archive_turns.search_turns(ranked_store, "kestrel", providers=["chatgpt"], limit=limit)
        archive_turns.search_turns(ranked_store, "kestrel", kinds=["chat"], limit=limit)
    assert calls == []


def _trace_sql(monkeypatch) -> list[str]:
    """Every SQL statement the search sends to any shard."""
    statements: list[str] = []
    real = archive_turns._open

    def traced(paths, item):
        connection = real(paths, item)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(archive_turns, "_open", traced)
    return statements


def test_a_shard_with_no_conversation_of_the_kind_is_never_ranked(
    ranked_store, monkeypatch
) -> None:
    statements = _trace_sql(monkeypatch)
    for query in ("quartz", "quartz marble"):
        want = archive_turns.search_turns_reference(
            ranked_store, query, providers=["perplexity"], kinds=["note"], limit=5
        )
        statements.clear()
        got = archive_turns.search_turns(
            ranked_store, query, providers=["perplexity"], kinds=["note"], limit=5
        )
        assert got == want == []
        assert statements and not [s for s in statements if "turn_fts" in s]


def test_a_shard_whose_conversations_all_have_the_kind_has_no_kind_left_to_filter(
    ranked_store, monkeypatch
) -> None:
    statements = _trace_sql(monkeypatch)
    want = archive_turns.search_turns_reference(
        ranked_store, "quartz", providers=["perplexity"], kinds=["chat"], limit=5
    )
    statements.clear()
    got = archive_turns.search_turns(
        ranked_store, "quartz", providers=["perplexity"], kinds=["chat"], limit=5
    )
    assert got == want and len(got) == 5
    assert [s for s in statements if "t.id IN" in s]  # the turns were looked up
    assert not [s for s in statements if "c.kind IN" in s]  # but no kind condition was left


def test_a_shard_that_mixes_kinds_keeps_the_kind_condition(ranked_store, monkeypatch) -> None:
    statements = _trace_sql(monkeypatch)
    want = archive_turns.search_turns_reference(
        ranked_store, "kestrel", providers=["claude"], kinds=["note"], limit=5
    )
    statements.clear()
    got = archive_turns.search_turns(
        ranked_store, "kestrel", providers=["claude"], kinds=["note"], limit=5
    )
    assert got == want and got
    assert {hit["kind"] for hit in got} == {"note"}
    assert [s for s in statements if "c.kind IN" in s]


class _Tracked(sqlite3.Connection):
    """A connection that remembers having been closed."""

    closed = False

    def close(self) -> None:
        self.closed = True
        super().close()


def _track_connections(monkeypatch) -> list[_Tracked]:
    opened: list[_Tracked] = []

    def tracking_open(paths, item):
        connection = sqlite3.connect(
            f"file:{paths.turns / item['path']}?mode=ro", uri=True, factory=_Tracked
        )
        connection.row_factory = sqlite3.Row
        opened.append(connection)
        return connection

    monkeypatch.setattr(archive_turns, "_open", tracking_open)
    return opened


@pytest.mark.parametrize("held", [0, 2, 32])
def test_holding_shards_open_for_the_excerpts_changes_no_answer(
    ranked_store, monkeypatch, held: int
) -> None:
    monkeypatch.setattr(archive_turns, "HELD_SHARDS", held)
    for limit in (5, 20):
        want = archive_turns.search_turns_reference(
            ranked_store, "kestrel", providers=["chatgpt"], limit=limit
        )
        opened = _track_connections(monkeypatch)
        got = archive_turns.search_turns(
            ranked_store, "kestrel", providers=["chatgpt"], limit=limit
        )
        assert got == want and got
        assert all(connection.closed for connection in opened)
        shards = len(archive_turns.select_shards(ranked_store, providers=["chatgpt"]))
        if held == 0:
            assert len(opened) > shards  # a shard that gave a winner is opened again for it
        if held == 32:
            assert len(opened) == shards  # each shard is opened once


def test_every_shard_is_closed_when_a_search_fails_part_way(ranked_store, monkeypatch) -> None:
    real = archive_turns._shard_candidates
    calls: list[int] = []

    def failing(connection, match, filters, per_shard):
        calls.append(1)
        if len(calls) == 4:
            raise sqlite3.OperationalError("disk I/O error")
        return real(connection, match, filters, per_shard)

    opened = _track_connections(monkeypatch)
    monkeypatch.setattr(archive_turns, "_shard_candidates", failing)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        archive_turns.search_turns(ranked_store, "kestrel", limit=5)
    assert len(opened) == 4 and all(connection.closed for connection in opened)


@pytest.mark.parametrize("limit", [1, 5, 20, 50])
def test_exact_ties_across_the_shard_cut_pick_the_same_turns(ranked_store, limit: int) -> None:
    for query in ("quartz", "quartz marble"):
        want = archive_turns.search_turns_reference(
            ranked_store, query, providers=["perplexity"], limit=limit
        )
        got = archive_turns.search_turns(ranked_store, query, providers=["perplexity"], limit=limit)
        assert len(want) == min(limit, 60)
        assert len({hit["rank"] for hit in want}) == 1  # every hit ties
        assert got == want


def test_a_turn_with_no_conversation_row_is_dropped_by_both_searches(ranked_store) -> None:
    ((_, item),) = select_shards(
        ranked_store, authors=[HUMAN], providers=["perplexity"], months=["202505"]
    )
    path = ranked_store.turns / item["path"]
    connection = sqlite3.connect(path)
    try:
        first = connection.execute("SELECT uid FROM turns ORDER BY id LIMIT 3").fetchall()
        for (uid,) in first:  # orphan the best-placed turns, so the cut has to look further
            connection.execute("DELETE FROM conversations WHERE uid = ?", (uid,))
        connection.commit()
    finally:
        connection.close()
    for limit in (1, 5, 50):
        want = archive_turns.search_turns_reference(
            ranked_store, "quartz", providers=["perplexity"], limit=limit, include_reasoning=True
        )
        got = archive_turns.search_turns(
            ranked_store, "quartz", providers=["perplexity"], limit=limit, include_reasoning=True
        )
        assert got == want and not {hit["uid"] for hit in got} & {uid for (uid,) in first}


def test_a_ranked_turn_that_vanishes_is_an_error_not_a_short_answer(
    ranked_store, monkeypatch
) -> None:
    real = archive_turns._shard_candidates

    def ghost(connection, match, filters, per_shard):
        rows = real(connection, match, filters, per_shard)
        return [(rank, uid, idx, rowid + 10_000_000) for rank, uid, idx, rowid in rows]

    monkeypatch.setattr(archive_turns, "_shard_candidates", ghost)
    with pytest.raises(archive_turns.TurnStoreError, match="vanished"):
        archive_turns.search_turns(ranked_store, "kestrel", providers=["claude"], limit=3)
