from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from conversation_corpus_engine.authority_ingest import (
    DIGEST_ALGORITHM,
    contract_digest,
    ingest_authority_bundle,
)
from conversation_corpus_engine.authority_projection import (
    canonical_json_bytes,
    sha256_contract,
)

CAPTURED_AT = "2026-07-16T16:00:00Z"
SNAPSHOT_ID = "fixture-snapshot"
CUSTODY_POINTER = "custody:fixture-snapshot"


def source_content_sha(role: str, text: str) -> str:
    normalized = " ".join(text.split())
    return hashlib.sha256(f"{role}\x00{normalized}".encode()).hexdigest()


def test_contract_digest_uses_rfc8785_numeric_and_unicode_canonicalization() -> None:
    payload = {"é": 1.0, "a": 1e-7, "z": -0.0}
    canonical = '{"a":1e-7,"z":0,"é":1}'.encode()

    assert canonical_json_bytes(payload) == canonical
    assert contract_digest(payload) == f"sha256:{hashlib.sha256(canonical).hexdigest()}"
    assert (
        contract_digest(payload)
        != f"sha256:{hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()}"
    )


def test_contract_digest_rejects_non_i_json_numeric_values() -> None:
    with pytest.raises(ValueError):
        contract_digest({"unsafe_integer": 2**60})
    with pytest.raises(ValueError):
        contract_digest({"not_finite": float("inf")})


def test_native_content_hash_controls_identity_without_exposing_raw_body(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "providers.json"
    atoms_path = tmp_path / "atoms.jsonl"
    write_manifest(manifest, [provider("provider-a", "family-a")])
    immutable_hash = f"sha256:{hashlib.sha256(b'body before redaction').hexdigest()}"
    write_atoms(
        atoms_path,
        [
            atom(
                source="family-a",
                role="user",
                text="[REDACTED-secret]",
                atom_id="native-redacted-event",
                ordinal=0,
                metadata={
                    "native_identity_namespace": "fixture-message-v1",
                    "native_identifiers": {"message_id": "native-message"},
                    "native_content_hash": immutable_hash,
                },
            )
        ],
    )

    result = run_ingest(
        output_root=tmp_path / "out",
        atoms_path=atoms_path,
        manifest_path=manifest,
    )
    event = read_jsonl(result["paths"]["normalized_events"])[0]
    envelope = read_jsonl(result["paths"]["source_envelopes"])[0]

    assert event["identity_basis"]["content_hash"] == immutable_hash
    assert envelope["body_hash"] == immutable_hash
    assert "[REDACTED-secret]" not in json.dumps(event)
    assert "body before redaction" not in json.dumps(envelope)


def atom(
    *,
    source: str,
    role: str,
    text: str,
    atom_id: str,
    ordinal: int,
    kind: str = "message",
    timestamp: str = "2026-07-16T15:00:00Z",
    metadata: dict[str, Any] | None = None,
    raw_unit_ids: list[str] | None = None,
) -> dict[str, Any]:
    atom_metadata = dict(metadata or {})
    if raw_unit_ids is not None:
        atom_metadata["raw_unit_ids"] = raw_unit_ids
        atom_metadata["raw_unit_content_hashes"] = {
            raw_unit_id: sha256_contract({"raw_unit_id": raw_unit_id})
            for raw_unit_id in raw_unit_ids
        }
    return {
        "atom_id": atom_id,
        "content_sha": source_content_sha(role, text),
        "blob_shas": [f"blob-{source}"],
        "source": source,
        "session_id": "session-fixture",
        "ordinal": ordinal,
        "role": role,
        "ts": timestamp,
        "text": text,
        "kind": kind,
        "meta": atom_metadata,
    }


def provider(
    provider_id: str,
    alias: str,
    *,
    root_env: str = "CCE_TEST_BUNDLE_ROOT",
    adapter_type: str = "adapter-reusable",
    authority_policy: str = "native-role",
) -> dict[str, Any]:
    return {
        "provider_id": provider_id,
        "display_name": f"Configured {provider_id}",
        "adapter_state": "supported",
        "default_adapter_id": "session-meta-redacted-jsonl-v1",
        "adapter_type": adapter_type,
        "discovery_mode": "redacted-bundle",
        "inbox_rel": f"{provider_id}/inbox",
        "default_corpus_id": f"{provider_id}-memory",
        "default_corpus_name": f"Configured {provider_id} Memory",
        "source_family_aliases": [alias],
        "source_contract": {
            "kind": "session-meta-redacted-bundle",
            "root_env": root_env,
        },
        "authority_policy": authority_policy,
        "owner_reference": "owner:test",
        "blocker": {
            "owner_reference": "owner:test",
            "failed_predicate": "redacted source bundle is readable",
            "next_action": "configure a frozen fixture and rerun",
        },
    }


def write_manifest(path: Path, providers: list[dict[str, Any]]) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": "provider-manifest.v1",
                "configuration_scope": "reusable-example",
                "unknown_source_blocker": {
                    "owner_reference": "owner:test",
                    "failed_predicate": "source family is registered",
                    "next_action": "register the source family and rerun",
                },
                "providers": providers,
            }
        ),
        encoding="utf-8",
    )


def write_atoms(path: Path, rows: list[dict[str, Any] | str]) -> None:
    path.write_text(
        "".join(
            (row if isinstance(row, str) else json.dumps(row, sort_keys=True)) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


def run_ingest(
    *,
    output_root: Path,
    atoms_path: Path | None,
    manifest_path: Path,
    census_path: Path | None = None,
) -> dict[str, Any]:
    return ingest_authority_bundle(
        output_root=output_root,
        source_root=atoms_path,
        source_census=census_path,
        provider_manifest=manifest_path,
        snapshot_id=SNAPSHOT_ID,
        captured_at=CAPTURED_AT,
        custody_pointer=CUSTODY_POINTER,
    )


def write_census(
    path: Path,
    *,
    snapshot_id: str,
    raw_units: list[dict[str, Any]],
) -> dict[str, Any]:
    snapshot_digest = sha256_contract(
        {
            "snapshot_id": snapshot_id,
            "raw_unit_ids": sorted(row["raw_unit_id"] for row in raw_units),
        }
    )
    census: dict[str, Any] = {
        "contract_name": "source-census.v1",
        "contract_version": 1,
        "census_id": f"census-{snapshot_id}",
        "snapshot_id": snapshot_id,
        "snapshot_at": CAPTURED_AT,
        "snapshot_digest": snapshot_digest,
        "manifest_reference": "source-manifest.v1",
        "manifest_digest": sha256_contract({"manifest": "fixture"}),
        "discovery_roots": [
            {
                "root_id": "fixture-root",
                "root_kind": "export",
                "runtime_reference": "fixture-export",
                "config_reference": "fixture-manifest",
            }
        ],
        "seed_expectations": [],
        "raw_units": raw_units,
        "digest_algorithm": DIGEST_ALGORITHM,
    }
    census["census_digest"] = contract_digest(census)
    path.write_text(json.dumps(census, sort_keys=True), encoding="utf-8")
    return census


def raw_unit(
    raw_unit_id: str,
    *,
    family: str = "family-a",
    status: str = "acquired",
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "raw_unit_id": raw_unit_id,
        "discovery_root_id": "fixture-root",
        "source_family": family,
        "source_instance": f"{family}-native",
        "format_adapter": "fixture-native",
        "native_identifiers": {"export_id": raw_unit_id},
        "acquisition_status": status,
        "content_hash": sha256_contract({"raw_unit_id": raw_unit_id})
        if status == "acquired"
        else None,
        "custody_pointer": f"custody:{raw_unit_id}" if status == "acquired" else None,
        "evidence_references": [f"fixture:{raw_unit_id}"],
    }
    if status != "acquired":
        row.update(
            {
                "owner_reference": "owner:test",
                "failed_predicate": "official export is present",
                "next_action": "acquire the official read-only export and rerun",
            }
        )
    return row


def test_identical_text_retains_five_distinct_authority_classes_and_byte_identity(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "providers.json"
    atoms_path = tmp_path / "atoms.jsonl"
    write_manifest(manifest, [provider("provider-renamed", "family-renamed")])
    shared_text = "The same transported text."
    write_atoms(
        atoms_path,
        [
            atom(
                source="family-renamed",
                role="user",
                text=shared_text,
                atom_id="operator-atom",
                ordinal=0,
            ),
            atom(
                source="family-renamed",
                role="assistant",
                text=shared_text,
                atom_id="assistant-atom",
                ordinal=1,
            ),
            atom(
                source="family-renamed",
                role="tool",
                text=shared_text,
                atom_id="tool-atom",
                ordinal=2,
                kind="tool_output",
            ),
            atom(
                source="family-renamed",
                role="system",
                text=shared_text,
                atom_id="continuation-atom",
                ordinal=3,
                kind="continuation_summary",
            ),
            atom(
                source="family-renamed",
                role="system",
                text=shared_text,
                atom_id="memory-atom",
                ordinal=4,
                kind="memory_summary",
            ),
        ],
    )

    first = run_ingest(
        output_root=tmp_path / "first", atoms_path=atoms_path, manifest_path=manifest
    )
    run_ingest(output_root=tmp_path / "second", atoms_path=atoms_path, manifest_path=manifest)
    events = read_jsonl(first["paths"]["normalized_events"])
    envelopes = read_jsonl(first["paths"]["source_envelopes"])

    assert {event["authority_class"] for event in events} == {
        "operator_intent",
        "artifact",
        "transport_echo",
        "system_metadata",
        "unknown",
    }
    assert len({event["transport_metadata"]["authority_detail"] for event in events}) == 5
    assert len({event["identity_basis"]["content_hash"] for event in events}) == 1
    assert {envelope["role"] for envelope in envelopes} == {
        "operator",
        "assistant",
        "tool",
        "continuation_summary",
        "memory_summary",
    }
    assert all("text" not in envelope for envelope in envelopes)
    assert first["coverage"]["exact_all"] is True
    assert first["coverage"]["ready"] is True
    for filename in (
        "source-envelope.v1.jsonl",
        "normalized-events.v1.jsonl",
        "quarantine.jsonl",
        "owner-blockers.jsonl",
        "coverage-receipt.v1.json",
        "normalization-parity-receipt.v1.json",
    ):
        assert (tmp_path / "first" / filename).read_bytes() == (
            tmp_path / "second" / filename
        ).read_bytes()


def test_provider_manifest_reorder_and_new_provider_are_config_only(tmp_path: Path) -> None:
    first_manifest = tmp_path / "first-manifest.json"
    second_manifest = tmp_path / "second-manifest.json"
    providers = [
        provider("provider-renamed", "family-renamed"),
        provider("provider-new", "family-new"),
    ]
    write_manifest(first_manifest, providers)
    write_manifest(second_manifest, list(reversed(providers)))
    atoms_path = tmp_path / "atoms.jsonl"
    write_atoms(
        atoms_path,
        [
            atom(
                source="family-renamed",
                role="user",
                text="Renamed provider event.",
                atom_id="renamed-atom",
                ordinal=0,
            ),
            atom(
                source="family-new",
                role="assistant",
                text="New provider event.",
                atom_id="new-atom",
                ordinal=1,
            ),
        ],
    )

    first = run_ingest(
        output_root=tmp_path / "first", atoms_path=atoms_path, manifest_path=first_manifest
    )
    second = run_ingest(
        output_root=tmp_path / "second", atoms_path=atoms_path, manifest_path=second_manifest
    )

    assert first["provider_manifest_hash"] == second["provider_manifest_hash"]
    assert first["coverage"]["ready"] is True
    assert (tmp_path / "first" / "source-envelope.v1.jsonl").read_bytes() == (
        tmp_path / "second" / "source-envelope.v1.jsonl"
    ).read_bytes()
    assert (tmp_path / "first" / "coverage-receipt.v1.json").read_bytes() == (
        tmp_path / "second" / "coverage-receipt.v1.json"
    ).read_bytes()


def test_immutable_document_authority_uses_only_config_and_native_roles(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "providers.json"
    atoms_path = tmp_path / "atoms.jsonl"
    write_manifest(
        manifest,
        [
            provider(
                "provider-renamed-at-runtime",
                "family-renamed-at-runtime",
                adapter_type="immutable-document",
                authority_policy="manifest-lane",
            )
        ],
    )
    native_metadata = {
        "format_adapter": "immutable-document",
        "manifest_lane": "artifact",
        "native_identity_namespace": "configured-document-v1",
    }
    write_atoms(
        atoms_path,
        [
            atom(
                source="family-renamed-at-runtime",
                role="system",
                text="Immutable specification.",
                atom_id="specification",
                ordinal=0,
                kind="document",
                metadata={
                    **native_metadata,
                    "native_identifiers": {
                        "repository": "owner/repository",
                        "commit": "a" * 40,
                        "path": "specs/SPEC-000.md",
                    },
                },
            ),
            atom(
                source="family-renamed-at-runtime",
                role="tool",
                text="Immutable operator prompt transport.",
                atom_id="operator-prompt-export",
                ordinal=1,
                kind="document",
                metadata={
                    **native_metadata,
                    "native_identifiers": {
                        "repository": "owner/prompt-custody",
                        "commit": "b" * 40,
                        "path": "exports/operator-prompts.txt",
                    },
                },
            ),
            atom(
                source="family-renamed-at-runtime",
                role="assistant",
                text="Immutable assistant plan.",
                atom_id="assistant-plan",
                ordinal=2,
                kind="document",
                metadata={
                    **native_metadata,
                    "native_identifiers": {
                        "repository": "owner/session-custody",
                        "commit": "c" * 40,
                        "path": "plans/assistant-plan.md",
                    },
                },
            ),
        ],
    )

    result = run_ingest(
        output_root=tmp_path / "out",
        atoms_path=atoms_path,
        manifest_path=manifest,
    )
    events = {
        event["normalized_role"]: event
        for event in read_jsonl(result["paths"]["normalized_events"])
    }

    assert set(events) == {"assistant", "system", "tool"}
    assert {event["authority_class"] for event in events.values()} == {"artifact"}
    assert {event["transport_metadata"]["authority_detail"] for event in events.values()} == {
        "manifested_artifact"
    }
    assert {event["transport_metadata"]["origin_lane"] for event in events.values()} == {"artifact"}
    assert {event["transport_metadata"]["effective_lane"] for event in events.values()} == {
        "artifact"
    }
    assert {event["transport_metadata"]["provider_id"] for event in events.values()} == {
        "provider-renamed-at-runtime"
    }
    assert {event["format_adapter"] for event in events.values()} == {"immutable-document"}
    assert result["coverage"]["ready"] is True


def test_manifest_lane_missing_and_invalid_metadata_quarantine_independently(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "providers.json"
    atoms_path = tmp_path / "atoms.jsonl"
    write_manifest(
        manifest,
        [
            provider("provider-valid", "family-valid"),
            provider(
                "provider-manifest-lane",
                "family-manifest-lane",
                adapter_type="immutable-document",
                authority_policy="manifest-lane",
            ),
        ],
    )
    write_atoms(
        atoms_path,
        [
            atom(
                source="family-valid",
                role="user",
                text="Valid native-role sibling.",
                atom_id="valid-native-role",
                ordinal=0,
            ),
            atom(
                source="family-manifest-lane",
                role="system",
                text="Valid manifested artifact.",
                atom_id="valid-manifest-lane",
                ordinal=1,
                metadata={"manifest_lane": "artifact"},
            ),
            atom(
                source="family-manifest-lane",
                role="tool",
                text="Missing manifested lane.",
                atom_id="missing-manifest-lane",
                ordinal=2,
            ),
            atom(
                source="family-manifest-lane",
                role="assistant",
                text="Invalid manifested lane.",
                atom_id="invalid-manifest-lane",
                ordinal=3,
                metadata={"manifest_lane": "operator_intent"},
            ),
        ],
    )

    result = run_ingest(
        output_root=tmp_path / "out",
        atoms_path=atoms_path,
        manifest_path=manifest,
    )
    events = read_jsonl(result["paths"]["normalized_events"])
    quarantines = read_jsonl(result["paths"]["quarantine"])

    assert {event["normalized_role"] for event in events} == {"operator", "system"}
    assert len(quarantines) == 2
    assert {quarantine["diagnostic"]["message"] for quarantine in quarantines} == {
        "manifest-lane authority_policy requires manifest_lane metadata",
        "manifest_lane must be 'artifact' for manifest-lane authority_policy",
    }
    assert result["coverage"]["counts"]["parsed"] == 2
    assert result["coverage"]["counts"]["quarantined"] == 2
    assert result["coverage"]["exact_all"] is True
    assert result["coverage"]["ready"] is False


def test_unsupported_authority_policy_quarantines_without_stopping_valid_sibling(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "providers.json"
    atoms_path = tmp_path / "atoms.jsonl"
    write_manifest(
        manifest,
        [
            provider("provider-valid", "family-valid"),
            provider(
                "provider-invalid-policy",
                "family-invalid-policy",
                authority_policy="unregistered-policy",
            ),
        ],
    )
    write_atoms(
        atoms_path,
        [
            atom(
                source="family-valid",
                role="user",
                text="Valid sibling.",
                atom_id="valid-sibling",
                ordinal=0,
            ),
            atom(
                source="family-invalid-policy",
                role="assistant",
                text="Policy has no registered projection.",
                atom_id="invalid-policy",
                ordinal=1,
            ),
        ],
    )

    result = run_ingest(
        output_root=tmp_path / "out",
        atoms_path=atoms_path,
        manifest_path=manifest,
    )
    events = read_jsonl(result["paths"]["normalized_events"])
    quarantines = read_jsonl(result["paths"]["quarantine"])

    assert [event["normalized_role"] for event in events] == ["operator"]
    assert len(quarantines) == 1
    assert quarantines[0]["diagnostic"]["code"] == "invalid-authority-projection"
    assert "unsupported authority_policy" in quarantines[0]["diagnostic"]["message"]
    assert result["coverage"]["counts"]["parsed"] == 1
    assert result["coverage"]["counts"]["quarantined"] == 1
    assert result["coverage"]["exact_all"] is True
    assert result["coverage"]["ready"] is False


def test_malformed_unknown_and_missing_sources_do_not_stop_valid_sibling(tmp_path: Path) -> None:
    manifest = tmp_path / "providers.json"
    write_manifest(
        manifest,
        [provider("provider-a", "family-a"), provider("provider-b", "family-b")],
    )
    atoms_path = tmp_path / "atoms.jsonl"
    write_atoms(
        atoms_path,
        [
            atom(
                source="family-a",
                role="user",
                text="Valid sibling.",
                atom_id="valid-atom",
                ordinal=0,
            ),
            "{malformed-json",
            atom(
                source="family-a",
                role="user",
                text="Attachment metadata is missing.",
                atom_id="missing-attachment",
                ordinal=1,
                kind="attachment_ref",
            ),
            atom(
                source="family-unknown",
                role="assistant",
                text="Unknown but hash-addressed.",
                atom_id="unknown-atom",
                ordinal=2,
            ),
        ],
    )

    result = run_ingest(output_root=tmp_path / "out", atoms_path=atoms_path, manifest_path=manifest)

    assert len(read_jsonl(result["paths"]["source_envelopes"])) == 1
    assert len(read_jsonl(result["paths"]["quarantine"])) == 2
    assert len(read_jsonl(result["paths"]["owner_blockers"])) == 2
    assert result["coverage"]["counts"] == {
        "acquired": 0,
        "parsed": 1,
        "quarantined": 2,
        "inaccessible": 0,
        "missing_expected": 1,
        "owner_blocked": 1,
    }
    assert result["coverage"]["exact_all"] is True
    assert result["coverage"]["ready"] is False
    assert len(result["coverage"]["residual_owners"]) == 4


def test_missing_runtime_roots_emit_owner_blockers(monkeypatch, tmp_path: Path) -> None:
    manifest = tmp_path / "providers.json"
    write_manifest(
        manifest,
        [
            provider("provider-a", "family-a", root_env="CCE_MISSING_A"),
            provider("provider-b", "family-b", root_env="CCE_MISSING_B"),
        ],
    )
    monkeypatch.delenv("CCE_MISSING_A", raising=False)
    monkeypatch.delenv("CCE_MISSING_B", raising=False)

    result = run_ingest(output_root=tmp_path / "out", atoms_path=None, manifest_path=manifest)

    assert result["coverage"]["counts"]["owner_blocked"] == 2
    assert result["coverage"]["exact_all"] is True
    assert result["coverage"]["ready"] is False
    assert len(read_jsonl(result["paths"]["owner_blockers"])) == 2


def test_assistant_plan_stays_artifact_without_reviewed_operator_adoption(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "providers.json"
    write_manifest(manifest, [provider("provider-a", "family-a")])
    atoms_path = tmp_path / "atoms.jsonl"
    write_atoms(
        atoms_path,
        [
            atom(
                source="family-a",
                role="assistant",
                text="Generated plan awaiting adoption.",
                atom_id="plan-adopted",
                ordinal=0,
                kind="plan",
                metadata={"adoption_event_id": "adoption-event"},
            ),
            atom(
                source="family-a",
                role="user",
                text="I explicitly adopt that plan.",
                atom_id="adoption-event",
                ordinal=1,
                kind="adoption",
                metadata={"review_state": "reviewed", "adopts_atom_id": "plan-adopted"},
            ),
            atom(
                source="family-a",
                role="assistant",
                text="Generated plan without adoption.",
                atom_id="plan-unadopted",
                ordinal=2,
                kind="plan",
            ),
        ],
    )

    result = run_ingest(output_root=tmp_path / "out", atoms_path=atoms_path, manifest_path=manifest)
    events = {
        event["identity_basis"]["native_identifiers"]["atom_id"]: event
        for event in read_jsonl(result["paths"]["normalized_events"])
    }

    assert events["plan-adopted"]["authority_class"] == "artifact"
    assert events["plan-adopted"]["transport_metadata"]["origin_lane"] == "artifact"
    assert events["plan-adopted"]["transport_metadata"]["effective_lane"] == "operator_intent"
    assert (
        events["plan-adopted"]["transport_metadata"]["adoption_state"]
        == "reviewed_operator_adoption"
    )
    assert events["plan-unadopted"]["authority_class"] == "artifact"
    assert events["plan-unadopted"]["transport_metadata"]["effective_lane"] == "artifact"
    assert events["plan-unadopted"]["transport_metadata"]["adoption_state"] == "not_adopted"


def test_invalid_timestamp_quarantines_without_repeating_duplicate_transports(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "providers.json"
    write_manifest(manifest, [provider("provider-a", "family-a")])
    atoms_path = tmp_path / "atoms.jsonl"
    duplicate = atom(
        source="family-a",
        role="user",
        text="Repeated transport.",
        atom_id="duplicate-atom",
        ordinal=0,
    )
    invalid = atom(
        source="family-a",
        role="assistant",
        text="No native event time.",
        atom_id="invalid-time",
        ordinal=1,
        timestamp="not-a-time",
    )
    write_atoms(atoms_path, [duplicate, duplicate, invalid])

    result = run_ingest(output_root=tmp_path / "out", atoms_path=atoms_path, manifest_path=manifest)

    assert result["coverage"]["counts"]["parsed"] == 1
    assert result["coverage"]["counts"]["quarantined"] == 1
    assert len(result["parity"]["output_events"]["event_ids"]) == 1
    assert result["parity"]["readiness"]["exact_all"] is True
    assert result["parity"]["readiness"]["ready"] is False
    assert len(result["parity"]["readiness"]["quarantines"]) == 1


def test_exact_census_crosswalk_classifies_every_raw_unit_once(tmp_path: Path) -> None:
    manifest = tmp_path / "providers.json"
    atoms_path = tmp_path / "atoms.jsonl"
    census_path = tmp_path / "source-census.v1.json"
    write_manifest(manifest, [provider("provider-a", "family-a")])
    mapped_id = "raw_" + "a" * 64
    unavailable_id = "raw_" + "b" * 64
    unsupported_id = "raw_" + "c" * 64
    write_atoms(
        atoms_path,
        [
            atom(
                source="family-a",
                role="user",
                text="Immutable operator event.",
                atom_id="native-event",
                ordinal=0,
                raw_unit_ids=[mapped_id],
                metadata={
                    "native_identifiers": {
                        "session_id": "native-session",
                        "event_uuid": "native-event",
                    },
                    "source_instance": "family-a-native",
                    "format_adapter": "fixture-native",
                    "native_identity_namespace": "fixture-native:family-a",
                },
            )
        ],
    )
    write_census(
        census_path,
        snapshot_id=SNAPSHOT_ID,
        raw_units=[
            raw_unit(mapped_id),
            raw_unit(unavailable_id, family="perplexity", status="inaccessible"),
            raw_unit(unsupported_id, family="provider-added-later"),
        ],
    )

    result = run_ingest(
        output_root=tmp_path / "out",
        atoms_path=atoms_path,
        manifest_path=manifest,
        census_path=census_path,
    )
    promotions = result["parity"]["promotions"]
    mapped_content_hash = sha256_contract({"raw_unit_id": mapped_id})

    assert [row["raw_unit_id"] for row in promotions] == sorted(
        [mapped_id, unavailable_id, unsupported_id]
    )
    assert len(promotions[0]["event_ids"]) == 1
    assert {row["raw_unit_id"]: row["raw_unit_content_hash"] for row in promotions} == {
        mapped_id: mapped_content_hash,
        unavailable_id: None,
        unsupported_id: sha256_contract({"raw_unit_id": unsupported_id}),
    }
    assert result["parity"]["input_census"]["raw_units"] == [
        {
            "raw_unit_id": mapped_id,
            "content_hash": mapped_content_hash,
        },
        {
            "raw_unit_id": unavailable_id,
            "content_hash": None,
        },
        {
            "raw_unit_id": unsupported_id,
            "content_hash": sha256_contract({"raw_unit_id": unsupported_id}),
        },
    ]
    dispositions = {
        row["raw_unit_id"]: row["disposition"]["type"] for row in promotions if "disposition" in row
    }
    assert dispositions == {
        unavailable_id: "blocked",
        unsupported_id: "unsupported",
    }
    assert result["parity"]["readiness"]["exact_all"] is True
    assert result["parity"]["readiness"]["ready"] is False
    coverage = result["coverage"]
    assert coverage["denominator"]["count"] == 3
    assert coverage["counts"]["parsed"] == 1
    assert coverage["counts"]["inaccessible"] == 1
    assert coverage["counts"]["acquired"] == 1
    assert coverage["ready"] is False
    assert coverage["closure_status"] == "closed_with_owner_routed_debt"
    assert coverage["unresolved_blockers"] == [f"src_{unavailable_id.removeprefix('raw_')}"]
    assert coverage["incomplete_predicates"] == [f"src_{unsupported_id.removeprefix('raw_')}"]
    event = read_jsonl(result["paths"]["normalized_events"])[0]
    assert event["format_adapter"] == "fixture-native"
    assert event["raw_unit_content_hash"] == mapped_content_hash
    envelope = read_jsonl(result["paths"]["source_envelopes"])[0]
    assert envelope["raw_unit_id"] == mapped_id
    assert envelope["raw_unit_content_hash"] == mapped_content_hash


def test_event_identity_excludes_snapshot_and_transport_position(tmp_path: Path) -> None:
    manifest = tmp_path / "providers.json"
    atoms_path = tmp_path / "atoms.jsonl"
    raw_id = "raw_" + "d" * 64
    write_manifest(manifest, [provider("provider-a", "family-a")])
    write_atoms(
        atoms_path,
        [
            atom(
                source="family-a",
                role="user",
                text="Same immutable event.",
                atom_id="native-event",
                ordinal=987,
                raw_unit_ids=[raw_id],
                metadata={
                    "native_identifiers": {
                        "session_id": "native-session",
                        "event_uuid": "event-one",
                    },
                    "source_instance": "family-a-native",
                },
            )
        ],
    )
    first_census = tmp_path / "first-census.json"
    second_census = tmp_path / "second-census.json"
    write_census(first_census, snapshot_id=SNAPSHOT_ID, raw_units=[raw_unit(raw_id)])
    write_census(second_census, snapshot_id="other-snapshot", raw_units=[raw_unit(raw_id)])

    first = run_ingest(
        output_root=tmp_path / "first",
        atoms_path=atoms_path,
        manifest_path=manifest,
        census_path=first_census,
    )
    second = ingest_authority_bundle(
        output_root=tmp_path / "second",
        source_root=atoms_path,
        source_census=second_census,
        provider_manifest=manifest,
        snapshot_id="other-snapshot",
        captured_at=CAPTURED_AT,
        custody_pointer=CUSTODY_POINTER,
    )
    first_event = read_jsonl(first["paths"]["normalized_events"])[0]
    second_event = read_jsonl(second["paths"]["normalized_events"])[0]

    assert first_event["event_id"] == second_event["event_id"]
    assert first_event["snapshot_id"] != second_event["snapshot_id"]
    assert "ordinal" not in first_event["identity_basis"]["native_identifiers"]
    assert "input_reference" not in first_event["identity_basis"]["native_identifiers"]


def test_adapter_native_fork_remains_transport_echo(tmp_path: Path) -> None:
    manifest = tmp_path / "providers.json"
    atoms_path = tmp_path / "atoms.jsonl"
    census_path = tmp_path / "census.json"
    root_id = "raw_" + "e" * 64
    fork_id = "raw_" + "f" * 64
    write_manifest(manifest, [provider("provider-a", "family-a")])
    write_atoms(
        atoms_path,
        [
            atom(
                source="family-a",
                role="user",
                text="Root authority.",
                atom_id="root-event",
                ordinal=0,
                raw_unit_ids=[root_id],
                metadata={
                    "native_identifiers": {"event_uuid": "root-event"},
                    "authority_class": "operator_intent",
                    "transport_classification": "adapter-native",
                },
            ),
            atom(
                source="family-a",
                role="user",
                text="Forked transport.",
                atom_id="fork-event",
                ordinal=1,
                raw_unit_ids=[fork_id],
                metadata={
                    "native_identifiers": {"event_uuid": "fork-event"},
                    "authority_class": "transport_echo",
                    "transport_classification": "adapter-native",
                },
            ),
        ],
    )
    write_census(
        census_path,
        snapshot_id=SNAPSHOT_ID,
        raw_units=[raw_unit(root_id), raw_unit(fork_id)],
    )

    result = run_ingest(
        output_root=tmp_path / "out",
        atoms_path=atoms_path,
        manifest_path=manifest,
        census_path=census_path,
    )
    classes = {
        event["identity_basis"]["native_identifiers"]["event_uuid"]: event["authority_class"]
        for event in read_jsonl(result["paths"]["normalized_events"])
    }

    assert classes == {
        "root-event": "operator_intent",
        "fork-event": "transport_echo",
    }


def test_census_tamper_and_snapshot_mismatch_fail_closed(tmp_path: Path) -> None:
    manifest = tmp_path / "providers.json"
    atoms_path = tmp_path / "atoms.jsonl"
    census_path = tmp_path / "census.json"
    raw_id = "raw_" + "1" * 64
    write_manifest(manifest, [provider("provider-a", "family-a")])
    write_atoms(atoms_path, [])
    census = write_census(
        census_path,
        snapshot_id=SNAPSHOT_ID,
        raw_units=[raw_unit(raw_id, status="inaccessible")],
    )
    census["raw_units"][0]["next_action"] = "tampered"
    census_path.write_text(json.dumps(census), encoding="utf-8")

    with pytest.raises(ValueError, match="digest"):
        run_ingest(
            output_root=tmp_path / "tampered",
            atoms_path=atoms_path,
            manifest_path=manifest,
            census_path=census_path,
        )

    write_census(
        census_path,
        snapshot_id="different-snapshot",
        raw_units=[raw_unit(raw_id, status="inaccessible")],
    )
    with pytest.raises(ValueError, match="snapshot_id"):
        run_ingest(
            output_root=tmp_path / "mismatch",
            atoms_path=atoms_path,
            manifest_path=manifest,
            census_path=census_path,
        )


def test_resealed_census_raw_content_hash_mismatch_fails_closed(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "providers.json"
    atoms_path = tmp_path / "atoms.jsonl"
    census_path = tmp_path / "census.json"
    raw_id = "raw_" + "3" * 64
    write_manifest(manifest, [provider("provider-a", "family-a")])
    write_atoms(
        atoms_path,
        [
            atom(
                source="family-a",
                role="user",
                text="Immutable operator event.",
                atom_id="native-event",
                ordinal=0,
                raw_unit_ids=[raw_id],
                metadata={
                    "native_identifiers": {"event_uuid": "native-event"},
                    "native_identity_namespace": "fixture-native-v1",
                },
            )
        ],
    )
    census = write_census(
        census_path,
        snapshot_id=SNAPSHOT_ID,
        raw_units=[raw_unit(raw_id)],
    )
    census["raw_units"][0]["content_hash"] = "sha256:" + "9" * 64
    census["census_digest"] = contract_digest(
        {key: value for key, value in census.items() if key != "census_digest"}
    )
    census_path.write_text(json.dumps(census), encoding="utf-8")

    with pytest.raises(ValueError, match="content binding does not match census"):
        run_ingest(
            output_root=tmp_path / "resealed",
            atoms_path=atoms_path,
            manifest_path=manifest,
            census_path=census_path,
        )


def test_census_shape_mismatch_fails_even_with_a_recomputed_digest(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "providers.json"
    atoms_path = tmp_path / "atoms.jsonl"
    census_path = tmp_path / "census.json"
    raw_id = "raw_" + "2" * 64
    write_manifest(manifest, [provider("provider-a", "family-a")])
    write_atoms(atoms_path, [])
    census = write_census(
        census_path,
        snapshot_id=SNAPSHOT_ID,
        raw_units=[raw_unit(raw_id)],
    )
    census["unexpected_transport_field"] = "must fail closed"
    census["census_digest"] = contract_digest(
        {key: value for key, value in census.items() if key != "census_digest"}
    )
    census_path.write_text(json.dumps(census), encoding="utf-8")

    with pytest.raises(ValueError, match="fields"):
        run_ingest(
            output_root=tmp_path / "shape-mismatch",
            atoms_path=atoms_path,
            manifest_path=manifest,
            census_path=census_path,
        )
