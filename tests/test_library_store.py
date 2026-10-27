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
