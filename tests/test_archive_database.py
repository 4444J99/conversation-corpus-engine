from __future__ import annotations

import sqlite3

import pytest

from conversation_corpus_engine.archive_database import (
    build_database,
    conversation_messages,
    database_path,
    fts_query,
    open_database,
    search,
    search_titles,
    verify_database,
)
from conversation_corpus_engine.archive_ingest import ingest
from conversation_corpus_engine.archive_topics import build_analysis


@pytest.fixture
def built(archive_env):
    manifest = ingest(
        export_root=archive_env.export, paths=archive_env.paths, authorization=archive_env.auth
    )
    build_analysis(archive_env.paths)
    summary = build_database(archive_env.paths, archive_env.auth)
    return archive_env, manifest, summary


def test_fts_query_never_exposes_operators() -> None:
    assert fts_query('kubernetes AND "exact phrase" OR -x NEAR(a b)') == (
        '"exact phrase" "kubernetes" "AND" "OR" "x" "NEAR" "a" "b"'
    )
    assert fts_query("   ") == ""
    assert fts_query('"') == ""
    assert fts_query("it's well-known") == '"it\'s" "well-known"'


def test_database_counts_match_manifest_denominators(built) -> None:
    env, manifest, summary = built
    assert summary["conversations"] == manifest["totals"]["conversations"]
    assert summary["messages"] == manifest["totals"]["messages"]
    assert verify_database(env.paths) == []
    assert database_path(env.paths).is_file()
    assert database_path(env.paths).name == "database-all-search.sqlite"
    assert database_path(env.paths).parent == env.paths.root


def test_verify_detects_a_stale_database(built) -> None:
    env, _, _ = built
    connection = sqlite3.connect(database_path(env.paths))
    connection.execute("DELETE FROM messages WHERE id = (SELECT MIN(id) FROM messages)")
    connection.commit()
    connection.close()
    problems = verify_database(env.paths)
    assert problems and "messages" in problems[0]


def test_full_text_search_returns_best_hit_per_conversation(built) -> None:
    env, _, _ = built
    connection = open_database(env.paths)
    try:
        hits = search(connection, "kubernetes cluster")
        uids = [hit["uid"] for hit in hits]
        assert len(uids) == len(set(uids))
        assert "chatgpt-aaaa1111" in uids and "claude-c1c1c1c1" in uids
        assert all("[" in hit["excerpt"] for hit in hits)
        assert [h["uid"] for h in search(connection, "kubernetes", providers=["grok"])] == [
            "grok-media-2025-06"
        ]
        assert (
            search(connection, "kubernetes", providers=["perplexity", "grok"], kinds=["chat"]) == []
        )
        sour = search(connection, "sourdough", providers=["claude", "grok"])
        assert {h["provider"] for h in sour} <= {"claude", "grok"} and sour
        assert search(connection, "nonexistentterm") == []
        assert search(connection, '"') == []
        searches = search(connection, "kubernetes", kinds=["search"])
        assert {h["provider"] for h in searches} == {"perplexity", "gemini"}
    finally:
        connection.close()


def test_porter_stemming_and_reasoning_filter(built) -> None:
    env, _, _ = built
    connection = open_database(env.paths)
    try:
        assert any(h["uid"] == "claude-c2c2c2c2" for h in search(connection, "hydrating"))
        assert any(h["uid"] == "claude-c2c2c2c2" for h in search(connection, "hydration"))
    finally:
        connection.close()


def test_title_search_and_message_listing(built) -> None:
    env, _, _ = built
    connection = open_database(env.paths)
    try:
        titles = search_titles(connection, "rollout")
        assert [t["uid"] for t in titles] == ["chatgpt-aaaa1111"]
        messages = conversation_messages(connection, "chatgpt-aaaa1111")
        assert [m["role"] for m in messages] == ["user", "assistant"]
        edges = connection.execute(
            "SELECT COUNT(*) FROM edges WHERE kind = 'collection'"
        ).fetchone()[0]
        assert edges >= 1
    finally:
        connection.close()


def test_read_only_connection_rejects_writes(built) -> None:
    env, _, _ = built
    connection = open_database(env.paths)
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM messages")
    finally:
        connection.close()


def test_open_database_reports_missing_database(archive_env) -> None:
    with pytest.raises(FileNotFoundError, match="cce archive database"):
        open_database(archive_env.paths)


def test_rebuild_replaces_the_database_atomically(built) -> None:
    env, _, first = built
    second = build_database(env.paths, env.auth)
    assert second["messages"] == first["messages"]
    assert not list(env.paths.root.glob(".*.tmp"))
