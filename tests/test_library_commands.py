import json
import subprocess

import pytest

from conversation_corpus_engine import library_commands, library_mcp
from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.cli import build_parser
from conversation_corpus_engine.corpus_store import CorpusStoreError, register_corpus_store
from conversation_corpus_engine.library_commands import handle_library, read_record
from conversation_corpus_engine.library_export import export_library
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


def test_analysis_cli_writes_private_payloads_and_prints_only_receipt(
    tmp_path, monkeypatch, capsys
):
    project, store = tmp_path / "project", tmp_path / "store"
    project.mkdir()
    store.mkdir()
    register_corpus_store(project, store)
    database = store / "library.sqlite"
    original = "Leading prose about lattice research must reach both native consumers."
    with Library.create(database) as library:
        library.put(
            Conversation(
                provider="chatgpt",
                source_id="analysis-cli",
                title="Lattice source",
                messages=[Message(role="user", text=original)],
            )
        )
    monkeypatch.setattr(library_commands, "open_library", lambda root: (store, Library(database)))
    output = store / "derived" / "analysis"
    args = build_parser().parse_args(
        [
            "library",
            "analyze",
            "lattice",
            "--output",
            str(output),
            "--project-root",
            str(project),
            "--store-root",
            str(store),
            "--limit",
            "1",
        ]
    )
    assert handle_library(args) == 0
    captured = capsys.readouterr()
    assert original not in captured.out
    assert isinstance(json.loads(captured.out), dict)
    for name in ("lingframe.json", "uaks.json", "receipt.json"):
        assert (output / name).is_file()
        assert (output / name).stat().st_mode & 0o777 == 0o600
    assert original in (output / "lingframe.json").read_text()
    assert original in (output / "uaks.json").read_text()


def test_analysis_cli_requires_one_selection_and_explicit_output(tmp_path):
    parser = build_parser()
    for options in (
        ["--output", str(tmp_path)],
        ["lattice", "--uid", "chatgpt-one", "--output", str(tmp_path)],
        ["lattice"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(["library", "analyze", *options])


# --- the private reading export must not land where Git can see it -------------------------------


def _git(cwd, *argv):
    subprocess.run(["git", "-C", str(cwd), *argv], check=True, capture_output=True)


def _private_library(tmp_path):
    database = tmp_path / "library.sqlite"
    with Library.create(database) as library:
        library.put(
            Conversation(
                provider="chatgpt",
                source_id="export-guard",
                title="Export guard",
                messages=[Message(role="user", text="Plaintext that must stay private.")],
            )
        )
    return database


def _export_command(tmp_path, out, *extra):
    return build_parser().parse_args(
        ["library", "export", "--store-root", str(tmp_path), "--out", str(out), *extra]
    )


def test_export_refuses_a_git_visible_destination_before_reading_or_writing(
    tmp_path, monkeypatch, capsys
):
    database = _private_library(tmp_path)
    work_tree = tmp_path / "work"
    work_tree.mkdir()
    _git(work_tree, "init", "-q")
    reviewed = tmp_path / "reviewed.json"
    reviewed.write_text("{}", encoding="utf-8")

    def opened(root):
        raise AssertionError("the library was opened before the destination was authorised")

    def loaded(path):
        raise AssertionError("the envelope was read before the destination was authorised")

    monkeypatch.setattr(library_commands, "open_library", opened)
    monkeypatch.setattr(library_commands, "_load_zettel_envelope", loaded)
    visible = work_tree / "visible"
    for extra in ((), ("--zettels", str(reviewed))):
        assert handle_library(_export_command(tmp_path, visible, *extra)) == 2
        assert "Git-visible" in capsys.readouterr().err
        assert not visible.exists()
    assert sorted(path.name for path in work_tree.iterdir()) == [".git"]

    monkeypatch.undo()
    monkeypatch.setattr(
        library_commands, "open_library", lambda root: (tmp_path, Library(database))
    )
    (work_tree / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    assert handle_library(_export_command(tmp_path, work_tree / "ignored")) == 0
    assert json.loads(capsys.readouterr().out)["records"] == 1
    assert (work_tree / "ignored" / "vault" / "Home.md").is_file()


def test_export_to_a_directory_outside_every_work_tree_is_unchanged(tmp_path, monkeypatch, capsys):
    database = _private_library(tmp_path)
    monkeypatch.setattr(
        library_commands, "open_library", lambda root: (tmp_path, Library(database))
    )
    assert handle_library(_export_command(tmp_path, tmp_path / "plain" / "export")) == 0
    assert json.loads(capsys.readouterr().out)["files"] > 0


def test_the_export_api_refuses_a_git_visible_destination_and_writes_nothing(tmp_path):
    database = _private_library(tmp_path)
    work_tree = tmp_path / "work"
    work_tree.mkdir()
    _git(work_tree, "init", "-q")
    with Library(database) as library, pytest.raises(CorpusStoreError, match="Git-visible"):
        export_library(library, work_tree / "visible")
    assert sorted(path.name for path in work_tree.iterdir()) == [".git"]


def test_verify_export_writes_nothing_so_it_needs_no_custody_check(tmp_path, monkeypatch, capsys):
    database = _private_library(tmp_path)
    monkeypatch.setattr(
        library_commands, "open_library", lambda root: (tmp_path, Library(database))
    )
    export = tmp_path / "plain-export"
    assert handle_library(_export_command(tmp_path, export)) == 0
    capsys.readouterr()

    def tree(root):
        return {
            str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in sorted(root.rglob("*"))
        }

    before = tree(export)
    args = build_parser().parse_args(["library", "verify-export", str(export)])
    assert handle_library(args) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True
    assert tree(export) == before
