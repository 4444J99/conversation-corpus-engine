"""`cce library read` on the command line takes what the MCP read tool takes, and refuses what it
refuses; the MCP search tool refuses a wing its own schema does not list; and `verify-export` can
check an `export-cards` folder. Every record here is synthetic.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from conversation_corpus_engine import library_commands, library_mcp
from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.cli import build_parser
from conversation_corpus_engine.library_cards import CARDS_MANIFEST, export_cards
from conversation_corpus_engine.library_commands import handle_library
from conversation_corpus_engine.library_mcp import READ_TOOL, SEARCH_TOOL
from conversation_corpus_engine.library_store import Library
from conversation_corpus_engine.mcp_server import CceMcpServer

TEXTS = [
    "# Alpha\nintro words\n## Beta\nmore words",
    "second message with the keyword zebra inside",
    "# Gamma\nthird message",
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    database = tmp_path / "library.sqlite"
    item = Conversation(
        provider="chatgpt",
        source_id="parity",
        title="Parity",
        messages=[Message(role="user", text=text) for text in TEXTS],
    )
    with Library.create(database) as library:
        library.put(item)

    def opener(root):
        return tmp_path, Library(database)

    monkeypatch.setattr(library_mcp, "open_library", opener)
    monkeypatch.setattr(library_commands, "open_library", opener)
    return SimpleNamespace(uid=item.uid, store=tmp_path, database=database)


def cli_read(env, capsys, *flags: str) -> tuple[int, str, str]:
    args = build_parser().parse_args(
        ["library", "read", env.uid, "--store-root", str(env.store), *flags]
    )
    code = handle_library(args)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def mcp_read(env, **arguments: Any) -> dict[str, Any]:
    return library_mcp.call_tool(env.store, READ_TOOL, {"uid": env.uid, **arguments})


# --- (a) the options the MCP tool has ---------------------------------------------------------

ACCEPTED = {
    "toc": (["--toc"], {"toc": True}),
    "toc-by-message": (
        ["--toc", "--toc-mode", "messages"],
        {"toc": True, "toc_mode": "messages"},
    ),
    "toc-headings-only": (
        ["--toc", "--toc-mode", "headings"],
        {"toc": True, "toc_mode": "headings"},
    ),
    "toc-paged": (
        ["--toc", "--toc-offset", "1", "--toc-limit", "2"],
        {"toc": True, "toc_offset": 1, "toc_limit": 2},
    ),
    "around-a-query": (["--around-query", "zebra"], {"around_query": "zebra"}),
    "around-a-query-from-a-message": (
        ["--around-query", "third", "--message", "1"],
        {"around_query": "third", "message": 1},
    ),
    "message-range": (
        ["--message", "0", "--message-end", "2", "--limit", "200"],
        {"message": 0, "message_end": 2, "limit": 200},
    ),
    "message-range-to-the-end": (
        ["--message-end", "3"],
        {"message_end": 3},
    ),
}


@pytest.mark.parametrize("name", sorted(ACCEPTED))
def test_the_command_line_returns_what_the_mcp_tool_returns(env, capsys, name) -> None:
    flags, arguments = ACCEPTED[name]
    code, out, err = cli_read(env, capsys, *flags)
    assert code == 0, err
    expected = mcp_read(env, **arguments)
    expected.pop("generation")  # the tool names the generation it read; the command does not
    assert json.loads(out) == expected


def test_the_new_options_default_to_off() -> None:
    args = build_parser().parse_args(["library", "read", "chatgpt-x"])
    assert args.toc is False
    assert (args.toc_mode, args.toc_offset, args.toc_limit) == (None, None, None)
    assert (args.around_query, args.message_end) == (None, None)


REFUSED = {
    "toc-mode-without-toc": (["--toc-mode", "headings"], {"toc_mode": "headings"}),
    "toc-offset-without-toc": (["--toc-offset", "1"], {"toc_offset": 1}),
    "toc-limit-without-toc": (["--toc-limit", "5"], {"toc_limit": 5}),
    "toc-with-around-query": (
        ["--toc", "--around-query", "zebra"],
        {"toc": True, "around_query": "zebra"},
    ),
    "toc-with-message-end": (["--toc", "--message-end", "2"], {"toc": True, "message_end": 2}),
    "blank-around-query": (["--around-query", "  "], {"around_query": "  "}),
    "message-end-not-after-message": (
        ["--message", "1", "--message-end", "1"],
        {"message": 1, "message_end": 1},
    ),
    "message-end-zero": (["--message-end", "0"], {"message_end": 0}),
    "toc-limit-too-large": (["--toc", "--toc-limit", "501"], {"toc": True, "toc_limit": 501}),
    "toc-limit-zero": (["--toc", "--toc-limit", "0"], {"toc": True, "toc_limit": 0}),
    "toc-offset-negative": (["--toc", "--toc-offset", "-1"], {"toc": True, "toc_offset": -1}),
    "around-query-with-offset": (
        ["--around-query", "zebra", "--offset", "3"],
        {"around_query": "zebra", "offset": 3},
    ),
    "around-query-with-message-end": (
        ["--around-query", "zebra", "--message-end", "3"],
        {"around_query": "zebra", "message_end": 3},
    ),
    "message-out-of-range": (["--message", "10000001"], {"message": 10000001}),
    # the tool checks offset and limit of every read, a table of contents included
    "toc-with-limit-zero": (["--toc", "--limit", "0"], {"toc": True, "limit": 0}),
    "toc-with-negative-offset": (["--toc", "--offset", "-1"], {"toc": True, "offset": -1}),
    "offset-over-the-bound": (["--offset", "2000000000"], {"offset": 2000000000}),
    "limit-over-the-bound": (["--limit", "32001"], {"limit": 32001}),
}


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_the_command_line_refuses_what_the_mcp_tool_refuses_in_the_same_words(
    env, capsys, name
) -> None:
    flags, arguments = REFUSED[name]
    with pytest.raises(ValueError) as refusal:
        mcp_read(env, **arguments)
    code, out, err = cli_read(env, capsys, *flags)
    assert code == 2 and out == ""
    assert err.strip() == f"library: {refusal.value}"


def test_an_empty_uid_is_refused_in_the_same_words(env, capsys) -> None:
    with pytest.raises(ValueError) as refusal:
        library_mcp.call_tool(env.store, READ_TOOL, {"uid": " "})
    args = build_parser().parse_args(["library", "read", " ", "--store-root", str(env.store)])
    assert handle_library(args) == 2
    assert capsys.readouterr().err.strip() == f"library: {refusal.value}"


def test_an_unknown_toc_mode_is_refused_on_both(env) -> None:
    with pytest.raises(ValueError, match="toc_mode"):
        mcp_read(env, toc=True, toc_mode="bogus")
    with pytest.raises(SystemExit):
        build_parser().parse_args(["library", "read", env.uid, "--toc", "--toc-mode", "bogus"])


# --- (b) the description no longer says read is one message only ------------------------------


def test_the_tool_description_says_read_can_span_messages_and_list_headings() -> None:
    descriptions = {item["name"]: item["description"] for item in library_mcp.tool_definitions()}
    for text in descriptions.values():
        assert "character offsets within one message" not in text
        assert "message_end" in text and "toc" in text


# --- (c) an enum the schema lists is enforced --------------------------------------------------


def test_the_search_tool_refuses_a_wing_its_schema_does_not_list(env) -> None:
    with pytest.raises(ValueError, match="wing must be ai or authored"):
        library_mcp.call_tool(env.store, SEARCH_TOOL, {"query": "zebra", "wing": "NOPE"})
    with pytest.raises(ValueError, match="wing must be a string"):
        library_mcp.call_tool(env.store, SEARCH_TOOL, {"query": "zebra", "wing": 5})
    found = library_mcp.call_tool(env.store, SEARCH_TOOL, {"query": "zebra", "wing": "ai"})
    assert found["total"] == 1


def test_a_refused_wing_is_a_tool_error_through_the_server(env, tmp_path) -> None:
    server = CceMcpServer(default_project_root=tmp_path, library_store_root=tmp_path)
    result = server.call_tool(SEARCH_TOOL, {"query": "zebra", "wing": "NOPE"})
    assert result["isError"] is True
    assert "wing must be ai or authored" in result["content"][0]["text"]


def test_every_enum_of_the_schema_is_enforced_by_the_tool(env) -> None:
    """Whatever the schema lists as an enum, the tool must refuse a value outside it."""
    refused = {
        (SEARCH_TOOL, "wing"): {"query": "zebra", "wing": "NOPE"},
        (SEARCH_TOOL, "plan"): {"query": "zebra", "plan": "NOPE"},
        (SEARCH_TOOL, "depth"): {"query": "zebra", "depth": "L9"},
        (READ_TOOL, "sequence"): {"uid": env.uid, "sequence": "NOPE"},
        (READ_TOOL, "toc_mode"): {"uid": env.uid, "toc": True, "toc_mode": "NOPE"},
    }
    listed = {
        (item["name"], name)
        for item in library_mcp.tool_definitions()
        for name, schema in item["inputSchema"]["properties"].items()
        if "enum" in schema
    }
    assert listed == set(refused), "a new enum needs a case here"
    for (tool, _), arguments in refused.items():
        with pytest.raises(ValueError):
            library_mcp.call_tool(env.store, tool, arguments)


# --- (d) verify-export checks an export-cards folder -------------------------------------------


def exported_cards(env, tmp_path):
    out = tmp_path / "cards-out"
    with Library(env.database) as library:
        export_cards(library, out)
    return out


def verify_cards(capsys, directory) -> tuple[int, dict[str, Any]]:
    args = build_parser().parse_args(["library", "verify-export", str(directory)])
    code = handle_library(args)
    captured = capsys.readouterr()
    return code, json.loads(captured.out)


def test_verify_export_accepts_a_fresh_card_folder(env, tmp_path, capsys) -> None:
    out = exported_cards(env, tmp_path)
    code, report = verify_cards(capsys, out)
    assert code == 0 and report["valid"] is True and report["problems"] == []
    assert report["kind"] == "cards" and report["cards"] >= 1 and report["unlisted"] == 0


def test_verify_export_reports_an_edited_and_a_deleted_card(env, tmp_path, capsys) -> None:
    out = exported_cards(env, tmp_path)
    manifest = json.loads((out / CARDS_MANIFEST).read_text(encoding="utf-8"))
    listed = [entry["path"] for entry in manifest["files"]]
    assert len(listed) >= 2
    edited, deleted = out / listed[0], out / listed[1]
    edited.write_text(edited.read_text(encoding="utf-8") + "\nan edit\n", encoding="utf-8")
    deleted.unlink()
    code, report = verify_cards(capsys, out)
    assert code == 1 and report["valid"] is False
    assert any("edited since export" in p and listed[0] in p for p in report["problems"])
    assert any("missing card file" in p and listed[1] in p for p in report["problems"])


def test_verify_export_counts_a_persons_own_files_without_failing_them(
    env, tmp_path, capsys
) -> None:
    out = exported_cards(env, tmp_path)
    (out / "my-notes.md").write_text("the person's own note\n", encoding="utf-8")
    code, report = verify_cards(capsys, out)
    assert code == 0 and report["unlisted"] == 1


def test_verify_export_runs_the_card_folder_through_the_vault_caps(env, tmp_path, capsys) -> None:
    out = exported_cards(env, tmp_path)
    for number in range(1100):  # more entries in one folder than the hard cap allows
        (out / f"own-{number:04d}.md").write_text("x\n", encoding="utf-8")
    code, report = verify_cards(capsys, out)
    assert code == 1
    assert any(problem.startswith("vault ") for problem in report["problems"])


def test_verify_export_reports_a_folder_with_a_marker_and_no_manifest(
    env, tmp_path, capsys
) -> None:
    out = exported_cards(env, tmp_path)
    (out / CARDS_MANIFEST).unlink()
    code, report = verify_cards(capsys, out)
    assert code == 1 and report["kind"] == "cards"
    assert any("card manifest missing" in problem for problem in report["problems"])


def test_verify_export_reports_a_malformed_card_manifest(env, tmp_path, capsys) -> None:
    out = exported_cards(env, tmp_path)
    (out / CARDS_MANIFEST).write_text("{not json", encoding="utf-8")
    code, report = verify_cards(capsys, out)
    assert code == 1 and any("card manifest" in problem for problem in report["problems"])


def test_verify_export_still_says_what_it_always_said_about_any_other_folder(
    tmp_path, capsys
) -> None:
    (tmp_path / "plain").mkdir()
    args = build_parser().parse_args(["library", "verify-export", str(tmp_path / "plain")])
    assert handle_library(args) == 1
    report = json.loads(capsys.readouterr().out)
    (problem,) = report["problems"]
    assert problem.startswith("export manifest missing or invalid")
    assert "kind" not in report
