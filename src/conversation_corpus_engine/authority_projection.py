from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import rfc8785

from .source_adapter_registry import RedactedSourceRecord

_ROLE_POLICY: dict[str, tuple[str, str, str]] = {
    "operator": ("operator_intent", "operator_intent", "operator_intent"),
    "assistant": ("artifact", "assistant_artifact", "artifact"),
    "system": ("system_metadata", "system_instruction", "artifact"),
    "tool": ("transport_echo", "tool_echo", "artifact"),
    "continuation_summary": (
        "system_metadata",
        "continuation_summary",
        "artifact",
    ),
    "memory_summary": ("unknown", "memory_summary", "artifact"),
    "unknown": ("unknown", "unknown_role", "artifact"),
}
_ROLE_ALIASES = {"user": "operator", "human": "operator"}
_SPECIAL_KINDS = {"continuation_summary", "memory_summary"}
_SAFE_SOURCE_INSTANCE = re.compile(r"^[A-Za-z0-9_.:-]+$")


class AuthorityProjectionError(ValueError):
    """A parsed event cannot satisfy the authority projection contract."""


@dataclass
class ProjectionCandidate:
    record: RedactedSourceRecord
    envelope: dict[str, Any]
    event: dict[str, Any]
    authority_policy: str


def canonical_json_bytes(value: Any) -> bytes:
    """Return the exact RFC 8785/JCS representation used by digest contracts."""
    return rfc8785.dumps(value)


def canonical_bytes(value: Any) -> bytes:
    """Return one canonical JSONL record; the record delimiter is not digest input."""
    return canonical_json_bytes(value) + b"\n"


def sha256_contract(value: Any) -> str:
    return f"sha256:{hashlib.sha256(canonical_json_bytes(value)).hexdigest()}"


def body_hash(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def normalize_timestamp(value: str) -> str:
    candidate = value.strip()
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuthorityProjectionError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise AuthorityProjectionError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_role(role: str, kind: str) -> str:
    normalized_kind = kind.strip().lower().replace("-", "_")
    if normalized_kind in _SPECIAL_KINDS:
        return normalized_kind
    normalized_role = role.strip().lower().replace("-", "_")
    normalized_role = _ROLE_ALIASES.get(normalized_role, normalized_role)
    return normalized_role if normalized_role in _ROLE_POLICY else "unknown"


def _source_id(identity: dict[str, Any]) -> str:
    digest = hashlib.sha256(canonical_json_bytes(identity)).hexdigest()
    return f"src_{digest}"


def _raw_unit_id(record: RedactedSourceRecord) -> str:
    if record.raw_unit_ids:
        return record.raw_unit_ids[0]
    legacy_identity = {
        "source_family": record.source_family,
        "blob_shas": list(record.blob_shas),
        "session_id": record.session_id,
    }
    return f"raw_{hashlib.sha256(canonical_json_bytes(legacy_identity)).hexdigest()}"


def build_projection_candidate(
    record: RedactedSourceRecord,
    provider: dict[str, Any],
    *,
    snapshot_id: str,
    captured_at: str,
    snapshot_hash: str,
    custody_pointer: str,
    raw_unit_content_hash: str,
) -> ProjectionCandidate:
    authority_policy = provider.get("authority_policy")
    if authority_policy not in {"manifest-lane", "native-role"}:
        raise AuthorityProjectionError(f"unsupported authority_policy: {authority_policy!r}")
    normalized_timestamp = normalize_timestamp(record.event_timestamp)
    normalized_captured_at = normalize_timestamp(captured_at)
    normalized_role = normalize_role(record.role, record.kind)
    authority_class, authority_detail, origin_lane = _ROLE_POLICY[normalized_role]
    if authority_policy == "manifest-lane":
        manifest_lane = record.metadata.get("manifest_lane")
        if manifest_lane is None:
            raise AuthorityProjectionError(
                "manifest-lane authority_policy requires manifest_lane metadata"
            )
        if manifest_lane != "artifact":
            raise AuthorityProjectionError(
                "manifest_lane must be 'artifact' for manifest-lane authority_policy"
            )
        authority_class = "artifact"
        authority_detail = "manifested_artifact"
        origin_lane = "artifact"
    elif (
        record.metadata.get("transport_classification") == "adapter-native"
        and record.metadata.get("authority_class") == "transport_echo"
    ):
        authority_class = "transport_echo"
        authority_detail = "adapter_native_transport_echo"
        origin_lane = "artifact"
    elif (
        record.metadata.get("transport_classification") == "adapter-native"
        and record.metadata.get("authority_class") == "system_metadata"
    ):
        authority_class = "system_metadata"
        authority_detail = "adapter_native_system_metadata"
        origin_lane = "artifact"
    native_content_hash = record.metadata.get("native_content_hash")
    event_body_hash = (
        native_content_hash if isinstance(native_content_hash, str) else body_hash(record.text)
    )
    source_instance = record.metadata.get("source_instance")
    if not isinstance(source_instance, str) or not source_instance.strip():
        source_instance = f"{provider['provider_id']}-session-meta"
    elif not _SAFE_SOURCE_INSTANCE.fullmatch(source_instance):
        raise AuthorityProjectionError("source_instance must be a safe identifier")
    format_adapter = record.metadata.get("format_adapter")
    if not isinstance(format_adapter, str) or not format_adapter.strip():
        format_adapter = provider["default_adapter_id"]
    native_identifiers = dict(record.native_identifiers)
    native_identity_namespace = record.metadata.get("native_identity_namespace")
    if not isinstance(native_identity_namespace, str) or not native_identity_namespace.strip():
        native_identity_namespace = f"{format_adapter}:{source_instance}"
    identity_basis = {
        "native_identity_namespace": native_identity_namespace,
        "native_identifiers": native_identifiers,
        "native_role": record.role,
        "content_hash": event_body_hash,
    }
    event_digest = hashlib.sha256(canonical_json_bytes(identity_basis)).hexdigest()
    event_id = f"evt_{event_digest}"
    source_id = _source_id(identity_basis)
    raw_unit_id = _raw_unit_id(record)
    provider_metadata = {
        "provider_id": provider["provider_id"],
        "source": record.source_family,
        "source_role": record.role,
        "kind": record.kind,
        "source_content_sha": f"sha256:{record.content_sha}",
        "authority_detail": authority_detail,
        "origin_lane": origin_lane,
        "effective_lane": origin_lane,
        "adoption_state": "not_applicable" if normalized_role != "assistant" else "not_adopted",
    }
    envelope = {
        "contract_name": "source-envelope.v1",
        "contract_version": 1,
        "source_id": source_id,
        "source_family": record.source_family,
        "source_instance": source_instance,
        "format_adapter": format_adapter,
        "raw_unit_id": raw_unit_id,
        "raw_unit_content_hash": raw_unit_content_hash,
        "custody_snapshot": {
            "snapshot_id": snapshot_id,
            "captured_at": normalized_captured_at,
            "snapshot_hash": snapshot_hash,
            "custody_pointer": custody_pointer,
            "immutable": True,
        },
        "native_identifiers": native_identifiers,
        "role": normalized_role,
        "event_timestamp": normalized_timestamp,
        "ingestion_timestamp": normalized_captured_at,
        "authority_class": authority_class,
        "body_hash": event_body_hash,
        "private_custody_pointer": f"{custody_pointer}#atom:{record.atom_id}",
        "historical_branding": provider["display_name"],
        "redacted_projection_pointer": f"source-envelope.v1.jsonl#{source_id}",
        "provider_metadata": provider_metadata,
    }
    if record.attachments:
        envelope["attachments"] = list(record.attachments)
    event = {
        "contract_name": "normalized-event.v1",
        "contract_version": 1,
        "event_id": event_id,
        "identity_algorithm": "sha256-canonical-json-native-identity-role-content-v1",
        "identity_basis": identity_basis,
        "snapshot_id": snapshot_id,
        "snapshot_digest": snapshot_hash,
        "raw_unit_id": raw_unit_id,
        "raw_unit_content_hash": raw_unit_content_hash,
        "source_family": record.source_family,
        "source_instance": source_instance,
        "format_adapter": format_adapter,
        "normalized_role": normalized_role,
        "occurred_at": normalized_timestamp,
        "authority_class": authority_class,
        "source_envelope_reference": f"source-envelope.v1.jsonl#{source_id}",
        "redacted_content_pointer": f"{custody_pointer}#atom:{record.atom_id}",
        "transport_metadata": {
            "provider_id": provider["provider_id"],
            "source_role": record.role,
            "kind": record.kind,
            "source_content_sha": f"sha256:{record.content_sha}",
            "authority_detail": authority_detail,
            "origin_lane": origin_lane,
            "effective_lane": origin_lane,
            "adoption_state": provider_metadata["adoption_state"],
            "input_reference": record.input_reference,
            "session_id": record.session_id,
            "ordinal": record.ordinal,
            "all_raw_unit_ids": list(record.raw_unit_ids) or [raw_unit_id],
        },
        "evidence_references": [
            record.input_reference,
            f"custody:{custody_pointer}",
            f"raw-unit:{raw_unit_id}",
        ],
    }
    return ProjectionCandidate(
        record=record,
        envelope=envelope,
        event=event,
        authority_policy=authority_policy,
    )


def apply_reviewed_adoptions(candidates: list[ProjectionCandidate]) -> None:
    operator_adoptions: dict[str, ProjectionCandidate] = {}
    for candidate in candidates:
        metadata = candidate.record.metadata
        if (
            candidate.event["normalized_role"] == "operator"
            and candidate.event["transport_metadata"]["kind"] == "adoption"
            and metadata.get("review_state") == "reviewed"
            and isinstance(metadata.get("adopts_atom_id"), str)
        ):
            operator_adoptions[candidate.record.atom_id] = candidate

    for candidate in candidates:
        if candidate.event["normalized_role"] != "assistant":
            continue
        if candidate.authority_policy == "manifest-lane":
            continue
        adoption_event_id = candidate.record.metadata.get("adoption_event_id")
        if not isinstance(adoption_event_id, str):
            continue
        adoption = operator_adoptions.get(adoption_event_id)
        if adoption is None:
            candidate.event["transport_metadata"]["adoption_state"] = "unverified_reference"
            candidate.envelope["provider_metadata"]["adoption_state"] = "unverified_reference"
            continue
        if adoption.record.metadata.get("adopts_atom_id") != candidate.record.atom_id:
            candidate.event["transport_metadata"]["adoption_state"] = "mismatched_reference"
            candidate.envelope["provider_metadata"]["adoption_state"] = "mismatched_reference"
            continue
        candidate.event["transport_metadata"]["effective_lane"] = "operator_intent"
        candidate.event["transport_metadata"]["adoption_state"] = "reviewed_operator_adoption"
        candidate.event["transport_metadata"]["adoption_evidence_event_id"] = adoption.event[
            "event_id"
        ]
        candidate.envelope["provider_metadata"]["effective_lane"] = "operator_intent"
        candidate.envelope["provider_metadata"]["adoption_state"] = "reviewed_operator_adoption"
        candidate.envelope["provider_metadata"]["adoption_evidence_event_id"] = adoption.event[
            "event_id"
        ]
