"""Library status explains non-full records per provider, never across the whole library.

Documents record the reason for a non-full body in ``extra.body_reason``. Native estate records
keep theirs in ``extra.findings``. Every library here is synthetic: documents are files created
under ``tmp_path`` and native sessions come from a throwaway inventory database read through the
real ``iter_estate``, so the findings have the shape the real reader writes.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from conversation_corpus_engine import library_commands
from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.cli import build_parser
from conversation_corpus_engine.corpus_store import register_corpus_store
from conversation_corpus_engine.library_build import build_library, publish_library
from conversation_corpus_engine.library_documents import iter_documents
from conversation_corpus_engine.library_native import iter_estate
from conversation_corpus_engine.library_store import Library

SPLIT_KEYS = {"non_full", "explained", "unexplained"}


def estate(tmp_path: Path, rows: list[tuple[str, str, Any]]) -> Path:
    """A minimal synthetic inventory database: one session row per ``(family, id, source)``."""
    path = tmp_path / "estate.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE sessions (agent_family TEXT, session_id TEXT, custody_path TEXT, "
            "parent_session_id TEXT, prompt_title TEXT, prompt_snippet TEXT, started_at TEXT, "
            "ended_at TEXT, project_root TEXT);"
            "CREATE TABLE source_observations (agent_family TEXT, source_path TEXT, "
            "object_key TEXT, native_id TEXT);"
        )
        for family, session_id, source in rows:
            connection.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
                (family, session_id, str(source), "", "A synthetic session", "", "", "", ""),
            )
    return path


def codex_lines(path: Path, texts: list[str], *, trailing: str = "") -> Path:
    rows = [
        {
            "type": "response_item",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {
                "type": "message",
                "id": f"event-{index}",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        }
        for index, text in enumerate(texts)
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n" + trailing)
    return path


def native_sessions(tmp_path: Path) -> dict[str, Conversation]:
    """Real native records: one full, one partial and two metadata-only (undeclared source)."""
    full = codex_lines(tmp_path / "full.jsonl", ["a synthetic complete session"])
    partial = codex_lines(
        tmp_path / "partial.jsonl", ["a synthetic readable session"], trailing="{bad-json\n"
    )
    database = estate(
        tmp_path,
        [
            ("codex", "native-full", full),
            ("codex", "native-partial", partial),
            ("codex", "native-orphan", ""),
            ("claude", "claude-orphan", ""),
        ],
    )
    items = {item.source_id: item for item in iter_estate(database)}
    assert {key: item.extra["body_status"] for key, item in items.items()} == {
        "native-full": "full",
        "native-partial": "partial",
        "native-orphan": "metadata_only",
        "claude-orphan": "metadata_only",
    }
    assert all(item.extra["findings"] for key, item in items.items() if key != "native-full")
    return items


def record(provider: str, source_id: str, status: str, **extra: Any) -> Conversation:
    """A synthetic record that is written exactly as given, so each case controls its own extra."""
    full = status == "full"
    return Conversation(
        provider=provider,
        source_id=source_id,
        title=f"Synthetic {source_id}",
        messages=[Message(role="user", text=f"body of {source_id}", id="m1", channel="typed")]
        if full
        else [],
        provenance={"source_file": f"{source_id}.json"},
        extra={
            "wing": "authored" if provider == "documents" else "ai",
            "lane": "writing" if provider == "documents" else "coding",
            "body_status": status,
            **extra,
        },
    )


def document_files(root: Path) -> None:
    (root / "full.md").write_text("A complete synthetic note.")
    (root / "empty.txt").write_text("")
    (root / "image.png").write_bytes(b"PNG")


def source(root: Path) -> dict[str, Any]:
    return {"id": "docs", "root": str(root), "include": ["**/*"], "exclude": []}


def mixed_library(tmp_path: Path) -> Library:
    """Documents, native sessions, archive rows and a full-only provider in one library."""
    docs = tmp_path / "docs"
    docs.mkdir()
    document_files(docs)
    library = Library.create(tmp_path / "library.sqlite")
    for item in iter_documents([source(docs)]):
        library.put(item)
    # documents: no reason recorded, and findings that never explain a document
    library.put(record("documents", "file-bare", "metadata_only"))
    library.put(record("documents", "file-findings", "partial", findings=["partial_read:x.docx"]))
    for item in native_sessions(tmp_path).values():
        library.put(item)
    # native: no reason recorded in any usable form
    library.put(record("codex", "bare", "metadata_only", findings=[]))
    library.put(record("claudecode", "absent", "metadata_only"))
    library.put(record("claudecode", "not-a-list", "partial", findings="source_error:x"))
    library.put(record("claudecode", "blank-entries", "partial", findings=["", None, 3]))
    # archive rows: a normalized archive row carries no reason unless its reader recorded one
    library.put(record("chatgpt", "archive-full", "full"))
    library.put(record("chatgpt", "archive-bare", "metadata_only"))
    library.put(record("chatgpt", "archive-reasoned", "metadata_only", findings=["no_export_body"]))
    # a provider whose records are all full
    library.put(record("grok", "full-one", "full"))
    library.put(record("grok", "full-two", "full", findings=["ignored_on_full:x"]))
    return library


# --- documents keep their meaning ---------------------------------------------------------


def test_documents_with_a_recorded_reason_are_explained(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    document_files(docs)
    with Library.create(tmp_path / "library.sqlite") as library:
        for item in iter_documents([source(docs)]):
            library.put(item)
        status = library.status()
    assert status["by_body_status"] == {"full": 1, "metadata_only": 2}
    assert status["unexplained_non_full"] == 0
    assert status["body_reasons"] == {"metadata_only": {"empty_body": 1, "unsupported_format": 1}}
    assert status["non_full_by_provider"] == {
        "documents": {"non_full": 2, "explained": 2, "unexplained": 0}
    }


def test_documents_without_a_reason_are_counted_as_unexplained(tmp_path: Path) -> None:
    with Library.create(tmp_path / "library.sqlite") as library:
        library.put(record("documents", "file-bare", "metadata_only"))
        library.put(record("documents", "file-blank", "partial", body_reason=""))
        status = library.status()
    assert status["unexplained_non_full"] == 2
    assert status["body_reasons"] == {}
    assert status["non_full_by_provider"] == {
        "documents": {"non_full": 2, "explained": 0, "unexplained": 2}
    }


def test_documents_are_explained_only_by_a_body_reason_never_by_findings(tmp_path: Path) -> None:
    with Library.create(tmp_path / "library.sqlite") as library:
        library.put(record("documents", "file-findings", "partial", findings=["partial_read:x"]))
        status = library.status()
    assert status["unexplained_non_full"] == 1
    assert status["non_full_by_provider"]["documents"]["unexplained"] == 1


# --- native records are explained by their findings ----------------------------------------


def test_native_non_full_records_with_findings_are_explained(tmp_path: Path) -> None:
    with Library.create(tmp_path / "library.sqlite") as library:
        for item in native_sessions(tmp_path).values():
            library.put(item)
        status = library.status()
    assert status["by_body_status"] == {"full": 1, "partial": 1, "metadata_only": 2}
    assert status["unexplained_non_full"] == 0
    assert status["non_full_by_provider"] == {
        "codex": {"non_full": 2, "explained": 2, "unexplained": 0},
        "claudecode": {"non_full": 1, "explained": 1, "unexplained": 0},
    }
    # native finding codes are not body_reason values and stay out of that histogram
    assert status["body_reasons"] == {}


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"findings": []},
        {"findings": "source_error:x"},
        {"findings": [""]},
        {"findings": [None, 3]},
        {"findings": None},
    ],
    ids=["absent", "empty", "string", "blank", "non-string", "null"],
)
def test_native_non_full_record_without_a_usable_reason_is_counted_as_unexplained(
    tmp_path: Path, extra: dict[str, Any]
) -> None:
    with Library.create(tmp_path / "library.sqlite") as library:
        library.put(record("codex", "bare", "metadata_only", **extra))
        status = library.status()
    assert status["unexplained_non_full"] == 1
    assert status["non_full_by_provider"] == {
        "codex": {"non_full": 1, "explained": 0, "unexplained": 1}
    }


def test_a_body_reason_explains_a_non_full_record_of_any_provider(tmp_path: Path) -> None:
    with Library.create(tmp_path / "library.sqlite") as library:
        library.put(record("chatgpt", "reasoned", "metadata_only", body_reason="no_export_body"))
        status = library.status()
    assert status["unexplained_non_full"] == 0
    assert status["body_reasons"] == {"metadata_only": {"no_export_body": 1}}
    assert status["non_full_by_provider"] == {
        "chatgpt": {"non_full": 1, "explained": 1, "unexplained": 0}
    }


def test_full_records_are_never_counted(tmp_path: Path) -> None:
    with Library.create(tmp_path / "library.sqlite") as library:
        library.put(record("codex", "full-plain", "full"))
        library.put(record("codex", "full-findings", "full", findings=["ignored:x"]))
        library.put(record("documents", "file-full", "full", body_reason="ignored"))
        status = library.status()
    assert status["unexplained_non_full"] == 0
    assert status["non_full_by_provider"] == {
        "codex": {"non_full": 0, "explained": 0, "unexplained": 0},
        "documents": {"non_full": 0, "explained": 0, "unexplained": 0},
    }


# --- a mixed library ----------------------------------------------------------------------


def test_mixed_library_total_counts_only_records_with_no_reason_anywhere(tmp_path: Path) -> None:
    with mixed_library(tmp_path) as library:
        status = library.status()
    # documents: file-bare, file-findings; codex: bare; claudecode: absent, not-a-list,
    # blank-entries; chatgpt: archive-bare. The real native sessions and both reasoned
    # records are explained and the full records are not counted.
    assert status["unexplained_non_full"] == 7


def test_mixed_library_reports_the_split_for_each_provider(tmp_path: Path) -> None:
    with mixed_library(tmp_path) as library:
        status = library.status()
    assert status["non_full_by_provider"] == {
        "documents": {"non_full": 4, "explained": 2, "unexplained": 2},
        "codex": {"non_full": 3, "explained": 2, "unexplained": 1},
        "claudecode": {"non_full": 4, "explained": 1, "unexplained": 3},
        "chatgpt": {"non_full": 2, "explained": 1, "unexplained": 1},
        "grok": {"non_full": 0, "explained": 0, "unexplained": 0},
    }


def test_mixed_library_split_agrees_with_the_totals_and_the_existing_keys(
    tmp_path: Path,
) -> None:
    with mixed_library(tmp_path) as library:
        status = library.status()
    split = status["non_full_by_provider"]
    assert set(split) == set(status["by_provider"])
    for provider, counts in split.items():
        assert set(counts) == SPLIT_KEYS
        assert counts["non_full"] == counts["explained"] + counts["unexplained"], provider
    assert sum(item["unexplained"] for item in split.values()) == status["unexplained_non_full"]
    assert sum(item["non_full"] for item in split.values()) == (
        status["by_body_status"].get("partial", 0)
        + status["by_body_status"].get("metadata_only", 0)
    )
    # body_reasons stays a histogram of body_reason values only
    assert status["body_reasons"] == {"metadata_only": {"empty_body": 1, "unsupported_format": 1}}


# --- end to end ---------------------------------------------------------------------------


def test_cli_status_reports_zero_unexplained_for_synthetic_documents_and_native_sessions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project, store, archive, docs, native = (
        tmp_path / name for name in ("project", "store", "archive", "docs", "native")
    )
    for folder in (project, store, archive, docs, native):
        folder.mkdir()
    register_corpus_store(project, store)
    document_files(docs)
    native_sessions(native)
    manifest = {
        "schema": "cce.library-sources.v1",
        "archives": [str(archive)],
        "estate_databases": [str(native / "estate.sqlite")],
        "documents": [source(docs)],
    }
    result = build_library(manifest, project_root=project, store_root=store)
    publish_library(Path(result["generation"]), project_root=project, store_root=store)
    capsys.readouterr()
    args = build_parser().parse_args(
        [
            "library",
            "status",
            "--json",
            "--store-root",
            str(store),
            "--project-root",
            str(project),
        ]
    )
    assert library_commands.handle_library(args) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["unexplained_non_full"] == 0
    assert status["non_full_by_provider"] == {
        "documents": {"non_full": 2, "explained": 2, "unexplained": 0},
        "codex": {"non_full": 2, "explained": 2, "unexplained": 0},
        "claudecode": {"non_full": 1, "explained": 1, "unexplained": 0},
    }
