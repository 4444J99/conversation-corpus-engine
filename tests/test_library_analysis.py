from __future__ import annotations

import hashlib
import json
import zlib
from pathlib import Path

import pytest

from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.corpus_store import register_corpus_store
from conversation_corpus_engine.library_analysis import AnalysisLimits, export_analysis
from conversation_corpus_engine.library_store import Library


def setup_library(tmp_path: Path) -> tuple[Path, Path, Path]:
    project, store = tmp_path / "project", tmp_path / "store"
    project.mkdir()
    store.mkdir()
    register_corpus_store(project, store)
    database = store / "generation" / "library.sqlite"
    text = "Leading lattice prose.\r\n\r\n    exact_code()\n\nUnicode: caf\u00e9.\n"
    with Library.create(database) as library:
        for identity in ("one", "two"):
            library.put(
                Conversation(
                    provider="chatgpt",
                    source_id=identity,
                    title="Lattice",
                    messages=[Message(role="user", text=text, id="message-source")],
                    provenance={"source_path": "/private/original.json"},
                    extra={
                        "wing": "ai",
                        "lane": "conversations",
                        "alternate_messages": [
                            {"role": "assistant", "id": "alternate-source", "text": "Alternate."}
                        ],
                    },
                )
            )
        library.put(
            Conversation(
                provider="documents",
                source_id="empty",
                title="Body unavailable",
                extra={"body_status": "metadata_only", "findings": ["encrypted"]},
            )
        )
    return project, store, database


def test_projection_reconstructs_exact_main_and_alternate_text_and_source(tmp_path: Path) -> None:
    project, store, database = setup_library(tmp_path)
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    destination = store / "derived" / "analysis" / "one"
    with Library(database) as library:
        receipt = export_analysis(
            library,
            destination,
            project_root=project,
            store_root=store,
            uids=["chatgpt-one", "chatgpt-two", "documents-empty"],
        )
        expected = library.record("chatgpt-one")
    native = json.loads((destination / "lingframe.json").read_text())
    assert len(native["documents"]) == 3
    document = native["documents"][0]
    assert document["id"] == "chatgpt-one"
    assert document["metadata"]["cce"]["provenance"] == expected["provenance"]
    assert document["metadata"]["cce"]["version"] == "1.0.0"
    assert [item["metadata"]["sequence"] for item in document["themes"]] == ["main", "alternate"]
    assert (
        "".join(a["text"] for a in document["themes"][0]["paragraphs"])
        == expected["messages"][0]["text"]
    )
    assert "".join(a["text"] for a in document["themes"][1]["paragraphs"]) == "Alternate."
    for message in document["themes"]:
        for atom in message["paragraphs"]:
            meta = atom["metadata"]
            source = (
                expected["messages"]
                if meta["sequence"] == "main"
                else expected["extra"]["alternate_messages"]
            )
            assert (
                source[meta["message_index"]]["text"][meta["start"] : meta["end"]] == atom["text"]
            )
    graph = json.loads((destination / "uaks.json").read_text())
    occurrences = [e for e in graph["edges"].values() if e["relationship"] == "occurrence"]
    assert len(occurrences) > len({e["target_id"] for e in occurrences})
    for edge in occurrences:
        assert edge["source_id"] in graph["nodes"] and edge["target_id"] in graph["nodes"]
        node = graph["nodes"][edge["target_id"]]
        assert hashlib.sha256(node["content"].encode()).hexdigest() == node["hash"]
        assert edge["metadata"]["uid"] in {"chatgpt-one", "chatgpt-two"}
    assert receipt["counts"]["records"] == 3
    assert receipt["counts"]["messages"] == 4
    assert receipt["coverage"]["metadata_only"] == 1
    assert "Leading lattice" not in json.dumps(receipt)
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in destination.iterdir())
    assert destination.stat().st_mode & 0o777 == 0o700


def test_selection_is_explicit_bounded_and_deterministic(tmp_path: Path) -> None:
    project, store, database = setup_library(tmp_path)
    with Library(database) as library:
        for name in ("first", "second"):
            receipt = export_analysis(
                library,
                store / name,
                project_root=project,
                store_root=store,
                query="lattice",
                limit=1,
                target="lingframe",
            )
            assert receipt["selection"]["matched_records"] == 2
            assert receipt["selection"]["selected_records"] == 1
            assert receipt["selection"]["remaining_records"] == 1
        assert (store / "first/lingframe.json").read_bytes() == (
            store / "second/lingframe.json"
        ).read_bytes()
        for kwargs in (
            {},
            {"query": "lattice", "uids": ["chatgpt-one"]},
            {"uids": ["missing"]},
            {"uids": ["chatgpt-one"], "limit": 0},
        ):
            with pytest.raises((ValueError, KeyError)):
                export_analysis(
                    library, store / "bad", project_root=project, store_root=store, **kwargs
                )
        assert not (store / "bad").exists()


@pytest.mark.parametrize("limit", ["max_source_bytes", "max_output_bytes", "max_atoms"])
def test_budgets_fail_without_partial_outputs(tmp_path: Path, limit: str) -> None:
    project, store, database = setup_library(tmp_path)
    with Library(database) as library, pytest.raises(ValueError, match="budget"):
        export_analysis(
            library,
            store / "limited",
            project_root=project,
            store_root=store,
            uids=["chatgpt-one"],
            limits=AnalysisLimits(**{limit: 1}),
        )
    assert not (store / "limited").exists()


def test_refuses_existing_and_source_destinations_and_writable_input(tmp_path: Path) -> None:
    project, store, database = setup_library(tmp_path)
    (store / "existing").mkdir()
    (store / "existing/note.md").write_text("Keep")
    with Library(database) as library:
        for destination in (store / "existing", database.parent / "analysis", tmp_path / "outside"):
            with pytest.raises((ValueError, FileExistsError)):
                export_analysis(
                    library,
                    destination,
                    project_root=project,
                    store_root=store,
                    uids=["chatgpt-one"],
                )
    with Library(database, writable=True) as library, pytest.raises(ValueError, match="read-only"):
        export_analysis(
            library,
            store / "writable",
            project_root=project,
            store_root=store,
            uids=["chatgpt-one"],
        )
    assert (store / "existing/note.md").read_text() == "Keep"


def test_declared_size_cannot_bypass_decompression_budget(tmp_path: Path) -> None:
    project, store, database = setup_library(tmp_path)
    with Library(database, writable=True) as library:
        library.put(
            Conversation(
                provider="chatgpt",
                source_id="large",
                title="Large",
                messages=[Message(role="user", text="x" * 1000000)],
            )
        )
        library.connection.execute("UPDATE objects SET raw_bytes=1")
        library.connection.commit()
    with Library(database) as library, pytest.raises(ValueError, match="budget"):
        export_analysis(
            library,
            store / "bomb",
            project_root=project,
            store_root=store,
            uids=["chatgpt-large"],
            limits=AnalysisLimits(max_source_bytes=2000),
        )
    assert not (store / "bomb").exists()


def test_deadline_and_object_integrity_fail_before_publication(tmp_path: Path) -> None:
    project, store, database = setup_library(tmp_path)
    with Library(database) as library, pytest.raises(TimeoutError, match="time budget"):
        export_analysis(
            library,
            store / "expired",
            project_root=project,
            store_root=store,
            query="lattice",
            limits=AnalysisLimits(max_seconds=1e-12),
        )
    assert not (store / "expired").exists()
    with Library(database, writable=True) as library:
        digest = library.connection.execute(
            "SELECT current_digest FROM records WHERE uid='chatgpt-one'"
        ).fetchone()[0]
        raw = b"{}"
        library.connection.execute(
            "UPDATE objects SET payload=?,raw_bytes=? WHERE digest=?",
            (zlib.compress(raw), len(raw), digest),
        )
        library.connection.commit()
    with Library(database) as library, pytest.raises(ValueError, match="digest mismatch"):
        export_analysis(
            library,
            store / "tampered",
            project_root=project,
            store_root=store,
            uids=["chatgpt-one"],
        )
    assert not (store / "tampered").exists()


def test_single_record_keeps_native_document_envelope_and_stable_occurrence_ids(
    tmp_path: Path,
) -> None:
    project, store, database = setup_library(tmp_path)
    with Library(database) as library:
        for name in ("single", "multiple"):
            uids = ["chatgpt-one"] if name == "single" else ["chatgpt-one", "chatgpt-two"]
            export_analysis(
                library, store / name, project_root=project, store_root=store, uids=uids
            )
    first = json.loads((store / "single/lingframe.json").read_text())
    second = json.loads((store / "multiple/lingframe.json").read_text())
    assert first["documents"][0] == second["documents"][0]
    assert first["documents"][0]["id"] == "chatgpt-one"
    assert first["documents"][0]["metadata"]["cce"]["provenance"]
    assert all(
        message["id"] != other["id"]
        for message in second["documents"][0]["themes"]
        for other in second["documents"][1]["themes"]
    )


def test_analysis_preserves_callers_sql_deadline_handler(tmp_path: Path) -> None:
    project, store, database = setup_library(tmp_path)
    calls = []

    def outer_deadline() -> None:
        calls.append(True)

    with Library(database) as library, library.sql_progress(outer_deadline, instructions=1):
        export_analysis(
            library, store / "nested", project_root=project, store_root=store, uids=["chatgpt-one"]
        )
        calls.clear()
        library.connection.execute("SELECT COUNT(*) FROM records").fetchone()
        assert calls


@pytest.mark.parametrize("query", ["", " ", "\n"])
def test_blank_query_cannot_ignore_explicit_uids(tmp_path: Path, query: str) -> None:
    project, store, database = setup_library(tmp_path)
    with Library(database) as library, pytest.raises(ValueError, match="query"):
        export_analysis(
            library,
            store / "blank",
            project_root=project,
            store_root=store,
            query=query,
            uids=["chatgpt-one"],
        )
    assert not (store / "blank").exists()
