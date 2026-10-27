import pytest

from conversation_corpus_engine import library_mcp
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


def test_searchable_alternate_branch_is_readable_in_bounded_spans(tmp_path):
    with Library.create(tmp_path / "library.sqlite") as library:
        item = Conversation(
            provider="chatgpt",
            source_id="branches",
            title="Branches",
            messages=[Message(role="user", text="Main branch")],
            extra={
                "alternate_messages": [
                    {"role": "assistant", "text": "alternateword and details"},
                    {"role": "assistant", "text": "second alternate"},
                ]
            },
        )
        library.put(item)
        assert library.search("alternateword")["total"] == 1
        first = read_record(library, item.uid, sequence="alternate", limit=13)
        assert first["text"] == "alternateword"
        assert first["sequence"] == "alternate" and first["alternate_message_count"] == 2
        assert first["next_offset"] == 13
        second = read_record(library, item.uid, sequence="alternate", offset=13)
        assert second["next_message"] == 1
        assert read_record(library, item.uid)["text"] == "Main branch"
        with pytest.raises(ValueError, match="sequence"):
            read_record(library, item.uid, sequence="other")


def test_missing_library_record_is_mcp_tool_error(tmp_path, monkeypatch):
    def missing(*args, **kwargs):
        raise KeyError("missing uid")

    monkeypatch.setattr(library_mcp, "call_tool", missing)
    server = CceMcpServer(default_project_root=tmp_path, library_store_root=tmp_path)
    result = server.call_tool(READ_TOOL, {"uid": "chatgpt-missing"})
    assert result["isError"] is True
    assert "not found" in result["content"][0]["text"]
    with pytest.raises(KeyError):
        server.call_tool("unknown-tool", {})


def test_search_rejects_offset_outside_sqlite_integer_range(tmp_path):
    with (
        Library.create(tmp_path / "library.sqlite") as library,
        pytest.raises(ValueError, match="offset"),
    ):
        library.search("lattice", offset=10**100)


def test_mcp_and_cli_expose_alternate_sequence_selector(tmp_path, monkeypatch):
    path = tmp_path / "library.sqlite"
    with Library.create(path) as library:
        item = Conversation(
            provider="chatgpt",
            source_id="alternate",
            title="Alternate",
            extra={"alternate_messages": [{"role": "assistant", "text": "alternate evidence"}]},
        )
        library.put(item)
    monkeypatch.setattr(library_mcp, "open_library", lambda root: (tmp_path, Library(path)))
    result = library_mcp.call_tool(
        tmp_path, READ_TOOL, {"uid": item.uid, "sequence": "alternate", "limit": 9}
    )
    assert result["text"] == "alternate" and result["next_offset"] == 9
    args = build_parser().parse_args(["library", "read", item.uid, "--sequence", "alternate"])
    assert args.sequence == "alternate"
    with pytest.raises(ValueError, match="sequence"):
        library_mcp.call_tool(tmp_path, READ_TOOL, {"uid": item.uid, "sequence": []})
