from __future__ import annotations

import json
from pathlib import Path

import pytest

from conversation_corpus_engine.archive_ingest import ingest
from conversation_corpus_engine.archive_kb import build_kb
from conversation_corpus_engine.archive_topics import build_analysis
from conversation_corpus_engine.mcp_server import ARCHIVE_TOOL, CceMcpServer, build_parser


@pytest.fixture
def archive_server(archive_env, tmp_path: Path):
    ingest(export_root=archive_env.export, paths=archive_env.paths, authorization=archive_env.auth)
    build_analysis(archive_env.paths)
    build_kb(archive_env.paths, archive_env.auth)
    return CceMcpServer(default_project_root=tmp_path, archive_root=archive_env.paths.root)


def _call(server: CceMcpServer, **arguments):
    response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": ARCHIVE_TOOL, "arguments": arguments},
        }
    )
    assert response is not None
    return response


def test_archive_tool_is_advertised_only_when_configured(tmp_path: Path, archive_server) -> None:
    plain = CceMcpServer(default_project_root=tmp_path)
    assert ARCHIVE_TOOL not in {tool["name"] for tool in plain.tool_definitions()}
    assert len(plain.tool_definitions()) == 3
    names = {tool["name"] for tool in archive_server.tool_definitions()}
    assert ARCHIVE_TOOL in names and len(names) == 4
    schema = next(t for t in archive_server.tool_definitions() if t["name"] == ARCHIVE_TOOL)
    assert schema["inputSchema"]["required"] == ["query"]
    assert "archive_root" not in schema["inputSchema"]["properties"]


def test_unconfigured_server_rejects_the_archive_tool(tmp_path: Path) -> None:
    response = _call(CceMcpServer(default_project_root=tmp_path), query="x")
    assert response["error"]["message"] == f"Unknown tool: {ARCHIVE_TOOL}"


def test_archive_search_returns_structured_hits(archive_server) -> None:
    result = _call(archive_server, query="kubernetes cluster", limit=5)["result"]
    assert result["isError"] is False
    structured = result["structuredContent"]
    assert structured["count"] == len(structured["results"]) >= 2
    assert {"chatgpt", "claude"} <= {hit["provider"] for hit in structured["results"]}
    assert "[" in result["content"][0]["text"]
    filtered = _call(archive_server, query="sourdough", provider=["claude"])["result"]
    assert {h["provider"] for h in filtered["structuredContent"]["results"]} == {"claude"}
    titles = _call(archive_server, query="rollout", titles_only=True)["result"]
    assert [h["uid"] for h in titles["structuredContent"]["results"]] == ["chatgpt-aaaa1111"]
    empty = _call(archive_server, query="zzzunmatchable")["result"]
    assert empty["content"][0]["text"] == "No matching conversations."


def test_archive_search_validates_arguments_and_caps_limit(archive_server) -> None:
    for arguments in (
        {},
        {"query": "  "},
        {"query": "x", "limit": 0},
        {"query": "x", "limit": "many"},
        {"query": "x", "provider": "claude"},
        {"query": "x", "kind": [1]},
        {"query": "x", "titles_only": "yes"},
    ):
        result = _call(archive_server, **arguments)["result"]
        assert result["isError"] is True, arguments
    big = _call(archive_server, query="kubernetes", limit=9999)["result"]
    assert big["structuredContent"]["count"] <= 25


def test_archive_search_reports_missing_knowledge_base(archive_env, tmp_path: Path) -> None:
    server = CceMcpServer(default_project_root=tmp_path, archive_root=archive_env.paths.root)
    result = _call(server, query="x")["result"]
    assert result["isError"] is True and "not built" in result["content"][0]["text"]


def test_archive_root_comes_from_server_configuration_only(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CCE_ARCHIVE_ROOT", str(tmp_path / "archive"))
    assert build_parser().parse_args([]).archive_root == tmp_path / "archive"
    monkeypatch.delenv("CCE_ARCHIVE_ROOT")
    assert build_parser().parse_args([]).archive_root is None
    assert build_parser().parse_args(["--archive-root", "/x"]).archive_root == Path("/x")


def test_stdio_round_trip_lists_the_archive_tool(archive_server) -> None:
    response = archive_server.handle_request({"jsonrpc": "2.0", "id": 7, "method": "tools/list"})
    assert response is not None
    names = [tool["name"] for tool in response["result"]["tools"]]
    assert names[-1] == ARCHIVE_TOOL
    assert json.dumps(response)
