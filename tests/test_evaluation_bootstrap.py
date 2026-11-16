from __future__ import annotations

import json
import re
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conversation_corpus_engine.corpus_store import CorpusStoreError, register_corpus_store
from conversation_corpus_engine.evaluation_bootstrap import (
    bootstrap_claude_evaluation,
    bootstrap_provider_evaluation,
)
from conversation_corpus_engine.federation import upsert_corpus


def seed_eval_target(root: Path, *, corpus_id: str, name: str, adapter_type: str) -> None:
    corpus = root / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    thread_uid = f"{corpus_id}-thread-001"
    family_id = f"{corpus_id}-family-001"
    pair_id = f"{thread_uid}-pair-001"
    (corpus / "threads-index.json").write_text(
        json.dumps(
            [{"thread_uid": thread_uid, "title_normalized": name, "family_ids": [family_id]}]
        ),
        encoding="utf-8",
    )
    (corpus / "semantic-v3-index.json").write_text(
        json.dumps(
            {
                "threads": [
                    {"thread_uid": thread_uid, "search_text": name, "family_ids": [family_id]}
                ]
            }
        ),
        encoding="utf-8",
    )
    (corpus / "pairs-index.json").write_text(
        json.dumps(
            [
                {
                    "pair_id": pair_id,
                    "thread_uid": thread_uid,
                    "family_ids": [family_id],
                    "search_text": name,
                }
            ]
        ),
        encoding="utf-8",
    )
    (corpus / "doctrine-briefs.json").write_text(
        json.dumps(
            [
                {
                    "family_id": family_id,
                    "canonical_title": name,
                    "canonical_thread_uid": thread_uid,
                    "stable_themes": [adapter_type],
                    "brief_text": f"{name} doctrine",
                },
            ],
        ),
        encoding="utf-8",
    )
    (corpus / "family-dossiers.json").write_text(
        json.dumps(
            [
                {
                    "family_id": family_id,
                    "canonical_title": name,
                    "canonical_thread_uid": thread_uid,
                    "actions": [],
                    "unresolved": [],
                    "key_entities": [],
                },
            ],
        ),
        encoding="utf-8",
    )
    (corpus / "canonical-families.json").write_text(
        json.dumps(
            [
                {
                    "canonical_family_id": family_id,
                    "canonical_title": name,
                    "canonical_thread_uid": thread_uid,
                    "thread_uids": [thread_uid],
                },
            ],
        ),
        encoding="utf-8",
    )
    (corpus / "action-ledger.json").write_text("[]", encoding="utf-8")
    (corpus / "unresolved-ledger.json").write_text("[]", encoding="utf-8")
    (corpus / "canonical-entities.json").write_text("[]", encoding="utf-8")
    (corpus / "entity-aliases.json").write_text("[]", encoding="utf-8")
    (corpus / "doctrine-timeline.json").write_text("[]", encoding="utf-8")
    (corpus / "contract.json").write_text(
        json.dumps(
            {
                "contract_name": "conversation-corpus-engine-v1",
                "contract_version": 1,
                "corpus_id": corpus_id,
                "name": name,
                "adapter_type": adapter_type,
            },
        ),
        encoding="utf-8",
    )
    (state / "canonical-decisions.json").write_text(json.dumps({}), encoding="utf-8")


class EvaluationBootstrapTests(unittest.TestCase):
    def test_manual_guide_commands_quote_authorized_project_and_store_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir)
            project_root = workspace_root / "project with spaces"
            project_root.mkdir()
            corpus_store_root = workspace_root / "corpus store"
            corpus_store_root.mkdir()
            target_root = corpus_store_root / "gemini history memory"
            seed_eval_target(
                target_root,
                corpus_id="gemini-history-memory",
                name="Gemini History Memory",
                adapter_type="gemini-export",
            )
            register_corpus_store(project_root, corpus_store_root)

            payload = bootstrap_provider_evaluation(
                project_root=project_root,
                provider="gemini",
                target_root=target_root,
                corpus_store_root=corpus_store_root,
            )

            guide = Path(payload["manual_guide_path"]).read_text(encoding="utf-8")
            commands = re.findall(r"`(cce [^`]+)`", guide)
            parsed_commands = [shlex.split(command) for command in commands]

            self.assertEqual(len(parsed_commands), 2)
            for parsed in parsed_commands:
                self.assertEqual(
                    parsed[parsed.index("--project-root") + 1],
                    str(project_root.resolve()),
                )
                self.assertEqual(
                    parsed[parsed.index("--corpus-store-root") + 1],
                    str(corpus_store_root.resolve()),
                )
            self.assertEqual(
                parsed_commands[0][parsed_commands[0].index("--root") + 1],
                str(target_root.resolve()),
            )
            self.assertEqual(
                parsed_commands[1][parsed_commands[1].index("--target-root") + 1],
                str(target_root.resolve()),
            )
            self.assertEqual(
                parsed_commands[1][parsed_commands[1].index("--provider") + 1],
                "gemini",
            )

    def test_manual_guide_omits_provider_bootstrap_without_real_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir) / "project"
            project_root.mkdir()
            corpus_store_root = Path(tmpdir) / "corpus-store"
            corpus_store_root.mkdir()
            target_root = corpus_store_root / "external-memory"
            seed_eval_target(
                target_root,
                corpus_id="external-memory",
                name="External Memory",
                adapter_type="external-export",
            )
            register_corpus_store(project_root, corpus_store_root)

            payload = bootstrap_provider_evaluation(
                project_root=project_root,
                provider=None,
                target_root=target_root,
                corpus_store_root=corpus_store_root,
            )

            guide = Path(payload["manual_guide_path"]).read_text(encoding="utf-8")
            commands = re.findall(r"`(cce [^`]+)`", guide)

            self.assertEqual(len(commands), 1)
            self.assertTrue(commands[0].startswith("cce evaluation run "))
            self.assertNotIn("provider bootstrap-eval", guide)

    def test_bootstrap_provider_resolves_registered_store_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir) / "project"
            project_root.mkdir()
            corpus_store_root = Path(tmpdir) / "corpus-store"
            corpus_store_root.mkdir()
            target_root = corpus_store_root / "perplexity-history-memory"
            seed_eval_target(
                target_root,
                corpus_id="perplexity-history-memory",
                name="Perplexity History Memory",
                adapter_type="perplexity-export",
            )
            register_corpus_store(project_root, corpus_store_root)
            upsert_corpus(
                project_root,
                target_root,
                corpus_id="perplexity-history-memory",
                name="Perplexity History Memory",
            )

            payload = bootstrap_provider_evaluation(
                project_root=project_root,
                provider="perplexity",
                corpus_store_root=corpus_store_root,
            )

            self.assertEqual(payload["resolution_source"], "primary")
            self.assertEqual(payload["target_root"], str(target_root.resolve()))
            self.assertTrue((target_root / "eval" / "manual-review-guide.md").exists())

    def test_bootstrap_explicit_target_writes_provider_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir) / "project"
            project_root.mkdir(parents=True, exist_ok=True)
            corpus_store_root = Path(tmpdir) / "corpus-store"
            corpus_store_root.mkdir()
            target_root = corpus_store_root / "gemini-history-memory"
            seed_eval_target(
                target_root,
                corpus_id="gemini-history-memory",
                name="Gemini History Memory",
                adapter_type="gemini-export",
            )
            register_corpus_store(project_root, corpus_store_root)

            payload = bootstrap_provider_evaluation(
                project_root=project_root,
                provider="gemini",
                target_root=target_root,
                corpus_store_root=corpus_store_root,
            )

            self.assertEqual(payload["provider"], "gemini")
            self.assertEqual(payload["target_root"], str(target_root.resolve()))
            self.assertTrue((target_root / "eval" / "gold" / "manual" / "detectors.json").exists())
            self.assertTrue((target_root / "eval" / "manual-review-guide.md").exists())
            self.assertTrue(
                (project_root / "reports" / "gemini-evaluation-bootstrap-latest.md").exists()
            )

    def test_bootstrap_denial_does_not_seed_or_write_project_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir) / "project"
            project_root.mkdir()
            corpus_store_root = Path(tmpdir) / "corpus-store"
            corpus_store_root.mkdir()
            register_corpus_store(project_root, corpus_store_root)
            outside_target = Path(tmpdir) / "outside-corpus"
            seed_eval_target(
                outside_target,
                corpus_id="gemini-history-memory",
                name="Gemini History Memory",
                adapter_type="gemini-export",
            )

            with self.assertRaises(CorpusStoreError):
                bootstrap_provider_evaluation(
                    project_root=project_root,
                    provider="gemini",
                    target_root=outside_target,
                    corpus_store_root=corpus_store_root,
                )

            self.assertFalse((outside_target / "eval" / "gold").exists())
            self.assertFalse((outside_target / "eval" / "manual-review-guide.md").exists())
            self.assertFalse((project_root / "reports").exists())

    def test_bootstrap_claude_uses_policy_primary_root_and_can_run_full_eval(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir) / "project"
            project_root.mkdir(parents=True, exist_ok=True)
            corpus_store_root = Path(tmpdir) / "corpus-store"
            corpus_store_root.mkdir()
            target_root = corpus_store_root / "claude-local-session-memory"
            seed_eval_target(
                target_root,
                corpus_id="claude-local-session-memory",
                name="Claude Local Session Memory",
                adapter_type="claude-local-session",
            )
            policy_path = project_root / "state" / "claude-source-policy.json"
            policy_path.parent.mkdir(parents=True, exist_ok=True)
            policy_path.write_text(
                json.dumps(
                    {
                        "primary_root": str(target_root),
                        "primary_corpus_id": "claude-local-session-memory",
                    },
                ),
                encoding="utf-8",
            )
            register_corpus_store(project_root, corpus_store_root)

            payload = bootstrap_claude_evaluation(
                project_root=project_root,
                policy_path=policy_path,
                full_eval=True,
                corpus_store_root=corpus_store_root,
            )

            self.assertEqual(payload["target_root"], str(target_root.resolve()))
            self.assertTrue((target_root / "eval" / "gold" / "manual" / "detectors.json").exists())
            self.assertTrue((target_root / "eval" / "gold" / "manual" / "families.json").exists())
            self.assertTrue(
                (target_root / "eval" / "fixtures" / "manual" / "retrieval.json").exists()
            )
            self.assertTrue((target_root / "eval" / "gold" / "manual" / "answers.json").exists())
            self.assertTrue((target_root / "eval" / "manual-review-guide.md").exists())
            self.assertTrue(
                (project_root / "reports" / "claude-evaluation-bootstrap-latest.md").exists()
            )
            self.assertTrue(
                any(path.endswith("evaluation-latest.json") for path in payload["outputs"].values())
            )


if __name__ == "__main__":
    unittest.main()
