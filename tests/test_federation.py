from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conversation_corpus_engine import federated_canon as FED_CANON
from conversation_corpus_engine import federation as MODULE
from conversation_corpus_engine.sharded_collection import load_corpus_collection


def token_map(*tokens: str) -> dict[str, float]:
    return {
        token: round(1.0 - (index * 0.05), 4)  # allow-secret
        for index, token in enumerate(tokens)
    }


class MemoryFederationTests(unittest.TestCase):
    def seed_corpus(
        self,
        root: Path,
        *,
        corpus_title: str,
        family_id: str,
        thread_uid: str,
        query_terms: list[str],
        action_key: str,
        question_key: str,
        gate_state: str = "pass",
    ) -> None:
        (root / "corpus").mkdir(parents=True, exist_ok=True)
        family_title = corpus_title
        query_text = " ".join(query_terms)
        action_text = f"Implement {query_terms[0]} {query_terms[1]} flow"
        question_text = f"How should {query_terms[0]} evolve?"
        vector_terms = token_map(*query_terms)
        (root / "corpus" / "threads-index.json").write_text(
            json.dumps(
                [
                    {
                        "thread_uid": thread_uid,
                        "title_normalized": family_title,
                        "semantic_summary": f"{family_title} summary for {query_text}.",
                        "semantic_v2_summary": f"{family_title} v2 summary for {query_text}.",
                        "semantic_v3_summary": f"{family_title} v3 summary for {query_text}.",
                        "semantic_v3_themes": query_terms[:3],
                        "semantic_v3_entities": [f"{family_title} Entity"],
                        "family_ids": [family_id],
                        "update_time_iso": "2026-03-13T16:00:00+00:00",
                    },
                ],
            ),
            encoding="utf-8",
        )
        (root / "corpus" / "semantic-v3-index.json").write_text(
            json.dumps(
                {
                    "threads": [
                        {
                            "thread_uid": thread_uid,
                            "title": family_title,
                            "summary": f"{family_title} summary",
                            "search_text": f"{family_title} {query_text} canonical thread",
                            "family_ids": [family_id],
                            "vector_terms": vector_terms,
                        },
                    ],
                },
            ),
            encoding="utf-8",
        )
        (root / "corpus" / "pairs-index.json").write_text(
            json.dumps(
                [
                    {
                        "pair_id": f"{thread_uid}-pair-001",
                        "thread_uid": thread_uid,
                        "title": family_title,
                        "summary": f"Pair about {query_text}",
                        "search_text": f"{thread_uid}-pair-001 {family_title} pair centers {query_text}",
                        "vector_terms": vector_terms,
                        "family_ids": [family_id],
                    },
                ],
            ),
            encoding="utf-8",
        )
        (root / "corpus" / "doctrine-briefs.json").write_text(
            json.dumps(
                [
                    {
                        "family_id": family_id,
                        "canonical_title": family_title,
                        "canonical_thread_uid": thread_uid,
                        "member_count": 1,
                        "stable_themes": query_terms[:4],
                        "brief_text": f"{family_title} currently centers on {query_text}.",
                        "search_text": f"{family_title} {query_text}",
                        "vector_terms": vector_terms,
                    },
                ],
            ),
            encoding="utf-8",
        )
        (root / "corpus" / "family-dossiers.json").write_text(
            json.dumps(
                [
                    {
                        "family_id": family_id,
                        "canonical_title": family_title,
                        "canonical_thread_uid": thread_uid,
                        "member_count": 1,
                        "stable_themes": query_terms[:4],
                        "doctrine_summary": f"{family_title} doctrine for {query_text}.",
                        "search_text": f"{family_title} doctrine {query_text}",
                        "actions": [{"action_key": action_key, "canonical_action": action_text}],
                        "unresolved": [
                            {"question_key": question_key, "canonical_question": question_text}
                        ],
                        "key_entities": [
                            {"canonical_label": f"{family_title} Entity", "entity_type": "concept"}
                        ],
                        "vector_terms": vector_terms,
                    },
                ],
            ),
            encoding="utf-8",
        )
        (root / "corpus" / "canonical-families.json").write_text(
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
        (root / "corpus" / "action-ledger.json").write_text(
            json.dumps(
                [
                    {
                        "action_key": action_key,
                        "canonical_action": action_text,
                        "status": "open",
                        "family_ids": [family_id],
                        "thread_uids": [thread_uid],
                        "occurrence_count": 1,
                    },
                ],
            ),
            encoding="utf-8",
        )
        (root / "corpus" / "unresolved-ledger.json").write_text(
            json.dumps(
                [
                    {
                        "question_key": question_key,
                        "canonical_question": question_text,
                        "why_unresolved": "No final synthesis yet.",
                        "family_ids": [family_id],
                        "thread_uids": [thread_uid],
                        "occurrence_count": 1,
                    },
                ],
            ),
            encoding="utf-8",
        )
        (root / "corpus" / "canonical-entities.json").write_text(
            json.dumps(
                [
                    {
                        "canonical_entity_id": f"entity-{thread_uid}",
                        "canonical_label": f"{family_title} Entity",
                        "entity_type": "concept",
                        "aliases": [family_title],
                    },
                ],
            ),
            encoding="utf-8",
        )
        (root / "corpus" / "entity-aliases.json").write_text(
            json.dumps([{"canonical_label": f"{family_title} Entity", "labels": [family_title]}]),
            encoding="utf-8",
        )
        (root / "corpus" / "doctrine-timeline.json").write_text(json.dumps([]), encoding="utf-8")
        (root / "corpus" / "evaluation-summary.json").write_text(
            json.dumps({"fixture_sources": {}, "regression_gates": {"overall_state": gate_state}}),
            encoding="utf-8",
        )
        (root / "corpus" / "regression-gates.json").write_text(
            json.dumps({"overall_state": gate_state, "source_reliability_state": "pass"}),
            encoding="utf-8",
        )
        (root / "corpus" / "contract.json").write_text(
            json.dumps(
                {
                    "contract_name": "conversation-corpus-engine-v1",
                    "contract_version": 1,
                    "adapter_type": "test-adapter",
                    "corpus_id": root.name if root.name != "project" else "primary-corpus",
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
                }
            ),
            encoding="utf-8",
        )

    def test_bootstrap_registry_uses_current_project_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.seed_corpus(
                root,
                corpus_title="Alpha Corpus",
                family_id="family-alpha",
                thread_uid="thread-alpha",
                query_terms=["alpha", "registry", "governance"],
                action_key="action-alpha",
                question_key="question-alpha",
            )

            registry = MODULE.load_registry(root)

            self.assertEqual(len(registry["corpora"]), 1)
            self.assertEqual(registry["corpora"][0]["root"], str(root))
            self.assertTrue(registry["corpora"][0]["default"])

    def test_build_federation_preserves_corpus_contract_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir) / "project"
            self.seed_corpus(
                project_root,
                corpus_title="Alpha Corpus",
                family_id="family-alpha",
                thread_uid="thread-alpha",
                query_terms=["alpha", "registry", "governance"],
                action_key="action-alpha",
                question_key="question-alpha",
            )
            contract_path = project_root / "corpus" / "contract.json"
            before_bytes = contract_path.read_bytes()

            MODULE.build_federation(project_root)

            after_bytes = contract_path.read_bytes()
            self.assertEqual(before_bytes, after_bytes)

    def test_build_federation_materializes_registered_corpora(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir) / "project"
            second_root = Path(tmpdir) / "notes-corpus"
            self.seed_corpus(
                project_root,
                corpus_title="Alpha Corpus",
                family_id="family-alpha",
                thread_uid="thread-alpha",
                query_terms=["alpha", "registry", "governance"],
                action_key="action-alpha",
                question_key="question-alpha",
            )
            self.seed_corpus(
                second_root,
                corpus_title="Beta Corpus",
                family_id="family-beta",
                thread_uid="thread-beta",
                query_terms=["beta", "orchard", "blueprint"],
                action_key="action-beta",
                question_key="question-beta",
            )

            MODULE.upsert_corpus(
                project_root, second_root, corpus_id="notes-memory", name="Notes Memory"
            )
            result = MODULE.build_federation(project_root)
            corpora_summary = load_corpus_collection(
                project_root / "federation" / "corpora-summary.json"
            )
            families_index = load_corpus_collection(
                project_root / "federation" / "families-index.json"
            )
            canonical_families = load_corpus_collection(
                project_root / "federation" / "canonical-families.json"
            )

            self.assertTrue(Path(result["summary_markdown_path"]).exists())
            self.assertTrue(Path(result["canonical_families_path"]).exists())
            self.assertEqual(len(corpora_summary), 2)
            self.assertTrue(any(item["corpus_id"] == "notes-memory" for item in corpora_summary))
            self.assertTrue(
                any(
                    item["federated_family_id"] == "notes-memory:family-beta"
                    for item in families_index
                )
            )
            self.assertGreaterEqual(len(canonical_families), 2)
            self.assertTrue(
                all(item["source_freshness_state"] == "not_applicable" for item in corpora_summary)
            )

    def test_build_federated_answer_selects_best_matching_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir) / "project"
            second_root = Path(tmpdir) / "notes-corpus"
            self.seed_corpus(
                project_root,
                corpus_title="Alpha Corpus",
                family_id="family-alpha",
                thread_uid="thread-alpha",
                query_terms=["alpha", "registry", "governance"],
                action_key="action-alpha",
                question_key="question-alpha",
            )
            self.seed_corpus(
                second_root,
                corpus_title="Beta Corpus",
                family_id="family-beta",
                thread_uid="thread-beta",
                query_terms=["beta", "orchard", "blueprint"],
                action_key="action-beta",
                question_key="question-beta",
            )
            MODULE.upsert_corpus(
                project_root, second_root, corpus_id="notes-memory", name="Notes Memory"
            )

            answer = MODULE.build_federated_answer(project_root, "beta orchard blueprint")

            self.assertEqual(answer["selected_corpus"]["corpus_id"], "notes-memory")
            self.assertTrue(any(item.startswith("notes-memory/") for item in answer["citations"]))
            self.assertTrue(any(item.startswith("federation/") for item in answer["citations"]))
            self.assertEqual(answer["answer_state"], "grounded")

    def test_search_federation_prefers_exact_family_title_alignment(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir) / "project"
            second_root = Path(tmpdir) / "notes-corpus"
            self.seed_corpus(
                project_root,
                corpus_title="Virtual System Architecture",
                family_id="family-alpha",
                thread_uid="thread-alpha",
                query_terms=["virtual", "system", "architecture"],
                action_key="action-alpha",
                question_key="question-alpha",
            )
            self.seed_corpus(
                second_root,
                corpus_title="Document ingestion overview",
                family_id="family-beta",
                thread_uid="thread-beta",
                query_terms=["virtual", "system", "architecture", "overview"],
                action_key="action-beta",
                question_key="question-beta",
            )
            MODULE.upsert_corpus(
                project_root, second_root, corpus_id="notes-memory", name="Notes Memory"
            )
            MODULE.build_federation(project_root)

            payload = MODULE.search_federation(project_root, "Virtual System Architecture")

            self.assertEqual(payload["selected"]["corpus_id"], "primary-corpus")

    def test_build_federation_generates_review_candidates_for_cross_corpus_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir) / "project"
            second_root = Path(tmpdir) / "notes-corpus"
            self.seed_corpus(
                project_root,
                corpus_title="Shared Doctrine",
                family_id="family-shared-a",
                thread_uid="thread-shared-a",
                query_terms=["shared", "doctrine", "memory"],
                action_key="action-shared-a",
                question_key="question-shared-a",
            )
            self.seed_corpus(
                second_root,
                corpus_title="Shared Doctrine",
                family_id="family-shared-b",
                thread_uid="thread-shared-b",
                query_terms=["shared", "doctrine", "memory"],
                action_key="action-shared-b",
                question_key="question-shared-b",
            )

            MODULE.upsert_corpus(
                project_root, second_root, corpus_id="notes-memory", name="Notes Memory"
            )
            MODULE.build_federation(project_root)
            review_queue = json.loads(
                (project_root / "federation" / "review-queue.json").read_text()
            )

            self.assertGreaterEqual(review_queue["open_count"], 1)
            self.assertTrue(
                any(item["review_type"] == "family-merge" for item in review_queue["items"])
            )

    def test_federated_review_resolution_persists_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir) / "project"
            second_root = Path(tmpdir) / "notes-corpus"
            self.seed_corpus(
                project_root,
                corpus_title="Shared Doctrine",
                family_id="family-shared-a",
                thread_uid="thread-shared-a",
                query_terms=["shared", "doctrine", "memory"],
                action_key="action-shared-a",
                question_key="question-shared-a",
            )
            self.seed_corpus(
                second_root,
                corpus_title="Shared Doctrine",
                family_id="family-shared-b",
                thread_uid="thread-shared-b",
                query_terms=["shared", "doctrine", "memory"],
                action_key="action-shared-b",
                question_key="question-shared-b",
            )

            MODULE.upsert_corpus(
                project_root, second_root, corpus_id="notes-memory", name="Notes Memory"
            )
            MODULE.build_federation(project_root)
            review_queue = json.loads(
                (project_root / "federation" / "review-queue.json").read_text()
            )
            family_merge = next(
                item for item in review_queue["items"] if item["review_type"] == "family-merge"
            )

            FED_CANON.resolve_federated_review_item(
                project_root,
                family_merge["review_id"],
                "accepted",
                "merge shared doctrine clusters",
                canonical_subject="shared-doctrine",
            )
            decisions = FED_CANON.load_federated_decisions(project_root)

            self.assertTrue(decisions["accepted_family_merges"])
            self.assertEqual(
                decisions["accepted_family_merges"][0]["canonical_subject"], "shared-doctrine"
            )

    def test_build_federation_suppresses_low_signal_single_token_entity_alias_reviews(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir) / "project"
            second_root = Path(tmpdir) / "notes-corpus"
            self.seed_corpus(
                project_root,
                corpus_title="Alpha Corpus",
                family_id="family-alpha",
                thread_uid="thread-alpha",
                query_terms=["alpha", "registry", "governance"],
                action_key="action-alpha",
                question_key="question-alpha",
            )
            self.seed_corpus(
                second_root,
                corpus_title="Beta Corpus",
                family_id="family-beta",
                thread_uid="thread-beta",
                query_terms=["beta", "orchard", "blueprint"],
                action_key="action-beta",
                question_key="question-beta",
            )
            (project_root / "corpus" / "canonical-entities.json").write_text(
                json.dumps(
                    [
                        {
                            "canonical_entity_id": "entity-system-a",
                            "canonical_label": "System",
                            "entity_type": "concept",
                            "aliases": ["Alpha Corpus"],
                        },
                    ]
                ),
                encoding="utf-8",
            )
            (second_root / "corpus" / "canonical-entities.json").write_text(
                json.dumps(
                    [
                        {
                            "canonical_entity_id": "entity-system-b",
                            "canonical_label": "System",
                            "entity_type": "concept",
                            "aliases": ["Beta Corpus"],
                        },
                    ]
                ),
                encoding="utf-8",
            )

            MODULE.upsert_corpus(
                project_root, second_root, corpus_id="notes-memory", name="Notes Memory"
            )
            MODULE.build_federation(project_root)
            review_queue = json.loads(
                (project_root / "federation" / "review-queue.json").read_text()
            )

            self.assertFalse(
                any(
                    item["review_type"] == "entity-alias" and item["title"] == "System <> System"
                    for item in review_queue["items"]
                )
            )

    def test_query_federation_index_filters_by_corpus_and_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir) / "project"
            second_root = Path(tmpdir) / "notes-corpus"
            self.seed_corpus(
                project_root,
                corpus_title="Alpha Corpus",
                family_id="family-alpha",
                thread_uid="thread-alpha",
                query_terms=["alpha", "registry", "governance"],
                action_key="action-alpha",
                question_key="question-alpha",
            )
            self.seed_corpus(
                second_root,
                corpus_title="Beta Corpus",
                family_id="family-beta",
                thread_uid="thread-beta",
                query_terms=["beta", "orchard", "blueprint"],
                action_key="action-beta",
                question_key="question-beta",
            )
            MODULE.upsert_corpus(
                project_root, second_root, corpus_id="notes-memory", name="Notes Memory"
            )
            MODULE.build_federation(project_root)

            payload = MODULE.query_federation_index(
                project_root,
                ledger="actions",
                text="orchard",
                corpus_id="notes-memory",
                limit=10,
            )

            self.assertEqual(len(payload), 1)
            self.assertEqual(payload[0]["corpus_id"], "notes-memory")
            self.assertIn("orchard", payload[0]["canonical_action"])


if __name__ == "__main__":
    unittest.main()
