"""The corpus projection: the normalized archive, shaped as corpora the federation accepts.

Every string is invented. The two-provider archive comes from `cce archive fixture`; the
single-purpose stores are written directly in the normalized layer.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from archive_support import conv, write_store

from conversation_corpus_engine.archive_corpus import (
    ALL_COLLECTIONS,
    OPTIONAL_COLLECTIONS,
    REQUIRED_COLLECTIONS,
    CorpusProjectionError,
    project_corpus,
)
from conversation_corpus_engine.archive_fixture import EDITED_ASK, SHARED_ASK, write_fixture
from conversation_corpus_engine.archive_ingest import (
    DEFAULT_ARCHIVE_DIRNAME,
    MANIFEST_SCHEMA,
    ArchivePaths,
    authorize_archive,
    ingest,
    load_manifest,
)
from conversation_corpus_engine.archive_model import (
    Message,
    iter_conversations,
    write_json_atomic,
)
from conversation_corpus_engine.archive_naming import NAMING_VERSION, parse_name, validate_name
from conversation_corpus_engine.cli import main
from conversation_corpus_engine.corpus_store import register_corpus_store
from conversation_corpus_engine.federation import (
    build_federation,
    list_registered_corpora,
    upsert_corpus,
    validate_corpus_root,
)
from conversation_corpus_engine.schema_validation import validate_json_file
from conversation_corpus_engine.sharded_collection import (
    collection_storage_path,
    load_corpus_collection,
)

# Row fields that hold an id, whatever the collection.
ID_FIELDS = (
    "thread_uid",
    "family_id",
    "pair_id",
    "action_key",
    "question_key",
    "canonical_family_id",
    "canonical_thread_uid",
    "canonical_entity_id",
    "conversation_id",
)
ID_LIST_FIELDS = ("thread_uids", "family_ids")
PROVIDERS = ("chatgpt", "claude")


@pytest.fixture
def fixture_archive(tmp_path: Path):
    """The synthetic fixture exports, ingested into a registered private store."""
    export = tmp_path / "export"
    write_fixture(export)
    project = tmp_path / "project"
    store = tmp_path / "store"
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
    return SimpleNamespace(paths=paths, manifest=manifest, tmp=tmp_path)


def _hand_built_store(tmp_path: Path, conversations, fingerprint: str = "f" * 64) -> ArchivePaths:
    """A normalized store written directly, with a manifest that states its true counts."""
    paths = ArchivePaths(tmp_path / "archive-all")
    write_store(paths, conversations)
    providers: dict[str, Any] = {}
    for provider in paths.providers_present():
        items = list(iter_conversations(paths.conversations(provider)))
        providers[provider] = {
            "status": "ok",
            "conversations": len(items),
            "messages": sum(item.message_count for item in items),
            "last_activity_max": max(item.last_activity for item in items),
        }
    write_json_atomic(
        paths.manifest,
        {
            "schema": MANIFEST_SCHEMA,
            "schema_version": 2,
            "naming_version": NAMING_VERSION,
            "reader_versions": {name: "test@1" for name in providers},
            "source_fingerprint": fingerprint,
            "providers": providers,
            "totals": {},
        },
    )
    return paths


def _rows(corpus: Path, name: str) -> list[dict[str, Any]]:
    return load_corpus_collection(corpus / "corpus" / name, default=[]) or []


def _ids(corpus: Path) -> list[str]:
    found: list[str] = []
    for name in ALL_COLLECTIONS:
        for row in _rows(corpus, name):
            found.extend(str(row[field]) for field in ID_FIELDS if row.get(field))
            for field in ID_LIST_FIELDS:
                found.extend(str(item) for item in row.get(field, []))
    return found


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _cce(monkeypatch, capsys, *argv: str) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "argv", ["cce", *argv])
    code = 0
    try:
        main()
    except SystemExit as exit_:
        code = int(exit_.code or 0)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --- what is written ---------------------------------------------------------------------------


def test_each_provider_gets_a_corpus_with_all_nine_collections(fixture_archive) -> None:
    out = fixture_archive.tmp / "corpora"
    summary = project_corpus(fixture_archive.paths.root, out)
    assert list(summary) == list(PROVIDERS)
    assert (len(REQUIRED_COLLECTIONS), len(OPTIONAL_COLLECTIONS)) == (5, 4)
    for provider in PROVIDERS:
        name = f"corpus-{provider}-archive"
        assert summary[provider]["status"] == "written"
        assert summary[provider]["corpus_id"] == name
        corpus = out / name
        for filename in ALL_COLLECTIONS:
            logical = corpus / "corpus" / filename
            assert collection_storage_path(logical).is_dir(), filename
            assert _rows(corpus, filename), f"{provider} {filename} is empty"
        assert validate_corpus_root(corpus)["valid"]
    assert sorted(item.name for item in out.iterdir()) == [
        "corpus-chatgpt-archive",
        "corpus-claude-archive",
    ]


def test_the_contract_names_the_folder_and_validates(fixture_archive) -> None:
    out = fixture_archive.tmp / "corpora"
    project_corpus(fixture_archive.paths.root, out)
    manifest = load_manifest(fixture_archive.paths)
    for provider in PROVIDERS:
        corpus = out / f"corpus-{provider}-archive"
        contract_path = corpus / "corpus" / "contract.json"
        verdict = validate_json_file("corpus-contract", contract_path)
        assert verdict["valid"], verdict["errors"]
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        assert contract["corpus_id"] == corpus.name
        assert contract["contract_name"] == "conversation-corpus-engine.v2"
        assert contract["adapter_type"] == "archive-normalized"
        assert contract["source_content_fingerprint"] == manifest["source_fingerprint"]
        assert contract["naming_version"] == NAMING_VERSION
        assert contract["generated_at"] == manifest["providers"][provider]["last_activity_max"]
        assert contract["counts"]["threads"] == len(_rows(corpus, "threads-index.json"))
        assert contract["counts"]["pairs"] == len(_rows(corpus, "pairs-index.json"))
        assert contract["counts"]["actions"] == len(_rows(corpus, "action-ledger.json"))
        assert contract["required_files"] == [
            f"corpus/{name.removesuffix('.json')}.collection" for name in REQUIRED_COLLECTIONS
        ]


def test_upsert_and_build_federation_accept_both_corpora(fixture_archive) -> None:
    out = fixture_archive.tmp / "corpora"
    summary = project_corpus(fixture_archive.paths.root, out)
    federation_root = fixture_archive.tmp / "federation-project"
    federation_root.mkdir()
    for provider in PROVIDERS:
        name = f"corpus-{provider}-archive"
        entry = upsert_corpus(federation_root, out / name, corpus_id=name)
        assert entry["corpus_id"] == name
    assert [e["corpus_id"] for e in list_registered_corpora(federation_root)] == [
        f"corpus-{provider}-archive" for provider in PROVIDERS
    ]
    build_federation(federation_root)
    index = {
        item["corpus_id"]: item
        for item in load_corpus_collection(
            federation_root / "federation" / "corpora-summary.json", default=[]
        )
    }
    for provider in PROVIDERS:
        item = index[f"corpus-{provider}-archive"]
        assert item["valid_contract"] and not item["missing_files"]
        assert item["thread_count"] == summary[provider]["threads"] > 0
        assert item["family_count"] == summary[provider]["families"] > 0
        assert item["action_count"] == summary[provider]["actions"] > 0
        assert item["unresolved_count"] == summary[provider]["unresolved"] > 0
        assert item["entity_count"] == summary[provider]["entities"] > 0
        assert item["adapter_type"] == "archive-normalized"
        verdict = validate_json_file(
            "corpus-contract", out / f"corpus-{provider}-archive" / "corpus" / "contract.json"
        )
        assert verdict["valid"], verdict["errors"]  # federation rewrites it; it still validates
    federated = load_corpus_collection(
        federation_root / "federation" / "families-index.json", default=[]
    )
    assert len(federated) == sum(summary[p]["families"] for p in PROVIDERS)


def test_federating_the_projected_corpora_leaves_the_archive_manifest_unchanged(
    fixture_archive,
) -> None:
    out = fixture_archive.tmp / "corpora"
    project_corpus(fixture_archive.paths.root, out)
    federation_root = fixture_archive.tmp / "federation-project"
    federation_root.mkdir()
    for provider in PROVIDERS:
        name = f"corpus-{provider}-archive"
        upsert_corpus(federation_root, out / name, corpus_id=name)
    manifest_bytes = fixture_archive.paths.manifest.read_bytes()
    result = build_federation(federation_root, archive_root=fixture_archive.paths.root)
    recorded = result["archive_manifest_fingerprint"]
    assert recorded["source_fingerprint"] == fixture_archive.manifest["source_fingerprint"]
    assert fixture_archive.paths.manifest.read_bytes() == manifest_bytes


# --- names -------------------------------------------------------------------------------------


def test_every_id_is_a_valid_contract_name_of_the_right_kind(fixture_archive) -> None:
    out = fixture_archive.tmp / "corpora"
    project_corpus(fixture_archive.paths.root, out)
    expected = {
        "thread_uid": "thread",
        "canonical_thread_uid": "thread",
        "conversation_id": None,
        "family_id": "family",
        "canonical_family_id": "family",
        "pair_id": "pair",
        "action_key": "action",
        "question_key": "question",
        "canonical_entity_id": "topic",
    }
    for provider in PROVIDERS:
        corpus = out / f"corpus-{provider}-archive"
        for name in ALL_COLLECTIONS:
            for row in _rows(corpus, name):
                for field, kind in expected.items():
                    if field not in row or kind is None:
                        continue
                    assert not validate_name(row[field]), (name, field, row[field])
                    parts = parse_name(row[field])
                    assert parts.kind == kind
                    assert parts.scope == ("all" if kind == "topic" else provider)


def test_ids_hold_no_title_and_do_not_move_when_a_title_changes(tmp_path: Path) -> None:
    def store(label: str, root: Path) -> ArchivePaths:
        texts = [
            ("user", "We should draft the migration checklist for the zebra herd."),
            ("assistant", "I will draft the checklist and list the herd routes."),
            ("user", "Maybe the river crossing needs a second plan?"),
            ("assistant", "A second crossing plan is reasonable."),
        ]
        return _hand_built_store(
            root,
            [
                conv("chatgpt", "alpha-1", f"{label} first", texts),
                conv("claude", "beta-2", f"{label} second", texts[:2]),
            ],
        )

    first = store("Quartz planning", tmp_path / "one")
    second = store("Basalt planning", tmp_path / "two")
    out_one, out_two = tmp_path / "out-one", tmp_path / "out-two"
    project_corpus(first.root, out_one)
    project_corpus(second.root, out_two)
    titles_one = {
        r["title_normalized"]
        for r in _rows(out_one / "corpus-chatgpt-archive", "threads-index.json")
    }
    titles_two = {
        r["title_normalized"]
        for r in _rows(out_two / "corpus-chatgpt-archive", "threads-index.json")
    }
    assert titles_one == {"Quartz planning first"} and titles_two == {"Basalt planning first"}
    for provider in PROVIDERS:
        name = f"corpus-{provider}-archive"
        ids_one, ids_two = _ids(out_one / name), _ids(out_two / name)
        assert ids_one and ids_one == ids_two
        for item in ids_one:
            assert "quartz" not in item and "basalt" not in item and "planning" not in item
    # The ids that carry a digest are the same for the same text, whatever the title.
    one_actions = _rows(out_one / "corpus-chatgpt-archive", "action-ledger.json")
    two_actions = _rows(out_two / "corpus-chatgpt-archive", "action-ledger.json")
    assert [r["action_key"] for r in one_actions] == [r["action_key"] for r in two_actions]
    assert len(one_actions) >= 1


def test_the_same_sentence_is_one_action_and_a_different_one_is_not(tmp_path: Path) -> None:
    sentence = "We should replace the lantern wick before the weekend."
    other = "We should oil the hinges before the weekend."
    paths = _hand_built_store(
        tmp_path,
        [
            conv("chatgpt", "one-1", "One", [("user", sentence), ("assistant", "Noted.")]),
            conv("chatgpt", "two-2", "Two", [("user", sentence), ("assistant", other)]),
        ],
    )
    project_corpus(paths.root, tmp_path / "out")
    actions = _rows(tmp_path / "out" / "corpus-chatgpt-archive", "action-ledger.json")
    by_text = {row["canonical_action"]: row for row in actions}
    assert set(by_text) == {sentence, other}
    assert by_text[sentence]["occurrence_count"] == 2
    assert len(by_text[sentence]["thread_uids"]) == 2
    assert by_text[sentence]["action_key"] != by_text[other]["action_key"]
    assert len({row["action_key"] for row in actions}) == len(actions)


# --- what is read ------------------------------------------------------------------------------


def test_only_the_current_branch_typed_and_reply_text_is_read(fixture_archive) -> None:
    out = fixture_archive.tmp / "corpora"
    project_corpus(fixture_archive.paths.root, out)
    chatgpt = out / "corpus-chatgpt-archive"
    blob = "\n".join(json.dumps(row) for name in ALL_COLLECTIONS for row in _rows(chatgpt, name))
    assert SHARED_ASK in blob
    assert EDITED_ASK not in blob  # the off-branch edit stays in the archive
    claude = out / "corpus-claude-archive"
    claude_blob = "\n".join(
        json.dumps(row) for name in ALL_COLLECTIONS for row in _rows(claude, name)
    )
    assert "Considering wick length" not in claude_blob  # reasoning is not corpus text
    assert "Trim it weekly." in claude_blob


def test_attachment_text_tool_output_and_other_kinds_stay_out(tmp_path: Path) -> None:
    attached = conv(
        "claude", "attach-1", "Attach", [("user", "typed words here"), ("assistant", "ok")]
    )
    attached.messages[0].text += "\n\n[attachment: a.txt]\nATTACHED-BODY"
    attached.messages[0].typed_chars = len("typed words here")
    attached.messages.append(
        Message(role="tool", text="TOOL-OUTPUT", ts="2025-01-01T00:00:01Z", id="t1", channel="tool")
    )
    attached.messages.append(
        Message(
            role="assistant",
            text="THINKING-TEXT",
            ts="2025-01-01T00:00:02Z",
            id="t2",
            channel="thinking",
        )
    )
    memory = conv(
        "claude", "mem-1", "Memory", [("user", "MEMORY-TEXT"), ("assistant", "x")], kind="memory"
    )
    notebook = conv("claude", "nb-1", "Notebook", [("user", "NOTEBOOK-TEXT")], kind="notebook")
    blank = conv("claude", "blank-1", "Blank", [("user", "   ")])
    paths = _hand_built_store(tmp_path, [attached, memory, notebook, blank])
    summary = project_corpus(paths.root, tmp_path / "out")
    assert summary["claude"]["threads"] == 1
    assert summary["claude"]["conversations_seen"] == 4
    assert summary["claude"]["excluded"] == {"kind-memory": 1, "kind-notebook": 1, "no-text": 1}
    assert summary["claude"]["excluded_conversations"] == 3
    corpus = tmp_path / "out" / "corpus-claude-archive"
    blob = "\n".join(json.dumps(row) for name in ALL_COLLECTIONS for row in _rows(corpus, name))
    assert "typed words here" in blob
    for hidden in ("ATTACHED-BODY", "TOOL-OUTPUT", "THINKING-TEXT", "MEMORY-TEXT", "NOTEBOOK-TEXT"):
        assert hidden not in blob


def test_a_provider_with_no_eligible_conversation_writes_nothing(tmp_path: Path) -> None:
    memory = conv("claude", "mem-1", "Memory", [("user", "x"), ("assistant", "y")], kind="memory")
    chat = conv(
        "chatgpt", "chat-1", "Chat", [("user", "We should plan the route."), ("assistant", "Yes.")]
    )
    paths = _hand_built_store(tmp_path, [memory, chat])
    summary = project_corpus(paths.root, tmp_path / "out")
    assert summary["claude"]["status"] == "empty"
    assert summary["chatgpt"]["status"] == "written"
    assert [item.name for item in (tmp_path / "out").iterdir()] == ["corpus-chatgpt-archive"]


# --- repeatability and safety -----------------------------------------------------------------


def test_two_runs_write_identical_bytes(fixture_archive) -> None:
    first, second = fixture_archive.tmp / "one", fixture_archive.tmp / "two"
    project_corpus(fixture_archive.paths.root, first)
    project_corpus(fixture_archive.paths.root, second)
    assert _tree_bytes(first) == _tree_bytes(second)
    project_corpus(fixture_archive.paths.root, first)  # over its own output
    assert _tree_bytes(first) == _tree_bytes(second)


def test_a_rerun_replaces_the_corpus_and_leaves_no_staging_folder(tmp_path: Path) -> None:
    paths = _hand_built_store(
        tmp_path, [conv("chatgpt", "a-1", "A", [("user", "We should plan the route.")])]
    )
    out = tmp_path / "out"
    project_corpus(paths.root, out)
    stale = out / "corpus-chatgpt-archive" / "stale.txt"
    stale.write_text("left over", encoding="utf-8")
    project_corpus(paths.root, out)
    assert not stale.exists()
    assert sorted(item.name for item in out.iterdir()) == ["corpus-chatgpt-archive"]


def test_a_store_that_disagrees_with_its_manifest_is_refused_and_keeps_the_old_corpus(
    fixture_archive,
) -> None:
    out = fixture_archive.tmp / "corpora"
    project_corpus(fixture_archive.paths.root, out)
    before = _tree_bytes(out)
    manifest = load_manifest(fixture_archive.paths)
    manifest["providers"]["claude"]["messages"] += 1
    write_json_atomic(fixture_archive.paths.manifest, manifest)
    with pytest.raises(CorpusProjectionError, match="disagrees with the manifest"):
        project_corpus(fixture_archive.paths.root, out)
    assert _tree_bytes(out) == before


def test_a_missing_manifest_or_unready_provider_is_refused(tmp_path: Path, fixture_archive) -> None:
    empty = tmp_path / "empty-archive"
    empty.mkdir()
    with pytest.raises(CorpusProjectionError, match="No archive manifest"):
        project_corpus(empty, tmp_path / "out")
    with pytest.raises(CorpusProjectionError, match="no readable normalized store"):
        project_corpus(fixture_archive.paths.root, tmp_path / "out", ["grok"])


# --- the command line --------------------------------------------------------------------------


def _git(*argv: str, cwd: Path) -> None:
    subprocess.run(["git", "-C", str(cwd), *argv], check=True, capture_output=True)


def test_project_corpus_command_and_exit_codes(fixture_archive, monkeypatch, capsys) -> None:
    out = fixture_archive.tmp / "corpora"
    base = ["archive", "project", "corpus", "--root", str(fixture_archive.paths.root)]
    code, stdout, _ = _cce(monkeypatch, capsys, *base, "--out", str(out), "--provider", "claude")
    payload = json.loads(stdout)
    assert code == 0 and list(payload) == ["claude"] and payload["claude"]["threads"] == 2
    assert [item.name for item in out.iterdir()] == ["corpus-claude-archive"]
    assert "Lantern" not in stdout and "lantern" not in stdout  # counts only
    code, _, err = _cce(monkeypatch, capsys, *base[:4], str(out / "nowhere"), "--out", str(out))
    assert code == 2 and "archive root not found" in err
    manifest = load_manifest(fixture_archive.paths)
    manifest["providers"]["chatgpt"]["conversations"] += 1
    write_json_atomic(fixture_archive.paths.manifest, manifest)
    code, _, err = _cce(monkeypatch, capsys, *base, "--out", str(fixture_archive.tmp / "again"))
    assert code == 1 and "corpus not written" in err


def test_a_git_visible_destination_is_refused(fixture_archive, monkeypatch, capsys) -> None:
    work_tree = fixture_archive.tmp / "work"
    work_tree.mkdir()
    _git("init", "-q", cwd=work_tree)
    common = ["archive", "project", "corpus", "--root", str(fixture_archive.paths.root)]
    code, _, err = _cce(monkeypatch, capsys, *common, "--out", str(work_tree / "visible"))
    assert code == 2 and "Git-visible" in err
    assert not (work_tree / "visible").exists()
    (work_tree / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    code, out, _ = _cce(monkeypatch, capsys, *common, "--out", str(work_tree / "ignored"))
    assert code == 0 and set(json.loads(out)) == set(PROVIDERS)
