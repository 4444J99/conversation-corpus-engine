from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from conversation_corpus_engine import authority_cadence
from conversation_corpus_engine.authority_cadence import (
    CLASSIFY_PROJECTION,
    CORE_ARTIFACTS,
    PARSE_PROJECTION,
    AuthorityCadenceError,
    run_authority_classify_stage,
    run_authority_parse_stage,
)
from conversation_corpus_engine.authority_cadence_predicate import (
    AuthorityCadencePredicateError,
    assert_classify_predicate,
    assert_parse_predicate,
)
from conversation_corpus_engine.authority_ingest import (
    DIGEST_ALGORITHM,
    contract_digest,
)
from conversation_corpus_engine.authority_projection import sha256_contract

SNAPSHOT_ID = "cadence-fixture-snapshot"
CAPTURED_AT = "2026-07-16T16:00:00Z"
CUSTODY_POINTER = "custody:cadence-fixture-snapshot"


def _source_content_sha(role: str, text: str) -> str:
    normalized = " ".join(text.split())
    return hashlib.sha256(f"{role}\x00{normalized}".encode()).hexdigest()


def _raw_unit(raw_unit_id: str, *, status: str = "acquired") -> dict[str, Any]:
    row: dict[str, Any] = {
        "raw_unit_id": raw_unit_id,
        "discovery_root_id": "fixture-root",
        "source_family": "runtime-family",
        "source_instance": "runtime-source-instance",
        "format_adapter": "session-meta-redacted-jsonl-v1",
        "native_identifiers": {"fixture_id": raw_unit_id},
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
                "owner_reference": "owner:fixture-export",
                "failed_predicate": "official read-only export is present",
                "next_action": "acquire the official export and rerun the same snapshot",
            }
        )
    return row


def _write_inputs(root: Path) -> tuple[Path, Path, Path]:
    source_root = root / "redacted-atoms.jsonl"
    census_path = root / "source-census.v1.json"
    manifest_path = root / "provider-manifest.v1.json"
    raw_acquired = _raw_unit("raw_acquired")
    raw_blocked = _raw_unit("raw_blocked", status="missing_expected")
    redacted_text = "[REDACTED-fixture-authority-event]"
    atom = {
        "atom_id": "runtime-atom",
        "content_sha": _source_content_sha("user", redacted_text),
        "blob_shas": ["blob-runtime-fixture"],
        "source": "runtime-family",
        "session_id": "runtime-session",
        "ordinal": 0,
        "role": "user",
        "ts": "2026-07-16T15:00:00Z",
        "text": redacted_text,
        "kind": "message",
        "meta": {
            "raw_unit_ids": ["raw_acquired"],
            "raw_unit_content_hashes": {
                "raw_acquired": raw_acquired["content_hash"],
            },
            "native_identity_namespace": "runtime-fixture-message-v1",
            "native_identifiers": {"message_id": "runtime-message"},
        },
    }
    source_root.write_text(f"{json.dumps(atom, sort_keys=True)}\n", encoding="utf-8")
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "provider-manifest.v1",
                "configuration_scope": "instance",
                "unknown_source_blocker": {
                    "owner_reference": "owner:fixture-manifest",
                    "failed_predicate": "source family is configured",
                    "next_action": "register the runtime family and rerun",
                },
                "providers": [
                    {
                        "provider_id": "runtime-configured-provider",
                        "display_name": "Runtime Configured Provider",
                        "adapter_state": "supported",
                        "default_adapter_id": "session-meta-redacted-jsonl-v1",
                        "adapter_type": "adapter-reusable",
                        "discovery_mode": "redacted-bundle",
                        "inbox_rel": "runtime/inbox",
                        "default_corpus_id": "runtime-memory",
                        "default_corpus_name": "Runtime Memory",
                        "source_family_aliases": ["runtime-family"],
                        "source_contract": {
                            "kind": "session-meta-redacted-bundle",
                            "root_env": "CCE_RUNTIME_FIXTURE_ROOT",
                        },
                        "authority_policy": "native-role",
                        "owner_reference": "owner:fixture-manifest",
                        "blocker": {
                            "owner_reference": "owner:fixture-manifest",
                            "failed_predicate": "runtime source bundle is readable",
                            "next_action": "restore the runtime source and rerun",
                        },
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    raw_units = [raw_acquired, raw_blocked]
    census: dict[str, Any] = {
        "contract_name": "source-census.v1",
        "contract_version": 1,
        "census_id": f"census-{SNAPSHOT_ID}",
        "snapshot_id": SNAPSHOT_ID,
        "snapshot_at": CAPTURED_AT,
        "snapshot_digest": sha256_contract(
            {
                "snapshot_id": SNAPSHOT_ID,
                "raw_unit_ids": sorted(row["raw_unit_id"] for row in raw_units),
            }
        ),
        "manifest_reference": "source-manifest.v1",
        "manifest_digest": sha256_contract({"manifest": "runtime-fixture"}),
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
    census_path.write_text(json.dumps(census, sort_keys=True), encoding="utf-8")
    return source_root, census_path, manifest_path


def _stage_environment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stage: str,
    metrics_path: Path,
    proof: bool = False,
    prior_receipt: Path | None = None,
) -> None:
    monkeypatch.setenv("LIMEN_GOV_STAGE", stage)
    monkeypatch.setenv("LIMEN_GOV_SNAPSHOT_ID", SNAPSHOT_ID)
    monkeypatch.setenv("LIMEN_GOV_SNAPSHOT_AT", CAPTURED_AT)
    monkeypatch.setenv("LIMEN_GOV_STAGE_METRICS_OUT", str(metrics_path))
    monkeypatch.setenv("LIMEN_GOV_MAX_ITEMS", "2")
    monkeypatch.setenv("LIMEN_GOV_PROOF_MODE", "1" if proof else "0")
    monkeypatch.setenv(
        "LIMEN_GOV_PRIOR_STAGE_RECEIPT",
        str(prior_receipt) if prior_receipt else "",
    )


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _output_bytes(root: Path, projection_name: str) -> dict[str, bytes]:
    filenames = [filename for _, filename, _ in CORE_ARTIFACTS] + [projection_name]
    return {filename: (root / filename).read_bytes() for filename in filenames}


def test_parse_and_classify_are_one_ingest_then_byte_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root, census_path, manifest_path = _write_inputs(tmp_path)
    parse_root = tmp_path / "parse"
    classify_root = tmp_path / "classify"
    parse_metrics = tmp_path / "parse-metrics.json"
    classify_metrics = tmp_path / "classify-metrics.json"
    _stage_environment(monkeypatch, stage="parse", metrics_path=parse_metrics)

    parse_projection = run_authority_parse_stage(
        source_root=source_root,
        source_census=census_path,
        provider_manifest=manifest_path,
        output_root=parse_root,
        snapshot_id=SNAPSHOT_ID,
        captured_at=CAPTURED_AT,
        custody_pointer=CUSTODY_POINTER,
    )

    assert parse_projection["boundary"] == {
        "implementation": "single-authority-ingest-transaction",
        "classification_materialized_during_parse": True,
        "classify_operation": "digest-verified-byte-projection",
    }
    assert parse_projection["readiness"]["exact_all"] is True
    assert parse_projection["readiness"]["ready"] is False
    assert parse_projection["readiness"]["status"] == "closed_with_owner_routed_debt"
    assert parse_projection["classification_summary"]["event_count"] == 1
    assert parse_projection["classification_summary"]["authority_class_counts"] == {
        "operator_intent": 1
    }
    assert "runtime-configured-provider" not in json.dumps(parse_projection)
    assert "[REDACTED-fixture-authority-event]" not in json.dumps(parse_projection)
    parse_metric = _read_object(parse_metrics)
    assert parse_metric["emitted_events"] == 1
    assert parse_metric["child_receipts"][0]["status"] == "completed"

    _stage_environment(monkeypatch, stage="classify", metrics_path=classify_metrics)
    classify_projection = run_authority_classify_stage(
        input_root=parse_root,
        output_root=classify_root,
        snapshot_id=SNAPSHOT_ID,
    )

    for _, filename, _ in CORE_ARTIFACTS:
        assert (parse_root / filename).read_bytes() == (classify_root / filename).read_bytes()
    assert (
        classify_projection["source_parse_projection_digest"]
        == parse_projection["projection_digest"]
    )
    assert classify_projection["readiness"] == parse_projection["readiness"]
    assert classify_projection["readiness"]["exact_all"] is True
    assert classify_projection["readiness"]["ready"] is False
    classify_metric = _read_object(classify_metrics)
    assert classify_metric["emitted_events"] == 0
    assert classify_metric["child_receipts"][0]["status"] == "completed"
    assert_parse_predicate(
        source_root=source_root,
        source_census=census_path,
        provider_manifest=manifest_path,
        output_root=parse_root,
        snapshot_id=SNAPSHOT_ID,
        captured_at=CAPTURED_AT,
        custody_pointer=CUSTODY_POINTER,
    )
    assert_classify_predicate(
        input_root=parse_root,
        output_root=classify_root,
        snapshot_id=SNAPSHOT_ID,
        captured_at=CAPTURED_AT,
    )


def test_proof_traversal_skips_exact_children_without_mutating_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root, census_path, manifest_path = _write_inputs(tmp_path)
    parse_root = tmp_path / "parse"
    classify_root = tmp_path / "classify"
    parse_metrics = tmp_path / "parse-metrics.json"
    classify_metrics = tmp_path / "classify-metrics.json"
    _stage_environment(monkeypatch, stage="parse", metrics_path=parse_metrics)
    ingest_calls = 0
    real_ingest = authority_cadence.ingest_authority_bundle

    def counted_ingest(**kwargs: Any) -> dict[str, Any]:
        nonlocal ingest_calls
        ingest_calls += 1
        return real_ingest(**kwargs)

    monkeypatch.setattr(
        authority_cadence,
        "ingest_authority_bundle",
        counted_ingest,
    )
    run_authority_parse_stage(
        source_root=source_root,
        source_census=census_path,
        provider_manifest=manifest_path,
        output_root=parse_root,
        snapshot_id=SNAPSHOT_ID,
        captured_at=CAPTURED_AT,
        custody_pointer=CUSTODY_POINTER,
    )
    _stage_environment(monkeypatch, stage="classify", metrics_path=classify_metrics)
    run_authority_classify_stage(
        input_root=parse_root,
        output_root=classify_root,
        snapshot_id=SNAPSHOT_ID,
    )
    before_parse = _output_bytes(parse_root, PARSE_PROJECTION)
    before_classify = _output_bytes(classify_root, CLASSIFY_PROJECTION)
    parse_child = _read_object(parse_metrics)["child_receipts"][0]
    classify_child = _read_object(classify_metrics)["child_receipts"][0]
    parse_receipt = tmp_path / "parse-receipt.json"
    classify_receipt = tmp_path / "classify-receipt.json"
    parse_receipt.write_text(
        json.dumps({"stage": "parse", "child_receipts": [parse_child]}),
        encoding="utf-8",
    )
    classify_receipt.write_text(
        json.dumps({"stage": "classify", "child_receipts": [classify_child]}),
        encoding="utf-8",
    )

    parse_proof_metrics = tmp_path / "parse-proof-metrics.json"
    _stage_environment(
        monkeypatch,
        stage="parse",
        metrics_path=parse_proof_metrics,
        proof=True,
        prior_receipt=parse_receipt,
    )
    run_authority_parse_stage(
        source_root=source_root,
        source_census=census_path,
        provider_manifest=manifest_path,
        output_root=parse_root,
        snapshot_id=SNAPSHOT_ID,
        captured_at=CAPTURED_AT,
        custody_pointer=CUSTODY_POINTER,
    )
    classify_proof_metrics = tmp_path / "classify-proof-metrics.json"
    _stage_environment(
        monkeypatch,
        stage="classify",
        metrics_path=classify_proof_metrics,
        proof=True,
        prior_receipt=classify_receipt,
    )
    run_authority_classify_stage(
        input_root=parse_root,
        output_root=classify_root,
        snapshot_id=SNAPSHOT_ID,
    )

    assert ingest_calls == 2
    assert _output_bytes(parse_root, PARSE_PROJECTION) == before_parse
    assert _output_bytes(classify_root, CLASSIFY_PROJECTION) == before_classify
    for path, prior_child in (
        (parse_proof_metrics, parse_child),
        (classify_proof_metrics, classify_child),
    ):
        metrics = _read_object(path)
        proof_child = metrics["child_receipts"][0]
        assert metrics["emitted_events"] == 0
        assert proof_child["status"] == "skipped_completed"
        assert proof_child["input_digest"] == prior_child["input_digest"]
        assert proof_child["output_digest"] == prior_child["output_digest"]
        assert proof_child["prior_receipt_digest"] == contract_digest(prior_child)


def test_independent_classify_predicate_rejects_artifact_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root, census_path, manifest_path = _write_inputs(tmp_path)
    parse_root = tmp_path / "parse"
    classify_root = tmp_path / "classify"
    _stage_environment(monkeypatch, stage="parse", metrics_path=tmp_path / "parse-metrics.json")
    run_authority_parse_stage(
        source_root=source_root,
        source_census=census_path,
        provider_manifest=manifest_path,
        output_root=parse_root,
        snapshot_id=SNAPSHOT_ID,
        captured_at=CAPTURED_AT,
        custody_pointer=CUSTODY_POINTER,
    )
    _stage_environment(
        monkeypatch,
        stage="classify",
        metrics_path=tmp_path / "classify-metrics.json",
    )
    run_authority_classify_stage(
        input_root=parse_root,
        output_root=classify_root,
        snapshot_id=SNAPSHOT_ID,
    )
    assert_classify_predicate(
        input_root=parse_root,
        output_root=classify_root,
        snapshot_id=SNAPSHOT_ID,
        captured_at=CAPTURED_AT,
    )

    events_path = classify_root / "normalized-events.v1.jsonl"
    events_path.write_bytes(events_path.read_bytes() + b"{}\n")

    with pytest.raises(AuthorityCadencePredicateError):
        assert_classify_predicate(
            input_root=parse_root,
            output_root=classify_root,
            snapshot_id=SNAPSHOT_ID,
            captured_at=CAPTURED_AT,
        )


def test_parse_denominator_must_fit_the_runtime_item_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root, census_path, manifest_path = _write_inputs(tmp_path)
    output_root = tmp_path / "parse"
    _stage_environment(monkeypatch, stage="parse", metrics_path=tmp_path / "metrics.json")
    monkeypatch.setenv("LIMEN_GOV_MAX_ITEMS", "1")

    with pytest.raises(AuthorityCadenceError, match="above LIMEN_GOV_MAX_ITEMS"):
        run_authority_parse_stage(
            source_root=source_root,
            source_census=census_path,
            provider_manifest=manifest_path,
            output_root=output_root,
            snapshot_id=SNAPSHOT_ID,
            captured_at=CAPTURED_AT,
            custody_pointer=CUSTODY_POINTER,
        )

    assert not output_root.exists()


def test_parse_checkpoint_cannot_be_reused_after_an_input_byte_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root, census_path, manifest_path = _write_inputs(tmp_path)
    output_root = tmp_path / "parse"
    first_metrics = tmp_path / "first-metrics.json"
    _stage_environment(monkeypatch, stage="parse", metrics_path=first_metrics)
    first = run_authority_parse_stage(
        source_root=source_root,
        source_census=census_path,
        provider_manifest=manifest_path,
        output_root=output_root,
        snapshot_id=SNAPSHOT_ID,
        captured_at=CAPTURED_AT,
        custody_pointer=CUSTODY_POINTER,
    )
    manifest_path.write_text(
        f"{manifest_path.read_text(encoding='utf-8')}\n",
        encoding="utf-8",
    )

    with pytest.raises(
        AuthorityCadencePredicateError,
        match="input digest is invalid",
    ):
        assert_parse_predicate(
            source_root=source_root,
            source_census=census_path,
            provider_manifest=manifest_path,
            output_root=output_root,
            snapshot_id=SNAPSHOT_ID,
            captured_at=CAPTURED_AT,
            custody_pointer=CUSTODY_POINTER,
        )

    second_metrics = tmp_path / "second-metrics.json"
    _stage_environment(monkeypatch, stage="parse", metrics_path=second_metrics)
    second = run_authority_parse_stage(
        source_root=source_root,
        source_census=census_path,
        provider_manifest=manifest_path,
        output_root=output_root,
        snapshot_id=SNAPSHOT_ID,
        captured_at=CAPTURED_AT,
        custody_pointer=CUSTODY_POINTER,
    )

    assert second["owner_input_digest"] != first["owner_input_digest"]
    assert _read_object(second_metrics)["emitted_events"] == 1
