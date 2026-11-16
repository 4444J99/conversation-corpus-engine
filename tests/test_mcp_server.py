from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conversation_corpus_engine.mcp_server import CceMcpServer


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _seed_searchable_corpus(root: Path) -> Path:
    corpus_dir = root / "corpus"
    _write_json(
        corpus_dir / "threads-index.json",
        [
            {
                "thread_uid": "thread-alpha",
                "title_normalized": "Alpha Registry Doctrine",
                "semantic_summary": "Alpha Registry Doctrine tracks registry governance.",
                "semantic_v3_summary": "The registry is governed through executable pair work.",
                "semantic_v3_themes": ["alpha", "registry", "governance"],
                "semantic_v3_entities": ["Alpha Engine"],
                "family_ids": ["family-alpha"],
                "vector_terms": {"alpha": 1.0, "registry": 1.0, "governance": 0.8},
            }
        ],
    )
    _write_json(
        corpus_dir / "semantic-v3-index.json",
        {
            "threads": [
                {
                    "thread_uid": "thread-alpha",
                    "title": "Alpha Registry Doctrine",
                    "summary": "Alpha semantic summary",
                    "search_text": "Alpha Registry Doctrine alpha registry governance pair execution",
                    "family_ids": ["family-alpha"],
                    "vector_terms": {"alpha": 1.0, "registry": 1.0, "governance": 0.8},
                }
            ]
        },
    )
    _write_json(
        corpus_dir / "pairs-index.json",
        [
            {
                "pair_id": "pair-alpha-001",
                "thread_uid": "thread-alpha",
                "title": "Alpha Registry Doctrine pair",
                "summary": "Pair translates doctrine into implementation.",
                "search_text": "Alpha pair implements registry doctrine and stabilizes governance",
                "vector_terms": {"pair": 1.0, "registry": 0.8, "implement": 0.7},
                "family_ids": ["family-alpha"],
            }
        ],
    )
    _write_json(
        corpus_dir / "doctrine-briefs.json",
        [
            {
                "family_id": "family-alpha",
                "canonical_title": "Alpha Registry Doctrine",
                "canonical_thread_uid": "thread-alpha",
                "member_count": 1,
                "stable_themes": ["alpha", "registry", "governance"],
                "brief_text": "Alpha Registry Doctrine governs the registry through executable ritual.",
                "search_text": "Alpha Registry Doctrine alpha registry governance executable ritual",
                "vector_terms": {"alpha": 1.0, "registry": 0.9, "governance": 0.9},
            }
        ],
    )
    _write_json(
        corpus_dir / "family-dossiers.json",
        [
            {
                "family_id": "family-alpha",
                "canonical_title": "Alpha Registry Doctrine",
                "canonical_thread_uid": "thread-alpha",
                "member_count": 1,
                "stable_themes": ["alpha", "registry", "governance"],
                "doctrine_summary": "Alpha Registry Doctrine keeps the registry coherent.",
                "search_text": "Alpha Registry Doctrine registry coherence governance",
                "actions": [
                    {
                        "action_key": "action-alpha",
                        "canonical_action": "Implement alpha registry ritual",
                    }
                ],
                "unresolved": [],
                "key_entities": [{"canonical_label": "Alpha Engine", "entity_type": "concept"}],
                "vector_terms": {"registry": 1.0, "governance": 0.85, "ritual": 0.7},
            }
        ],
    )
    _write_json(
        corpus_dir / "canonical-families.json",
        [
            {
                "canonical_family_id": "family-alpha",
                "canonical_title": "Alpha Registry Doctrine",
                "canonical_thread_uid": "thread-alpha",
                "thread_uids": ["thread-alpha"],
            }
        ],
    )
    return root


def test_initialize_declares_tools_capability(tmp_path: Path) -> None:
    server = CceMcpServer(default_project_root=tmp_path)

    response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        }
    )

    assert response is not None
    assert response["result"]["protocolVersion"] == "2025-06-18"
    assert response["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert response["result"]["serverInfo"]["name"] == "conversation-corpus-engine"


def test_tools_list_includes_corpus_search_tool(tmp_path: Path) -> None:
    server = CceMcpServer(default_project_root=tmp_path)

    response = server.handle_request({"jsonrpc": "2.0", "id": "tools", "method": "tools/list"})

    assert response is not None
    tool_names = {tool["name"] for tool in response["result"]["tools"]}
    assert {"cce_search", "cce_list_corpora", "cce_surface_context"} <= tool_names


def test_search_tool_returns_grounded_answer_with_structured_content(tmp_path: Path) -> None:
    corpus_root = _seed_searchable_corpus(tmp_path / "alpha-corpus")
    server = CceMcpServer(default_project_root=tmp_path)

    result = server.call_tool(
        "cce_search",
        {
            "query": "Alpha Registry Doctrine",
            "corpus_root": str(corpus_root),
            "limit": 4,
        },
    )

    assert result["isError"] is False
    assert result["content"][0]["type"] == "text"
    assert "Alpha Registry Doctrine" in result["content"][0]["text"]
    assert result["structuredContent"]["answer"]["answer_state"] == "grounded"
    assert result["structuredContent"]["answer"]["citations"]
    assert result["structuredContent"]["hits"][0]["doc_id"]
    assert result["structuredContent"]["hits"][0]["score"] > 0


def test_tools_call_reports_unknown_tool_as_protocol_error(tmp_path: Path) -> None:
    server = CceMcpServer(default_project_root=tmp_path)

    response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "missing_tool", "arguments": {}},
        }
    )

    assert response is not None
    assert response["error"]["code"] == -32602
    assert response["error"]["message"] == "Unknown tool: missing_tool"
