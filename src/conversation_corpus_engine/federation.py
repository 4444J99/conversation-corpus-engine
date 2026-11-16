#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import archive_layout
from .answering import (
    STOP_WORDS,
    build_answer,
    lexical_support_for_tokens,
    load_json,
    render_answer_text,
    search_documents_v4,
    shorten,
    slugify,
    tokenize,
    write_json,
    write_markdown,
)
from .federated_canon import build_federated_canon
from .paths import default_project_root, resolve_workspace_path
from .sharded_collection import (
    collection_exists,
    collection_storage_path,
    load_corpus_collection,
    write_corpus_collection,
)
from .source_lifecycle import compute_source_freshness

DEFAULT_PROJECT_ROOT = default_project_root()
FEDERATION_CONTRACT = "conversation-corpus-engine.v2"
REGISTRY_VERSION = 2
REQUIRED_CONTRACT_FILES = (
    "corpus/threads-index.json",
    "corpus/semantic-v3-index.json",
    "corpus/pairs-index.json",
    "corpus/doctrine-briefs.json",
    "corpus/family-dossiers.json",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def registry_path(root: Path) -> Path:
    return root / "state" / "federation-registry.json"


def federation_dir(root: Path) -> Path:
    return root / "federation"


def federation_report_path(root: Path) -> Path:
    return federation_dir(root) / "federation-summary.md"


def answers_dir(root: Path) -> Path:
    return root / "reports" / "federation-answers"


def validate_corpus_root(corpus_root: Path) -> dict[str, Any]:
    corpus_root = resolve_workspace_path(corpus_root)
    missing = [
        relative
        for relative in REQUIRED_CONTRACT_FILES
        if not collection_exists(corpus_root / relative)
    ]
    contract_manifest = load_json(corpus_root / "corpus" / "contract.json", default={}) or {}
    return {
        "valid": not missing,
        "missing_files": missing,
        "contract_manifest_present": bool(contract_manifest),
        "contract_name": contract_manifest.get("contract_name"),
        "contract_version": contract_manifest.get("contract_version"),
    }


def default_registry_entry(project_root: Path) -> dict[str, Any]:
    validation = validate_corpus_root(project_root)
    return {
        "corpus_id": "primary-corpus",
        "name": "Primary Corpus",
        "root": str(project_root),
        "contract": validation.get("contract_name") or FEDERATION_CONTRACT,
        "status": "active",
        "default": True,
        "registered_at": now_iso(),
    }


def bootstrap_registry(project_root: Path) -> dict[str, Any]:
    path = registry_path(project_root)
    if path.exists():
        return load_json(path, default={}) or {}
    path.parent.mkdir(parents=True, exist_ok=True)
    validation = validate_corpus_root(project_root)
    corpora = [default_registry_entry(project_root)] if validation["valid"] else []
    payload = {
        "generated_at": now_iso(),
        "registry_version": REGISTRY_VERSION,
        "corpora": corpora,
    }
    write_json(path, payload)
    return payload


def normalize_corpus_id(value: str) -> str:
    return slugify(value, limit=80)


def load_registry(project_root: Path) -> dict[str, Any]:
    registry = bootstrap_registry(project_root)
    if registry.get("registry_version") != REGISTRY_VERSION:
        registry["registry_version"] = REGISTRY_VERSION
    registry.setdefault("corpora", [])
    return registry


def save_registry(project_root: Path, registry: dict[str, Any]) -> dict[str, Any]:
    registry["registry_version"] = REGISTRY_VERSION
    registry["generated_at"] = now_iso()
    active = [
        entry for entry in registry.get("corpora", []) if entry.get("status", "active") == "active"
    ]
    if active and not any(entry.get("default") for entry in active):
        active[0]["default"] = True
    if sum(1 for entry in active if entry.get("default")) > 1:
        seen_default = False
        for entry in registry["corpora"]:
            if entry.get("status", "active") != "active":
                entry["default"] = False
                continue
            if entry.get("default") and not seen_default:
                seen_default = True
                continue
            if seen_default:
                entry["default"] = False
    write_json(registry_path(project_root), registry)
    return registry


def list_registered_corpora(
    project_root: Path, *, active_only: bool = False
) -> list[dict[str, Any]]:
    registry = load_registry(project_root)
    corpora = registry.get("corpora", [])
    if active_only:
        corpora = [entry for entry in corpora if entry.get("status", "active") == "active"]
    return corpora


def upsert_corpus(
    project_root: Path,
    corpus_root: Path,
    *,
    corpus_id: str | None = None,
    name: str | None = None,
    contract: str | None = None,
    status: str = "active",
    make_default: bool = False,
) -> dict[str, Any]:
    corpus_root = resolve_workspace_path(corpus_root)
    validation = validate_corpus_root(corpus_root)
    if not validation["valid"]:
        missing = ", ".join(validation["missing_files"])
        raise ValueError(f"Corpus root {corpus_root} is missing required contract files: {missing}")

    registry = load_registry(project_root)
    resolved_contract = contract or validation.get("contract_name") or FEDERATION_CONTRACT
    resolved_id = normalize_corpus_id(corpus_id or corpus_root.name)
    existing = next(
        (entry for entry in registry["corpora"] if entry["corpus_id"] == resolved_id), None
    )
    if existing:
        entry = existing
        entry["name"] = name or entry.get("name") or corpus_root.name
        entry["root"] = str(corpus_root)
        entry["contract"] = resolved_contract
        entry["status"] = status
    else:
        entry = {
            "corpus_id": resolved_id,
            "name": name or corpus_root.name,
            "root": str(corpus_root),
            "contract": resolved_contract,
            "status": status,
            "default": False,
            "registered_at": now_iso(),
        }
        registry["corpora"].append(entry)
    if make_default:
        for item in registry["corpora"]:
            item["default"] = (
                item["corpus_id"] == resolved_id and item.get("status", "active") == "active"
            )
    save_registry(project_root, registry)
    return entry


def remove_corpus(project_root: Path, corpus_id: str) -> dict[str, Any]:
    registry = load_registry(project_root)
    before = len(registry["corpora"])
    registry["corpora"] = [
        entry for entry in registry["corpora"] if entry["corpus_id"] != corpus_id
    ]
    if len(registry["corpora"]) == before:
        raise KeyError(corpus_id)
    save_registry(project_root, registry)
    return registry


def load_corpus_surface(entry: dict[str, Any]) -> dict[str, Any]:
    root = resolve_workspace_path(Path(entry["root"]))
    corpus_dir = root / "corpus"
    threads = load_corpus_collection(corpus_dir / "threads-index.json", default=[]) or []
    doctrine_briefs = load_corpus_collection(corpus_dir / "doctrine-briefs.json", default=[]) or []
    family_dossiers = load_corpus_collection(corpus_dir / "family-dossiers.json", default=[]) or []
    families = load_corpus_collection(corpus_dir / "canonical-families.json", default=[]) or []
    actions = load_corpus_collection(corpus_dir / "action-ledger.json", default=[]) or []
    unresolved = load_corpus_collection(corpus_dir / "unresolved-ledger.json", default=[]) or []
    entities = load_corpus_collection(corpus_dir / "canonical-entities.json", default=[]) or []
    contract_manifest = load_json(root / "corpus" / "contract.json", default={}) or {}
    evaluation = load_json(root / "corpus" / "evaluation-summary.json", default={}) or {}
    gates = load_json(root / "corpus" / "regression-gates.json", default={}) or {}
    validation = validate_corpus_root(root)
    freshness = compute_source_freshness(root)
    summary = {
        "corpus_id": entry["corpus_id"],
        "name": entry["name"],
        "root": str(root),
        "contract": entry.get("contract", FEDERATION_CONTRACT),
        "status": entry.get("status", "active"),
        "default": bool(entry.get("default")),
        "thread_count": len(threads),
        "family_count": len(families),
        "action_count": len(actions),
        "unresolved_count": len(unresolved),
        "entity_count": len(entities),
        "adapter_type": contract_manifest.get("adapter_type")
        or entry.get("adapter_type")
        or "unknown",
        "evaluation_overall_state": gates.get("overall_state")
        or (evaluation.get("regression_gates") or {}).get("overall_state"),
        "source_reliability_state": gates.get("source_reliability_state"),
        "source_freshness_state": freshness.get("state"),
        "source_freshness_note": freshness.get("note"),
        "source_needs_refresh": freshness.get("needs_refresh", False),
        "valid_contract": validation["valid"],
        "missing_files": validation["missing_files"],
        "contract_manifest_present": validation.get("contract_manifest_present", False),
        "contract_name": validation.get("contract_name"),
        "contract_version": validation.get("contract_version"),
    }
    return {
        "entry": entry,
        "summary": summary,
        "threads": threads,
        "doctrine_briefs": doctrine_briefs,
        "family_dossiers": family_dossiers,
        "families": families,
        "actions": actions,
        "unresolved": unresolved,
        "entities": entities,
        "contract_manifest": contract_manifest,
        "evaluation": evaluation,
        "gates": gates,
    }


class ArchiveManifestChanged(RuntimeError):
    """A federation build changed the archive manifest, which it must only ever read around."""


def archive_manifest_fingerprint(archive_root: Path) -> dict[str, str | None]:
    """The archive manifest's recorded source fingerprint and the sha256 of the whole file."""
    path = archive_root / archive_layout.MANIFEST_FILENAME
    if not path.is_file():
        raise FileNotFoundError(f"No archive manifest at {path}")
    raw = path.read_bytes()
    recorded = json.loads(raw.decode("utf-8")).get("source_fingerprint")
    return {"source_fingerprint": recorded, "file_sha256": hashlib.sha256(raw).hexdigest()}


def build_federation(project_root: Path, *, archive_root: Path | None = None) -> dict[str, Any]:
    """Materialize the federation.

    With `archive_root`, the archive manifest fingerprint is recorded before and after the build
    and the build fails if it moved: federation reads the projected corpora and must never touch
    the archive they came from.
    """
    before = archive_manifest_fingerprint(archive_root) if archive_root is not None else None
    result = _build_federation(project_root)
    if archive_root is not None:
        after = archive_manifest_fingerprint(archive_root)
        if after != before:
            raise ArchiveManifestChanged(
                f"the archive manifest changed during the federation build ({before} -> {after})"
            )
        result["archive_manifest_fingerprint"] = after
    return result


def _build_federation(project_root: Path) -> dict[str, Any]:
    registry = load_registry(project_root)
    active_entries = [
        entry for entry in registry.get("corpora", []) if entry.get("status", "active") == "active"
    ]
    surfaces = [load_corpus_surface(entry) for entry in active_entries]

    corpora_summary = [surface["summary"] for surface in surfaces]
    families_index: list[dict[str, Any]] = []
    actions_index: list[dict[str, Any]] = []
    unresolved_index: list[dict[str, Any]] = []
    entities_index: list[dict[str, Any]] = []

    for surface in surfaces:
        summary = surface["summary"]
        corpus_id = summary["corpus_id"]
        for item in surface["families"]:
            families_index.append(
                {
                    "federated_family_id": f"{corpus_id}:{item.get('canonical_family_id')}",
                    "corpus_id": corpus_id,
                    "corpus_name": summary["name"],
                    "source_root": summary["root"],
                    "canonical_family_id": item.get("canonical_family_id"),
                    "canonical_title": item.get("canonical_title"),
                    "canonical_thread_uid": item.get("canonical_thread_uid"),
                    "thread_uids": item.get("thread_uids") or [],
                },
            )
        for item in surface["actions"]:
            actions_index.append(
                {
                    "federated_action_id": f"{corpus_id}:{item.get('action_key')}",
                    "corpus_id": corpus_id,
                    "corpus_name": summary["name"],
                    "source_root": summary["root"],
                    "action_key": item.get("action_key"),
                    "canonical_action": item.get("canonical_action"),
                    "status": item.get("status"),
                    "family_ids": item.get("family_ids") or [],
                    "thread_uids": item.get("thread_uids") or [],
                    "occurrence_count": item.get("occurrence_count", 0),
                },
            )
        for item in surface["unresolved"]:
            unresolved_index.append(
                {
                    "federated_question_id": f"{corpus_id}:{item.get('question_key')}",
                    "corpus_id": corpus_id,
                    "corpus_name": summary["name"],
                    "source_root": summary["root"],
                    "question_key": item.get("question_key"),
                    "canonical_question": item.get("canonical_question"),
                    "why_unresolved": item.get("why_unresolved"),
                    "family_ids": item.get("family_ids") or [],
                    "thread_uids": item.get("thread_uids") or [],
                    "occurrence_count": item.get("occurrence_count", 0),
                },
            )
        for item in surface["entities"]:
            entities_index.append(
                {
                    "federated_entity_id": f"{corpus_id}:{item.get('canonical_entity_id') or item.get('canonical_label')}",
                    "corpus_id": corpus_id,
                    "corpus_name": summary["name"],
                    "source_root": summary["root"],
                    "canonical_entity_id": item.get("canonical_entity_id"),
                    "canonical_label": item.get("canonical_label"),
                    "entity_type": item.get("entity_type"),
                    "aliases": item.get("aliases") or [],
                },
            )

    canon_outputs = build_federated_canon(project_root, surfaces)
    for item in corpora_summary:
        contract_manifest = (
            load_json(Path(item["root"]) / "corpus" / "contract.json", default={}) or {}
        )
        item["adapter_type"] = (
            contract_manifest.get("adapter_type") or item.get("adapter_type") or "unknown"
        )
        item["contract_name"] = contract_manifest.get("contract_name") or item.get("contract_name")
        item["contract_version"] = contract_manifest.get("contract_version") or item.get(
            "contract_version"
        )
        item["contract_manifest_present"] = bool(contract_manifest) or item.get(
            "contract_manifest_present", False
        )
    evaluation_summary = {
        "generated_at": now_iso(),
        "corpus_count": len(corpora_summary),
        "healthy_corpus_count": sum(
            1 for item in corpora_summary if item.get("evaluation_overall_state") == "pass"
        ),
        "managed_corpus_count": sum(
            1
            for item in corpora_summary
            if item.get("source_freshness_state") not in {"not_applicable", None}
        ),
        "fresh_corpus_count": sum(
            1 for item in corpora_summary if item.get("source_freshness_state") == "fresh"
        ),
        "stale_corpus_count": sum(
            1 for item in corpora_summary if item.get("source_freshness_state") == "stale"
        ),
        "snapshot_missing_count": sum(
            1
            for item in corpora_summary
            if item.get("source_freshness_state") == "missing_snapshot"
        ),
        "missing_source_count": sum(
            1 for item in corpora_summary if item.get("source_freshness_state") == "missing_source"
        ),
        "aggregate": {
            "thread_count": sum(item.get("thread_count", 0) for item in corpora_summary),
            "family_count": sum(item.get("family_count", 0) for item in corpora_summary),
            "action_count": sum(item.get("action_count", 0) for item in corpora_summary),
            "unresolved_count": sum(item.get("unresolved_count", 0) for item in corpora_summary),
            "entity_count": sum(item.get("entity_count", 0) for item in corpora_summary),
        },
        "corpora": corpora_summary,
    }

    fed_dir = federation_dir(project_root)
    fed_dir.mkdir(parents=True, exist_ok=True)
    write_json(fed_dir / "registry.json", registry)
    collections = {
        "corpora-summary.json": corpora_summary,
        "families-index.json": families_index,
        "actions-index.json": actions_index,
        "unresolved-index.json": unresolved_index,
        "entities-index.json": entities_index,
    }
    for filename, records in collections.items():
        write_corpus_collection(fed_dir / filename, records)
    write_json(fed_dir / "evaluation-summary.json", evaluation_summary)
    write_markdown(
        federation_report_path(project_root),
        render_federation_summary(corpora_summary, evaluation_summary),
    )
    return {
        "registry_path": str(fed_dir / "registry.json"),
        "corpora_summary_path": str(collection_storage_path(fed_dir / "corpora-summary.json")),
        "families_index_path": str(collection_storage_path(fed_dir / "families-index.json")),
        "actions_index_path": str(collection_storage_path(fed_dir / "actions-index.json")),
        "unresolved_index_path": str(collection_storage_path(fed_dir / "unresolved-index.json")),
        "entities_index_path": str(collection_storage_path(fed_dir / "entities-index.json")),
        "evaluation_summary_path": str(fed_dir / "evaluation-summary.json"),
        "summary_markdown_path": str(federation_report_path(project_root)),
        **canon_outputs,
    }


def render_federation_summary(
    corpora_summary: list[dict[str, Any]], evaluation_summary: dict[str, Any]
) -> str:
    lines = [
        "# Conversation Corpus Federation Summary",
        "",
        f"- Generated: {evaluation_summary.get('generated_at')}",
        f"- Corpus count: {evaluation_summary.get('corpus_count', 0)}",
        f"- Healthy corpus count: {evaluation_summary.get('healthy_corpus_count', 0)}",
        f"- Managed corpus count: {evaluation_summary.get('managed_corpus_count', 0)}",
        f"- Fresh corpus count: {evaluation_summary.get('fresh_corpus_count', 0)}",
        f"- Stale corpus count: {evaluation_summary.get('stale_corpus_count', 0)}",
        f"- Missing snapshot count: {evaluation_summary.get('snapshot_missing_count', 0)}",
        f"- Missing source count: {evaluation_summary.get('missing_source_count', 0)}",
        f"- Thread count: {(evaluation_summary.get('aggregate') or {}).get('thread_count', 0)}",
        f"- Family count: {(evaluation_summary.get('aggregate') or {}).get('family_count', 0)}",
        f"- Action count: {(evaluation_summary.get('aggregate') or {}).get('action_count', 0)}",
        f"- Unresolved count: {(evaluation_summary.get('aggregate') or {}).get('unresolved_count', 0)}",
        f"- Entity count: {(evaluation_summary.get('aggregate') or {}).get('entity_count', 0)}",
        "",
        "## Corpora",
        "",
    ]
    if not corpora_summary:
        lines.append("No registered corpora.")
        return "\n".join(lines)
    for item in corpora_summary:
        lines.append(
            "- "
            + f"{item.get('name')} ({item.get('corpus_id')}): "
            + f"threads={item.get('thread_count', 0)}, "
            + f"families={item.get('family_count', 0)}, "
            + f"actions={item.get('action_count', 0)}, "
            + f"unresolved={item.get('unresolved_count', 0)}, "
            + f"entities={item.get('entity_count', 0)}, "
            + f"adapter={item.get('adapter_type') or 'unknown'}, "
            + f"gate={item.get('evaluation_overall_state') or 'unknown'}, "
            + f"freshness={item.get('source_freshness_state') or 'unknown'}, "
            + f"default={item.get('default')}",
        )
        if item.get("missing_files"):
            lines.append(f"  missing={', '.join(item['missing_files'])}")
        if item.get("source_freshness_note") and item.get("source_freshness_state") not in {
            "fresh",
            "not_applicable",
        }:
            lines.append(f"  freshness_note={item['source_freshness_note']}")
    return "\n".join(lines).rstrip()


def ensure_federation(project_root: Path) -> None:
    fed_dir = federation_dir(project_root)
    collection_names = (
        "corpora-summary.json",
        "families-index.json",
        "actions-index.json",
        "unresolved-index.json",
        "entities-index.json",
    )
    if (
        not all(collection_exists(fed_dir / name) for name in collection_names)
        or not (fed_dir / "evaluation-summary.json").exists()
    ):
        build_federation(project_root)


def load_federation_index(project_root: Path, ledger: str) -> Any:
    ensure_federation(project_root)
    mapping = {
        "corpora": "corpora-summary.json",
        "families": "families-index.json",
        "actions": "actions-index.json",
        "unresolved": "unresolved-index.json",
        "entities": "entities-index.json",
        "evaluation": "evaluation-summary.json",
        "canonical-families": "canonical-families.json",
        "canonical-actions": "canonical-actions.json",
        "canonical-unresolved": "canonical-unresolved.json",
        "canonical-entities": "canonical-entities.json",
        "doctrine-briefs": "doctrine-briefs.json",
        "entity-dossiers": "entity-dossiers.json",
        "project-dossiers": "project-dossiers.json",
        "lineage": "lineage-map.json",
        "conflicts": "conflict-report.json",
        "review-queue": "../state/federated-review-queue.json",
        "review-history": "../state/federated-review-history.json",
        "evaluation-gates": "evaluation-gates.json",
        "evaluation-scorecard": "evaluation-scorecard.json",
    }
    dict_ledgers = {
        "evaluation",
        "conflicts",
        "review-queue",
        "review-history",
        "evaluation-gates",
        "evaluation-scorecard",
    }
    default = {} if ledger in dict_ledgers else []
    logical_path = federation_dir(project_root) / mapping[ledger]
    if ledger not in dict_ledgers:
        return load_corpus_collection(logical_path, default=default) or default
    return load_json(logical_path, default=default) or default


def query_federation_index(
    project_root: Path,
    *,
    ledger: str,
    text: str | None = None,
    corpus_id: str | None = None,
    limit: int = 20,
) -> Any:
    payload = load_federation_index(project_root, ledger)
    if ledger in {"evaluation", "evaluation-gates", "evaluation-scorecard"}:
        return payload
    if ledger == "review-queue":
        entries = payload.get("items", []) if isinstance(payload, dict) else []
        if corpus_id:
            entries = [
                entry for entry in entries if corpus_id in (entry.get("source_corpora") or [])
            ]
        if text:
            lowered = text.lower()
            entries = [
                entry
                for entry in entries
                if lowered in " ".join(str(value) for value in entry.values()).lower()
            ]
        return entries[:limit]
    if ledger == "review-history":
        entries = payload.get("items", []) if isinstance(payload, dict) else []
        if corpus_id:
            entries = [
                entry for entry in entries if corpus_id in (entry.get("source_corpora") or [])
            ]
        if text:
            lowered = text.lower()
            entries = [
                entry
                for entry in entries
                if lowered in " ".join(str(value) for value in entry.values()).lower()
            ]
        return entries[:limit]
    if ledger == "conflicts":
        entries = payload.get("potential_conflicts", []) if isinstance(payload, dict) else []
        if corpus_id:
            entries = [
                entry for entry in entries if corpus_id in (entry.get("source_corpora") or [])
            ]
        if text:
            lowered = text.lower()
            entries = [
                entry
                for entry in entries
                if lowered in " ".join(str(value) for value in entry.values()).lower()
            ]
        return entries[:limit]
    entries = payload
    if corpus_id:
        entries = [entry for entry in entries if entry.get("corpus_id") == corpus_id]
    if text:
        lower_text = text.lower()
        filtered: list[dict[str, Any]] = []
        for entry in entries:
            haystack = " ".join(str(value) for value in entry.values()).lower()
            if lower_text in haystack:
                filtered.append(entry)
        entries = filtered
    return entries[:limit]


def corpus_gate_state(project_root: Path, corpus_root: Path) -> str | None:
    gates = load_json(corpus_root / "corpus" / "regression-gates.json", default={}) or {}
    return gates.get("overall_state")


def exact_family_title_alignment_bonus(query: str, retrieval: dict[str, Any]) -> float:
    lower_query = query.lower().strip()
    if not lower_query:
        return 0.0
    family_hits = retrieval.get("family_hits") or []
    for index, item in enumerate(family_hits[:3]):
        title = (item.get("title") or "").lower().strip()
        if title != lower_query:
            continue
        return 8.0 if index == 0 else 6.0
    top_thread = (retrieval.get("thread_hits") or [None])[0] or {}
    if (top_thread.get("title") or "").lower().strip() == lower_query:
        return 2.5
    return 0.0


def federated_score(
    entry: dict[str, Any], retrieval: dict[str, Any], project_root: Path, *, query: str
) -> float:
    top_hit = (retrieval.get("hits") or [None])[0]
    score = float((top_hit or {}).get("score", 0.0))
    score += exact_family_title_alignment_bonus(query, retrieval)
    if entry.get("default"):
        score += 0.05
    gate_state = corpus_gate_state(project_root, Path(entry["root"]))
    if gate_state == "pass":
        score += 0.05
    elif gate_state == "warn":
        score += 0.02
    return round(score, 4)


def corpora_from_canon_hit(hit: dict[str, Any]) -> list[str]:
    payload = hit.get("payload") or {}
    corpora: list[str] = []
    for corpus_id in payload.get("corpus_ids") or []:
        if corpus_id and corpus_id not in corpora:
            corpora.append(corpus_id)
    for item in payload.get("canonical_thread_refs") or []:
        corpus_id = item.get("corpus_id")
        if corpus_id and corpus_id not in corpora:
            corpora.append(corpus_id)
    for item in payload.get("member_families") or []:
        corpus_id = item.get("corpus_id")
        if corpus_id and corpus_id not in corpora:
            corpora.append(corpus_id)
    for corpus_id in payload.get("source_corpora") or []:
        if corpus_id and corpus_id not in corpora:
            corpora.append(corpus_id)
    return corpora


def canon_support_for_corpus(corpus_id: str, canon_hits: list[dict[str, Any]]) -> float:
    support = 0.0
    for hit in canon_hits[:3]:
        if corpus_id not in corpora_from_canon_hit(hit):
            continue
        diagnostics = hit.get("diagnostics") or {}
        coverage = diagnostics.get("coverage", 0.0)
        phrase_boost = diagnostics.get("phrase_boost", 0.0)
        if coverage >= 0.66 or phrase_boost > 0:
            support += 0.75 + (0.35 * hit.get("score", 0.0))
        elif coverage >= 0.5:
            support += 0.2 + (0.1 * hit.get("score", 0.0))
    return round(support, 4)


def search_federation(
    project_root: Path,
    query: str,
    *,
    mode: str | None = None,
    limit: int = 8,
    corpus_id: str | None = None,
) -> dict[str, Any]:
    canon_hits = search_federated_canon(project_root, query, limit=max(limit, 6), mode=mode)
    candidates: list[dict[str, Any]] = []
    for entry in list_registered_corpora(project_root, active_only=True):
        if corpus_id and entry["corpus_id"] != corpus_id:
            continue
        corpus_root = Path(entry["root"])
        retrieval = search_documents_v4(corpus_root, query, limit=max(limit, 8), mode=mode)
        top_hit = (retrieval.get("hits") or [None])[0]
        candidate = {
            "corpus_id": entry["corpus_id"],
            "name": entry["name"],
            "root": str(corpus_root),
            "default": bool(entry.get("default")),
            "gate_state": corpus_gate_state(project_root, corpus_root),
            "top_score": float((top_hit or {}).get("score", 0.0)),
            "top_hit_kind": (top_hit or {}).get("kind"),
            "top_hit_title": (top_hit or {}).get("title"),
            "retrieval": retrieval,
        }
        candidate["canon_support"] = canon_support_for_corpus(entry["corpus_id"], canon_hits)
        candidate["federated_score"] = round(
            federated_score(entry, retrieval, project_root, query=query)
            + candidate["canon_support"],
            4,
        )
        candidates.append(candidate)
    candidates.sort(
        key=lambda item: (
            item["federated_score"],
            item["top_score"],
            item["default"],
            item["corpus_id"],
        ),
        reverse=True,
    )
    return {
        "query": query,
        "mode": mode,
        "candidates": candidates,
        "selected": candidates[0] if candidates else None,
        "canon_hits": canon_hits,
    }


def build_federated_documents(project_root: Path) -> list[dict[str, Any]]:
    doctrine_briefs = load_federation_index(project_root, "doctrine-briefs") or []
    entity_dossiers = load_federation_index(project_root, "entity-dossiers") or []
    project_dossiers = load_federation_index(project_root, "project-dossiers") or []
    canonical_actions = load_federation_index(project_root, "canonical-actions") or []
    canonical_unresolved = load_federation_index(project_root, "canonical-unresolved") or []
    documents: list[dict[str, Any]] = []
    for item in doctrine_briefs:
        documents.append(
            {
                "kind": "federated_family_brief",
                "doc_id": f"federated-family:{item['federated_family_id']}",
                "title": item.get("canonical_title") or item["federated_family_id"],
                "text": item.get("search_text") or item.get("brief_text") or "",
                "citations": [f"federation/family:{item['federated_family_id']}"],
                "vector_terms": item.get("vector_terms") or {},
                "payload": item,
            },
        )
    for item in entity_dossiers:
        documents.append(
            {
                "kind": "federated_entity_dossier",
                "doc_id": f"federated-entity:{item['federated_entity_id']}",
                "title": item.get("canonical_label") or item["federated_entity_id"],
                "text": item.get("search_text") or item.get("dossier_text") or "",
                "citations": [f"federation/entity:{item['federated_entity_id']}"],
                "vector_terms": item.get("vector_terms") or {},
                "payload": item,
            },
        )
    for item in project_dossiers:
        documents.append(
            {
                "kind": "federated_project_dossier",
                "doc_id": f"federated-project:{item['project_id']}",
                "title": item.get("canonical_title") or item["project_id"],
                "text": item.get("search_text") or item.get("project_text") or "",
                "citations": [f"federation/project:{item['project_id']}"],
                "vector_terms": item.get("vector_terms") or {},
                "payload": item,
            },
        )
    for item in canonical_actions:
        documents.append(
            {
                "kind": "federated_action_cluster",
                "doc_id": f"federated-action:{item['federated_action_id']}",
                "title": item.get("canonical_action") or item["federated_action_id"],
                "text": item.get("search_text") or item.get("canonical_action") or "",
                "citations": [f"federation/action:{item['federated_action_id']}"],
                "vector_terms": item.get("vector_terms") or {},
                "payload": item,
            },
        )
    for item in canonical_unresolved:
        documents.append(
            {
                "kind": "federated_unresolved_cluster",
                "doc_id": f"federated-question:{item['federated_question_id']}",
                "title": item.get("canonical_question") or item["federated_question_id"],
                "text": item.get("search_text") or item.get("canonical_question") or "",
                "citations": [f"federation/question:{item['federated_question_id']}"],
                "vector_terms": item.get("vector_terms") or {},
                "payload": item,
            },
        )
    return documents


def search_federated_canon(
    project_root: Path, query: str, *, limit: int = 6, mode: str | None = None
) -> list[dict[str, Any]]:
    query_tokens = [
        token for token in tokenize(query) if token and token not in STOP_WORDS and len(token) > 2
    ]
    if not query_tokens:
        return []
    documents = build_federated_documents(project_root)
    if mode == "family_brief":
        documents = [item for item in documents if item["kind"] == "federated_family_brief"]
    elif mode == "action":
        documents = [item for item in documents if item["kind"] == "federated_action_cluster"]
    elif mode == "unresolved":
        documents = [item for item in documents if item["kind"] == "federated_unresolved_cluster"]
    scored: list[dict[str, Any]] = []
    for document in documents:
        lower_title = (document.get("title") or "").lower()
        lower_text = (document.get("text") or "").lower()
        vector_terms = document.get("vector_terms") or {}
        token_hits = sum(1 for token in query_tokens if token in lower_title or token in lower_text)
        coverage = token_hits / max(1, len(query_tokens))
        vector_score = sum(vector_terms.get(token, 0.0) for token in query_tokens)
        phrase_boost = 0.75 if query.lower() in lower_title or query.lower() in lower_text else 0.0
        kind_bonus = {
            "federated_family_brief": 0.7,
            "federated_project_dossier": 0.35,
            "federated_entity_dossier": 0.15,
            "federated_action_cluster": 0.25,
            "federated_unresolved_cluster": 0.2,
        }.get(document["kind"], 0.0)
        score = round((1.8 * coverage) + (0.7 * vector_score) + phrase_boost + kind_bonus, 4)
        if coverage < 0.25 and vector_score <= 0:
            continue
        if score <= 0:
            continue
        enriched = dict(document)
        enriched["score"] = score
        enriched["snippet"] = shorten(document.get("text") or "", 220)
        enriched["diagnostics"] = {
            "coverage": round(coverage, 4),
            "vector_score": round(vector_score, 4),
            "phrase_boost": round(phrase_boost, 4),
        }
        scored.append(enriched)
    scored.sort(key=lambda item: (item["score"], item["kind"], item["doc_id"]), reverse=True)
    return scored[:limit]


def prefix_citations(corpus_id: str, citations: list[str] | None) -> list[str]:
    return [f"{corpus_id}/{citation}" for citation in citations or [] if citation]


def prefix_hit_payload(corpus_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    enriched = deepcopy(payload)
    if enriched.get("doc_id"):
        enriched["doc_id"] = f"{corpus_id}:{enriched['doc_id']}"
    if enriched.get("citations") is not None:
        enriched["citations"] = prefix_citations(corpus_id, enriched.get("citations"))
    enriched["corpus_id"] = corpus_id
    return enriched


def annotate_federated_answer(
    answer: dict[str, Any],
    candidate: dict[str, Any],
    federation: dict[str, Any],
    *,
    canon_hits: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    corpus_id = candidate["corpus_id"]
    answer = deepcopy(answer)
    answer["citations"] = prefix_citations(corpus_id, answer.get("citations"))
    answer["evidence"] = [
        prefix_hit_payload(corpus_id, item) for item in answer.get("evidence", [])
    ]
    answer["top_hits"] = [
        prefix_hit_payload(corpus_id, item) for item in answer.get("top_hits", [])
    ]
    retrieval = answer.get("retrieval") or {}
    for key in ("family_hits", "thread_hits", "pair_hits"):
        retrieval[key] = [prefix_hit_payload(corpus_id, item) for item in retrieval.get(key, [])]
    answer["retrieval"] = retrieval
    answer["selected_corpus"] = {
        "corpus_id": candidate["corpus_id"],
        "name": candidate["name"],
        "root": candidate["root"],
        "gate_state": candidate.get("gate_state"),
        "federated_score": candidate.get("federated_score"),
    }
    canon_hits = canon_hits or []
    answer["federated_canon"] = {
        "hit_count": len(canon_hits),
        "hits": canon_hits,
    }
    if canon_hits:
        canon_facts = []
        for item in canon_hits[:2]:
            canon_facts.append(f"Federated canon: {item.get('title') or 'Untitled'}")
        answer["corpus_facts"] = canon_facts + list(answer.get("corpus_facts", []))
        answer["evidence"] = canon_hits[:2] + list(answer.get("evidence", []))
        answer["citations"] = list(
            dict.fromkeys(
                [citation for item in canon_hits for citation in item.get("citations", [])]
                + answer.get("citations", [])
            )
        )
    answer["federation"] = {
        "selected_corpus": answer["selected_corpus"],
        "candidate_corpora": [
            {
                "corpus_id": item["corpus_id"],
                "name": item["name"],
                "root": item["root"],
                "gate_state": item.get("gate_state"),
                "federated_score": item.get("federated_score"),
                "top_score": item.get("top_score"),
                "top_hit_kind": item.get("top_hit_kind"),
                "top_hit_title": item.get("top_hit_title"),
            }
            for item in federation.get("candidates", [])
        ],
    }
    return answer


def build_federated_answer(
    project_root: Path,
    query: str,
    *,
    mode: str | None = None,
    limit: int = 8,
    corpus_id: str | None = None,
) -> dict[str, Any]:
    federation = search_federation(project_root, query, mode=mode, limit=limit, corpus_id=corpus_id)
    selected = federation.get("selected")
    if not selected:
        return {
            "query": query,
            "answer_state": "abstain",
            "confidence": 0.0,
            "state_reason": "No registered corpora were available for federated search.",
            "answer": "No registered corpora were available for federated search.",
            "corpus_facts": [],
            "inference": [],
            "ambiguity": ["Register at least one memory corpus before querying the federation."],
            "citations": [],
            "evidence": [],
            "top_hits": [],
            "federation": {"candidate_corpora": []},
        }
    answer = build_answer(query, selected["retrieval"], mode=mode)
    canon_hits = federation.get("canon_hits") or []
    top_canon = canon_hits[0] if canon_hits else {}
    top_hit = ((selected.get("retrieval") or {}).get("hits") or [None])[0]
    raw_query_tokens = tokenize(query)
    raw_lexical_support = lexical_support_for_tokens(raw_query_tokens, top_hit) if top_hit else 0.0
    canon_coverage = (
        ((top_canon.get("diagnostics") or {}).get("coverage", 0.0)) if top_canon else 0.0
    )
    canon_phrase = (
        ((top_canon.get("diagnostics") or {}).get("phrase_boost", 0.0)) if top_canon else 0.0
    )
    canon_score = top_canon.get("score", 0.0) if top_canon else 0.0
    annotated = annotate_federated_answer(answer, selected, federation, canon_hits=canon_hits)
    if (
        annotated.get("answer_state") != "abstain"
        and raw_lexical_support < 0.34
        and canon_coverage < 0.5
        and canon_phrase <= 0.0
        and canon_score < 1.6
    ):
        annotated["answer_state"] = "abstain"
        annotated["confidence"] = 0.18
        annotated["state_reason"] = (
            "Cross-corpus evidence is too weak or generic to support a grounded federated answer."
        )
        annotated["answer"] = (
            "The federation does not have strong enough cross-corpus evidence to answer that reliably."
        )
        annotated["corpus_facts"] = []
        annotated["inference"] = []
        annotated["ambiguity"] = [
            "Closest matches are too generic or weakly grounded across corpora.",
        ]
        annotated["citations"] = []
        annotated["evidence"] = []
        annotated["top_hits"] = []
    return annotated


def render_federated_answer_text(answer: dict[str, Any]) -> str:
    selected = answer.get("selected_corpus") or {}
    candidate_corpora = (answer.get("federation") or {}).get("candidate_corpora") or []
    canon_hits = (answer.get("federated_canon") or {}).get("hits") or []
    lines = [
        "Federated Answer",
        "",
        f"Corpus: {selected.get('name') or 'n/a'} ({selected.get('corpus_id') or 'n/a'})",
        f"Gate: {selected.get('gate_state') or 'unknown'}",
        "",
        render_answer_text(answer),
        "",
        "Federated Canon",
        "",
    ]
    if canon_hits:
        for item in canon_hits[:3]:
            lines.append(
                "- "
                + f"{item.get('title') or 'Untitled'}: "
                + f"score={item.get('score', 0.0)} "
                + f"kind={item.get('kind') or 'n/a'}",
            )
    else:
        lines.append("No federated canon hits.")
    lines.extend(
        [
            "",
            "Candidate Corpora",
            "",
        ],
    )
    if candidate_corpora:
        for item in candidate_corpora[:5]:
            lines.append(
                "- "
                + f"{item.get('name')} ({item.get('corpus_id')}): "
                + f"federated_score={item.get('federated_score', 0.0)} "
                + f"top={item.get('top_score', 0.0)} "
                + f"hit={item.get('top_hit_kind') or 'n/a'}",
            )
    else:
        lines.append("No candidate corpora.")
    return "\n".join(lines).rstrip()


def save_federated_answer_dossier(project_root: Path, answer: dict[str, Any]) -> dict[str, str]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    query_slug = slugify(answer.get("query") or "query")
    report_dir = answers_dir(project_root) / datetime.now(timezone.utc).strftime("%Y-%m-%d")
    report_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = report_dir / f"{stamp}-{query_slug}.md"
    json_path = report_dir / f"{stamp}-{query_slug}.json"
    write_markdown(markdown_path, render_federated_answer_text(answer))
    write_json(json_path, answer)
    return {
        "markdown_path": str(markdown_path),
        "json_path": str(json_path),
    }


def render_federation_query_text(ledger: str, payload: Any) -> str:
    if ledger == "evaluation":
        corpora = payload.get("corpora", []) if isinstance(payload, dict) else []
        lines = [
            "Federation Evaluation",
            "",
            f"Corpus count: {payload.get('corpus_count', 0)}",
            f"Healthy corpus count: {payload.get('healthy_corpus_count', 0)}",
            "",
        ]
        if payload.get("overall_state"):
            lines.append(f"Overall state: {payload.get('overall_state')}")
            lines.append("")
        for item in corpora:
            lines.append(
                "- "
                + f"{item.get('name')} ({item.get('corpus_id')}): "
                + f"threads={item.get('thread_count', 0)} "
                + f"families={item.get('family_count', 0)} "
                + f"gate={item.get('evaluation_overall_state') or 'unknown'}",
            )
        return "\n".join(lines).rstrip()
    if ledger == "evaluation-gates":
        lines = [
            "Federation Evaluation Gates",
            "",
            f"Overall state: {payload.get('overall_state') or 'unknown'}",
            f"Source reliability: {payload.get('source_reliability_state') or 'unknown'}",
            "",
        ]
        for item in payload.get("gates", []):
            lines.append(
                "- "
                + f"{item.get('metric')}: "
                + f"state={item.get('state')} value={item.get('value')}",
            )
        return "\n".join(lines).rstrip()
    if ledger == "evaluation-scorecard":
        lines = [
            "Federation Evaluation Scorecard",
            "",
            f"Generated: {payload.get('generated_at') or 'n/a'}",
            "",
        ]
        for key, item in (payload.get("fixture_sources") or {}).items():
            lines.append(
                "- " + f"{key}: source={item.get('source')} count={item.get('count')}",
            )
        if payload.get("routing_metrics"):
            lines.append("")
            lines.append(f"Routing hit@1: {payload['routing_metrics'].get('corpus_hit_at_1')}")
        if payload.get("family_metrics"):
            lines.append(f"Family hit@1: {payload['family_metrics'].get('family_hit_at_1')}")
        if payload.get("answer_metrics"):
            lines.append(f"Answer state match: {payload['answer_metrics'].get('state_match_rate')}")
        return "\n".join(lines).rstrip()

    entries = payload if isinstance(payload, list) else []
    if not entries:
        return "No matching federation entries."
    lines: list[str] = []
    for entry in entries:
        title = (
            entry.get("name")
            or entry.get("canonical_title")
            or entry.get("canonical_action")
            or entry.get("canonical_question")
            or entry.get("canonical_label")
            or entry.get("title")
            or entry.get("corpus_id")
            or "Untitled"
        )
        lines.append(f"{title}")
        corpus_display = (
            entry.get("corpus_name")
            or entry.get("corpus_id")
            or ", ".join(entry.get("corpus_ids") or [])
            or "n/a"
        )
        lines.append(f"  corpus: {corpus_display}")
        if entry.get("source_corpora"):
            lines.append(f"  source_corpora: {', '.join(entry.get('source_corpora') or [])}")
        if entry.get("federated_family_id"):
            lines.append(f"  family: {entry['federated_family_id']}")
        if entry.get("federated_action_id"):
            lines.append(f"  action: {entry['federated_action_id']}")
        if entry.get("federated_question_id"):
            lines.append(f"  unresolved: {entry['federated_question_id']}")
        if entry.get("federated_entity_id"):
            lines.append(f"  entity: {entry['federated_entity_id']}")
        if entry.get("project_id"):
            lines.append(f"  project: {entry['project_id']}")
        if entry.get("review_id"):
            lines.append(f"  review: {entry['review_id']}")
        if entry.get("review_type"):
            lines.append(f"  type: {entry['review_type']}")
        if entry.get("root"):
            lines.append(f"  root: {entry['root']}")
        if entry.get("source_root"):
            lines.append(f"  source_root: {entry['source_root']}")
        if entry.get("adapter_type"):
            lines.append(f"  adapter: {entry['adapter_type']}")
        if entry.get("evaluation_overall_state"):
            lines.append(f"  gate: {entry['evaluation_overall_state']}")
        if entry.get("canonical_action"):
            lines.append(f"  summary: {shorten(entry['canonical_action'], 160)}")
        elif entry.get("canonical_question"):
            lines.append(f"  summary: {shorten(entry['canonical_question'], 160)}")
        elif entry.get("brief_text"):
            lines.append(f"  summary: {shorten(entry['brief_text'], 160)}")
        elif entry.get("dossier_text"):
            lines.append(f"  summary: {shorten(entry['dossier_text'], 160)}")
        elif entry.get("project_text"):
            lines.append(f"  summary: {shorten(entry['project_text'], 160)}")
        elif entry.get("canonical_title"):
            lines.append(f"  summary: {shorten(entry['canonical_title'], 160)}")
        lines.append("")
    return "\n".join(lines).rstrip()
