"""Local memory, Copilot reports, project/notebook provenance and the oversize-row exemption."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from conversation_corpus_engine import (
    archive_read_captures,
    archive_read_grok,
    archive_read_localmemory,
    archive_read_notebooklm,
)
from conversation_corpus_engine.archive_author import HUMAN, MACHINE, reconcile
from conversation_corpus_engine.archive_commands import oversize_row_files, reconcile_source
from conversation_corpus_engine.archive_ingest import ingest, iter_store
from conversation_corpus_engine.archive_limits import (
    OVERSIZE_ROW_REASON,
    Limits,
    census_problems,
    take_census,
)
from conversation_corpus_engine.archive_model import SourceTracker
from conversation_corpus_engine.archive_shard import ShardWriter

DOCX_XML = (
    '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
    'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Report text</w:t></w:r></w:p>'
    "</w:body></w:document>"
)


def _write(path: Path, content: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_bytes(content)
    return path


def _local_memory(
    root: Path, ids: tuple[str, str] = ("a", "b"), folder: str = "claude-local-session-memory"
) -> Path:
    first, second = ids
    corpus = root / folder
    _write(corpus / "README.md", "# Local session memory\nNotes about the capture.")
    _write(corpus / "import-manifest.json", json.dumps({"files": 3}))
    _write(corpus / "corpus" / "threads-index.json", json.dumps([{"title": "a thread"}]))
    _write(corpus / "eval" / "scorecard.json", json.dumps({"gate": "pass"}))
    _write(corpus / "source" / "projects.json", json.dumps([{"name": "Bread"}]))
    _write(
        corpus / "source" / "conversations.json",
        json.dumps([{"uuid": first}, {"uuid": second}]),
    )
    _write(corpus / "source" / "conversation-summaries.json", json.dumps([]))
    _write(corpus / "source" / "conversation-cache" / f"{first}.json", json.dumps({"uuid": first}))
    _write(
        corpus / "source" / "conversation-details" / f"{second}.json", json.dumps({"uuid": second})
    )
    _write(corpus / "source" / "users.json", json.dumps([{"email": "x@example.com"}]))
    _write(corpus / "source" / "blob.json", b"\x00\x01binary")
    return root


def _ingest_claude_then(archive_env, root: Path) -> dict:
    """Read the export's Claude first, so the folded ids have an owner to be found in."""
    ingest(
        export_root=archive_env.export,
        paths=archive_env.paths,
        authorization=archive_env.auth,
        providers=["claude"],
    )
    return ingest(
        export_root=archive_env.export,
        paths=archive_env.paths,
        authorization=archive_env.auth,
        providers=["localmemory"],
        overrides={"localmemory": root},
        merge=True,
    )


def _claude_ids(archive_env) -> tuple[str, str]:
    ingest(
        export_root=archive_env.export,
        paths=archive_env.paths,
        authorization=archive_env.auth,
        providers=["claude"],
    )
    chats = [c.source_id for c in iter_store(archive_env.paths, ["claude"]) if c.kind == "chat"]
    return chats[0], chats[1]


def test_local_memory_accounts_for_every_file_as_a_record_a_fold_or_an_exclusion(
    archive_env, tmp_path: Path
) -> None:
    root = _local_memory(tmp_path / "local-memory", _claude_ids(archive_env))
    manifest = _ingest_claude_then(archive_env, root)
    info = manifest["providers"]["localmemory"]
    counters = info["reader_counters"]
    assert counters["files_seen"] == 11
    # records: README, import-manifest, corpus, eval, projects and the binary file (no text)
    assert info["conversations"] == 6
    assert (counters["files_folded"], counters["files_excluded"]) == (4, 1)
    assert counters["conversations_folded"] == 2
    assert counters["records_without_text"] == 1 and info["empty_conversations"] == 1
    assert info["kinds"] == {"memory": 6}
    assert info["authors"] == {HUMAN: 0, MACHINE: 5}
    assert reconcile(info["authors"], info["messages"]) == []
    # Both folded ids are conversations the Claude export already holds.
    assert counters["folded_ids"] == 2 and counters["folded_ids_missing"] == 0
    assert reconcile_source("localmemory", info) == []
    stored = list(iter_store(archive_env.paths, ["localmemory"]))
    assert {c.title for c in stored} >= {"claude-local-session-memory/README.md"}
    assert all(c.collection == "claude-local-session-memory" for c in stored)
    assert not any("example.com" in m.text for c in stored for m in c.messages)
    broken = {**info, "conversations": info["conversations"] - 1}
    assert "accounted for" in reconcile_source("localmemory", broken)[0]


def test_a_folded_conversation_the_owner_lacks_fails_verify_instead_of_vanishing(
    archive_env, tmp_path: Path
) -> None:
    known, _ = _claude_ids(archive_env)
    root = _local_memory(tmp_path / "local-memory", (known, "never-exported-uuid"))
    info = _ingest_claude_then(archive_env, root)["providers"]["localmemory"]
    assert info["reader_counters"]["folded_ids"] == 2
    assert info["reader_counters"]["folded_ids_missing"] == 1
    (problem,) = reconcile_source("localmemory", info)
    assert problem.startswith("1 folded conversation id(s) are not conversations")


def test_a_payload_in_a_folder_no_provider_owns_is_flagged(archive_env, tmp_path: Path) -> None:
    root = _local_memory(tmp_path / "local-memory", folder="mystery-memory")
    info = ingest(
        export_root=archive_env.export,
        paths=archive_env.paths,
        authorization=archive_env.auth,
        providers=["localmemory"],
        overrides={"localmemory": root},
    )["providers"]["localmemory"]
    assert info["reader_counters"]["folded_ids_unowned"] == 2
    assert "no provider owns" in "".join(reconcile_source("localmemory", info))


def test_local_memory_detect_and_channels(tmp_path: Path) -> None:
    assert not archive_read_localmemory.detect(tmp_path / "missing")
    root = _local_memory(tmp_path / "local-memory")
    _write(root / ".DS_Store", b"finder")
    (root / "link.json").symlink_to(root / "claude-local-session-memory" / "README.md")
    assert archive_read_localmemory.detect(root)
    described = archive_read_localmemory.channels(root)
    assert described["read"] == ["6 file(s), one machine record each"]
    assert any("4 conversation payload file(s)" in item for item in described["not_read"])
    assert any("1 account file(s)" in item for item in described["not_read"])
    assert any("2 hidden file(s) or link(s)" in item for item in described["not_read"])


def _captures(root: Path) -> Path:
    docx = root / "A Report.docx"
    docx.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(docx, "w") as archive:
        archive.writestr("word/document.xml", DOCX_XML)
    _write(root / "notes.txt", "Plain research notes from Copilot.")
    _write(root / "broken.docx", b"stub")
    _write(root / "page.html", "<html><body>a saved page, read by the page reader</body></html>")
    return root


def test_copilot_reports_are_machine_documents_one_per_file(archive_env, tmp_path: Path) -> None:
    root = _captures(tmp_path / "captures")
    assert archive_read_captures.detect(root)
    assert not archive_read_captures.detect(tmp_path / "no-reports")
    manifest = ingest(
        export_root=archive_env.export,
        paths=archive_env.paths,
        authorization=archive_env.auth,
        providers=["copilotreports"],
        overrides={"copilotreports": root},
    )
    info = manifest["providers"]["copilotreports"]
    # Three report files: two readable, one that stays a conversation with no turn.
    assert (info["conversations"], info["messages"], info["empty_conversations"]) == (3, 2, 1)
    assert info["kinds"] == {"document": 3}
    assert info["authors"] == {HUMAN: 0, MACHINE: 2}
    assert info["reader_counters"]["report_files"] == 3
    assert info["reader_counters"]["report_files_unread"] == 1
    stored = {c.title: c for c in iter_store(archive_env.paths, ["copilotreports"])}
    assert set(stored) == {"A Report", "notes", "broken"}
    assert stored["A Report"].messages[0].text.strip() == "Report text"
    assert {(m.role, m.channel) for c in stored.values() for m in c.messages} == {
        ("assistant", "reply")
    }
    assert all(c.provider == "copilotreports" for c in stored.values())
    # Nothing host-specific (where the folder was, when the host touched the file) is stored.
    for conversation in stored.values():
        assert not set(archive_read_captures.HOST_KEYS) & set(conversation.provenance)
        assert "original_refs" not in conversation.extra
        assert conversation.provenance["source_file"].endswith((".docx", ".txt"))


def test_a_notebooks_sources_and_notes_are_the_persons_the_rest_is_the_services(
    archive_env,
) -> None:
    root = archive_env.export / "gemini" / "NotebookLM"
    tracker = SourceTracker(root, archive_read_notebooklm.READER)
    (notebook,) = archive_read_notebooklm.iter_conversations(root, tracker)
    by_kind = {}
    for message in notebook.messages:
        by_kind.setdefault(message.meta["kind"], []).append(message)
    for kind, basis in (("source", "user-supplied:source"), ("note", "user-saved:note")):
        assert {(m.role, m.channel, m.meta["author_basis"]) for m in by_kind[kind]} == {
            ("user", "written", basis)
        }
    assert {m.role for m in by_kind["artifact"]} == {"system"}
    assert all("author_basis" not in m.meta for m in by_kind["artifact"])


def test_a_grok_personality_is_the_persons_a_conversation_starter_is_not_proven(
    archive_env,
) -> None:
    root = archive_env.export / "grok"
    tracker = SourceTracker(root, archive_read_grok.READER)
    projects = [
        c for c in archive_read_grok.iter_conversations(root, tracker) if c.kind == "notebook"
    ]
    (project,) = projects
    kinds = {m.meta["kind"]: m for m in project.messages}
    personality = kinds["custom-personality"]
    assert (personality.role, personality.channel) == ("user", "written")
    assert personality.meta["author_basis"] == "user-supplied:custom-personality"
    assert kinds["starter"].role == "system"


def test_a_lone_row_past_the_shard_size_is_flagged_and_the_census_names_the_exemption(
    tmp_path: Path,
) -> None:
    size = 65536
    writer = ShardWriter(tmp_path / "provider-codex", "codex", shard_bytes=size)
    writer.add("202601", b"x" * 40_000 + b"\n", b"i\n")
    # Alone in a second shard, and past both the shard size and the data ceiling.
    writer.add("202601", b"y" * 70_000 + b"\n", b"i\n")
    shards = writer.close()
    assert [shard.get("oversize_row", False) for shard in shards] == [False, True]
    manifest = {"providers": {"codex": {"layout": {"shards": shards}}}}
    exempt = oversize_row_files(manifest)
    assert exempt == {"provider-codex/shard-codex-202601-002.jsonl"}
    limits = Limits(shard_bytes=size, data_ceiling=size)
    flagged = take_census(tmp_path, limits)
    assert not flagged["ok"] and census_problems(flagged)
    cleared = take_census(tmp_path, limits, exempt=exempt)
    assert cleared["ok"] and census_problems(cleared) == []
    (item,) = cleared["violations"]["exempt"]
    assert item["path"] == "provider-codex/shard-codex-202601-002.jsonl"
    assert item["reason"] == OVERSIZE_ROW_REASON and item["bytes"] > size
    # An unnamed file past the ceiling is still a violation.
    (tmp_path / "stray.jsonl").write_bytes(b"z" * 70_000)
    assert not take_census(tmp_path, limits, exempt=exempt)["ok"]
