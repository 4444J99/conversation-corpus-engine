from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from .authority_ingest import contract_digest, ingest_authority_bundle
from .authority_projection import canonical_bytes

PARSE_PROJECTION = "cce-authority-parse-projection.v1.json"
CLASSIFY_PROJECTION = "cce-authority-classify-projection.v1.json"
CORE_ARTIFACTS: tuple[tuple[str, str, str], ...] = (
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
_READINESS_DEBT_FIELDS = (
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
_DIGEST_PREFIX = "sha256:"
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


class AuthorityCadenceError(ValueError):
    """An owner-native parse/classify cadence projection is invalid."""


def _is_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(_DIGEST_PREFIX)
        and len(value) == len(_DIGEST_PREFIX) + 64
        and set(value.removeprefix(_DIGEST_PREFIX)) <= _HEX
    )


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"{_DIGEST_PREFIX}{digest.hexdigest()}"


def _write_canonical(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(canonical_bytes(payload))
    temporary.replace(path)


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthorityCadenceError(f"{path.name} is unreadable or malformed") from exc
    if not isinstance(value, dict):
        raise AuthorityCadenceError(f"{path.name} must contain an object")
    return value


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise AuthorityCadenceError(f"{path.name} is unreadable") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AuthorityCadenceError(f"{path.name}:{line_number} is malformed JSONL") from exc
        if not isinstance(row, dict):
            raise AuthorityCadenceError(f"{path.name}:{line_number} must contain an object")
        rows.append(row)
    return rows


def _artifact_descriptors(root: Path) -> list[dict[str, Any]]:
    descriptors: list[dict[str, Any]] = []
    for artifact_id, filename, contract in CORE_ARTIFACTS:
        path = root / filename
        if not path.is_file():
            raise AuthorityCadenceError(f"authority transaction is missing {filename}")
        if filename.endswith(".jsonl"):
            record_count = len(_load_jsonl(path))
        else:
            _load_object(path)
            record_count = 1
        descriptors.append(
            {
                "artifact_id": artifact_id,
                "reference": filename,
                "contract": contract,
                "digest": _file_digest(path),
                "size_bytes": path.stat().st_size,
                "record_count": record_count,
            }
        )
    return descriptors


def _combined_readiness(coverage: dict[str, Any], parity: dict[str, Any]) -> dict[str, Any]:
    parity_readiness = parity.get("readiness")
    if not isinstance(parity_readiness, dict):
        raise AuthorityCadenceError("normalization parity receipt has no typed readiness")
    exact_all = coverage.get("exact_all") is True and parity_readiness.get("exact_all") is True
    readiness: dict[str, Any] = {"exact_all": exact_all}
    for field in _READINESS_DEBT_FIELDS:
        values: list[str] = []
        for owner in (coverage, parity_readiness):
            debt = owner.get(field)
            if not isinstance(debt, list) or not all(
                isinstance(item, str) and item.strip() for item in debt
            ):
                raise AuthorityCadenceError(f"authority readiness {field} is invalid")
            values.extend(debt)
        readiness[field] = sorted(set(values))
    readiness["ready"] = exact_all and not any(readiness[field] for field in _READINESS_DEBT_FIELDS)
    readiness["status"] = (
        "ready"
        if readiness["ready"]
        else "closed_with_owner_routed_debt"
        if exact_all
        else "incomplete"
    )
    return readiness


def _classification_summary(root: Path) -> dict[str, Any]:
    events = _load_jsonl(root / "normalized-events.v1.jsonl")
    envelopes = _load_jsonl(root / "source-envelope.v1.jsonl")
    parity = _load_object(root / "normalization-parity-receipt.v1.json")
    return {
        "event_count": len(events),
        "source_envelope_count": len(envelopes),
        "promotion_count": len(parity.get("promotions", [])),
        "authority_class_counts": dict(
            sorted(Counter(str(row.get("authority_class")) for row in events).items())
        ),
        "normalized_role_counts": dict(
            sorted(Counter(str(row.get("normalized_role")) for row in events).items())
        ),
    }


def _validate_coverage_semantics(coverage: dict[str, Any]) -> None:
    sources = coverage.get("sources")
    if not isinstance(sources, list) or not sources:
        raise AuthorityCadenceError("coverage receipt has no classified denominator")
    source_ids = [row.get("source_id") for row in sources if isinstance(row, dict)]
    if len(source_ids) != len(sources) or len(source_ids) != len(set(source_ids)):
        raise AuthorityCadenceError("coverage source identities must be complete and unique")
    counts = Counter(str(row.get("status")) for row in sources)
    expected_counts = {status: counts.get(status, 0) for status in _COVERAGE_STATUSES}
    if coverage.get("counts") != expected_counts or sum(expected_counts.values()) != len(sources):
        raise AuthorityCadenceError("coverage counts do not classify every source exactly once")
    residuals = [row for row in sources if row.get("status") != "parsed"]
    routed = all(
        all(
            isinstance(row.get(field), str) and row[field].strip()
            for field in (
                "owner_reference",
                "failed_predicate",
                "next_action",
            )
        )
        for row in residuals
    )
    exact_all = routed
    if coverage.get("exact_all") is not exact_all:
        raise AuthorityCadenceError("coverage exact_all is not owner-derived")
    debt_free = all(not coverage.get(field) for field in _READINESS_DEBT_FIELDS)
    ready = exact_all and not residuals and debt_free
    if coverage.get("ready") is not ready:
        raise AuthorityCadenceError("coverage ready is not owner-derived")


def _validate_parity_semantics(parity: dict[str, Any], event_ids: list[str]) -> None:
    input_census = parity.get("input_census")
    promotions = parity.get("promotions")
    if not isinstance(input_census, dict) or not isinstance(promotions, list) or not promotions:
        raise AuthorityCadenceError("normalization parity denominator is incomplete")
    raw_unit_ids = input_census.get("raw_unit_ids")
    if (
        not isinstance(raw_unit_ids, list)
        or not raw_unit_ids
        or len(raw_unit_ids) != len(set(raw_unit_ids))
    ):
        raise AuthorityCadenceError("normalization parity raw-unit denominator is invalid")
    promotion_ids = [
        promotion.get("raw_unit_id") for promotion in promotions if isinstance(promotion, dict)
    ]
    if (
        len(promotion_ids) != len(promotions)
        or len(promotion_ids) != len(set(promotion_ids))
        or set(promotion_ids) != set(raw_unit_ids)
    ):
        raise AuthorityCadenceError("normalization parity promotions are not a complete crosswalk")
    promoted_event_ids: set[str] = set()
    for promotion in promotions:
        promoted = promotion.get("event_ids")
        disposition = promotion.get("disposition")
        if isinstance(promoted, list) and promoted and disposition is None:
            if len(promoted) != len(set(promoted)) or any(
                event_id not in event_ids for event_id in promoted
            ):
                raise AuthorityCadenceError("normalization parity promotion event IDs are invalid")
            promoted_event_ids.update(promoted)
        elif not (
            promoted is None
            and isinstance(disposition, dict)
            and all(
                isinstance(disposition.get(field), str) and disposition[field].strip()
                for field in ("type", "owner_reference", "failed_predicate", "next_action")
            )
        ):
            raise AuthorityCadenceError(
                "normalization parity requires one event mapping or owner disposition"
            )
    exact_all = sorted(promoted_event_ids) == sorted(event_ids)
    readiness = parity.get("readiness")
    if not isinstance(readiness, dict) or readiness.get("exact_all") is not exact_all:
        raise AuthorityCadenceError("normalization parity exact_all is not owner-derived")
    debt_free = all(not readiness.get(field) for field in _READINESS_DEBT_FIELDS)
    if readiness.get("ready") is not (exact_all and debt_free):
        raise AuthorityCadenceError("normalization parity ready is not owner-derived")


def _validate_transaction(root: Path, *, snapshot_id: str) -> dict[str, Any]:
    descriptors = _artifact_descriptors(root)
    events = _load_jsonl(root / "normalized-events.v1.jsonl")
    envelopes = _load_jsonl(root / "source-envelope.v1.jsonl")
    coverage = _load_object(root / "coverage-receipt.v1.json")
    parity = _load_object(root / "normalization-parity-receipt.v1.json")
    snapshot_digest = parity.get("snapshot_digest")
    if not _is_digest(snapshot_digest):
        raise AuthorityCadenceError("normalization parity snapshot digest is invalid")
    raw_event_ids = [row.get("event_id") for row in events]
    envelope_by_id = {row.get("source_id"): row for row in envelopes}
    source_ids = set(envelope_by_id)
    if (
        coverage.get("snapshot_id") != snapshot_id
        or parity.get("snapshot_id") != snapshot_id
        or any(row.get("snapshot_id") != snapshot_id for row in events)
        or any(row.get("snapshot_digest") != snapshot_digest for row in events)
        or any(
            row.get("custody_snapshot", {}).get("snapshot_id") != snapshot_id for row in envelopes
        )
        or any(
            row.get("custody_snapshot", {}).get("snapshot_hash") != snapshot_digest
            for row in envelopes
        )
    ):
        raise AuthorityCadenceError("authority transaction is not bound to the requested snapshot")
    if coverage.get("receipt_hash") != contract_digest(
        {key: value for key, value in coverage.items() if key != "receipt_hash"}
    ):
        raise AuthorityCadenceError("coverage receipt digest is invalid")
    if parity.get("receipt_digest") != contract_digest(
        {key: value for key, value in parity.items() if key != "receipt_digest"}
    ):
        raise AuthorityCadenceError("normalization parity receipt digest is invalid")
    if len(raw_event_ids) != len(set(raw_event_ids)) or any(
        not isinstance(event_id, str) or not event_id for event_id in raw_event_ids
    ):
        raise AuthorityCadenceError("normalized event identities must be nonempty and unique")
    event_ids = [str(event_id) for event_id in raw_event_ids]
    if len(envelope_by_id) != len(envelopes):
        raise AuthorityCadenceError("source envelope identities must be unique")
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
        raise AuthorityCadenceError("normalized event contract fields are invalid")
    if any(
        row.get("contract_name") != "source-envelope.v1"
        or row.get("contract_version") != 1
        or row.get("authority_class") not in _AUTHORITY_CLASSES
        or row.get("role") not in _NORMALIZED_ROLES
        for row in envelopes
    ):
        raise AuthorityCadenceError("source envelope contract fields are invalid")
    if any(
        str(row.get("source_envelope_reference", "")).rpartition("#")[2] not in source_ids
        for row in events
    ):
        raise AuthorityCadenceError("normalized event references an absent source envelope")
    for event in events:
        envelope_id = str(event["source_envelope_reference"]).rpartition("#")[2]
        envelope = envelope_by_id[envelope_id]
        if (
            event.get("raw_unit_id") != envelope.get("raw_unit_id")
            or event.get("raw_unit_content_hash") != envelope.get("raw_unit_content_hash")
            or event.get("identity_basis", {}).get("content_hash") != envelope.get("body_hash")
        ):
            raise AuthorityCadenceError("event and source envelope immutable bindings differ")
    output_events = parity.get("output_events")
    if not isinstance(output_events, dict):
        raise AuthorityCadenceError("normalization parity has no output event binding")
    if sorted(event_ids) != sorted(output_events.get("event_ids", [])):
        raise AuthorityCadenceError("normalization parity event IDs differ from the event set")
    if output_events.get("event_set_digest") != contract_digest(events):
        raise AuthorityCadenceError("normalization parity event digest differs from the event set")
    _validate_coverage_semantics(coverage)
    _validate_parity_semantics(parity, event_ids)
    return {
        "artifacts": descriptors,
        "readiness": _combined_readiness(coverage, parity),
        "classification_summary": _classification_summary(root),
        "snapshot_digest": snapshot_digest,
    }


def _unsigned_projection(document: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key != "projection_digest"}


def _validate_projection(
    projection_path: Path,
    *,
    contract_name: str,
    stage: str,
    snapshot_id: str,
    expected_input_digest: str | None = None,
    expected_captured_at: str | None = None,
) -> dict[str, Any]:
    root = projection_path.parent
    projection = _load_object(projection_path)
    if (
        set(projection) != _PROJECTION_STAGE_FIELDS[stage]
        or projection.get("contract_name") != contract_name
        or projection.get("contract_version") != 1
        or projection.get("stage") != stage
        or projection.get("snapshot_id") != snapshot_id
    ):
        raise AuthorityCadenceError(f"{stage} projection identity is invalid")
    if projection.get("projection_digest") != contract_digest(_unsigned_projection(projection)):
        raise AuthorityCadenceError(f"{stage} projection digest is invalid")
    owner_input_digest = projection.get("owner_input_digest")
    if not _is_digest(owner_input_digest) or (
        expected_input_digest is not None and owner_input_digest != expected_input_digest
    ):
        raise AuthorityCadenceError(f"{stage} projection input digest is invalid")
    if expected_captured_at is not None and projection.get("captured_at") != expected_captured_at:
        raise AuthorityCadenceError(f"{stage} projection capture time is invalid")
    transaction = _validate_transaction(root, snapshot_id=snapshot_id)
    for field in ("artifacts", "readiness", "classification_summary", "snapshot_digest"):
        if projection.get(field) != transaction[field]:
            raise AuthorityCadenceError(f"{stage} projection {field} differs from owner artifacts")
    boundary = projection.get("boundary")
    if boundary != {
        "implementation": "single-authority-ingest-transaction",
        "classification_materialized_during_parse": True,
        "classify_operation": "digest-verified-byte-projection",
    }:
        raise AuthorityCadenceError(f"{stage} projection has an invalid parse/classify boundary")
    return projection


def verify_authority_parse_projection(
    *,
    output_root: Path,
    snapshot_id: str,
    expected_input_digest: str | None = None,
    expected_captured_at: str | None = None,
) -> dict[str, Any]:
    return _validate_projection(
        output_root / PARSE_PROJECTION,
        contract_name="cce-authority-parse-projection.v1",
        stage="parse",
        snapshot_id=snapshot_id,
        expected_input_digest=expected_input_digest,
        expected_captured_at=expected_captured_at,
    )


def verify_authority_classify_projection(
    *,
    input_root: Path,
    output_root: Path,
    snapshot_id: str,
) -> dict[str, Any]:
    parse_projection = verify_authority_parse_projection(
        output_root=input_root,
        snapshot_id=snapshot_id,
    )
    classify_projection = _validate_projection(
        output_root / CLASSIFY_PROJECTION,
        contract_name="cce-authority-classify-projection.v1",
        stage="classify",
        snapshot_id=snapshot_id,
    )
    if classify_projection.get("source_parse_projection_digest") != parse_projection.get(
        "projection_digest"
    ):
        raise AuthorityCadenceError("classify projection is not bound to the parse projection")
    if classify_projection.get("captured_at") != parse_projection.get("captured_at"):
        raise AuthorityCadenceError("classify projection capture time differs from parse")
    input_descriptors = _artifact_descriptors(input_root)
    expected_input_digest = _stage_output_digest(input_root, PARSE_PROJECTION)
    if classify_projection.get("owner_input_digest") != expected_input_digest:
        raise AuthorityCadenceError("classify projection input digest differs from parse artifacts")
    if classify_projection.get("artifacts") != input_descriptors:
        raise AuthorityCadenceError(
            "classify projection is not a byte-identical projection of parse artifacts"
        )
    return classify_projection


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
        raise AuthorityCadenceError(f"cadence input does not exist: {path}")
    files = [
        {
            "relative_path": item.relative_to(resolved).as_posix(),
            "digest": _file_digest(item),
            "size_bytes": item.stat().st_size,
        }
        for item in sorted(candidate for candidate in resolved.rglob("*") if candidate.is_file())
    ]
    if not files:
        raise AuthorityCadenceError("cadence source root must contain at least one file")
    return contract_digest({"kind": "directory", "files": files})


def _ensure_disjoint_roots(input_root: Path, output_root: Path) -> None:
    source = input_root.resolve()
    output = output_root.resolve()
    if source == output or (source.is_dir() and output.is_relative_to(source)):
        raise AuthorityCadenceError("cadence output root must not be inside its input root")


def _stage_output_digest(root: Path, projection_name: str) -> str:
    projection_path = root / projection_name
    return contract_digest(
        {
            "artifacts": _artifact_descriptors(root),
            "projection": {
                "reference": projection_name,
                "digest": _file_digest(projection_path),
                "size_bytes": projection_path.stat().st_size,
            },
        }
    )


def _max_items() -> int:
    maximum = os.environ.get("LIMEN_GOV_MAX_ITEMS")
    try:
        max_items = int(maximum or "0")
    except ValueError as exc:
        raise AuthorityCadenceError("LIMEN_GOV_MAX_ITEMS must be an integer") from exc
    if max_items < 1:
        raise AuthorityCadenceError("LIMEN_GOV_MAX_ITEMS must allow the authority transaction")
    return max_items


def _metrics_path() -> Path:
    value = os.environ.get("LIMEN_GOV_STAGE_METRICS_OUT")
    if not value:
        raise AuthorityCadenceError("LIMEN_GOV_STAGE_METRICS_OUT is required")
    _max_items()
    return Path(value)


def _assert_item_bound(item_count: int) -> None:
    if item_count > _max_items():
        raise AuthorityCadenceError(
            f"authority stage contains {item_count} items, above LIMEN_GOV_MAX_ITEMS"
        )


def _write_metrics(
    *,
    stage: str,
    snapshot_id: str,
    input_digest: str,
    output_digest: str,
    emitted_events: int,
) -> None:
    metrics_path = _metrics_path()
    proof_mode = os.environ.get("LIMEN_GOV_PROOF_MODE") == "1"
    runtime_snapshot_id = os.environ.get("LIMEN_GOV_SNAPSHOT_ID")
    if runtime_snapshot_id not in {None, "", snapshot_id}:
        raise AuthorityCadenceError("cadence runtime snapshot differs from the owner command")
    child_id = f"cce-authority-{stage}-{snapshot_id}"
    child: dict[str, Any] = {
        "child_id": child_id,
        "status": "completed",
        "input_digest": input_digest,
        "output_digest": output_digest,
    }
    if proof_mode:
        prior_path = Path(os.environ.get("LIMEN_GOV_PRIOR_STAGE_RECEIPT", ""))
        prior = _load_object(prior_path)
        prior_children = prior.get("child_receipts")
        if (
            prior.get("stage") != stage
            or not isinstance(prior_children, list)
            or len(prior_children) != 1
            or not isinstance(prior_children[0], dict)
        ):
            raise AuthorityCadenceError("proof mode requires the exact prior stage child receipt")
        prior_child = prior_children[0]
        if (
            prior_child.get("child_id") != child_id
            or prior_child.get("input_digest") != input_digest
            or prior_child.get("output_digest") != output_digest
        ):
            raise AuthorityCadenceError(
                "proof inputs or outputs differ from the prior child receipt"
            )
        child["status"] = "skipped_completed"
        child["prior_receipt_digest"] = contract_digest(prior_child)
        emitted_events = 0
    _write_canonical(
        metrics_path,
        {
            "resume_token": None,
            "completed_child_ids": [child_id],
            "pending_child_ids": [],
            "child_receipts": [child],
            "emitted_events": emitted_events,
        },
    )


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


def _build_parse_projection(
    *,
    root: Path,
    result: dict[str, Any],
    snapshot_id: str,
    captured_at: str,
    input_digest: str,
) -> dict[str, Any]:
    transaction = _validate_transaction(root, snapshot_id=snapshot_id)
    projection = {
        "contract_name": "cce-authority-parse-projection.v1",
        "contract_version": 1,
        "stage": "parse",
        "snapshot_id": snapshot_id,
        "snapshot_digest": result["snapshot_hash"],
        "owner_input_digest": input_digest,
        "captured_at": captured_at,
        "source_census_id": result["source_census_id"],
        "provider_manifest_digest": result["provider_manifest_hash"],
        "boundary": {
            "implementation": "single-authority-ingest-transaction",
            "classification_materialized_during_parse": True,
            "classify_operation": "digest-verified-byte-projection",
        },
        **transaction,
    }
    projection["projection_digest"] = contract_digest(projection)
    return projection


def _assert_parse_proof_bytes(
    proof_root: Path,
    governed_root: Path,
    proof_projection: dict[str, Any],
) -> None:
    for _, filename, _ in CORE_ARTIFACTS:
        if (proof_root / filename).read_bytes() != (governed_root / filename).read_bytes():
            raise AuthorityCadenceError(f"parse proof changed governed artifact bytes: {filename}")
    governed_projection = governed_root / PARSE_PROJECTION
    if canonical_bytes(proof_projection) != governed_projection.read_bytes():
        raise AuthorityCadenceError("parse proof changed the governed projection bytes")


def run_authority_parse_stage(
    *,
    source_root: Path,
    source_census: Path,
    provider_manifest: Path,
    output_root: Path,
    snapshot_id: str,
    captured_at: str,
    custody_pointer: str,
) -> dict[str, Any]:
    if os.environ.get("LIMEN_GOV_STAGE") not in {None, "", "parse"}:
        raise AuthorityCadenceError("authority parse adapter may only own the parse stage")
    runtime_snapshot_at = os.environ.get("LIMEN_GOV_SNAPSHOT_AT")
    if runtime_snapshot_at not in {None, "", captured_at}:
        raise AuthorityCadenceError("cadence runtime capture time differs from the owner command")
    _ensure_disjoint_roots(source_root, output_root)
    input_digest = _parse_input_digest(
        source_root=source_root,
        source_census=source_census,
        provider_manifest=provider_manifest,
        snapshot_id=snapshot_id,
        captured_at=captured_at,
        custody_pointer=custody_pointer,
    )
    census_document = _load_object(source_census)
    raw_units = census_document.get("raw_units")
    if not isinstance(raw_units, list) or not raw_units:
        raise AuthorityCadenceError("parse source census has no bounded raw-unit denominator")
    _assert_item_bound(len(raw_units))
    projection_path = output_root / PARSE_PROJECTION
    proof_mode = os.environ.get("LIMEN_GOV_PROOF_MODE") == "1"
    emitted_events = 0
    if projection_path.is_file():
        try:
            projection = verify_authority_parse_projection(
                output_root=output_root,
                snapshot_id=snapshot_id,
                expected_input_digest=input_digest,
                expected_captured_at=captured_at,
            )
        except AuthorityCadenceError:
            if proof_mode:
                raise
            projection = {}
    else:
        projection = {}
    if proof_mode and projection:
        output_root.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".cce-authority-parse-proof-",
            dir=output_root.parent,
        ) as temporary:
            proof_root = Path(temporary)
            proof_result = ingest_authority_bundle(
                output_root=proof_root,
                source_root=source_root,
                source_census=source_census,
                provider_manifest=provider_manifest,
                snapshot_id=snapshot_id,
                captured_at=captured_at,
                custody_pointer=custody_pointer,
            )
            proof_projection = _build_parse_projection(
                root=proof_root,
                result=proof_result,
                snapshot_id=snapshot_id,
                captured_at=captured_at,
                input_digest=input_digest,
            )
            _assert_parse_proof_bytes(
                proof_root,
                output_root,
                proof_projection,
            )
    if not projection:
        if proof_mode:
            raise AuthorityCadenceError("proof mode cannot create missing parse outputs")
        output_root.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".cce-authority-parse-",
            dir=output_root.parent,
        ) as temporary:
            temporary_root = Path(temporary)
            result = ingest_authority_bundle(
                output_root=temporary_root,
                source_root=source_root,
                source_census=source_census,
                provider_manifest=provider_manifest,
                snapshot_id=snapshot_id,
                captured_at=captured_at,
                custody_pointer=custody_pointer,
            )
            output_root.mkdir(parents=True, exist_ok=True)
            for _, filename, _ in CORE_ARTIFACTS:
                (temporary_root / filename).replace(output_root / filename)
            projection = _build_parse_projection(
                root=output_root,
                result=result,
                snapshot_id=snapshot_id,
                captured_at=captured_at,
                input_digest=input_digest,
            )
            _write_canonical(projection_path, projection)
            emitted_events = int(projection["classification_summary"]["event_count"])
    output_digest = _stage_output_digest(output_root, PARSE_PROJECTION)
    _write_metrics(
        stage="parse",
        snapshot_id=snapshot_id,
        input_digest=input_digest,
        output_digest=output_digest,
        emitted_events=emitted_events,
    )
    return projection


def _copy_if_changed(source: Path, target: Path) -> None:
    if target.is_file() and _file_digest(target) == _file_digest(source):
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    shutil.copyfile(source, temporary)
    temporary.replace(target)


def run_authority_classify_stage(
    *,
    input_root: Path,
    output_root: Path,
    snapshot_id: str,
) -> dict[str, Any]:
    if os.environ.get("LIMEN_GOV_STAGE") not in {None, "", "classify"}:
        raise AuthorityCadenceError("authority classify adapter may only own the classify stage")
    _ensure_disjoint_roots(input_root, output_root)
    parse_projection = verify_authority_parse_projection(
        output_root=input_root,
        snapshot_id=snapshot_id,
    )
    runtime_snapshot_at = os.environ.get("LIMEN_GOV_SNAPSHOT_AT")
    if runtime_snapshot_at not in {None, "", parse_projection["captured_at"]}:
        raise AuthorityCadenceError("classify input capture time differs from cadence runtime")
    _assert_item_bound(int(parse_projection["classification_summary"]["promotion_count"]))
    input_digest = _stage_output_digest(input_root, PARSE_PROJECTION)
    projection_path = output_root / CLASSIFY_PROJECTION
    proof_mode = os.environ.get("LIMEN_GOV_PROOF_MODE") == "1"
    if proof_mode:
        projection = verify_authority_classify_projection(
            input_root=input_root,
            output_root=output_root,
            snapshot_id=snapshot_id,
        )
    else:
        output_root.mkdir(parents=True, exist_ok=True)
        for _, filename, _ in CORE_ARTIFACTS:
            _copy_if_changed(input_root / filename, output_root / filename)
        transaction = _validate_transaction(output_root, snapshot_id=snapshot_id)
        projection = {
            "contract_name": "cce-authority-classify-projection.v1",
            "contract_version": 1,
            "stage": "classify",
            "snapshot_id": snapshot_id,
            "snapshot_digest": parse_projection["snapshot_digest"],
            "owner_input_digest": input_digest,
            "captured_at": parse_projection["captured_at"],
            "source_parse_projection_digest": parse_projection["projection_digest"],
            "boundary": parse_projection["boundary"],
            **transaction,
        }
        projection["projection_digest"] = contract_digest(projection)
        _write_canonical(projection_path, projection)
        projection = verify_authority_classify_projection(
            input_root=input_root,
            output_root=output_root,
            snapshot_id=snapshot_id,
        )
    output_digest = _stage_output_digest(output_root, CLASSIFY_PROJECTION)
    _write_metrics(
        stage="classify",
        snapshot_id=snapshot_id,
        input_digest=input_digest,
        output_digest=output_digest,
        emitted_events=0,
    )
    return projection


__all__ = [
    "AuthorityCadenceError",
    "CLASSIFY_PROJECTION",
    "CORE_ARTIFACTS",
    "PARSE_PROJECTION",
    "run_authority_classify_stage",
    "run_authority_parse_stage",
    "verify_authority_classify_projection",
    "verify_authority_parse_projection",
]
