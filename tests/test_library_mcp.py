"""Progressive disclosure at the library MCP boundary, on a synthetic library only."""

import json
import math
import re
import sqlite3
import zlib

import pytest

from conversation_corpus_engine import library_mcp
from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.library_commands import PASSAGE_LIMIT
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
    # Not located is said on the hit; `read` stays exactly the read tool's arguments.
    assert hit["located"] is False and "located" not in hit["read"]


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


SENTINEL = "sentinelaltcontent"
# Shapes Library.put stores and indexes without complaint but the read adapters cannot interpret.
MALFORMED_ALTERNATES = [
    pytest.param(SENTINEL, id="string"),
    pytest.param([1, SENTINEL], id="non-dict-items"),
    pytest.param({"key": SENTINEL}, id="mapping"),
]


def synthetic(source_id: str, title: str, text: str, extra: dict | None = None) -> Conversation:
    return Conversation(
        provider="chatgpt",
        source_id=source_id,
        title=title,
        messages=[Message(role="user", text=text)],
        extra=extra or {},
    )


@pytest.fixture
def mount(tmp_path, monkeypatch):
    """Build a small private library from the given records and return its tool caller."""

    def build(*records: Conversation):
        path = tmp_path / "small-library.sqlite"
        with Library.create(path) as library:
            for record in records:
                library.put(record)
        monkeypatch.setattr(library_mcp, "open_library", lambda root: (tmp_path, Library(path)))

        def call(name, **arguments):
            return library_mcp.call_tool(tmp_path, name, arguments)

        return call

    return build


@pytest.mark.parametrize("alternates", MALFORMED_ALTERNATES)
def test_default_search_degrades_one_malformed_hit_instead_of_failing_the_page(mount, alternates):
    good = synthetic("good", "badalt good", "badalt body")
    bad = synthetic("bad", "badalt record", "badalt body", {"alternate_messages": alternates})
    result = mount(good, bad)(SEARCH_TOOL, query="badalt")
    assert result["total"] == 2 and result["returned"] == 2 and result["next_offset"] is None
    by_uid = {hit["uid"]: hit for hit in result["hits"]}
    assert set(by_uid) == {good.uid, bad.uid}
    fallback = by_uid[bad.uid]["read"]
    assert set(fallback) == {"uid", "message", "offset", "limit"}
    assert fallback["uid"] == bad.uid and fallback["message"] == 0 and fallback["offset"] == 0
    assert fallback["limit"] == PASSAGE_LIMIT
    degraded = by_uid[bad.uid]
    assert degraded["located"] is False
    assert isinstance(degraded["locator_error"], str) and degraded["locator_error"]
    assert set(degraded) >= LEGACY_HIT_KEYS
    intact = by_uid[good.uid]["read"]
    assert set(intact) == {"uid", "message", "offset", "limit"}
    assert not {"located", "locator_error"} & set(by_uid[good.uid])
    assert SENTINEL not in json.dumps(result)


# Record `name` holds `lead` filler messages and then the first message with the query term, so
# a located hit points at message `lead`; the malformed record is `omega`, placed mid-ranking.
LEADS = {"alpha": 0, "beta": 2, "gamma": 3, "omega": 1, "delta": 4}
SHORT_KEYS = {short: long for long, short in library_mcp.COMPACT_KEYS.items()}


def paged_record(name: str, extra: dict | None = None) -> Conversation:
    lead = LEADS[name]
    messages = [Message(role="user", text="plain filler words") for _ in range(lead)]
    messages.append(Message(role="assistant", text=f"pagemark finding of {name}"))
    return Conversation(
        provider="chatgpt",
        source_id=name,
        title=f"Synthetic {name}",
        messages=messages,
        extra=extra or {},
    )


def paged_library(mount, alternates=SENTINEL):
    records = [
        paged_record(name, {"alternate_messages": alternates} if name == "omega" else None)
        for name in LEADS
    ]
    return mount(*records)


def unpacked(result: dict, compact: bool) -> dict:
    """Page summary with long keys, so default and compact responses are checked alike."""
    if not compact:
        return {
            "total": result["total"],
            "returned": result["returned"],
            "next_offset": result["next_offset"],
            "reads": {hit["uid"]: dict(hit["read"]) for hit in result["hits"]},
            "flags": {hit["uid"]: located_flags(hit) for hit in result["hits"]},
        }
    return {
        "total": result["n"],
        "returned": result["c"],
        "next_offset": result["no"],
        "reads": {
            hit["i"]: {
                "uid": hit["i"],
                **{SHORT_KEYS[key]: value for key, value in hit["r"].items()},
            }
            for hit in result["h"]
        },
        "flags": {
            hit["i"]: located_flags({SHORT_KEYS.get(key, key): value for key, value in hit.items()})
            for hit in result["h"]
        },
    }


def located_flags(hit: dict) -> dict:
    """The hit-level not-located signal (`located`, `locator_error`), under its long names."""
    return {key: hit[key] for key in ("located", "locator_error") if key in hit}


SEARCH_MODES = [
    pytest.param({}, id="default"),
    pytest.param({"compact": True}, id="default-compact"),
    pytest.param({"depth": "L0"}, id="l0"),
    pytest.param({"depth": "L0", "compact": True}, id="l0-compact"),
    pytest.param({"budget_tokens": 20000}, id="budgeted"),
    pytest.param({"depth": "L0", "budget_tokens": 1200, "compact": True}, id="l0-budgeted"),
]


@pytest.mark.parametrize("alternates", MALFORMED_ALTERNATES)
@pytest.mark.parametrize("mode", SEARCH_MODES)
def test_one_malformed_record_among_several_leaves_the_other_hits_and_counts_intact(
    mount, mode, alternates
):
    call = paged_library(mount, alternates)
    result = call(SEARCH_TOOL, query="pagemark", **mode)
    page = unpacked(result, mode.get("compact", False))
    uids = {name: paged_record(name).uid for name in LEADS}
    assert page["total"] == len(LEADS) and page["returned"] == len(LEADS)
    assert page["next_offset"] is None and not {"truncated", "tr"} & set(result)
    assert set(page["reads"]) == set(uids.values())
    for name, uid in uids.items():
        read = page["reads"][uid]
        flags = page["flags"][uid]
        if name == "omega":
            assert read == {"uid": uid, "message": 0, "offset": 0, "limit": PASSAGE_LIMIT}
            assert flags == {"located": False, "locator_error": flags["locator_error"]}
            assert flags["locator_error"] and SENTINEL not in flags["locator_error"]
            continue
        assert flags == {}
        assert read == {"uid": uid, "message": LEADS[name], "offset": 0, "limit": PASSAGE_LIMIT}
        passage = call(READ_TOOL, **read)
        assert (
            passage["message"] == LEADS[name] and f"pagemark finding of {name}" in passage["text"]
        )
    assert SENTINEL not in json.dumps(result)


def test_compact_search_carries_the_located_and_locator_error_keys_without_a_uid(mount):
    result = paged_library(mount)(SEARCH_TOOL, query="pagemark", depth="L0", compact=True)
    bad_uid = paged_record("omega").uid
    # The signal sits on the hit, so `r` keeps only the read tool's arguments (no uid, short keys).
    degraded = next(hit for hit in result["h"] if hit["i"] == bad_uid)
    assert set(degraded["r"]) == {"m", "o", "l"}
    assert set(degraded) == {"i", "t", "s", "r", "lc", "le"} and degraded["lc"] is False
    assert isinstance(degraded["le"], str) and degraded["le"]
    intact = next(hit for hit in result["h"] if hit["i"] != bad_uid)
    assert set(intact["r"]) == {"m", "o", "l"} and set(intact) == {"i", "t", "s", "r"}
    assert library_mcp.COMPACT_KEYS["located"] == "lc"
    assert library_mcp.COMPACT_KEYS["locator_error"] == "le"


def test_pagination_and_truncation_stay_exact_across_a_malformed_record(mount):
    call = paged_library(mount)
    uids = {paged_record(name).uid for name in LEADS}
    for arguments in ({"limit": 2}, {"limit": 5, "budget_tokens": 230}):
        seen, offset, pages = [], 0, 0
        while offset is not None:
            result = call(SEARCH_TOOL, query="pagemark", depth="L0", offset=offset, **arguments)
            assert result["total"] == len(LEADS) and result["total_exact"] is True
            assert result["returned"] == len(result["hits"]) >= 1
            seen.extend(hit["uid"] for hit in result["hits"])
            offset, pages = result["next_offset"], pages + 1
            assert pages <= len(LEADS)
        assert pages > 1 and sorted(seen) == sorted(uids)


def test_single_record_reads_of_a_malformed_record_raise_an_error_naming_it(mount):
    good = synthetic("good", "badalt good", "badalt body")
    bad = synthetic("bad", "badalt record", "badalt body", {"alternate_messages": SENTINEL})
    call = mount(good, bad)
    for arguments in (
        {},
        {"sequence": "alternate"},
        {"around_query": "badalt"},
        {"message_end": 1},
        {"toc": True},
    ):
        with pytest.raises(ValueError, match=re.escape(bad.uid)) as raised:
            call(READ_TOOL, uid=bad.uid, **arguments)
        assert SENTINEL not in str(raised.value)
    assert call(READ_TOOL, uid=good.uid)["text"] == "badalt body"


def test_server_answers_a_page_with_a_malformed_hit_and_names_the_record_on_a_single_read(
    mount, tmp_path
):
    good = synthetic("good", "badalt good", "badalt body")
    bad = synthetic("bad", "badalt record", "badalt body", {"alternate_messages": SENTINEL})
    mount(good, bad)
    server = CceMcpServer(default_project_root=tmp_path, library_store_root=tmp_path)
    page = server.call_tool(SEARCH_TOOL, {"query": "badalt", "depth": "L0"})
    assert page["isError"] is False and page["structuredContent"]["total"] == 2
    assert SENTINEL not in page["content"][0]["text"]
    single = server.call_tool(READ_TOOL, {"uid": bad.uid})
    assert single["isError"] is True and bad.uid in single["content"][0]["text"]


@pytest.mark.parametrize(
    "error",
    [
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        IndexError,
        RecursionError,
        pytest.param(zlib.error, id="zlib.error"),
    ],
)
def test_every_malformed_data_error_class_degrades_only_its_own_hit(mount, monkeypatch, error):
    call = paged_library(mount, [])
    bad_uid = paged_record("beta").uid
    real = library_mcp.passage_handoff

    def handoff(payload, query, *args):
        if payload["uid"] == bad_uid:
            raise error(SENTINEL)
        return real(payload, query, *args)

    monkeypatch.setattr(library_mcp, "passage_handoff", handoff)
    result = call(SEARCH_TOOL, query="pagemark", depth="L0")
    hits = {hit["uid"]: hit for hit in result["hits"]}
    assert result["total"] == result["returned"] == len(LEADS) == len(hits)
    assert hits[bad_uid]["located"] is False
    assert f"({error.__name__})" in hits[bad_uid]["locator_error"]
    assert [uid for uid, hit in hits.items() if "locator_error" in hit] == [bad_uid]
    assert not any({"located", "locator_error"} & set(hit["read"]) for hit in hits.values())
    assert SENTINEL not in json.dumps(result)


def test_a_database_failure_is_not_a_malformed_record_and_still_fails_the_call(mount, monkeypatch):
    call = paged_library(mount, [])

    def broken(payload, query, *args):
        raise sqlite3.OperationalError("database is unavailable")

    monkeypatch.setattr(library_mcp, "passage_handoff", broken)
    with pytest.raises(sqlite3.OperationalError):
        call(SEARCH_TOOL, query="pagemark", depth="L0")


READ_PROPERTIES = {
    item["name"]: set(item["inputSchema"]["properties"]) for item in library_mcp.tool_definitions()
}


def probe_records(alternates=SENTINEL) -> dict[str, Conversation]:
    """One record per way a hit's passage is (or is not) located, all matching `readprobe`."""
    return {
        "located": synthetic(
            "located", "Plain located", "opening words then readprobe in the body"
        ),
        "title_only": synthetic("title-only", "Readprobe in the title only", "body without it"),
        "alternate": Conversation(
            provider="chatgpt",
            source_id="alternate",
            title="Branch record",
            messages=[Message(role="user", text="main branch words")],
            extra={"alternate_messages": [{"role": "assistant", "text": "branch readprobe here"}]},
        ),
        "malformed": synthetic(
            "malformed", "Readprobe malformed", "readprobe body", {"alternate_messages": alternates}
        ),
    }


@pytest.mark.parametrize("mode", SEARCH_MODES)
def test_every_hit_read_object_is_exactly_the_read_tools_arguments(mount, mode):
    """`read` is documented as the arguments to pass to cce_library_read, so it must carry nothing
    else: a located, title-only, alternate-branch or malformed-record hit alike (default, L0,
    compact; a compact `r` is decoded with the full key table and gets its uid back first)."""
    records = probe_records()
    call = mount(*records.values())
    result = call(SEARCH_TOOL, query="readprobe", **mode)
    page = unpacked(result, mode.get("compact", False))
    assert page["total"] == page["returned"] == len(records) and page["next_offset"] is None
    uids = {name: record.uid for name, record in records.items()}
    assert set(page["reads"]) == set(uids.values())
    for name, uid in uids.items():
        read = page["reads"][uid]
        assert set(read) <= READ_PROPERTIES[READ_TOOL], (name, sorted(read))
        if name == "malformed":
            # The record's own shape is unreadable, so a read of it fails by design, and says so
            # by naming the record; it is not refused as an unsupported argument.
            with pytest.raises(ValueError, match=re.escape(uid)) as refused:
                call(READ_TOOL, **read)
            assert "unsupported" not in str(refused.value)
            continue
        passage = call(READ_TOOL, **read)
        assert passage["uid"] == uid and passage["message"] == read["message"]
        if name == "located":
            assert "readprobe" in passage["text"]
        if name == "alternate":
            assert read["sequence"] == "alternate" and "readprobe" in passage["text"]
    # What is not located is said on the hit, never inside `read`.
    assert page["flags"][uids["located"]] == {} and page["flags"][uids["alternate"]] == {}
    assert page["flags"][uids["title_only"]] == {"located": False}
    flags = page["flags"][uids["malformed"]]
    assert flags["located"] is False and flags["locator_error"]
