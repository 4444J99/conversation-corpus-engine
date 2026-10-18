#!/usr/bin/env python3
"""Federated canon: review queue, decisions and the materialized canonical groups.

Federated group ids. A group a human named keeps that name (`canonical_subject`). Any other
group, a singleton included, is `<prefix>-<slug of its member ids>`. The slug is cut at
`GROUP_SLUG_LIMIT` characters, and a cut slug no longer tells two long member ids apart (archive
members share a long `corpus-<provider>-archive:family-<provider>-` stem), so whenever the cut
removes anything the id gets a stable 16 hex digest of the full, sorted member ids appended. A slug
that fits is left exactly as it was, and the same members always give the same id.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from typing import Any

from .answering import load_json, slugify, tokenize, write_json, write_markdown
from .archive_naming import hash_identity
from .schema_validation import validate_json_file
from .sharded_collection import collection_storage_path, write_corpus_collection

GROUP_SLUG_LIMIT = 72
FEDERATION_CONTRACT = "conversation-corpus-engine.v2"
FEDERATION_CONTRACT_VERSION = 2
FEDERATED_REVIEW_TYPES = {
    "entity-alias": ("accepted_entity_aliases", "rejected_entity_aliases"),
    "family-merge": ("accepted_family_merges", "rejected_family_merges"),
    "action-merge": ("accepted_action_merges", "rejected_action_merges"),
    "unresolved-merge": ("accepted_unresolved_merges", "rejected_unresolved_merges"),
    "contradiction": ("accepted_contradictions", "rejected_contradictions"),
}
DEFAULT_FEDERATED_DECISIONS = {
    "accepted_entity_aliases": [],
    "rejected_entity_aliases": [],
    "accepted_family_merges": [],
    "rejected_family_merges": [],
    "accepted_action_merges": [],
    "rejected_action_merges": [],
    "accepted_unresolved_merges": [],
    "rejected_unresolved_merges": [],
    "accepted_contradictions": [],
    "rejected_contradictions": [],
}
CORE_CONTRACT_FILES = (
    "corpus/threads-index.collection",
    "corpus/semantic-v3-index.collection",
    "corpus/pairs-index.collection",
    "corpus/doctrine-briefs.collection",
    "corpus/family-dossiers.collection",
)
LEGACY_CORE_CONTRACT_FILES = (
    "corpus/threads-index.json",
    "corpus/semantic-v3-index.json",
    "corpus/pairs-index.json",
    "corpus/doctrine-briefs.json",
    "corpus/family-dossiers.json",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def federation_state_path(project_root: Path, filename: str) -> Path:
    return project_root / "state" / filename


def federation_output_path(project_root: Path, filename: str) -> Path:
    return project_root / "federation" / filename


def normalize_label(value: str) -> str:
    return " ".join(tokenize(value))


def token_set(value: str | list[str]) -> set[str]:
    if isinstance(value, list):
        tokens: list[str] = []
        for item in value:
            tokens.extend(tokenize(item))
        return set(tokens)
    return set(tokenize(value))


def jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    overlap = len(left & right)
    union = len(left | right)
    return round(overlap / union, 4) if union else 0.0


def suppress_low_signal_entity_alias(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_label = normalize_label(left.get("canonical_label") or "")
    right_label = normalize_label(right.get("canonical_label") or "")
    if not left_label or left_label != right_label:
        return False
    if left.get("entity_type") != "concept" or right.get("entity_type") != "concept":
        return False
    return len(left_label.split()) <= 1


def decision_subject_key(subject_ids: list[str]) -> str:
    return "::".join(sorted(set(subject_ids)))


def decision_lookup(decisions: dict[str, Any], key: str) -> set[str]:
    return {
        decision_subject_key(item.get("subject_ids") or [])
        for item in decisions.get(key, [])
        if item.get("subject_ids")
    }


def load_federated_decisions(project_root: Path) -> dict[str, Any]:
    path = federation_state_path(project_root, "federated-canonical-decisions.json")
    payload = load_json(path, default={}) or {}
    merged = {"generated_at": payload.get("generated_at"), **DEFAULT_FEDERATED_DECISIONS}
    for key in DEFAULT_FEDERATED_DECISIONS:
        merged[key] = payload.get(key, [])
    return merged


def save_federated_decisions(project_root: Path, payload: dict[str, Any]) -> None:
    payload["generated_at"] = now_iso()
    write_json(federation_state_path(project_root, "federated-canonical-decisions.json"), payload)


def load_federated_review_queue(project_root: Path) -> dict[str, Any]:
    return load_json(
        federation_state_path(project_root, "federated-review-queue.json"),
        default={"generated_at": None, "open_count": 0, "items": []},
    ) or {"generated_at": None, "open_count": 0, "items": []}


def save_federated_review_queue(project_root: Path, payload: dict[str, Any]) -> None:
    payload["generated_at"] = now_iso()
    payload["open_count"] = sum(
        1 for item in payload.get("items", []) if item.get("status") == "open"
    )
    write_json(federation_state_path(project_root, "federated-review-queue.json"), payload)
    write_json(federation_output_path(project_root, "review-queue.json"), payload)


def load_federated_review_history(project_root: Path) -> dict[str, Any]:
    return load_json(
        federation_state_path(project_root, "federated-review-history.json"),
        default={"generated_at": None, "count": 0, "items": []},
    ) or {"generated_at": None, "count": 0, "items": []}


def save_federated_review_history(project_root: Path, payload: dict[str, Any]) -> None:
    payload["generated_at"] = now_iso()
    payload["count"] = len(payload.get("items", []))
    write_json(federation_state_path(project_root, "federated-review-history.json"), payload)
    write_json(federation_output_path(project_root, "review-history.json"), payload)


def append_federated_review_history(
    project_root: Path,
    item: dict[str, Any],
    *,
    decision: str,
    note: str,
    canonical_subject: str | None = None,
) -> dict[str, Any]:
    payload = load_federated_review_history(project_root)
    entry = {
        "review_id": item.get("review_id"),
        "review_type": item.get("review_type"),
        "decision": decision,
        "note": note,
        "canonical_subject": canonical_subject
        or item.get("canonical_subject")
        or item.get("suggested_canonical_subject")
        or "",
        "subject_ids": item.get("subject_ids") or [],
        "source_corpora": item.get("source_corpora") or [],
        "recorded_at": now_iso(),
    }
    payload.setdefault("items", []).append(entry)
    save_federated_review_history(project_root, payload)
    return entry


def add_decision_record(
    decisions: dict[str, Any],
    review_type: str,
    subject_ids: list[str],
    *,
    decision: str,
    canonical_subject: str | None,
    review_id: str,
) -> None:
    accepted_key, rejected_key = FEDERATED_REVIEW_TYPES[review_type]
    target_key = accepted_key if decision == "accepted" else rejected_key
    other_key = rejected_key if decision == "accepted" else accepted_key
    pair_key = decision_subject_key(subject_ids)
    decisions[target_key] = [
        item
        for item in decisions.get(target_key, [])
        if decision_subject_key(item.get("subject_ids") or []) != pair_key
    ]
    decisions[other_key] = [
        item
        for item in decisions.get(other_key, [])
        if decision_subject_key(item.get("subject_ids") or []) != pair_key
    ]
    decisions[target_key].append(
        {
            "review_id": review_id,
            "subject_ids": sorted(set(subject_ids)),
            "canonical_subject": canonical_subject or "",
            "recorded_at": now_iso(),
        },
    )


def resolve_federated_review_item(
    project_root: Path,
    review_id: str,
    decision: str,
    note: str,
    canonical_subject: str | None = None,
) -> dict[str, Any]:
    queue = load_federated_review_queue(project_root)
    for item in queue.get("items", []):
        if item.get("review_id") != review_id:
            continue
        item["status"] = decision
        item["decision_note"] = note
        item["canonical_subject"] = (
            canonical_subject
            or item.get("canonical_subject")
            or item.get("suggested_canonical_subject")
            or ""
        )
        item["resolved_at"] = now_iso()
        item["updated_at"] = item["resolved_at"]
        save_federated_review_queue(project_root, queue)
        append_federated_review_history(
            project_root,
            item,
            decision=decision,
            note=note,
            canonical_subject=canonical_subject,
        )
        if (
            decision in {"accepted", "rejected"}
            and item.get("review_type") in FEDERATED_REVIEW_TYPES
        ):
            decisions = load_federated_decisions(project_root)
            add_decision_record(
                decisions,
                item["review_type"],
                item.get("subject_ids") or [],
                decision=decision,
                canonical_subject=canonical_subject,
                review_id=review_id,
            )
            save_federated_decisions(project_root, decisions)
        return item
    raise KeyError(f"Federated review item not found: {review_id}")


class DuplicateFederatedId(ValueError):
    """Two different groups of members were given the same federated id."""


class FederatedGroupingError(ValueError):
    """The grouping lost or repeated a member, so the federation would be wrong."""


class ContractInvalid(ValueError):
    """A corpus contract.json does not satisfy corpus-contract.schema.json."""


class UnionFind:
    """Disjoint sets over string items.

    With `labels` (item -> label, here the corpus id), a union is refused when the two sets
    already share a label, so no set ever holds two items with the same label.
    """

    def __init__(self, items: list[str], labels: dict[str, str] | None = None) -> None:
        self.parent = {item: item for item in items}
        self.labels: dict[str, set[str]] | None = (
            {item: {labels[item]} for item in items} if labels is not None else None
        )

    def find(self, item: str) -> str:
        root = self.parent[item]
        if root != item:
            self.parent[item] = self.find(root)
        return self.parent[item]

    def union(self, left: str, right: str) -> bool:
        """Join the sets of `left` and `right`; False when the labels forbid it."""
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return True
        if self.labels is not None and self.labels[left_root] & self.labels[right_root]:
            return False
        keep, drop = (left_root, right_root) if left_root < right_root else (right_root, left_root)
        self.parent[drop] = keep
        if self.labels is not None:
            self.labels[keep] = self.labels[keep] | self.labels.pop(drop)
        return True

    def groups(self) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = defaultdict(list)
        for item in self.parent:
            grouped[self.find(item)].append(item)
        return {root: sorted(values) for root, values in grouped.items()}


def collect_family_records(surfaces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for surface in surfaces:
        summary = surface["summary"]
        dossiers = {item["family_id"]: item for item in surface.get("family_dossiers", [])}
        briefs = {item["family_id"]: item for item in surface.get("doctrine_briefs", [])}
        for item in surface["families"]:
            family_id = item.get("canonical_family_id") or item.get("family_id")
            dossier = dossiers.get(family_id, {})
            brief = briefs.get(family_id, {})
            record = {
                "member_id": f"{summary['corpus_id']}:{family_id}",
                "corpus_id": summary["corpus_id"],
                "corpus_name": summary["name"],
                "source_root": summary["root"],
                "family_id": family_id,
                "canonical_title": item.get("canonical_title") or family_id,
                "canonical_thread_uid": item.get("canonical_thread_uid"),
                "thread_uids": item.get("thread_uids") or [],
                "stable_themes": brief.get("stable_themes") or dossier.get("stable_themes") or [],
                "key_entities": [
                    entity.get("canonical_label")
                    for entity in dossier.get("key_entities", [])
                    if entity.get("canonical_label")
                ],
                "actions": [
                    action.get("canonical_action")
                    for action in dossier.get("actions", [])
                    if action.get("canonical_action")
                ],
                "unresolved": [
                    question.get("canonical_question")
                    for question in dossier.get("unresolved", [])
                    if question.get("canonical_question")
                ],
                "search_text": " ".join(
                    [
                        item.get("canonical_title") or "",
                        brief.get("brief_text") or "",
                        dossier.get("doctrine_summary") or "",
                        " ".join(brief.get("stable_themes") or dossier.get("stable_themes") or []),
                    ],
                ).strip(),
            }
            records.append(record)
    return records


def collect_entity_records(surfaces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for surface in surfaces:
        summary = surface["summary"]
        for item in surface["entities"]:
            label = item.get("canonical_label")
            if not label:
                continue
            records.append(
                {
                    "member_id": f"{summary['corpus_id']}:{item.get('canonical_entity_id') or slugify(label)}",
                    "corpus_id": summary["corpus_id"],
                    "corpus_name": summary["name"],
                    "source_root": summary["root"],
                    "entity_id": item.get("canonical_entity_id") or slugify(label),
                    "canonical_label": label,
                    "entity_type": item.get("entity_type") or "concept",
                    "aliases": item.get("aliases") or [],
                    "search_text": " ".join([label] + (item.get("aliases") or [])),
                },
            )
    return records


def collect_action_records(surfaces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for surface in surfaces:
        summary = surface["summary"]
        for item in surface["actions"]:
            action = item.get("canonical_action")
            if not action:
                continue
            records.append(
                {
                    "member_id": f"{summary['corpus_id']}:{item.get('action_key')}",
                    "corpus_id": summary["corpus_id"],
                    "corpus_name": summary["name"],
                    "source_root": summary["root"],
                    "action_key": item.get("action_key"),
                    "canonical_action": action,
                    "status": item.get("status") or "open",
                    "family_ids": item.get("family_ids") or [],
                    "thread_uids": item.get("thread_uids") or [],
                    "occurrence_count": item.get("occurrence_count", 0),
                    "search_text": action,
                },
            )
    return records


def collect_unresolved_records(surfaces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for surface in surfaces:
        summary = surface["summary"]
        for item in surface["unresolved"]:
            question = item.get("canonical_question")
            if not question:
                continue
            records.append(
                {
                    "member_id": f"{summary['corpus_id']}:{item.get('question_key')}",
                    "corpus_id": summary["corpus_id"],
                    "corpus_name": summary["name"],
                    "source_root": summary["root"],
                    "question_key": item.get("question_key"),
                    "canonical_question": question,
                    "why_unresolved": item.get("why_unresolved") or "",
                    "family_ids": item.get("family_ids") or [],
                    "thread_uids": item.get("thread_uids") or [],
                    "occurrence_count": item.get("occurrence_count", 0),
                    "search_text": " ".join([question, item.get("why_unresolved") or ""]).strip(),
                },
            )
    return records


def entity_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    label_score = (
        1.0
        if normalize_label(left["canonical_label"]) == normalize_label(right["canonical_label"])
        else 0.0
    )
    alias_score = jaccard(
        token_set([left["canonical_label"], *left.get("aliases", [])]),
        token_set([right["canonical_label"], *right.get("aliases", [])]),
    )
    return round(max(label_score, alias_score), 4)


def family_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    title_score = (
        1.0
        if normalize_label(left["canonical_title"]) == normalize_label(right["canonical_title"])
        else jaccard(token_set(left["canonical_title"]), token_set(right["canonical_title"]))
    )
    theme_score = jaccard(
        token_set(left.get("stable_themes", [])), token_set(right.get("stable_themes", []))
    )
    entity_score = jaccard(
        token_set(left.get("key_entities", [])), token_set(right.get("key_entities", []))
    )
    action_score = jaccard(token_set(left.get("actions", [])), token_set(right.get("actions", [])))
    return round(
        max(
            title_score,
            (0.6 * title_score) + (0.2 * theme_score) + (0.1 * entity_score) + (0.1 * action_score),
        ),
        4,
    )


def action_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    return round(
        max(
            1.0
            if normalize_label(left["canonical_action"])
            == normalize_label(right["canonical_action"])
            else 0.0,
            jaccard(token_set(left["canonical_action"]), token_set(right["canonical_action"])),
        ),
        4,
    )


def unresolved_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    return round(
        max(
            1.0
            if normalize_label(left["canonical_question"])
            == normalize_label(right["canonical_question"])
            else 0.0,
            jaccard(token_set(left["canonical_question"]), token_set(right["canonical_question"])),
        ),
        4,
    )


def contradiction_signal(left: dict[str, Any], right: dict[str, Any]) -> float:
    title_score = jaccard(
        token_set(left.get("canonical_title", "")), token_set(right.get("canonical_title", ""))
    )
    theme_score = jaccard(
        token_set(left.get("stable_themes", [])), token_set(right.get("stable_themes", []))
    )
    if title_score >= 0.6 and theme_score <= 0.15:
        return round(title_score - theme_score, 4)
    return 0.0


def _subject_fingerprint(subject_ids: list[str]) -> str:
    return sha1(decision_subject_key(subject_ids).encode("utf-8")).hexdigest()[:8]


def build_review_id(review_type: str, subject_ids: list[str]) -> str:
    base = f"federated-{review_type}-{slugify(' '.join(subject_ids), limit=80)}"
    return f"{base}-{_subject_fingerprint(subject_ids)}"


def build_review_id_legacy(review_type: str, subject_ids: list[str]) -> str:
    """Pre-migration ID format without fingerprint suffix. Used for mapping."""
    return f"federated-{review_type}-{slugify(' '.join(subject_ids), limit=80)}"


def stabilize_review_ids(suggestions: list[dict[str, Any]]) -> None:
    review_id_counts = Counter(item["review_id"] for item in suggestions)
    for item in suggestions:
        review_id = item["review_id"]
        if review_id_counts[review_id] <= 1:
            continue
        fingerprint = _subject_fingerprint(item.get("subject_ids") or [])
        item["review_id"] = f"{review_id}-{fingerprint}"


def build_pair_suggestions(
    records: list[dict[str, Any]],
    *,
    review_type: str,
    similarity_fn,
    threshold: float,
    decisions: dict[str, Any],
    contradiction_mode: bool = False,
) -> list[dict[str, Any]]:
    accepted_key, rejected_key = FEDERATED_REVIEW_TYPES[review_type]
    accepted = decision_lookup(decisions, accepted_key)
    rejected = decision_lookup(decisions, rejected_key)
    suggestions: list[dict[str, Any]] = []
    for index, left in enumerate(records):
        for right in records[index + 1 :]:
            if left["corpus_id"] == right["corpus_id"]:
                continue
            subject_ids = sorted([left["member_id"], right["member_id"]])
            pair_key = decision_subject_key(subject_ids)
            if pair_key in accepted or pair_key in rejected:
                continue
            if review_type == "entity-alias" and suppress_low_signal_entity_alias(left, right):
                continue
            score = similarity_fn(left, right)
            if score < threshold:
                continue
            rationale = (
                f"Potential contradiction between {left.get('canonical_title') or left.get('canonical_label')}"
                if contradiction_mode
                else f"High cross-corpus overlap between {left.get('canonical_title') or left.get('canonical_label') or left.get('canonical_action') or left.get('canonical_question')}"
            )
            suggestions.append(
                {
                    "review_id": build_review_id(review_type, subject_ids),
                    "review_type": review_type,
                    "status": "open",
                    "priority": "high" if score >= max(0.8, threshold + 0.15) else "medium",
                    "title": f"{left.get('canonical_title') or left.get('canonical_label') or left.get('canonical_action') or left.get('canonical_question')} <> {right.get('canonical_title') or right.get('canonical_label') or right.get('canonical_action') or right.get('canonical_question')}",
                    "subject_ids": subject_ids,
                    "suggested_canonical_subject": slugify(
                        left.get("canonical_title")
                        or left.get("canonical_label")
                        or left.get("canonical_action")
                        or left.get("canonical_question"),
                        limit=72,
                    ),
                    "canonical_subject": "",
                    "rationale": rationale,
                    "score": round(score, 4),
                    "source_corpora": sorted({left["corpus_id"], right["corpus_id"]}),
                    "updated_at": now_iso(),
                },
            )
    stabilize_review_ids(suggestions)
    suggestions.sort(
        key=lambda item: (item["priority"] != "high", -item["score"], item["review_id"])
    )
    return suggestions


def _group_canonical_subject(accepted: list[dict[str, Any]], member_ids: list[str]) -> str:
    """The canonical subject a human gave to (part of) this group.

    Accepted decisions are pairwise as often as not, so a group of three or more is covered by
    several decisions and no single one lists every member. Every decision whose subjects all lie
    inside the group competes; the one covering the most members wins, then the latest recorded,
    then the one stored last.
    """
    members = set(member_ids)
    best: tuple[int, str, int] | None = None
    chosen = ""
    for position, decision in enumerate(accepted):
        subject = decision.get("canonical_subject")
        subject_ids = set(decision.get("subject_ids") or [])
        if not subject or len(subject_ids) < 2 or not subject_ids <= members:
            continue
        rank = (len(subject_ids), str(decision.get("recorded_at") or ""), position)
        if best is None or rank > best:
            best, chosen = rank, str(subject)
    return chosen


def default_group_id(prefix: str, member_ids: list[str]) -> str:
    """`<prefix>-<slug>` for a group no human named; the full-member digest is appended when the
    slug had to be cut, so two groups whose ids share their first characters cannot collide."""
    joined = " ".join(member_ids)
    slug = slugify(joined, limit=GROUP_SLUG_LIMIT)
    if slug != slugify(joined, limit=len(joined) + 1):
        return f"{prefix}-{slug}-{hash_identity(decision_subject_key(member_ids))}"
    return f"{prefix}-{slug}"


def apply_decision_groups(
    records: list[dict[str, Any]],
    decisions: dict[str, Any],
    accepted_key: str,
    canonical_subject_prefix: str,
    *,
    skipped: list[dict[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    repeated = sorted(
        member_id
        for member_id, count in Counter(record["member_id"] for record in records).items()
        if count > 1
    )
    if repeated:
        raise FederatedGroupingError(
            f"{len(repeated)} member id(s) appear more than once in the {accepted_key} records, "
            f"so a record would be silently lost: {', '.join(repeated[:5])}"
        )
    record_map = {record["member_id"]: record for record in records}
    union = UnionFind(
        list(record_map.keys()),
        labels={member_id: str(record["corpus_id"]) for member_id, record in record_map.items()},
    )
    accepted = list(decisions.get(accepted_key, []))
    for decision in accepted:
        subject_ids = [
            subject_id for subject_id in decision.get("subject_ids", []) if subject_id in record_map
        ]
        if len(subject_ids) < 2:
            continue
        base = subject_ids[0]
        for subject_id in subject_ids[1:]:
            if not union.union(base, subject_id) and skipped is not None:
                skipped.append(
                    {
                        "decision": accepted_key,
                        "review_id": decision.get("review_id"),
                        "members": sorted([base, subject_id]),
                        "reason": "both sides already hold a member from the same corpus",
                    }
                )
    groups: dict[str, list[dict[str, Any]]] = {}
    for _root, member_ids in union.groups().items():
        members = [record_map[member_id] for member_id in member_ids]
        canonical_subject = _group_canonical_subject(accepted, member_ids)
        group_id = canonical_subject or default_group_id(canonical_subject_prefix, member_ids)
        if group_id in groups:
            clash = sorted({record["member_id"] for record in groups[group_id]} | set(member_ids))
            raise DuplicateFederatedId(
                f"federated id {group_id!r} would be given to two different groups "
                f"({', '.join(clash)})"
            )
        groups[group_id] = members
    placed = [member["member_id"] for members in groups.values() for member in members]
    if sorted(placed) != sorted(record["member_id"] for record in records):
        raise FederatedGroupingError(
            f"grouping for {accepted_key} placed {len(placed)} members "
            f"of {len(records)}: every member must appear exactly once"
        )
    return groups


def aggregate_vector_terms(texts: list[str], *, limit: int = 16) -> dict[str, float]:
    counts = Counter(token for text in texts for token in tokenize(text))
    if not counts:
        return {}
    maximum = max(counts.values())
    return {
        token: round(count / maximum, 4)  # allow-secret
        for token, count in counts.most_common(limit)
    }


def materialize_canonical_entities(groups: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for canonical_id, members in sorted(groups.items()):
        labels = [member["canonical_label"] for member in members if member.get("canonical_label")]
        canonical_label = sorted(labels, key=lambda value: (len(value), value.lower()))[0]
        aliases = sorted(
            {
                alias
                for member in members
                for alias in ([member["canonical_label"]] + (member.get("aliases") or []))
                if alias and alias != canonical_label
            }
        )
        texts = [member.get("search_text") or "" for member in members]
        payload.append(
            {
                "federated_entity_id": canonical_id,
                "canonical_label": canonical_label,
                "entity_type": Counter(
                    member.get("entity_type") or "concept" for member in members
                ).most_common(1)[0][0],
                "aliases": aliases,
                "corpus_ids": sorted({member["corpus_id"] for member in members}),
                "member_count": len(members),
                "member_entities": members,
                "vector_terms": aggregate_vector_terms(texts),
                "search_text": " ".join([canonical_label] + aliases + texts).strip(),
            },
        )
    return payload


def materialize_canonical_families(groups: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for canonical_id, members in sorted(groups.items()):
        titles = [member["canonical_title"] for member in members if member.get("canonical_title")]
        canonical_title = Counter(titles).most_common(1)[0][0]
        theme_counts = Counter(
            theme for member in members for theme in member.get("stable_themes", [])
        )
        entity_counts = Counter(
            entity for member in members for entity in member.get("key_entities", [])
        )
        action_text = [action for member in members for action in member.get("actions", [])]
        unresolved_text = [
            question for member in members for question in member.get("unresolved", [])
        ]
        texts = [member.get("search_text") or "" for member in members]
        payload.append(
            {
                "federated_family_id": canonical_id,
                "canonical_title": canonical_title,
                "corpus_ids": sorted({member["corpus_id"] for member in members}),
                "member_count": len(members),
                "member_families": members,
                "canonical_thread_refs": [
                    {
                        "corpus_id": member["corpus_id"],
                        "thread_uid": member.get("canonical_thread_uid"),
                    }
                    for member in members
                    if member.get("canonical_thread_uid")
                ],
                "stable_themes": [token for token, _ in theme_counts.most_common(8)],
                "key_entities": [token for token, _ in entity_counts.most_common(6)],
                "action_count": len(action_text),
                "unresolved_count": len(unresolved_text),
                "vector_terms": aggregate_vector_terms(texts + action_text + unresolved_text),
                "search_text": " ".join(
                    [canonical_title] + texts + action_text + unresolved_text
                ).strip(),
            },
        )
    return payload


def materialize_canonical_actions(groups: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for canonical_id, members in sorted(groups.items()):
        canonical_action = Counter(member["canonical_action"] for member in members).most_common(1)[
            0
        ][0]
        payload.append(
            {
                "federated_action_id": canonical_id,
                "canonical_action": canonical_action,
                "corpus_ids": sorted({member["corpus_id"] for member in members}),
                "member_count": len(members),
                "member_actions": members,
                "status": Counter(member.get("status") or "open" for member in members).most_common(
                    1
                )[0][0],
                "vector_terms": aggregate_vector_terms(
                    [member.get("search_text") or "" for member in members]
                ),
                "search_text": " ".join(
                    [canonical_action] + [member.get("search_text") or "" for member in members]
                ).strip(),
            },
        )
    return payload


def materialize_canonical_unresolved(
    groups: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for canonical_id, members in sorted(groups.items()):
        canonical_question = Counter(
            member["canonical_question"] for member in members
        ).most_common(1)[0][0]
        why_unresolved = Counter(
            member.get("why_unresolved") or "" for member in members if member.get("why_unresolved")
        ).most_common(1)
        payload.append(
            {
                "federated_question_id": canonical_id,
                "canonical_question": canonical_question,
                "why_unresolved": why_unresolved[0][0] if why_unresolved else "",
                "corpus_ids": sorted({member["corpus_id"] for member in members}),
                "member_count": len(members),
                "member_questions": members,
                "vector_terms": aggregate_vector_terms(
                    [member.get("search_text") or "" for member in members]
                ),
                "search_text": " ".join(
                    [canonical_question] + [member.get("search_text") or "" for member in members]
                ).strip(),
            },
        )
    return payload


def materialize_doctrine_briefs(canonical_families: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for item in canonical_families:
        corpora = ", ".join(item.get("corpus_ids") or [])
        themes = ", ".join(item.get("stable_themes") or []) or "no stable themes"
        brief = (
            f"{item['canonical_title']} spans {len(item.get('corpus_ids') or [])} corpora and "
            f"{item.get('member_count', 0)} family members. Stable themes: {themes}. "
            f"Corpora: {corpora or 'n/a'}."
        )
        payload.append(
            {
                "federated_family_id": item["federated_family_id"],
                "canonical_title": item["canonical_title"],
                "corpus_ids": item.get("corpus_ids") or [],
                "member_count": item.get("member_count", 0),
                "stable_themes": item.get("stable_themes") or [],
                "key_entities": item.get("key_entities") or [],
                "brief_text": brief,
                "search_text": " ".join(
                    [item["canonical_title"], themes, corpora, item.get("search_text") or ""]
                ).strip(),
                "vector_terms": item.get("vector_terms") or {},
            },
        )
    return payload


def materialize_entity_dossiers(canonical_entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for item in canonical_entities:
        aliases = ", ".join(item.get("aliases") or []) or "none"
        corpora = ", ".join(item.get("corpus_ids") or []) or "n/a"
        dossier = (
            f"{item['canonical_label']} appears across {len(item.get('corpus_ids') or [])} corpora. "
            f"Aliases: {aliases}. Corpora: {corpora}."
        )
        payload.append(
            {
                "federated_entity_id": item["federated_entity_id"],
                "canonical_label": item["canonical_label"],
                "entity_type": item.get("entity_type") or "concept",
                "corpus_ids": item.get("corpus_ids") or [],
                "aliases": item.get("aliases") or [],
                "member_count": item.get("member_count", 0),
                "dossier_text": dossier,
                "search_text": " ".join(
                    [item["canonical_label"], aliases, corpora, item.get("search_text") or ""]
                ).strip(),
                "vector_terms": item.get("vector_terms") or {},
            },
        )
    return payload


def materialize_project_dossiers(canonical_families: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for item in canonical_families:
        project_text = (
            f"{item['canonical_title']} is currently tracked as a federated project cluster with "
            f"{item.get('action_count', 0)} actions and {item.get('unresolved_count', 0)} unresolved questions "
            f"across {len(item.get('corpus_ids') or [])} corpora."
        )
        payload.append(
            {
                "project_id": f"project-{slugify(item['canonical_title'], limit=72)}",
                "canonical_title": item["canonical_title"],
                "federated_family_id": item["federated_family_id"],
                "corpus_ids": item.get("corpus_ids") or [],
                "stable_themes": item.get("stable_themes") or [],
                "project_text": project_text,
                "search_text": " ".join(
                    [item["canonical_title"], project_text, item.get("search_text") or ""]
                ).strip(),
                "vector_terms": item.get("vector_terms") or {},
            },
        )
    return payload


def materialize_lineage_map(canonical_families: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for item in canonical_families:
        payload.append(
            {
                "federated_family_id": item["federated_family_id"],
                "canonical_title": item["canonical_title"],
                "corpus_ids": item.get("corpus_ids") or [],
                "lineage": [
                    {
                        "corpus_id": member["corpus_id"],
                        "family_id": member["family_id"],
                        "canonical_thread_uid": member.get("canonical_thread_uid"),
                        "canonical_title": member.get("canonical_title"),
                    }
                    for member in item.get("member_families", [])
                ],
            },
        )
    return payload


def materialize_conflict_report(
    contradiction_suggestions: list[dict[str, Any]],
    canonical_families: list[dict[str, Any]],
    skipped_merges: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    multi_corpus = [item for item in canonical_families if len(item.get("corpus_ids") or []) > 1]
    return {
        "generated_at": now_iso(),
        "potential_conflict_count": len(contradiction_suggestions),
        "multi_corpus_family_count": len(multi_corpus),
        "potential_conflicts": contradiction_suggestions,
        "skipped_merges": list(skipped_merges or []),
        "multi_corpus_families": [
            {
                "federated_family_id": item["federated_family_id"],
                "canonical_title": item["canonical_title"],
                "corpus_ids": item.get("corpus_ids") or [],
                "stable_themes": item.get("stable_themes") or [],
            }
            for item in multi_corpus
        ],
    }


def render_overlap_report(
    canonical_entities: list[dict[str, Any]],
    canonical_families: list[dict[str, Any]],
    canonical_actions: list[dict[str, Any]],
    canonical_unresolved: list[dict[str, Any]],
    review_queue: dict[str, Any],
    conflict_report: dict[str, Any],
) -> str:
    lines = [
        "# Federated Overlap Report",
        "",
        f"- Generated: {now_iso()}",
        f"- Canonical entities: {len(canonical_entities)}",
        f"- Canonical families: {len(canonical_families)}",
        f"- Canonical actions: {len(canonical_actions)}",
        f"- Canonical unresolved: {len(canonical_unresolved)}",
        f"- Open review items: {review_queue.get('open_count', 0)}",
        f"- Potential conflicts: {conflict_report.get('potential_conflict_count', 0)}",
        "",
        "## Open Review Types",
        "",
    ]
    counter = Counter(
        item.get("review_type") or "unknown"
        for item in review_queue.get("items", [])
        if item.get("status") == "open"
    )
    if not counter:
        lines.append("No open federated review items.")
    else:
        for review_type, count in counter.most_common():
            lines.append(f"- {review_type}: {count}")
    return "\n".join(lines).rstrip()


def ensure_corpus_contract_manifest(
    corpus_root: Path,
    *,
    corpus_id: str,
    name: str,
    adapter_type: str,
    summary: dict[str, Any],
) -> dict[str, Any]:
    path = corpus_root / "corpus" / "contract.json"
    payload = load_json(path, default={}) or {}
    existing_contract_name = payload.get("contract_name")
    default_required_files = (
        LEGACY_CORE_CONTRACT_FILES
        if existing_contract_name == "conversation-corpus-engine-v1"
        else CORE_CONTRACT_FILES
    )
    payload.update(
        {
            "contract_name": existing_contract_name or FEDERATION_CONTRACT,
            "contract_version": payload.get("contract_version") or FEDERATION_CONTRACT_VERSION,
            "adapter_type": payload.get("adapter_type") or adapter_type,
            "corpus_id": payload.get("corpus_id") or corpus_id,
            "name": payload.get("name") or name,
            "generated_at": now_iso(),
            "required_files": payload.get("required_files") or list(default_required_files),
            "counts": {
                "threads": summary.get("thread_count", 0),
                "families": summary.get("family_count", 0),
                "actions": summary.get("action_count", 0),
                "unresolved": summary.get("unresolved_count", 0),
                "entities": summary.get("entity_count", 0),
            },
        },
    )
    write_json(path, payload)
    return payload


def validate_corpus_contracts(surfaces: list[dict[str, Any]]) -> None:
    """Check every corpus contract.json against its schema; raise on any miss."""
    problems: list[str] = []
    for surface in surfaces:
        summary = surface["summary"]
        path = Path(summary["root"]) / "corpus" / "contract.json"
        verdict = validate_json_file("corpus-contract", path)
        if not verdict["valid"]:
            problems.append(f"{summary['corpus_id']}: {'; '.join(map(str, verdict['errors']))}")
    if problems:
        raise ContractInvalid(
            "contract.json fails corpus-contract.schema.json: " + " | ".join(problems)
        )


def build_federated_canon(project_root: Path, surfaces: list[dict[str, Any]]) -> dict[str, str]:
    for surface in surfaces:
        summary = surface["summary"]
        ensure_corpus_contract_manifest(
            Path(summary["root"]),
            corpus_id=summary["corpus_id"],
            name=summary["name"],
            adapter_type=summary.get("adapter_type") or "external-memory-corpus",
            summary=summary,
        )
    validate_corpus_contracts(surfaces)

    decisions = load_federated_decisions(project_root)
    family_records = collect_family_records(surfaces)
    entity_records = collect_entity_records(surfaces)
    action_records = collect_action_records(surfaces)
    unresolved_records = collect_unresolved_records(surfaces)

    entity_suggestions = build_pair_suggestions(
        entity_records,
        review_type="entity-alias",
        similarity_fn=entity_similarity,
        threshold=0.7,
        decisions=decisions,
    )
    family_suggestions = build_pair_suggestions(
        family_records,
        review_type="family-merge",
        similarity_fn=family_similarity,
        threshold=0.65,
        decisions=decisions,
    )
    action_suggestions = build_pair_suggestions(
        action_records,
        review_type="action-merge",
        similarity_fn=action_similarity,
        threshold=0.78,
        decisions=decisions,
    )
    unresolved_suggestions = build_pair_suggestions(
        unresolved_records,
        review_type="unresolved-merge",
        similarity_fn=unresolved_similarity,
        threshold=0.78,
        decisions=decisions,
    )
    contradiction_suggestions = build_pair_suggestions(
        family_records,
        review_type="contradiction",
        similarity_fn=contradiction_signal,
        threshold=0.55,
        decisions=decisions,
        contradiction_mode=True,
    )

    queue_items = (
        entity_suggestions
        + family_suggestions
        + action_suggestions
        + unresolved_suggestions
        + contradiction_suggestions
    )
    existing_queue = load_federated_review_queue(project_root)
    prior_status = {item.get("review_id"): item for item in existing_queue.get("items", [])}
    for item in queue_items:
        previous = prior_status.get(item["review_id"])
        if previous and previous.get("status") != "open":
            item["status"] = previous.get("status")
            item["decision_note"] = previous.get("decision_note") or ""
            item["canonical_subject"] = previous.get("canonical_subject") or ""
            item["resolved_at"] = previous.get("resolved_at")
            item["updated_at"] = previous.get("updated_at") or item["updated_at"]
    stale_resolved = [
        item
        for item in existing_queue.get("items", [])
        if item.get("review_id") not in {candidate["review_id"] for candidate in queue_items}
        and item.get("status") != "open"
    ]
    queue_payload = {
        "generated_at": now_iso(),
        "open_count": 0,
        "items": queue_items + stale_resolved,
    }
    save_federated_review_queue(project_root, queue_payload)
    save_federated_decisions(project_root, decisions)

    skipped_merges: list[dict[str, Any]] = []
    entity_groups = apply_decision_groups(
        entity_records,
        decisions,
        "accepted_entity_aliases",
        "federated-entity",
        skipped=skipped_merges,
    )
    family_groups = apply_decision_groups(
        family_records,
        decisions,
        "accepted_family_merges",
        "federated-family",
        skipped=skipped_merges,
    )
    action_groups = apply_decision_groups(
        action_records,
        decisions,
        "accepted_action_merges",
        "federated-action",
        skipped=skipped_merges,
    )
    unresolved_groups = apply_decision_groups(
        unresolved_records,
        decisions,
        "accepted_unresolved_merges",
        "federated-question",
        skipped=skipped_merges,
    )

    canonical_entities = materialize_canonical_entities(entity_groups)
    canonical_families = materialize_canonical_families(family_groups)
    canonical_actions = materialize_canonical_actions(action_groups)
    canonical_unresolved = materialize_canonical_unresolved(unresolved_groups)
    doctrine_briefs = materialize_doctrine_briefs(canonical_families)
    entity_dossiers = materialize_entity_dossiers(canonical_entities)
    project_dossiers = materialize_project_dossiers(canonical_families)
    lineage_map = materialize_lineage_map(canonical_families)
    conflict_report = materialize_conflict_report(
        contradiction_suggestions, canonical_families, skipped_merges
    )

    overlap_report = render_overlap_report(
        canonical_entities,
        canonical_families,
        canonical_actions,
        canonical_unresolved,
        queue_payload,
        conflict_report,
    )

    fed_dir = project_root / "federation"
    fed_dir.mkdir(parents=True, exist_ok=True)
    collections = {
        "canonical-entities.json": canonical_entities,
        "canonical-families.json": canonical_families,
        "canonical-actions.json": canonical_actions,
        "canonical-unresolved.json": canonical_unresolved,
        "doctrine-briefs.json": doctrine_briefs,
        "entity-dossiers.json": entity_dossiers,
        "project-dossiers.json": project_dossiers,
        "lineage-map.json": lineage_map,
    }
    for filename, records in collections.items():
        write_corpus_collection(fed_dir / filename, records)
    write_json(fed_dir / "conflict-report.json", conflict_report)
    write_markdown(fed_dir / "overlap-report.md", overlap_report)
    write_markdown(
        fed_dir / "conflict-report.md",
        "\n".join(
            [
                "# Federated Conflict Report",
                "",
                f"- Generated: {conflict_report['generated_at']}",
                f"- Potential conflicts: {conflict_report['potential_conflict_count']}",
                f"- Multi-corpus families: {conflict_report['multi_corpus_family_count']}",
            ],
        ),
    )
    return {
        "canonical_entities_path": str(
            collection_storage_path(fed_dir / "canonical-entities.json")
        ),
        "canonical_families_path": str(
            collection_storage_path(fed_dir / "canonical-families.json")
        ),
        "canonical_actions_path": str(collection_storage_path(fed_dir / "canonical-actions.json")),
        "canonical_unresolved_path": str(
            collection_storage_path(fed_dir / "canonical-unresolved.json")
        ),
        "doctrine_briefs_path": str(collection_storage_path(fed_dir / "doctrine-briefs.json")),
        "entity_dossiers_path": str(collection_storage_path(fed_dir / "entity-dossiers.json")),
        "project_dossiers_path": str(collection_storage_path(fed_dir / "project-dossiers.json")),
        "lineage_map_path": str(collection_storage_path(fed_dir / "lineage-map.json")),
        "conflict_report_path": str(fed_dir / "conflict-report.json"),
        "overlap_report_path": str(fed_dir / "overlap-report.md"),
        "review_queue_path": str(fed_dir / "review-queue.json"),
        "review_history_path": str(fed_dir / "review-history.json"),
    }


# ---------------------------------------------------------------------------
# Review-ID migration: add fingerprint suffix to all IDs
# ---------------------------------------------------------------------------


def _migrate_item_id(item: dict[str, Any]) -> tuple[str, str]:
    """Compute the new fingerprinted review_id for an item. Returns (old_id, new_id)."""
    old_id = item.get("review_id", "")
    subject_ids = item.get("subject_ids") or []
    review_type = item.get("review_type", "")
    if not subject_ids or not review_type:
        return old_id, old_id
    new_id = build_review_id(review_type, subject_ids)
    return old_id, new_id


def migrate_review_ids(
    project_root: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Migrate all review IDs to the fingerprinted format.

    Updates queue, history, and decisions in-place. Writes a mapping file
    to state/review-id-mapping.json for audit trail.
    """
    state_dir = project_root / "state"
    queue_path = state_dir / "federated-review-queue.json"
    history_path = state_dir / "federated-review-history.json"
    decisions_path = state_dir / "federated-canonical-decisions.json"
    mapping_path = state_dir / "review-id-mapping.json"

    queue = load_json(queue_path, default={"items": []}) or {"items": []}
    history = load_json(history_path, default={"items": []}) or {"items": []}
    decisions = load_json(decisions_path, default=DEFAULT_FEDERATED_DECISIONS.copy()) or (
        DEFAULT_FEDERATED_DECISIONS.copy()
    )

    id_mapping: dict[str, str] = {}
    stats = {"queue_migrated": 0, "history_migrated": 0, "decisions_migrated": 0, "unchanged": 0}

    for item in queue.get("items", []):
        old_id, new_id = _migrate_item_id(item)
        if old_id != new_id:
            id_mapping[old_id] = new_id
            item["review_id"] = new_id
            stats["queue_migrated"] += 1
        else:
            stats["unchanged"] += 1

    for item in history.get("items", []):
        old_id, new_id = _migrate_item_id(item)
        if old_id != new_id:
            id_mapping.setdefault(old_id, new_id)
            item["review_id"] = new_id
            stats["history_migrated"] += 1
        else:
            stats["unchanged"] += 1

    for key in decisions:
        entries = decisions[key]
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict) or "review_id" not in entry:
                continue
            old_id = entry["review_id"]
            subject_ids = entry.get("subject_ids") or []
            review_type = entry.get("review_type", "")
            if subject_ids and not review_type:
                # Infer review_type from the key in decisions dictionary
                for rtype, (akey, rkey) in FEDERATED_REVIEW_TYPES.items():
                    if key in {akey, rkey}:
                        review_type = rtype
                        break

            if subject_ids and review_type:
                new_id = build_review_id(review_type, subject_ids)
                if old_id != new_id:
                    id_mapping.setdefault(old_id, new_id)
                    entry["review_id"] = new_id
                    stats["decisions_migrated"] += 1
                else:
                    stats["unchanged"] += 1
            else:
                stats["unchanged"] += 1

    mapping_payload = {
        "generated_at": now_iso(),
        "migration": "review-id-fingerprint-v1",
        "stats": stats,
        "id_count": len(id_mapping),
        "mapping": id_mapping,
    }

    if not dry_run:
        queue["generated_at"] = now_iso()
        history["generated_at"] = now_iso()
        decisions["generated_at"] = now_iso()
        write_json(queue_path, queue)
        write_json(history_path, history)
        write_json(decisions_path, decisions)
        write_json(mapping_path, mapping_payload)

    return mapping_payload
