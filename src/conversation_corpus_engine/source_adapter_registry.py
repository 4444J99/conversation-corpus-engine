from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

SESSION_META_ADAPTER_ID = "session-meta-redacted-jsonl-v1"
_HEX_SHA256_LENGTH = 64


class SourceBundleError(ValueError):
    """A configured redacted source bundle cannot be read safely."""


@dataclass(frozen=True)
class RedactedSourceRecord:
    line_number: int
    input_reference: str
    source_family: str
    atom_id: str
    role: str
    text: str
    session_id: str
    ordinal: int
    kind: str
    event_timestamp: str
    content_sha: str
    blob_shas: tuple[str, ...]
    raw_unit_ids: tuple[str, ...]
    raw_unit_content_hashes: dict[str, str]
    native_identifiers: dict[str, str]
    attachments: tuple[dict[str, str], ...]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class AdapterDiagnostic:
    line_number: int
    input_reference: str
    body_hash: str
    code: str
    message: str


@dataclass(frozen=True)
class AdapterResult:
    source_path: Path
    source_hash: str
    records: tuple[RedactedSourceRecord, ...]
    diagnostics: tuple[AdapterDiagnostic, ...]
    nonblank_lines: int


SourceAdapter = Callable[[Path], AdapterResult]
_ADAPTERS: dict[str, SourceAdapter] = {}


def register_source_adapter(adapter_id: str) -> Callable[[SourceAdapter], SourceAdapter]:
    def decorator(function: SourceAdapter) -> SourceAdapter:
        if adapter_id in _ADAPTERS:
            raise RuntimeError(f"source adapter already registered: {adapter_id}")
        _ADAPTERS[adapter_id] = function
        return function

    return decorator


def available_source_adapters() -> tuple[str, ...]:
    return tuple(sorted(_ADAPTERS))


def read_source_bundle(adapter_id: str, source_root: Path) -> AdapterResult:
    try:
        adapter = _ADAPTERS[adapter_id]
    except KeyError as exc:
        raise SourceBundleError(f"unknown source adapter: {adapter_id}") from exc
    try:
        return adapter(source_root)
    except OSError as exc:
        raise SourceBundleError("configured redacted source bundle could not be inspected") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _source_content_sha(role: str, text: str) -> str:
    normalized = " ".join(text.split())
    return _sha256(f"{role}\x00{normalized}".encode("utf-8", "replace"))


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _HEX_SHA256_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _attachment_hash(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("sha256:") and _is_sha256(value[7:])


def _attachments(payload: dict[str, Any], kind: str) -> tuple[dict[str, str], ...]:
    raw_attachments = payload.get("attachments")
    if raw_attachments is None:
        metadata = payload.get("meta")
        raw_attachments = metadata.get("attachments", []) if isinstance(metadata, dict) else []
    if not isinstance(raw_attachments, list):
        raise SourceBundleError("attachments must be a list")
    if kind == "attachment_ref" and not raw_attachments:
        raise SourceBundleError("attachment_ref is missing attachment custody metadata")
    attachments: list[dict[str, str]] = []
    for raw in raw_attachments:
        if not isinstance(raw, dict):
            raise SourceBundleError("attachment entries must be objects")
        native_id = raw.get("native_id")
        attachment_body_hash = raw.get("body_hash")
        custody_pointer = raw.get("custody_pointer")
        status = raw.get("status")
        if not isinstance(native_id, str) or not native_id.strip():
            raise SourceBundleError("attachment.native_id must be non-empty")
        if not isinstance(attachment_body_hash, str) or not _attachment_hash(attachment_body_hash):
            raise SourceBundleError("attachment.body_hash must be a SHA-256 contract hash")
        if not isinstance(custody_pointer, str) or not custody_pointer.strip():
            raise SourceBundleError("attachment.custody_pointer must be non-empty")
        if status != "parsed":
            raise SourceBundleError("attachment must be parsed before event projection")
        attachments.append(
            {
                "native_id": native_id.strip(),
                "body_hash": attachment_body_hash,
                "custody_pointer": custody_pointer.strip(),
                "status": "parsed",
            }
        )
    return tuple(sorted(attachments, key=lambda item: item["native_id"]))


def _required_string(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise SourceBundleError(f"{field} must be a non-empty string")
    return value.strip()


def _resolve_atoms_path(source_root: Path) -> Path:
    root = source_root.expanduser().resolve()
    if not root.exists():
        raise SourceBundleError("configured redacted source bundle does not exist")
    if root.is_file():
        return root
    candidates = [
        candidate
        for candidate in (
            root / "atoms.jsonl",
            root / "redacted-atoms.jsonl",
            root / "ingest" / "atoms.jsonl",
        )
        if candidate.is_file()
    ]
    if not candidates:
        raise SourceBundleError(
            "redacted bundle must contain atoms.jsonl, redacted-atoms.jsonl, or ingest/atoms.jsonl"
        )
    if len(candidates) > 1:
        raise SourceBundleError("redacted bundle contains multiple canonical atom files")
    return candidates[0]


def _diagnostic(raw: bytes, line_number: int, input_hash: str, message: str) -> AdapterDiagnostic:
    return AdapterDiagnostic(
        line_number=line_number,
        input_reference=f"bundle:sha256:{input_hash}#line:{line_number}",
        body_hash=f"sha256:{_sha256(raw)}",
        code="invalid-redacted-source-row",
        message=message,
    )


def _parse_record(
    payload: Any,
    *,
    line_number: int,
    input_reference: str,
) -> RedactedSourceRecord:
    if not isinstance(payload, dict):
        raise SourceBundleError("source row must be an object")
    source_family = _required_string(payload, "source")
    role = _required_string(payload, "role")
    text = _required_string(payload, "text")
    session_id = _required_string(payload, "session_id")
    event_timestamp = _required_string(payload, "ts")
    atom_id = payload.get("atom_id")
    if not isinstance(atom_id, str) or not atom_id.strip():
        atom_id = _source_content_sha(role, text)[:16]
    ordinal = payload.get("ordinal")
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
        raise SourceBundleError("ordinal must be a non-negative integer")
    kind = payload.get("kind", "message")
    if not isinstance(kind, str) or not kind.strip():
        raise SourceBundleError("kind must be a non-empty string")
    content_sha = payload.get("content_sha")
    if not _is_sha256(content_sha):
        raise SourceBundleError("content_sha must be a lowercase SHA-256 digest")
    if content_sha != _source_content_sha(role, text):
        raise SourceBundleError("content_sha does not match the redacted role and text")
    blob_shas = payload.get("blob_shas")
    if (
        not isinstance(blob_shas, list)
        or not blob_shas
        or any(not isinstance(value, str) or not value.strip() for value in blob_shas)
    ):
        raise SourceBundleError("blob_shas must be a non-empty string list")
    metadata = payload.get("meta", {})
    if not isinstance(metadata, dict):
        raise SourceBundleError("meta must be an object")
    native_content_hash = metadata.get("native_content_hash")
    if native_content_hash is not None and not (
        isinstance(native_content_hash, str)
        and native_content_hash.startswith("sha256:")
        and _is_sha256(native_content_hash[7:])
    ):
        raise SourceBundleError("meta.native_content_hash must be a SHA-256 contract hash")
    raw_unit_values = metadata.get("raw_unit_ids", [])
    if not isinstance(raw_unit_values, list) or any(
        not isinstance(value, str) or not value.startswith("raw_") for value in raw_unit_values
    ):
        raise SourceBundleError("meta.raw_unit_ids must contain canonical raw unit ids")
    raw_unit_content_hash_values = metadata.get("raw_unit_content_hashes", {})
    if not isinstance(raw_unit_content_hash_values, dict) or any(
        not isinstance(key, str)
        or not key.startswith("raw_")
        or not isinstance(value, str)
        or not value.startswith("sha256:")
        or not _is_sha256(value[7:])
        for key, value in raw_unit_content_hash_values.items()
    ):
        raise SourceBundleError(
            "meta.raw_unit_content_hashes must map canonical raw unit ids "
            "to SHA-256 contract hashes"
        )
    if raw_unit_content_hash_values and set(raw_unit_content_hash_values) != set(raw_unit_values):
        raise SourceBundleError(
            "meta.raw_unit_content_hashes keys must exactly match meta.raw_unit_ids"
        )
    native_identifiers_value = metadata.get("native_identifiers", {})
    if not isinstance(native_identifiers_value, dict) or any(
        not isinstance(key, str)
        or not key.strip()
        or not isinstance(value, str)
        or not value.strip()
        for key, value in native_identifiers_value.items()
    ):
        raise SourceBundleError("meta.native_identifiers must be a string map")
    native_identifiers = {
        str(key): str(value) for key, value in native_identifiers_value.items()
    } or {
        "session_id": session_id,
        "atom_id": atom_id.strip(),
        "content_identity": content_sha,
    }
    attachments = _attachments(payload, kind.strip())
    return RedactedSourceRecord(
        line_number=line_number,
        input_reference=input_reference,
        source_family=source_family,
        atom_id=atom_id.strip(),
        role=role,
        text=text,
        session_id=session_id,
        ordinal=ordinal,
        kind=kind.strip(),
        event_timestamp=event_timestamp,
        content_sha=content_sha,
        blob_shas=tuple(sorted(set(value.strip() for value in blob_shas))),
        raw_unit_ids=tuple(sorted(set(raw_unit_values))),
        raw_unit_content_hashes={
            str(key): str(value) for key, value in sorted(raw_unit_content_hash_values.items())
        },
        native_identifiers=native_identifiers,
        attachments=attachments,
        metadata=metadata,
    )


@register_source_adapter(SESSION_META_ADAPTER_ID)
def read_session_meta_redacted_jsonl(source_root: Path) -> AdapterResult:
    source_path = _resolve_atoms_path(source_root)
    try:
        source_bytes = source_path.read_bytes()
    except OSError as exc:
        raise SourceBundleError("configured redacted source bundle is unreadable") from exc
    input_hash = _sha256(source_bytes)
    records: list[RedactedSourceRecord] = []
    diagnostics: list[AdapterDiagnostic] = []
    nonblank_lines = 0
    for line_number, raw_line in enumerate(source_bytes.splitlines(), 1):
        if not raw_line.strip():
            continue
        nonblank_lines += 1
        input_reference = f"bundle:sha256:{input_hash}#line:{line_number}"
        try:
            decoded = raw_line.decode("utf-8")
            payload = json.loads(decoded)
            records.append(
                _parse_record(
                    payload,
                    line_number=line_number,
                    input_reference=input_reference,
                )
            )
        except (UnicodeDecodeError, json.JSONDecodeError, SourceBundleError) as exc:
            diagnostics.append(_diagnostic(raw_line, line_number, input_hash, str(exc)))
    return AdapterResult(
        source_path=source_path,
        source_hash=input_hash,
        records=tuple(records),
        diagnostics=tuple(diagnostics),
        nonblank_lines=nonblank_lines,
    )
