"""The author partition: who wrote a turn, and the identity that every turn has exactly one."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from conversation_corpus_engine.archive_author import (
    AUTHORS,
    CONTAINER_BY_AUTHOR,
    HUMAN,
    MACHINE,
    author_of,
    container_of,
    count_authors,
    reconcile,
)
from conversation_corpus_engine.archive_feed import project_feed
from conversation_corpus_engine.archive_fixture import write_fixture
from conversation_corpus_engine.archive_ingest import (
    DEFAULT_ARCHIVE_DIRNAME,
    ArchivePaths,
    authorize_archive,
    ingest,
    iter_store,
)
from conversation_corpus_engine.archive_model import CHANNELS, ROLES, Message
from conversation_corpus_engine.corpus_store import register_corpus_store


@pytest.mark.parametrize(
    ("role", "channel", "author"),
    [
        ("user", "typed", HUMAN),
        ("assistant", "reply", MACHINE),
        ("assistant", "thinking", MACHINE),
        ("tool", "tool", MACHINE),
        ("system", "system", MACHINE),
        ("system", "memory", MACHINE),
        ("system", "project", MACHINE),
        # a user-role turn that is not a typed ask is not the person's words
        ("user", "reply", MACHINE),
        ("user", "project", MACHINE),
        ("assistant", "typed", MACHINE),
    ],
)
def test_only_a_typed_user_turn_is_human(role: str, channel: str, author: str) -> None:
    assert author_of(role, channel) == author


def test_every_role_and_channel_pair_has_exactly_one_author() -> None:
    for role in ROLES:
        for channel in CHANNELS:
            assert author_of(role, channel) in AUTHORS


def test_a_message_derives_its_author_and_the_rule_never_changes_its_row() -> None:
    message = Message(role="user", text="hello", channel="typed")
    assert message.author == HUMAN
    assert "author" not in message.to_dict(), "the stored row stays as the v2 schema wrote it"
    assert Message(role="assistant", text="hi", channel="reply").author == MACHINE


def test_containers_are_named_for_the_person_and_the_machine() -> None:
    assert container_of(HUMAN) == "authored" and container_of(MACHINE) == "machine"
    assert set(CONTAINER_BY_AUTHOR) == set(AUTHORS)
    with pytest.raises(ValueError, match="unknown author"):
        container_of("ghost")


def test_reconcile_states_the_identity_human_plus_machine_equals_messages() -> None:
    assert reconcile({HUMAN: 3, MACHINE: 4}, 7) == []
    assert "do not reconcile" in reconcile({HUMAN: 3, MACHINE: 4}, 8)[0]
    assert reconcile({HUMAN: 3, MACHINE: 4, "ghost": 1}, 8) != []
    assert reconcile({}, 0) == []


def test_count_authors_counts_objects_that_carry_a_role_and_channel() -> None:
    items = [
        SimpleNamespace(role="user", channel="typed"),
        SimpleNamespace(role="assistant", channel="reply"),
        SimpleNamespace(role="assistant", channel="reply"),
    ]
    assert dict(count_authors(items)) == {HUMAN: 1, MACHINE: 2}


def test_the_human_count_of_a_build_equals_the_asks_the_feed_reads(tmp_path) -> None:
    """The author rule is the feed's own: a provider's human turns are its feed rows (less the
    abandoned-branch asks, which are not main-branch turns) plus the typed turns the feed excluded
    only because they hold no text."""
    export = tmp_path / "export"
    write_fixture(export)
    project, store = tmp_path / "project", tmp_path / "store"
    project.mkdir()
    store.mkdir()
    register_corpus_store(project, store)
    authorization = authorize_archive(
        project_root=project,
        archive_root=store / DEFAULT_ARCHIVE_DIRNAME,
        corpus_store_root=store,
        source_roots=(export,),
    )
    paths = ArchivePaths(authorization.destination)
    manifest = ingest(export_root=export, paths=paths, authorization=authorization)
    feeds = tmp_path / "feeds"
    summary = project_feed(paths.root, feeds)
    for provider in ("chatgpt", "claude"):
        info = manifest["providers"][provider]
        human = info["authors"][HUMAN]
        feed = summary[provider]
        asks = feed["rows"] - feed["alternate_rows"] + feed["excluded"].get("empty-text", 0)
        assert human == asks, (provider, human, feed)
        assert reconcile(info["authors"], info["messages"]) == []
    assert manifest["totals"]["authors"][HUMAN] == sum(
        info["authors"][HUMAN] for info in manifest["providers"].values() if info["status"] == "ok"
    )
    assert reconcile(manifest["totals"]["authors"], manifest["totals"]["messages"]) == []
    counted = count_authors(m for c in iter_store(paths) for m in c.messages)
    assert dict(counted) == manifest["totals"]["authors"]
