from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conversation_corpus_engine.corpus_candidates import (
    corpus_candidate_history_path,
    load_corpus_candidate_manifest,
    promote_corpus_candidate,
    review_corpus_candidate,
    rollback_corpus_promotion,
    stage_corpus_candidate,
)
from conversation_corpus_engine.federation import list_registered_corpora, upsert_corpus
from conversation_corpus_engine.source_policy import load_source_policy, set_source_policy


def token_map(*tokens: str) -> dict[str, float]:
    return {
        token: round(1.0 - (index * 0.08), 4)  # allow-secret
        for index, token in enumerate(tokens)
    }


def seed_corpus(
    root: Path,
    *,
    corpus_title: str,
    family_title: str,
    family_id: str,
    thread_uid: str,
    action_key: str,
    action_text: str,
    question_key: str,
    question_text: str,
    entity_label: str,
) -> None:
    corpus_dir = root / "corpus"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    tokens = [token.lower() for token in family_title.split()]
    vectors = token_map(*tokens)
    (corpus_dir / "threads-index.json").write_text(
        json.dumps(
            [
                {
                    "thread_uid": thread_uid,
                    "title_normalized": family_title,
                    "semantic_v3_summary": f"{family_title} governs {corpus_title}.",
                    "semantic_v3_themes": tokens,
                    "semantic_v3_entities": [entity_label],
                    "family_ids": [family_id],
                },
            ],
        ),
        encoding="utf-8",
    )
    (corpus_dir / "semantic-v3-index.json").write_text(
        json.dumps(
            {
                "threads": [
                    {
                        "thread_uid": thread_uid,
                        "title": family_title,
                        "search_text": f"{family_title} {action_text}",
                        "vector_terms": vectors,
                        "family_ids": [family_id],
                    },
                ],
            },
        ),
        encoding="utf-8",
    )
    (corpus_dir / "pairs-index.json").write_text(
        json.dumps(
            [
                {
                    "pair_id": f"{thread_uid}-pair-001",
                    "thread_uid": thread_uid,
                    "family_ids": [family_id],
                    "search_text": action_text,
                    "summary": action_text,
                    "vector_terms": vectors,
                },
            ],
        ),
        encoding="utf-8",
    )
    (corpus_dir / "doctrine-briefs.json").write_text(
        json.dumps(
            [
                {
                    "family_id": family_id,
                    "canonical_title": family_title,
                    "canonical_thread_uid": thread_uid,
                    "stable_themes": tokens,
                    "brief_text": f"{family_title} doctrine.",
                    "search_text": f"{family_title} doctrine",
                    "vector_terms": vectors,
                },
            ],
        ),
        encoding="utf-8",
    )
    (corpus_dir / "family-dossiers.json").write_text(
        json.dumps(
            [
                {
                    "family_id": family_id,
                    "canonical_title": family_title,
                    "canonical_thread_uid": thread_uid,
                    "member_count": 1,
                    "stable_themes": tokens,
                    "doctrine_summary": f"{family_title} doctrine.",
                    "search_text": f"{family_title} {action_text}",
                    "actions": [{"action_key": action_key, "canonical_action": action_text}],
                    "unresolved": [
                        {"question_key": question_key, "canonical_question": question_text}
                    ],
                    "key_entities": [{"canonical_label": entity_label, "entity_type": "concept"}],
                    "vector_terms": vectors,
                },
            ],
        ),
        encoding="utf-8",
    )
    (corpus_dir / "canonical-families.json").write_text(
        json.dumps(
            [
                {
                    "canonical_family_id": family_id,
                    "canonical_title": family_title,
                    "canonical_thread_uid": thread_uid,
                    "thread_uids": [thread_uid],
                },
            ],
        ),
        encoding="utf-8",
    )
    (corpus_dir / "action-ledger.json").write_text(
        json.dumps(
            [
                {
                    "action_key": action_key,
                    "canonical_action": action_text,
                    "family_ids": [family_id],
                    "thread_uids": [thread_uid],
                },
            ],
        ),
        encoding="utf-8",
    )
    (corpus_dir / "unresolved-ledger.json").write_text(
        json.dumps(
            [
                {
                    "question_key": question_key,
                    "canonical_question": question_text,
                    "why_unresolved": "Pending decision.",
                    "family_ids": [family_id],
                    "thread_uids": [thread_uid],
                },
            ],
        ),
        encoding="utf-8",
    )
    (corpus_dir / "canonical-entities.json").write_text(
        json.dumps(
            [
                {
                    "canonical_entity_id": f"entity-{thread_uid}",
                    "canonical_label": entity_label,
                    "aliases": [family_title],
                },
            ],
        ),
        encoding="utf-8",
    )
    (corpus_dir / "evaluation-summary.json").write_text(
        json.dumps({"regression_gates": {"overall_state": "pass"}}),
        encoding="utf-8",
    )
    (corpus_dir / "regression-gates.json").write_text(
        json.dumps({"overall_state": "pass", "source_reliability_state": "pass"}),
        encoding="utf-8",
    )
    (corpus_dir / "contract.json").write_text(
        json.dumps(
            {
                "contract_name": "conversation-corpus-engine-v1",
                "contract_version": 1,
                "adapter_type": "manual-export",
                "corpus_id": "test-corpus",
                "name": corpus_title,
                "generated_at": "2026-03-25T00:00:00+00:00",
                "required_files": [
                    "corpus/threads-index.json",
                    "corpus/semantic-v3-index.json",
                    "corpus/pairs-index.json",
                    "corpus/doctrine-briefs.json",
                    "corpus/family-dossiers.json",
                ],
                "counts": {
                    "threads": 1,
                    "families": 1,
                    "actions": 1,
                    "unresolved": 1,
                    "entities": 1,
                },
            },
        ),
        encoding="utf-8",
    )


class CorpusCandidatesTests(unittest.TestCase):
    def test_stage_review_promote_and_rollback_corpus_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir)
            project_root = workspace_root / "project"
            live_root = workspace_root / "claude-live"
            candidate_root = workspace_root / "claude-candidate"
            seed_corpus(
                live_root,
                corpus_title="Claude Live Memory",
                family_title="Alpha Launch",
                family_id="family-launch",
                thread_uid="thread-alpha",
                action_key="action-alpha",
                action_text="Implement alpha rollout",
                question_key="question-alpha",
                question_text="How should alpha launch evolve?",
                entity_label="Alpha Engine",
            )
            seed_corpus(
                candidate_root,
                corpus_title="Claude Candidate Memory",
                family_title="Beta Launch",
                family_id="family-launch",
                thread_uid="thread-beta",
                action_key="action-beta",
                action_text="Implement beta rollout",
                question_key="question-beta",
                question_text="How should beta launch evolve?",
                entity_label="Beta Engine",
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
            approved = review_corpus_candidate(
                project_root,
                staged["candidate_id"],
                decision="approve",
                note="Promotion approved.",
            )
            promoted = promote_corpus_candidate(
                project_root,
                staged["candidate_id"],
                note="Promote the refreshed corpus.",
            )
            registry_after_promote = list_registered_corpora(project_root)
            source_policy_after_promote = load_source_policy(project_root, "claude")
            rolled_back = rollback_corpus_promotion(
                project_root,
                note="Restore the original live corpus.",
            )
            registry_after_rollback = list_registered_corpora(project_root)
            source_policy_after_rollback = load_source_policy(project_root, "claude")
            history = json.loads(
                corpus_candidate_history_path(project_root).read_text(encoding="utf-8")
            )
            manifest = load_corpus_candidate_manifest(project_root, staged["candidate_id"])

            self.assertEqual(staged["status"], "staged")
            self.assertEqual(approved["status"], "approved")
            self.assertEqual(promoted["live_corpus_id"], "claude-local-session-memory")
            self.assertEqual(registry_after_promote[0]["root"], str(candidate_root.resolve()))
            self.assertEqual(
                source_policy_after_promote["primary_root"], str(candidate_root.resolve())
            )
            self.assertEqual(rolled_back["source_candidate_id"], staged["candidate_id"])
            self.assertEqual(registry_after_rollback[0]["root"], str(live_root.resolve()))
            self.assertEqual(source_policy_after_rollback["primary_root"], str(live_root.resolve()))
            self.assertEqual(history["count"], 1)
            self.assertEqual(manifest["status"], "rolled-back")

    def test_rejected_candidate_cannot_be_promoted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir)
            project_root = workspace_root / "project"
            live_root = workspace_root / "live"
            candidate_root = workspace_root / "candidate"
            seed_corpus(
                live_root,
                corpus_title="Gemini Live Memory",
                family_title="Gamma Launch",
                family_id="family-gamma",
                thread_uid="thread-gamma",
                action_key="action-gamma",
                action_text="Implement gamma rollout",
                question_key="question-gamma",
                question_text="How should gamma launch evolve?",
                entity_label="Gamma Engine",
            )
            seed_corpus(
                candidate_root,
                corpus_title="Gemini Candidate Memory",
                family_title="Delta Launch",
                family_id="family-gamma",
                thread_uid="thread-delta",
                action_key="action-delta",
                action_text="Implement delta rollout",
                question_key="question-delta",
                question_text="How should delta launch evolve?",
                entity_label="Delta Engine",
            )
            upsert_corpus(
                project_root,
                live_root,
                corpus_id="gemini-history-memory",
                name="Gemini History Memory",
                make_default=True,
            )

            staged = stage_corpus_candidate(
                project_root,
                candidate_root=candidate_root,
                live_corpus_id="gemini-history-memory",
            )
            review_corpus_candidate(
                project_root,
                staged["candidate_id"],
                decision="reject",
                note="Keep the live corpus.",
            )

            with self.assertRaisesRegex(ValueError, "must be approved"):
                promote_corpus_candidate(project_root, staged["candidate_id"])


if __name__ == "__main__":
    unittest.main()
