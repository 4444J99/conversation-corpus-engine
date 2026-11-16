from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import conversation_corpus_engine.cli as MODULE


def _run_main(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> None:
    monkeypatch.setattr(sys, "argv", ["cce", *argv])
    MODULE.main()


def test_parse_threshold_overrides_parses_values_and_rejects_malformed_input() -> None:
    assert MODULE.parse_threshold_overrides(["max_stale_corpora=1", "min_pass_rate=0.5"]) == {
        "max_stale_corpora": 1.0,
        "min_pass_rate": 0.5,
    }

    with pytest.raises(ValueError):
        MODULE.parse_threshold_overrides(["missing-separator"])


def test_main_corpus_list_renders_text_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(
        MODULE,
        "list_registered_corpora",
        lambda project_root: [
            {
                "corpus_id": "primary-corpus",
                "name": "Primary Corpus",
                "status": "active",
                "default": True,
                "root": str(project_root),
            }
        ],
    )

    _run_main(monkeypatch, ["corpus", "list", "--project-root", str(tmp_path)])
    output = capsys.readouterr().out

    assert "* primary-corpus: Primary Corpus [active]" in output
    assert f"  root: {tmp_path}" in output


def test_main_federation_build_prints_json_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(
        MODULE,
        "build_federation",
        lambda project_root: {"project_root": str(project_root), "built": True},
    )

    _run_main(monkeypatch, ["federation", "build", "--project-root", str(tmp_path)])
    payload = json.loads(capsys.readouterr().out)

    assert payload == {"project_root": str(tmp_path), "built": True}


def test_main_corpus_store_register_forwards_root_and_project(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    calls: dict[str, Path] = {}

    def fake_register(project_root: Path, root: Path) -> dict[str, str]:
        calls["project_root"] = project_root
        calls["root"] = root
        return {"root": str(root), "status": "active", "registered_at": "now"}

    monkeypatch.setattr(MODULE, "register_corpus_store", fake_register)

    _run_main(
        monkeypatch,
        [
            "corpus-store",
            "register",
            "--project-root",
            str(tmp_path / "project"),
            "--root",
            str(tmp_path / "store"),
        ],
    )
    payload = json.loads(capsys.readouterr().out)

    assert calls == {
        "project_root": tmp_path / "project",
        "root": tmp_path / "store",
    }
    assert payload["status"] == "active"


def test_main_provider_write_commands_forward_corpus_store_root(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    project_root = tmp_path / "project"
    corpus_store_root = tmp_path / "store"
    calls: dict[str, dict[str, object]] = {}

    def fake_import(**kwargs: object) -> dict[str, object]:
        calls["import"] = kwargs
        return {"provider": kwargs["provider"]}

    def fake_bootstrap(**kwargs: object) -> dict[str, object]:
        calls["bootstrap"] = kwargs
        return {"provider": kwargs["provider"]}

    def fake_refresh(**kwargs: object) -> dict[str, object]:
        calls["refresh"] = kwargs
        return {"provider": kwargs["provider"]}

    monkeypatch.setattr(MODULE, "import_provider_corpus", fake_import)
    monkeypatch.setattr(MODULE, "bootstrap_provider_evaluation", fake_bootstrap)
    monkeypatch.setattr(MODULE, "refresh_provider_corpus", fake_refresh)

    common = [
        "--provider",
        "perplexity",
        "--project-root",
        str(project_root),
        "--corpus-store-root",
        str(corpus_store_root),
    ]
    _run_main(monkeypatch, ["provider", "import", *common])
    capsys.readouterr()
    _run_main(monkeypatch, ["provider", "bootstrap-eval", *common])
    capsys.readouterr()
    _run_main(monkeypatch, ["provider", "refresh", *common])
    capsys.readouterr()

    for kwargs in calls.values():
        assert kwargs["project_root"] == project_root
        assert kwargs["corpus_store_root"] == corpus_store_root


def test_main_provider_readiness_json_mode_writes_reports(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    project_root = tmp_path / "project"
    source_drop_root = tmp_path / "source-drop"
    calls: dict[str, object] = {}

    def fake_build_provider_readiness(project: Path, source_drop: Path) -> dict[str, object]:
        calls["build"] = (project, source_drop)
        return {"providers": [{"provider": "claude", "readiness_state": "ready"}]}

    def fake_write_provider_readiness_reports(
        project: Path, payload: dict[str, object]
    ) -> dict[str, str]:
        calls["write"] = (project, payload)
        return {"json_path": str(project / "reports" / "readiness.json")}

    monkeypatch.setattr(MODULE, "build_provider_readiness", fake_build_provider_readiness)
    monkeypatch.setattr(
        MODULE,
        "write_provider_readiness_reports",
        fake_write_provider_readiness_reports,
    )

    _run_main(
        monkeypatch,
        [
            "provider",
            "readiness",
            "--project-root",
            str(project_root),
            "--source-drop-root",
            str(source_drop_root),
            "--write",
            "--json",
        ],
    )
    payload = json.loads(capsys.readouterr().out)

    assert calls["build"] == (project_root, source_drop_root)
    assert calls["write"] == (
        project_root,
        {"providers": [{"provider": "claude", "readiness_state": "ready"}]},
    )
    assert payload["report_paths"]["json_path"].endswith("readiness.json")


def test_main_provider_authority_ingest_passes_frozen_snapshot_contract(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    calls: dict[str, object] = {}

    def fake_ingest_authority_bundle(**kwargs: object) -> dict[str, object]:
        calls.update(kwargs)
        return {"snapshot_id": kwargs["snapshot_id"], "parity": {"parity_exact": True}}

    monkeypatch.setattr(MODULE, "ingest_authority_bundle", fake_ingest_authority_bundle)
    source_root = tmp_path / "redacted-bundle"
    manifest = tmp_path / "provider-manifest.json"
    output_root = tmp_path / "output"

    _run_main(
        monkeypatch,
        [
            "provider",
            "authority-ingest",
            "--source-root",
            str(source_root),
            "--provider-manifest",
            str(manifest),
            "--output-root",
            str(output_root),
            "--snapshot-id",
            "snapshot-fixture",
            "--captured-at",
            "2026-07-16T16:00:00Z",
            "--custody-pointer",
            "custody:snapshot-fixture",
        ],
    )
    payload = json.loads(capsys.readouterr().out)

    assert calls == {
        "output_root": output_root,
        "source_root": source_root,
        "source_census": None,
        "provider_manifest": manifest,
        "snapshot_id": "snapshot-fixture",
        "captured_at": "2026-07-16T16:00:00Z",
        "custody_pointer": "custody:snapshot-fixture",
    }
    assert payload["parity"]["parity_exact"] is True


def test_main_provider_authority_classify_cadence_uses_stage_contract(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    calls: dict[str, object] = {}

    def fake_run_authority_classify_stage(**kwargs: object) -> dict[str, object]:
        calls.update(kwargs)
        return {"stage": "classify", "snapshot_id": kwargs["snapshot_id"]}

    monkeypatch.setattr(
        MODULE,
        "run_authority_classify_stage",
        fake_run_authority_classify_stage,
    )
    input_root = tmp_path / "parse"
    output_root = tmp_path / "classify"

    _run_main(
        monkeypatch,
        [
            "provider",
            "authority-classify-cadence",
            "--input-root",
            str(input_root),
            "--output-root",
            str(output_root),
            "--snapshot-id",
            "snapshot-fixture",
        ],
    )

    assert calls == {
        "input_root": input_root,
        "output_root": output_root,
        "snapshot_id": "snapshot-fixture",
    }
    assert json.loads(capsys.readouterr().out)["stage"] == "classify"


def test_main_provider_authority_classify_predicate_binds_capture_time(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    calls: dict[str, object] = {}

    def fake_assert_classify_predicate(**kwargs: object) -> None:
        calls.update(kwargs)

    monkeypatch.setattr(
        MODULE,
        "assert_classify_predicate",
        fake_assert_classify_predicate,
    )
    input_root = tmp_path / "parse"
    output_root = tmp_path / "classify"

    _run_main(
        monkeypatch,
        [
            "provider",
            "authority-classify-predicate",
            "--input-root",
            str(input_root),
            "--output-root",
            str(output_root),
            "--snapshot-id",
            "snapshot-fixture",
            "--captured-at",
            "2026-07-16T16:00:00Z",
        ],
    )

    assert calls == {
        "input_root": input_root,
        "output_root": output_root,
        "snapshot_id": "snapshot-fixture",
        "captured_at": "2026-07-16T16:00:00Z",
    }
    assert json.loads(capsys.readouterr().out) == {
        "stage": "classify",
        "predicate": "passed",
    }


def test_main_surface_bundle_prints_json_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(
        MODULE,
        "export_surface_bundle",
        lambda project_root, source_drop_root=None: {
            "project_root": str(project_root),
            "source_drop_root": str(source_drop_root),
            "written": True,
        },
    )

    _run_main(
        monkeypatch,
        [
            "surface",
            "bundle",
            "--project-root",
            str(tmp_path / "project"),
            "--source-drop-root",
            str(tmp_path / "drop"),
        ],
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["written"] is True
    assert payload["source_drop_root"] == str(tmp_path / "drop")


def test_main_commercial_h1_writes_contract_and_prints_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    calls: dict[str, object] = {}

    def fake_build_commercial_h1_readiness(
        project_root: Path, source_drop_root: Path | None = None
    ) -> dict[str, object]:
        calls["build"] = (project_root, source_drop_root)
        return {
            "contract_name": "conversation-corpus-engine-commercial-h1-readiness-v1",
            "summary": {"commercial_h1_repo_ready": True},
        }

    def fake_write_commercial_h1_artifacts(
        project_root: Path, payload: dict[str, object]
    ) -> dict[str, str]:
        calls["write"] = (project_root, payload)
        return {"json_path": str(project_root / "reports" / "commercial.json")}

    monkeypatch.setattr(
        MODULE,
        "build_commercial_h1_readiness",
        fake_build_commercial_h1_readiness,
    )
    monkeypatch.setattr(
        MODULE,
        "write_commercial_h1_artifacts",
        fake_write_commercial_h1_artifacts,
    )

    _run_main(
        monkeypatch,
        [
            "commercial",
            "h1",
            "--project-root",
            str(tmp_path / "project"),
            "--source-drop-root",
            str(tmp_path / "drop"),
            "--write",
            "--json",
        ],
    )
    payload = json.loads(capsys.readouterr().out)

    assert calls["build"] == (tmp_path / "project", tmp_path / "drop")
    assert calls["write"] == (
        tmp_path / "project",
        {
            "contract_name": "conversation-corpus-engine-commercial-h1-readiness-v1",
            "summary": {"commercial_h1_repo_ready": True},
        },
    )
    assert payload["artifacts_written"]["json_path"].endswith("commercial.json")


def test_main_mcp_serve_delegates_to_stdio_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: dict[str, object] = {}

    class FakeServer:
        def __init__(self, *, default_project_root: Path) -> None:
            calls["serve"] = default_project_root

        def serve_stdio(self) -> None:
            calls["served"] = True

    # main's mcp_server exposes the stdio loop as CceMcpServer.serve_stdio;
    # the branch's module-level serve_stdio was replaced by that class.
    monkeypatch.setattr(MODULE, "CceMcpServer", FakeServer)

    _run_main(monkeypatch, ["mcp", "serve", "--project-root", str(tmp_path)])

    assert calls["serve"] == tmp_path
    assert calls["served"] is True


def test_main_policy_replay_passes_threshold_overrides_and_writes_artifacts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    calls: dict[str, object] = {}

    def fake_build_policy_replay_payload(
        project_root: Path, threshold_overrides: dict[str, float] | None = None
    ) -> dict[str, object]:
        calls["build"] = (project_root, threshold_overrides)
        return {"summary": {"active_corpus_count": 2}}

    def fake_write_policy_replay_artifacts(
        project_root: Path, payload: dict[str, object]
    ) -> dict[str, str]:
        calls["write"] = (project_root, payload)
        return {"latest_json_path": str(project_root / "reports" / "policy.json")}

    monkeypatch.setattr(MODULE, "build_policy_replay_payload", fake_build_policy_replay_payload)
    monkeypatch.setattr(
        MODULE,
        "write_policy_replay_artifacts",
        fake_write_policy_replay_artifacts,
    )

    _run_main(
        monkeypatch,
        [
            "policy",
            "replay",
            "--project-root",
            str(tmp_path),
            "--set-threshold",
            "max_stale_corpora=1",
            "--set-threshold",
            "min_manual_pass_rate=0.5",
            "--write",
        ],
    )
    payload = json.loads(capsys.readouterr().out)

    assert calls["build"] == (
        tmp_path,
        {"max_stale_corpora": 1.0, "min_manual_pass_rate": 0.5},
    )
    assert calls["write"] == (tmp_path, {"summary": {"active_corpus_count": 2}})
    assert payload["artifacts"]["latest_json_path"].endswith("policy.json")


def test_main_candidate_promote_prints_json_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(
        MODULE,
        "promote_corpus_candidate",
        lambda project_root, candidate_id, note="": {
            "project_root": str(project_root),
            "candidate_id": candidate_id,
            "note": note,
        },
    )

    _run_main(
        monkeypatch,
        [
            "candidate",
            "promote",
            "--project-root",
            str(tmp_path),
            "--candidate-id",
            "latest",
            "--note",
            "promote now",
        ],
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["candidate_id"] == "latest"
    assert payload["note"] == "promote now"


def test_main_evaluation_run_json_mode_prints_scorecard_and_outputs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = tmp_path / "corpus-root"

    def fake_run_corpus_evaluation(
        corpus_root: Path,
        *,
        seed: bool,
        markdown_output: Path | None,
        json_output: Path | None,
        project_root: Path,
        corpus_store_root: Path | None,
    ) -> tuple[dict[str, object], dict[str, Path]]:
        assert seed is True
        assert markdown_output == root / "answer.md"
        assert json_output == root / "answer.json"
        assert project_root == tmp_path / "project"
        assert corpus_store_root == tmp_path / "store"
        return (
            {"overall_state": "pass"},
            {
                "markdown": root / "answer.md",
                "json": root / "answer.json",
            },
        )

    monkeypatch.setattr(MODULE, "run_corpus_evaluation", fake_run_corpus_evaluation)

    _run_main(
        monkeypatch,
        [
            "evaluation",
            "run",
            "--root",
            str(root),
            "--project-root",
            str(tmp_path / "project"),
            "--corpus-store-root",
            str(tmp_path / "store"),
            "--seed",
            "--markdown-output",
            str(root / "answer.md"),
            "--json-output",
            str(root / "answer.json"),
            "--json",
        ],
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["root"] == str(root.resolve())
    assert payload["scorecard"]["overall_state"] == "pass"
    assert payload["outputs"]["markdown"].endswith("answer.md")


def test_main_review_triage_renders_text_summary_and_execute_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    plan = {
        "total_open": 5,
        "auto_resolvable": 3,
        "summary": {"accepted": 1, "rejected": 1, "deferred": 1, "manual": 2},
        "policy_counts": {"prefix-match": 2, "manual-review": 2},
    }
    monkeypatch.setattr(MODULE, "build_triage_plan", lambda project_root: plan)

    _run_main(monkeypatch, ["review", "triage", "--project-root", str(tmp_path)])
    output = capsys.readouterr().out

    assert "Review queue triage: 5 open items" in output
    assert "Accept: 1  Reject: 1  Defer: 1" in output
    assert "Run with --execute to apply." in output


def test_main_review_triage_execute_calls_executor(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    calls: dict[str, object] = {}
    plan = {
        "total_open": 4,
        "auto_resolvable": 2,
        "summary": {"accepted": 1, "rejected": 1, "deferred": 0, "manual": 2},
        "policy_counts": {},
    }

    def fake_execute_triage_plan(project_root: Path, payload: dict[str, object]) -> dict[str, int]:
        calls["execute"] = (project_root, payload)
        return {"resolved": 2, "remaining_open": 2, "errors": []}

    monkeypatch.setattr(MODULE, "build_triage_plan", lambda project_root: plan)
    monkeypatch.setattr(MODULE, "execute_triage_plan", fake_execute_triage_plan)

    _run_main(monkeypatch, ["review", "triage", "--project-root", str(tmp_path), "--execute"])
    output = capsys.readouterr().out

    assert calls["execute"] == (tmp_path, plan)
    assert "Executed: 2 resolved, 2 remaining" in output


def test_main_review_assist_renders_text_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    calls: dict[str, object] = {}
    payload = {"open_count": 3}

    def fake_build_review_assist(
        project_root: Path,
        *,
        batch_size: int,
        relation_filters: list[str] | None = None,
        source_pair: str | None = None,
        anchor_contains: str | None = None,
    ) -> dict[str, object]:
        calls["build"] = (
            project_root,
            batch_size,
            relation_filters,
            source_pair,
            anchor_contains,
        )
        return payload

    monkeypatch.setattr(MODULE, "build_entity_alias_review_assist", fake_build_review_assist)
    monkeypatch.setattr(
        MODULE,
        "render_entity_alias_review_assist",
        lambda data, *, group_limit: f"assist for {data['open_count']} with limit {group_limit}",
    )
    monkeypatch.setattr(
        MODULE, "select_entity_alias_review_assist_batch", lambda payload, batch_id: payload
    )

    _run_main(
        monkeypatch,
        [
            "review",
            "assist",
            "--project-root",
            str(tmp_path),
            "--batch-size",
            "12",
            "--group-limit",
            "4",
            "--relation",
            "disjoint",
            "--source-pair",
            "a <> b",
            "--anchor-contains",
            "apps",
        ],
    )
    output = capsys.readouterr().out

    assert calls["build"] == (tmp_path, 12, ["disjoint"], "a <> b", "apps")
    assert output.strip() == "assist for 3 with limit 4"


def test_main_review_assist_json_mode_prints_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(
        MODULE,
        "build_entity_alias_review_assist",
        lambda project_root, **kwargs: {
            "project_root": str(project_root),
            **kwargs,
        },
    )
    monkeypatch.setattr(
        MODULE,
        "write_entity_alias_review_assist_artifacts",
        lambda project_root, payload: {
            "latest_json_path": str(project_root / "reports" / "assist.json")
        },
    )
    monkeypatch.setattr(
        MODULE, "select_entity_alias_review_assist_batch", lambda payload, batch_id: payload
    )

    _run_main(
        monkeypatch,
        [
            "review",
            "assist",
            "--project-root",
            str(tmp_path),
            "--batch-size",
            "8",
            "--write",
            "--json",
        ],
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["project_root"] == str(tmp_path)
    assert payload["batch_size"] == 8
    assert payload["artifacts"] == {"latest_json_path": str(tmp_path / "reports" / "assist.json")}


def test_main_review_assist_text_mode_prints_artifact_paths_when_written(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(
        MODULE,
        "build_entity_alias_review_assist",
        lambda project_root, **kwargs: {"open_count": 2},
    )
    monkeypatch.setattr(
        MODULE,
        "render_entity_alias_review_assist",
        lambda payload, *, group_limit: "rendered assist",
    )
    monkeypatch.setattr(
        MODULE,
        "write_entity_alias_review_assist_artifacts",
        lambda project_root, payload: {
            "latest_json_path": str(project_root / "reports" / "assist.json"),
            "latest_markdown_path": str(project_root / "reports" / "assist.md"),
            "report_path": str(project_root / "reports" / "assist-2026-03-25.md"),
        },
    )
    monkeypatch.setattr(
        MODULE, "select_entity_alias_review_assist_batch", lambda payload, batch_id: payload
    )

    _run_main(monkeypatch, ["review", "assist", "--project-root", str(tmp_path), "--write"])
    output = capsys.readouterr().out

    assert "rendered assist" in output
    assert "Artifacts:" in output
    assert str(tmp_path / "reports" / "assist.json") in output


def test_main_review_assist_batch_id_selects_single_batch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    calls: dict[str, object] = {}
    monkeypatch.setattr(
        MODULE,
        "build_entity_alias_review_assist",
        lambda project_root, **kwargs: {
            "open_count": 4,
            "batches": [{"batch_id": "entity-alias-batch-001"}],
        },
    )

    def fake_select_review_assist_batch(
        payload: dict[str, object], batch_id: str
    ) -> dict[str, object]:
        calls["select"] = (payload, batch_id)
        return {"open_count": 2, "selection": {"batch_id": batch_id}}

    monkeypatch.setattr(
        MODULE,
        "select_entity_alias_review_assist_batch",
        fake_select_review_assist_batch,
    )
    monkeypatch.setattr(
        MODULE,
        "render_entity_alias_review_assist",
        lambda payload, *, group_limit: (
            f"batch {payload['selection']['batch_id']} limit {group_limit}"
        ),
    )
    monkeypatch.setattr(
        MODULE,
        "filter_entity_alias_review_assist_groups",
        lambda payload, review_bucket_filters=None: payload,
    )

    _run_main(
        monkeypatch,
        [
            "review",
            "assist",
            "--project-root",
            str(tmp_path),
            "--batch-id",
            "entity-alias-batch-001",
        ],
    )
    output = capsys.readouterr().out

    assert calls["select"][1] == "entity-alias-batch-001"
    assert output.strip() == "batch entity-alias-batch-001 limit 10"


def test_main_review_assist_invalid_batch_id_exits_via_parser(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        MODULE,
        "build_entity_alias_review_assist",
        lambda project_root, **kwargs: {
            "open_count": 4,
            "batches": [{"batch_id": "entity-alias-batch-001"}],
        },
    )
    monkeypatch.setattr(
        MODULE,
        "select_entity_alias_review_assist_batch",
        lambda payload, batch_id: (_ for _ in ()).throw(ValueError(f"Unknown batch: {batch_id}")),
    )
    monkeypatch.setattr(
        MODULE,
        "filter_entity_alias_review_assist_groups",
        lambda payload, review_bucket_filters=None: payload,
    )

    with pytest.raises(SystemExit) as exc_info:
        _run_main(
            monkeypatch,
            [
                "review",
                "assist",
                "--project-root",
                str(tmp_path),
                "--batch-id",
                "entity-alias-batch-999",
            ],
        )

    assert exc_info.value.code == 2


def test_main_review_assist_sample_mode_filters_groups_and_writes_sample_artifacts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    calls: dict[str, object] = {}
    monkeypatch.setattr(
        MODULE,
        "build_entity_alias_review_assist",
        lambda project_root, **kwargs: {"open_count": 8, "groups": [], "batches": []},
    )
    monkeypatch.setattr(
        MODULE, "select_entity_alias_review_assist_batch", lambda payload, batch_id: payload
    )

    def fake_filter_review_assist_groups(
        payload: dict[str, object],
        *,
        review_bucket_filters: list[str] | None = None,
    ) -> dict[str, object]:
        calls["filter"] = review_bucket_filters
        return {**payload, "group_filters": {"review_buckets": review_bucket_filters or []}}

    def fake_sample_review_assist_groups(
        payload: dict[str, object],
        *,
        sample_groups: int,
        sample_batches: int | None = None,
        batch_offset: int = 0,
    ) -> dict[str, object]:
        calls["sample"] = (sample_groups, sample_batches, batch_offset)
        return {
            **payload,
            "open_count": 2,
            "sample": {"selected_group_count": 2},
        }

    monkeypatch.setattr(
        MODULE, "filter_entity_alias_review_assist_groups", fake_filter_review_assist_groups
    )
    monkeypatch.setattr(
        MODULE, "sample_entity_alias_review_assist_groups", fake_sample_review_assist_groups
    )
    monkeypatch.setattr(
        MODULE, "render_entity_alias_review_sample", lambda payload: "sample report"
    )
    monkeypatch.setattr(
        MODULE,
        "write_entity_alias_review_sample_artifacts",
        lambda project_root, payload: {
            "latest_json_path": str(project_root / "reports" / "sample-latest.json"),
            "session_markdown_path": str(project_root / "reports" / "sample-2026-03-25-150000.md"),
        },
    )

    _run_main(
        monkeypatch,
        [
            "review",
            "assist",
            "--project-root",
            str(tmp_path),
            "--bucket",
            "likely-reject",
            "--sample-groups",
            "5",
            "--sample-batches",
            "3",
            "--batch-offset",
            "2",
            "--write",
        ],
    )
    output = capsys.readouterr().out

    assert calls["filter"] == ["likely-reject"]
    assert calls["sample"] == (5, 3, 2)
    assert "sample report" in output
    assert str(tmp_path / "reports" / "sample-latest.json") in output


def test_main_review_assist_sample_json_mode_prints_sample_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(
        MODULE,
        "build_entity_alias_review_assist",
        lambda project_root, **kwargs: {
            "project_root": str(project_root),
            "groups": [],
            "batches": [],
        },
    )
    monkeypatch.setattr(
        MODULE, "select_entity_alias_review_assist_batch", lambda payload, batch_id: payload
    )
    monkeypatch.setattr(
        MODULE,
        "filter_entity_alias_review_assist_groups",
        lambda payload, review_bucket_filters=None: payload,
    )
    monkeypatch.setattr(
        MODULE,
        "sample_entity_alias_review_assist_groups",
        lambda payload, *, sample_groups, sample_batches=None, batch_offset=0: {
            **payload,
            "sample": {
                "selected_group_count": sample_groups,
                "candidate_batch_count": sample_batches or 0,
                "requested_batch_offset": batch_offset,
            },
        },
    )
    monkeypatch.setattr(
        MODULE,
        "write_entity_alias_review_sample_artifacts",
        lambda project_root, payload: {
            "latest_markdown_path": str(project_root / "reports" / "sample-latest.md")
        },
    )

    _run_main(
        monkeypatch,
        [
            "review",
            "assist",
            "--project-root",
            str(tmp_path),
            "--sample-groups",
            "4",
            "--sample-batches",
            "2",
            "--batch-offset",
            "1",
            "--write",
            "--json",
        ],
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["sample"]["selected_group_count"] == 4
    assert payload["sample"]["candidate_batch_count"] == 2
    assert payload["sample"]["requested_batch_offset"] == 1
    assert payload["artifacts"]["latest_markdown_path"].endswith("sample-latest.md")


def test_main_review_sample_summary_renders_text_and_artifacts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    sample_path = tmp_path / "sample.md"
    calls: dict[str, object] = {}

    def fake_summarize_review_sample(path: Path) -> dict[str, object]:
        calls["summarize"] = path
        return {"source_path": str(path), "reject_precision": 0.75, "samples": []}

    def fake_write_review_sample_summary_artifacts(
        project_root: Path, payload: dict[str, object]
    ) -> dict[str, str]:
        calls["write"] = (project_root, payload)
        return {"latest_markdown_path": str(project_root / "reports" / "sample-summary-latest.md")}

    monkeypatch.setattr(
        MODULE, "summarize_entity_alias_review_sample", fake_summarize_review_sample
    )
    monkeypatch.setattr(
        MODULE,
        "render_entity_alias_review_sample_summary",
        lambda payload: f"summary for {payload['source_path']}",
    )
    monkeypatch.setattr(
        MODULE,
        "write_entity_alias_review_sample_summary_artifacts",
        fake_write_review_sample_summary_artifacts,
    )

    _run_main(
        monkeypatch,
        [
            "review",
            "sample-summary",
            "--path",
            str(sample_path),
            "--project-root",
            str(tmp_path),
            "--write",
        ],
    )
    output = capsys.readouterr().out

    assert calls["summarize"] == sample_path
    assert calls["write"] == (
        tmp_path,
        {"source_path": str(sample_path), "reject_precision": 0.75, "samples": []},
    )
    assert "summary for" in output
    assert str(tmp_path / "reports" / "sample-summary-latest.md") in output


def test_main_review_sample_summary_json_mode_prints_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    sample_path = tmp_path / "sample.md"
    monkeypatch.setattr(
        MODULE,
        "summarize_entity_alias_review_sample",
        lambda path: {"source_path": str(path), "reject_precision": 0.5, "samples": []},
    )
    monkeypatch.setattr(
        MODULE,
        "write_entity_alias_review_sample_summary_artifacts",
        lambda project_root, payload: {
            "latest_json_path": str(project_root / "reports" / "sample-summary-latest.json")
        },
    )

    _run_main(
        monkeypatch,
        [
            "review",
            "sample-summary",
            "--path",
            str(sample_path),
            "--project-root",
            str(tmp_path),
            "--write",
            "--json",
        ],
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["source_path"] == str(sample_path)
    assert payload["reject_precision"] == 0.5
    assert payload["artifacts"]["latest_json_path"].endswith("sample-summary-latest.json")


def test_main_review_sample_propose_renders_text_and_artifacts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    sample_path = tmp_path / "sample.md"
    calls: dict[str, object] = {}

    def fake_propose_review_sample(path: Path) -> dict[str, object]:
        calls["propose"] = path
        return {"source_path": str(path), "assistant_outcome_counts": {"reject": 3}, "samples": []}

    def fake_write_review_sample_proposal_artifacts(
        project_root: Path, payload: dict[str, object]
    ) -> dict[str, str]:
        calls["write"] = (project_root, payload)
        return {"latest_markdown_path": str(project_root / "reports" / "sample-proposal-latest.md")}

    monkeypatch.setattr(MODULE, "propose_entity_alias_review_sample", fake_propose_review_sample)
    monkeypatch.setattr(
        MODULE,
        "render_entity_alias_review_sample_proposal",
        lambda payload: f"proposal for {payload['source_path']}",
    )
    monkeypatch.setattr(
        MODULE,
        "write_entity_alias_review_sample_proposal_artifacts",
        fake_write_review_sample_proposal_artifacts,
    )

    _run_main(
        monkeypatch,
        [
            "review",
            "sample-propose",
            "--path",
            str(sample_path),
            "--project-root",
            str(tmp_path),
            "--write",
        ],
    )
    output = capsys.readouterr().out

    assert calls["propose"] == sample_path
    assert calls["write"] == (
        tmp_path,
        {"source_path": str(sample_path), "assistant_outcome_counts": {"reject": 3}, "samples": []},
    )
    assert "proposal for" in output
    assert str(tmp_path / "reports" / "sample-proposal-latest.md") in output


def test_main_review_sample_propose_json_mode_prints_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    sample_path = tmp_path / "sample.md"
    monkeypatch.setattr(
        MODULE,
        "propose_entity_alias_review_sample",
        lambda path: {
            "source_path": str(path),
            "assistant_outcome_counts": {"reject": 2},
            "samples": [],
        },
    )
    monkeypatch.setattr(
        MODULE,
        "write_entity_alias_review_sample_proposal_artifacts",
        lambda project_root, payload: {
            "latest_json_path": str(project_root / "reports" / "sample-proposal-latest.json")
        },
    )

    _run_main(
        monkeypatch,
        [
            "review",
            "sample-propose",
            "--path",
            str(sample_path),
            "--project-root",
            str(tmp_path),
            "--write",
            "--json",
        ],
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["source_path"] == str(sample_path)
    assert payload["assistant_outcome_counts"]["reject"] == 2
    assert payload["artifacts"]["latest_json_path"].endswith("sample-proposal-latest.json")


def test_main_review_sample_compare_renders_text_and_artifacts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    sample_path = tmp_path / "sample.md"
    proposal_path = tmp_path / "proposal.json"
    calls: dict[str, object] = {}

    def fake_compare_review_sample(sample: Path, proposal: Path) -> dict[str, object]:
        calls["compare"] = (sample, proposal)
        return {"sample_path": str(sample), "proposal_path": str(proposal), "agreement_rate": 0.75}

    def fake_write_review_sample_compare_artifacts(
        project_root: Path, payload: dict[str, object]
    ) -> dict[str, str]:
        calls["write"] = (project_root, payload)
        return {"latest_markdown_path": str(project_root / "reports" / "sample-compare-latest.md")}

    monkeypatch.setattr(
        MODULE, "compare_entity_alias_review_sample_to_proposal", fake_compare_review_sample
    )
    monkeypatch.setattr(
        MODULE,
        "render_entity_alias_review_sample_comparison",
        lambda payload: f"comparison for {payload['sample_path']} vs {payload['proposal_path']}",
    )
    monkeypatch.setattr(
        MODULE,
        "write_entity_alias_review_sample_comparison_artifacts",
        fake_write_review_sample_compare_artifacts,
    )

    _run_main(
        monkeypatch,
        [
            "review",
            "sample-compare",
            "--sample-path",
            str(sample_path),
            "--proposal-path",
            str(proposal_path),
            "--project-root",
            str(tmp_path),
            "--write",
        ],
    )
    output = capsys.readouterr().out

    assert calls["compare"] == (sample_path, proposal_path)
    assert calls["write"] == (
        tmp_path,
        {
            "sample_path": str(sample_path),
            "proposal_path": str(proposal_path),
            "agreement_rate": 0.75,
        },
    )
    assert "comparison for" in output
    assert str(tmp_path / "reports" / "sample-compare-latest.md") in output


def test_main_review_sample_compare_json_mode_prints_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    sample_path = tmp_path / "sample.md"
    proposal_path = tmp_path / "proposal.json"
    monkeypatch.setattr(
        MODULE,
        "compare_entity_alias_review_sample_to_proposal",
        lambda sample, proposal: {
            "sample_path": str(sample),
            "proposal_path": str(proposal),
            "agreement_rate": 0.5,
        },
    )
    monkeypatch.setattr(
        MODULE,
        "write_entity_alias_review_sample_comparison_artifacts",
        lambda project_root, payload: {
            "latest_json_path": str(project_root / "reports" / "sample-compare-latest.json")
        },
    )

    _run_main(
        monkeypatch,
        [
            "review",
            "sample-compare",
            "--sample-path",
            str(sample_path),
            "--proposal-path",
            str(proposal_path),
            "--project-root",
            str(tmp_path),
            "--write",
            "--json",
        ],
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["sample_path"] == str(sample_path)
    assert payload["proposal_path"] == str(proposal_path)
    assert payload["agreement_rate"] == 0.5
    assert payload["artifacts"]["latest_json_path"].endswith("sample-compare-latest.json")


def test_main_review_campaign_renders_text_and_artifacts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    calls: dict[str, object] = {}

    def fake_build_review_campaign(
        project_root: Path, *, batch_size: int, scenario_labels: list[str] | None = None
    ) -> dict[str, object]:
        calls["build"] = (project_root, batch_size, scenario_labels)
        return {
            "project_root": str(project_root),
            "scenario_count": 1,
            "scenarios": [{"label": "likely_front"}],
        }

    def fake_write_review_campaign_artifacts(
        project_root: Path, payload: dict[str, object]
    ) -> dict[str, object]:
        calls["write"] = (project_root, payload)
        return {
            "latest_markdown_path": str(project_root / "reports" / "campaign-latest.md"),
            "scenario_artifacts": {
                "likely_front": {
                    "sample": {
                        "session_markdown_path": str(project_root / "reports" / "sample-front.md")
                    }
                }
            },
        }

    monkeypatch.setattr(MODULE, "build_entity_alias_review_campaign", fake_build_review_campaign)
    monkeypatch.setattr(
        MODULE,
        "render_entity_alias_review_campaign",
        lambda payload: f"campaign for {payload['scenario_count']} scenario",
    )
    monkeypatch.setattr(
        MODULE,
        "write_entity_alias_review_campaign_artifacts",
        fake_write_review_campaign_artifacts,
    )

    _run_main(
        monkeypatch,
        [
            "review",
            "campaign",
            "--project-root",
            str(tmp_path),
            "--batch-size",
            "12",
            "--scenario",
            "likely_front",
            "--write",
        ],
    )
    output = capsys.readouterr().out

    assert calls["build"] == (tmp_path, 12, ["likely_front"])
    assert calls["write"] == (
        tmp_path,
        {
            "project_root": str(tmp_path),
            "scenario_count": 1,
            "scenarios": [{"label": "likely_front"}],
        },
    )
    assert "campaign for 1 scenario" in output
    assert str(tmp_path / "reports" / "campaign-latest.md") in output
    assert str(tmp_path / "reports" / "sample-front.md") in output


def test_main_review_campaign_json_mode_prints_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(
        MODULE,
        "build_entity_alias_review_campaign",
        lambda project_root, *, batch_size, scenario_labels=None: {
            "project_root": str(project_root),
            "scenario_count": 2,
            "selected_scenarios": list(scenario_labels or []),
        },
    )
    monkeypatch.setattr(
        MODULE,
        "write_entity_alias_review_campaign_artifacts",
        lambda project_root, payload: {
            "latest_json_path": str(project_root / "reports" / "campaign-latest.json")
        },
    )

    _run_main(
        monkeypatch,
        [
            "review",
            "campaign",
            "--project-root",
            str(tmp_path),
            "--scenario",
            "likely_front",
            "--scenario",
            "needs_context",
            "--write",
            "--json",
        ],
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["project_root"] == str(tmp_path)
    assert payload["scenario_count"] == 2
    assert payload["selected_scenarios"] == ["likely_front", "needs_context"]
    assert payload["artifacts"]["latest_json_path"].endswith("campaign-latest.json")


def test_main_review_campaign_index_renders_text_and_artifacts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    calls: dict[str, object] = {}

    def fake_build_campaign_index(project_root: Path) -> dict[str, object]:
        calls["build"] = project_root
        return {"project_root": str(project_root), "campaign_count": 1, "packet_count": 2}

    def fake_write_campaign_index_artifacts(
        project_root: Path, payload: dict[str, object]
    ) -> dict[str, str]:
        calls["write"] = (project_root, payload)
        return {"latest_markdown_path": str(project_root / "reports" / "campaign-index-latest.md")}

    monkeypatch.setattr(
        MODULE, "build_entity_alias_review_campaign_index", fake_build_campaign_index
    )
    monkeypatch.setattr(
        MODULE,
        "render_entity_alias_review_campaign_index",
        lambda payload: f"index for {payload['packet_count']} packets",
    )
    monkeypatch.setattr(
        MODULE,
        "write_entity_alias_review_campaign_index_artifacts",
        fake_write_campaign_index_artifacts,
    )

    _run_main(
        monkeypatch,
        [
            "review",
            "campaign-index",
            "--project-root",
            str(tmp_path),
            "--write",
        ],
    )
    output = capsys.readouterr().out

    assert calls["build"] == tmp_path
    assert calls["write"] == (
        tmp_path,
        {"project_root": str(tmp_path), "campaign_count": 1, "packet_count": 2},
    )
    assert "index for 2 packets" in output
    assert str(tmp_path / "reports" / "campaign-index-latest.md") in output


def test_main_review_campaign_rollup_json_mode_prints_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(
        MODULE,
        "build_entity_alias_review_rollup",
        lambda project_root, *, packet_statuses=None, scenario_labels=None, packet_ids=None, campaign_ids=None: {
            "project_root": str(project_root),
            "selected_packet_count": 1,
            "filters": {
                "packet_statuses": list(packet_statuses or []),
                "scenario_labels": list(scenario_labels or []),
                "packet_ids": list(packet_ids or []),
                "campaign_ids": list(campaign_ids or []),
            },
        },
    )
    monkeypatch.setattr(
        MODULE,
        "write_entity_alias_review_rollup_artifacts",
        lambda project_root, payload: {
            "latest_json_path": str(project_root / "reports" / "rollup-latest.json")
        },
    )

    _run_main(
        monkeypatch,
        [
            "review",
            "campaign-rollup",
            "--project-root",
            str(tmp_path),
            "--status",
            "complete",
            "--scenario",
            "likely_front",
            "--packet-id",
            "packet-001",
            "--campaign-id",
            "campaign-001",
            "--write",
            "--json",
        ],
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["project_root"] == str(tmp_path)
    assert payload["selected_packet_count"] == 1
    assert payload["filters"]["packet_statuses"] == ["complete"]
    assert payload["filters"]["scenario_labels"] == ["likely_front"]
    assert payload["filters"]["packet_ids"] == ["packet-001"]
    assert payload["filters"]["campaign_ids"] == ["campaign-001"]
    assert payload["artifacts"]["latest_json_path"].endswith("rollup-latest.json")


def test_main_review_reject_stage_renders_text_and_artifacts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    calls: dict[str, object] = {}

    def fake_build_reject_stage(
        project_root: Path,
        *,
        packet_statuses=None,
        scenario_labels=None,
        packet_ids=None,
        campaign_ids=None,
        min_reject_precision: float,
        min_adjudicated: int,
    ) -> dict[str, object]:
        calls["build"] = (
            project_root,
            list(packet_statuses or []),
            list(scenario_labels or []),
            list(packet_ids or []),
            list(campaign_ids or []),
            min_reject_precision,
            min_adjudicated,
        )
        return {"project_root": str(project_root), "stage_status": "blocked", "candidate_count": 0}

    def fake_write_reject_stage_artifacts(
        project_root: Path, payload: dict[str, object]
    ) -> dict[str, str]:
        calls["write"] = (project_root, payload)
        return {"latest_markdown_path": str(project_root / "reports" / "reject-stage-latest.md")}

    monkeypatch.setattr(MODULE, "build_entity_alias_reject_stage", fake_build_reject_stage)
    monkeypatch.setattr(
        MODULE,
        "render_entity_alias_reject_stage",
        lambda payload: f"stage status {payload['stage_status']}",
    )
    monkeypatch.setattr(
        MODULE,
        "write_entity_alias_reject_stage_artifacts",
        fake_write_reject_stage_artifacts,
    )

    _run_main(
        monkeypatch,
        [
            "review",
            "reject-stage",
            "--project-root",
            str(tmp_path),
            "--status",
            "partial",
            "--scenario",
            "needs_context",
            "--packet-id",
            "packet-002",
            "--campaign-id",
            "campaign-002",
            "--min-reject-precision",
            "0.9",
            "--min-adjudicated",
            "5",
            "--write",
        ],
    )
    output = capsys.readouterr().out

    assert calls["build"] == (
        tmp_path,
        ["partial"],
        ["needs_context"],
        ["packet-002"],
        ["campaign-002"],
        0.9,
        5,
    )
    assert calls["write"] == (
        tmp_path,
        {"project_root": str(tmp_path), "stage_status": "blocked", "candidate_count": 0},
    )
    assert "stage status blocked" in output
    assert str(tmp_path / "reports" / "reject-stage-latest.md") in output


def test_main_review_packet_hydrate_json_mode_prints_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    sample_path = tmp_path / "sample.md"
    monkeypatch.setattr(
        MODULE,
        "hydrate_entity_alias_review_sample_packet",
        lambda path: {"source_path": str(path), "valid": True, "samples": []},
    )
    monkeypatch.setattr(
        MODULE,
        "write_entity_alias_review_packet_hydration_artifacts",
        lambda project_root, payload: {
            "latest_json_path": str(project_root / "reports" / "packet-hydrate-latest.json")
        },
    )

    _run_main(
        monkeypatch,
        [
            "review",
            "packet-hydrate",
            "--path",
            str(sample_path),
            "--project-root",
            str(tmp_path),
            "--write",
            "--json",
        ],
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["source_path"] == str(sample_path)
    assert payload["valid"] is True
    assert payload["artifacts"]["latest_json_path"].endswith("packet-hydrate-latest.json")


def test_main_review_campaign_scoreboard_renders_text_and_artifacts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    calls: dict[str, object] = {}

    def fake_build_scoreboard(
        project_root: Path, *, min_reject_precision: float, min_adjudicated: int
    ) -> dict[str, object]:
        calls["build"] = (project_root, min_reject_precision, min_adjudicated)
        return {"project_root": str(project_root), "candidate_packet_count": 3, "packets": []}

    def fake_write_scoreboard_artifacts(
        project_root: Path, payload: dict[str, object]
    ) -> dict[str, str]:
        calls["write"] = (project_root, payload)
        return {"latest_markdown_path": str(project_root / "reports" / "scoreboard-latest.md")}

    monkeypatch.setattr(MODULE, "build_entity_alias_review_scoreboard", fake_build_scoreboard)
    monkeypatch.setattr(
        MODULE,
        "render_entity_alias_review_scoreboard",
        lambda payload: f"scoreboard for {payload['candidate_packet_count']} packets",
    )
    monkeypatch.setattr(
        MODULE,
        "write_entity_alias_review_scoreboard_artifacts",
        fake_write_scoreboard_artifacts,
    )

    _run_main(
        monkeypatch,
        [
            "review",
            "campaign-scoreboard",
            "--project-root",
            str(tmp_path),
            "--min-reject-precision",
            "0.9",
            "--min-adjudicated",
            "12",
            "--write",
        ],
    )
    output = capsys.readouterr().out

    assert calls["build"] == (tmp_path, 0.9, 12)
    assert calls["write"] == (
        tmp_path,
        {"project_root": str(tmp_path), "candidate_packet_count": 3, "packets": []},
    )
    assert "scoreboard for 3 packets" in output
    assert str(tmp_path / "reports" / "scoreboard-latest.md") in output


def test_main_review_apply_plan_json_mode_prints_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(
        MODULE,
        "build_entity_alias_review_apply_plan",
        lambda project_root, *, packet_statuses=None, scenario_labels=None, packet_ids=None, campaign_ids=None, min_reject_precision, min_adjudicated: {
            "project_root": str(project_root),
            "apply_status": "disabled",
            "filters": {
                "packet_statuses": list(packet_statuses or []),
                "scenario_labels": list(scenario_labels or []),
                "packet_ids": list(packet_ids or []),
                "campaign_ids": list(campaign_ids or []),
            },
        },
    )
    monkeypatch.setattr(
        MODULE,
        "write_entity_alias_review_apply_plan_artifacts",
        lambda project_root, payload: {
            "latest_json_path": str(project_root / "reports" / "apply-plan-latest.json")
        },
    )

    _run_main(
        monkeypatch,
        [
            "review",
            "apply-plan",
            "--project-root",
            str(tmp_path),
            "--status",
            "pending",
            "--scenario",
            "likely_front",
            "--packet-id",
            "packet-003",
            "--campaign-id",
            "campaign-003",
            "--write",
            "--json",
        ],
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["project_root"] == str(tmp_path)
    assert payload["apply_status"] == "disabled"
    assert payload["filters"]["packet_statuses"] == ["pending"]
    assert payload["filters"]["scenario_labels"] == ["likely_front"]
    assert payload["filters"]["packet_ids"] == ["packet-003"]
    assert payload["filters"]["campaign_ids"] == ["campaign-003"]
    assert payload["artifacts"]["latest_json_path"].endswith("apply-plan-latest.json")


def test_main_source_freshness_prints_json_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(
        MODULE,
        "compute_source_freshness",
        lambda corpus_root: {"state": "fresh", "corpus_root": str(corpus_root)},
    )

    _run_main(monkeypatch, ["source", "freshness", str(tmp_path)])
    payload = json.loads(capsys.readouterr().out)

    assert payload == {"state": "fresh", "corpus_root": str(tmp_path)}


def test_main_dashboard_text_mode_uses_renderer_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    project_root = tmp_path / "project"
    source_drop_root = tmp_path / "source-drop"
    monkeypatch.setattr(
        MODULE,
        "build_dashboard",
        lambda project, source_drop: {
            "project_root": str(project),
            "source_drop_root": str(source_drop),
        },
    )
    monkeypatch.setattr(
        MODULE,
        "render_dashboard_text",
        lambda payload: (
            f"dashboard for {payload['project_root']} via {payload['source_drop_root']}"
        ),
    )

    _run_main(
        monkeypatch,
        [
            "dashboard",
            "--project-root",
            str(project_root),
            "--source-drop-root",
            str(source_drop_root),
        ],
    )
    output = capsys.readouterr().out

    assert output.strip() == f"dashboard for {project_root} via {source_drop_root}"


def test_main_schema_validate_non_json_mode_exits_on_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    schema_name = next(iter(MODULE.SCHEMA_CATALOG))
    monkeypatch.setattr(
        MODULE,
        "validate_json_file",
        lambda name, path: {
            "valid": False,
            "path": str(path),
            "errors": [{"path": "$.field", "message": "is required"}],
        },
    )

    with pytest.raises(SystemExit) as exc_info:
        _run_main(
            monkeypatch,
            ["schema", "validate", schema_name, "--path", str(tmp_path / "payload.json")],
        )

    output = capsys.readouterr().out
    assert exc_info.value.code == 1
    assert f"FAIL {schema_name}" in output
    assert "- $.field: is required" in output
