from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conversation_corpus_engine.corpus_store import register_corpus_store
from conversation_corpus_engine.federation import list_registered_corpora
from conversation_corpus_engine.migration import migrate_corpus_v2, seed_registry_from_staging
from conversation_corpus_engine.sharded_collection import (
    collection_current_path,
    collection_storage_path,
    load_corpus_collection,
)


def seed_valid_corpus(root: Path, *, contract_name: str = "conversation-corpus-engine-v1") -> None:
    corpus_dir = root / "corpus"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    for rel in (
        "threads-index.json",
        "semantic-v3-index.json",
        "pairs-index.json",
        "doctrine-briefs.json",
        "family-dossiers.json",
    ):
        (corpus_dir / rel).write_text(
            "[]" if rel != "semantic-v3-index.json" else '{"threads":[]}', encoding="utf-8"
        )
    (corpus_dir / "canonical-families.json").write_text("[]", encoding="utf-8")
    (corpus_dir / "action-ledger.json").write_text("[]", encoding="utf-8")
    (corpus_dir / "unresolved-ledger.json").write_text("[]", encoding="utf-8")
    (corpus_dir / "canonical-entities.json").write_text("[]", encoding="utf-8")
    (corpus_dir / "evaluation-summary.json").write_text("{}", encoding="utf-8")
    (corpus_dir / "regression-gates.json").write_text("{}", encoding="utf-8")
    (corpus_dir / "contract.json").write_text(
        json.dumps(
            {
                "contract_name": contract_name,
                "contract_version": 1,
                "adapter_type": "markdown-document",
                "corpus_id": root.name,
                "name": root.name.replace("-", " ").title(),
                "generated_at": "2026-07-23T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )


class MigrationTests(unittest.TestCase):
    def test_seed_registry_from_staging_registers_valid_corpora(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir) / "project"
            staging_root = Path(tmpdir) / "staging"
            seed_valid_corpus(staging_root / "chatgpt-history")
            seed_valid_corpus(staging_root / "notes-memory")
            (staging_root / "ignored-dir").mkdir(parents=True, exist_ok=True)

            result = seed_registry_from_staging(project_root, staging_root)
            corpora = list_registered_corpora(project_root)

            self.assertEqual(result["registered_count"], 2)
            self.assertEqual(len(corpora), 2)
            self.assertEqual(corpora[0]["corpus_id"], "chatgpt-history")
            self.assertTrue(any(entry["default"] for entry in corpora))

    def test_corpus_v2_migration_is_read_only_by_default_and_idempotent_on_write(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            project_root = root / "project"
            source_root = root / "legacy"
            store_root = root / "store"
            destination_root = store_root / "migrated"
            project_root.mkdir()
            store_root.mkdir()
            seed_valid_corpus(source_root)
            (source_root / "corpus" / "threads-index.json").write_text(
                json.dumps([{"thread_uid": "thread-1", "title_normalized": "One"}]),
                encoding="utf-8",
            )
            (source_root / "source").mkdir()
            (source_root / "source" / "conversations.json").write_text(
                json.dumps([{"conversation_id": "conversation-1", "title": "One"}]),
                encoding="utf-8",
            )
            multipart_source = source_root / "source" / "part-2"
            multipart_source.mkdir()
            (multipart_source / "conversations.json").write_text(
                json.dumps([{"conversation_id": "conversation-2", "title": "Two"}]),
                encoding="utf-8",
            )
            (source_root / "corpus" / "action-ledger.json").write_text(
                json.dumps(
                    [
                        {
                            "action_key": "action-shared",
                            "canonical_action": "Ship the migration.",
                            "family_ids": ["family-1"],
                            "thread_uids": ["thread-1"],
                            "occurrence_count": 1,
                        },
                        {
                            "action_key": "action-shared",
                            "canonical_action": "Ship the migration.",
                            "family_ids": ["family-2"],
                            "thread_uids": ["thread-2"],
                            "occurrence_count": 1,
                        },
                    ]
                ),
                encoding="utf-8",
            )
            source_bytes = (source_root / "corpus" / "threads-index.json").read_bytes()
            register_corpus_store(project_root, store_root)

            dry_run = migrate_corpus_v2(
                project_root=project_root,
                source_root=source_root,
                destination_root=destination_root,
                corpus_store_root=store_root,
            )
            self.assertEqual(dry_run["mode"], "read-only")
            self.assertFalse(destination_root.exists())

            first = migrate_corpus_v2(
                project_root=project_root,
                source_root=source_root,
                destination_root=destination_root,
                corpus_store_root=store_root,
                write=True,
            )
            pointer_before = collection_current_path(
                destination_root / "corpus" / "threads-index.json"
            ).read_text(encoding="utf-8")
            second = migrate_corpus_v2(
                project_root=project_root,
                source_root=source_root,
                destination_root=destination_root,
                corpus_store_root=store_root,
                write=True,
            )
            pointer_after = collection_current_path(
                destination_root / "corpus" / "threads-index.json"
            ).read_text(encoding="utf-8")

            self.assertEqual(first["generation_ids"], second["generation_ids"])
            self.assertTrue(first["contract_validation"]["valid"])
            self.assertEqual(pointer_before, pointer_after)
            self.assertEqual(
                load_corpus_collection(destination_root / "corpus" / "threads-index.json"),
                [{"thread_uid": "thread-1", "title_normalized": "One"}],
            )
            self.assertTrue(
                collection_storage_path(destination_root / "source" / "conversations.json").exists()
            )
            self.assertTrue(
                collection_storage_path(
                    destination_root / "source" / "part-2" / "conversations.json"
                ).exists()
            )
            self.assertFalse((destination_root / "source" / "conversations.json").exists())
            self.assertEqual(
                (source_root / "corpus" / "threads-index.json").read_bytes(),
                source_bytes,
            )
            migrated_actions = load_corpus_collection(
                destination_root / "corpus" / "action-ledger.json"
            )
            self.assertEqual(len(migrated_actions), 1)
            self.assertEqual(migrated_actions[0]["occurrence_count"], 2)


if __name__ == "__main__":
    unittest.main()
