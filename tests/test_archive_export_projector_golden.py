"""Golden comparison for conversation-corpus-engine#78.

The legacy export importers now route through the archive corpus projector
(see ``export_projector``): a bundle is ingested into a temporary normalized
store and projected, so the importer and ``cce archive project corpus`` must
write the same contracts and member ids over the same synthetic fixture.

The fixture is ingested once through the archive path and once through each
importer. Collection bytes must match exactly. Contracts match except for
``source_content_fingerprint``: that fingerprint hashes the source layout, and
the importer bundle layout (one bundle directory) legitimately differs from
the archive export-root layout (the fixture's sharded ``conversations-000.json``
for ChatGPT), so the two fingerprints name different layouts of the same
conversations. Every other contract field — including counts, corpus id,
adapter type and naming/schema versions — must match.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from conversation_corpus_engine.answering import load_json
from conversation_corpus_engine.archive_corpus import ALL_COLLECTIONS, project_corpus
from conversation_corpus_engine.archive_fixture import write_fixture
from conversation_corpus_engine.archive_ingest import (
    DEFAULT_ARCHIVE_DIRNAME,
    ArchivePaths,
    authorize_archive,
    ingest,
)
from conversation_corpus_engine.corpus_store import register_corpus_store
from conversation_corpus_engine.import_chatgpt_export_corpus import (
    import_chatgpt_export_corpus,
)
from conversation_corpus_engine.import_claude_export_corpus import (
    import_claude_export_corpus,
)
from conversation_corpus_engine.sharded_collection import load_corpus_collection

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


def _importer_bundle(tmp_path: Path, export: Path, provider: str) -> Path:
    bundle = tmp_path / f"bundle-{provider}"
    bundle.mkdir()
    if provider == "chatgpt":
        conversations = json.loads(
            (export / "chatgpt" / "conversations-000.json").read_text(encoding="utf-8")
        )
        (bundle / "conversations.json").write_text(
            json.dumps(conversations, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (bundle / "user.json").write_text("{}\n", encoding="utf-8")
    else:
        shutil.copy(export / "claude" / "conversations.json", bundle / "conversations.json")
        (bundle / "users.json").write_text("[]\n", encoding="utf-8")
        for name in ("projects-000.zip", "memories-000.zip"):
            shutil.copy(export / "claude" / name, bundle / name)
    return bundle


def _rows(corpus_dir: Path) -> dict[str, list[dict]]:
    return {
        name: load_corpus_collection(corpus_dir / name, default=[]) or []
        for name in ALL_COLLECTIONS
    }


def _ids(rows: dict[str, list[dict]]) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for records in rows.values():
        for row in records:
            for field in ID_FIELDS:
                if row.get(field):
                    found.setdefault(field, set()).add(str(row[field]))
    return found


@pytest.mark.parametrize("provider", ["chatgpt", "claude"])
def test_importer_matches_projector_over_fixture(tmp_path: Path, provider: str) -> None:
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
    ingest(export_root=export, paths=paths, authorization=authorization)
    projected_out = tmp_path / "projected"
    project_corpus(paths.root, projected_out, providers=[provider])
    projected_corpus = projected_out / f"corpus-{provider}-archive" / "corpus"

    importer = (
        import_chatgpt_export_corpus if provider == "chatgpt" else import_claude_export_corpus
    )
    output = tmp_path / f"imported-{provider}"
    result = importer(
        _importer_bundle(tmp_path, export, provider), output, corpus_id=f"golden-{provider}"
    )
    imported_corpus = output / "corpus"

    projected_rows = _rows(projected_corpus)
    imported_rows = _rows(imported_corpus)
    assert imported_rows == projected_rows
    assert _ids(imported_rows) == _ids(projected_rows)

    projected_contract = load_json(projected_corpus / "contract.json", default={}) or {}
    imported_contract = load_json(imported_corpus / "contract.json", default={}) or {}
    projected_contract.pop("source_content_fingerprint", None)
    imported_contract.pop("source_content_fingerprint", None)
    assert imported_contract == projected_contract
    assert imported_contract["adapter_type"] == "archive-normalized"
    assert imported_contract["corpus_id"] == f"corpus-{provider}-archive"
    assert result["thread_count"] == imported_contract["counts"]["threads"]
    assert result["pair_count"] == imported_contract["counts"]["pairs"]
    assert result["projector_corpus_id"] == f"corpus-{provider}-archive"
