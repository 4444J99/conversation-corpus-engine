from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .sharded_collection import collection_storage_path, load_corpus_collection

SCHEMA_CATALOG = {
    "commercial-awareness": {
        "filename": "commercial-awareness.schema.json",
        "description": "Commercial awareness bridge between CCE export surfaces and pipeline consumers.",
    },
    "corpus-contract": {
        "filename": "corpus-contract.schema.json",
        "description": "Canonical corpus contract manifest emitted under corpus/contract.json.",
    },
    "import-audit": {
        "filename": "import-audit.schema.json",
        "description": "Per-thread import audit records emitted under corpus/import-audit.json.",
    },
    "near-duplicates": {
        "filename": "near-duplicates.schema.json",
        "description": "Near-duplicate thread candidates emitted under corpus/near-duplicates.json.",
    },
    "source-policy": {
        "filename": "source-policy.schema.json",
        "description": "Per-provider source authority policy under state/source-policies/<provider>.json.",
    },
    "sharded-collection": {
        "filename": "sharded-collection.schema.json",
        "description": "Immutable generation manifest for a cce.sharded_collection.v1 collection.",
    },
    "promotion-policy": {
        "filename": "promotion-policy.schema.json",
        "description": "Live promotion thresholds under promotion-policy.json.",
    },
    "corpus-candidate": {
        "filename": "corpus-candidate.schema.json",
        "description": "Corpus candidate manifest emitted by the candidate workflow.",
    },
    "provider-refresh": {
        "filename": "provider-refresh.schema.json",
        "description": "Provider refresh run payload emitted by the refresh workflow.",
    },
    "surface-manifest": {
        "filename": "surface-manifest.schema.json",
        "description": "Engine-facing externalization manifest for Meta integration.",
    },
    "mcp-context": {
        "filename": "mcp-context.schema.json",
        "description": "MCP-facing project context payload for external consumers.",
    },
    "surface-bundle": {
        "filename": "surface-bundle.schema.json",
        "description": "Bundle of exported surface artifacts plus validation results.",
    },
}


def schema_dir() -> Path:
    return Path(__file__).resolve().parent / "schemas"


def schema_path(schema_name: str) -> Path:
    try:
        entry = SCHEMA_CATALOG[schema_name]
    except KeyError as exc:
        raise ValueError(f"Unknown schema: {schema_name}") from exc
    return schema_dir() / entry["filename"]


def list_schemas() -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for name in sorted(SCHEMA_CATALOG):
        entry = SCHEMA_CATALOG[name]
        items.append(
            {
                "name": name,
                "description": entry["description"],
                "path": str(schema_path(name)),
            },
        )
    return items


def load_schema(schema_name: str) -> dict[str, Any]:
    path = schema_path(schema_name)
    return json.loads(path.read_text(encoding="utf-8"))


def expected_types(schema: dict[str, Any]) -> list[str]:
    value = schema.get("type")
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def type_matches(value: Any, expected_type: str) -> bool:
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "null":
        return value is None
    return True


def join_path(base_path: str, segment: str) -> str:
    if base_path == "$":
        return f"$.{segment}"
    return f"{base_path}.{segment}"


def validate_instance(
    instance: Any, schema: dict[str, Any], *, path: str = "$"
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    types = expected_types(schema)
    if types and not any(type_matches(instance, item) for item in types):
        issues.append(
            {
                "path": path,
                "message": f"Expected type {types}, got {type(instance).__name__}.",
            },
        )
        return issues

    if "const" in schema and instance != schema["const"]:
        issues.append(
            {
                "path": path,
                "message": f"Expected constant value {schema['const']!r}.",
            },
        )
        return issues

    if "enum" in schema and instance not in schema["enum"]:
        issues.append(
            {
                "path": path,
                "message": f"Expected one of {schema['enum']!r}.",
            },
        )
        return issues

    if isinstance(instance, dict):
        required = schema.get("required") or []
        for key in required:
            if key not in instance:
                issues.append(
                    {
                        "path": join_path(path, key),
                        "message": "Missing required property.",
                    },
                )
        for key, subschema in (schema.get("properties") or {}).items():
            if key not in instance:
                continue
            issues.extend(validate_instance(instance[key], subschema, path=join_path(path, key)))
        return issues

    if isinstance(instance, list):
        item_schema = schema.get("items")
        if not isinstance(item_schema, dict):
            return issues
        for index, item in enumerate(instance):
            issues.extend(validate_instance(item, item_schema, path=f"{path}[{index}]"))
        return issues

    return issues


def validate_payload(schema_name: str, payload: Any) -> dict[str, Any]:
    schema = load_schema(schema_name)
    issues = validate_instance(payload, schema)
    return {
        "schema": schema_name,
        "schema_path": str(schema_path(schema_name)),
        "valid": not issues,
        "error_count": len(issues),
        "errors": issues,
    }


def validate_json_file(schema_name: str, path: Path) -> dict[str, Any]:
    resolved_path = path.resolve(strict=False)
    if collection_storage_path(resolved_path).exists():
        payload = load_corpus_collection(resolved_path, default=[])
    else:
        payload = json.loads(resolved_path.read_text(encoding="utf-8"))
    result = validate_payload(schema_name, payload)
    result["path"] = str(resolved_path)
    return result
