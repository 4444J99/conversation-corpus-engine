from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conversation_corpus_engine import federated_canon as MODULE
from conversation_corpus_engine.sharded_collection import load_corpus_collection


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _surface(
    root: Path,
    *,
    corpus_id: str,
    name: str,
    family_id: str,
    family_title: str,
    thread_uid: str,
    themes: list[str],
    entity_id: str,
    entity_label: str,
    aliases: list[str],
    action_key: str,
    action_text: str,
    question_key: str,
    question_text: str,
) -> dict[str, Any]:
    (root / "corpus").mkdir(parents=True, exist_ok=True)
    _write_json(
        root / "corpus" / "contract.json",
        {
            "contract_name": MODULE.FEDERATION_CONTRACT,
            "contract_version": MODULE.FEDERATION_CONTRACT_VERSION,
            "adapter_type": f"{corpus_id}-adapter",
            "corpus_id": corpus_id,
            "name": name,
            "generated_at": "2026-03-25T00:00:00+00:00",
            "required_files": list(MODULE.CORE_CONTRACT_FILES),
            "counts": {
                "threads": 1,
                "families": 1,
                "actions": 1,
                "unresolved": 1,
                "entities": 1,
            },
        },
    )
    return {
        "summary": {
            "corpus_id": corpus_id,
            "name": name,
            "root": str(root),
            "adapter_type": f"{corpus_id}-adapter",
            "thread_count": 1,
            "family_count": 1,
            "action_count": 1,
            "unresolved_count": 1,
            "entity_count": 1,
        },
        "families": [
            {
                "canonical_family_id": family_id,
                "canonical_title": family_title,
                "canonical_thread_uid": thread_uid,
                "thread_uids": [thread_uid],
            }
        ],
        "family_dossiers": [
            {
                "family_id": family_id,
                "canonical_title": family_title,
                "canonical_thread_uid": thread_uid,
                "stable_themes": themes,
                "doctrine_summary": f"{family_title} doctrine.",
                "actions": [{"action_key": action_key, "canonical_action": action_text}],
                "unresolved": [{"question_key": question_key, "canonical_question": question_text}],
                "key_entities": [{"canonical_label": entity_label, "entity_type": "concept"}],
            }
        ],
        "doctrine_briefs": [
            {
                "family_id": family_id,
                "canonical_title": family_title,
                "canonical_thread_uid": thread_uid,
                "stable_themes": themes,
                "brief_text": f"{family_title} brief.",
            }
        ],
        "actions": [
            {
                "action_key": action_key,
                "canonical_action": action_text,
                "status": "open",
                "family_ids": [family_id],
                "thread_uids": [thread_uid],
                "occurrence_count": 1,
            }
        ],
        "unresolved": [
            {
                "question_key": question_key,
                "canonical_question": question_text,
                "why_unresolved": "Need a final synthesis.",
                "family_ids": [family_id],
                "thread_uids": [thread_uid],
                "occurrence_count": 1,
            }
        ],
        "entities": [
            {
                "canonical_entity_id": entity_id,
                "canonical_label": entity_label,
                "entity_type": "concept",
                "aliases": aliases,
            }
        ],
    }


def test_build_federated_canon_merges_records_from_accepted_decisions(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    alpha_root = tmp_path / "alpha"
    beta_root = tmp_path / "beta"

    alpha_surface = _surface(
        alpha_root,
        corpus_id="alpha",
        name="Alpha Corpus",
        family_id="family-alpha",
        family_title="Shared Memory Fabric",
        thread_uid="thread-alpha",
        themes=["memory", "fabric", "stability"],
        entity_id="entity-alpha",
        entity_label="Memory Fabric",
        aliases=["Shared Fabric"],
        action_key="action-alpha",
        action_text="Stabilize the shared memory fabric",
        question_key="question-alpha",
        question_text="How should the shared memory fabric evolve?",
    )
    beta_surface = _surface(
        beta_root,
        corpus_id="beta",
        name="Beta Corpus",
        family_id="family-beta",
        family_title="Shared Memory Fabric",
        thread_uid="thread-beta",
        themes=["memory", "fabric", "resilience"],
        entity_id="entity-beta",
        entity_label="Memory Fabric",
        aliases=["Fabric Mesh"],
        action_key="action-beta",
        action_text="Stabilize the shared memory fabric",
        question_key="question-beta",
        question_text="How should the shared memory fabric evolve?",
    )

    decisions = {"generated_at": None, **MODULE.DEFAULT_FEDERATED_DECISIONS}
    decisions["accepted_family_merges"] = [
        {
            "review_id": "family-merge-001",
            "subject_ids": ["alpha:family-alpha", "beta:family-beta"],
            "canonical_subject": "shared-memory-fabric",
            "recorded_at": "2026-03-25T00:00:00+00:00",
        }
    ]
    decisions["accepted_entity_aliases"] = [
        {
            "review_id": "entity-merge-001",
            "subject_ids": ["alpha:entity-alpha", "beta:entity-beta"],
            "canonical_subject": "shared-memory-entity",
            "recorded_at": "2026-03-25T00:00:00+00:00",
        }
    ]
    decisions["accepted_action_merges"] = [
        {
            "review_id": "action-merge-001",
            "subject_ids": ["alpha:action-alpha", "beta:action-beta"],
            "canonical_subject": "shared-memory-action",
            "recorded_at": "2026-03-25T00:00:00+00:00",
        }
    ]
    decisions["accepted_unresolved_merges"] = [
        {
            "review_id": "question-merge-001",
            "subject_ids": ["alpha:question-alpha", "beta:question-beta"],
            "canonical_subject": "shared-memory-question",
            "recorded_at": "2026-03-25T00:00:00+00:00",
        }
    ]
    MODULE.save_federated_decisions(project_root, decisions)

    result = MODULE.build_federated_canon(project_root, [alpha_surface, beta_surface])

    federation_root = project_root / "federation"
    canonical_families = load_corpus_collection(federation_root / "canonical-families.json")
    canonical_entities = load_corpus_collection(federation_root / "canonical-entities.json")
    canonical_actions = load_corpus_collection(federation_root / "canonical-actions.json")
    canonical_unresolved = load_corpus_collection(federation_root / "canonical-unresolved.json")
    lineage_map = load_corpus_collection(federation_root / "lineage-map.json")
    conflict_report = json.loads(Path(result["conflict_report_path"]).read_text())
    review_queue = json.loads(Path(result["review_queue_path"]).read_text())

    assert len(canonical_families) == 1
    family = canonical_families[0]
    assert family["federated_family_id"] == "shared-memory-fabric"
    assert family["member_count"] == 2
    assert family["corpus_ids"] == ["alpha", "beta"]
    assert family["action_count"] == 2
    assert family["unresolved_count"] == 2
    assert sorted(ref["thread_uid"] for ref in family["canonical_thread_refs"]) == [
        "thread-alpha",
        "thread-beta",
    ]
    assert "memory" in family["stable_themes"]
    assert "Memory Fabric" in family["key_entities"]

    assert len(canonical_entities) == 1
    entity = canonical_entities[0]
    assert entity["federated_entity_id"] == "shared-memory-entity"
    assert entity["canonical_label"] == "Memory Fabric"
    assert entity["entity_type"] == "concept"
    assert entity["aliases"] == ["Fabric Mesh", "Shared Fabric"]
    assert entity["corpus_ids"] == ["alpha", "beta"]
    assert entity["member_count"] == 2
    assert [member["member_id"] for member in entity["member_entities"]] == [
        "alpha:entity-alpha",
        "beta:entity-beta",
    ]
    assert "memory" in entity["vector_terms"]
    assert "Shared Fabric" in entity["search_text"]
    assert canonical_actions[0]["federated_action_id"] == "shared-memory-action"
    assert canonical_actions[0]["member_count"] == 2
    assert canonical_unresolved[0]["federated_question_id"] == "shared-memory-question"
    assert canonical_unresolved[0]["member_count"] == 2

    assert lineage_map == [
        {
            "federated_family_id": "shared-memory-fabric",
            "canonical_title": "Shared Memory Fabric",
            "corpus_ids": ["alpha", "beta"],
            "lineage": [
                {
                    "corpus_id": "alpha",
                    "family_id": "family-alpha",
                    "canonical_thread_uid": "thread-alpha",
                    "canonical_title": "Shared Memory Fabric",
                },
                {
                    "corpus_id": "beta",
                    "family_id": "family-beta",
                    "canonical_thread_uid": "thread-beta",
                    "canonical_title": "Shared Memory Fabric",
                },
            ],
        }
    ]
    assert conflict_report["multi_corpus_family_count"] == 1
    assert conflict_report["potential_conflict_count"] == 0
    assert review_queue["open_count"] == 0
    assert review_queue["items"] == []

    alpha_contract = json.loads((alpha_root / "corpus" / "contract.json").read_text())
    beta_contract = json.loads((beta_root / "corpus" / "contract.json").read_text())
    assert alpha_contract["corpus_id"] == "alpha"
    assert beta_contract["corpus_id"] == "beta"
    assert alpha_contract["counts"] == {
        "threads": 1,
        "families": 1,
        "actions": 1,
        "unresolved": 1,
        "entities": 1,
    }
    assert alpha_contract["required_files"] == list(MODULE.CORE_CONTRACT_FILES)


def test_build_federated_canon_preserves_stale_resolved_reviews_only(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    solo_root = tmp_path / "solo"
    solo_surface = _surface(
        solo_root,
        corpus_id="solo",
        name="Solo Corpus",
        family_id="family-solo",
        family_title="Solo Fabric",
        thread_uid="thread-solo",
        themes=["solo", "fabric"],
        entity_id="entity-solo",
        entity_label="Solo Fabric",
        aliases=["Solo Mesh"],
        action_key="action-solo",
        action_text="Stabilize the solo fabric",
        question_key="question-solo",
        question_text="How should the solo fabric evolve?",
    )

    MODULE.save_federated_review_queue(
        project_root,
        {
            "generated_at": None,
            "items": [
                {
                    "review_id": "resolved-review",
                    "review_type": "family-merge",
                    "status": "accepted",
                    "decision_note": "already reviewed",
                    "canonical_subject": "shared-fabric",
                    "resolved_at": "2026-03-24T22:00:00+00:00",
                    "subject_ids": ["alpha:family-alpha", "beta:family-beta"],
                    "source_corpora": ["alpha", "beta"],
                    "updated_at": "2026-03-24T22:00:00+00:00",
                },
                {
                    "review_id": "stale-open-review",
                    "review_type": "action-merge",
                    "status": "open",
                    "subject_ids": ["alpha:action-alpha", "beta:action-beta"],
                    "source_corpora": ["alpha", "beta"],
                    "updated_at": "2026-03-24T22:00:00+00:00",
                },
            ],
        },
    )

    result = MODULE.build_federated_canon(project_root, [solo_surface])
    review_queue = json.loads(Path(result["review_queue_path"]).read_text())

    assert review_queue["open_count"] == 0
    assert [item["review_id"] for item in review_queue["items"]] == ["resolved-review"]
    assert review_queue["items"][0]["status"] == "accepted"


def test_ensure_corpus_contract_manifest_preserves_existing_identity(tmp_path: Path) -> None:
    corpus_root = tmp_path / "existing-corpus"
    _write_json(
        corpus_root / "corpus" / "contract.json",
        {
            "contract_name": "conversation-corpus-engine-v1",
            "contract_version": 1,
            "adapter_type": "existing-adapter",
            "corpus_id": "existing-corpus-id",
            "name": "Existing Corpus Name",
        },
    )

    payload = MODULE.ensure_corpus_contract_manifest(
        corpus_root,
        corpus_id="new-corpus-id",
        name="Replacement Name",
        adapter_type="replacement-adapter",
        summary={
            "thread_count": 2,
            "family_count": 3,
            "action_count": 5,
            "unresolved_count": 7,
            "entity_count": 11,
        },
    )

    assert payload["adapter_type"] == "existing-adapter"
    assert payload["corpus_id"] == "existing-corpus-id"
    assert payload["name"] == "Existing Corpus Name"
    assert payload["required_files"] == list(MODULE.LEGACY_CORE_CONTRACT_FILES)
    assert payload["counts"] == {
        "threads": 2,
        "families": 3,
        "actions": 5,
        "unresolved": 7,
        "entities": 11,
    }


def test_resolve_federated_review_item_moves_pair_between_decision_buckets(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    item = {
        "review_id": "family-review-001",
        "review_type": "family-merge",
        "status": "open",
        "title": "Alpha <> Beta",
        "subject_ids": ["alpha:family-alpha", "beta:family-beta"],
        "source_corpora": ["alpha", "beta"],
        "suggested_canonical_subject": "shared-doctrine",
        "updated_at": "2026-03-25T00:00:00+00:00",
    }
    MODULE.save_federated_review_queue(project_root, {"generated_at": None, "items": [item]})

    decisions = {"generated_at": None, **MODULE.DEFAULT_FEDERATED_DECISIONS}
    decisions["accepted_family_merges"] = [
        {
            "review_id": "older-review",
            "subject_ids": ["alpha:family-alpha", "beta:family-beta"],
            "canonical_subject": "shared-doctrine",
            "recorded_at": "2026-03-24T00:00:00+00:00",
        }
    ]
    MODULE.save_federated_decisions(project_root, decisions)

    resolved = MODULE.resolve_federated_review_item(
        project_root,
        "family-review-001",
        "rejected",
        "titles match but the doctrine split is real",
    )

    assert resolved["status"] == "rejected"
    assert resolved["canonical_subject"] == "shared-doctrine"

    stored_queue = MODULE.load_federated_review_queue(project_root)
    stored_history = MODULE.load_federated_review_history(project_root)
    stored_decisions = MODULE.load_federated_decisions(project_root)

    assert stored_queue["open_count"] == 0
    assert stored_queue["items"][0]["status"] == "rejected"
    assert stored_history["count"] == 1
    assert stored_history["items"][0]["decision"] == "rejected"
    assert stored_history["items"][0]["canonical_subject"] == "shared-doctrine"
    assert stored_decisions["accepted_family_merges"] == []
    assert stored_decisions["rejected_family_merges"][0]["review_id"] == "family-review-001"


def test_build_pair_suggestions_produces_unique_review_ids() -> None:
    """After the fingerprint migration, build_review_id always produces unique IDs
    even for subject_ids whose slugs would truncate identically. The legacy behavior
    (collision → stabilize) is no longer needed for new IDs."""
    left = {
        "member_id": "claude-history-memory:entity-collision",
        "corpus_id": "claude-history-memory",
        "canonical_label": "Collision",
        "aliases": [],
    }
    right_storyline = {
        "member_id": "claude-local-session-memory:entity-storyline",
        "corpus_id": "claude-local-session-memory",
        "canonical_label": "Storyline",
        "aliases": [],
    }
    right_storylines = {
        "member_id": "claude-local-session-memory:entity-storylines",
        "corpus_id": "claude-local-session-memory",
        "canonical_label": "Storylines",
        "aliases": [],
    }
    id_storyline = MODULE.build_review_id(
        "entity-alias",
        [left["member_id"], right_storyline["member_id"]],
    )
    id_storylines = MODULE.build_review_id(
        "entity-alias",
        [left["member_id"], right_storylines["member_id"]],
    )
    # Fingerprint suffix makes them unique even when slug portion truncates identically
    assert id_storyline != id_storylines

    # Legacy format would have collided
    legacy_a = MODULE.build_review_id_legacy(
        "entity-alias",
        [left["member_id"], right_storyline["member_id"]],
    )
    legacy_b = MODULE.build_review_id_legacy(
        "entity-alias",
        [left["member_id"], right_storylines["member_id"]],
    )
    assert legacy_a == legacy_b  # confirms the old collision existed

    suggestions = MODULE.build_pair_suggestions(
        [left, right_storyline, right_storylines],
        review_type="entity-alias",
        similarity_fn=lambda _left, _right: 0.83,
        threshold=0.8,
        decisions={"generated_at": None, **MODULE.DEFAULT_FEDERATED_DECISIONS},
    )

    assert len(suggestions) == 2
    review_ids = [item["review_id"] for item in suggestions]
    assert len(set(review_ids)) == 2


# ---------------------------------------------------------------------------
# Review-ID migration tests
# ---------------------------------------------------------------------------


class TestMigrateReviewIds:
    def _seed_state(self, project_root: Path, items: list[dict[str, Any]]) -> None:
        state_dir = project_root / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        queue = {"generated_at": None, "open_count": len(items), "items": list(items)}
        history = {"generated_at": None, "items": list(items)}
        decisions: dict[str, Any] = {
            "generated_at": None,
            **MODULE.DEFAULT_FEDERATED_DECISIONS,
        }
        for item in items:
            rt = item.get("review_type", "entity-alias")
            accepted_key = MODULE.FEDERATED_REVIEW_TYPES[rt][0]
            decisions[accepted_key].append(dict(item))
        _write_json(state_dir / "federated-review-queue.json", queue)
        _write_json(state_dir / "federated-review-history.json", history)
        _write_json(state_dir / "federated-canonical-decisions.json", decisions)

    def test_migration_adds_fingerprints(self, tmp_path: Path) -> None:
        items = [
            {
                "review_id": "federated-entity-alias-old-slug",
                "review_type": "entity-alias",
                "subject_ids": ["corpus-a:entity-1", "corpus-b:entity-2"],
                "status": "pending",
            },
        ]
        self._seed_state(tmp_path, items)
        result = MODULE.migrate_review_ids(tmp_path)
        assert result["stats"]["queue_migrated"] >= 1
        assert result["stats"]["history_migrated"] >= 1
        assert result["id_count"] >= 1
        mapping_path = tmp_path / "state" / "review-id-mapping.json"
        assert mapping_path.exists()

    def test_migration_resolves_collisions(self, tmp_path: Path) -> None:
        items = [
            {
                "review_id": "federated-entity-alias-same-slug",
                "review_type": "entity-alias",
                "subject_ids": ["corpus-a:entity-alpha", "corpus-b:entity-beta"],
                "status": "pending",
            },
            {
                "review_id": "federated-entity-alias-same-slug",
                "review_type": "entity-alias",
                "subject_ids": ["corpus-a:entity-gamma", "corpus-b:entity-delta"],
                "status": "pending",
            },
        ]
        self._seed_state(tmp_path, items)
        MODULE.migrate_review_ids(tmp_path)
        queue = json.loads((tmp_path / "state" / "federated-review-queue.json").read_text())
        new_ids = [i["review_id"] for i in queue["items"]]
        assert len(set(new_ids)) == 2, f"Expected unique IDs after migration, got {new_ids}"

    def test_dry_run_does_not_modify_files(self, tmp_path: Path) -> None:
        items = [
            {
                "review_id": "federated-entity-alias-dry",
                "review_type": "entity-alias",
                "subject_ids": ["corpus-a:entity-1", "corpus-b:entity-2"],
                "status": "pending",
            },
        ]
        self._seed_state(tmp_path, items)
        queue_before = (tmp_path / "state" / "federated-review-queue.json").read_text()
        result = MODULE.migrate_review_ids(tmp_path, dry_run=True)
        queue_after = (tmp_path / "state" / "federated-review-queue.json").read_text()
        assert queue_before == queue_after
        assert not (tmp_path / "state" / "review-id-mapping.json").exists()
        assert result["id_count"] >= 1

    def test_already_migrated_items_unchanged(self, tmp_path: Path) -> None:
        subject_ids = ["corpus-a:entity-1", "corpus-b:entity-2"]
        new_id = MODULE.build_review_id("entity-alias", subject_ids)
        items = [
            {
                "review_id": new_id,
                "review_type": "entity-alias",
                "subject_ids": subject_ids,
                "status": "pending",
            },
        ]
        self._seed_state(tmp_path, items)
        result = MODULE.migrate_review_ids(tmp_path)
        assert result["stats"]["queue_migrated"] == 0
        assert result["stats"]["unchanged"] >= 1
