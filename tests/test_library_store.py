import hashlib

import pytest

from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.library_store import Library


def record(identity="one", text="A lattice of remembered ideas.", **extra):
    return Conversation(
        provider="chatgpt",
        source_id=identity,
        title=f"Record {identity}",
        messages=[Message(role="user", text=text, id="m1", channel="typed")],
        provenance={"source_file": f"{identity}.json", "source_sha256": "a" * 64},
        extra={"wing": "ai", "lane": "conversations", "body_status": "full", **extra},
    )


def test_versions_preserve_content_metadata_and_classification(tmp_path):
    with Library.create(tmp_path / "library.sqlite") as library:
        item = record()
        assert library.put(item)["version"] == "1.0.0"
        assert library.put(item)["changed"] is False
        item.title = "Corrected title"
        assert library.put(item)["version"] == "1.0.1"
        item.messages[0].text = "A lattice revised with another observation."
        assert library.put(item)["version"] == "1.1.0"
        item.extra["lane"] = "coding"
        assert library.put(item)["version"] == "2.0.0"
        assert (
            library.record(item.uid, version="1.0.0")["messages"][0]["text"]
            == "A lattice of remembered ideas."
        )
        assert len(library.history(item.uid)) == 4
        assert library.verify() == []


def test_equal_text_keeps_distinct_identities_and_source_observations(tmp_path):
    with Library.create(tmp_path / "library.sqlite") as library:
        a, b = record("one"), record("two")
        library.put(a)
        library.put(b)
        c = record("one")
        c.provider = "claude"
        library.put(c)
        assert library.status()["records"] == 3
        assert library.search("lattice")["total"] == 3
        assert len({hit["uid"] for hit in library.search("lattice")["hits"]}) == 3
        assert library.status()["observations"] == 3


def test_record_pagination_is_not_starved_by_long_conversation(tmp_path):
    with Library.create(tmp_path / "library.sqlite") as library:
        long = record("long", "lattice " * 5000)
        long.messages.extend(
            Message(role="assistant", text="lattice", id=f"a{i}") for i in range(200)
        )
        library.put(long)
        for number in range(205):
            library.put(record(f"short-{number:03}", f"lattice observation {number}"))
        first = library.search("lattice", limit=100)
        second = library.search("lattice", limit=100, offset=100)
        last = library.search("lattice", limit=100, offset=200)
        assert first["total"] == 206
        assert [len(part["hits"]) for part in (first, second, last)] == [100, 100, 6]
        assert len({hit["uid"] for part in (first, second, last) for hit in part["hits"]}) == 206
        assert first["next_offset"] == 100
        assert last["next_offset"] is None
        assert max(len(hit["excerpt"]) for hit in first["hits"]) <= 360


def test_candidate_inherits_history_and_does_not_prune_missing_sources(tmp_path):
    original = tmp_path / "original.sqlite"
    with Library.create(original) as library:
        library.put(record("retained"))
    before = original.read_bytes()
    with Library.create(tmp_path / "candidate.sqlite", previous=original) as candidate:
        candidate.put(record("new"))
        assert candidate.status()["records"] == 2
        assert candidate.record("chatgpt-retained")["source_id"] == "retained"
    assert original.read_bytes() == before


def test_repeat_blocks_keep_all_occurrences_and_unique_text(tmp_path):
    repeated = (
        "A detailed account of the lattice records its origin, context, evidence, and relationships. "
        * 3
    )
    with Library.create(tmp_path / "library.sqlite") as library:
        library.put(record("one", repeated + "\n\nUnique ending in the first record."))
        library.put(record("two", repeated + "\n\nA different ending remains visible."))
        library.index_blocks()
        result = library.status()
        assert result["repeated_blocks"] == 1
        assert result["reference_occurrences"] == 1
        assert library.record("chatgpt-two")["messages"][0]["text"].endswith("visible.")
        assert library.verify() == []


def test_read_only_and_filter_contract(tmp_path):
    path = tmp_path / "library.sqlite"
    with Library.create(path) as library:
        library.put(record())
        authored = record("writing", wing="authored", lane="research")
        authored.kind = "document"
        library.put(authored)
    with Library(path) as library:
        assert library.search("lattice", wing="authored")["total"] == 1
        assert library.search("lattice", kinds=["document"])["hits"][0]["lane"] == "research"
        assert library.search("lattice", providers=["claude"])["total"] == 0
        assert library.search('" OR *')["hits"] == []
        with pytest.raises(ValueError, match="read-only"):
            library.put(record("refused"))
        with pytest.raises(ValueError):
            library.search("lattice", limit=-1)


def test_reverting_content_retains_a_new_semantic_revision(tmp_path):
    with Library.create(tmp_path / "library.sqlite") as library:
        item = record()
        original = item.messages[0].text
        library.put(item)
        item.messages[0].text = "Another lattice."
        library.put(item)
        item.messages[0].text = original
        assert library.put(item)["version"] == "1.2.0"
        assert len(library.history(item.uid)) == 3


def test_partial_observation_never_erases_an_accepted_body(tmp_path):
    with Library.create(tmp_path / "library.sqlite") as library:
        item = record()
        library.put(item)
        item.messages = []
        item.extra["body_status"] = "metadata_only"
        result = library.put(item, reconcile=True)
        assert result["changed"] is False
        assert library.search("lattice")["total"] == 1
        assert library.record(item.uid)["extra"]["body_status"] == "full"
        assert len(library.snapshots(item.uid)) == 2


def test_replayed_source_snapshots_are_idempotent(tmp_path):
    with Library.create(tmp_path / "library.sqlite") as library:
        old, new = record(text="Original lattice."), record(text="New lattice.")
        for item in (old, new, old, new):
            library.put(item, reconcile=True)
        assert len(library.history(old.uid)) == 2
        assert library.record(old.uid)["messages"][0]["text"] == "New lattice."


def test_refuse_provider_path_escape(tmp_path):
    with Library.create(tmp_path / "library.sqlite") as library:
        item = record()
        item.provider = "../escape"
        with pytest.raises(ValueError, match="safe stable identifiers"):
            library.put(item)


def test_compressed_reference_index_preserves_originals_and_search(tmp_path):
    passage = "The shared account keeps its exact provenance and original wording. " * 40
    code = "```python\n" + "print('searchablecode')\n" * 20 + "```"
    source = passage + "\n\n" + code + "\n\nA short searchabletail."
    with Library.create(tmp_path / "library.sqlite") as library:
        library.put(record("one", source))
        library.put(record("two", "> " + passage))
        library.index_blocks()
        digest = hashlib.sha256(passage.encode()).hexdigest()
        assert library.block_text(digest) == passage
        assert library.status()["blocks"] == 1
        assert library.status()["reference_occurrences"] == 1
        assert library.search("searchablecode")["total"] == 1
        assert library.search("searchabletail")["total"] == 1
        assert library.record("chatgpt-one")["messages"][0]["text"] == source
        stored_bytes = library.connection.execute("SELECT length(text) FROM blocks").fetchone()[0]
        assert stored_bytes < len(passage.encode()) // 4


def test_compacted_resume_copy_keeps_all_retained_evidence(tmp_path):
    original = tmp_path / "original.sqlite"
    with Library.create(original) as library:
        item = record("retained", "Lattice version one.")
        library.put(item)
        item.messages[0].text = "Lattice version two."
        library.put(item)
        item.messages[0].text = "An older unselected lattice snapshot."
        library.put(item, observation_only=True)
        evidence = library.evidence_fingerprint()
        library.connection.execute("CREATE TABLE temporary_space(payload BLOB)")
        library.connection.execute("INSERT INTO temporary_space VALUES (zeroblob(1048576))")
        library.connection.execute("DROP TABLE temporary_space")
        library.connection.commit()
    source_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    candidate_path = tmp_path / "candidate.sqlite"
    with Library.create(candidate_path, previous=original, compact_previous=True) as candidate:
        assert candidate.evidence_fingerprint() == evidence
        assert len(candidate.history(item.uid)) == 2
        assert len(candidate.snapshots(item.uid)) == 3
        assert candidate.record(item.uid)["messages"][0]["text"] == "Lattice version two."
        assert candidate.verify() == []
    assert candidate_path.stat().st_size < original.stat().st_size
    assert hashlib.sha256(original.read_bytes()).hexdigest() == source_hash


def test_interrupted_block_rebuild_keeps_prior_rows_and_lookup_index(tmp_path):
    with Library.create(tmp_path / "library.sqlite") as library:
        library.put(record(text="A carefully retained passage with source and context. " * 4))
        library.index_blocks()
        previous = [tuple(row) for row in library.connection.execute("SELECT * FROM occurrences")]

        def interrupt():
            raise TimeoutError("bounded rebuild interrupted")

        with pytest.raises(TimeoutError):
            library.index_blocks(check_callback=interrupt)
        assert [
            tuple(row) for row in library.connection.execute("SELECT * FROM occurrences")
        ] == previous
        assert library.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='occurrences_digest'"
        ).fetchone()


def test_summary_queries_use_covering_metadata_and_keep_exact_counts(tmp_path):
    passage = "A shared passage retains the original context and all its source observations. " * 5
    with Library.create(tmp_path / "library.sqlite") as library:
        library.put(record("one", passage))
        library.put(record("two", passage))
        library.index_blocks()
        traced = []
        library.connection.set_trace_callback(traced.append)
        status = library.status()
        library.connection.set_trace_callback(None)
        assert status["repeated_blocks"] == 1
        assert status["reference_occurrences"] == 1
        assert status["repeated_characters"] == len(passage)
        summary = next(sql for sql in traced if "SUM(n-1)" in sql)
        plan = library.connection.execute("EXPLAIN QUERY PLAN " + summary).fetchall()
        assert any("b USING COVERING INDEX" in row[3] for row in plan)


def test_scoped_sql_cancellation_restores_outer_handler_and_clears_on_exit(tmp_path):
    query = "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<20000) SELECT sum(x) FROM n"
    with Library.create(tmp_path / "library.sqlite") as library:
        outer_calls = []

        def outer():
            outer_calls.append(True)

        def cancel():
            raise TimeoutError("inner SQL deadline")

        with library.sql_progress(outer, instructions=100):
            assert library.connection.execute(query).fetchone()[0] == 200010000
            before = len(outer_calls)
            assert before > 0
            with (
                pytest.raises(TimeoutError, match="inner SQL deadline"),
                library.sql_progress(cancel, instructions=100),
            ):
                library.connection.execute(query).fetchone()
            assert library.connection.execute(query).fetchone()[0] == 200010000
            assert len(outer_calls) > before
        after = len(outer_calls)
        assert library.connection.execute(query).fetchone()[0] == 200010000
        assert len(outer_calls) == after


@pytest.mark.parametrize("method", ["status", "verify"])
def test_sql_only_summary_and_verification_honor_cancellation(tmp_path, method):
    with Library.create(tmp_path / "library.sqlite") as library:
        item = record()
        library.put(item)
        library.connection.executemany(
            "INSERT INTO blocks VALUES (?,zeroblob(1000),1000,1)",
            ((f"{number:064x}",) for number in range(5000)),
        )
        library.connection.executemany(
            "INSERT INTO occurrences VALUES (?,0,?,?,0,1000,'{}')",
            ((item.uid, number, f"{number:064x}") for number in range(5000)),
        )
        library.connection.commit()
        traced = []
        library.connection.set_trace_callback(traced.append)

        def cancel():
            assert traced, "cancellation must run while SQL is executing"
            raise TimeoutError("SQL budget exhausted")

        kwargs = {"check_callback": cancel}
        if method == "verify":
            kwargs["full"] = False
        with pytest.raises(TimeoutError, match="SQL budget exhausted"):
            getattr(library, method)(**kwargs)
        library.connection.set_trace_callback(None)
        assert library.status()["blocks"] == 5000


@pytest.mark.parametrize("index_name", ["occurrences_digest", "blocks_eligible_digest_chars"])
def test_sql_cancellation_during_index_creation_restores_both_indexes(tmp_path, index_name):
    text = "\n\n".join(
        f"Passage {number} keeps its source evidence, original wording, and all observed context. "
        * 3
        for number in range(1500)
    )
    with Library.create(tmp_path / "library.sqlite") as library:
        library.put(record(text=text))
        library.index_blocks()
        previous = [tuple(row) for row in library.connection.execute("SELECT * FROM occurrences")]
        traced = []
        library.connection.set_trace_callback(traced.append)

        def cancel():
            if traced and traced[-1].startswith("CREATE INDEX " + index_name):
                raise TimeoutError("cancel SQL index construction")

        with pytest.raises(TimeoutError, match="cancel SQL index construction"):
            library.index_blocks(check_callback=cancel)
        library.connection.set_trace_callback(None)
        assert [
            tuple(row) for row in library.connection.execute("SELECT * FROM occurrences")
        ] == previous
        indexes = {
            row[0]
            for row in library.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        assert {"occurrences_digest", "blocks_eligible_digest_chars"} <= indexes
        assert library.verify() == []


def test_legacy_library_without_covering_index_remains_read_only_compatible(tmp_path):
    path = tmp_path / "library.sqlite"
    with Library.create(path) as library:
        library.put(record(text="A historical passage retains context and source evidence. " * 4))
        library.index_blocks()
        library.connection.execute("DROP INDEX IF EXISTS blocks_eligible_digest_chars")
        library.connection.commit()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with Library(path) as library:
        traced = []
        library.connection.set_trace_callback(traced.append)
        assert library.status()["blocks"] == 1
        library.connection.set_trace_callback(None)
        summary = next(sql for sql in traced if "SUM(n-1)" in sql)
        plan = library.connection.execute("EXPLAIN QUERY PLAN " + summary).fetchall()
        assert any("occurrences USING COVERING INDEX" in row[3] for row in plan)
        assert not any("SCAN b " in row[3] for row in plan)
        assert library.verify() == []
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_incremental_blocks_match_full_rebuild_and_preserve_unaffected_occurrences(tmp_path):
    shared = (
        "A shared detailed paragraph preserves source occurrence identity and stable context. " * 3
    )
    old = (
        "An obsolete detailed paragraph is removed only after its last current occurrence changes. "
        * 3
    )
    new = (
        "Recovered detailed evidence becomes searchable while historical source objects stay retained. "
        * 3
    )
    with Library.create(tmp_path / "library.sqlite") as library:
        for item in (record("one", shared + "\n\n" + old), record("two", shared)):
            library.put(item)
        library.index_blocks()
        untouched = [
            tuple(row)
            for row in library.connection.execute(
                "SELECT * FROM occurrences WHERE uid='chatgpt-two'"
            )
        ]
        library.put(record("one", shared + "\n\n" + new))
        incremental = library.index_blocks(uids=["chatgpt-one"])
        assert untouched == [
            tuple(row)
            for row in library.connection.execute(
                "SELECT * FROM occurrences WHERE uid='chatgpt-two'"
            )
        ]
        occurrences = [
            tuple(row)
            for row in library.connection.execute(
                "SELECT * FROM occurrences ORDER BY uid,message_index,block_index"
            )
        ]
        blocks = [
            tuple(row) for row in library.connection.execute("SELECT * FROM blocks ORDER BY digest")
        ]
        assert library.index_blocks() == incremental
        assert occurrences == [
            tuple(row)
            for row in library.connection.execute(
                "SELECT * FROM occurrences ORDER BY uid,message_index,block_index"
            )
        ]
        assert blocks == [
            tuple(row) for row in library.connection.execute("SELECT * FROM blocks ORDER BY digest")
        ]
        assert len(library.history("chatgpt-one")) == 2


def test_incremental_blocks_rollback_after_replacement_is_interrupted(tmp_path):
    text = (
        "A sufficiently detailed source paragraph remains intact when an incremental transaction stops. "
        * 3
    )
    with Library.create(tmp_path / "library.sqlite") as library:
        library.put(record("one", text))
        library.index_blocks()
        library.put(record("one", text + "Changed."))
        before = [tuple(row) for row in library.connection.execute("SELECT * FROM occurrences")]
        calls = 0

        def stop():
            nonlocal calls
            calls += 1
            if calls == 4:
                raise ValueError("interrupted after inserting new occurrences")

        with pytest.raises(ValueError, match="interrupted"):
            library.index_blocks(uids=["chatgpt-one"], check_callback=stop)
        assert before == [
            tuple(row) for row in library.connection.execute("SELECT * FROM occurrences")
        ]
