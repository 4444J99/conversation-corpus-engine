from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from archive_support import add_idless_chatgpt_shard

from conversation_corpus_engine import archive_layout as layout
from conversation_corpus_engine.archive_commands import (
    STAGES,
    ArchiveContext,
    run_stages,
    verify_archive,
)
from conversation_corpus_engine.archive_ingest import (
    DEFAULT_ARCHIVE_DIRNAME,
    ArchivePaths,
    iter_store,
    latest_activity_day,
    load_manifest,
)
from conversation_corpus_engine.archive_naming import (
    STRUCTURAL_NAMES,
    InvalidName,
    hash_identity,
    parse_name,
    validate_name,
    verify_names,
)
from conversation_corpus_engine.archive_vault import link_targets
from conversation_corpus_engine.cli import main

PACKAGE = Path(__file__).resolve().parent.parent / "src" / "conversation_corpus_engine"


def _cce(monkeypatch, capsys, *argv: str) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "argv", ["cce", *argv])
    code = 0
    try:
        main()
    except SystemExit as exit_:
        code = int(exit_.code or 0)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _stem(name: str) -> str:
    return name.split(".", 1)[0]


# --- golden names ---------------------------------------------------------------------------


def test_the_archive_folder_and_its_direct_children_have_the_specified_names() -> None:
    assert DEFAULT_ARCHIVE_DIRNAME == layout.ARCHIVE_DIRNAME == "archive-all"
    paths = ArchivePaths(Path("/store/archive-all"))
    assert paths.manifest.name == "manifest-all.json"
    assert paths.vault.name == "vault-all"
    assert paths.site.name == "site-all"
    assert paths.graph.name == "graph-all"
    assert paths.database.name == "database-all-search.sqlite"
    assert paths.enrichment.name == "index-all-enrichment.jsonl"
    assert paths.topics.name == "report-all-topics.json"
    assert paths.provider_dir("chatgpt").name == "provider-chatgpt"
    assert paths.conversations("chatgpt").name == "collection-chatgpt-conversations.jsonl"
    assert paths.index("chatgpt").name == "index-chatgpt-conversations.jsonl"
    assert paths.conversations("chatgpt").parent == paths.provider_dir("chatgpt")


def test_feed_and_receipt_names_follow_the_golden_vectors() -> None:
    paths = ArchivePaths(Path("/store/archive-all"))
    assert paths.feed("chatgpt").name == "feed-chatgpt-prompts.jsonl"
    fingerprint = "ab" * 32
    receipt = paths.receipt("chatgpt", fingerprint, "20261017")
    assert receipt.name == f"receipt-chatgpt-prompts-{hash_identity(fingerprint)}-20261017.json"
    parts = parse_name(receipt.name.removesuffix(".json"))
    assert (parts.kind, parts.scope, parts.subject, parts.date) == (
        "receipt",
        "chatgpt",
        "prompts",
        "20261017",
    )
    with pytest.raises(InvalidName):
        paths.receipt("chatgpt", fingerprint, "2026-10-17")
    with pytest.raises(InvalidName):
        paths.receipt("nowhere", fingerprint, "20261017")


def test_every_fixed_name_in_the_layout_is_a_valid_contract_name() -> None:
    fixed = [
        value
        for key, value in vars(layout).items()
        if key.isupper()
        and isinstance(value, str)
        and key.endswith(("FILENAME", "DIRNAME", "NOTE"))
    ]
    assert len(fixed) > 20
    for value in fixed:
        stem = _stem(value)
        assert stem in STRUCTURAL_NAMES or validate_name(stem) == [], value
    for provider in ("chatgpt", "claude", "gemini", "notebooklm", "grok", "perplexity"):
        for name in (
            layout.provider_dirname(provider),
            layout.conversations_filename(provider),
            layout.index_filename(provider),
            layout.feed_filename(provider),
        ):
            assert validate_name(_stem(name)) == [], name
    assert layout.layout_payload()["archive"] == "archive-all"


def test_note_names_for_hubs() -> None:
    assert layout.provider_note("chatgpt") == "provider-chatgpt"
    assert layout.month_note("2025-06-01T10:00:00Z") == "month-all-202506"
    assert layout.month_note("2025-06") == "month-all-202506"
    assert layout.month_note("") is None
    assert layout.month_note("2025-13-01T00:00:00Z") is None
    assert layout.month_note("not a date") is None
    assert layout.topic_note("kubernetes") == "topic-all-kubernetes"
    for term in ("well-known", "Kubernetes", "snake_case", "x" * 33, "", "naïve"):
        assert layout.topic_note(term) is None, term
    assert layout.collection_note("claude", "p1") == "collection-claude-p1"
    assert layout.conversation_note("chatgpt", "chatgpt-abc-123") == "conversation-chatgpt-abc-123"
    assert (
        layout.conversation_note_for_uid("grok-media-2025-06") == "conversation-grok-media-2025-06"
    )


def test_a_collection_identifier_that_is_not_a_valid_subject_is_replaced_by_its_digest() -> None:
    long_id = "g-p-" + "0123456789abcdef" * 2
    assert len(long_id) > 32
    assert layout.collection_subject(long_id) == hash_identity(long_id)
    assert layout.collection_subject("Mixed Case") == hash_identity("Mixed Case")
    assert layout.collection_subject("proj-1") == "proj-1"
    note = layout.collection_note("chatgpt", long_id)
    assert note == f"collection-chatgpt-{hash_identity(long_id)}"
    assert layout.collection_note("chatgpt", long_id) == note
    assert validate_name(note) == []


def test_the_wikilink_function_only_links_to_valid_names() -> None:
    assert layout.wikilink("home-all") == "[[home-all]]"
    assert layout.wikilink("provider-grok", "Grok") == "[[provider-grok|Grok]]"
    assert layout.wikilink("provider-grok", "Grok", in_table=True) == "[[provider-grok\\|Grok]]"
    for bad in ("Home", "home all", "a|b", "[[x]]", ""):
        with pytest.raises(InvalidName):
            layout.wikilink(bad)


def test_latest_activity_day_comes_from_the_data() -> None:
    manifest = {
        "providers": {
            "grok": {"status": "ok", "last_activity_max": "2025-05-03T10:00:00Z"},
            "claude": {"status": "ok", "last_activity_max": "2025-07-01T23:59:59Z"},
            "gemini": {"status": "failed", "last_activity_max": "2030-01-01T00:00:00Z"},
            "perplexity": {"status": "absent"},
        }
    }
    assert latest_activity_day(manifest) == "20250701"
    with pytest.raises(InvalidName):
        latest_activity_day({"providers": {"grok": {"status": "absent"}}})


# --- the arbiter: a whole build passes the naming check ---------------------------------------


@pytest.fixture
def full_build(archive_env):
    add_idless_chatgpt_shard(archive_env.export)
    context = ArchiveContext(archive_env.paths, archive_env.auth, archive_env.export)
    result = run_stages(context, STAGES)
    return archive_env, result


def test_a_full_build_writes_only_names_the_contract_accepts(full_build) -> None:
    env, result = full_build
    report = verify_names(env.paths.root)
    assert report["problems"] == []
    assert report["ok"] is True
    assert report["checked"] > 100
    assert verify_archive(env.paths) == []
    top = sorted(path.name for path in env.paths.root.iterdir())
    assert top == [
        "database-all-turns",
        "graph-all",
        "index-all-enrichment.jsonl",
        "manifest-all-vault.json",
        "manifest-all.json",
        "provider-chatgpt",
        "provider-claude",
        "provider-gemini",
        "provider-grok",
        "provider-notebooklm",
        "provider-perplexity",
        "report-all-topics.json",
        "site-all",
        "vault-all",
    ]
    assert result["ingest"]["totals"]["conversations"] == 26


def test_derived_ids_flow_through_every_surface(full_build) -> None:
    env, result = full_build
    derived = [c for c in iter_store(env.paths) if c.source_id.startswith("derived-")]
    assert len(derived) == 2
    assert load_manifest(env.paths)["providers"]["chatgpt"]["derived_ids"] == 2
    for conversation in derived:
        name = layout.conversation_note(conversation.provider, conversation.uid)
        assert re.fullmatch(r"conversation-chatgpt-derived-[0-9a-f]{16}", name)
        assert env.paths.vault_note(conversation.provider, conversation.uid).is_file()
        assert env.paths.site_page(conversation.provider, conversation.uid).is_file()
    assert result["site"]["pages"] == result["ingest"]["totals"]["conversations"] == 26


def test_vault_links_use_only_minted_names_and_all_resolve(full_build) -> None:
    env, _ = full_build
    stems = {path.stem for path in env.paths.vault.rglob("*.md")}
    checked = 0
    for note in env.paths.vault.rglob("*.md"):
        for target in link_targets(note.read_text(encoding="utf-8")):
            assert validate_name(target) == [], (note.name, target)
            assert target in stems, (note.name, target)
            checked += 1
    assert checked > 100
    folders = sorted(p.name for p in env.paths.vault.iterdir() if not p.name.startswith("."))
    assert folders == ["conversations", "home-all.md", "hubs", "meta"]
    assert sorted(p.name for p in env.paths.vault_meta.iterdir()) == [
        "coverage-all.md",
        "guide-all.md",
    ]
    hubs = {p.name for p in env.paths.vault_hubs.iterdir()}
    assert "provider-chatgpt.md" in hubs and "month-all-202506.md" in hubs
    assert not any(name.startswith("month-all-undated") for name in hubs)
    assert not [p for p in env.paths.vault.rglob("*") if p.name != p.name.lower()]


def test_site_pages_link_only_to_files_that_exist(full_build) -> None:
    env, _ = full_build
    home = env.paths.site_home.read_text(encoding="utf-8")
    for name in ("site-all.css", "graph-all.html", "index-all-conversations.js", "home-all.js"):
        assert name in home
        assert (env.paths.site / name).is_file()
    script = (env.paths.site / "home-all.js").read_text(encoding="utf-8")
    assert "conversations/conversation-" in script and "__" not in script
    graph_script = (env.paths.site / "graph-all.js").read_text(encoding="utf-8")
    assert "home-all.html#t=" in graph_script and "__" not in graph_script


def test_the_command_line_checks_names_and_the_database_stage_exists(
    full_build, monkeypatch, capsys
) -> None:
    env, _ = full_build
    code, out, _ = _cce(
        monkeypatch, capsys, "archive", "verify-names", "--root", str(env.paths.root)
    )
    assert code == 0 and json.loads(out)["ok"] is True
    (env.paths.root / "stray.txt").write_text("x", encoding="utf-8")
    code, out, _ = _cce(
        monkeypatch, capsys, "archive", "verify-names", "--root", str(env.paths.root)
    )
    assert code == 1 and any("stray.txt" in problem for problem in json.loads(out)["problems"])
    common = ["--project-root", str(env.project), "--corpus-store-root", str(env.store)]
    code, _, _ = _cce(monkeypatch, capsys, "archive", "database", *common)
    assert code == 0
    code, _, err = _cce(monkeypatch, capsys, "archive", "kb", *common)
    assert code == 2 and "invalid choice" in err


def test_a_layout_name_is_never_spelled_by_hand_in_the_archive_modules() -> None:
    hand_spelled = [
        re.compile(r"\bkb\b"),
        re.compile(r"(?<![A-Za-z0-9])_archive\b"),
        re.compile(r"[\"'f](?:(?:Hubs|Conversations|Meta)/|Home\.md)"),
        re.compile(r"[\"'](?:manifest|run|summary|nodes|edges)\.json(?:l)?[\"']"),
    ]
    offenders = []
    for path in sorted(PACKAGE.glob("archive_*.py")) + [PACKAGE / "mcp_server.py"]:
        if path.name == "archive_naming.py":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if any(pattern.search(line) for pattern in hand_spelled):
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert offenders == []
