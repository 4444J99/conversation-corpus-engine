import pytest

from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.cli import build_parser
from conversation_corpus_engine.library_commands import handle_library, read_record
from conversation_corpus_engine.library_mcp import READ_TOOL, SEARCH_TOOL
from conversation_corpus_engine.library_store import Library
from conversation_corpus_engine.mcp_server import CceMcpServer


def test_parser_and_bounded_read(tmp_path):
    args = build_parser().parse_args(
        [
            "library",
            "search",
            "lattice",
            "--store-root",
            str(tmp_path),
            "--offset",
            "100",
            "--limit",
            "50",
        ]
    )
    assert args.group == "library" and args.offset == 100 and args.limit == 50
    with Library.create(tmp_path / "library.sqlite") as library:
        item = Conversation(
            provider="chatgpt",
            source_id="one",
            title="One",
            messages=[
                Message(role="user", text="abcdefghij"),
                Message(role="assistant", text="second"),
            ],
        )
        library.put(item)
        a = read_record(library, item.uid, limit=4)
        assert a["text"] == "abcd" and a["next_offset"] == 4
        b = read_record(library, item.uid, offset=4, limit=6)
        assert b["text"] == "efghij" and b["next_message"] == 1
        assert read_record(library, item.uid, message=1)["text"] == "second"
        with pytest.raises(ValueError):
            read_record(library, item.uid, limit=1000000)


def test_mcp_only_exposes_library_when_configured_and_rejects_paths(tmp_path):
    plain = CceMcpServer(default_project_root=tmp_path)
    assert SEARCH_TOOL not in {tool["name"] for tool in plain.tool_definitions()}
    configured = CceMcpServer(default_project_root=tmp_path, library_store_root=tmp_path)
    tools = {tool["name"] for tool in configured.tool_definitions()}
    assert SEARCH_TOOL in tools and READ_TOOL in tools
    result = configured.call_tool(SEARCH_TOOL, {"query": "lattice", "store_root": "/etc"})
    assert result["isError"] is True and "unsupported" in result["content"][0]["text"]
    result = configured.call_tool(SEARCH_TOOL, {"query": "lattice", "limit": 1000000})
    assert result["isError"] is True


def test_missing_library_cli_is_nonzero(tmp_path, capsys):
    args = build_parser().parse_args(["library", "status", "--store-root", str(tmp_path)])
    assert handle_library(args) == 2
    assert "no library generation" in capsys.readouterr().err
