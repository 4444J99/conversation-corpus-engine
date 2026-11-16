from __future__ import annotations

from pathlib import Path

from conversation_corpus_engine.commercial_architecture import (
    build_commercial_h1_readiness,
    commercial_h1_json_path,
    write_commercial_h1_artifacts,
)
from conversation_corpus_engine.schema_validation import validate_json_file, validate_payload


def test_commercial_h1_readiness_contract_tracks_repo_and_external_actions(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    source_drop_root = tmp_path / "source-drop"

    payload = build_commercial_h1_readiness(
        project_root,
        source_drop_root=source_drop_root,
    )
    validation = validate_payload("commercial-h1-readiness", payload)

    assert validation["valid"], validation["errors"]
    assert payload["summary"]["commercial_h1_repo_ready"] is True
    assert payload["summary"]["repo_ready_action_count"] == 2
    assert payload["summary"]["external_actions_remaining"] == 4
    assert payload["package"]["readiness"]["mcp_distribution_ready"] is True
    assert payload["interface_contract"]["target_repo"] == "conversation-corpus--surfaces"
    assert payload["interface_contract"]["expected_output_signal"] == "INTERFACE_CONTRACT"


def test_commercial_h1_artifacts_validate_against_schema(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    payload = build_commercial_h1_readiness(project_root)

    artifacts = write_commercial_h1_artifacts(project_root, payload)
    validation = validate_json_file(
        "commercial-h1-readiness", commercial_h1_json_path(project_root)
    )

    assert validation["valid"], validation["errors"]
    assert artifacts["json_path"].endswith("commercial-h1-readiness.json")
    assert artifacts["markdown_path"].endswith("commercial-h1-readiness.md")
