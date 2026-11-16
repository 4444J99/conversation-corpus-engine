from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import conversation_corpus_engine.provider_refresh as provider_refresh_module
from conversation_corpus_engine.corpus_store import register_corpus_store
from conversation_corpus_engine.federation import list_registered_corpora, upsert_corpus
from conversation_corpus_engine.provider_refresh import refresh_provider_corpus
from conversation_corpus_engine.schema_validation import validate_json_file
from conversation_corpus_engine.source_policy import load_source_policy, set_source_policy


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


def tree_snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class ProviderRefreshTests(unittest.TestCase):
    def test_one_level_candidate_keeps_receipts_outside_immutable_staged_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir)
            project_root = workspace_root / "project"
            project_root.mkdir()
            corpus_store_root = workspace_root / "corpus-store"
            corpus_store_root.mkdir()
            source_drop_root = workspace_root / "source-drop"
            inbox = source_drop_root / "perplexity" / "inbox"
            inbox.mkdir(parents=True)
            (inbox / "export.md").write_text(
                "# Fresh Export\n\nKeep refresh receipts outside the candidate.\n",
                encoding="utf-8",
            )
            live_root = workspace_root / "perplexity-history-memory"
            seed_valid_corpus(live_root)
            upsert_corpus(
                project_root,
                live_root,
                corpus_id="perplexity-history-memory",
                name="Perplexity History Memory",
                make_default=True,
            )
            register_corpus_store(project_root, corpus_store_root)
            set_source_policy(
                project_root,
                "perplexity",
                primary_root=live_root,
                primary_corpus_id="perplexity-history-memory",
                decision="manual",
            )
            candidate_root = corpus_store_root / "candidate-corpus"
            original_stage = provider_refresh_module.stage_corpus_candidate
            staged_snapshot: dict[str, dict[str, bytes]] = {}

            def capture_staged_candidate(*args: object, **kwargs: object) -> dict[str, object]:
                result = original_stage(*args, **kwargs)
                staged_snapshot["files"] = tree_snapshot(candidate_root)
                return result

            with patch.object(
                provider_refresh_module,
                "stage_corpus_candidate",
                side_effect=capture_staged_candidate,
            ):
                payload = refresh_provider_corpus(
                    project_root=project_root,
                    provider="perplexity",
                    source_drop_root=source_drop_root,
                    corpus_store_root=corpus_store_root,
                    candidate_root=candidate_root,
                )

            refresh_json_path = Path(payload["refresh_json_path"])
            refresh_markdown_path = Path(payload["refresh_markdown_path"])
            expected_receipt_root = (
                corpus_store_root / "provider-refresh" / "perplexity" / payload["run_id"]
            )
            validation = validate_json_file("provider-refresh", refresh_json_path)

            self.assertEqual(Path(payload["candidate_root"]), candidate_root.resolve())
            self.assertEqual(refresh_json_path.parent, expected_receipt_root.resolve())
            self.assertEqual(refresh_markdown_path.parent, expected_receipt_root.resolve())
            self.assertFalse(refresh_json_path.is_relative_to(candidate_root.resolve()))
            self.assertFalse(refresh_markdown_path.is_relative_to(candidate_root.resolve()))
            self.assertFalse((corpus_store_root / "refresh.json").exists())
            self.assertFalse((candidate_root / "refresh.json").exists())
            self.assertEqual(tree_snapshot(candidate_root), staged_snapshot["files"])
            self.assertTrue(validation["valid"], validation["errors"])

    def test_refresh_provider_corpus_stages_candidate_without_mutating_live_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir)
            project_root = workspace_root / "project"
            project_root.mkdir()
            corpus_store_root = workspace_root / "corpus-store"
            corpus_store_root.mkdir()
            source_drop_root = workspace_root / "source-drop"
            inbox = source_drop_root / "perplexity" / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            (inbox / "export.md").write_text(
                "# Fresh Export\n\nPerplexity now prefers the provider refresh orchestration.\n",
                encoding="utf-8",
            )

            live_root = workspace_root / "perplexity-history-memory"
            seed_valid_corpus(live_root)
            upsert_corpus(
                project_root,
                live_root,
                corpus_id="perplexity-history-memory",
                name="Perplexity History Memory",
                make_default=True,
            )
            register_corpus_store(project_root, corpus_store_root)
            set_source_policy(
                project_root,
                "perplexity",
                primary_root=live_root,
                primary_corpus_id="perplexity-history-memory",
                decision="manual",
                note="Use the live Perplexity corpus.",
            )

            payload = refresh_provider_corpus(
                project_root=project_root,
                provider="perplexity",
                source_drop_root=source_drop_root,
                corpus_store_root=corpus_store_root,
                note="Stage a refreshed Perplexity corpus.",
            )

            registry = list_registered_corpora(project_root)
            policy = load_source_policy(project_root, "perplexity")

            self.assertEqual(payload["candidate"]["status"], "staged")
            self.assertTrue(Path(payload["candidate_root"]).exists())
            self.assertTrue(
                Path(payload["candidate_root"])
                .resolve()
                .is_relative_to((corpus_store_root / "provider-refresh" / "perplexity").resolve())
            )
            self.assertTrue(payload["evaluation"]["ran"])
            self.assertTrue(Path(payload["evaluation"]["outputs"]["scorecard_md"]).exists())
            self.assertEqual(str(Path(registry[0]["root"]).resolve()), str(live_root.resolve()))
            self.assertEqual(policy["primary_root"], str(live_root.resolve()))
            self.assertIsNone(payload["promotion"])

    def test_refresh_provider_corpus_can_auto_promote_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir)
            project_root = workspace_root / "project"
            project_root.mkdir()
            corpus_store_root = workspace_root / "corpus-store"
            corpus_store_root.mkdir()
            source_drop_root = workspace_root / "source-drop"
            inbox = source_drop_root / "perplexity" / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            (inbox / "export.md").write_text(
                "# Promotion Export\n\nPromote the refreshed Perplexity corpus.\n",
                encoding="utf-8",
            )

            live_root = workspace_root / "perplexity-history-memory"
            seed_valid_corpus(live_root)
            upsert_corpus(
                project_root,
                live_root,
                corpus_id="perplexity-history-memory",
                name="Perplexity History Memory",
                make_default=True,
            )
            register_corpus_store(project_root, corpus_store_root)
            set_source_policy(
                project_root,
                "perplexity",
                primary_root=live_root,
                primary_corpus_id="perplexity-history-memory",
                decision="manual",
                note="Use the live Perplexity corpus.",
            )

            payload = refresh_provider_corpus(
                project_root=project_root,
                provider="perplexity",
                source_drop_root=source_drop_root,
                corpus_store_root=corpus_store_root,
                promote=True,
                note="Promote the refreshed Perplexity corpus.",
            )

            registry = list_registered_corpora(project_root)
            policy = load_source_policy(project_root, "perplexity")

            self.assertEqual(payload["candidate"]["status"], "promoted")
            self.assertIsNotNone(payload["review"])
            self.assertIsNotNone(payload["promotion"])
            self.assertEqual(registry[0]["root"], str(Path(payload["candidate_root"]).resolve()))
            self.assertEqual(policy["primary_root"], str(Path(payload["candidate_root"]).resolve()))


if __name__ == "__main__":
    unittest.main()
