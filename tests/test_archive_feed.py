from __future__ import annotations

import hashlib
import inspect
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from archive_support import conv, write_store

from conversation_corpus_engine import archive_feed
from conversation_corpus_engine.archive_feed import (
    ALTERNATE_FLAG,
    FEED_PROVENANCE,
    ROW_FIELDS,
    FeedError,
    feed_source,
    project_feed,
)
from conversation_corpus_engine.archive_fixture import SHARED_ASK, expected_counts, write_fixture
from conversation_corpus_engine.archive_ingest import (
    DEFAULT_ARCHIVE_DIRNAME,
    LEGACY_MANIFEST_SCHEMA,
    MANIFEST_SCHEMA,
    ArchivePaths,
    authorize_archive,
    ingest,
    latest_activity_day,
    load_manifest,
)
from conversation_corpus_engine.archive_model import SCHEMA as CONVERSATION_SCHEMA
from conversation_corpus_engine.archive_model import iter_conversations, write_json_atomic
from conversation_corpus_engine.archive_naming import NAMING_VERSION, verify_names
from conversation_corpus_engine.cli import main
from conversation_corpus_engine.corpus_store import register_corpus_store

# What the downstream ledger reads from one event row. Limen is not importable here, so the field
# list of `limen.prompt_corpus.occurrence_from_event` is replicated: it reads exactly these keys,
# and derives the occurrence id from source, session_ref, event_ref, text_index and the text hash.
LEDGER_READS = (
    "text",
    "source",
    "session_ref",
    "event_index",
    "text_index",
    "event_ref",
    "source_locator",
    "timestamp",
    "body_kind",
    "provenance",
)
LEDGER_PROVENANCE_KINDS = {"operator_typed", "unknown_user_input"}
# The feed row keys the ledger's allowlist names, spelled out here and nowhere else. `branch` is
# the one optional key and rides on off-branch rows only.
FROZEN_ROW_KEYS = frozenset(
    {
        "body_kind",
        "event_index",
        "event_ref",
        "provenance",
        "session_ref",
        "source",
        "source_locator",
        "text",
        "text_index",
        "timestamp",
    }
)


def ledger_occurrence(event: dict[str, Any]) -> dict[str, Any]:
    """The part of `occurrence_from_event` the feed has to satisfy, replicated."""
    text = str(event.get("text") or "")
    source = str(event.get("source") or "unknown")
    session_ref = str(event.get("session_ref") or "unknown")
    event_index = event.get("event_index")
    assert isinstance(event_index, int) and not isinstance(event_index, bool)
    text_index = event.get("text_index")
    assert isinstance(text_index, int) and not isinstance(text_index, bool)
    event_ref = str(event.get("event_ref") or event_index or "0")
    prompt_hash = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    provenance = str(event.get("provenance") or "unknown_user_input")
    assert provenance in LEDGER_PROVENANCE_KINDS
    key = "\0".join([source, session_ref, event_ref, str(text_index), prompt_hash])
    return {
        "occurrence_id": "po-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24],
        "prompt_hash": prompt_hash,
        "authority": "operator" if provenance == "operator_typed" else "unknown",
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


def _read_feed(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


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
    return SimpleNamespace(
        export=export, project=project, store=store, paths=paths, manifest=manifest, tmp=tmp_path
    )


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
            "conversation_schema": CONVERSATION_SCHEMA,
            "schema_version": 2,
            "naming_version": NAMING_VERSION,
            "reader_versions": {name: "test@1" for name in providers},
            "source_fingerprint": fingerprint,
            "providers": providers,
            "totals": {},
        },
    )
    return paths


# --- the fixture command -----------------------------------------------------------------------


def test_the_fixture_is_the_same_bytes_every_time(tmp_path: Path) -> None:
    first = write_fixture(tmp_path / "one")
    second = write_fixture(tmp_path / "two")
    assert _tree_bytes(tmp_path / "one") == _tree_bytes(tmp_path / "two")
    assert first["files"] == second["files"]
    assert first["files"] == [
        "chatgpt/conversations-000.json",
        "claude/conversations.json",
        "claude/memories-000.zip",
        "claude/projects-000.zip",
        "fixture-all.json",
    ]
    assert first["shared_ask_providers"] == ["chatgpt", "claude"]
    assert first["providers"] == expected_counts()
    assert expected_counts() == {
        "chatgpt": {"asks": 4, "alternate_asks": 1},
        "claude": {"asks": 2, "alternate_asks": 0},
    }


def test_the_fixture_command_writes_the_exports(tmp_path: Path, monkeypatch, capsys) -> None:
    code, out, _ = _cce(monkeypatch, capsys, "archive", "fixture", "--out", str(tmp_path / "x"))
    payload = json.loads(out)
    assert code == 0 and payload["providers"] == expected_counts()
    assert (tmp_path / "x" / "chatgpt" / "conversations-000.json").is_file()
    assert (tmp_path / "x" / "claude" / "conversations.json").is_file()


# --- the feed ----------------------------------------------------------------------------------


def test_feed_rows_carry_exactly_what_the_ledger_reads(fixture_archive) -> None:
    out = fixture_archive.tmp / "feeds"
    project_feed(fixture_archive.paths.root, out)
    expected = expected_counts()
    occurrences: dict[str, dict[str, Any]] = {}
    for provider in ("chatgpt", "claude"):
        rows = _read_feed(out / f"feed-{provider}-prompts.jsonl")
        assert len(rows) == expected[provider]["asks"]
        uids = {c.uid for c in iter_conversations(fixture_archive.paths.conversations(provider))}
        for index, row in enumerate(rows):
            allowed = set(ROW_FIELDS) | ({"branch"} if row.get("branch") else set())
            assert set(row) == allowed
            assert set(LEDGER_READS) <= set(row)
            assert row["source"] == feed_source(provider) == f"archive-{provider}-messages"
            assert row["session_ref"] in uids
            assert row["event_index"] == index and row["text_index"] == 0
            assert row["source_locator"] == f"{row['session_ref']}#{row['event_ref']}"
            assert row["body_kind"] == "direct"
            assert row["provenance"] == FEED_PROVENANCE == "unknown_user_input"
            assert row["event_ref"].startswith(f"message-{provider}-")
            occurrence = ledger_occurrence(row)
            assert occurrence["authority"] == "unknown"
            assert occurrence["occurrence_id"] not in occurrences
            occurrences[occurrence["occurrence_id"]] = occurrence
        alternates = [row for row in rows if row.get("branch")]
        assert len(alternates) == expected[provider]["alternate_asks"]
        assert all(row["branch"] == ALTERNATE_FLAG for row in alternates)
    assert len(occurrences) == sum(item["asks"] for item in expected.values())


def test_the_row_key_set_is_exactly_the_frozen_list(fixture_archive) -> None:
    """Limen allowlists these keys by name: a key added, renamed or dropped here must fail loudly."""
    out = fixture_archive.tmp / "feeds"
    project_feed(fixture_archive.paths.root, out)
    assert set(ROW_FIELDS) == FROZEN_ROW_KEYS and len(ROW_FIELDS) == len(FROZEN_ROW_KEYS)
    seen_keys: set[str] = set()
    for provider in ("chatgpt", "claude"):
        for row in _read_feed(out / f"feed-{provider}-prompts.jsonl"):
            seen_keys |= set(row)
            assert set(row) - {"branch"} == FROZEN_ROW_KEYS
    assert seen_keys == FROZEN_ROW_KEYS | {"branch"}


def test_one_ask_shared_by_two_providers_hashes_the_same(fixture_archive) -> None:
    out = fixture_archive.tmp / "feeds"
    project_feed(fixture_archive.paths.root, out)
    shared = hashlib.sha256(SHARED_ASK.encode("utf-8")).hexdigest()
    holders = {}
    for provider in ("chatgpt", "claude"):
        hashes = [
            ledger_occurrence(row)["prompt_hash"]
            for row in _read_feed(out / f"feed-{provider}-prompts.jsonl")
        ]
        holders[provider] = hashes.count(shared)
    assert holders == {"chatgpt": 1, "claude": 1}


def test_alternate_asks_follow_the_current_branch_inside_their_conversation(
    fixture_archive,
) -> None:
    out = fixture_archive.tmp / "feeds"
    project_feed(fixture_archive.paths.root, out, ["chatgpt"])
    rows = _read_feed(out / "feed-chatgpt-prompts.jsonl")
    by_session: dict[str, list[str]] = {}
    for row in rows:
        by_session.setdefault(row["session_ref"], []).append(row.get("branch", "current"))
    assert by_session["chatgpt-fixture-lantern-1"] == ["current", "alternate"]
    assert by_session["chatgpt-fixture-hammock-1"] == ["current", "current"]


def test_two_runs_write_identical_bytes(fixture_archive) -> None:
    root = fixture_archive.paths.root
    project_feed(root, fixture_archive.tmp / "one")
    project_feed(root, fixture_archive.tmp / "two")
    project_feed(root, fixture_archive.tmp / "one")
    assert _tree_bytes(fixture_archive.tmp / "one") == _tree_bytes(fixture_archive.tmp / "two")
    assert len(_tree_bytes(fixture_archive.tmp / "one")) == 4


def test_the_receipt_reconciles_with_the_feed_and_the_archive(fixture_archive) -> None:
    out = fixture_archive.tmp / "feeds"
    summary = project_feed(fixture_archive.paths.root, out)
    manifest = load_manifest(fixture_archive.paths)
    day = latest_activity_day(manifest)
    assert day == "20251013"
    for provider in ("chatgpt", "claude"):
        feed = out / f"feed-{provider}-prompts.jsonl"
        receipt_path = fixture_archive.paths.receipt(provider, manifest["source_fingerprint"], day)
        receipt = json.loads((out / receipt_path.name).read_text(encoding="utf-8"))
        assert summary[provider]["receipt"] == receipt_path.name
        assert receipt["feed_sha256"] == hashlib.sha256(feed.read_bytes()).hexdigest()
        assert receipt["rows"] == len(_read_feed(feed))
        assert receipt["source_fingerprint"] == manifest["source_fingerprint"]
        assert receipt["naming_version"] == NAMING_VERSION
        assert receipt["reader_versions"] == manifest["reader_versions"]
        assert receipt["day"] == day and receipt["provenance"] == FEED_PROVENANCE
        assert receipt["source"] == feed_source(provider)
        assert receipt["messages_seen"] == receipt["rows"] + sum(receipt["excluded"].values())
        assert receipt["conversations_seen"] == manifest["providers"][provider]["conversations"]
    chatgpt = json.loads(
        (
            out / fixture_archive.paths.receipt("chatgpt", manifest["source_fingerprint"], day).name
        ).read_text(encoding="utf-8")
    )
    claude = json.loads(
        (
            out / fixture_archive.paths.receipt("claude", manifest["source_fingerprint"], day).name
        ).read_text(encoding="utf-8")
    )
    assert chatgpt["alternate_rows"] == 1 and chatgpt["excluded"] == {"role-assistant": 4}
    assert chatgpt["messages_seen"] == manifest["providers"]["chatgpt"]["messages"] + 2
    assert claude["alternate_rows"] == 0
    assert claude["excluded"] == {
        "empty-text": 1,
        "kind-memory": 3,
        "kind-notebook": 2,
        "role-assistant": 3,
        "role-tool": 1,
    }
    assert claude["conversations_by_kind"] == {"chat": 2, "memory": 1, "notebook": 1}
    assert claude["messages_seen"] == manifest["providers"]["claude"]["messages"]


def test_the_fixture_exercises_every_exclusion_path_and_each_receipt_reconciles(
    fixture_archive,
) -> None:
    out = fixture_archive.tmp / "feeds"
    project_feed(fixture_archive.paths.root, out)
    reasons: set[str] = set()
    for provider in ("chatgpt", "claude"):
        (path,) = out.glob(f"receipt-{provider}-prompts-*.json")
        receipt = json.loads(path.read_text(encoding="utf-8"))
        # `rows` already counts the alternate-branch rows, so they are not added a second time
        assert receipt["rows"] + sum(receipt["excluded"].values()) == receipt["messages_seen"]
        assert 0 <= receipt["alternate_rows"] <= receipt["rows"]
        reasons |= set(receipt["excluded"])
        if provider == "chatgpt":
            assert receipt["alternate_rows"] > 0
    assert {"empty-text", "kind-memory", "kind-notebook", "role-assistant", "role-tool"} <= reasons


def test_a_receipt_holds_no_conversation_text(fixture_archive) -> None:
    out = fixture_archive.tmp / "feeds"
    project_feed(fixture_archive.paths.root, out)
    for receipt in out.glob("receipt-*.json"):
        text = receipt.read_text(encoding="utf-8")
        assert SHARED_ASK not in text and "lantern" not in text.lower()


def test_the_writer_has_no_provenance_parameter() -> None:
    for function in (project_feed, archive_feed._stage_provider):
        assert not [name for name in inspect.signature(function).parameters if "proven" in name]


def test_a_repeated_event_ref_aborts_and_leaves_nothing_behind(tmp_path: Path) -> None:
    paths = _hand_built_store(tmp_path, [conv("chatgpt", "same-1", "Same", [("user", "an ask")])])
    path = paths.conversations("chatgpt")
    path.write_text(path.read_text(encoding="utf-8") * 2, encoding="utf-8")
    out = tmp_path / "out"
    with pytest.raises(FeedError, match="repeated event_ref"):
        project_feed(paths.root, out)
    assert [item.name for item in out.iterdir()] == []


def test_a_store_that_disagrees_with_its_manifest_is_refused(fixture_archive) -> None:
    manifest = load_manifest(fixture_archive.paths)
    manifest["providers"]["claude"]["messages"] += 1
    write_json_atomic(fixture_archive.paths.manifest, manifest)
    out = fixture_archive.tmp / "feeds"
    with pytest.raises(FeedError, match="disagrees with the manifest"):
        project_feed(fixture_archive.paths.root, out)
    assert list(out.iterdir()) == []


def test_only_the_typed_part_of_an_ask_enters_the_feed(tmp_path: Path) -> None:
    attached = conv("claude", "attach-1", "Attach", [("user", "typed words"), ("assistant", "ok")])
    attached.messages[0].text += "\n\n[attachment: a.txt]\nattached body"
    attached.messages[0].typed_chars = len("typed words")
    search = conv("perplexity", "search-1", "Search", [("user", "a query")], kind="search")
    activity = conv("gemini", "act-1", "Activity", [("user", "a prompt")], kind="activity")
    blank = conv("grok", "blank-1", "Blank", [("user", "   ")])
    paths = _hand_built_store(tmp_path, [attached, search, activity, blank])
    out = tmp_path / "out"
    project_feed(paths.root, out)
    (row,) = _read_feed(out / "feed-claude-prompts.jsonl")
    assert row["text"] == "typed words"
    receipt = next(out.glob("receipt-claude-prompts-*.json"))
    assert json.loads(receipt.read_text(encoding="utf-8"))["attachment_text_trimmed"] == 1
    assert _read_feed(out / "feed-perplexity-prompts.jsonl")[0]["text"] == "a query"
    assert _read_feed(out / "feed-gemini-prompts.jsonl")[0]["text"] == "a prompt"
    assert _read_feed(out / "feed-grok-prompts.jsonl") == []
    blank_receipt = json.loads(
        next(out.glob("receipt-grok-prompts-*.json")).read_text(encoding="utf-8")
    )
    assert blank_receipt["rows"] == 0 and blank_receipt["excluded"] == {"empty-text": 1}


def test_a_new_fingerprint_replaces_the_old_receipt(tmp_path: Path) -> None:
    store = [conv("chatgpt", "r-1", "R", [("user", "one ask")])]
    paths = _hand_built_store(tmp_path, store, fingerprint="a" * 64)
    out = tmp_path / "out"
    project_feed(paths.root, out)
    first = sorted(item.name for item in out.glob("receipt-*.json"))
    manifest = load_manifest(paths)
    manifest["source_fingerprint"] = "b" * 64
    write_json_atomic(paths.manifest, manifest)
    project_feed(paths.root, out)
    second = sorted(item.name for item in out.glob("receipt-*.json"))
    assert len(first) == len(second) == 1 and first != second


def _make_legacy(paths: ArchivePaths) -> None:
    """Rewrite the manifest as a first-generation one; the conversation rows stay as they are."""
    manifest = load_manifest(paths)
    manifest["schema"] = LEGACY_MANIFEST_SCHEMA
    manifest.pop("conversation_schema")
    write_json_atomic(paths.manifest, manifest)


def test_a_first_generation_store_is_refused_with_a_rebuild_message(fixture_archive) -> None:
    _make_legacy(fixture_archive.paths)
    assert (
        load_manifest(fixture_archive.paths)["schema"] == LEGACY_MANIFEST_SCHEMA
    )  # still readable
    out = fixture_archive.tmp / "feeds"
    with pytest.raises(FeedError, match="cce archive build"):
        project_feed(fixture_archive.paths.root, out)
    assert not out.exists()


def test_a_first_generation_store_exits_nonzero_from_the_command(
    fixture_archive, monkeypatch, capsys
) -> None:
    _make_legacy(fixture_archive.paths)
    base = ["archive", "project", "feed", "--root", str(fixture_archive.paths.root)]
    code, _, err = _cce(monkeypatch, capsys, *base, "--out", str(fixture_archive.tmp / "feeds"))
    assert code == 1 and "feed not written" in err and "cce archive build" in err


def test_the_feed_names_are_valid_contract_names(fixture_archive) -> None:
    project_feed(fixture_archive.paths.root, fixture_archive.paths.root)
    report = verify_names(fixture_archive.paths.root)
    assert report["ok"], report["problems"]


def test_a_missing_manifest_or_unknown_provider_is_refused(tmp_path: Path, fixture_archive) -> None:
    empty = tmp_path / "empty-archive"
    empty.mkdir()
    with pytest.raises(FeedError, match="No archive manifest"):
        project_feed(empty, tmp_path / "out")
    with pytest.raises(FeedError, match="no readable normalized store"):
        project_feed(fixture_archive.paths.root, tmp_path / "out", ["grok"])


# --- keeping private text out of Git -----------------------------------------------------------


def _git(*argv: str, cwd: Path) -> None:
    subprocess.run(["git", "-C", str(cwd), *argv], check=True, capture_output=True)


def test_a_git_visible_destination_is_refused(fixture_archive, monkeypatch, capsys) -> None:
    work_tree = fixture_archive.tmp / "work"
    work_tree.mkdir()
    _git("init", "-q", cwd=work_tree)
    common = ["archive", "project", "feed", "--root", str(fixture_archive.paths.root)]
    code, _, err = _cce(monkeypatch, capsys, *common, "--out", str(work_tree / "visible"))
    assert code == 2 and "Git-visible" in err
    assert not (work_tree / "visible").exists()
    (work_tree / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    code, out, _ = _cce(monkeypatch, capsys, *common, "--out", str(work_tree / "ignored"))
    assert code == 0 and set(json.loads(out)) == {"chatgpt", "claude"}
    assert (work_tree / "ignored" / "feed-chatgpt-prompts.jsonl").is_file()


# --- the command line --------------------------------------------------------------------------


def test_project_feed_command_and_exit_codes(fixture_archive, monkeypatch, capsys) -> None:
    out = fixture_archive.tmp / "feeds"
    base = ["archive", "project", "feed", "--root", str(fixture_archive.paths.root)]
    code, stdout, _ = _cce(monkeypatch, capsys, *base, "--out", str(out), "--provider", "claude")
    payload = json.loads(stdout)
    assert code == 0 and list(payload) == ["claude"] and payload["claude"]["rows"] == 2
    assert sorted(item.name for item in out.glob("feed-*.jsonl")) == ["feed-claude-prompts.jsonl"]
    assert '"text"' not in stdout and SHARED_ASK not in stdout
    code, _, err = _cce(monkeypatch, capsys, *base[:4], str(out / "nowhere"), "--out", str(out))
    assert code == 2 and "archive root not found" in err
    manifest = load_manifest(fixture_archive.paths)
    manifest["providers"]["chatgpt"]["conversations"] += 1
    write_json_atomic(fixture_archive.paths.manifest, manifest)
    code, _, err = _cce(monkeypatch, capsys, *base, "--out", str(fixture_archive.tmp / "again"))
    assert code == 1 and "feed not written" in err
