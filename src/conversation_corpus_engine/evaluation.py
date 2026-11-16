#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .answering import (
    build_answer,
    build_documents,
    load_json,
    search_documents_v4,
    write_json,
    write_markdown,
)
from .corpus_store import (
    CorpusStoreError,
    CorpusWriteAuthorization,
    authorize_additional_corpus_path,
    authorize_corpus_write,
)
from .paths import default_project_root
from .sharded_collection import load_corpus_collection

DEFAULT_ROOT = Path.cwd()
DETECTOR_KEYS = (
    "accepted_duplicates",
    "rejected_duplicates",
    "accepted_drift_pairs",
    "rejected_drift_pairs",
    "accepted_contradictions",
    "rejected_contradictions",
    "accepted_entity_aliases",
    "rejected_entity_aliases",
)
GATE_THRESHOLDS = {
    "family_stability.exact_member_match_rate": {"direction": "min", "pass": 1.0, "warn": 0.9},
    "retrieval_metrics.family_hit_at_1": {"direction": "min", "pass": 0.9, "warn": 0.75},
    "retrieval_metrics.thread_hit_at_1": {"direction": "min", "pass": 0.5, "warn": 0.3},
    "retrieval_metrics.pair_hit_at_3": {"direction": "min", "pass": 0.5, "warn": 0.25},
    "answer_metrics.state_match_rate": {"direction": "min", "pass": 0.9, "warn": 0.75},
    "answer_metrics.required_citation_coverage_avg": {
        "direction": "min",
        "pass": 0.9,
        "warn": 0.75,
    },
    "answer_metrics.forbidden_citation_violation_rate": {
        "direction": "max",
        "pass": 0.0,
        "warn": 0.1,
    },
    "answer_metrics.abstention_match_rate": {"direction": "min", "pass": 0.9, "warn": 0.75},
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate a conversation corpus against local gold fixtures."
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--project-root", type=Path, default=default_project_root())
    parser.add_argument("--corpus-store-root", type=Path)
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Seed seeded fixtures and create manual fixture templates.",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--markdown-output", type=Path)
    parser.add_argument("--json-output", type=Path)
    return parser.parse_args()


def normalize_pair(values: list[str], *, labels: bool = False) -> tuple[str, str] | None:
    filtered = [value for value in values if value]
    if len(filtered) < 2:
        return None
    if labels:
        filtered = [value.lower() for value in filtered]
    unique = sorted(set(filtered))
    if len(unique) < 2:
        return None
    return unique[0], unique[1]


def precision_recall(current: set[Any], gold: set[Any]) -> dict[str, Any]:
    true_positive = len(current & gold)
    false_positive = len(current - gold)
    false_negative = len(gold - current)
    precision = (
        round(true_positive / (true_positive + false_positive), 4)
        if (true_positive + false_positive)
        else None
    )
    recall = (
        round(true_positive / (true_positive + false_negative), 4)
        if (true_positive + false_negative)
        else None
    )
    return {
        "current_count": len(current),
        "gold_count": len(gold),
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "precision": precision,
        "recall": recall,
    }


def fixture_count(kind: str, payload: dict[str, Any]) -> int:
    if kind == "detectors":
        return sum(len(payload.get(key, [])) for key in DETECTOR_KEYS)
    if kind == "families":
        return len(payload.get("families", []))
    return len(payload.get("fixtures", []))


def manual_fixture_review_complete(kind: str, payload: dict[str, Any], source: str) -> bool:
    if source != "manual":
        return False
    if fixture_count(kind, payload) > 0:
        return True
    return bool(payload.get("manual_review_complete") or payload.get("reviewed_none"))


def ensure_manual_template(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        return
    write_json(path, payload)


def build_seeded_retrieval_fixtures(
    doctrine_briefs: list[dict[str, Any]],
    family_dossiers: list[dict[str, Any]],
    pairs_index: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    pairs_by_thread: dict[str, list[dict[str, Any]]] = {}
    for item in pairs_index:
        pairs_by_thread.setdefault(item.get("thread_uid") or "", []).append(item)

    dossier_by_family = {item["family_id"]: item for item in family_dossiers}
    fixtures: list[dict[str, Any]] = []
    for brief in doctrine_briefs:
        fixtures.append(
            {
                "query": brief["canonical_title"],
                "expected_family_id": brief["family_id"],
                "expected_thread_uid": brief.get("canonical_thread_uid"),
                "scenario": "canonical-title",
            },
        )
        themes = brief.get("stable_themes", [])
        if themes:
            fixtures.append(
                {
                    "query": " ".join(themes[:2]),
                    "expected_family_id": brief["family_id"],
                    "expected_thread_uid": brief.get("canonical_thread_uid"),
                    "scenario": "stable-themes",
                },
            )
        canonical_thread_uid = brief.get("canonical_thread_uid")
        pair_candidates = pairs_by_thread.get(canonical_thread_uid or "", [])
        if pair_candidates:
            pair = pair_candidates[0]
            query_terms = [
                token
                for token in (pair.get("search_text") or pair.get("summary") or "").split()
                if len(token) > 4
            ][:5]
            if query_terms:
                fixtures.append(
                    {
                        "query": " ".join(query_terms),
                        "expected_family_id": brief["family_id"],
                        "expected_thread_uid": brief.get("canonical_thread_uid"),
                        "expected_pair_id": pair["pair_id"],
                        "scenario": "pair-evidence",
                    },
                )
        dossier = dossier_by_family.get(brief["family_id"], {})
        key_entities = [item["canonical_label"] for item in dossier.get("key_entities", [])[:2]]
        if key_entities:
            fixtures.append(
                {
                    "query": " ".join(key_entities),
                    "expected_family_id": brief["family_id"],
                    "expected_thread_uid": brief.get("canonical_thread_uid"),
                    "scenario": "entity-focus",
                },
            )
    fixtures.append(
        {
            "query": "nonexistent corpus signal for abstention",
            "scenario": "abstention",
        },
    )
    return fixtures


def build_seeded_answer_fixtures(
    doctrine_briefs: list[dict[str, Any]],
    family_dossiers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    dossier_by_family = {item["family_id"]: item for item in family_dossiers}
    fixtures: list[dict[str, Any]] = []
    for brief in doctrine_briefs:
        dossier = dossier_by_family.get(brief["family_id"], {})
        required = [f"family:{brief['family_id']}"]
        if brief.get("canonical_thread_uid"):
            required.append(f"thread:{brief['canonical_thread_uid']}")
        minimum_evidence = 2
        if dossier.get("actions"):
            required.append(f"action:{dossier['actions'][0]['action_key']}")
            minimum_evidence = 3
        fixtures.append(
            {
                "query": brief["canonical_title"],
                "expected_state": "grounded",
                "required_citations": required,
                "forbidden_citations": [],
                "min_evidence_count": minimum_evidence,
            },
        )
    fixtures.append(
        {
            "query": "nonexistent corpus signal for abstention",
            "expected_state": "abstain",
            "required_citations": [],
            "forbidden_citations": [],
            "min_evidence_count": 0,
        },
    )
    return fixtures


def seed_gold(root: Path) -> dict[str, Path]:
    eval_dir = root / "eval"
    gold_seeded_dir = eval_dir / "gold" / "seeded"
    gold_manual_dir = eval_dir / "gold" / "manual"
    fixtures_seeded_dir = eval_dir / "fixtures" / "seeded"
    fixtures_manual_dir = eval_dir / "fixtures" / "manual"
    gold_seeded_dir.mkdir(parents=True, exist_ok=True)
    gold_manual_dir.mkdir(parents=True, exist_ok=True)
    fixtures_seeded_dir.mkdir(parents=True, exist_ok=True)
    fixtures_manual_dir.mkdir(parents=True, exist_ok=True)

    canonical_decisions = load_json(root / "state" / "canonical-decisions.json", default={}) or {}
    corpus_dir = root / "corpus"
    canonical_families = (
        load_corpus_collection(corpus_dir / "canonical-families.json", default=[]) or []
    )
    doctrine_briefs = load_corpus_collection(corpus_dir / "doctrine-briefs.json", default=[]) or []
    family_dossiers = load_corpus_collection(corpus_dir / "family-dossiers.json", default=[]) or []
    pairs_index = load_corpus_collection(corpus_dir / "pairs-index.json", default=[]) or []

    detectors_payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **{key: canonical_decisions.get(key, []) for key in DETECTOR_KEYS},
    }
    families_payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "families": [
            {
                "family_id": item["canonical_family_id"],
                "canonical_title": item["canonical_title"],
                "canonical_thread_uid": item["canonical_thread_uid"],
                "thread_uids": item.get("thread_uids", []),
            }
            for item in canonical_families
        ],
    }
    retrieval_payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fixtures": build_seeded_retrieval_fixtures(doctrine_briefs, family_dossiers, pairs_index),
    }
    answers_payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fixtures": build_seeded_answer_fixtures(doctrine_briefs, family_dossiers),
    }

    seeded_paths = {
        "detectors": gold_seeded_dir / "detectors.json",
        "families": gold_seeded_dir / "families.json",
        "retrieval": fixtures_seeded_dir / "retrieval.json",
        "answers": gold_seeded_dir / "answers.json",
    }
    write_json(seeded_paths["detectors"], detectors_payload)
    write_json(seeded_paths["families"], families_payload)
    write_json(seeded_paths["retrieval"], retrieval_payload)
    write_json(seeded_paths["answers"], answers_payload)

    ensure_manual_template(
        gold_manual_dir / "detectors.json",
        {
            "instructions": "Populate manually reviewed detector decisions here. Empty arrays fall back to seeded fixtures.",
            "manual_review_complete": False,
            **{key: [] for key in DETECTOR_KEYS},
        },
    )
    ensure_manual_template(
        gold_manual_dir / "families.json",
        {
            "instructions": "Populate manually reviewed canonical family membership here. Empty fixtures fall back to seeded fixtures.",
            "manual_review_complete": False,
            "families": [],
        },
    )
    ensure_manual_template(
        fixtures_manual_dir / "retrieval.json",
        {
            "instructions": "Populate manual and adversarial retrieval fixtures here. Empty fixtures fall back to seeded fixtures.",
            "manual_review_complete": False,
            "fixtures": [],
        },
    )
    ensure_manual_template(
        gold_manual_dir / "answers.json",
        {
            "instructions": "Populate manual answer fixtures here. Empty fixtures fall back to seeded fixtures.",
            "manual_review_complete": False,
            "fixtures": [],
        },
    )

    return seeded_paths


def load_preferred_fixture(root: Path, kind: str) -> tuple[dict[str, Any], str, str | None]:
    if kind == "detectors":
        candidates = [
            ("manual", root / "eval" / "gold" / "manual" / "detectors.json"),
            ("seeded", root / "eval" / "gold" / "seeded" / "detectors.json"),
            ("legacy", root / "eval" / "gold" / "detectors.json"),
        ]
    elif kind == "families":
        candidates = [
            ("manual", root / "eval" / "gold" / "manual" / "families.json"),
            ("seeded", root / "eval" / "gold" / "seeded" / "families.json"),
            ("legacy", root / "eval" / "gold" / "families.json"),
        ]
    elif kind == "answers":
        candidates = [
            ("manual", root / "eval" / "gold" / "manual" / "answers.json"),
            ("seeded", root / "eval" / "gold" / "seeded" / "answers.json"),
            ("legacy", root / "eval" / "gold" / "answers.json"),
        ]
    else:
        candidates = [
            ("manual", root / "eval" / "fixtures" / "manual" / "retrieval.json"),
            ("seeded", root / "eval" / "fixtures" / "seeded" / "retrieval.json"),
            ("legacy", root / "eval" / "fixtures" / "retrieval.json"),
        ]

    fallback: tuple[dict[str, Any], str, str | None] = ({}, "missing", None)
    for source, path in candidates:
        payload = load_json(path, default={}) or {}
        if path.exists() and fallback[2] is None:
            fallback = (payload, source, str(path))
        if (
            manual_fixture_review_complete(kind, payload, source)
            or fixture_count(kind, payload) > 0
        ):
            return payload, source, str(path)
    return fallback


def average_metric(values: list[float | None]) -> float | None:
    usable = [value for value in values if value is not None]
    if not usable:
        return None
    return round(sum(usable) / len(usable), 4)


def evaluate_answer_fixtures(
    root: Path, answer_payload: dict[str, Any], *, corpus: dict[str, Any] | None = None
) -> dict[str, Any]:
    fixtures = answer_payload.get("fixtures", [])
    results: list[dict[str, Any]] = []
    state_matches = 0
    evidence_matches = 0
    forbidden_violations = 0
    required_coverages: list[float | None] = []
    abstention_expected = 0
    abstention_matches = 0

    for fixture in fixtures:
        query = fixture.get("query") or ""
        mode = fixture.get("mode")
        retrieval = search_documents_v4(
            root, query, limit=max(8, fixture.get("limit", 8)), mode=mode, corpus=corpus
        )
        answer = build_answer(query, retrieval, mode=mode)
        required = set(fixture.get("required_citations", []))
        forbidden = set(fixture.get("forbidden_citations", []))
        present = set(answer.get("citations", []))
        coverage = round(len(required & present) / len(required), 4) if required else None
        required_coverages.append(coverage)
        state_match = (
            fixture.get("expected_state") == answer.get("answer_state")
            if fixture.get("expected_state")
            else None
        )
        evidence_ok = len(answer.get("evidence", [])) >= fixture.get("min_evidence_count", 0)
        forbidden_violation = bool(forbidden & present)

        if state_match:
            state_matches += 1
        if evidence_ok:
            evidence_matches += 1
        if forbidden_violation:
            forbidden_violations += 1
        if fixture.get("expected_state") == "abstain":
            abstention_expected += 1
            if answer.get("answer_state") == "abstain":
                abstention_matches += 1

        results.append(
            {
                "query": query,
                "expected_state": fixture.get("expected_state"),
                "actual_state": answer.get("answer_state"),
                "required_citations": sorted(required),
                "present_citations": sorted(present),
                "required_coverage": coverage,
                "forbidden_violated": forbidden_violation,
                "min_evidence_count": fixture.get("min_evidence_count", 0),
                "evidence_count": len(answer.get("evidence", [])),
                "state_match": state_match,
                "top_hit": (answer.get("top_hits") or [{}])[0],
            },
        )

    fixture_count_total = len(fixtures)
    return {
        "fixture_count": fixture_count_total,
        "state_match_rate": round(state_matches / fixture_count_total, 4)
        if fixture_count_total
        else None,
        "required_citation_coverage_avg": average_metric(required_coverages),
        "forbidden_citation_violation_rate": round(forbidden_violations / fixture_count_total, 4)
        if fixture_count_total
        else None,
        "evidence_minimum_pass_rate": round(evidence_matches / fixture_count_total, 4)
        if fixture_count_total
        else None,
        "abstention_fixture_count": abstention_expected,
        "abstention_match_rate": round(abstention_matches / abstention_expected, 4)
        if abstention_expected
        else None,
        "fixtures": results,
    }


def evaluate_current_corpus(root: Path) -> dict[str, Any]:
    detectors_gold, detectors_source, detectors_path = load_preferred_fixture(root, "detectors")
    families_gold, families_source, families_path = load_preferred_fixture(root, "families")
    retrieval_payload, retrieval_source, retrieval_path = load_preferred_fixture(root, "retrieval")
    answer_payload, answer_source, answer_path = load_preferred_fixture(root, "answers")

    canonical_decisions = load_json(root / "state" / "canonical-decisions.json", default={}) or {}
    canonical_families = (
        load_corpus_collection(root / "corpus" / "canonical-families.json", default=[]) or []
    )

    # Pre-build the corpus document index once — avoids rebuilding from disk per fixture.
    corpus = build_documents(root)

    def pair_set(key: str) -> set[tuple[str, str]]:
        if "entity_alias" in key:
            return {
                normalize_pair(item.get("labels", []), labels=True)
                for item in canonical_decisions.get(key, [])
                if normalize_pair(item.get("labels", []), labels=True)
            }
        return {
            normalize_pair(item.get("thread_uids", []))
            for item in canonical_decisions.get(key, [])
            if normalize_pair(item.get("thread_uids", []))
        }

    detector_metrics = {}
    detector_precisions: list[float | None] = []
    detector_recalls: list[float | None] = []
    for key in DETECTOR_KEYS:
        gold_set = {
            normalize_pair(item.get("labels", []), labels=True)
            if "entity_alias" in key
            else normalize_pair(item.get("thread_uids", []))
            for item in detectors_gold.get(key, [])
        }
        gold_set = {item for item in gold_set if item}
        detector_metrics[key] = precision_recall(pair_set(key), gold_set)
        detector_precisions.append(detector_metrics[key]["precision"])
        detector_recalls.append(detector_metrics[key]["recall"])

    current_family_map = {
        item["canonical_family_id"]: set(item.get("thread_uids", [])) for item in canonical_families
    }
    family_scores: list[dict[str, Any]] = []
    exact_match_count = 0
    for item in families_gold.get("families", []):
        current_members = current_family_map.get(item["family_id"], set())
        gold_members = set(item.get("thread_uids", []))
        if current_members == gold_members:
            exact_match_count += 1
        union = current_members | gold_members
        overlap = current_members & gold_members
        family_scores.append(
            {
                "family_id": item["family_id"],
                "member_jaccard": round(len(overlap) / len(union), 4) if union else None,
                "canonical_thread_matches": item.get("canonical_thread_uid")
                == next(
                    (
                        family["canonical_thread_uid"]
                        for family in canonical_families
                        if family["canonical_family_id"] == item["family_id"]
                    ),
                    None,
                ),
            },
        )
    family_stability = {
        "gold_family_count": len(families_gold.get("families", [])),
        "current_family_count": len(canonical_families),
        "exact_member_match_rate": round(
            exact_match_count / len(families_gold.get("families", [])), 4
        )
        if families_gold.get("families")
        else None,
        "families": family_scores,
    }

    retrieval_results: list[dict[str, Any]] = []
    family_hit_at_1 = 0
    family_hit_at_3 = 0
    thread_hit_at_1 = 0
    thread_hit_at_3 = 0
    pair_hit_at_1 = 0
    pair_hit_at_3 = 0
    family_expected = 0
    thread_expected = 0
    pair_expected = 0
    for fixture in retrieval_payload.get("fixtures", []):
        retrieval = search_documents_v4(
            root,
            fixture.get("query") or "",
            limit=max(8, fixture.get("limit", 8)),
            mode=fixture.get("mode"),
            corpus=corpus,
        )
        family_ids = [
            item.get("family_id")
            for item in retrieval.get("family_hits", [])
            if item.get("family_id")
        ]
        thread_ids = [
            item.get("thread_uid")
            for item in retrieval.get("thread_hits", [])
            if item.get("thread_uid")
        ]
        pair_ids = [
            item.get("pair_id") for item in retrieval.get("pair_hits", []) if item.get("pair_id")
        ]
        if fixture.get("expected_family_id"):
            family_expected += 1
            if family_ids[:1] == [fixture["expected_family_id"]]:
                family_hit_at_1 += 1
            if fixture["expected_family_id"] in family_ids[:3]:
                family_hit_at_3 += 1
        if fixture.get("expected_thread_uid"):
            thread_expected += 1
            if thread_ids[:1] == [fixture["expected_thread_uid"]]:
                thread_hit_at_1 += 1
            if fixture["expected_thread_uid"] in thread_ids[:3]:
                thread_hit_at_3 += 1
        if fixture.get("expected_pair_id"):
            pair_expected += 1
            if pair_ids[:1] == [fixture["expected_pair_id"]]:
                pair_hit_at_1 += 1
            if fixture["expected_pair_id"] in pair_ids[:3]:
                pair_hit_at_3 += 1

        retrieval_results.append(
            {
                "query": fixture.get("query"),
                "scenario": fixture.get("scenario"),
                "expected_family_id": fixture.get("expected_family_id"),
                "expected_thread_uid": fixture.get("expected_thread_uid"),
                "expected_pair_id": fixture.get("expected_pair_id"),
                "family_hits": family_ids[:3],
                "thread_hits": thread_ids[:3],
                "pair_hits": pair_ids[:3],
                "family_focus": retrieval.get("family_focus", []),
            },
        )

    retrieval_metrics = {
        "fixture_count": len(retrieval_payload.get("fixtures", [])),
        "family_fixture_count": family_expected,
        "thread_fixture_count": thread_expected,
        "pair_fixture_count": pair_expected,
        "family_hit_at_1": round(family_hit_at_1 / family_expected, 4) if family_expected else None,
        "family_hit_at_3": round(family_hit_at_3 / family_expected, 4) if family_expected else None,
        "thread_hit_at_1": round(thread_hit_at_1 / thread_expected, 4) if thread_expected else None,
        "thread_hit_at_3": round(thread_hit_at_3 / thread_expected, 4) if thread_expected else None,
        "pair_hit_at_1": round(pair_hit_at_1 / pair_expected, 4) if pair_expected else None,
        "pair_hit_at_3": round(pair_hit_at_3 / pair_expected, 4) if pair_expected else None,
        "fixtures": retrieval_results,
    }

    answer_metrics = evaluate_answer_fixtures(root, answer_payload, corpus=corpus)

    scorecard = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fixture_sources": {
            "detectors": {
                "source": detectors_source,
                "path": detectors_path,
                "count": fixture_count("detectors", detectors_gold),
            },
            "families": {
                "source": families_source,
                "path": families_path,
                "count": fixture_count("families", families_gold),
            },
            "retrieval": {
                "source": retrieval_source,
                "path": retrieval_path,
                "count": fixture_count("retrieval", retrieval_payload),
            },
            "answers": {
                "source": answer_source,
                "path": answer_path,
                "count": fixture_count("answers", answer_payload),
            },
        },
        "detector_metrics": detector_metrics,
        "detector_summary": {
            "average_precision": average_metric(detector_precisions),
            "average_recall": average_metric(detector_recalls),
        },
        "family_stability": family_stability,
        "retrieval_metrics": retrieval_metrics,
        "answer_metrics": answer_metrics,
    }
    scorecard["regression_gates"] = build_regression_gates(scorecard)
    return scorecard


def get_metric(scorecard: dict[str, Any], path: str) -> float | None:
    current: Any = scorecard
    for key in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current if isinstance(current, (int, float)) else None


def evaluate_gate(metric: float | None, config: dict[str, Any]) -> str:
    if metric is None:
        return "skip"
    if config["direction"] == "min":
        if metric >= config["pass"]:
            return "pass"
        if metric >= config["warn"]:
            return "warn"
        return "fail"
    if metric <= config["pass"]:
        return "pass"
    if metric <= config["warn"]:
        return "warn"
    return "fail"


def build_regression_gates(scorecard: dict[str, Any]) -> dict[str, Any]:
    gates: list[dict[str, Any]] = []
    overall_state = "pass"
    for path, config in GATE_THRESHOLDS.items():
        value = get_metric(scorecard, path)
        state = evaluate_gate(value, config)
        gates.append({"metric": path, "value": value, "state": state, **config})
        if state == "fail":
            overall_state = "fail"
        elif state == "warn" and overall_state == "pass":
            overall_state = "warn"

    source_notes: list[str] = []
    fixture_sources = scorecard.get("fixture_sources", {})
    for kind in ("retrieval", "answers", "detectors", "families"):
        source = (fixture_sources.get(kind) or {}).get("source")
        if source != "manual":
            source_notes.append(
                f"{kind} fixtures are sourced from {source or 'missing'}, not manual gold."
            )
    reliability_state = "warn" if source_notes else "pass"
    if reliability_state == "warn" and overall_state == "pass":
        overall_state = "warn"

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overall_state": overall_state,
        "source_reliability_state": reliability_state,
        "source_notes": source_notes,
        "gates": gates,
    }


def render_markdown(scorecard: dict[str, Any]) -> str:
    lines = [
        "# Evaluation Scorecard",
        "",
        f"- Generated: {scorecard['generated_at']}",
        "",
        "## Fixture Sources",
        "",
    ]
    for key, item in scorecard.get("fixture_sources", {}).items():
        lines.append(
            f"- {key}: source={item.get('source')}, count={item.get('count')}, path={item.get('path') or 'n/a'}",
        )
    lines.extend(["", "## Detector Metrics", ""])
    for key, metrics in scorecard["detector_metrics"].items():
        lines.append(
            f"- {key}: precision={metrics.get('precision')}, recall={metrics.get('recall')}, "
            f"tp={metrics['true_positive']}, fp={metrics['false_positive']}, fn={metrics['false_negative']}",
        )
    lines.extend(
        [
            "",
            "## Family Stability",
            "",
            f"- Gold families: {scorecard['family_stability']['gold_family_count']}",
            f"- Current families: {scorecard['family_stability']['current_family_count']}",
            f"- Exact member match rate: {scorecard['family_stability']['exact_member_match_rate']}",
            "",
            "## Retrieval Metrics",
            "",
            f"- Fixture count: {scorecard['retrieval_metrics']['fixture_count']}",
            f"- Family hit@1: {scorecard['retrieval_metrics']['family_hit_at_1']}",
            f"- Family hit@3: {scorecard['retrieval_metrics']['family_hit_at_3']}",
            f"- Thread hit@1: {scorecard['retrieval_metrics']['thread_hit_at_1']}",
            f"- Thread hit@3: {scorecard['retrieval_metrics']['thread_hit_at_3']}",
            f"- Pair hit@1: {scorecard['retrieval_metrics']['pair_hit_at_1']}",
            f"- Pair hit@3: {scorecard['retrieval_metrics']['pair_hit_at_3']}",
            "",
            "## Answer Metrics",
            "",
            f"- Fixture count: {scorecard['answer_metrics']['fixture_count']}",
            f"- State match rate: {scorecard['answer_metrics']['state_match_rate']}",
            f"- Required citation coverage avg: {scorecard['answer_metrics']['required_citation_coverage_avg']}",
            f"- Forbidden citation violation rate: {scorecard['answer_metrics']['forbidden_citation_violation_rate']}",
            f"- Evidence minimum pass rate: {scorecard['answer_metrics']['evidence_minimum_pass_rate']}",
            f"- Abstention match rate: {scorecard['answer_metrics']['abstention_match_rate']}",
            "",
            "## Regression Gates",
            "",
            f"- Overall state: {scorecard['regression_gates']['overall_state']}",
            f"- Source reliability: {scorecard['regression_gates']['source_reliability_state']}",
        ],
    )
    for item in scorecard["regression_gates"].get("source_notes", []):
        lines.append(f"- Source note: {item}")
    return "\n".join(lines)


def render_gate_markdown(gates: dict[str, Any]) -> str:
    lines = [
        "# Regression Gates",
        "",
        f"- Generated: {gates.get('generated_at')}",
        f"- Overall State: {gates.get('overall_state')}",
        f"- Source Reliability: {gates.get('source_reliability_state')}",
        "",
        "## Source Notes",
        "",
    ]
    if gates.get("source_notes"):
        lines.extend(f"- {item}" for item in gates["source_notes"])
    else:
        lines.append("All evaluated fixture classes are sourced from manual gold.")
    lines.extend(["", "## Metric Gates", ""])
    for item in gates.get("gates", []):
        lines.append(
            f"- {item['metric']}: state={item['state']}, value={item.get('value')}, "
            f"pass={item.get('pass')}, warn={item.get('warn')}, direction={item.get('direction')}",
        )
    return "\n".join(lines)


def default_output_paths(root: Path) -> dict[str, Path]:
    reports_dir = root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d")
    return {
        "scorecard_md": reports_dir / f"evaluation-scorecard-{stamp}.md",
        "scorecard_json": reports_dir / f"evaluation-scorecard-{stamp}.json",
        "gate_md": reports_dir / f"evaluation-gates-{stamp}.md",
        "gate_json": reports_dir / f"evaluation-gates-{stamp}.json",
        "latest_scorecard_md": reports_dir / "evaluation-latest.md",
        "latest_scorecard_json": reports_dir / "evaluation-latest.json",
        "latest_gate_md": reports_dir / "evaluation-gates-latest.md",
        "latest_gate_json": reports_dir / "evaluation-gates-latest.json",
    }


def write_evaluation_outputs(
    root: Path,
    scorecard: dict[str, Any],
    *,
    outputs: dict[str, Path] | None = None,
) -> dict[str, Path]:
    resolved_outputs = outputs or default_output_paths(root)
    write_markdown(resolved_outputs["scorecard_md"], render_markdown(scorecard))
    write_json(resolved_outputs["scorecard_json"], scorecard)
    write_markdown(resolved_outputs["gate_md"], render_gate_markdown(scorecard["regression_gates"]))
    write_json(resolved_outputs["gate_json"], scorecard["regression_gates"])
    write_markdown(resolved_outputs["latest_scorecard_md"], render_markdown(scorecard))
    write_json(resolved_outputs["latest_scorecard_json"], scorecard)
    write_markdown(
        resolved_outputs["latest_gate_md"], render_gate_markdown(scorecard["regression_gates"])
    )
    write_json(resolved_outputs["latest_gate_json"], scorecard["regression_gates"])
    write_json(root / "corpus" / "evaluation-summary.json", scorecard)
    write_json(root / "corpus" / "regression-gates.json", scorecard["regression_gates"])
    return resolved_outputs


def run_corpus_evaluation(
    root: Path,
    *,
    seed: bool = False,
    markdown_output: Path | None = None,
    json_output: Path | None = None,
    project_root: Path | None = None,
    corpus_store_root: Path | None = None,
    authorization: CorpusWriteAuthorization | None = None,
) -> tuple[dict[str, Any], dict[str, Path]]:
    requested_root = root
    if authorization is None:
        if project_root is None:
            raise CorpusStoreError(
                "Evaluation writes require project_root and registered corpus-store custody."
            )
        resolved_authorization = authorize_corpus_write(
            project_root=project_root,
            corpus_store_root=corpus_store_root,
            destination=requested_root,
        )
    else:
        resolved_authorization = authorize_corpus_write(
            project_root=authorization.project_root,
            corpus_store_root=authorization.store_root,
            destination=requested_root,
        )
    root = resolved_authorization.destination
    if markdown_output is not None:
        authorize_additional_corpus_path(resolved_authorization, markdown_output)
    if json_output is not None:
        authorize_additional_corpus_path(resolved_authorization, json_output)
    if seed:
        seed_gold(root)
    scorecard = evaluate_current_corpus(root)
    outputs = default_output_paths(root)
    if markdown_output is not None:
        outputs["scorecard_md"] = markdown_output
    if json_output is not None:
        outputs["scorecard_json"] = json_output
    resolved_outputs = write_evaluation_outputs(root, scorecard, outputs=outputs)
    return scorecard, resolved_outputs


def main() -> int:
    args = parse_args()
    scorecard, outputs = run_corpus_evaluation(
        args.root,
        seed=args.seed,
        markdown_output=args.markdown_output,
        json_output=args.json_output,
        project_root=args.project_root,
        corpus_store_root=args.corpus_store_root,
    )

    if args.json:
        print(json.dumps(scorecard, indent=2))
    else:
        print(outputs["scorecard_md"])
        print(outputs["scorecard_json"])
        print(outputs["gate_md"])
        print(outputs["gate_json"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
