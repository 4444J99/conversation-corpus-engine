from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conversation_corpus_engine.corpus_candidates import stage_corpus_candidate
from conversation_corpus_engine.corpus_store import register_corpus_store
from conversation_corpus_engine.federation import upsert_corpus
from conversation_corpus_engine.governance_policy import (
    load_or_create_promotion_policy,
    promotion_policy_path,
)
from conversation_corpus_engine.import_claude_export_corpus import import_claude_export_corpus
from conversation_corpus_engine.import_document_export_corpus import import_document_export_corpus
from conversation_corpus_engine.import_markdown_document_corpus import (
    import_markdown_document_corpus,
)
from conversation_corpus_engine.provider_refresh import refresh_provider_corpus
from conversation_corpus_engine.schema_validation import (
    list_schemas,
    validate_json_file,
    validate_payload,
)
from conversation_corpus_engine.source_policy import set_source_policy, source_policy_path


def write_markdown_sources(root: Path, files: dict[str, str]) -> None:
    for relative_path, content in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


class SchemaValidationTests(unittest.TestCase):
    def test_list_schemas_returns_publishable_catalog(self) -> None:
        catalog = list_schemas()
        names = [entry["name"] for entry in catalog]

        self.assertEqual(
            names,
            [
                "commercial-awareness",
                "corpus-candidate",
                "corpus-contract",
                "import-audit",
                "mcp-context",
                "near-duplicates",
                "promotion-policy",
                "provider-refresh",
                "sharded-collection",
                "source-policy",
                "surface-bundle",
                "surface-manifest",
            ],
        )
        for entry in catalog:
            self.assertTrue(Path(entry["path"]).exists())

    def test_validate_real_corpus_contract_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir)
            export_root = workspace_root / "perplexity-export"
            output_root = workspace_root / "perplexity-history-memory"
            write_markdown_sources(
                export_root,
                {
                    "query.md": "# Search Doctrine\n\nNeed to preserve provider export normalization.\n\nMaybe keep evidence links grouped by source.\n",
                },
            )

            import_document_export_corpus(
                export_root,
                output_root,
                provider_slug="perplexity",
                corpus_id="perplexity-history-memory",
                name="Perplexity History Memory",
            )
            result = validate_json_file("corpus-contract", output_root / "corpus" / "contract.json")

            self.assertTrue(result["valid"], result["errors"])

    def test_validate_real_projector_contract_artifact(self) -> None:
        # The rewire (#78) retired the importers' import-audit and
        # near-duplicates files; the artifact a real import now validates is
        # the projector's corpus contract.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir)
            bundle_root = workspace_root / "claude-export"
            output_root = workspace_root / "claude-history-memory"
            prompt = "Please build a deterministic federation adapter with audit visibility."
            bundle_root.mkdir(parents=True, exist_ok=True)
            (bundle_root / "users.json").write_text(
                '[{"uuid":"user-1","full_name":"Schema Test User"}]',
                encoding="utf-8",
            )
            (bundle_root / "projects.json").write_text("[]", encoding="utf-8")
            (bundle_root / "memories.json").write_text("[]", encoding="utf-8")
            (bundle_root / "conversations.json").write_text(
                json.dumps(
                    [
                        {
                            "uuid": "conv-1",
                            "name": "Audit Thread A",
                            "created_at": "2026-03-14T10:00:00Z",
                            "updated_at": "2026-03-14T10:05:00Z",
                            "chat_messages": [
                                {
                                    "uuid": "msg-1",
                                    "sender": "human",
                                    "created_at": "2026-03-14T10:00:00Z",
                                    "updated_at": "2026-03-14T10:00:00Z",
                                    "text": prompt,
                                    "content": [{"type": "text", "text": prompt}],
                                    "attachments": [],
                                    "files": [],
                                },
                                {
                                    "uuid": "msg-2",
                                    "sender": "assistant",
                                    "created_at": "2026-03-14T10:01:00Z",
                                    "updated_at": "2026-03-14T10:01:00Z",
                                    "text": "Ready.",
                                    "content": [{"type": "text", "text": "Ready."}],
                                    "attachments": [],
                                    "files": [],
                                },
                            ],
                        },
                        {
                            "uuid": "conv-2",
                            "name": "Audit Thread B",
                            "created_at": "2026-03-14T11:00:00Z",
                            "updated_at": "2026-03-14T11:05:00Z",
                            "chat_messages": [
                                {
                                    "uuid": "msg-3",
                                    "sender": "human",
                                    "created_at": "2026-03-14T11:00:00Z",
                                    "updated_at": "2026-03-14T11:00:00Z",
                                    "text": prompt,
                                    "content": [{"type": "text", "text": prompt}],
                                    "attachments": [],
                                    "files": [],
                                },
                                {
                                    "uuid": "msg-4",
                                    "sender": "assistant",
                                    "created_at": "2026-03-14T11:01:00Z",
                                    "updated_at": "2026-03-14T11:01:00Z",
                                    "text": "Still ready.",
                                    "content": [{"type": "text", "text": "Still ready."}],
                                    "attachments": [],
                                    "files": [],
                                },
                            ],
                        },
                    ]
                ),
                encoding="utf-8",
            )

            import_claude_export_corpus(
                bundle_root,
                output_root,
                corpus_id="claude-history-memory",
                name="Claude History Memory",
            )

            contract_result = validate_json_file(
                "corpus-contract",
                output_root / "corpus" / "contract.json",
            )

            self.assertTrue(contract_result["valid"], contract_result["errors"])
            self.assertFalse((output_root / "corpus" / "import-audit.json").exists())
            self.assertFalse((output_root / "corpus" / "near-duplicates.json").exists())

    def test_validate_source_policy_and_promotion_policy_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir)
            project_root = workspace_root / "project"
            project_root.mkdir()
            primary_root = workspace_root / "gemini-history-memory"
            fallback_root = workspace_root / "gemini-archive-memory"
            primary_root.mkdir(parents=True, exist_ok=True)
            fallback_root.mkdir(parents=True, exist_ok=True)

            set_source_policy(
                project_root,
                "gemini",
                primary_root=primary_root,
                primary_corpus_id="gemini-history-memory",
                fallback_root=fallback_root,
                fallback_corpus_id="gemini-archive-memory",
                decision="manual-override",
                note="Prefer the curated Gemini history corpus.",
            )
            live_policy = load_or_create_promotion_policy(project_root)

            source_result = validate_json_file(
                "source-policy",
                source_policy_path(project_root, "gemini"),
            )
            promotion_file_result = validate_json_file(
                "promotion-policy",
                promotion_policy_path(project_root),
            )
            promotion_payload_result = validate_payload("promotion-policy", live_policy)

            self.assertTrue(source_result["valid"], source_result["errors"])
            self.assertTrue(promotion_file_result["valid"], promotion_file_result["errors"])
            self.assertTrue(promotion_payload_result["valid"], promotion_payload_result["errors"])

    def test_validate_real_corpus_candidate_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir)
            project_root = workspace_root / "project"
            live_source = workspace_root / "live-source"
            candidate_source = workspace_root / "candidate-source"
            live_root = workspace_root / "claude-live-memory"
            candidate_root = workspace_root / "claude-candidate-memory"
            write_markdown_sources(
                live_source,
                {
                    "launch.md": "# Alpha Launch\n\nNeed to stabilize the alpha rollout.\n\nMaybe keep the review lane manual for now.\n",
                },
            )
            write_markdown_sources(
                candidate_source,
                {
                    "launch.md": "# Beta Launch\n\nNeed to promote the refreshed rollout candidate.\n\nMaybe widen doctrine coverage once the diff is reviewed.\n",
                },
            )

            import_markdown_document_corpus(
                live_source,
                live_root,
                corpus_id="claude-local-session-memory",
                name="Claude Local Session Memory",
            )
            import_markdown_document_corpus(
                candidate_source,
                candidate_root,
                corpus_id="claude-local-session-memory",
                name="Claude Local Session Memory",
            )
            upsert_corpus(
                project_root,
                live_root,
                corpus_id="claude-local-session-memory",
                name="Claude Local Session Memory",
                make_default=True,
            )
            set_source_policy(
                project_root,
                "claude",
                primary_root=live_root,
                primary_corpus_id="claude-local-session-memory",
                decision="manual",
                note="Use the live Claude corpus.",
            )

            staged = stage_corpus_candidate(
                project_root,
                candidate_root=candidate_root,
                provider="claude",
                note="Stage the refreshed Claude corpus.",
            )
            result = validate_json_file("corpus-candidate", Path(staged["manifest_path"]))

            self.assertTrue(result["valid"], result["errors"])

    def test_validate_real_provider_refresh_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir)
            project_root = workspace_root / "project"
            project_root.mkdir()
            corpus_store_root = workspace_root / "corpus-store"
            corpus_store_root.mkdir()
            live_source = workspace_root / "live-source"
            live_root = workspace_root / "perplexity-history-memory"
            source_drop_root = workspace_root / "source-drop"
            inbox = source_drop_root / "perplexity" / "inbox"
            write_markdown_sources(
                live_source,
                {
                    "baseline.md": "# Baseline Memory\n\nNeed to preserve the live Perplexity baseline.\n\nMaybe keep a staged refresh lane for replacements.\n",
                },
            )
            write_markdown_sources(
                inbox,
                {
                    "export.md": "# Fresh Export\n\nNeed to import the refreshed Perplexity export.\n\nMaybe review the candidate before promotion.\n",
                },
            )

            import_markdown_document_corpus(
                live_source,
                live_root,
                corpus_id="perplexity-history-memory",
                name="Perplexity History Memory",
            )
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
            result = validate_json_file("provider-refresh", Path(payload["refresh_json_path"]))

            self.assertTrue(result["valid"], result["errors"])

    def test_validate_payload_reports_missing_required_property_path(self) -> None:
        result = validate_payload(
            "source-policy",
            {
                "provider": "claude",
                "generated_at": "2026-03-21T00:00:00+00:00",
                "decision": "manual",
                "primary_corpus_id": "claude-local-session-memory",
                "note": "Missing the primary root on purpose.",
            },
        )
        error_paths = {issue["path"] for issue in result["errors"]}

        self.assertFalse(result["valid"])
        self.assertIn("$.primary_root", error_paths)


if __name__ == "__main__":
    unittest.main()
