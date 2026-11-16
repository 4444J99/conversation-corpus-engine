from __future__ import annotations

import json
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conversation_corpus_engine.corpus_store import register_corpus_store
from conversation_corpus_engine.federation import list_registered_corpora, upsert_corpus
from conversation_corpus_engine.provider_discovery import discover_provider_uploads
from conversation_corpus_engine.provider_readiness import build_provider_readiness


def seed_valid_corpus(
    root: Path, *, adapter_type: str = "perplexity-export", gate_state: str = "pass"
) -> None:
    corpus_dir = root / "corpus"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    (root / "eval").mkdir(parents=True, exist_ok=True)
    for rel in (
        "threads-index.json",
        "pairs-index.json",
        "doctrine-briefs.json",
        "family-dossiers.json",
        "canonical-families.json",
        "action-ledger.json",
        "unresolved-ledger.json",
        "canonical-entities.json",
    ):
        (corpus_dir / rel).write_text("[]", encoding="utf-8")
    (corpus_dir / "semantic-v3-index.json").write_text('{"threads":[]}', encoding="utf-8")
    (corpus_dir / "evaluation-summary.json").write_text(
        json.dumps({"regression_gates": {"overall_state": gate_state}}),
        encoding="utf-8",
    )
    (corpus_dir / "regression-gates.json").write_text(
        json.dumps({"overall_state": gate_state, "source_reliability_state": "pass"}),
        encoding="utf-8",
    )
    (corpus_dir / "contract.json").write_text(
        json.dumps(
            {
                "contract_name": "conversation-corpus-engine-v1",
                "contract_version": 1,
                "adapter_type": adapter_type,
                "name": "Perplexity History Memory",
            },
        ),
        encoding="utf-8",
    )
    (root / "eval" / "manual-review-guide.md").write_text("# Manual Review\n", encoding="utf-8")


class ProviderReadinessTests(unittest.TestCase):
    def test_discover_provider_uploads_marks_ready_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir) / "project"
            source_drop_root = Path(tmpdir) / "source-drop"
            inbox = source_drop_root / "perplexity" / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            (inbox / "export.md").write_text("# export\n", encoding="utf-8")

            payload = discover_provider_uploads(project_root, source_drop_root)
            perplexity = next(
                item for item in payload["providers"] if item["provider"] == "perplexity"
            )

            self.assertEqual(perplexity["upload_state"], "ready")
            self.assertEqual(perplexity["detected_source_path"], str(inbox.resolve()))

    def test_build_provider_readiness_reports_registered_active_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir) / "project"
            project_root.mkdir()
            source_drop_root = Path(tmpdir) / "source-drop"
            inbox = source_drop_root / "perplexity" / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            (inbox / "export.md").write_text("# export\n", encoding="utf-8")

            corpus_store_root = Path(tmpdir) / "corpus-store"
            corpus_store_root.mkdir()
            corpus_root = corpus_store_root / "perplexity-history-memory"
            seed_valid_corpus(corpus_root)
            register_corpus_store(project_root, corpus_store_root)
            upsert_corpus(
                project_root,
                corpus_root,
                corpus_id="perplexity-history-memory",
                name="Perplexity History Memory",
            )

            payload = build_provider_readiness(project_root, source_drop_root)
            readiness = next(
                item for item in payload["providers"] if item["provider"] == "perplexity"
            )
            corpora = list_registered_corpora(project_root)

            self.assertEqual(readiness["overall_state"], "healthy-federation")
            self.assertIn("cce provider refresh --provider perplexity", readiness["next_command"])
            self.assertIn(
                f"--corpus-store-root {corpus_store_root.resolve()}",
                readiness["next_command"],
            )
            self.assertEqual(len(corpora), 1)

    def test_build_provider_readiness_requires_store_setup_before_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir) / "project"
            source_drop_root = Path(tmpdir) / "source-drop"
            inbox = source_drop_root / "perplexity" / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            (inbox / "export.md").write_text("# export\n", encoding="utf-8")

            payload = build_provider_readiness(project_root, source_drop_root)
            readiness = next(
                item for item in payload["providers"] if item["provider"] == "perplexity"
            )

            self.assertEqual(
                readiness["next_command"],
                (
                    "cce corpus-store register "
                    f"--project-root {project_root.resolve()} "
                    "--root /path/to/private-corpus-store"
                ),
            )

    def test_legacy_out_of_store_bootstrap_target_emits_migration_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir) / "project"
            project_root.mkdir()
            source_drop_root = Path(tmpdir) / "source-drop"
            corpus_store_root = Path(tmpdir) / "corpus-store"
            corpus_store_root.mkdir()
            legacy_root = Path(tmpdir) / "perplexity-history-memory"
            seed_valid_corpus(legacy_root)
            (legacy_root / "eval" / "manual-review-guide.md").unlink()
            upsert_corpus(
                project_root,
                legacy_root,
                corpus_id="perplexity-history-memory",
                name="Perplexity History Memory",
            )
            register_corpus_store(project_root, corpus_store_root)

            payload = build_provider_readiness(project_root, source_drop_root)
            readiness = next(
                item for item in payload["providers"] if item["provider"] == "perplexity"
            )

            self.assertEqual(readiness["overall_state"], "imported-needs-bootstrap")
            self.assertIn("Migrate or re-import", readiness["next_command"])
            self.assertIn(str(corpus_store_root.resolve()), readiness["next_command"])
            self.assertNotIn("cce provider bootstrap-eval", readiness["next_command"])

    def test_missing_target_import_remains_store_backed_and_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir) / "project with spaces"
            project_root.mkdir()
            source_drop_root = Path(tmpdir) / "source drop"
            inbox = source_drop_root / "perplexity" / "inbox"
            inbox.mkdir(parents=True)
            (inbox / "export.md").write_text("# export\n", encoding="utf-8")
            corpus_store_root = Path(tmpdir) / "corpus store"
            corpus_store_root.mkdir()
            register_corpus_store(project_root, corpus_store_root)

            payload = build_provider_readiness(project_root, source_drop_root)
            readiness = next(
                item for item in payload["providers"] if item["provider"] == "perplexity"
            )
            command = shlex.split(readiness["next_command"])

            self.assertEqual(readiness["overall_state"], "missing")
            self.assertEqual(command[:4], ["cce", "provider", "import", "--provider"])
            self.assertEqual(
                command[command.index("--project-root") + 1],
                str(project_root.resolve()),
            )
            self.assertEqual(
                command[command.index("--source-drop-root") + 1],
                str(source_drop_root.resolve()),
            )
            self.assertEqual(
                command[command.index("--corpus-store-root") + 1],
                str(corpus_store_root.resolve()),
            )

    def test_healthy_legacy_target_refresh_remains_store_backed_and_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir) / "project"
            project_root.mkdir()
            source_drop_root = Path(tmpdir) / "source-drop"
            inbox = source_drop_root / "perplexity" / "inbox"
            inbox.mkdir(parents=True)
            (inbox / "export.md").write_text("# export\n", encoding="utf-8")
            corpus_store_root = Path(tmpdir) / "corpus-store"
            corpus_store_root.mkdir()
            legacy_root = Path(tmpdir) / "perplexity-history-memory"
            seed_valid_corpus(legacy_root)
            upsert_corpus(
                project_root,
                legacy_root,
                corpus_id="perplexity-history-memory",
                name="Perplexity History Memory",
            )
            register_corpus_store(project_root, corpus_store_root)

            payload = build_provider_readiness(project_root, source_drop_root)
            readiness = next(
                item for item in payload["providers"] if item["provider"] == "perplexity"
            )
            command = shlex.split(readiness["next_command"])

            self.assertEqual(readiness["overall_state"], "healthy-federation")
            self.assertEqual(command[:4], ["cce", "provider", "refresh", "--provider"])
            self.assertEqual(
                command[command.index("--corpus-store-root") + 1],
                str(corpus_store_root.resolve()),
            )

    def test_build_provider_readiness_prefers_registered_chatgpt_fallback_when_default_missing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir) / "project"
            source_drop_root = Path(tmpdir) / "source-drop"
            chatgpt_root = source_drop_root.parent / "chatgpt-history"

            seed_valid_corpus(chatgpt_root, adapter_type="chatgpt-history")
            upsert_corpus(
                project_root,
                chatgpt_root,
                corpus_id="chatgpt-history",
                name="ChatGPT History",
            )

            payload = build_provider_readiness(project_root, source_drop_root)
            readiness = next(item for item in payload["providers"] if item["provider"] == "chatgpt")

            self.assertEqual(readiness["overall_state"], "healthy-federation")
            self.assertEqual(readiness["selected_target"]["corpus_id"], "chatgpt-history")
            self.assertEqual(readiness["next_command"], "ready")


if __name__ == "__main__":
    unittest.main()
