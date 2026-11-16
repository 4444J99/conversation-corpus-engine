"""Passage retrieval, candidate-only migration and source-bound character locators."""

import hashlib
import sqlite3
from pathlib import Path

import pytest

from conversation_corpus_engine import library_build, library_commands, library_mcp, library_store
from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.cli import build_parser
from conversation_corpus_engine.corpus_store import register_corpus_store
from conversation_corpus_engine.library_commands import read_record
from conversation_corpus_engine.library_mcp import READ_TOOL, SEARCH_TOOL
from conversation_corpus_engine.library_store import LEGACY_LIBRARY_SCHEMA, LIBRARY_SCHEMA, Library

PROSE = (
    "The luminous crystal lattice records a detailed account of the observation, its historical "
    "setting, the original evidence and a carefully qualified interpretation."
)


def record(identity, *texts, provider="chatgpt", **extra):
    return Conversation(
        provider=provider,
        source_id=identity,
        title=f"Record {identity}",
        messages=[Message(role="user", text=text) for text in texts],
        extra={"wing": "ai", "lane": "conversations", **extra},
    )


def legacy(path):
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE passage_fts")
        connection.execute("DROP INDEX occurrences_passage")
        connection.execute("UPDATE meta SET value=? WHERE key='schema'", (LEGACY_LIBRARY_SCHEMA,))


def test_passage_pages_deduplicate_blocks_but_count_conversations_and_filter_occurrences(tmp_path):
    with Library.create(tmp_path / "library.sqlite") as library:
        library.put(record("a", PROSE, PROSE))
        library.put(record("b", PROSE, provider="claude", wing="authored", lane="research"))
        library.put(record("c", PROSE + " Additional unique evidence."))
        library.index_blocks()
        first = library.search("luminous crystal", unit="passage", limit=1)
        second = library.search("luminous crystal", unit="passage", limit=1, offset=1)
        assert first["total"] == 3 and first["total_unit"] == "conversation"
        assert first["unit_total"] == 2 and first["unit_total_exact"] is True
        assert first["next_offset"] == 1 and second["next_offset"] is None
        assert first["hits"][0]["passage"]["digest"] != second["hits"][0]["passage"]["digest"]
        hit = library.search(
            "luminous crystal",
            unit="passage",
            providers=["claude"],
            wing="authored",
            lane="research",
        )["hits"][0]
        assert hit["uid"] == "claude-b" and hit["read"]["message"] == 0
        assert read_record(library, **hit["read"])["text"] == PROSE
        capped = library.search("luminous crystal", unit="passage", count_cap=1)
        assert capped["total"] == capped["unit_total"] == 1
        assert not capped["total_exact"] and not capped["unit_total_exact"]
        assert len(capped["hits"]) == 2


def test_occurrence_locators_use_original_unicode_and_crlf_characters_not_canonical_offsets(
    tmp_path,
):
    quoted = "> " + PROSE.replace("observation,", "observation,\r\n> café 雪") + "\r\n"
    text = "🙂 An original preface.\r\n\r\n" + quoted
    with Library.create(tmp_path / "library.sqlite") as library:
        item = record("quoted", "earlier message", text)
        library.put(item)
        library.index_blocks()
        hit = library.search("luminous crystal", unit="passage")["hits"][0]
        read = hit["read"]
        assert read["message"] == 1 and read["offset"] == text.index("> ")
        assert read_record(library, **read)["text"] == quoted
        library.put(record("quoted", "later replacement"))
        # An old handoff keeps pointing at the immutable revision it searched.
        assert read_record(library, **read)["text"] == quoted
        assert library.search("luminous crystal", unit="passage")["hits"] == []


def test_auto_selects_query_unit_and_falls_back_for_code_titles_short_text_and_alternates(tmp_path):
    with Library.create(tmp_path / "library.sqlite") as library:
        library.put(record("a", PROSE, "tiny specialtoken", "```\ncodeonlytoken = 1\n```"))
        library.put(record("titletoken", "short", alternate_messages=[{"text": "alternatetoken"}]))
        library.index_blocks()
        assert library.search("luminous crystal", unit="auto")["unit"] == "passage"
        assert (
            library.search('"luminous crystal lattice records"', unit="auto")["unit"] == "passage"
        )
        assert (
            library.search("luminous crystal lattice records", unit="auto")["unit"]
            == "conversation"
        )
        assert library.search("luminous crystal")["unit"] == "conversation"
        for query in ("specialtoken", "codeonlytoken", "titletoken", "alternatetoken"):
            auto = library.search(query, unit="auto")
            explicit = library.search(query, unit="passage")
            assert auto["unit"] == "conversation" and auto["total"] == 1
            assert explicit["unit"] == "passage" and explicit["hits"] == []
            assert explicit["unit_total"] == 0 and explicit["total"] == 1


@pytest.mark.parametrize("compact", [False, True])
def test_v1_migrates_existing_blocks_only_on_a_new_copy(tmp_path, monkeypatch, compact):
    path = tmp_path / "original.sqlite"
    with Library.create(path) as library:
        library.put(record("a", PROSE))
        library.index_blocks()
        evidence = library.evidence_fingerprint()
    legacy(path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with Library(path) as library:
        assert library.status()["schema"] == LEGACY_LIBRARY_SCHEMA
        assert library.search("luminous", unit="auto")["unit"] == "conversation"
        with pytest.raises(ValueError, match="new candidate"):
            library.search("luminous", unit="passage")
        with pytest.raises(ValueError, match="read-only"):
            library.migrate_passages()
    with monkeypatch.context() as patch:
        patch.setattr(
            Library, "record", lambda *args, **kwargs: pytest.fail("migration decoded a source")
        )
        with Library.create(
            tmp_path / "copy.sqlite", previous=path, compact_previous=compact
        ) as copied:
            assert copied.schema == LIBRARY_SCHEMA == "cce.knowledge-library.v2"
            assert copied.evidence_fingerprint() == evidence
            assert copied.search("luminous", unit="passage")["unit_total"] == 1
            assert copied.verify(full=False) == []
    with Library(tmp_path / "copy.sqlite") as copied:
        assert copied.verify() == []
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_interrupted_migration_keeps_the_legacy_schema_and_source_copy_intact(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with Library.create(path) as library:
        library.put(record("a", PROSE))
        library.index_blocks()
    legacy(path)
    with Library(path, writable=True) as library:
        evidence = library.evidence_fingerprint()

        def stop():
            raise TimeoutError("stop the candidate migration")

        with pytest.raises(TimeoutError, match="candidate migration"):
            library.migrate_passages(check_callback=stop)
        assert library.schema == LEGACY_LIBRARY_SCHEMA
        assert library.evidence_fingerprint() == evidence
        assert not library.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='passage_fts'"
        ).fetchall()


def test_incremental_index_removes_obsolete_passages_and_preserves_shared_copies(tmp_path):
    with Library.create(tmp_path / "library.sqlite") as library:
        library.put(record("a", PROSE))
        library.put(record("b", PROSE))
        library.index_blocks()
        library.put(record("a", PROSE.replace("luminous", "iridescent")))
        library.index_blocks(uids=["chatgpt-a"])
        assert library.search("luminous", unit="passage")["hits"][0]["uid"] == "chatgpt-b"
        assert library.search("iridescent", unit="passage")["hits"][0]["uid"] == "chatgpt-a"
        assert library.verify() == []
        library.put(record("b", "short replacement"))
        library.index_blocks(uids=["chatgpt-b"])
        assert library.search("luminous", unit="passage")["hits"] == []
        assert library.verify() == []


@pytest.mark.parametrize(
    "mutation",
    [
        "DELETE FROM passage_fts",
        "UPDATE passage_fts SET digest='wrong'",
        "UPDATE passage_fts SET body='wrong'",
        "DELETE FROM occurrences",
        "UPDATE occurrences SET start=-1",
        "UPDATE occurrences SET end=1000000",
        "UPDATE occurrences SET message_index=999",
        "UPDATE occurrences SET block_index=999",
        "UPDATE occurrences SET normalization='[\"different\"]'",
    ],
)
def test_verify_detects_index_drift_against_blocks_and_occurrences(tmp_path, mutation):
    with Library.create(tmp_path / "library.sqlite") as library:
        library.put(record("a", PROSE))
        library.index_blocks()
        library.connection.execute(mutation)
        library.connection.commit()
        assert any("passage" in problem for problem in library.verify())


def test_passage_budget_expiry_keeps_valid_locators_and_truthful_counts(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(library_store, "_clock", lambda: clock[0])
    monkeypatch.setattr(library_store, "SEARCH_PROGRESS_INSTRUCTIONS", 1)
    with Library.create(tmp_path / "library.sqlite") as library:
        for number in range(20):
            library.put(record(str(number), PROSE + f" Record suffix {number}."))
        library.index_blocks()
        library.connection.set_trace_callback(
            lambda sql: clock.__setitem__(0, 100.0) if "bm25" in sql else None
        )
        result = library.search("luminous", unit="passage", max_seconds=1, limit=3)
        library.connection.set_trace_callback(None)
        assert result["partial"] and not result["ranked"]
        assert result["unit_total"] == 20 and result["unit_total_exact"]
        assert result["total"] == 3 and not result["total_exact"]
        assert result["next_offset"] == 3
        assert all(
            "luminous" in read_record(library, **hit["read"])["text"] for hit in result["hits"]
        )
        assert not library.connection.in_transaction


def test_mcp_and_cli_units_use_exact_bounded_handoffs_for_long_blocks(
    tmp_path, monkeypatch, capsys
):
    path = tmp_path / "library.sqlite"
    text = ("ordinary context " * 300) + PROSE
    with Library.create(path) as library:
        library.put(record("a", "earlier unrelated message", text))
        library.index_blocks()
    monkeypatch.setattr(library_mcp, "open_library", lambda _: (tmp_path, Library(path)))
    monkeypatch.setattr(library_commands, "open_library", lambda _: (tmp_path, Library(path)))
    found = library_mcp.call_tool(
        tmp_path, SEARCH_TOOL, {"query": "luminous crystal", "depth": "L0"}
    )
    assert found["unit"] == "passage"
    read = found["hits"][0]["read"]
    assert read["message"] == 1 and read["offset"] > 0
    assert PROSE in library_mcp.call_tool(tmp_path, READ_TOOL, read)["text"]
    compact = library_mcp.call_tool(
        tmp_path, SEARCH_TOOL, {"query": "luminous", "compact": True, "depth": "L0"}
    )
    assert compact["u"] == "passage" and compact["un"] == 1 and compact["nu"] == "conversation"
    args = build_parser().parse_args(
        ["library", "search", "luminous crystal", "--store-root", str(tmp_path)]
    )
    assert args.unit == "auto"
    assert library_commands.handle_library(args) == 0
    assert '"unit": "passage"' in capsys.readouterr().out


@pytest.mark.parametrize("unit", [None, True, [], {}, "block", "PASSAGE"])
def test_invalid_search_units_are_rejected(tmp_path, unit):
    with (
        Library.create(tmp_path / "library.sqlite") as library,
        pytest.raises(ValueError, match="unit must be"),
    ):
        library.search("word", unit=unit)


def test_auto_still_reaches_conversation_fallback_after_a_passage_probe_times_out(
    tmp_path, monkeypatch
):
    clock = [0.0]
    monkeypatch.setattr(library_store, "_clock", lambda: clock[0])
    monkeypatch.setattr(library_store, "SEARCH_PROGRESS_INSTRUCTIONS", 1)
    with Library.create(tmp_path / "library.sqlite") as library:
        library.put(record("a", "short uniqueword"))
        library.index_blocks()
        library.connection.set_trace_callback(
            lambda sql: (
                clock.__setitem__(0, 100.0)
                if "SELECT COUNT(*)" in sql and "passage_fts" in sql
                else None
            )
        )
        found = library.search("uniqueword", unit="auto", max_seconds=1)
        library.connection.set_trace_callback(None)
        assert found["unit"] == "conversation" and found["partial"]
        assert [hit["uid"] for hit in found["hits"]] == ["chatgpt-a"]


def test_passage_keys_occurrences_and_counts_share_one_read_snapshot(tmp_path):
    path = tmp_path / "library.sqlite"
    with Library.create(path) as library:
        library.put(record("a", PROSE))
        library.index_blocks()
    attempted = []
    with Library(path) as library:

        def race(sql):
            if "rowid IN" in sql and "snippet(" in sql and not attempted:
                with sqlite3.connect(path, timeout=0) as writer:
                    writer.execute("DELETE FROM occurrences")
                    try:
                        writer.commit()
                    except sqlite3.OperationalError as error:
                        attempted.append(str(error))
                        writer.rollback()

        library.connection.set_trace_callback(race)
        found = library.search("luminous", unit="passage")
        library.connection.set_trace_callback(None)
        assert attempted == ["database is locked"]
        assert found["unit_total"] == found["total"] == 1
        assert read_record(library, **found["hits"][0]["read"])["text"] == PROSE
        assert not library.connection.in_transaction


def test_auto_prefers_a_conversation_and_match_over_a_partial_passage_or_match(tmp_path):
    with Library.create(tmp_path / "library.sqlite") as library:
        library.put(record("a", "luminous uniquehint", PROSE))
        library.put(record("b", PROSE + " Another observation."))
        library.index_blocks()
        result = library.search("uniquehint luminous", unit="auto")
        assert (result["unit"], result["plan"]) == ("conversation", "and")
        assert [hit["uid"] for hit in result["hits"]] == ["chatgpt-a"]


def test_full_verification_rejects_an_occurrence_linked_to_a_different_existing_block(tmp_path):
    with Library.create(tmp_path / "library.sqlite") as library:
        library.put(record("a", PROSE))
        library.put(record("b", PROSE.replace("luminous", "glowingx")))
        library.index_blocks()
        library.connection.execute(
            "UPDATE occurrences SET digest=(SELECT digest FROM occurrences WHERE uid='chatgpt-b') "
            "WHERE uid='chatgpt-a'"
        )
        library.connection.commit()
        assert any("differs from source: chatgpt-a" in problem for problem in library.verify())


def test_schema_only_candidate_publishes_without_reimporting_or_changing_source_evidence(
    tmp_path, monkeypatch
):
    project, store = tmp_path / "project", tmp_path / "store"
    project.mkdir()
    store.mkdir()
    register_corpus_store(project, store)
    manifest = {"schema": "cce.library-sources.v1"}
    create = Library.create

    def old_generation(cls, path, **kwargs):
        # Simulate an existing v1 generation retaining a source whose import is no longer
        # declared. Both subsequent builds have exactly the same empty source declarations.
        with create(path, **kwargs) as seed:
            seed.put(record("retained", PROSE))
            seed.index_blocks()
        legacy(path)
        return Library(path, writable=True)

    with monkeypatch.context() as patch:
        patch.setattr(Library, "create", classmethod(old_generation))
        patch.setattr(library_build, "LIBRARY_SCHEMA", LEGACY_LIBRARY_SCHEMA)
        old = library_build.build_library(manifest, project_root=project, store_root=store)
    old_path = Path(old["generation"])
    library_build.publish_library(old_path, project_root=project, store_root=store)
    before = hashlib.sha256((old_path / "library.sqlite").read_bytes()).hexdigest()
    with Library(old_path / "library.sqlite") as original:
        fingerprint = original.evidence_fingerprint()
    with monkeypatch.context() as patch:
        patch.setattr(
            Library, "put", lambda *args, **kwargs: pytest.fail("migration re-imported a source")
        )
        new = library_build.build_library(manifest, project_root=project, store_root=store)
    new_path = Path(new["generation"])
    assert new["record_fingerprint"] == old["record_fingerprint"]
    assert new["source_manifest_hash"] == old["source_manifest_hash"]
    assert new["sources"] == old["sources"]
    assert new["evidence_fingerprint"] != old["evidence_fingerprint"]
    assert library_build.publish_library(new_path, project_root=project, store_root=store)[
        "changed"
    ]
    assert library_build.current_generation(store) == new_path
    with Library(new_path / "library.sqlite") as upgraded:
        assert upgraded.evidence_fingerprint() == fingerprint
        assert upgraded.search("luminous", unit="passage")["unit_total"] == 1
    assert hashlib.sha256((old_path / "library.sqlite").read_bytes()).hexdigest() == before
