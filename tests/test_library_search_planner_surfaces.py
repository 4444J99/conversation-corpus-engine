"""The search planner at the MCP tool and the command line, on a synthetic library only."""

import json
import math
import time

import pytest

from conversation_corpus_engine import library_commands, library_mcp, library_store
from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.cli import build_parser
from conversation_corpus_engine.library_commands import handle_library
from conversation_corpus_engine.library_mcp import COMPACT_KEYS, SEARCH_TOOL
from conversation_corpus_engine.library_store import (
    BROAD_QUERY_HINT,
    DEFAULT_SEARCH_SECONDS,
    MAX_SEARCH_SECONDS,
    SEARCH_PLANS,
    Library,
)
from conversation_corpus_engine.mcp_server import CceMcpServer

CROWD = 1010
LEGACY_RESPONSE_KEYS = {
    "query",
    "total",
    "total_exact",
    "total_label",
    "offset",
    "limit",
    "returned",
    "next_offset",
    "hits",
    "generation",
}
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
    "score",
    "read",
}


def conv(identity: str, *texts: str) -> Conversation:
    return Conversation(
        provider="chatgpt",
        source_id=identity,
        title=f"Note {identity}",
        messages=[Message(role="user", text=text) for text in texts],
        extra={"wing": "ai", "lane": "conversations", "body_status": "full"},
    )


@pytest.fixture(scope="module")
def library_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("surfaces") / "library.sqlite"
    with Library.create(path) as library:
        library.connection.execute("PRAGMA synchronous=OFF")
        library.connection.execute("PRAGMA journal_mode=MEMORY")
        for number in range(CROWD):
            library.put(conv(f"crowd{number:04d}", f"omega filler {number}"))
        # The words are adjacent only in the last message of "together".
        library.put(
            conv(
                "together",
                "lumen alone",
                "lumen then glyph apart",
                "filler",
                "see the lumen glyph together",
            )
        )
        library.put(conv("apart", "lumen and later glyph, never adjacent"))
    return path


@pytest.fixture
def mcp(library_path, tmp_path, monkeypatch):
    monkeypatch.setattr(library_mcp, "open_library", lambda root: (tmp_path, Library(library_path)))

    def call(name, **arguments):
        return library_mcp.call_tool(tmp_path, name, arguments)

    return call


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def expire_when_ranking_starts(library_path, tmp_path, monkeypatch) -> FakeClock:
    """Make every library the tool opens move a fake clock past any budget once it ranks."""
    clock = FakeClock()
    monkeypatch.setattr(library_store, "_clock", clock)
    monkeypatch.setattr(library_store, "SEARCH_PROGRESS_INSTRUCTIONS", 10)

    def opened(_root):
        clock.now = 0.0  # every call starts within its budget
        library = Library(library_path)
        library.connection.set_trace_callback(
            lambda statement: setattr(clock, "now", 100.0) if "bm25" in statement else None
        )
        return tmp_path, library

    monkeypatch.setattr(library_mcp, "open_library", opened)
    monkeypatch.setattr(library_commands, "open_library", opened)
    return clock


# --- the MCP tool ------------------------------------------------------------------------------


def test_tool_definition_declares_plan_and_max_seconds():
    definition = next(d for d in library_mcp.tool_definitions() if d["name"] == SEARCH_TOOL)
    properties = definition["inputSchema"]["properties"]
    assert properties["plan"]["enum"] == list(SEARCH_PLANS)
    seconds = properties["max_seconds"]
    assert seconds["type"] == "number" and seconds["exclusiveMinimum"] == 0
    assert seconds["maximum"] == MAX_SEARCH_SECONDS == 600
    assert "15" in seconds["description"] and "index order" in seconds["description"]
    assert "phrase" in properties["plan"]["description"]
    assert "phrase" in properties["query"]["description"]
    assert "1000+" in definition["description"]
    assert "lower bound" in definition["description"] and "N+" in definition["description"]
    for key in ("pn", "pt", "rk", "es"):
        assert key in properties["compact"]["description"]


def test_default_response_keeps_every_existing_field_and_adds_the_planner_fields(mcp):
    result = mcp(SEARCH_TOOL, query="omega", limit=3)
    assert set(result) >= LEGACY_RESPONSE_KEYS
    assert set(result) - LEGACY_RESPONSE_KEYS == {
        "plan",
        "partial",
        "ranked",
        "elapsed_s",
        "unit",
        "total_unit",
    }
    assert result["plan"] == "and" and result["partial"] is False and result["ranked"] is True
    assert isinstance(result["elapsed_s"], float) and result["elapsed_s"] >= 0
    for hit in result["hits"]:
        assert set(hit) == LEGACY_HIT_KEYS


def test_the_first_plan_that_matches_answers_and_hits_are_scored_and_located_for_it(mcp):
    phrase = mcp(SEARCH_TOOL, query="lumen glyph")
    assert phrase["plan"] == "phrase" and phrase["total"] == 1
    hit = phrase["hits"][0]
    assert isinstance(hit["score"], float) and hit["score"] > 0
    # The phrase plan centres the passage on the adjacent words (message 3), not on the first
    # message that merely holds both (message 1).
    assert hit["read"]["message"] == 3
    anded = mcp(SEARCH_TOOL, query="lumen glyph", plan="and")
    assert anded["plan"] == "and" and anded["total"] == 2
    by_uid = {item["uid"]: item for item in anded["hits"]}
    assert by_uid[conv("together", "x").uid]["read"]["message"] == 1


def test_or_answered_hits_are_scored_with_the_or_match(mcp):
    result = mcp(SEARCH_TOOL, query="lumen nosuchword", limit=5)
    assert result["plan"] == "or" and result["total"] == 2
    assert all(isinstance(hit["score"], float) for hit in result["hits"])
    assert all(hit["read"]["message"] in (0, 1, 3) for hit in result["hits"])


def test_a_total_beyond_the_cap_is_labelled_and_an_or_over_the_cap_carries_the_hint(mcp):
    result = mcp(SEARCH_TOOL, query="omega filler", limit=2)
    assert result["plan"] == "phrase" and result["total"] == 1000
    assert result["total_exact"] is False and result["total_label"] == "1000+"
    assert "hint" not in result
    broad = mcp(SEARCH_TOOL, query="omega nosuchword", limit=2)
    assert broad["plan"] == "or" and broad["total_label"] == "1000+"
    assert broad["hint"] == BROAD_QUERY_HINT
    assert broad["next_offset"] == 2


def test_the_cap_label_and_planner_fields_reach_the_depth_l0_response(mcp):
    result = mcp(SEARCH_TOOL, query="omega nosuchword", depth="L0", limit=2)
    assert result["total"] == 1000 and result["total_exact"] is False
    assert result["total_label"] == "1000+" and result["plan"] == "or"
    assert result["partial"] is False and result["ranked"] is True
    assert result["hint"] == BROAD_QUERY_HINT
    assert result["token_estimate"]["tokens"] > 0


def test_compact_mode_shortens_the_new_keys_and_keeps_the_cap_flag(mcp):
    result = mcp(SEARCH_TOOL, query="omega nosuchword", depth="L0", limit=2, compact=True)
    assert result["n"] == 1000 and result["nx"] is False
    assert result["pn"] == "or" and result["pt"] is False and result["rk"] is True
    assert isinstance(result["es"], float) and result["ht"] == BROAD_QUERY_HINT
    assert not {"plan", "partial", "ranked", "elapsed_s", "hint", "total_label"} & set(result)
    default = mcp(SEARCH_TOOL, query="omega", limit=2, compact=True)
    assert default["pn"] == "and" and default["nx"] is False and "total_label" not in default


def test_compact_keys_are_unique_and_do_not_collide_with_the_token_estimate_key():
    values = list(COMPACT_KEYS.values())
    assert len(set(values)) == len(values)
    assert "tk" not in values
    assert {"plan", "partial", "ranked", "elapsed_s", "hint", "notice"} <= set(COMPACT_KEYS)
    assert all(len(COMPACT_KEYS[key]) <= 3 for key in ("plan", "partial", "ranked", "elapsed_s"))


def test_an_expired_budget_returns_a_partial_unranked_page_through_the_tool(
    library_path, tmp_path, monkeypatch
):
    expire_when_ranking_starts(library_path, tmp_path, monkeypatch)

    def call(**arguments):
        return library_mcp.call_tool(tmp_path, SEARCH_TOOL, {"query": "omega filler", **arguments})

    result = call(limit=4, max_seconds=1)
    assert result["partial"] is True and result["ranked"] is False and result["plan"] == "phrase"
    assert "index order" in result["notice"] and result["hint"] == BROAD_QUERY_HINT
    assert [hit["uid"] for hit in result["hits"]] == [
        conv(f"crowd{n:04d}", "x").uid for n in range(4)
    ]
    assert all(hit["score"] is None for hit in result["hits"])
    assert all(hit["read"]["message"] == 0 for hit in result["hits"])
    assert result["total"] == 1000 and result["next_offset"] == 4
    compact = call(limit=4, max_seconds=1, depth="L0", compact=True)
    assert compact["pt"] is True and compact["rk"] is False and "nt" in compact
    assert all(hit["s"] is None for hit in compact["h"])


def test_a_search_inside_its_budget_is_not_partial(mcp):
    result = mcp(SEARCH_TOOL, query="omega", limit=3, max_seconds=30)
    assert result["partial"] is False and result["ranked"] is True


def test_plan_and_max_seconds_defaults_and_overrides_reach_the_search(mcp, monkeypatch):
    seen = []
    real = Library.search

    def spy(self, query, **kwargs):
        seen.append(kwargs)
        return real(self, query, **kwargs)

    monkeypatch.setattr(Library, "search", spy)
    mcp(SEARCH_TOOL, query="lumen glyph")
    mcp(SEARCH_TOOL, query="lumen glyph", plan="or", max_seconds=2.5)
    assert (seen[0]["plan"], seen[0]["max_seconds"]) == ("auto", DEFAULT_SEARCH_SECONDS)
    assert DEFAULT_SEARCH_SECONDS == 15.0
    assert (seen[1]["plan"], seen[1]["max_seconds"]) == ("or", 2.5)


@pytest.mark.parametrize(
    ("arguments", "name"),
    [
        ({"plan": "fast"}, "plan"),
        ({"plan": ""}, "plan"),
        ({"plan": None}, "plan"),
        ({"plan": 3}, "plan"),
        ({"max_seconds": 0}, "max_seconds"),
        ({"max_seconds": -1}, "max_seconds"),
        ({"max_seconds": 601}, "max_seconds"),
        ({"max_seconds": math.nan}, "max_seconds"),
        ({"max_seconds": math.inf}, "max_seconds"),
        ({"max_seconds": True}, "max_seconds"),
        ({"max_seconds": "5"}, "max_seconds"),
        ({"max_seconds": None}, "max_seconds"),
    ],
)
def test_plan_and_max_seconds_are_validated(mcp, arguments, name):
    with pytest.raises(ValueError, match=name):
        mcp(SEARCH_TOOL, query="lumen glyph", **arguments)


def test_the_upper_bound_of_max_seconds_is_accepted(mcp):
    assert mcp(SEARCH_TOOL, query="lumen glyph", max_seconds=600)["total"] == 1
    tiny = mcp(SEARCH_TOOL, query="lumen glyph", max_seconds=0.001)
    assert tiny["total"] in (0, 1) and isinstance(tiny["partial"], bool)


def test_the_server_reports_a_bad_planner_argument_as_a_tool_error(
    library_path, tmp_path, monkeypatch
):
    monkeypatch.setattr(library_mcp, "open_library", lambda root: (tmp_path, Library(library_path)))
    server = CceMcpServer(default_project_root=tmp_path, library_store_root=tmp_path)
    bad = server.call_tool(SEARCH_TOOL, {"query": "lumen glyph", "max_seconds": 601})
    assert bad["isError"] is True and "max_seconds" in bad["content"][0]["text"]
    good = server.call_tool(SEARCH_TOOL, {"query": "lumen glyph", "plan": "phrase"})
    assert good["isError"] is False and good["structuredContent"]["plan"] == "phrase"


# --- the command line --------------------------------------------------------------------------


def parse(*extra):
    return build_parser().parse_args(["library", "search", "lumen glyph", *extra])


def test_cli_defaults_and_values():
    args = parse()
    assert args.plan == "auto" and args.max_seconds == DEFAULT_SEARCH_SECONDS
    args = parse("--plan", "or", "--max-seconds", "2.5")
    assert args.plan == "or" and args.max_seconds == 2.5
    assert parse("--max-seconds", "600").max_seconds == 600.0


@pytest.mark.parametrize("plan", SEARCH_PLANS)
def test_cli_accepts_every_plan(plan):
    assert parse("--plan", plan).plan == plan


BAD_BUDGET = "max_seconds must be a number greater than 0 and at most 600"


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (("--plan", "fast"), "argument --plan: invalid choice: 'fast'"),
        (("--plan", "AND"), "argument --plan: invalid choice: 'AND'"),
        (("--max-seconds", "0"), BAD_BUDGET),
        (("--max-seconds", "-3"), BAD_BUDGET),
        (("--max-seconds", "601"), BAD_BUDGET),
        (("--max-seconds", "-0.0"), BAD_BUDGET),
        (("--max-seconds", "0.0"), BAD_BUDGET),
        (("--max-seconds", "1" + "0" * 400), BAD_BUDGET),
        *(
            (("--max-seconds", text), f"argument --max-seconds: {text!r} is not a decimal number")
            for text in (
                "nan",
                "inf",
                "infinity",
                "abc",
                "",
                "5_0",
                " 5 ",
                "5 ",
                "0x10",
                "1e-9",
                "1E3",
                "+5",
                "1.2.3",
                ".",
                "\u0665",  # an Arabic-Indic digit, which float() accepts
                "\uff15",  # a full-width digit, which float() accepts
            )
        ),
    ],
)
def test_cli_rejects_a_bad_plan_or_budget_before_searching(extra, message, capsys):
    with pytest.raises(SystemExit) as raised:
        parse(*extra)
    assert raised.value.code == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("15", 15.0), ("2.5", 2.5), (".5", 0.5), ("5.", 5.0), ("600", 600.0), ("007", 7.0)],
)
def test_cli_accepts_plain_decimal_budgets(text, seconds):
    assert parse("--max-seconds", text).max_seconds == seconds


def test_cli_help_documents_the_plan_order_the_budget_and_the_cost_of_or(capsys):
    with pytest.raises(SystemExit) as raised:
        build_parser().parse_args(["library", "search", "--help"])
    assert raised.value.code == 0
    text = " ".join(capsys.readouterr().out.split())
    assert "--plan" in text and "--max-seconds" in text
    assert "phrase" in text and "AND" in text and "OR" in text
    assert "15" in text and "600" in text and "index order" in text
    assert "slow" in text and "quote a phrase" in text
    assert "lower bound" in text and "1000+" in text


def run_search(tmp_path, capsys, *extra):
    args = build_parser().parse_args(["library", "search", "--store-root", str(tmp_path), *extra])
    code = handle_library(args)
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


@pytest.fixture
def cli(library_path, tmp_path, monkeypatch):
    monkeypatch.setattr(
        library_commands, "open_library", lambda root: (tmp_path, Library(library_path))
    )


def test_cli_prints_the_plan_fields_and_stays_quiet_when_nothing_is_wrong(cli, tmp_path, capsys):
    code, result, err = run_search(tmp_path, capsys, "lumen glyph")
    assert code == 0 and result["plan"] == "phrase" and result["total"] == 1
    assert result["partial"] is False and result["ranked"] is True and err == ""


def test_cli_passes_the_plan_and_prints_the_hint_to_stderr_for_a_broad_or(cli, tmp_path, capsys):
    code, result, err = run_search(tmp_path, capsys, "omega nosuchword", "--limit", "2")
    assert code == 0 and result["plan"] == "or" and result["total_label"] == "1000+"
    assert result["hint"] == BROAD_QUERY_HINT
    assert BROAD_QUERY_HINT in err and err.startswith("library: hint:")
    code, forced, err = run_search(tmp_path, capsys, "lumen glyph", "--plan", "and")
    assert forced["plan"] == "and" and forced["total"] == 2 and err == ""


def test_cli_reports_a_partial_page_and_says_so_on_stderr(
    library_path, tmp_path, monkeypatch, capsys
):
    expire_when_ranking_starts(library_path, tmp_path, monkeypatch)
    code, result, err = run_search(
        tmp_path, capsys, "omega filler", "--limit", "3", "--max-seconds", "1"
    )
    assert code == 0 and result["partial"] is True and result["ranked"] is False
    assert [hit["uid"] for hit in result["hits"]] == [
        conv(f"crowd{n:04d}", "x").uid for n in range(3)
    ]
    assert "index order" in err and BROAD_QUERY_HINT in err


def test_cli_uses_the_default_budget_when_none_is_given(cli, tmp_path, capsys, monkeypatch):
    seen = []
    real = Library.search

    def spy(self, query, **kwargs):
        seen.append(kwargs)
        return real(self, query, **kwargs)

    monkeypatch.setattr(Library, "search", spy)
    run_search(tmp_path, capsys, "lumen glyph")
    assert seen[0]["max_seconds"] == DEFAULT_SEARCH_SECONDS and seen[0]["plan"] == "auto"


def test_an_oversized_query_is_bounded_and_says_so_through_the_tool(mcp):
    huge = ("omega " * 17000)[:100000]
    started = time.monotonic()
    result = mcp(SEARCH_TOOL, query=huge, limit=3, max_seconds=2)
    assert time.monotonic() - started < 2.0
    assert result["terms_truncated"] is True and "shortened" in result["notice"]
    assert result["plan"] == "and" and result["total"] == 1000 and len(result["hits"]) == 3
    assert all(hit["read"]["limit"] for hit in result["hits"])
    compact = mcp(SEARCH_TOOL, query=huge, limit=3, depth="L0", compact=True)
    assert compact["tq"] is True and "nt" in compact and "terms_truncated" not in compact
    assert "tq" in library_mcp.COMPACT_KEYS.values()
    plain = mcp(SEARCH_TOOL, query="omega", limit=3)
    assert "terms_truncated" not in plain and "notice" not in plain


HUGE_INT = 10**400


@pytest.mark.parametrize("name", ["max_seconds", "limit", "offset"])
def test_a_huge_integer_argument_is_a_clean_value_error_through_the_tool(mcp, name):
    with pytest.raises(ValueError, match=name):
        mcp(SEARCH_TOOL, query="lumen glyph", **{name: HUGE_INT})


def test_a_huge_integer_over_json_rpc_is_a_tool_error_not_an_internal_error(
    library_path, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(library_mcp, "open_library", lambda root: (tmp_path, Library(library_path)))
    server = CceMcpServer(default_project_root=tmp_path, library_store_root=tmp_path)
    raw = (
        '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"cce_library_search",'
        '"arguments":{"query":"lumen glyph","max_seconds":1' + "0" * 400 + "}}}"
    )
    response = server.handle_request(json.loads(raw))
    assert "error" not in response and response["result"]["isError"] is True
    assert "max_seconds" in response["result"]["content"][0]["text"]
    assert capsys.readouterr().err == ""


def test_the_cli_reports_a_huge_integer_without_a_traceback(cli, tmp_path, capsys):
    with pytest.raises(SystemExit):
        parse("--max-seconds", "1" + "0" * 400)
    assert BAD_BUDGET in capsys.readouterr().err
    for flag, name in (("--limit", "limit"), ("--offset", "offset")):
        args = build_parser().parse_args(
            ["library", "search", "--store-root", str(tmp_path), "lumen glyph", flag, str(HUGE_INT)]
        )
        assert handle_library(args) == 2
        captured = capsys.readouterr()
        assert f"library: {name} must be" in captured.err and captured.out == ""


def test_a_nul_inside_a_quoted_phrase_is_answered_not_an_internal_error(
    library_path, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(library_mcp, "open_library", lambda root: (tmp_path, Library(library_path)))
    server = CceMcpServer(default_project_root=tmp_path, library_store_root=tmp_path)
    raw = (
        '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"cce_library_search",'
        '"arguments":{"query":"\\"lumen\\u0000glyph\\""}}}'
    )
    response = server.handle_request(json.loads(raw))
    assert "error" not in response and response["result"]["isError"] is False
    assert response["result"]["structuredContent"]["total"] == 1
    assert capsys.readouterr().err == ""


def test_the_cli_answers_a_query_with_a_nul(cli, tmp_path, capsys):
    code, result, err = run_search(tmp_path, capsys, '"lumen\x00glyph"')
    assert code == 0 and result["total"] == 1 and err == ""
