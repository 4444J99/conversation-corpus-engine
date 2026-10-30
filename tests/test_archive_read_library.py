"""The library bridge: a published generation becomes archive providers, one atom each."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conversation_corpus_engine import archive_read_library as bridge
from conversation_corpus_engine.archive_author import HUMAN, MACHINE, reconcile
from conversation_corpus_engine.archive_commands import open_context, reconcile_source
from conversation_corpus_engine.archive_ingest import (
    PROVIDER_SPECS,
    SOURCE_LIBRARY,
    ingest,
    iter_store,
    load_manifest,
    resolve_sources,
)
from conversation_corpus_engine.archive_model import Conversation, Message, SourceTracker
from conversation_corpus_engine.corpus_store import register_corpus_store
from conversation_corpus_engine.library_store import Library

GENERATION = "generations/generation-test-aaaa"


def _conversation(
    provider: str,
    source_id: str,
    messages: list[Message],
    *,
    kind: str = "chat",
    wing: str = "ai",
    lane: str = "coding",
    **extra: Any,
) -> Conversation:
    return Conversation(
        provider=provider,
        source_id=source_id,
        title=f"{provider} {source_id}",
        created="2026-03-04T10:00:00Z",
        kind=kind,
        messages=messages,
        provenance={"source_file": f"{source_id}.json", "source_sha256": "a" * 64},
        extra={"wing": wing, "lane": lane, "body_status": "full", **extra},
    )


def _turn(role: str, text: str, channel: str, n: int, **meta: Any) -> Message:
    return Message(
        role=role,
        text=text,
        id=f"m{n}",
        channel=channel,
        ts=f"2026-03-04T10:0{n}:00Z",
        meta=meta,
    )


def _records() -> list[Conversation]:
    return [
        _conversation(
            "claudecode",
            "root-1",
            [
                _turn("user", "please refactor the parser", "typed", 1),
                _turn("user", "<system-reminder>stay brief</system-reminder>", "typed", 2),
                _turn("assistant", "done", "reply", 3),
                _turn("tool", "ok", "tool", 4, kind="tool"),
            ],
            session_kind="root",
        ),
        _conversation(
            "claudecode",
            "sub-1",
            [
                _turn("user", "research the thing and report back", "typed", 1),
                _turn("assistant", "reported", "reply", 2),
            ],
            session_kind="subagent",
        ),
        _conversation(
            "codex",
            "cx-1",
            [
                _turn("user", "# AGENTS.md instructions for /repo", "typed", 1),
                _turn("user", "run the tests", "typed", 2),
                _turn("assistant", "passed", "reply", 3),
            ],
            session_kind="root",
        ),
        _conversation(
            "applenotes",
            "note-1",
            [
                _turn("user", "grocery list", "typed", 1, source_field="body"),
                _turn("user", "eggs", "project", 2, table_order=0),
            ],
            kind="note",
            wing="authored",
            lane="notes",
        ),
        _conversation(
            "googledrive",
            "doc-1",
            [_turn("user", "a chapter draft", "typed", 1)],
            kind="document",
            wing="authored",
            lane="writing",
        ),
        _conversation(
            "documents",
            "file-authored",
            [_turn("user", "an essay the person wrote", "project", 1, kind="document")],
            kind="document",
            wing="authored",
            lane="research",
        ),
        _conversation(
            "documents",
            "file-memory",
            [_turn("user", "a remembered preference", "project", 1, kind="memory")],
            kind="memory",
            lane="support",
        ),
        _conversation(
            "documents",
            "file-plan",
            [_turn("user", "step one then step two", "project", 1, kind="plan")],
            kind="plan",
            lane="support",
        ),
        _conversation(
            "documents",
            "file-empty",
            [],
            kind="document",
            wing="authored",
            lane="writing",
            body_status="metadata_only",
        ),
        _conversation(
            "chatgpt",
            "page-1",
            [_turn("user", "a shared page question", "typed", 1)],
            kind="page",
            wing="ai",
            lane="conversations",
        ),
        _conversation(
            "chatgpt",
            "chat-1",
            [_turn("user", "not bridged: a plain chat", "typed", 1)],
            kind="chat",
            wing="ai",
            lane="conversations",
        ),
    ]


@pytest.fixture
def library_root(tmp_path: Path) -> Path:
    root = tmp_path / "library-all"
    database = root / GENERATION / "library.sqlite"
    database.parent.mkdir(parents=True)
    with Library.create(database) as library:
        for record in _records():
            library.put(record)
    (root / "current.json").write_text(
        json.dumps(
            {
                "generation": GENERATION,
                "record_fingerprint": "r" * 64,
                "evidence_fingerprint": "e" * 64,
                "report_sha256": "p" * 64,
            }
        ),
        encoding="utf-8",
    )
    return root


def _ingest(archive_env, library_root: Path, providers: list[str], **kwargs: Any) -> dict:
    return ingest(
        export_root=archive_env.export,
        paths=archive_env.paths,
        authorization=archive_env.auth,
        providers=providers,
        library_root=library_root,
        **kwargs,
    )


def test_locate_resolves_a_pointer_a_store_folder_and_a_bare_generation(
    library_root: Path,
) -> None:
    database = library_root / GENERATION / "library.sqlite"
    assert bridge.locate(library_root)[0] == database
    assert bridge.locate(library_root.parent)[0] == database  # the folder holding library-all
    assert bridge.locate(database.parent) == (database, None)
    assert bridge.detect(library_root) and not bridge.detect(library_root / "nowhere")


def test_every_library_family_is_a_provider_that_reads_the_library_root() -> None:
    names = {family.name for family in bridge.FAMILIES}
    assert names == {
        "claudecode",
        "codex",
        "opencode",
        "antigravity",
        "geminicli",
        "copilotcli",
        "applenotes",
        "googledrive",
        "documents",
        "agentmemory",
        "agentplans",
    }
    assert all(PROVIDER_SPECS[name].source == SOURCE_LIBRARY for name in names)


def test_resolve_sources_hands_a_library_family_the_library_root(
    tmp_path: Path, library_root: Path
) -> None:
    export = tmp_path / "export"
    sources = resolve_sources(export, ["codex", "chatgpt"], library_root=library_root)
    assert sources["codex"] == library_root.resolve()
    assert sources["chatgpt"] == (export / "chatgpt").resolve()
    other = tmp_path / "elsewhere"
    assert resolve_sources(export, ["codex"], {"codex": other}, library_root)["codex"] == (
        other.resolve()
    )


def test_a_coding_session_lands_as_turns_with_an_author_a_provider_and_a_kind(
    archive_env, library_root: Path
) -> None:
    manifest = _ingest(archive_env, library_root, ["claudecode", "codex"])
    claude = manifest["providers"]["claudecode"]
    assert (claude["conversations"], claude["messages"]) == (2, 6)
    assert claude["kinds"] == {"session": 2}
    # root-1: one typed prompt is human; the reminder, reply and tool are machine; sub-1's prompt
    # was written by the parent agent, so it is machine too.
    assert claude["authors"] == {HUMAN: 1, MACHINE: 5}
    assert claude["author_basis"] == {
        "human": {"typed": 1},
        "machine": {"injected:system-reminder": 1, "subagent-prompt": 1},
    }
    codex = manifest["providers"]["codex"]
    assert codex["authors"] == {HUMAN: 1, MACHINE: 2}
    assert codex["author_basis"]["machine"] == {"harness-text": 1}
    for info in (claude, codex):
        assert reconcile(info["authors"], info["messages"]) == []
        assert reconcile_source("x", info) == []
    stored = {c.uid: c for c in iter_store(archive_env.paths, ["claudecode"])}
    assert set(stored) == {"claudecode-root-1", "claudecode-sub-1"}
    root = stored["claudecode-root-1"]
    assert [(m.channel, m.author) for m in root.messages] == [
        ("typed", HUMAN),
        ("system", MACHINE),
        ("reply", MACHINE),
        ("tool", MACHINE),
    ]
    assert root.provenance["library_uid"] == "claudecode-root-1"
    assert root.provenance["bridge_reader"] == bridge.READER
    assert root.extra["library"]["wing"] == "ai"


def test_notes_and_drive_are_the_persons_including_table_cells(
    archive_env, library_root: Path
) -> None:
    manifest = _ingest(archive_env, library_root, ["applenotes", "googledrive"])
    notes = manifest["providers"]["applenotes"]
    assert notes["kinds"] == {"note": 1}
    assert notes["authors"] == {HUMAN: 2, MACHINE: 0}
    assert notes["turn_channels"] == {"typed": 1, "written": 1}
    drive = manifest["providers"]["googledrive"]
    assert (drive["kinds"], drive["authors"]) == ({"document": 1}, {HUMAN: 1, MACHINE: 0})


def test_the_librarys_documents_split_by_who_wrote_them(archive_env, library_root: Path) -> None:
    manifest = _ingest(archive_env, library_root, ["documents", "agentmemory", "agentplans"])
    documents = manifest["providers"]["documents"]
    # The authored essay is human text; the metadata-only file is a conversation with no turn.
    assert (documents["conversations"], documents["messages"]) == (2, 1)
    assert documents["empty_conversations"] == 1
    assert documents["authors"] == {HUMAN: 1, MACHINE: 0}
    assert documents["turn_channels"] == {"written": 1}
    memory = manifest["providers"]["agentmemory"]
    assert (memory["kinds"], memory["authors"]) == ({"memory": 1}, {HUMAN: 0, MACHINE: 1})
    plans = manifest["providers"]["agentplans"]
    assert (plans["kinds"], plans["authors"]) == ({"plan": 1}, {HUMAN: 0, MACHINE: 1})
    family_names = ["documents", "agentmemory", "agentplans"]
    assert sum(len(list(iter_store(archive_env.paths, [name]))) for name in family_names) == 4


def test_the_library_pages_join_chatgpt_but_a_plain_chat_is_not_bridged(
    archive_env, library_root: Path
) -> None:
    base = ingest(
        export_root=archive_env.export,
        paths=archive_env.paths,
        authorization=archive_env.auth,
        providers=["chatgpt"],
    )["providers"]["chatgpt"]
    manifest = _ingest(archive_env, library_root, ["chatgpt"])
    info = manifest["providers"]["chatgpt"]
    assert info["conversations"] == base["conversations"] + 1
    assert info["kinds"].get("page") == 1
    assert info["reader_counters"]["library_records"] == 1
    assert reconcile_source("chatgpt", info) == []
    assert "chatgpt-chat-1" not in {c.uid for c in iter_store(archive_env.paths, ["chatgpt"])}


def test_the_manifest_binds_the_generation_and_holds_the_ingest_to_its_counts(
    archive_env, library_root: Path
) -> None:
    manifest = _ingest(archive_env, library_root, ["codex"])
    info = manifest["providers"]["codex"]
    read = "\n".join(info["channels"]["read"])
    assert GENERATION in read and "r" * 64 in read and "p" * 64 in read
    assert set(info["source_files"]) == {"current.json"}
    assert info["reader_counters"]["library_records"] == info["reader_counters"]["bridge_records"]
    broken = {**info, "conversations": info["conversations"] - 1}
    problems = reconcile_source("codex", broken)
    assert problems and "library record" in problems[0]
    short = {
        **info,
        "reader_counters": {**info["reader_counters"], "bridge_messages": 1},
    }
    assert "read 1 messages of the" in reconcile_source("codex", short)[0]


def test_a_family_without_a_library_is_absent_not_an_error(archive_env, tmp_path: Path) -> None:
    manifest = ingest(
        export_root=archive_env.export,
        paths=archive_env.paths,
        authorization=archive_env.auth,
        providers=["codex"],
        library_root=tmp_path / "no-library-here",
    )
    assert manifest["providers"]["codex"]["status"] == "absent"


def test_merge_keeps_the_providers_a_call_does_not_read(archive_env, library_root: Path) -> None:
    _ingest(archive_env, library_root, ["codex"])
    merged = _ingest(archive_env, library_root, ["applenotes"], merge=True)
    ok = {name for name, info in merged["providers"].items() if info["status"] == "ok"}
    assert ok == {"codex", "applenotes"}
    assert merged["totals"]["conversations"] == 1 + 1
    assert load_manifest(archive_env.paths) == merged
    replaced = _ingest(archive_env, library_root, ["applenotes"])
    assert set(replaced["providers"]) == {"applenotes"}


def test_merge_keeps_the_reader_version_a_provider_was_read_with(
    archive_env, library_root: Path
) -> None:
    _ingest(archive_env, library_root, ["codex"])
    stored = json.loads(archive_env.paths.manifest.read_text(encoding="utf-8"))
    stored["providers"]["codex"]["reader"] = "archive_read_library@0"
    archive_env.paths.manifest.write_text(json.dumps(stored), encoding="utf-8")
    merged = _ingest(archive_env, library_root, ["applenotes"], merge=True)
    assert merged["reader_versions"]["codex"] == "archive_read_library@0"
    assert merged["reader_versions"]["applenotes"] == bridge.READER


def test_chatgpt_reads_its_export_alone_when_the_library_root_is_not_a_library(
    archive_env, tmp_path: Path
) -> None:
    info = ingest(
        export_root=archive_env.export,
        paths=archive_env.paths,
        authorization=archive_env.auth,
        providers=["chatgpt"],
        library_root=tmp_path / "nowhere",
    )["providers"]["chatgpt"]
    assert info["status"] == "ok" and "page" not in info["kinds"]


def test_the_reader_counters_group_the_basis_by_author() -> None:
    grouped = bridge.counter_report(
        {"basis:human:typed": 3, "basis:machine:subagent-prompt": 2, "bridge_records": 9}
    )
    assert grouped == {"human": {"typed": 3}, "machine": {"subagent-prompt": 2}}


def test_family_reader_follows_the_provider_reader_contract(library_root: Path) -> None:
    reader = bridge.reader_for("agentmemory")
    assert reader.READER == bridge.READER and reader.detect(library_root)
    described = reader.channels(library_root)
    assert described["read"][0].startswith("1 library record(s), 1 message(s)")
    tracker = SourceTracker(library_root, reader.READER)
    (conversation,) = reader.iter_conversations(library_root, tracker)
    assert conversation.provider == "agentmemory" and conversation.kind == "memory"
    assert tracker.counters["bridge_records"] == 1


def test_a_library_record_no_family_reads_is_counted_and_fails_verify(
    archive_env, library_root: Path
) -> None:
    database = library_root / GENERATION / "library.sqlite"
    with Library(database, writable=True) as library:
        # A document source with a wing of its own and the default kind: no family selects it.
        library.put(
            _conversation(
                "documents",
                "file-orphan",
                [_turn("user", "text nobody reads", "project", 1)],
                kind="document",
                wing="ai",
                lane="support",
            )
        )
    assert bridge.unclaimed(library_root) == 1
    assert any(
        "belong to no family" in item
        for item in bridge.describe(library_root, bridge.FAMILY_BY_NAME["codex"])["not_read"]
    )
    info = _ingest(archive_env, library_root, ["codex"])["providers"]["codex"]
    assert info["reader_counters"]["library_unclaimed"] == 1
    (problem,) = reconcile_source("codex", info)
    assert problem == "1 library record(s) belong to no family and were not read"


def test_a_damaged_library_fails_that_provider_and_not_the_whole_ingest(
    archive_env, library_root: Path
) -> None:
    ingest(
        export_root=archive_env.export,
        paths=archive_env.paths,
        authorization=archive_env.auth,
        providers=["chatgpt"],
    )
    (library_root / GENERATION / "library.sqlite").write_bytes(b"this is not a database" * 100)
    manifest = _ingest(archive_env, library_root, ["codex", "applenotes"], merge=True)
    for name in ("codex", "applenotes"):
        info = manifest["providers"][name]
        assert info["status"] == "failed" and "DatabaseError" in info["error"]
        assert info["channels"]["not_read"][0].startswith("source unreadable")
    # The provider read earlier keeps its entry; the manifest was written.
    assert manifest["providers"]["chatgpt"]["status"] == "ok"
    assert load_manifest(archive_env.paths) == manifest


def test_chatgpt_pages_are_read_even_when_the_export_has_no_chatgpt_folder(
    archive_env, library_root: Path, tmp_path: Path
) -> None:
    empty_export = tmp_path / "empty-export"
    empty_export.mkdir()
    manifest = ingest(
        export_root=empty_export,
        paths=archive_env.paths,
        authorization=archive_env.auth,
        providers=["chatgpt"],
        library_root=library_root,
    )
    info = manifest["providers"]["chatgpt"]
    assert info["status"] == "ok" and info["conversations"] == 1 and info["kinds"] == {"page": 1}
    assert reconcile_source("chatgpt", info) == []


def test_a_library_built_in_the_archives_own_store_is_not_an_ancestor_of_it(
    tmp_path: Path,
) -> None:
    store = tmp_path / "shared-store"
    database = store / "library-all" / GENERATION / "library.sqlite"
    database.parent.mkdir(parents=True)
    with Library.create(database) as library:
        library.put(_records()[0])
    (store / "library-all" / "current.json").write_text(
        json.dumps({"generation": GENERATION}), encoding="utf-8"
    )
    project = tmp_path / "project"
    project.mkdir()
    register_corpus_store(project, store)
    exports = tmp_path / "exports"
    exports.mkdir()
    # The store holds both the library and the archive: naming the store as the library root
    # must not make custody see the whole store as a source that contains the destination.
    context = open_context(
        project_root=project,
        corpus_store_root=store,
        archive_root=None,
        export_root=exports,
        library_root=store,
    )
    assert context.paths.root == (store / "archive-all").resolve()
    assert context.library_root == store


def test_a_snippet_is_the_first_thing_the_person_wrote_not_a_harness_turn(
    archive_env, library_root: Path
) -> None:
    _ingest(archive_env, library_root, ["codex", "claudecode"])
    by_uid = {c.uid: c for c in iter_store(archive_env.paths, ["codex", "claudecode"])}
    assert by_uid["codex-cx-1"].index_row()["snippet"] == "run the tests"
    assert by_uid["claudecode-root-1"].index_row()["snippet"] == "please refactor the parser"
    # A subagent session has no human turn at all, so it has no snippet to borrow.
    assert by_uid["claudecode-sub-1"].index_row()["snippet"] == ""
