from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .authority_projection import (
    AuthorityProjectionError,
    ProjectionCandidate,
    apply_reviewed_adoptions,
    body_hash,
    build_projection_candidate,
    canonical_bytes,
    canonical_json_bytes,
    normalize_timestamp,
    sha256_contract,
)
from .provider_catalog import load_provider_manifest_snapshot
from .source_adapter_registry import (
    AdapterDiagnostic,
    AdapterResult,
    RedactedSourceRecord,
    SourceBundleError,
    read_source_bundle,
)

_COVERAGE_STATUSES = (
    "acquired",
    "parsed",
    "quarantined",
    "inaccessible",
    "missing_expected",
    "owner_blocked",
)
DIGEST_ALGORITHM = "sha256-rfc8785-excluding-self-digest-v1"
_DIGEST_PATTERN = re.compile(r"^sha256:[a-f0-9]{64}$")
_SOURCE_CENSUS_FIELDS = {
    "contract_name",
    "contract_version",
    "census_id",
    "snapshot_id",
    "snapshot_at",
    "snapshot_digest",
    "manifest_reference",
    "manifest_digest",
    "discovery_roots",
    "seed_expectations",
    "raw_units",
    "digest_algorithm",
    "census_digest",
}
_RAW_UNIT_REQUIRED_FIELDS = {
    "raw_unit_id",
    "discovery_root_id",
    "source_family",
    "source_instance",
    "format_adapter",
    "native_identifiers",
    "acquisition_status",
    "content_hash",
    "custody_pointer",
    "evidence_references",
}
_RAW_UNIT_FIELDS = _RAW_UNIT_REQUIRED_FIELDS | {
    "legacy_source_id",
    "expectation_id",
    "owner_reference",
    "failed_predicate",
    "next_action",
}


class AuthorityIngestError(ValueError):
    """The authority-ingest run configuration is incomplete or contradictory."""


def contract_digest(value: Any) -> str:
    return f"sha256:{hashlib.sha256(canonical_json_bytes(value)).hexdigest()}"


def _is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_digest(value: Any) -> bool:
    return isinstance(value, str) and _DIGEST_PATTERN.fullmatch(value) is not None


def _validate_source_census_contract(census: dict[str, Any]) -> None:
    if set(census) != _SOURCE_CENSUS_FIELDS:
        raise AuthorityIngestError("source census fields do not match source-census.v1")
    if (
        census.get("contract_name") != "source-census.v1"
        or census.get("contract_version") != 1
        or census.get("digest_algorithm") != DIGEST_ALGORITHM
    ):
        raise AuthorityIngestError("source census version or digest algorithm is invalid")
    for field in ("census_id", "snapshot_id", "manifest_reference"):
        if not _is_non_empty_string(census.get(field)):
            raise AuthorityIngestError(f"source census {field} must be non-empty")
    normalize_timestamp(str(census.get("snapshot_at", "")))
    for field in ("snapshot_digest", "manifest_digest", "census_digest"):
        if not _is_digest(census.get(field)):
            raise AuthorityIngestError(f"source census {field} must be a SHA-256 digest")

    discovery_roots = census.get("discovery_roots")
    if not isinstance(discovery_roots, list) or not discovery_roots:
        raise AuthorityIngestError("source census discovery_roots must be non-empty")
    root_ids: set[str] = set()
    root_values: set[str] = set()
    for root in discovery_roots:
        if not isinstance(root, dict) or set(root) != {
            "root_id",
            "root_kind",
            "runtime_reference",
            "config_reference",
        }:
            raise AuthorityIngestError("source census discovery root is invalid")
        if root["root_kind"] not in {
            "git_ref",
            "workspace_root",
            "custody_manifest",
            "application_store",
            "export",
            "connector",
        } or any(
            not _is_non_empty_string(root[field])
            for field in ("root_id", "runtime_reference", "config_reference")
        ):
            raise AuthorityIngestError("source census discovery root is invalid")
        serialized = json.dumps(root, separators=(",", ":"), sort_keys=True)
        if root["root_id"] in root_ids or serialized in root_values:
            raise AuthorityIngestError("source census discovery roots must be unique")
        root_ids.add(root["root_id"])
        root_values.add(serialized)

    expectations = census.get("seed_expectations")
    if not isinstance(expectations, list):
        raise AuthorityIngestError("source census seed_expectations must be a list")
    expectation_ids: set[str] = set()
    expectation_values: set[str] = set()
    for expectation in expectations:
        if not isinstance(expectation, dict) or set(expectation) != {
            "expectation_id",
            "source_family",
            "config_reference",
            "required",
        }:
            raise AuthorityIngestError("source census seed expectation is invalid")
        if any(
            not _is_non_empty_string(expectation[field])
            for field in ("expectation_id", "source_family", "config_reference")
        ) or not isinstance(expectation["required"], bool):
            raise AuthorityIngestError("source census seed expectation is invalid")
        serialized = json.dumps(expectation, separators=(",", ":"), sort_keys=True)
        if expectation["expectation_id"] in expectation_ids or serialized in expectation_values:
            raise AuthorityIngestError("source census seed expectations must be unique")
        expectation_ids.add(expectation["expectation_id"])
        expectation_values.add(serialized)

    raw_units = census.get("raw_units")
    if not isinstance(raw_units, list) or not raw_units:
        raise AuthorityIngestError("source census raw_units must be non-empty")
    raw_unit_ids: set[str] = set()
    for raw_unit in raw_units:
        if (
            not isinstance(raw_unit, dict)
            or not _RAW_UNIT_REQUIRED_FIELDS.issubset(raw_unit)
            or not set(raw_unit).issubset(_RAW_UNIT_FIELDS)
        ):
            raise AuthorityIngestError("source census raw unit fields are invalid")
        raw_unit_id = raw_unit["raw_unit_id"]
        if (
            not isinstance(raw_unit_id, str)
            or not raw_unit_id.startswith("raw_")
            or raw_unit_id in raw_unit_ids
        ):
            raise AuthorityIngestError("source census raw_unit_ids must be unique and canonical")
        raw_unit_ids.add(raw_unit_id)
        if raw_unit["discovery_root_id"] not in root_ids:
            raise AuthorityIngestError("source census raw unit has an unknown discovery root")
        expectation_id = raw_unit.get("expectation_id")
        if expectation_id is not None and expectation_id not in expectation_ids:
            raise AuthorityIngestError("source census raw unit has an unknown expectation")
        for field in ("source_family", "source_instance", "format_adapter"):
            if not _is_non_empty_string(raw_unit[field]):
                raise AuthorityIngestError("source census raw unit identity is incomplete")
        native_identifiers = raw_unit["native_identifiers"]
        if (
            not isinstance(native_identifiers, dict)
            or not native_identifiers
            or any(
                not _is_non_empty_string(key) or not _is_non_empty_string(value)
                for key, value in native_identifiers.items()
            )
        ):
            raise AuthorityIngestError("source census native identifiers are invalid")
        evidence = raw_unit["evidence_references"]
        if (
            not isinstance(evidence, list)
            or not evidence
            or len(evidence) != len(set(evidence))
            or any(not _is_non_empty_string(reference) for reference in evidence)
        ):
            raise AuthorityIngestError("source census evidence references are invalid")
        acquisition_status = raw_unit["acquisition_status"]
        if acquisition_status not in {
            "acquired",
            "inaccessible",
            "missing_expected",
            "blocked",
        }:
            raise AuthorityIngestError("source census acquisition status is invalid")
        if acquisition_status == "acquired":
            if not _is_digest(raw_unit["content_hash"]) or not _is_non_empty_string(
                raw_unit["custody_pointer"]
            ):
                raise AuthorityIngestError(
                    "acquired source census units require content hash and custody"
                )
        elif any(
            not _is_non_empty_string(raw_unit.get(field))
            for field in ("owner_reference", "failed_predicate", "next_action")
        ):
            raise AuthorityIngestError(
                "non-acquired source census units require an owner-routed blocker"
            )


def _residual_source_id(identity: dict[str, Any]) -> str:
    return f"src_residual_{sha256_contract(identity).removeprefix('sha256:')[:24]}"


def _write_canonical(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(canonical_bytes(payload))
    temporary.replace(path)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as handle:
        for row in sorted(
            rows,
            key=lambda item: str(
                item.get("source_id") or item.get("event_id") or item.get("raw_unit_id") or ""
            ),
        ):
            handle.write(canonical_bytes(row))
    temporary.replace(path)


def _source_alias_map(catalog: dict[str, dict[str, Any]]) -> dict[str, str]:
    return {
        alias: provider_id
        for provider_id, provider in catalog.items()
        for alias in provider["source_family_aliases"]
    }


def _load_source_census(path: Path, snapshot_id: str) -> dict[str, Any]:
    try:
        census = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthorityIngestError("source census is unreadable or malformed") from exc
    if not isinstance(census, dict):
        raise AuthorityIngestError("source census must satisfy source-census.v1")
    _validate_source_census_contract(census)
    if census.get("snapshot_id") != snapshot_id:
        raise AuthorityIngestError("source census snapshot_id does not match the requested run")
    census_digest = census["census_digest"]
    unsigned = {key: value for key, value in census.items() if key != "census_digest"}
    if contract_digest(unsigned) != census_digest:
        raise AuthorityIngestError("source census digest does not match its canonical payload")
    return census


def _census_raw_unit_content_hashes(census: dict[str, Any]) -> dict[str, str | None]:
    return {raw_unit["raw_unit_id"]: raw_unit["content_hash"] for raw_unit in census["raw_units"]}


def _legacy_raw_unit_content_hash(record: RedactedSourceRecord) -> str:
    if record.raw_unit_ids and record.raw_unit_content_hashes:
        primary_raw_unit_id = record.raw_unit_ids[0]
        content_hash = record.raw_unit_content_hashes.get(primary_raw_unit_id)
        if isinstance(content_hash, str) and _is_digest(content_hash):
            return content_hash
    return contract_digest({"blob_shas": list(record.blob_shas)})


def _bound_raw_unit_content_hash(
    record: RedactedSourceRecord,
    census_content_hashes: dict[str, str | None] | None,
) -> str:
    if census_content_hashes is None:
        return _legacy_raw_unit_content_hash(record)
    if not record.raw_unit_ids:
        raise AuthorityIngestError(
            "an exact source census requires every redacted atom to name its raw unit"
        )
    if set(record.raw_unit_content_hashes) != set(record.raw_unit_ids):
        raise AuthorityIngestError(
            "redacted atom raw-unit content bindings must exactly cover its raw unit ids"
        )
    for raw_unit_id in record.raw_unit_ids:
        if raw_unit_id not in census_content_hashes:
            raise AuthorityIngestError(
                f"redacted atom references raw unit outside the exact census: {raw_unit_id}"
            )
        expected_content_hash = census_content_hashes[raw_unit_id]
        actual_content_hash = record.raw_unit_content_hashes[raw_unit_id]
        if not _is_digest(expected_content_hash):
            raise AuthorityIngestError(
                f"redacted atom references non-acquired census raw unit: {raw_unit_id}"
            )
        if actual_content_hash != expected_content_hash:
            raise AuthorityIngestError(
                f"redacted atom content binding does not match census raw unit: {raw_unit_id}"
            )
    return record.raw_unit_content_hashes[record.raw_unit_ids[0]]


def _blocker_source(
    *,
    snapshot_id: str,
    blocker: dict[str, str],
    identity: dict[str, Any],
    evidence_references: list[str],
    status: str = "owner_blocked",
    accessible: bool = False,
) -> dict[str, Any]:
    return {
        "source_id": _residual_source_id(
            {"snapshot_id": snapshot_id, "status": status, **identity}
        ),
        "status": status,
        "accessible": accessible,
        "owner_reference": blocker["owner_reference"],
        "failed_predicate": blocker["failed_predicate"],
        "next_action": blocker["next_action"],
        "evidence_references": sorted(set(evidence_references)),
    }


def _quarantine_source(
    *,
    snapshot_id: str,
    diagnostic: AdapterDiagnostic,
    blocker: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    coverage = _blocker_source(
        snapshot_id=snapshot_id,
        blocker=blocker,
        identity={"input_reference": diagnostic.input_reference, "code": diagnostic.code},
        evidence_references=[diagnostic.input_reference],
        status="quarantined",
        accessible=True,
    )
    row = {
        "contract_name": "authority-ingest-quarantine.v1",
        "contract_version": 1,
        "source_id": coverage["source_id"],
        "snapshot_id": snapshot_id,
        "status": "quarantined",
        "body_hash": diagnostic.body_hash,
        "input_reference": diagnostic.input_reference,
        "diagnostic": {"code": diagnostic.code, "message": diagnostic.message},
        "blocker": blocker,
    }
    return coverage, row


def _record_projection_diagnostic(record: RedactedSourceRecord, message: str) -> AdapterDiagnostic:
    return AdapterDiagnostic(
        line_number=record.line_number,
        input_reference=record.input_reference,
        body_hash=body_hash(record.text),
        code="invalid-authority-projection",
        message=message,
    )


def _resolve_runs(
    catalog: dict[str, dict[str, Any]], source_root: Path | None
) -> tuple[list[tuple[Path, tuple[str, ...], str]], list[str]]:
    if source_root is not None:
        adapter_ids = {provider["default_adapter_id"] for provider in catalog.values()}
        if len(adapter_ids) != 1:
            raise AuthorityIngestError(
                "an explicit shared bundle requires one common configured adapter"
            )
        return [
            (
                source_root,
                tuple(sorted(catalog)),
                next(iter(adapter_ids)),
            )
        ], []

    grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
    unresolved: list[str] = []
    for provider_id, provider in catalog.items():
        root_env = provider["source_contract"]["root_env"]
        value = os.environ.get(root_env)
        if not value:
            unresolved.append(provider_id)
            continue
        grouped[(str(Path(value).expanduser().resolve()), provider["default_adapter_id"])].append(
            provider_id
        )
    runs = [
        (Path(path), tuple(sorted(provider_ids)), adapter_id)
        for (path, adapter_id), provider_ids in sorted(grouped.items())
    ]
    return runs, sorted(unresolved)


def _read_runs(
    runs: list[tuple[Path, tuple[str, ...], str]],
) -> tuple[
    list[tuple[AdapterResult, tuple[str, ...]]],
    list[tuple[tuple[str, ...], str]],
]:
    results: list[tuple[AdapterResult, tuple[str, ...]]] = []
    failures: list[tuple[tuple[str, ...], str]] = []
    for source_root, provider_ids, adapter_id in runs:
        try:
            result = read_source_bundle(adapter_id, source_root)
        except SourceBundleError as exc:
            failures.append((provider_ids, str(exc)))
            continue
        results.append((result, provider_ids))
    return results, failures


def _coverage_receipt(
    *,
    snapshot_id: str,
    captured_at: str,
    manifest_hash: str,
    sources: list[dict[str, Any]],
) -> dict[str, Any]:
    ordered_sources = sorted(sources, key=lambda item: item["source_id"])
    counts = Counter(item["status"] for item in ordered_sources)
    count_payload = {status: counts.get(status, 0) for status in _COVERAGE_STATUSES}
    unique_ids = len({item["source_id"] for item in ordered_sources}) == len(ordered_sources)
    exact_all = (
        unique_ids
        and sum(count_payload.values()) == len(ordered_sources)
        and all(
            item["status"] == "parsed"
            or all(
                item.get(field) for field in ("owner_reference", "failed_predicate", "next_action")
            )
            for item in ordered_sources
        )
    )
    residual_owners = [
        {
            "source_id": item["source_id"],
            "owner_reference": item["owner_reference"],
            "failed_predicate": item["failed_predicate"],
            "next_action": item["next_action"],
        }
        for item in ordered_sources
        if item["status"] != "parsed"
    ]
    unresolved_blockers = sorted(
        item["source_id"]
        for item in ordered_sources
        if item["status"] in {"inaccessible", "owner_blocked"}
    )
    quarantines = sorted(
        item["source_id"] for item in ordered_sources if item["status"] == "quarantined"
    )
    missing_requirements = sorted(
        item["source_id"] for item in ordered_sources if item["status"] == "missing_expected"
    )
    incomplete_predicates = sorted(
        item["source_id"] for item in ordered_sources if item["status"] == "acquired"
    )
    ready = (
        exact_all
        and count_payload["parsed"] == len(ordered_sources)
        and not residual_owners
        and not unresolved_blockers
        and not quarantines
        and not missing_requirements
        and not incomplete_predicates
    )
    receipt_id = _residual_source_id(
        {"snapshot_id": snapshot_id, "manifest_hash": manifest_hash, "kind": "coverage"}
    ).removeprefix("src_")
    receipt: dict[str, Any] = {
        "contract_name": "coverage-receipt.v1",
        "contract_version": 1,
        "receipt_id": receipt_id,
        "snapshot_id": snapshot_id,
        "generated_at": normalize_timestamp(captured_at),
        "denominator": {
            "discovery_manifest_reference": f"provider-manifest:{manifest_hash}",
            "count": len(ordered_sources),
            "manifest_hash": manifest_hash,
        },
        "sources": ordered_sources,
        "counts": count_payload,
        "exact_all": exact_all,
        "ready": ready,
        "unresolved_blockers": unresolved_blockers,
        "quarantines": quarantines,
        "missing_requirements": missing_requirements,
        "citation_debt": [],
        "incomplete_predicates": incomplete_predicates,
        "closure_status": (
            "ready" if ready else "closed_with_owner_routed_debt" if exact_all else "incomplete"
        ),
        "residual_owners": residual_owners,
    }
    receipt["receipt_hash"] = sha256_contract(receipt)
    return receipt


def _coverage_sources_from_parity(
    census: dict[str, Any], parity: dict[str, Any]
) -> list[dict[str, Any]]:
    promotions = {promotion["raw_unit_id"]: promotion for promotion in parity["promotions"]}
    sources: list[dict[str, Any]] = []
    for raw_unit in census["raw_units"]:
        raw_unit_id = raw_unit["raw_unit_id"]
        promotion = promotions[raw_unit_id]
        if promotion.get("raw_unit_content_hash") != raw_unit.get("content_hash"):
            raise AuthorityIngestError(
                f"parity promotion content binding does not match census raw unit: {raw_unit_id}"
            )
        source_id = f"src_{raw_unit_id.removeprefix('raw_')}"
        evidence_references = sorted(
            set(raw_unit.get("evidence_references") or [f"raw-unit:{raw_unit_id}"])
        )
        if promotion.get("event_ids"):
            sources.append(
                {
                    "source_id": source_id,
                    "status": "parsed",
                    "accessible": True,
                    "evidence_references": evidence_references,
                }
            )
            continue
        disposition = promotion["disposition"]
        acquisition_status = raw_unit["acquisition_status"]
        if acquisition_status == "inaccessible":
            status = "inaccessible"
        elif acquisition_status == "missing_expected":
            status = "missing_expected"
        elif acquisition_status == "blocked":
            status = "owner_blocked"
        elif disposition["type"] == "quarantined":
            status = "quarantined"
        else:
            status = "acquired"
        sources.append(
            {
                "source_id": source_id,
                "status": status,
                "accessible": status in {"acquired", "parsed", "quarantined"},
                "owner_reference": disposition["owner_reference"],
                "failed_predicate": disposition["failed_predicate"],
                "next_action": disposition["next_action"],
                "evidence_references": evidence_references,
            }
        )
    return sources


def _normalization_parity_receipt(
    *,
    census: dict[str, Any],
    candidates: list[ProjectionCandidate],
    events: list[dict[str, Any]],
    aliases: dict[str, str],
    catalog: dict[str, dict[str, Any]],
    captured_at: str,
    diagnostic_blockers: list[str],
    diagnostic_quarantines: list[str],
) -> dict[str, Any]:
    events_by_raw_unit: dict[str, set[str]] = defaultdict(set)
    for candidate in candidates:
        raw_unit_ids = candidate.record.raw_unit_ids or (candidate.event["raw_unit_id"],)
        for raw_unit_id in raw_unit_ids:
            events_by_raw_unit[raw_unit_id].add(candidate.event["event_id"])

    promotions: list[dict[str, Any]] = []
    blockers: list[str] = []
    quarantines: list[str] = []
    missing: list[str] = []
    census_raw_unit_ids: list[str] = []
    census_raw_units: list[dict[str, Any]] = []
    for raw_unit in sorted(census["raw_units"], key=lambda item: item["raw_unit_id"]):
        raw_unit_id = raw_unit["raw_unit_id"]
        raw_unit_content_hash = raw_unit.get("content_hash")
        census_raw_unit_ids.append(raw_unit_id)
        census_raw_units.append(
            {
                "raw_unit_id": raw_unit_id,
                "content_hash": raw_unit_content_hash,
            }
        )
        event_ids = sorted(events_by_raw_unit.get(raw_unit_id, set()))
        if event_ids:
            promotions.append(
                {
                    "raw_unit_id": raw_unit_id,
                    "raw_unit_content_hash": raw_unit_content_hash,
                    "event_ids": event_ids,
                }
            )
            continue

        provider_id = aliases.get(raw_unit["source_family"])
        provider = catalog.get(provider_id) if provider_id else None
        acquisition_status = raw_unit["acquisition_status"]
        if acquisition_status != "acquired":
            disposition_type = "blocked"
            owner_reference = raw_unit.get("owner_reference")
            failed_predicate = raw_unit.get("failed_predicate")
            next_action = raw_unit.get("next_action")
            blockers.append(raw_unit_id)
            if acquisition_status == "missing_expected":
                missing.append(raw_unit_id)
        elif provider is None:
            disposition_type = "unsupported"
            owner_reference = "repo:organvm/conversation-corpus-engine"
            failed_predicate = "source family has a configured normalization adapter"
            next_action = "register the source family and adapter, then rerun the frozen snapshot"
            blockers.append(raw_unit_id)
        else:
            disposition_type = "quarantined"
            owner_reference = provider["blocker"]["owner_reference"]
            failed_predicate = "registered adapter emits a valid normalized event"
            next_action = "repair or re-export the source unit, then rerun the same snapshot"
            quarantines.append(raw_unit_id)
        promotions.append(
            {
                "raw_unit_id": raw_unit_id,
                "raw_unit_content_hash": raw_unit_content_hash,
                "disposition": {
                    "type": disposition_type,
                    "owner_reference": owner_reference or "repo:organvm/conversation-corpus-engine",
                    "failed_predicate": failed_predicate
                    or "raw source unit is available for normalization",
                    "next_action": next_action
                    or "restore the configured source and rerun the frozen snapshot",
                    "evidence_references": sorted(
                        set(raw_unit.get("evidence_references") or [f"raw-unit:{raw_unit_id}"])
                    ),
                },
            }
        )

    event_ids = sorted(event["event_id"] for event in events)
    promoted_event_ids = sorted(
        {event_id for promotion in promotions for event_id in promotion.get("event_ids", [])}
    )
    exact_all = (
        len(promotions) == len(census_raw_unit_ids)
        and len({item["raw_unit_id"] for item in promotions}) == len(census_raw_unit_ids)
        and promoted_event_ids == event_ids
    )
    blocker_debt = sorted(set(blockers + diagnostic_blockers))
    quarantine_debt = sorted(set(quarantines + diagnostic_quarantines))
    ready = exact_all and not blocker_debt and not quarantine_debt and not missing
    readiness = {
        "exact_all": exact_all,
        "unresolved_blockers": blocker_debt,
        "quarantines": quarantine_debt,
        "missing_requirements": sorted(set(missing)),
        "citation_debt": [],
        "incomplete_predicates": [],
        "ready": ready,
        "status": ("ready" if ready else "blocked" if blocker_debt or missing else "incomplete"),
    }
    receipt_identity = {
        "snapshot_id": census["snapshot_id"],
        "census_digest": census["census_digest"],
        "event_ids": event_ids,
    }
    receipt: dict[str, Any] = {
        "contract_name": "normalization-parity-receipt.v1",
        "contract_version": 1,
        "receipt_id": "normalization-parity-"
        f"{contract_digest(receipt_identity).removeprefix('sha256:')}",
        "snapshot_id": census["snapshot_id"],
        "snapshot_digest": census["snapshot_digest"],
        "generated_at": normalize_timestamp(captured_at),
        "input_census": {
            "census_id": census["census_id"],
            "census_reference": "source-census.v1.json",
            "census_digest": census["census_digest"],
            "raw_unit_ids": census_raw_unit_ids,
            "raw_units": census_raw_units,
        },
        "output_events": {
            "event_set_reference": "normalized-events.v1.jsonl",
            "event_set_digest": contract_digest(events),
            "event_ids": event_ids,
        },
        "promotions": promotions,
        "readiness": readiness,
        "digest_algorithm": DIGEST_ALGORITHM,
    }
    receipt["receipt_digest"] = contract_digest(receipt)
    return receipt


def _legacy_census(
    *,
    snapshot_id: str,
    snapshot_digest: str,
    captured_at: str,
    candidates: list[ProjectionCandidate],
    coverage_sources: list[dict[str, Any]],
) -> dict[str, Any]:
    raw_units_by_id: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        event = candidate.event
        for raw_unit_id in candidate.record.raw_unit_ids or (event["raw_unit_id"],):
            raw_units_by_id.setdefault(
                raw_unit_id,
                {
                    "raw_unit_id": raw_unit_id,
                    "source_family": event["source_family"],
                    "source_instance": event["source_instance"],
                    "format_adapter": event["format_adapter"],
                    "acquisition_status": "acquired",
                    "content_hash": event["raw_unit_content_hash"],
                    "evidence_references": [f"raw-unit:{raw_unit_id}"],
                },
            )
    for source in coverage_sources:
        if source["status"] == "parsed":
            continue
        raw_unit_id = (
            f"raw_{sha256_contract({'source_id': source['source_id']}).removeprefix('sha256:')}"
        )
        raw_units_by_id.setdefault(
            raw_unit_id,
            {
                "raw_unit_id": raw_unit_id,
                "source_family": "legacy-residual",
                "source_instance": source["source_id"],
                "format_adapter": "legacy-compatibility",
                "acquisition_status": "blocked",
                "content_hash": None,
                "owner_reference": source.get("owner_reference", "owner:unresolved"),
                "failed_predicate": source.get("failed_predicate", "legacy residual is normalized"),
                "next_action": source.get(
                    "next_action", "supply a source-census.v1 snapshot and rerun"
                ),
                "evidence_references": source.get("evidence_references")
                or [f"legacy-source:{source['source_id']}"],
            },
        )
    raw_units = sorted(raw_units_by_id.values(), key=lambda item: item["raw_unit_id"])
    if not raw_units:
        raw_unit_id = f"raw_{sha256_contract({'snapshot_id': snapshot_id}).removeprefix('sha256:')}"
        raw_units = [
            {
                "raw_unit_id": raw_unit_id,
                "source_family": "legacy-empty",
                "source_instance": "legacy-empty",
                "format_adapter": "legacy-compatibility",
                "acquisition_status": "blocked",
                "content_hash": None,
                "owner_reference": "repo:organvm/conversation-corpus-engine",
                "failed_predicate": "a source-census.v1 snapshot is supplied",
                "next_action": "supply the exact session-meta census and rerun",
                "evidence_references": [f"snapshot:{snapshot_id}"],
            }
        ]
    census: dict[str, Any] = {
        "contract_name": "source-census.v1",
        "contract_version": 1,
        "census_id": f"legacy-census-{snapshot_id}",
        "snapshot_id": snapshot_id,
        "snapshot_at": normalize_timestamp(captured_at),
        "snapshot_digest": snapshot_digest,
        "raw_units": raw_units,
    }
    census["census_digest"] = sha256_contract(census)
    return census


def ingest_authority_bundle(
    *,
    output_root: Path,
    snapshot_id: str,
    captured_at: str,
    custody_pointer: str,
    source_root: Path | None = None,
    source_census: Path | None = None,
    provider_manifest: Path | None = None,
) -> dict[str, Any]:
    if not snapshot_id.strip():
        raise AuthorityIngestError("snapshot_id must be non-empty")
    if not custody_pointer.strip():
        raise AuthorityIngestError("custody_pointer must be non-empty")
    normalize_timestamp(captured_at)
    census = _load_source_census(source_census, snapshot_id) if source_census else None
    census_content_hashes = _census_raw_unit_content_hashes(census) if census is not None else None
    if census is not None and source_root is None:
        raise AuthorityIngestError("an exact source census requires its bound redacted source root")
    manifest = load_provider_manifest_snapshot(provider_manifest)
    catalog = manifest["providers"]
    aliases = _source_alias_map(catalog)
    manifest_hash = sha256_contract(manifest)
    runs, unresolved_provider_ids = _resolve_runs(catalog, source_root)
    read_results, read_failures = _read_runs(runs)
    snapshot_hash = (
        census["snapshot_digest"]
        if census is not None
        else sha256_contract(
            {"source_hashes": sorted({result.source_hash for result, _ in read_results})}
        )
    )

    candidates: list[ProjectionCandidate] = []
    coverage_sources: list[dict[str, Any]] = []
    quarantine_rows: list[dict[str, Any]] = []
    blocker_rows: list[dict[str, Any]] = []

    for provider_id in unresolved_provider_ids:
        provider = catalog[provider_id]
        coverage = _blocker_source(
            snapshot_id=snapshot_id,
            blocker=provider["blocker"],
            identity={"provider_id": provider_id, "reason": "unresolved-root"},
            evidence_references=[f"provider-manifest:{manifest_hash}"],
        )
        coverage_sources.append(coverage)
        blocker_rows.append({"snapshot_id": snapshot_id, **coverage})

    for provider_ids, message in read_failures:
        for provider_id in provider_ids:
            provider = catalog[provider_id]
            coverage = _blocker_source(
                snapshot_id=snapshot_id,
                blocker=provider["blocker"],
                identity={"provider_id": provider_id, "reason": "unreadable-root"},
                evidence_references=[f"provider-manifest:{manifest_hash}"],
            )
            coverage_sources.append(coverage)
            blocker_rows.append(
                {
                    "snapshot_id": snapshot_id,
                    "diagnostic": {"code": "unreadable-source-root", "message": message},
                    **coverage,
                }
            )

    for result, expected_provider_ids in read_results:
        seen_provider_ids: set[str] = set()
        for diagnostic in result.diagnostics:
            coverage, quarantine = _quarantine_source(
                snapshot_id=snapshot_id,
                diagnostic=diagnostic,
                blocker=manifest["unknown_source_blocker"],
            )
            coverage_sources.append(coverage)
            quarantine_rows.append(quarantine)
        for record in result.records:
            matched_provider_id = aliases.get(record.source_family)
            if matched_provider_id is None or matched_provider_id not in expected_provider_ids:
                coverage = _blocker_source(
                    snapshot_id=snapshot_id,
                    blocker=manifest["unknown_source_blocker"],
                    identity={
                        "source_family": record.source_family,
                        "input_reference": record.input_reference,
                    },
                    evidence_references=[
                        record.input_reference,
                        f"provider-manifest:{manifest_hash}",
                    ],
                    accessible=True,
                )
                coverage_sources.append(coverage)
                blocker_rows.append({"snapshot_id": snapshot_id, **coverage})
                continue
            seen_provider_ids.add(matched_provider_id)
            provider = catalog[matched_provider_id]
            raw_unit_content_hash = _bound_raw_unit_content_hash(record, census_content_hashes)
            try:
                candidate = build_projection_candidate(
                    record,
                    provider,
                    snapshot_id=snapshot_id,
                    captured_at=captured_at,
                    snapshot_hash=snapshot_hash,
                    custody_pointer=custody_pointer,
                    raw_unit_content_hash=raw_unit_content_hash,
                )
            except AuthorityProjectionError as exc:
                diagnostic = _record_projection_diagnostic(record, str(exc))
                coverage, quarantine = _quarantine_source(
                    snapshot_id=snapshot_id,
                    diagnostic=diagnostic,
                    blocker=provider["blocker"],
                )
                coverage_sources.append(coverage)
                quarantine_rows.append(quarantine)
                continue
            candidates.append(candidate)
            coverage_sources.append(
                {
                    "source_id": candidate.envelope["source_id"],
                    "status": "parsed",
                    "accessible": True,
                    "evidence_references": [record.input_reference],
                }
            )
        for provider_id in (
            set() if census is not None else set(expected_provider_ids) - seen_provider_ids
        ):
            provider = catalog[provider_id]
            coverage = _blocker_source(
                snapshot_id=snapshot_id,
                blocker=provider["blocker"],
                identity={"provider_id": provider_id, "reason": "missing-expected-source"},
                evidence_references=[
                    f"bundle:sha256:{result.source_hash}",
                    f"provider-manifest:{manifest_hash}",
                ],
                status="missing_expected",
            )
            coverage_sources.append(coverage)
            blocker_rows.append({"snapshot_id": snapshot_id, **coverage})

    apply_reviewed_adoptions(candidates)
    candidates_by_event_id: dict[str, ProjectionCandidate] = {}
    for candidate in candidates:
        candidates_by_event_id.setdefault(candidate.event["event_id"], candidate)
    unique_candidates = [
        candidates_by_event_id[event_id] for event_id in sorted(candidates_by_event_id)
    ]
    envelopes = [candidate.envelope for candidate in unique_candidates]
    events = [candidate.event for candidate in unique_candidates]
    coverage_sources = list({source["source_id"]: source for source in coverage_sources}.values())
    if census is None:
        census = _legacy_census(
            snapshot_id=snapshot_id,
            snapshot_digest=snapshot_hash,
            captured_at=captured_at,
            candidates=unique_candidates,
            coverage_sources=coverage_sources,
        )
    parity = _normalization_parity_receipt(
        census=census,
        candidates=unique_candidates,
        events=events,
        aliases=aliases,
        catalog=catalog,
        captured_at=captured_at,
        diagnostic_blockers=[
            row["source_id"] for row in blocker_rows if isinstance(row.get("source_id"), str)
        ],
        diagnostic_quarantines=[
            row["source_id"] for row in quarantine_rows if isinstance(row.get("source_id"), str)
        ],
    )
    coverage = _coverage_receipt(
        snapshot_id=snapshot_id,
        captured_at=captured_at,
        manifest_hash=manifest_hash,
        sources=(
            _coverage_sources_from_parity(census, parity)
            if source_census is not None
            else coverage_sources
        ),
    )

    resolved_output = output_root.resolve()
    paths = {
        "source_envelopes": resolved_output / "source-envelope.v1.jsonl",
        "normalized_events": resolved_output / "normalized-events.v1.jsonl",
        "quarantine": resolved_output / "quarantine.jsonl",
        "owner_blockers": resolved_output / "owner-blockers.jsonl",
        "coverage_receipt": resolved_output / "coverage-receipt.v1.json",
        "parity_receipt": resolved_output / "normalization-parity-receipt.v1.json",
    }
    _write_jsonl(paths["source_envelopes"], envelopes)
    _write_jsonl(paths["normalized_events"], events)
    _write_jsonl(paths["quarantine"], quarantine_rows)
    _write_jsonl(paths["owner_blockers"], blocker_rows)
    _write_canonical(paths["coverage_receipt"], coverage)
    _write_canonical(paths["parity_receipt"], parity)
    return {
        "snapshot_id": snapshot_id,
        "snapshot_hash": snapshot_hash,
        "source_census_id": census["census_id"],
        "provider_manifest_hash": manifest_hash,
        "coverage": coverage,
        "parity": parity,
        "paths": {key: str(path) for key, path in paths.items()},
    }
