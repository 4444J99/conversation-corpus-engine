from __future__ import annotations

import csv
import json
import zipfile
from pathlib import Path

import pytest

from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.library_export import export_library, verify_export
from conversation_corpus_engine.library_store import Library

REPEATED = "This is a shared paragraph explaining a recurring research idea, with enough precise original wording to remain a meaningful passage across independent records and source observations."


@pytest.mark.parametrize("prefix", ["        ", "\t", ">     "])
def test_source_links_in_indented_code_are_literal_and_pass_validation(tmp_path, prefix):
    text = (
        "{\n"
        + prefix
        + '"body": "[dashboard](https://example.invalid) ![pixel](https://example.invalid/pixel)"\n}\n'
    )
    with Library.create(tmp_path / "library.sqlite") as library:
        source = Conversation(
            provider="antigravity",
            source_id="indented-code",
            title="Indented source code",
            messages=[Message(role="tool", text=text)],
        )
        library.put(source)
        library.index_blocks()
        destination = tmp_path / "export"
        export_library(library, destination)
        assert verify_export(destination) == []
        assert library.record(source.uid)["messages"][0]["text"] == text
        note = (destination / "vault/records" / (source.uid + ".md")).read_text()
        assert prefix + '"body": "[dashboard](https://example.invalid)' in note


def populate(tmp_path: Path) -> Library:
    library = Library.create(tmp_path / "library.sqlite")
    library.put(
        Conversation(
            provider="chatgpt",
            source_id="one",
            title="A shared title",
            messages=[Message(role="user", text=REPEATED + "\n\nFirst unique observation.")],
            provenance={"source_path": "/private/source one.json"},
            extra={"wing": "ai", "lane": "conversations"},
        )
    )
    library.put(
        Conversation(
            provider="documents",
            source_id="two",
            title="A shared title",
            kind="document",
            messages=[Message(role="user", text=REPEATED + "\n\nSecond unique observation.")],
            extra={"wing": "authored", "lane": "research"},
        )
    )
    library.put(
        Conversation(
            provider="chatgpt",
            source_id="child",
            title="<script>bad</script> [[injected]]",
            messages=[
                Message(
                    role="assistant",
                    text='<img src="https://example.invalid/pixel"> [execute](javascript:alert) [[poison]]',
                )
            ],
            extra={"wing": "ai", "lane": "conversations", "parent_session_id": "one"},
        )
    )
    library.put(
        Conversation(
            provider="documents",
            source_id="formula",
            title='=HYPERLINK("evil")',
            kind="plan",
            extra={"wing": "ai", "lane": "support", "body_status": "metadata_only"},
        )
    )
    library.index_blocks()
    return library


def test_export_preserves_both_wings_and_title_collisions(tmp_path: Path) -> None:
    with populate(tmp_path) as library:
        destination = tmp_path / "export"
        result = export_library(library, destination)
        assert result["records"] == 4
        assert len(list((destination / "vault" / "records").glob("*.md"))) == 4
        assert (
            "First unique observation."
            in (destination / "vault/records/chatgpt-one.md").read_text()
        )
        assert (
            "Second unique observation."
            in (destination / "vault/records/documents-two.md").read_text()
        )
        home = (destination / "vault/Home.md").read_text()
        assert "Authored" in home and "AI" in home
        index = [
            json.loads(line)
            for line in (destination / "library-index.jsonl").read_text().splitlines()
        ]
        assert {row["uid"] for row in index} == {row["uid"] for row in library.rows()}
        assert all(row["version"] == "1.0.0" for row in index)
        assert all("messages" not in row for row in index)
        assert verify_export(destination) == []


def test_repeated_passage_is_linked_once_with_all_backlinks(tmp_path: Path) -> None:
    with populate(tmp_path) as library:
        destination = tmp_path / "export"
        export_library(library, destination)
        blocks = list((destination / "vault/blocks").glob("*.md"))
        assert len(blocks) == 1
        passage = blocks[0].read_text()
        assert REPEATED in passage
        assert "../records/chatgpt-one.md#message-1" in passage
        assert "../records/documents-two.md#message-1" in passage
        record = (destination / "vault/records/chatgpt-one.md").read_text()
        assert REPEATED not in record
        assert f"../blocks/{blocks[0].name}" in record
        graph = [
            json.loads(line) for line in (destination / "graph.jsonl").read_text().splitlines()
        ]
        assert any(
            row.get("kind") == "parent"
            and row["source"] == "chatgpt-child"
            and row["target"] == "chatgpt-one"
            for row in graph
        )
        assert verify_export(destination) == []


def test_markup_is_inert_and_csv_formulas_are_quoted(tmp_path: Path) -> None:
    with populate(tmp_path) as library:
        destination = tmp_path / "export"
        export_library(library, destination)
        record = (destination / "vault/records/chatgpt-child.md").read_text()
        assert "<script>" not in record
        assert "<img" not in record
        assert "[[injected]]" not in record
        assert "[[poison]]" not in record
        assert "[execute](javascript:" not in record
        with (destination / "records.csv").open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        assert next(row for row in rows if row["uid"] == "documents-formula")["title"].startswith(
            "'="
        )
        assert (
            "file:///private/source%20one.json"
            in (destination / "vault/records/chatgpt-one.md").read_text()
        )


def test_private_files_zip_and_bounded_agent_entry(tmp_path: Path) -> None:
    with populate(tmp_path) as library:
        destination = tmp_path / "export"
        export_library(library, destination)
        assert (destination / "agent-entry.md").stat().st_size < 4096
        for path in destination.rglob("*"):
            assert path.stat().st_mode & 0o777 == (0o700 if path.is_dir() else 0o600)
        with zipfile.ZipFile(destination / "notion-import.zip") as archive:
            names = set(archive.namelist())
            assert "vault/Home.md" in names and "records.csv" in names
            assert all(not name.startswith("/") and ".." not in Path(name).parts for name in names)
            assert archive.read("vault/Home.md") == (destination / "vault/Home.md").read_bytes()
            assert not any(name.endswith(".sqlite") for name in names)


def test_export_refuses_replacement_user_notes_and_unsafe_ids(tmp_path: Path) -> None:
    with populate(tmp_path) as library:
        destination = tmp_path / "export"
        destination.mkdir()
        sentinel = destination / "personal.md"
        sentinel.write_text("Keep my notes")
        with pytest.raises(FileExistsError):
            export_library(library, destination)
        assert sentinel.read_text() == "Keep my notes"
        with pytest.raises(ValueError):
            library.put(Conversation(provider="../../outside", source_id="bad", title="bad"))
        # Export validation also protects against legacy or tampered databases.
        row = next(library.rows())
        row["uid"] = "../../outside"
        library.connection.execute(
            f"INSERT INTO records ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
            list(row.values()),
        )
        with pytest.raises(ValueError, match="uid"):
            export_library(library, tmp_path / "other")
        assert not (tmp_path / "outside").exists()


def test_verify_reports_broken_links_and_allows_candidate_receipts(tmp_path: Path) -> None:
    destination = tmp_path / "candidate"
    destination.mkdir()
    with Library.create(destination / "library.sqlite") as library:
        (destination / "sources.json").write_text("{}")
        (destination / "build-state.json").write_text("{}")
        library.put(
            Conversation(
                provider="chatgpt",
                source_id="one",
                title="One",
                messages=[Message(role="user", text="Text")],
            )
        )
        library.index_blocks()
        export_library(library, destination)
        assert verify_export(destination) == []
        (destination / "vault/records/chatgpt-one.md").unlink()
        assert any("missing" in problem for problem in verify_export(destination))


def test_budget_is_checked_before_any_excess_write(tmp_path: Path) -> None:
    written = 0
    checkpoints = 0

    def budget(*, bytes_written: int = 0) -> None:
        nonlocal written, checkpoints
        checkpoints += 1
        if written + bytes_written > 200:
            raise ValueError("test output budget exhausted")
        written += bytes_written

    with populate(tmp_path) as library:
        destination = tmp_path / "export"
        with pytest.raises(ValueError, match="test output budget"):
            export_library(library, destination, check_callback=budget)
        assert checkpoints > 4
        assert sum(path.stat().st_size for path in destination.rglob("*") if path.is_file()) <= 200
        assert not (destination / "notion-import.zip").exists()


def test_zip_writes_and_verification_observe_callback(tmp_path: Path) -> None:
    impending_bytes = 0
    checkpoints = 0

    def budget(*, bytes_written: int = 0) -> None:
        nonlocal impending_bytes, checkpoints
        impending_bytes += bytes_written
        checkpoints += 1

    with populate(tmp_path) as library:
        destination = tmp_path / "export"
        export_library(library, destination, check_callback=budget)
        assert impending_bytes >= sum(
            path.stat().st_size for path in destination.rglob("*") if path.is_file()
        )
        before = checkpoints
        assert verify_export(destination, check_callback=budget) == []
        assert checkpoints > before + 20


def test_export_and_verification_refuse_symlinks(tmp_path: Path) -> None:
    with populate(tmp_path) as library:
        untouched = tmp_path / "untouched"
        untouched.mkdir()
        link = tmp_path / "linked"
        link.symlink_to(untouched, target_is_directory=True)
        with pytest.raises(ValueError, match="symlink"):
            export_library(library, link)
        assert list(untouched.iterdir()) == []
        destination = tmp_path / "export"
        export_library(library, destination)
        index = destination / "library-index.jsonl"
        index.unlink()
        secret = tmp_path / "secret"
        secret.write_text("must not be parsed")
        index.symlink_to(secret)
        assert any("symlink" in problem for problem in verify_export(destination))


def test_zip_parity_and_anchor_corruption_are_reported(tmp_path: Path) -> None:
    with populate(tmp_path) as library:
        destination = tmp_path / "export"
        export_library(library, destination)
        note = destination / "vault/records/chatgpt-one.md"
        note.write_text(note.read_text().replace("## Message 1", "## Removed anchor"))
        problems = verify_export(destination)
        assert any("missing anchor" in problem for problem in problems)
        assert any("ZIP bytes differ" in problem for problem in problems)


def test_agent_entry_uses_store_root_and_empty_wings_remain_visible(tmp_path: Path) -> None:
    with Library.create(tmp_path / "library.sqlite") as library:
        library.index_blocks()
        destination = tmp_path / "registered store/library-all/generations/generation-test"
        export_library(library, destination)
        entry = (destination / "agent-entry.md").read_text()
        assert f"STORE='{tmp_path}/registered store'" in entry
        assert "generation-test" not in entry
        assert "--limit 20 --offset 0" in entry
        assert "--message 0 --offset 0 --limit 12000" in entry
        assert "--offset 0 --limit 50" in entry
        assert (destination / "vault/hubs/wing-ai.md").exists()
        assert (destination / "vault/hubs/wing-authored.md").exists()
        assert verify_export(destination) == []


def test_verifier_returns_findings_for_malformed_manifest(tmp_path: Path) -> None:
    destination = tmp_path / "export"
    destination.mkdir()
    (destination / "export-manifest.json").write_text("[]")
    assert any("manifest" in problem for problem in verify_export(destination))
