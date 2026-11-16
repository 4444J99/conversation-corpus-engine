from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conversation_corpus_engine import perplexity_local_session as module  # noqa: E402
from conversation_corpus_engine.perplexity_local_session import (  # noqa: E402
    PerplexityHttpSession,
    PerplexityLocalSessionError,
)

# Import the shared binary-cookie-jar builder from the ChatGPT adapter's tests.
from tests.test_chatgpt_local_session import build_binary_cookie_jar  # noqa: E402

# ---------------------------------------------------------------------------
# Fixture: the real step-block shape (INITIAL_QUERY, SEARCH_WEB, FINAL).
# The `entries[i].text` field is a JSON *string* of a list of step blocks;
# the FINAL block's `content.answer` is itself a JSON string {"answer": "..."}.
# ---------------------------------------------------------------------------


def _entry_text() -> str:
    steps = [
        {"step_type": "INITIAL_QUERY", "content": {"query": "What is the capital of France?"}},
        {"step_type": "SEARCH_WEB", "content": {"queries": ["capital of France"]}},
        {"step_type": "SEARCH_RESULTS", "content": {"web_results": [{"url": "https://x"}]}},
        {
            "step_type": "FINAL",
            "content": {"answer": json.dumps({"answer": "The capital of France is **Paris**."})},
        },
    ]
    return json.dumps(steps)


def _sample_detail() -> dict[str, object]:
    return {"entries": [{"text": _entry_text()}], "background_entries": []}


def _sample_summary() -> dict[str, object]:
    return {
        "uuid": "thread-abc",
        "title": "  France   capital  ",
        "link": "/search/thread-abc",
        "status": "COMPLETED",
    }


# ---------------------------------------------------------------------------
# Step-block extraction
# ---------------------------------------------------------------------------


def test_extract_user_query_from_initial_query_block() -> None:
    steps = module._parse_entry_steps({"text": _entry_text()})
    assert module.extract_user_query(steps) == "What is the capital of France?"


def test_extract_assistant_answer_unwraps_nested_json() -> None:
    steps = module._parse_entry_steps({"text": _entry_text()})
    assert module.extract_assistant_answer(steps) == "The capital of France is **Paris**."


def test_extract_user_query_missing_returns_empty() -> None:
    steps = [{"step_type": "FINAL", "content": {"answer": "{}"}}]
    assert module.extract_user_query(steps) == ""


def test_extract_assistant_answer_missing_final_returns_empty() -> None:
    steps = [{"step_type": "INITIAL_QUERY", "content": {"query": "hi"}}]
    assert module.extract_assistant_answer(steps) == ""


def test_extract_assistant_answer_falls_back_on_unwrappable_string() -> None:
    steps = [{"step_type": "FINAL", "content": {"answer": "plain not-json text"}}]
    assert module.extract_assistant_answer(steps) == "plain not-json text"


def test_parse_entry_steps_tolerates_malformed_text() -> None:
    assert module._parse_entry_steps({"text": "not json"}) == []
    assert module._parse_entry_steps({"text": None}) == []
    assert module._parse_entry_steps({}) == []


# ---------------------------------------------------------------------------
# Thread normalization
# ---------------------------------------------------------------------------


def test_normalize_thread_produces_corpus_record() -> None:
    record = module.normalize_thread(_sample_summary(), _sample_detail())
    assert record["thread_uid"] == "thread-abc"
    assert record["conversation_id"] == "thread-abc"
    assert record["title_raw"] == "  France   capital  "
    assert record["title_normalized"] == "France capital"
    assert record["source"] == "perplexity-local-session"
    assert record["url"] == "https://www.perplexity.ai/search/thread-abc"
    assert record["messages"] == [
        {"role": "user", "text": "What is the capital of France?"},
        {"role": "assistant", "text": "The capital of France is **Paris**."},
    ]


def test_normalize_thread_skips_empty_entries() -> None:
    detail = {"entries": [{"text": "not json"}, {"text": _entry_text()}]}
    record = module.normalize_thread(_sample_summary(), detail)
    assert len(record["messages"]) == 2


# ---------------------------------------------------------------------------
# Session validity / discovery (network monkeypatched — no real calls)
# ---------------------------------------------------------------------------


def test_session_is_valid_requires_user_id() -> None:
    assert module._session_is_valid({"user": {"id": "u1"}})
    assert not module._session_is_valid({"user": {}})
    assert not module._session_is_valid({})


def test_build_perplexity_session_raises_when_invalid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    jar = tmp_path / "perplexity.binarycookies"
    jar.write_bytes(
        build_binary_cookie_jar([{"domain": "www.perplexity.ai", "name": "x", "value": "y"}])
    )
    monkeypatch.setattr(module, "_fetch_session", lambda cookies: {"user": {}})
    with pytest.raises(PerplexityLocalSessionError, match="Perplexity macOS app"):
        module.build_perplexity_session(jar)


def test_resolve_cookie_jar_missing_raises_signin_message(tmp_path: Path) -> None:
    with pytest.raises(PerplexityLocalSessionError, match="Sign in to the Perplexity macOS app"):
        module.resolve_perplexity_cookie_jar(tmp_path / "nope.binarycookies")


def _fake_session() -> PerplexityHttpSession:
    return PerplexityHttpSession(
        cookies=[],
        session_payload={
            "user": {
                "id": "u1",
                "email": "u@example.com",
                "username": "u",
                "subscription_tier": "pro",
            }
        },
        account_id="u1",
        account_email="u@example.com",
        account_username="u",
    )


def test_discover_perplexity_local_session_returns_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    jar = tmp_path / "perplexity.binarycookies"
    jar.write_bytes(
        build_binary_cookie_jar([{"domain": "www.perplexity.ai", "name": "x", "value": "y"}])
    )
    monkeypatch.setattr(module, "build_perplexity_session", lambda cookie_jar: _fake_session())
    monkeypatch.setattr(module, "_list_recent_threads", lambda session, **_kw: [_sample_summary()])

    payload = module.discover_perplexity_local_session(jar)
    assert payload["session_state"] == "ready"
    assert payload["conversation_count"] == 1
    assert payload["account_email"] == "u@example.com"
    assert payload["adapter_type"] == "perplexity-local-session"


def test_materialize_writes_threads_and_index(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    jar = tmp_path / "perplexity.binarycookies"
    jar.write_bytes(
        build_binary_cookie_jar([{"domain": "www.perplexity.ai", "name": "x", "value": "y"}])
    )
    monkeypatch.setattr(module, "build_perplexity_session", lambda cookie_jar: _fake_session())
    monkeypatch.setattr(module, "_list_recent_threads", lambda session, **_kw: [_sample_summary()])
    monkeypatch.setattr(module, "_fetch_thread_detail", lambda session, uuid: _sample_detail())

    out = tmp_path / "out"
    summary = module.materialize_perplexity_local_session(out, cookie_jar=jar)

    assert summary["threads_written"] == 1
    assert summary["fetched_count"] == 1
    thread_file = out / "threads" / "thread-abc.json"
    assert thread_file.exists()
    written = json.loads(thread_file.read_text(encoding="utf-8"))
    assert written["messages"][0]["role"] == "user"

    index = json.loads((out / "threads-index.json").read_text(encoding="utf-8"))
    assert index["thread_count"] == 1
    assert index["threads"][0]["message_count"] == 2


def test_render_discovery_text_includes_key_fields() -> None:
    text = module.render_discovery_text(
        {
            "cookie_jar": "/tmp/cookies",
            "generated_at": "2026-07-23T00:00:00+00:00",
            "session_state": "ready",
            "account_email": "u@example.com",
            "account_id": "u1",
            "conversation_count": 20,
            "calibration_only": True,
            "recommended_command": "cce provider import --provider perplexity",
        }
    )
    assert "Perplexity cookie jar: /tmp/cookies" in text
    assert "u@example.com" in text
    assert "Threads: 20" in text
