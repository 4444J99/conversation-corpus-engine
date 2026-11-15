from __future__ import annotations

import json
import os
import re
from importlib.resources import files
from pathlib import Path
from typing import Any

from .source_policy import load_source_policy

DEFAULT_SOURCE_DROP_ENV = "CCE_SOURCE_DROP_ROOT"
DEFAULT_PROVIDER_MANIFEST_ENV = "CCE_PROVIDER_MANIFEST"
PROVIDER_MANIFEST_VERSION = "provider-manifest.v1"
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]+$")
_ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")


class ProviderManifestError(ValueError):
    """The runtime provider manifest is incomplete or unsafe."""


def default_provider_manifest_path() -> Path:
    override = os.environ.get(DEFAULT_PROVIDER_MANIFEST_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return Path(
        str(
            files("conversation_corpus_engine").joinpath(
                "schemas/provider-manifest.organvm.v1.json"
            )
        )
    )


def _required_text(row: dict[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value or not _SAFE_ID.fullmatch(value):
        raise ProviderManifestError(f"{field} must match {_SAFE_ID.pattern}")
    return value


def _required_label(row: dict[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ProviderManifestError(f"{field} must be a non-empty string")
    return value.strip()


def _validate_blocker(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ProviderManifestError("blocker must be an object")
    blocker: dict[str, str] = {}
    for field in ("owner_reference", "failed_predicate", "next_action"):
        item = value.get(field)
        if not isinstance(item, str) or not item.strip():
            raise ProviderManifestError(f"blocker.{field} must be a non-empty string")
        blocker[field] = item.strip()
    return blocker


def _validate_provider(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ProviderManifestError("provider entries must be objects")
    provider_id = _required_text(row, "provider_id")
    aliases = row.get("source_family_aliases", [provider_id])
    if not isinstance(aliases, list) or not aliases:
        raise ProviderManifestError("source_family_aliases must be a non-empty list")
    normalized_aliases = []
    for alias in aliases:
        if not isinstance(alias, str) or not alias or not _SAFE_ID.fullmatch(alias):
            raise ProviderManifestError("source_family_aliases contains an unsafe value")
        normalized_aliases.append(alias)
    source_contract = row.get("source_contract")
    if not isinstance(source_contract, dict):
        raise ProviderManifestError("source_contract must be an object")
    if source_contract.get("kind") != "session-meta-redacted-bundle":
        raise ProviderManifestError("source_contract.kind must be session-meta-redacted-bundle")
    root_env = source_contract.get("root_env")
    if not isinstance(root_env, str) or not _ENV_KEY.fullmatch(root_env):
        raise ProviderManifestError("source_contract.root_env must be an environment key")

    inbox_rel = Path(_required_label(row, "inbox_rel"))
    if inbox_rel.is_absolute() or ".." in inbox_rel.parts:
        raise ProviderManifestError("inbox_rel must be a safe relative path")

    config: dict[str, Any] = {
        "provider_id": provider_id,
        "display_name": _required_label(row, "display_name"),
        "adapter_state": _required_text(row, "adapter_state"),
        "default_adapter_id": _required_text(row, "default_adapter_id"),
        "adapter_type": _required_text(row, "adapter_type"),
        "discovery_mode": _required_text(row, "discovery_mode"),
        "inbox_rel": inbox_rel.as_posix(),
        "default_corpus_id": _required_text(row, "default_corpus_id"),
        "default_corpus_name": _required_label(row, "default_corpus_name"),
        "source_family_aliases": sorted(set(normalized_aliases)),
        "source_contract": {
            "kind": "session-meta-redacted-bundle",
            "root_env": root_env,
        },
        "authority_policy": _required_text(row, "authority_policy"),
        "owner_reference": _required_label(row, "owner_reference"),
        "blocker": _validate_blocker(row.get("blocker")),
        "local_session_supported": bool(row.get("local_session_supported", False)),
        "calibration_only": bool(row.get("calibration_only", True)),
    }
    if "fallback_corpus_id" in row:
        config["fallback_corpus_id"] = _required_text(row, "fallback_corpus_id")
    if "fallback_corpus_name" in row:
        config["fallback_corpus_name"] = _required_label(row, "fallback_corpus_name")
    if "adapter_type_aliases" in row:
        values = row["adapter_type_aliases"]
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not _SAFE_ID.fullmatch(value) for value in values
        ):
            raise ProviderManifestError("adapter_type_aliases must contain safe identifiers")
        config["adapter_type_aliases"] = sorted(set(values))
    for field in ("local_source_root", "local_session_cookie_jar"):
        if field in row:
            value = row[field]
            if not isinstance(value, str) or not value.strip() or not Path(value).is_absolute():
                raise ProviderManifestError(f"{field} must be an absolute path string")
            config[field] = value.strip()
    if "notes" in row:
        values = row["notes"]
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not value.strip() for value in values
        ):
            raise ProviderManifestError("notes must contain non-empty strings")
        config["notes"] = [value.strip() for value in values]
    return config


def load_provider_manifest_snapshot(path: Path | None = None) -> dict[str, Any]:
    manifest_path = (path or default_provider_manifest_path()).resolve()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderManifestError("provider manifest is unreadable or malformed") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != PROVIDER_MANIFEST_VERSION:
        raise ProviderManifestError(f"schema_version must be {PROVIDER_MANIFEST_VERSION}")
    configuration_scope = payload.get("configuration_scope")
    if configuration_scope not in {"instance", "reusable-example"}:
        raise ProviderManifestError("configuration_scope must be instance or reusable-example")
    unknown_source_blocker = _validate_blocker(payload.get("unknown_source_blocker"))
    providers = payload.get("providers")
    if not isinstance(providers, list) or not providers:
        raise ProviderManifestError("providers must be a non-empty list")
    catalog: dict[str, dict[str, Any]] = {}
    alias_owners: dict[str, str] = {}
    for raw in providers:
        config = _validate_provider(raw)
        provider_id = config["provider_id"]
        if provider_id in catalog:
            raise ProviderManifestError(f"duplicate provider_id: {provider_id}")
        for alias in config["source_family_aliases"]:
            previous = alias_owners.get(alias)
            if previous is not None:
                raise ProviderManifestError(
                    f"source family alias {alias!r} is shared by {previous} and {provider_id}"
                )
            alias_owners[alias] = provider_id
        catalog[provider_id] = config
    return {
        "schema_version": PROVIDER_MANIFEST_VERSION,
        "configuration_scope": configuration_scope,
        "unknown_source_blocker": unknown_source_blocker,
        "providers": {provider_id: catalog[provider_id] for provider_id in sorted(catalog)},
    }


def load_provider_manifest(path: Path | None = None) -> dict[str, dict[str, Any]]:
    return load_provider_manifest_snapshot(path)["providers"]


# Compatibility read model. The authoritative catalog is the runtime manifest;
# callers that need a different snapshot call load_provider_manifest(path).
PROVIDER_CONFIG: dict[str, dict[str, Any]] = load_provider_manifest()


def default_source_drop_root(project_root: Path | None = None) -> Path:
    override = os.environ.get(DEFAULT_SOURCE_DROP_ENV)
    if override:
        return Path(override).expanduser().resolve()
    base_root = (project_root or Path.cwd()).resolve()
    return (base_root.parent / "source-drop").resolve()


def get_provider_config(provider: str, *, manifest_path: Path | None = None) -> dict[str, Any]:
    catalog = load_provider_manifest(manifest_path) if manifest_path else PROVIDER_CONFIG
    try:
        return catalog[provider]
    except KeyError as exc:
        raise KeyError(f"Unknown provider: {provider}") from exc


def conventional_corpus_root(source_drop_root: Path, corpus_id: str) -> Path:
    return (source_drop_root.resolve().parent / corpus_id).resolve()


def provider_bootstrap_report_path(project_root: Path, provider: str) -> Path:
    return project_root.resolve() / "reports" / f"{provider}-evaluation-bootstrap-latest.md"


def provider_corpus_targets(
    project_root: Path,
    provider: str,
    source_drop_root: Path,
    *,
    registry: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    config = get_provider_config(provider)
    registry_by_id = {item["corpus_id"]: item for item in registry or []}
    registry_roots = {
        str(Path(item["root"]).resolve()) for item in registry or [] if item.get("root")
    }
    policy = load_source_policy(project_root, provider)
    targets: list[dict[str, Any]] = []
    explicit_primary = bool(policy.get("primary_corpus_id") or policy.get("primary_root"))

    primary_corpus_id = policy.get("primary_corpus_id") or config["default_corpus_id"]
    primary_corpus_name = config["default_corpus_name"]
    primary_root = (
        policy.get("primary_root")
        or registry_by_id.get(primary_corpus_id, {}).get("root")
        or conventional_corpus_root(source_drop_root, primary_corpus_id)
    )
    if primary_root:
        targets.append(
            {
                "role": "primary",
                "selected": True,
                "corpus_id": primary_corpus_id,
                "corpus_name": primary_corpus_name,
                "root": str(Path(primary_root).resolve()),
                "policy": policy or None,
            }
        )

    fallback_corpus_id = policy.get("fallback_corpus_id") or config.get("fallback_corpus_id")
    fallback_corpus_name = config.get("fallback_corpus_name")
    fallback_root = policy.get("fallback_root")
    if fallback_corpus_id and not fallback_root:
        fallback_root = registry_by_id.get(fallback_corpus_id, {}).get(
            "root"
        ) or conventional_corpus_root(source_drop_root, fallback_corpus_id)
    if fallback_root and fallback_corpus_id:
        targets.append(
            {
                "role": "fallback",
                "selected": False,
                "corpus_id": fallback_corpus_id,
                "corpus_name": fallback_corpus_name or fallback_corpus_id,
                "root": str(Path(fallback_root).resolve()),
                "policy": policy or None,
            }
        )
    if not explicit_primary and len(targets) == 2:
        primary_root_path = Path(targets[0]["root"]).resolve()
        fallback_root_path = Path(targets[1]["root"]).resolve()
        primary_viable = primary_root_path.exists() and (
            (primary_root_path / "corpus" / "contract.json").exists()
            or str(primary_root_path) in registry_roots
        )
        fallback_viable = fallback_root_path.exists() and (
            (fallback_root_path / "corpus" / "contract.json").exists()
            or str(fallback_root_path) in registry_roots
        )
        if not primary_viable and fallback_viable:
            targets[0]["selected"] = False
            targets[1]["selected"] = True
    return targets
