from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .authority_ingest import contract_digest

PARSE_PROJECTION = "cce-authority-parse-projection.v1.json"
CLASSIFY_PROJECTION = "cce-authority-classify-projection.v1.json"
_ARTIFACTS: tuple[tuple[str, str, str], ...] = (
    ("source_envelopes", "source-envelope.v1.jsonl", "source-envelope.v1"),
    ("normalized_events", "normalized-events.v1.jsonl", "normalized-event.v1"),
    ("quarantine", "quarantine.jsonl", "authority-ingest-quarantine.v1"),
    ("owner_blockers", "owner-blockers.jsonl", "authority-ingest-owner-blocker.v1"),
    ("coverage_receipt", "coverage-receipt.v1.json", "coverage-receipt.v1"),
    (
        "parity_receipt",
        "normalization-parity-receipt.v1.json",
        "normalization-parity-receipt.v1",
    ),
)
_DEBT_FIELDS = (
    "unresolved_blockers",
    "quarantines",
    "missing_requirements",
    "citation_debt",
    "incomplete_predicates",
)
_COVERAGE_STATUSES = (
    "acquired",
    "parsed",
    "quarantined",
    "inaccessible",
    "missing_expected",
    "owner_blocked",
)
_AUTHORITY_CLASSES = {
    "operator_intent",
    "artifact",
    "transport_echo",
    "system_metadata",
    "unknown",
}
_NORMALIZED_ROLES = {
    "operator",
    "assistant",
    "system",
    "tool",
    "continuation_summary",
    "memory_summary",
    "unknown",
}
_BOUNDARY = {
    "implementation": "single-authority-ingest-transaction",
    "classification_materialized_during_parse": True,
    "classify_operation": "digest-verified-byte-projection",
}
_HEX = frozenset("0123456789abcdef")
_PROJECTION_COMMON_FIELDS = {
    "contract_name",
    "contract_version",
    "stage",
    "snapshot_id",
    "snapshot_digest",
    "owner_input_digest",
    "captured_at",
    "boundary",
    "artifacts",
    "readiness",
    "classification_summary",
    "projection_digest",
}
_PROJECTION_STAGE_FIELDS = {
    "parse": _PROJECTION_COMMON_FIELDS | {"source_census_id", "provider_manifest_digest"},
    "classify": _PROJECTION_COMMON_FIELDS | {"source_parse_projection_digest"},
}


class AuthorityCadencePredicateError(ValueError):
    """A separately executed CCE cadence predicate failed."""


def _is_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("sha256:")
        and len(value) == 71
        and set(value.removeprefix("sha256:")) <= _HEX
    )


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthorityCadencePredicateError(f"{path.name} is unreadable or malformed") from exc
    if not isinstance(value, dict):
        raise AuthorityCadencePredicateError(f"{path.name} must contain an object")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise AuthorityCadencePredicateError(f"{path.name} is unreadable") from exc
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AuthorityCadencePredicateError(
                f"{path.name}:{line_number} is malformed JSONL"
            ) from exc
        if not isinstance(row, dict):
            raise AuthorityCadencePredicateError(
                f"{path.name}:{line_number} must contain an object"
            )
        rows.append(row)
    return rows


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise AuthorityCadencePredicateError(f"{path.name} is unreadable") from exc
    return f"sha256:{digest.hexdigest()}"


def _tree_digest(path: Path) -> str:
    resolved = path.resolve()
    if resolved.is_file():
        return contract_digest(
            {
                "kind": "file",
                "digest": _file_digest(resolved),
                "size_bytes": resolved.stat().st_size,
            }
        )
    if not resolved.is_dir():
        raise AuthorityCadencePredicateError(f"predicate input does not exist: {path}")
    files = [
        {
            "relative_path": item.relative_to(resolved).as_posix(),
            "digest": _file_digest(item),
            "size_bytes": item.stat().st_size,
        }
        for item in sorted(candidate for candidate in resolved.rglob("*") if candidate.is_file())
    ]
    if not files:
        raise AuthorityCadencePredicateError("predicate source root has no files")
    return contract_digest({"kind": "directory", "files": files})


def _descriptors(root: Path) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for artifact_id, filename, contract in _ARTIFACTS:
        path = root / filename
        if not path.is_file():
            raise AuthorityCadencePredicateError(f"owner artifact is missing: {filename}")
        record_count = len(_read_jsonl(path)) if filename.endswith(".jsonl") else 1
        if not filename.endswith(".jsonl"):
            _read_object(path)
        values.append(
            {
                "artifact_id": artifact_id,
                "reference": filename,
                "contract": contract,
                "digest": _file_digest(path),
                "size_bytes": path.stat().st_size,
                "record_count": record_count,
            }
        )
    return values


def _combined_readiness(coverage: dict[str, Any], parity: dict[str, Any]) -> dict[str, Any]:
    parity_readiness = parity.get("readiness")
    if not isinstance(parity_readiness, dict):
        raise AuthorityCadencePredicateError("parity readiness is missing")
    exact_all = coverage.get("exact_all") is True and parity_readiness.get("exact_all") is True
    readiness: dict[str, Any] = {"exact_all": exact_all}
    for field in _DEBT_FIELDS:
        values: list[str] = []
        for owner in (coverage, parity_readiness):
            debt = owner.get(field)
            if not isinstance(debt, list) or not all(
                isinstance(item, str) and item.strip() for item in debt
            ):
                raise AuthorityCadencePredicateError(f"owner readiness {field} is invalid")
            values.extend(debt)
        readiness[field] = sorted(set(values))
    readiness["ready"] = exact_all and not any(readiness[field] for field in _DEBT_FIELDS)
    readiness["status"] = (
        "ready"
        if readiness["ready"]
        else "closed_with_owner_routed_debt"
        if exact_all
        else "incomplete"
    )
    return readiness


def _summary(
    events: list[dict[str, Any]], envelopes: list[dict[str, Any]], parity: dict[str, Any]
) -> dict[str, Any]:
    promotions = parity.get("promotions")
    if not isinstance(promotions, list):
        raise AuthorityCadencePredicateError("parity promotions must be a list")
    return {
        "event_count": len(events),
        "source_envelope_count": len(envelopes),
        "promotion_count": len(promotions),
        "authority_class_counts": dict(
            sorted(Counter(str(row.get("authority_class")) for row in events).items())
        ),
        "normalized_role_counts": dict(
            sorted(Counter(str(row.get("normalized_role")) for row in events).items())
        ),
    }


def _verify_coverage_semantics(coverage: dict[str, Any]) -> None:
    sources = coverage.get("sources")
    if not isinstance(sources, list) or not sources:
        raise AuthorityCadencePredicateError("coverage denominator is empty")
    source_ids = [row.get("source_id") for row in sources if isinstance(row, dict)]
    if len(source_ids) != len(sources) or len(source_ids) != len(set(source_ids)):
        raise AuthorityCadencePredicateError("coverage source IDs are invalid")
    counts = Counter(str(row.get("status")) for row in sources)
    expected_counts = {status: counts.get(status, 0) for status in _COVERAGE_STATUSES}
    if coverage.get("counts") != expected_counts or sum(expected_counts.values()) != len(sources):
        raise AuthorityCadencePredicateError("coverage counts are not exact")
    residuals = [row for row in sources if row.get("status") != "parsed"]
    exact_all = all(
        all(
            isinstance(row.get(field), str) and row[field].strip()
            for field in ("owner_reference", "failed_predicate", "next_action")
        )
        for row in residuals
    )
    if coverage.get("exact_all") is not exact_all:
        raise AuthorityCadencePredicateError("coverage exact_all is not derived")
    debt_free = all(not coverage.get(field) for field in _DEBT_FIELDS)
    if coverage.get("ready") is not (exact_all and not residuals and debt_free):
        raise AuthorityCadencePredicateError("coverage ready is not derived")


def _verify_parity_semantics(parity: dict[str, Any], event_ids: list[str]) -> None:
    input_census = parity.get("input_census")
    promotions = parity.get("promotions")
    if not isinstance(input_census, dict) or not isinstance(promotions, list) or not promotions:
        raise AuthorityCadencePredicateError("parity denominator is incomplete")
    raw_unit_ids = input_census.get("raw_unit_ids")
    if (
        not isinstance(raw_unit_ids, list)
        or not raw_unit_ids
        or len(raw_unit_ids) != len(set(raw_unit_ids))
    ):
        raise AuthorityCadencePredicateError("parity raw-unit denominator is invalid")
    promotion_ids = [
        promotion.get("raw_unit_id") for promotion in promotions if isinstance(promotion, dict)
    ]
    if (
        len(promotion_ids) != len(promotions)
        or len(promotion_ids) != len(set(promotion_ids))
        or set(promotion_ids) != set(raw_unit_ids)
    ):
        raise AuthorityCadencePredicateError("parity promotions do not form an exact crosswalk")
    promoted_event_ids: set[str] = set()
    for promotion in promotions:
        promoted = promotion.get("event_ids")
        disposition = promotion.get("disposition")
        if isinstance(promoted, list) and promoted and disposition is None:
            if len(promoted) != len(set(promoted)) or any(
                event_id not in event_ids for event_id in promoted
            ):
                raise AuthorityCadencePredicateError("parity event promotion is invalid")
            promoted_event_ids.update(promoted)
        elif not (
            promoted is None
            and isinstance(disposition, dict)
            and all(
                isinstance(disposition.get(field), str) and disposition[field].strip()
                for field in ("type", "owner_reference", "failed_predicate", "next_action")
            )
        ):
            raise AuthorityCadencePredicateError(
                "parity requires one event mapping or owner disposition"
            )
    exact_all = sorted(promoted_event_ids) == sorted(event_ids)
    readiness = parity.get("readiness")
    if not isinstance(readiness, dict) or readiness.get("exact_all") is not exact_all:
        raise AuthorityCadencePredicateError("parity exact_all is not derived")
    debt_free = all(not readiness.get(field) for field in _DEBT_FIELDS)
    if readiness.get("ready") is not (exact_all and debt_free):
        raise AuthorityCadencePredicateError("parity ready is not derived")


def _verify_transaction(root: Path, *, snapshot_id: str) -> dict[str, Any]:
    events = _read_jsonl(root / "normalized-events.v1.jsonl")
    envelopes = _read_jsonl(root / "source-envelope.v1.jsonl")
    coverage = _read_object(root / "coverage-receipt.v1.json")
    parity = _read_object(root / "normalization-parity-receipt.v1.json")
    snapshot_digest = parity.get("snapshot_digest")
    if not _is_digest(snapshot_digest):
        raise AuthorityCadencePredicateError("parity snapshot digest is invalid")
    if coverage.get("receipt_hash") != contract_digest(
        {key: value for key, value in coverage.items() if key != "receipt_hash"}
    ):
        raise AuthorityCadencePredicateError("coverage receipt digest is invalid")
    if parity.get("receipt_digest") != contract_digest(
        {key: value for key, value in parity.items() if key != "receipt_digest"}
    ):
        raise AuthorityCadencePredicateError("parity receipt digest is invalid")
    if (
        coverage.get("snapshot_id") != snapshot_id
        or parity.get("snapshot_id") != snapshot_id
        or any(
            row.get("snapshot_id") != snapshot_id or row.get("snapshot_digest") != snapshot_digest
            for row in events
        )
        or any(
            row.get("custody_snapshot", {}).get("snapshot_id") != snapshot_id
            or row.get("custody_snapshot", {}).get("snapshot_hash") != snapshot_digest
            for row in envelopes
        )
    ):
        raise AuthorityCadencePredicateError("owner artifacts do not bind the frozen snapshot")
    event_ids = [row.get("event_id") for row in events]
    if any(not isinstance(event_id, str) or not event_id for event_id in event_ids) or len(
        event_ids
    ) != len(set(event_ids)):
        raise AuthorityCadencePredicateError("normalized event IDs are invalid")
    envelope_by_id = {row.get("source_id"): row for row in envelopes}
    source_ids = set(envelope_by_id)
    if len(envelope_by_id) != len(envelopes):
        raise AuthorityCadencePredicateError("source envelope IDs are not unique")
    if any(
        row.get("contract_name") != "normalized-event.v1"
        or row.get("contract_version") != 1
        or row.get("authority_class") not in _AUTHORITY_CLASSES
        or row.get("normalized_role") not in _NORMALIZED_ROLES
        or (
            row.get("authority_class") == "operator_intent"
            and row.get("normalized_role") != "operator"
        )
        for row in events
    ):
        raise AuthorityCadencePredicateError("normalized event contract fields are invalid")
    if any(
        row.get("contract_name") != "source-envelope.v1"
        or row.get("contract_version") != 1
        or row.get("authority_class") not in _AUTHORITY_CLASSES
        or row.get("role") not in _NORMALIZED_ROLES
        for row in envelopes
    ):
        raise AuthorityCadencePredicateError("source envelope contract fields are invalid")
    if any(
        str(row.get("source_envelope_reference", "")).rpartition("#")[2] not in source_ids
        for row in events
    ):
        raise AuthorityCadencePredicateError("event to source-envelope linkage is incomplete")
    for event in events:
        envelope_id = str(event["source_envelope_reference"]).rpartition("#")[2]
        envelope = envelope_by_id[envelope_id]
        if (
            event.get("raw_unit_id") != envelope.get("raw_unit_id")
            or event.get("raw_unit_content_hash") != envelope.get("raw_unit_content_hash")
            or event.get("identity_basis", {}).get("content_hash") != envelope.get("body_hash")
        ):
            raise AuthorityCadencePredicateError(
                "event and source-envelope immutable bindings differ"
            )
    output_events = parity.get("output_events")
    if (
        not isinstance(output_events, dict)
        or sorted(str(event_id) for event_id in event_ids)
        != sorted(output_events.get("event_ids", []))
        or output_events.get("event_set_digest") != contract_digest(events)
    ):
        raise AuthorityCadencePredicateError("parity output event binding is invalid")
    normalized_event_ids = [str(event_id) for event_id in event_ids]
    _verify_coverage_semantics(coverage)
    _verify_parity_semantics(parity, normalized_event_ids)
    return {
        "artifacts": _descriptors(root),
        "snapshot_digest": snapshot_digest,
        "readiness": _combined_readiness(coverage, parity),
        "classification_summary": _summary(events, envelopes, parity),
    }


def _verify_projection(
    root: Path,
    *,
    filename: str,
    contract_name: str,
    stage: str,
    snapshot_id: str,
    expected_input_digest: str | None = None,
    expected_captured_at: str | None = None,
) -> dict[str, Any]:
    projection = _read_object(root / filename)
    if (
        set(projection) != _PROJECTION_STAGE_FIELDS[stage]
        or projection.get("contract_name") != contract_name
        or projection.get("contract_version") != 1
        or projection.get("stage") != stage
        or projection.get("snapshot_id") != snapshot_id
        or projection.get("boundary") != _BOUNDARY
    ):
        raise AuthorityCadencePredicateError(f"{stage} projection identity is invalid")
    unsigned = {key: value for key, value in projection.items() if key != "projection_digest"}
    if projection.get("projection_digest") != contract_digest(unsigned):
        raise AuthorityCadencePredicateError(f"{stage} projection digest is invalid")
    owner_input_digest = projection.get("owner_input_digest")
    if not _is_digest(owner_input_digest) or (
        expected_input_digest is not None and owner_input_digest != expected_input_digest
    ):
        raise AuthorityCadencePredicateError(f"{stage} projection input digest is invalid")
    if expected_captured_at is not None and projection.get("captured_at") != expected_captured_at:
        raise AuthorityCadencePredicateError(f"{stage} projection capture time is invalid")
    expected = _verify_transaction(root, snapshot_id=snapshot_id)
    for field, value in expected.items():
        if projection.get(field) != value:
            raise AuthorityCadencePredicateError(f"{stage} projection {field} is not owner-derived")
    readiness = projection["readiness"]
    if readiness.get("exact_all") is not True:
        raise AuthorityCadencePredicateError(f"{stage} predicate requires exact classification")
    return projection


def _parse_input_digest(
    *,
    source_root: Path,
    source_census: Path,
    provider_manifest: Path,
    snapshot_id: str,
    captured_at: str,
    custody_pointer: str,
) -> str:
    return contract_digest(
        {
            "source_root": _tree_digest(source_root),
            "source_census": _file_digest(source_census),
            "provider_manifest": _file_digest(provider_manifest),
            "snapshot_id": snapshot_id,
            "captured_at": captured_at,
            "custody_pointer_digest": contract_digest(custody_pointer),
        }
    )


def _stage_output_digest(root: Path, projection_name: str) -> str:
    projection_path = root / projection_name
    return contract_digest(
        {
            "artifacts": _descriptors(root),
            "projection": {
                "reference": projection_name,
                "digest": _file_digest(projection_path),
                "size_bytes": projection_path.stat().st_size,
            },
        }
    )


def assert_parse_predicate(
    *,
    source_root: Path,
    source_census: Path,
    provider_manifest: Path,
    output_root: Path,
    snapshot_id: str,
    captured_at: str,
    custody_pointer: str,
) -> None:
    _verify_projection(
        output_root,
        filename=PARSE_PROJECTION,
        contract_name="cce-authority-parse-projection.v1",
        stage="parse",
        snapshot_id=snapshot_id,
        expected_input_digest=_parse_input_digest(
            source_root=source_root,
            source_census=source_census,
            provider_manifest=provider_manifest,
            snapshot_id=snapshot_id,
            captured_at=captured_at,
            custody_pointer=custody_pointer,
        ),
        expected_captured_at=captured_at,
    )


def assert_classify_predicate(
    *,
    input_root: Path,
    output_root: Path,
    snapshot_id: str,
    captured_at: str,
) -> None:
    parse_projection = _verify_projection(
        input_root,
        filename=PARSE_PROJECTION,
        contract_name="cce-authority-parse-projection.v1",
        stage="parse",
        snapshot_id=snapshot_id,
        expected_captured_at=captured_at,
    )
    classify_projection = _verify_projection(
        output_root,
        filename=CLASSIFY_PROJECTION,
        contract_name="cce-authority-classify-projection.v1",
        stage="classify",
        snapshot_id=snapshot_id,
        expected_input_digest=_stage_output_digest(input_root, PARSE_PROJECTION),
        expected_captured_at=captured_at,
    )
    if (
        classify_projection.get("source_parse_projection_digest")
        != parse_projection.get("projection_digest")
        or classify_projection.get("captured_at") != parse_projection.get("captured_at")
        or _descriptors(input_root) != _descriptors(output_root)
    ):
        raise AuthorityCadencePredicateError(
            "classify predicate requires byte-identical parse projection"
        )


__all__ = [
    "AuthorityCadencePredicateError",
    "assert_classify_predicate",
    "assert_parse_predicate",
]
