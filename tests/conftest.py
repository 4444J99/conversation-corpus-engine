from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from archive_support import build_export

from conversation_corpus_engine.archive_ingest import (
    DEFAULT_ARCHIVE_DIRNAME,
    ArchivePaths,
    authorize_archive,
)
from conversation_corpus_engine.corpus_store import register_corpus_store


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Isolated workspace root with project and source-drop subdirectories."""
    project_root = tmp_path / "project"
    source_drop_root = tmp_path / "source-drop"
    project_root.mkdir()
    source_drop_root.mkdir()
    return tmp_path


@pytest.fixture
def project_root(workspace: Path) -> Path:
    return workspace / "project"


@pytest.fixture
def source_drop_root(workspace: Path) -> Path:
    return workspace / "source-drop"


@pytest.fixture
def archive_env(tmp_path: Path):
    """Synthetic export plus a registered private store and an authorized archive root."""
    export = build_export(tmp_path / "export")
    project = tmp_path / "project"
    store = tmp_path / "store"
    project.mkdir(exist_ok=True)
    store.mkdir()
    register_corpus_store(project, store)
    authorization = authorize_archive(
        project_root=project,
        archive_root=store / DEFAULT_ARCHIVE_DIRNAME,
        corpus_store_root=store,
        source_roots=(export,),
    )
    return SimpleNamespace(
        export=export,
        project=project,
        store=store,
        auth=authorization,
        paths=ArchivePaths(authorization.destination),
    )


def seed_minimal_corpus(
    root: Path,
    *,
    corpus_id: str = "test-corpus",
    name: str = "Test Corpus",
    adapter_type: str = "test-adapter",
    gate_state: str = "pass",
    thread_count: int = 1,
) -> Path:
    """Seed a minimal valid corpus at `root` with configurable gate state."""
    corpus_dir = root / "corpus"
    eval_dir = root / "eval"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    eval_dir.mkdir(parents=True, exist_ok=True)

    threads = [
        {
            "thread_uid": f"thread-test-{i:03d}",
            "title_normalized": f"Test Thread {i}",
            "family_ids": [f"family-test-{i:03d}"],
        }
        for i in range(thread_count)
    ]
    families = [
        {
            "canonical_family_id": f"family-test-{i:03d}",
            "canonical_title": f"Test Family {i}",
            "canonical_thread_uid": f"thread-test-{i:03d}",
            "thread_uids": [f"thread-test-{i:03d}"],
        }
        for i in range(thread_count)
    ]
    doctrine_briefs = [
        {
            "family_id": f"family-test-{i:03d}",
            "canonical_title": f"Test Family {i}",
            "canonical_thread_uid": f"thread-test-{i:03d}",
            "stable_themes": ["test", "fixture"],
            "brief_text": f"Test brief {i}",
        }
        for i in range(thread_count)
    ]

    for rel, content in {
        "threads-index.json": threads,
        "pairs-index.json": [],
        "doctrine-briefs.json": doctrine_briefs,
        "family-dossiers.json": [],
        "canonical-families.json": families,
        "action-ledger.json": [],
        "unresolved-ledger.json": [],
        "canonical-entities.json": [],
    }.items():
        (corpus_dir / rel).write_text(json.dumps(content), encoding="utf-8")

    (corpus_dir / "semantic-v3-index.json").write_text(
        json.dumps({"threads": []}),
        encoding="utf-8",
    )
    (corpus_dir / "evaluation-summary.json").write_text(
        json.dumps({"regression_gates": {"overall_state": gate_state}}),
        encoding="utf-8",
    )
    (corpus_dir / "regression-gates.json").write_text(
        json.dumps(
            {
                "overall_state": gate_state,
                "source_reliability_state": "pass",
            }
        ),
        encoding="utf-8",
    )
    (corpus_dir / "contract.json").write_text(
        json.dumps(
            {
                "contract_name": "conversation-corpus-engine-v1",
                "contract_version": 1,
                "adapter_type": adapter_type,
                "corpus_id": corpus_id,
                "name": name,
                "generated_at": "2026-03-25T00:00:00+00:00",
                "required_files": [
                    "corpus/threads-index.json",
                    "corpus/semantic-v3-index.json",
                    "corpus/pairs-index.json",
                    "corpus/doctrine-briefs.json",
                    "corpus/family-dossiers.json",
                ],
                "counts": {
                    "threads": thread_count,
                    "families": thread_count,
                    "actions": 0,
                    "unresolved": 0,
                    "entities": 0,
                },
            }
        ),
        encoding="utf-8",
    )
    (eval_dir / "manual-review-guide.md").write_text("# Manual Review\n", encoding="utf-8")
    return root


def write_markdown_sources(root: Path, files: dict[str, str]) -> None:
    """Write markdown files into a directory tree for import testing."""
    for relative_path, content in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def seed_provider_inbox(source_drop_root: Path, provider: str, files: dict[str, str]) -> Path:
    """Seed a provider inbox under source-drop with the given files."""
    inbox = source_drop_root / provider / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (inbox / name).write_text(content, encoding="utf-8")
    return inbox
