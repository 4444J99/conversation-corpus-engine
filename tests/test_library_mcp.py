"""Progressive disclosure at the library MCP boundary, on a synthetic library only."""

import json
import math

import pytest

from conversation_corpus_engine import library_mcp
from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.library_mcp import READ_TOOL, SEARCH_TOOL
from conversation_corpus_engine.library_store import Library
from conversation_corpus_engine.mcp_server import CceMcpServer

RECORDS = 220
MESSAGES = 12
LEGACY_HIT_KEYS = {
    "uid",
    "title",
    "provider",
    "kind",
    "wing",
    "lane",
    "version",
    "body_status",
    "message_count",
    "source_ref",
    "excerpt",
    "path",
}


def marker(index: int) -> str:
    return f"zorblax{index:03d}"


def target_message(index: int) -> int:
    return (index % 9) + 2


def build_record(index: int) -> Conversation:
    messages = []
    for position in range(MESSAGES):
        if position == target_message(index):
            text = (
                f"Intro sentence number {position}. " * 12
                + f"The decisive fact is {marker(index)} and nothing else. "
                + "Closing sentence about lattice notes. " * 20
            )
        elif position == 0:
            text = f"# Overview {index}\nOpening lattice notes for record {index}. " * 3
        elif position == 5:
            text = f"## Details {index}\nMiddle lattice notes for record {index}. " * 3
        else:
            text = f"Filler paragraph {position} about lattice notes {index}. " * 6
        messages.append(Message(role="user" if position % 2 == 0 else "assistant", text=text))
    title = f"Synthetic record {index}" + (" with an unusually long descriptive title " * 6)
    return Conversation(
        provider="chatgpt",
        source_id=f"record-{index}",
        title=title if index % 2 else f"Synthetic record {index}",
        messages=messages,
    )


@pytest.fixture(scope="module")
def library_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("library") / "library.sqlite"
    with Library.create(path) as library:
        for index in range(RECORDS):
            library.put(build_record(index))
        library.put(
            Conversation(
                provider="chatgpt",
                source_id="longlog",
                title="Long log",
                messages=[Message(role="user", text=f"entry {n}") for n in range(250)],
            )
        )
        library.put(
            Conversation(
                provider="chatgpt",
                source_id="branchy",
                title="Branchy",
                messages=[Message(role="user", text="main branch only")],
                extra={
                    "alternate_messages": [
                        {"role": "assistant", "text": "first alternate"},
                        {"role": "assistant", "text": "the alternate carries quasarpulse here"},
                    ]
                },
            )
        )
    return path


@pytest.fixture
def mcp(library_path, tmp_path, monkeypatch):
    monkeypatch.setattr(library_mcp, "open_library", lambda root: (tmp_path, Library(library_path)))

    def call(name, **arguments):
        return library_mcp.call_tool(tmp_path, name, arguments)

    return call


def uid_of(index: int) -> str:
    return build_record(index).uid


def compact_tokens(payload) -> int:
    return math.ceil(len(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()) / 4)


def test_default_search_keeps_legacy_hit_fields_and_states_total(mcp, library_path):
    result = mcp(SEARCH_TOOL, query="lattice", limit=3)
    with Library(library_path) as library:
        legacy = library.search("lattice", limit=3)
    assert result["total"] == legacy["total"] == RECORDS
    assert result["next_offset"] == legacy["next_offset"] == 3
    assert result["total_exact"] is True and result["total_label"] == str(RECORDS)
    assert [hit["uid"] for hit in result["hits"]] == [hit["uid"] for hit in legacy["hits"]]
    for hit, old in zip(result["hits"], legacy["hits"], strict=True):
        assert set(hit) >= LEGACY_HIT_KEYS
        assert {key: hit[key] for key in LEGACY_HIT_KEYS} == old
        assert set(hit["read"]) >= {"uid", "message", "offset", "limit"}


def test_l0_ten_hits_fit_nine_hundred_estimated_tokens(mcp):
    result = mcp(SEARCH_TOOL, query="lattice", depth="L0", limit=10)
    assert len(result["hits"]) == 10 and result["total"] == RECORDS
    assert result["token_estimate"]["basis"] == "bytes/4"
    assert result["token_estimate"]["tokens"] <= 900
    assert compact_tokens(result) <= 900
    for hit in result["hits"]:
        assert set(hit) == {"uid", "title", "score", "read"}
        assert "\n" not in hit["title"] and len(hit["title"]) <= 80
        assert isinstance(hit["score"], float)


def test_budget_truncates_with_next_offset_instead_of_overflowing(mcp):
    first = mcp(SEARCH_TOOL, query="lattice", depth="L0", limit=40, budget_tokens=300)
    assert 0 < len(first["hits"]) < 40
    assert first["truncated"] is True
    assert first["token_estimate"]["tokens"] <= 300 and compact_tokens(first) <= 300
    assert first["next_offset"] == len(first["hits"])
    second = mcp(
        SEARCH_TOOL,
        query="lattice",
        depth="L0",
        limit=40,
        budget_tokens=300,
        offset=first["next_offset"],
    )
    seen = {hit["uid"] for hit in first["hits"]}
    assert not seen & {hit["uid"] for hit in second["hits"]}
    assert second["total"] == first["total"]


def test_budget_smaller_than_one_hit_is_an_error_not_a_stuck_cursor(mcp):
    with pytest.raises(ValueError, match="budget_tokens"):
        mcp(SEARCH_TOOL, query="lattice", budget_tokens=100, limit=5)


@pytest.mark.parametrize(
    ("arguments", "name"),
    [
        ({"depth": "L9"}, "depth"),
        ({"budget_tokens": 0}, "budget_tokens"),
        ({"budget_tokens": "many"}, "budget_tokens"),
        ({"budget_tokens": True}, "budget_tokens"),
        ({"depth": "L0", "budget_tokens": 5000}, "budget_tokens"),
        ({"compact": "yes"}, "compact"),
    ],
)
def test_new_search_arguments_are_validated(mcp, arguments, name):
    with pytest.raises(ValueError, match=name):
        mcp(SEARCH_TOOL, query="lattice", **arguments)


def test_compact_mode_uses_short_keys_and_is_smaller(mcp):
    plain = mcp(SEARCH_TOOL, query="lattice", depth="L0", limit=8)
    short = mcp(SEARCH_TOOL, query="lattice", depth="L0", limit=8, compact=True)
    assert short["h"][0].keys() == {"i", "t", "s", "r"}
    assert set(short["h"][0]["r"]) >= {"m", "o", "l"}
    assert short["n"] == plain["total"] and short["nx"] is True
    assert short["h"][0]["i"] == plain["hits"][0]["uid"]
    assert short["tk"] < plain["token_estimate"]["tokens"]
    assert "hits" not in short and "total" not in short


@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("index", range(0, RECORDS, 11))
def test_passage_handoff_reaches_the_unit_in_two_calls(mcp, index, compact):
    query = f"lattice {marker(index)}" if index % 2 else marker(index)
    found = mcp(SEARCH_TOOL, query=query, depth="L0", limit=5, compact=compact)
    hit = found["h" if compact else "hits"][0]
    if compact:
        args = {
            "uid": hit["i"],
            "message": hit["r"]["m"],
            "offset": hit["r"]["o"],
            "limit": hit["r"]["l"],
        }
    else:
        args = dict(hit["read"])
    assert args["uid"] == uid_of(index)
    assert set(args) == {"uid", "message", "offset", "limit"}
    passage = mcp(READ_TOOL, **args)
    assert passage["message"] == target_message(index)
    assert marker(index) in passage["text"]
    assert len(passage["text"]) <= args["limit"]


def test_benchmark_every_synthetic_query_reaches_its_unit_within_two_calls(mcp):
    reached = 0
    for index in range(RECORDS):
        query = f"lattice {marker(index)}" if index % 2 else marker(index)
        hit = mcp(SEARCH_TOOL, query=query, depth="L0", limit=5)["hits"][0]
        passage = mcp(READ_TOOL, **hit["read"])
        reached += hit["uid"] == uid_of(index) and marker(index) in passage["text"]
    assert reached == RECORDS


def test_scores_follow_the_ranking_order_and_rare_terms_score_higher(mcp):
    ranked = mcp(SEARCH_TOOL, query="synthetic lattice", depth="L0", limit=30)["hits"]
    scores = [hit["score"] for hit in ranked]
    assert scores == sorted(scores, reverse=True)
    rare = mcp(SEARCH_TOOL, query=marker(7), depth="L0")["hits"][0]["score"]
    common = mcp(SEARCH_TOOL, query="lattice", depth="L0")["hits"][0]["score"]
    assert rare > common > -1


def test_handoff_follows_a_match_that_lives_in_the_alternate_branch(mcp):
    found = mcp(SEARCH_TOOL, query="quasarpulse", depth="L0")
    read = found["hits"][0]["read"]
    assert read["sequence"] == "alternate" and read["message"] == 1
    passage = mcp(READ_TOOL, **read)
    assert "quasarpulse" in passage["text"] and passage["sequence"] == "alternate"


def test_title_only_match_has_a_start_of_record_locator(mcp):
    found = mcp(SEARCH_TOOL, query="Branchy", depth="L0")
    hit = found["hits"][0]
    assert hit["read"]["message"] == 0 and hit["read"]["offset"] == 0
    assert hit["read"]["located"] is False


def test_read_around_query_centres_on_the_first_match(mcp):
    index = 40
    result = mcp(READ_TOOL, uid=uid_of(index), around_query=marker(index), limit=200)
    assert result["message"] == target_message(index)
    assert marker(index) in result["text"] and len(result["text"]) == 200
    match = result["match"]
    assert match["message"] == target_message(index)
    assert result["text"][match["start"] - result["offset"] :].startswith(marker(index))
    assert result["offset"] > 0
    middle = result["offset"] + 100
    assert abs((match["start"] + match["end"]) // 2 - middle) <= 1


def test_read_around_query_without_a_match_reports_it_and_keeps_position(mcp):
    result = mcp(READ_TOOL, uid=uid_of(3), around_query="nonexistentterm", limit=50, message=2)
    assert result["match"] is None
    assert result["message"] == 2 and result["offset"] == 0 and len(result["text"]) == 50


def test_read_around_query_is_exclusive_with_offset_and_toc(mcp):
    with pytest.raises(ValueError, match="around_query"):
        mcp(READ_TOOL, uid=uid_of(3), around_query="lattice", offset=5)
    with pytest.raises(ValueError, match="around_query"):
        mcp(READ_TOOL, uid=uid_of(3), around_query="lattice", toc=True)


def test_range_read_returns_messages_and_a_resumable_cursor(mcp):
    uid = uid_of(4)
    whole = mcp(READ_TOOL, uid=uid, message=2, message_end=5, limit=32000)
    assert [item["message"] for item in whole["messages"]] == [2, 3, 4]
    assert whole["next_message"] is None and whole["next_offset"] is None
    assert whole["total_exact"] is True and whole["message_count"] == MESSAGES
    expected = "".join(mcp(READ_TOOL, uid=uid, message=n, limit=32000)["text"] for n in (2, 3, 4))
    assert "".join(item["text"] for item in whole["messages"]) == expected

    pieces, cursor = [], {"message": 2, "offset": 0}
    for _ in range(100):
        page = mcp(READ_TOOL, uid=uid, message_end=5, limit=700, **cursor)
        pieces.append("".join(item["text"] for item in page["messages"]))
        assert sum(len(item["text"]) for item in page["messages"]) <= 700
        if page["next_message"] is None:
            break
        cursor = {"message": page["next_message"], "offset": page["next_offset"] or 0}
    assert "".join(pieces) == expected and len(pieces) > 1


def test_range_read_beyond_the_record_end_stops_at_the_last_message(mcp):
    page = mcp(READ_TOOL, uid=uid_of(4), message=10, message_end=500, limit=32000)
    assert [item["message"] for item in page["messages"]] == [10, 11]
    assert page["next_message"] is None
    with pytest.raises(ValueError, match="message_end"):
        mcp(READ_TOOL, uid=uid_of(4), message=5, message_end=5)


def test_range_read_is_capped_per_call_with_a_cursor_to_the_rest(mcp):
    uid = Conversation(provider="chatgpt", source_id="longlog", title="Long log").uid
    first = mcp(READ_TOOL, uid=uid, message=0, message_end=250, limit=32000)
    assert len(first["messages"]) == 200 and first["message_end"] == 200
    assert first["next_message"] == 200 and first["next_offset"] == 0
    second = mcp(READ_TOOL, uid=uid, message=200, message_end=250, limit=32000)
    assert [item["message"] for item in second["messages"]] == list(range(200, 250))
    assert second["next_message"] is None
    toc = mcp(READ_TOOL, uid=uid, toc=True, toc_limit=500)
    assert toc["toc_total"] == 250 and len(toc["toc"]) == 250 and toc["toc_next_offset"] is None


def test_compact_read_drops_envelope_and_shortens_keys(mcp):
    full = mcp(READ_TOOL, uid=uid_of(4), message=2, limit=300)
    short = mcp(READ_TOOL, uid=uid_of(4), message=2, limit=300, compact=True)
    assert short["x"] == full["text"] and short["i"] == full["uid"] and short["m"] == 2
    assert short["no"] == full["next_offset"] and short["tc"] == full["total_characters"]
    assert not {"provenance", "source_notice", "text", "uid"} & set(short)
    assert len(json.dumps(short)) < len(json.dumps(full)) - 400


def test_compact_key_table_has_unique_short_names():
    assert len(set(library_mcp.COMPACT_KEYS.values())) == len(library_mcp.COMPACT_KEYS)


def test_toc_lists_headings_with_offsets_and_paginates(mcp):
    toc = mcp(READ_TOOL, uid=uid_of(4), toc=True)
    assert toc["toc_kind"] == "headings" and toc["message_count"] == MESSAGES
    headings = [entry["heading"] for entry in toc["toc"]]
    assert headings[0].startswith("Overview") and any(h.startswith("Details") for h in headings)
    assert toc["toc_total"] == len(toc["toc"]) and toc["toc_exact"] is True
    first = toc["toc"][0]
    passage = mcp(
        READ_TOOL, uid=uid_of(4), message=first["message"], offset=first["offset"], limit=10
    )
    assert passage["text"] == "# Overview"
    page = mcp(READ_TOOL, uid=uid_of(4), toc=True, toc_limit=1, toc_offset=1)
    assert len(page["toc"]) == 1 and page["toc_next_offset"] is None
    assert page["toc"][0] == toc["toc"][1]


def test_toc_falls_back_to_a_message_index_without_headings(mcp):
    plain = mcp(READ_TOOL, uid=build_branchy_uid(), toc=True)
    assert plain["toc_kind"] == "messages" and plain["toc_total"] == 1
    entry = plain["toc"][0]
    assert entry["role"] == "user" and entry["chars"] == len("main branch only")
    assert entry["preview"] == "main branch only"


def build_branchy_uid() -> str:
    return Conversation(provider="chatgpt", source_id="branchy", title="Branchy").uid


def test_toc_message_mode_is_bounded_on_long_records(mcp):
    toc = mcp(READ_TOOL, uid=uid_of(4), toc=True, toc_mode="messages", toc_limit=5)
    assert toc["toc_kind"] == "messages" and len(toc["toc"]) == 5
    assert toc["toc_total"] == MESSAGES and toc["toc_next_offset"] == 5
    assert all(len(entry["preview"]) <= 80 for entry in toc["toc"])


def test_budgeted_and_compact_calls_use_compact_wire_text_default_stays_pretty(
    library_path, tmp_path, monkeypatch
):
    monkeypatch.setattr(library_mcp, "open_library", lambda root: (tmp_path, Library(library_path)))
    server = CceMcpServer(default_project_root=tmp_path, library_store_root=tmp_path)
    plain = server.call_tool(SEARCH_TOOL, {"query": "lattice", "limit": 2})
    assert plain["content"][0]["text"] == json.dumps(
        plain["structuredContent"], indent=2, sort_keys=True
    )
    slim = server.call_tool(SEARCH_TOOL, {"query": "lattice", "depth": "L0", "limit": 10})
    text = slim["content"][0]["text"]
    assert "\n" not in text and json.loads(text) == slim["structuredContent"]
    stated = slim["structuredContent"]["token_estimate"]["tokens"]
    assert math.ceil(len(text.encode()) / 4) <= stated
