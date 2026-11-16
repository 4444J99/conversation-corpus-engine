from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .answering import build_answer, search_documents_v4, shorten, unique_preserve
from .federation import FEDERATION_CONTRACT, load_corpus_surface, validate_corpus_root


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def candidate_entry_for_root(
    candidate_root: Path,
    *,
    target_corpus_id: str,
    target_name: str,
) -> dict[str, Any]:
    validation = validate_corpus_root(candidate_root)
    return {
        "corpus_id": f"{target_corpus_id}-candidate",
        "name": f"{target_name} Candidate",
        "root": str(candidate_root.resolve()),
        "contract": validation.get("contract_name") or FEDERATION_CONTRACT,
        "status": "candidate",
        "default": False,
    }


def collection_map(
    items: list[dict[str, Any]], *, key_field: str, label_field: str
) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in items:
        key = str(item.get(key_field) or item.get(label_field) or "").strip()
        if not key:
            continue
        values[key] = str(item.get(label_field) or key).strip()
    return values


def build_collection_delta(
    live_map: dict[str, str],
    candidate_map: dict[str, str],
    *,
    include_label_changes: bool = False,
) -> dict[str, Any]:
    live_keys = set(live_map)
    candidate_keys = set(candidate_map)
    shared_keys = sorted(live_keys & candidate_keys)
    added_keys = sorted(candidate_keys - live_keys)
    removed_keys = sorted(live_keys - candidate_keys)
    label_changes: list[dict[str, str]] = []
    if include_label_changes:
        for key in shared_keys:
            if live_map.get(key) != candidate_map.get(key):
                label_changes.append(
                    {
                        "key": key,
                        "live": live_map.get(key) or key,
                        "candidate": candidate_map.get(key) or key,
                    },
                )
    return {
        "live_count": len(live_map),
        "candidate_count": len(candidate_map),
        "shared_count": len(shared_keys),
        "added_count": len(added_keys),
        "removed_count": len(removed_keys),
        "added": [{"key": key, "label": candidate_map[key]} for key in added_keys[:10]],
        "removed": [{"key": key, "label": live_map[key]} for key in removed_keys[:10]],
        "label_change_count": len(label_changes),
        "label_changes": label_changes[:10],
    }


def candidate_queries(surface: dict[str, Any]) -> list[str]:
    queries: list[str] = []
    summary = surface.get("summary") or {}
    if summary.get("name"):
        queries.append(str(summary["name"]))
    for item in surface.get("family_dossiers") or []:
        value = item.get("canonical_title") or item.get("family_id")
        if value:
            queries.append(shorten(str(value), 120))
    for item in surface.get("actions") or []:
        value = item.get("canonical_action") or item.get("action_key")
        if value:
            queries.append(shorten(str(value), 120))
    for item in surface.get("unresolved") or []:
        value = item.get("canonical_question") or item.get("question_key")
        if value:
            queries.append(shorten(str(value), 120))
    for item in surface.get("entities") or []:
        value = item.get("canonical_label") or item.get("canonical_entity_id")
        if value:
            queries.append(shorten(str(value), 120))
    for item in surface.get("threads") or []:
        value = item.get("title_normalized") or item.get("title_raw") or item.get("thread_uid")
        if value:
            queries.append(shorten(str(value), 120))
    return [item for item in unique_preserve(queries) if item]


def representative_queries(
    live_surface: dict[str, Any], candidate_surface: dict[str, Any], *, limit: int = 8
) -> list[str]:
    queries = candidate_queries(candidate_surface) + candidate_queries(live_surface)
    return unique_preserve(queries)[: max(limit, 1)]


def summarize_query_result(root: Path, query: str) -> dict[str, Any]:
    retrieval = search_documents_v4(root, query, limit=6)
    answer = build_answer(query, retrieval)
    top_hit = (retrieval.get("hits") or [None])[0] or {}
    return {
        "answer_state": answer.get("answer_state"),
        "confidence": round(float(answer.get("confidence", 0.0)), 4)
        if answer.get("confidence") is not None
        else None,
        "state_reason": answer.get("state_reason"),
        "top_hit_kind": top_hit.get("kind"),
        "top_hit_title": top_hit.get("title"),
        "top_score": round(float(top_hit.get("score", 0.0)), 4) if top_hit else 0.0,
        "citations": list(answer.get("citations") or [])[:6],
        "evidence_count": len(answer.get("evidence") or []),
    }


def compare_query_result(query: str, live_root: Path, candidate_root: Path) -> dict[str, Any]:
    live_result = summarize_query_result(live_root, query)
    candidate_result = summarize_query_result(candidate_root, query)
    changed_fields: list[str] = []
    for field in ("answer_state", "top_hit_kind", "top_hit_title", "citations", "evidence_count"):
        if live_result.get(field) != candidate_result.get(field):
            changed_fields.append(field)
    confidence_delta = round(
        float(candidate_result.get("confidence") or 0.0)
        - float(live_result.get("confidence") or 0.0),
        4,
    )
    if confidence_delta != 0:
        changed_fields.append("confidence")
    return {
        "query": query,
        "changed": bool(changed_fields),
        "changed_fields": changed_fields,
        "confidence_delta": confidence_delta,
        "live": live_result,
        "candidate": candidate_result,
    }


def build_corpus_diff_payload(
    live_entry: dict[str, Any],
    candidate_root: Path,
    *,
    provider: str | None = None,
    query_limit: int = 8,
) -> dict[str, Any]:
    resolved_candidate_root = candidate_root.resolve()
    live_surface = load_corpus_surface(live_entry)
    candidate_surface = load_corpus_surface(
        candidate_entry_for_root(
            resolved_candidate_root,
            target_corpus_id=live_entry["corpus_id"],
            target_name=live_entry["name"],
        ),
    )
    live_summary = dict(live_surface.get("summary") or {})
    candidate_summary = dict(candidate_surface.get("summary") or {})
    candidate_validation = validate_corpus_root(resolved_candidate_root)

    family_delta = build_collection_delta(
        collection_map(
            live_surface.get("families") or [],
            key_field="canonical_family_id",
            label_field="canonical_title",
        ),
        collection_map(
            candidate_surface.get("families") or [],
            key_field="canonical_family_id",
            label_field="canonical_title",
        ),
        include_label_changes=True,
    )
    action_delta = build_collection_delta(
        collection_map(
            live_surface.get("actions") or [],
            key_field="action_key",
            label_field="canonical_action",
        ),
        collection_map(
            candidate_surface.get("actions") or [],
            key_field="action_key",
            label_field="canonical_action",
        ),
    )
    unresolved_delta = build_collection_delta(
        collection_map(
            live_surface.get("unresolved") or [],
            key_field="question_key",
            label_field="canonical_question",
        ),
        collection_map(
            candidate_surface.get("unresolved") or [],
            key_field="question_key",
            label_field="canonical_question",
        ),
    )
    entity_delta = build_collection_delta(
        collection_map(
            live_surface.get("entities") or [],
            key_field="canonical_entity_id",
            label_field="canonical_label",
        ),
        collection_map(
            candidate_surface.get("entities") or [],
            key_field="canonical_entity_id",
            label_field="canonical_label",
        ),
    )

    queries = representative_queries(live_surface, candidate_surface, limit=query_limit)
    query_comparisons = [
        compare_query_result(query, Path(live_entry["root"]).resolve(), resolved_candidate_root)
        for query in queries
    ]
    changed_query_count = sum(1 for item in query_comparisons if item["changed"])

    summary = {
        "thread_count_delta": candidate_summary.get("thread_count", 0)
        - live_summary.get("thread_count", 0),
        "family_count_delta": candidate_summary.get("family_count", 0)
        - live_summary.get("family_count", 0),
        "action_count_delta": candidate_summary.get("action_count", 0)
        - live_summary.get("action_count", 0),
        "unresolved_count_delta": candidate_summary.get("unresolved_count", 0)
        - live_summary.get("unresolved_count", 0),
        "entity_count_delta": candidate_summary.get("entity_count", 0)
        - live_summary.get("entity_count", 0),
        "added_family_count": family_delta["added_count"],
        "removed_family_count": family_delta["removed_count"],
        "changed_family_title_count": family_delta["label_change_count"],
        "added_action_count": action_delta["added_count"],
        "removed_action_count": action_delta["removed_count"],
        "added_unresolved_count": unresolved_delta["added_count"],
        "removed_unresolved_count": unresolved_delta["removed_count"],
        "added_entity_count": entity_delta["added_count"],
        "removed_entity_count": entity_delta["removed_count"],
        "query_count": len(query_comparisons),
        "changed_query_count": changed_query_count,
    }
    summary["structural_change_count"] = sum(
        int(summary[key])
        for key in (
            "added_family_count",
            "removed_family_count",
            "changed_family_title_count",
            "added_action_count",
            "removed_action_count",
            "added_unresolved_count",
            "removed_unresolved_count",
            "added_entity_count",
            "removed_entity_count",
        )
    )

    fail_reasons: list[str] = []
    review_reasons: list[str] = []
    if not candidate_validation.get("valid"):
        fail_reasons.append("Candidate corpus is missing required contract files.")
    if candidate_summary.get("evaluation_overall_state") == "fail":
        fail_reasons.append("Candidate regression gates are failing.")
    if candidate_summary.get("source_reliability_state") == "fail":
        fail_reasons.append("Candidate source reliability is failing.")
    if summary["structural_change_count"] > 0:
        review_reasons.append("Canonical families, actions, unresolved items, or entities changed.")
    if changed_query_count > 0:
        review_reasons.append("Representative retrieval and answer behavior changed.")
    if live_summary.get("evaluation_overall_state") != candidate_summary.get(
        "evaluation_overall_state"
    ):
        review_reasons.append("Candidate evaluation state differs from the live baseline.")
    if candidate_summary.get("source_freshness_state") not in {"fresh", "not_applicable"}:
        review_reasons.append("Candidate source freshness is not yet fresh.")

    if fail_reasons:
        overall_state = "fail"
    elif review_reasons:
        overall_state = "review"
    else:
        overall_state = "ready"

    return {
        "generated_at": now_iso(),
        "provider": provider,
        "live": {
            "corpus_id": live_entry["corpus_id"],
            "name": live_entry["name"],
            "root": str(Path(live_entry["root"]).resolve()),
            "summary": live_summary,
        },
        "candidate": {
            "corpus_id": live_entry["corpus_id"],
            "name": candidate_summary.get("name") or live_entry["name"],
            "root": str(resolved_candidate_root),
            "summary": candidate_summary,
            "validation": candidate_validation,
        },
        "summary": summary,
        "collections": {
            "families": family_delta,
            "actions": action_delta,
            "unresolved": unresolved_delta,
            "entities": entity_delta,
        },
        "queries": query_comparisons,
        "evaluation": {
            "overall_state": overall_state,
            "fail_reasons": fail_reasons,
            "review_reasons": review_reasons,
        },
    }


def render_corpus_diff(payload: dict[str, Any]) -> str:
    live = payload.get("live") or {}
    candidate = payload.get("candidate") or {}
    summary = payload.get("summary") or {}
    evaluation = payload.get("evaluation") or {}
    lines = [
        "# Corpus Candidate Diff",
        "",
        f"- Generated: {payload.get('generated_at') or 'n/a'}",
        f"- Provider: {payload.get('provider') or 'n/a'}",
        f"- Live corpus: `{live.get('corpus_id') or 'n/a'}`",
        f"- Live root: {live.get('root') or 'n/a'}",
        f"- Candidate root: {candidate.get('root') or 'n/a'}",
        f"- Recommendation: {evaluation.get('overall_state') or 'n/a'}",
        f"- Structural changes: {summary.get('structural_change_count', 0)}",
        f"- Changed representative queries: {summary.get('changed_query_count', 0)} / {summary.get('query_count', 0)}",
        "",
        "## Counts",
        "",
        f"- Threads delta: {summary.get('thread_count_delta', 0)}",
        f"- Families delta: {summary.get('family_count_delta', 0)}",
        f"- Actions delta: {summary.get('action_count_delta', 0)}",
        f"- Unresolved delta: {summary.get('unresolved_count_delta', 0)}",
        f"- Entities delta: {summary.get('entity_count_delta', 0)}",
        "",
        "## Review Reasons",
        "",
    ]
    reasons = evaluation.get("fail_reasons") or evaluation.get("review_reasons") or []
    if reasons:
        for item in reasons:
            lines.append(f"- {item}")
    else:
        lines.append("- No blocking or review-level changes detected.")

    lines.extend(["", "## Representative Queries", ""])
    for item in payload.get("queries") or []:
        live_result = item.get("live") or {}
        candidate_result = item.get("candidate") or {}
        lines.append(
            f"- {item['query']}: "
            f"live={live_result.get('answer_state') or 'n/a'} "
            f"candidate={candidate_result.get('answer_state') or 'n/a'} "
            f"changed={'yes' if item.get('changed') else 'no'}",
        )
        if item.get("changed_fields"):
            lines.append(f"  fields={', '.join(item['changed_fields'])}")
    return "\n".join(lines)
