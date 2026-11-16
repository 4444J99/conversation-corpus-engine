from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from .corpus_store import authorize_corpus_write
from .federation import upsert_corpus, validate_corpus_root
from .schema_validation import validate_payload
from .sharded_collection import (
    collection_current_path,
    collection_storage_path,
    content_hash_key,
    load_collection,
    load_corpus_collection,
    merge_entity_records,
    merge_ledger_records,
    write_collection,
    write_corpus_collection,
)

MIGRATABLE_CORPUS_COLLECTIONS = (
    "threads-index.json",
    "semantic-v3-index.json",
    "pairs-index.json",
    "doctrine-briefs.json",
    "family-dossiers.json",
    "canonical-families.json",
    "action-ledger.json",
    "unresolved-ledger.json",
    "canonical-entities.json",
    "entity-aliases.json",
    "doctrine-timeline.json",
    "import-audit.json",
    "near-duplicates.json",
)
V2_CONTRACT_NAME = "conversation-corpus-engine.v2"
V2_CONTRACT_VERSION = 2


def discover_staging_corpora(staging_root: Path) -> list[Path]:
    staging_root = staging_root.resolve()
    if not staging_root.exists():
        return []

    corpora: list[Path] = []
    for child in sorted(staging_root.iterdir()):
        if not child.is_dir():
            continue
        if validate_corpus_root(child)["valid"]:
            corpora.append(child.resolve())
    return corpora


def seed_registry_from_staging(
    project_root: Path,
    staging_root: Path,
    *,
    prefer_default: str = "chatgpt-history",
) -> dict[str, object]:
    discovered = discover_staging_corpora(staging_root)
    registered: list[dict[str, str]] = []

    for corpus_root in discovered:
        corpus_id = corpus_root.name
        entry = upsert_corpus(
            project_root,
            corpus_root,
            corpus_id=corpus_id,
            name=corpus_root.name.replace("-", " ").title(),
            make_default=corpus_root.name == prefer_default,
        )
        registered.append(
            {
                "corpus_id": entry["corpus_id"],
                "name": entry["name"],
                "root": entry["root"],
                "default": str(bool(entry.get("default"))).lower(),
            },
        )

    return {
        "staging_root": str(staging_root.resolve()),
        "registered_count": len(registered),
        "registered": registered,
    }


def _copy_non_collection_files(source_root: Path, destination_root: Path) -> list[str]:
    copied: list[str] = []
    excluded_corpus_files = {*MIGRATABLE_CORPUS_COLLECTIONS, "contract.json"}
    for path in sorted(source_root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(source_root)
        if any(parent.name.endswith(".collection") for parent in path.parents):
            continue
        if relative.parts[:1] == ("corpus",) and path.name in excluded_corpus_files:
            continue
        if relative.parts[:1] == ("source",) and path.name == "conversations.json":
            continue
        if relative.as_posix() == "import-manifest.json":
            continue
        destination = destination_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        copied.append(relative.as_posix())
    return copied


def _source_conversation_key(record: Any) -> str:
    if isinstance(record, dict):
        for field in ("conversation_id", "uuid", "id"):
            value = record.get(field)
            if value:
                return str(value)
    return content_hash_key(record)


def _merge_alias_records(records: list[Any]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Legacy entity alias records must be JSON objects.")
        key = str(record.get("canonical_entity_id") or record.get("canonical_label") or "")
        if not key:
            raise ValueError("Legacy entity alias record is missing a stable identity.")
        existing = merged.get(key)
        if existing is None:
            merged[key] = {
                **record,
                "labels": list(record.get("labels") or []),
            }
            continue
        existing["labels"] = list(
            dict.fromkeys([*existing["labels"], *(record.get("labels") or [])])
        )
    return list(merged.values())


def _normalize_legacy_collection(filename: str, records: list[Any]) -> list[Any]:
    if filename == "action-ledger.json":
        return merge_ledger_records(
            records,
            key_field="action_key",
            text_field="canonical_action",
        )
    if filename == "unresolved-ledger.json":
        return merge_ledger_records(
            records,
            key_field="question_key",
            text_field="canonical_question",
        )
    if filename == "canonical-entities.json":
        return merge_entity_records(records)
    if filename == "entity-aliases.json":
        return _merge_alias_records(records)
    return records


def migrate_corpus_v2(
    *,
    project_root: Path,
    source_root: Path,
    destination_root: Path,
    corpus_store_root: Path | None = None,
    write: bool = False,
) -> dict[str, Any]:
    source_root = source_root.resolve(strict=True)
    destination_root = destination_root.resolve(strict=False)
    validation = validate_corpus_root(source_root)
    if not validation["valid"]:
        missing = ", ".join(validation["missing_files"])
        raise ValueError(f"Source corpus is missing required collections: {missing}")
    if validation.get("contract_name") != "conversation-corpus-engine-v1":
        raise ValueError("corpus-v2 migration requires a conversation-corpus-engine-v1 source.")

    source_corpus = source_root / "corpus"
    available: dict[str, list[Any]] = {}
    for filename in MIGRATABLE_CORPUS_COLLECTIONS:
        logical_path = source_corpus / filename
        if collection_storage_path(logical_path).exists():
            raise ValueError(
                "corpus-v2 migration requires a v1 source without an existing v2 generation: "
                f"{logical_path}"
            )
        if logical_path.is_file():
            records = load_corpus_collection(logical_path, default=[]) or []
            available[filename] = _normalize_legacy_collection(filename, records)

    plan: dict[str, Any] = {
        "migration": "conversation-corpus-engine.v1-to-v2",
        "mode": "write" if write else "read-only",
        "source_root": str(source_root),
        "destination_root": str(destination_root),
        "collection_counts": {filename: len(records) for filename, records in available.items()},
        "collection_count": len(available),
        "source_unchanged": True,
    }
    if not write:
        return plan

    authorization = authorize_corpus_write(
        project_root=project_root,
        corpus_store_root=corpus_store_root,
        destination=destination_root,
        source_roots=(source_root,),
    )
    destination_root = authorization.destination
    destination_corpus = destination_root / "corpus"
    destination_corpus.mkdir(parents=True, exist_ok=True)
    copied_files = _copy_non_collection_files(source_root, destination_root)

    generations: dict[str, str] = {}
    for filename, records in available.items():
        logical_path = destination_corpus / filename
        if logical_path.exists():
            raise ValueError(f"Destination contains a legacy collection file: {logical_path}")
        manifest = write_corpus_collection(
            logical_path,
            records,
            authorization=authorization,
        )
        generations[filename] = manifest["generation_id"]

    source_directory = source_root / "source"
    source_conversation_files = (
        sorted(source_directory.rglob("conversations.json")) if source_directory.is_dir() else []
    )
    for source_conversations in source_conversation_files:
        relative = source_conversations.relative_to(source_root)
        destination_conversations = destination_root / relative
        manifest = write_collection(
            destination_conversations,
            load_collection(source_conversations, default=[]),
            key=_source_conversation_key,
            authorization=authorization,
        )
        generations[relative.as_posix()] = manifest["generation_id"]

    source_import_manifest = source_root / "import-manifest.json"
    if source_import_manifest.is_file():
        manifest = write_corpus_collection(
            destination_root / "import-manifest.json",
            load_corpus_collection(source_import_manifest, default=[]),
            authorization=authorization,
        )
        generations["import-manifest.json"] = manifest["generation_id"]

    source_contract_path = source_corpus / "contract.json"
    source_contract = (
        json.loads(source_contract_path.read_text(encoding="utf-8"))
        if source_contract_path.is_file()
        else {}
    )
    counts = dict(source_contract.get("counts") or {})
    for count_name, filename in {
        "threads": "threads-index.json",
        "families": "canonical-families.json",
        "pairs": "pairs-index.json",
        "actions": "action-ledger.json",
        "unresolved": "unresolved-ledger.json",
        "entities": "canonical-entities.json",
        "near_duplicates": "near-duplicates.json",
    }.items():
        if filename in available:
            counts[count_name] = len(available[filename])
    contract = {
        **source_contract,
        "contract_name": V2_CONTRACT_NAME,
        "contract_version": V2_CONTRACT_VERSION,
        "required_files": [
            f"corpus/{Path(filename).with_suffix('.collection').name}"
            for filename in MIGRATABLE_CORPUS_COLLECTIONS[:5]
        ],
        "counts": counts,
    }
    contract_validation = validate_payload("corpus-contract", contract)
    if not contract_validation["valid"]:
        raise ValueError(
            "Migrated corpus contract is invalid: "
            + "; ".join(item["message"] for item in contract_validation["errors"])
        )
    contract_path = destination_corpus / "contract.json"
    contract_path.write_text(json.dumps(contract, indent=2) + "\n", encoding="utf-8")

    plan.update(
        {
            "destination_root": str(destination_root),
            "copied_file_count": len(copied_files),
            "generation_ids": generations,
            "contract_validation": contract_validation,
            "current_pointers": {
                filename: collection_current_path(destination_corpus / filename)
                .read_text(encoding="utf-8")
                .strip()
                for filename in available
            },
        }
    )
    return plan
