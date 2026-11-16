from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conversation_corpus_engine import import_chatgpt_local_session_corpus as module  # noqa: E402
from conversation_corpus_engine.sharded_collection import (  # noqa: E402
    collection_storage_path,
    load_collection,
)


def test_write_local_session_bundle_writes_expected_files(tmp_path: Path) -> None:
    bundle_root = tmp_path / "bundle"
    module.write_local_session_bundle(
        bundle_root,
        {
            "user": {"id": "acct-1", "email": "user@example.com"},
            "conversations": [
                {
                    "conversation_id": "conv-1",
                    "title": "Test Chat",
                    "create_time": 1711900000,
                    "update_time": 1711900100,
                    "mapping": {
                        "root": {
                            "id": "root",
                            "parent": None,
                            "children": ["msg-1"],
                            "message": None,
                        },
                        "msg-1": {
                            "id": "msg-1",
                            "parent": "root",
                            "children": ["msg-2"],
                            "message": {
                                "id": "msg-1",
                                "author": {"role": "user"},
                                "create_time": 1711900000,
                                "content": {"content_type": "text", "parts": ["Hello"]},
                            },
                        },
                        "msg-2": {
                            "id": "msg-2",
                            "parent": "msg-1",
                            "children": [],
                            "message": {
                                "id": "msg-2",
                                "author": {"role": "assistant"},
                                "create_time": 1711900010,
                                "content": {"content_type": "text", "parts": ["Hi there!"]},
                            },
                        },
                    },
                    "current_node": "msg-2",
                }
            ],
            "conversation_summaries": [{"id": "conv-1", "title": "Test Chat"}],
            "conversation_detail_failures": [],
        },
    )

    assert (bundle_root / "user.json").exists()
    assert collection_storage_path(bundle_root / "conversations.json").exists()
    user = json.loads((bundle_root / "user.json").read_text(encoding="utf-8"))
    assert user["email"] == "user@example.com"
    conversations = load_collection(bundle_root / "conversations.json")
    assert len(conversations) == 1
    assert conversations[0]["conversation_id"] == "conv-1"


def test_patch_contract_for_local_session_updates_contract_and_notes(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "output"
    cookie_jar = tmp_path / "cookies" / "com.openai.chat.binarycookies"
    cookie_jar.parent.mkdir(parents=True, exist_ok=True)
    cookie_jar.write_bytes(b"cook" + b"\x00" * 100)

    corpus_dir = output_root / "corpus"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    (corpus_dir / "contract.json").write_text(
        json.dumps({"corpus_id": "chatgpt-local-session-memory"}),
        encoding="utf-8",
    )
    (corpus_dir / "evaluation-summary.json").write_text(
        json.dumps({"notes": ["placeholder"]}), encoding="utf-8"
    )
    (corpus_dir / "regression-gates.json").write_text(
        json.dumps({"source_notes": ["placeholder"]}), encoding="utf-8"
    )

    module.patch_contract_for_local_session(
        output_root,
        cookie_jar=cookie_jar,
        discovery={
            "generated_at": "2026-03-25T00:00:00+00:00",
            "account_id": "acct-1",
            "account_email": "user@example.com",
            "conversation_count": 42,
        },
    )

    contract = json.loads((corpus_dir / "contract.json").read_text(encoding="utf-8"))
    assert contract["adapter_type"] == "chatgpt-local-session"
    assert contract["collection_scope"] == "local-session"
    assert contract["local_session"]["account_id"] == "acct-1"


def test_import_chatgpt_local_session_corpus_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cookie_jar = tmp_path / "cookies" / "com.openai.chat.binarycookies"
    cookie_jar.parent.mkdir(parents=True, exist_ok=True)
    cookie_jar.write_bytes(b"cook" + b"\x00" * 100)
    output_root = tmp_path / "output"

    discovery = {
        "generated_at": "2026-03-25T00:00:00+00:00",
        "account_id": "acct-1",
        "account_email": "user@example.com",
        "conversation_count": 1,
    }
    bundle = {
        "generated_at": "2026-03-25T00:01:00+00:00",
        "user": {"id": "acct-1", "email": "user@example.com"},
        "conversations": [
            {
                "conversation_id": "conv-1",
                "title": "ChatGPT Local Session Thread",
                "create_time": 1711900000,
                "update_time": 1711900100,
                "mapping": {
                    "root": {
                        "id": "root",
                        "parent": None,
                        "children": ["msg-1"],
                        "message": None,
                    },
                    "msg-1": {
                        "id": "msg-1",
                        "parent": "root",
                        "children": ["msg-2"],
                        "message": {
                            "id": "msg-1",
                            "author": {"role": "user"},
                            "create_time": 1711900000,
                            "content": {"content_type": "text", "parts": ["Test prompt"]},
                        },
                    },
                    "msg-2": {
                        "id": "msg-2",
                        "parent": "msg-1",
                        "children": [],
                        "message": {
                            "id": "msg-2",
                            "author": {"role": "assistant"},
                            "create_time": 1711900010,
                            "content": {
                                "content_type": "text",
                                "parts": ["Test response from the local session."],
                            },
                        },
                    },
                },
                "current_node": "msg-2",
            }
        ],
        "conversation_summaries": [{"id": "conv-1", "title": "ChatGPT Local Session Thread"}],
        "conversation_detail_failures": [],
        "total_count": 1,
        "fetched_count": 1,
    }
    monkeypatch.setattr(module, "discover_chatgpt_local_session", lambda cookie_jar: discovery)
    monkeypatch.setattr(
        module,
        "fetch_chatgpt_local_session_bundle",
        lambda cookie_jar, limit=100, offset=0, prior_state=None, output_root=None: bundle,
    )

    result = module.import_chatgpt_local_session_corpus(cookie_jar, output_root)

    assert result["source_type"] == "chatgpt-local-session"
    contract = json.loads((output_root / "corpus" / "contract.json").read_text(encoding="utf-8"))
    assert contract["adapter_type"] == "chatgpt-local-session"
    discovery_file = json.loads(
        (output_root / "source" / "local-session-discovery.json").read_text(encoding="utf-8")
    )
    assert discovery_file["account_id"] == "acct-1"
    readme = (output_root / "README.md").read_text(encoding="utf-8")
    assert "ChatGPT Local Session Memory Corpus" in readme


def test_import_chatgpt_local_session_corpus_saves_acquisition_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cookie_jar = tmp_path / "cookies" / "com.openai.chat.binarycookies"
    cookie_jar.parent.mkdir(parents=True, exist_ok=True)
    cookie_jar.write_bytes(b"cook" + b"\x00" * 100)
    output_root = tmp_path / "output"

    discovery = {
        "generated_at": "2026-03-30T00:00:00+00:00",
        "account_id": "acct-1",
        "account_email": "user@example.com",
        "conversation_count": 1,
    }
    bundle = {
        "generated_at": "2026-03-30T00:01:00+00:00",
        "user": {"id": "acct-1", "email": "user@example.com"},
        "conversations": [
            {
                "conversation_id": "conv-1",
                "title": "Incremental Test",
                "create_time": 1711900000,
                "update_time": 1711900100,
                "mapping": {
                    "root": {"id": "root", "parent": None, "children": ["msg-1"], "message": None},
                    "msg-1": {
                        "id": "msg-1",
                        "parent": "root",
                        "children": [],
                        "message": {
                            "id": "msg-1",
                            "author": {"role": "user"},
                            "create_time": 1711900000,
                            "content": {"content_type": "text", "parts": ["Test"]},
                        },
                    },
                },
                "current_node": "msg-1",
            }
        ],
        "conversation_summaries": [{"id": "conv-1"}],
        "conversation_detail_failures": [],
        "total_count": 1,
        "fetched_count": 1,
        "reused_count": 0,
        "acquisition_report": {
            "generated_at": "2026-03-30T00:01:00+00:00",
            "total_listed": 1,
            "fetched_count": 1,
            "reused_count": 0,
            "skipped_count": 0,
            "failure_count": 0,
            "full_refresh": True,
        },
    }
    monkeypatch.setattr(module, "discover_chatgpt_local_session", lambda cookie_jar: discovery)
    monkeypatch.setattr(
        module,
        "fetch_chatgpt_local_session_bundle",
        lambda cookie_jar, limit=100, offset=0, prior_state=None, output_root=None: bundle,
    )

    result = module.import_chatgpt_local_session_corpus(cookie_jar, output_root)

    assert result["acquisition_report"]["fetched_count"] == 1
    assert result["acquisition_report"]["full_refresh"] is True

    state_path = output_root / "source" / "acquisition-state.json"
    assert state_path.exists()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert "conv-1" in state["conversations"]
    assert state["conversations"]["conv-1"]["update_time"] == 1711900100
