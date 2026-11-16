from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conversation_corpus_engine.provider_catalog import (  # noqa: E402
    ProviderManifestError,
    conventional_corpus_root,
    default_source_drop_root,
    get_provider_config,
    load_provider_manifest,
    provider_bootstrap_report_path,
    provider_corpus_targets,
)
from conversation_corpus_engine.source_policy import set_source_policy  # noqa: E402


def seed_contract(root: Path, *, adapter_type: str = "chatgpt-history") -> None:
    corpus_dir = root / "corpus"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    (corpus_dir / "contract.json").write_text(
        json.dumps(
            {
                "contract_name": "conversation-corpus-engine-v1",
                "contract_version": 1,
                "adapter_type": adapter_type,
                "name": "Seed Corpus",
            }
        ),
        encoding="utf-8",
    )


def provider_row(provider_id: str, alias: str) -> dict[str, object]:
    return {
        "provider_id": provider_id,
        "display_name": f"Configured {provider_id}",
        "adapter_state": "supported",
        "default_adapter_id": "session-meta-redacted-jsonl-v1",
        "adapter_type": "adapter-reusable",
        "discovery_mode": "redacted-bundle",
        "inbox_rel": f"{provider_id}/inbox",
        "default_corpus_id": f"{provider_id}-memory",
        "default_corpus_name": f"Configured {provider_id} Memory",
        "source_family_aliases": [alias],
        "source_contract": {
            "kind": "session-meta-redacted-bundle",
            "root_env": "CCE_TEST_BUNDLE_ROOT",
        },
        "authority_policy": "native-role",
        "owner_reference": "owner:test",
        "blocker": {
            "owner_reference": "owner:test",
            "failed_predicate": "redacted source bundle is readable",
            "next_action": "configure a frozen fixture and rerun",
        },
    }


def write_provider_manifest(path: Path, providers: list[dict[str, object]]) -> None:
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


def test_default_source_drop_root_prefers_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "override-source-drop"
    monkeypatch.setenv("CCE_SOURCE_DROP_ROOT", str(override))

    assert default_source_drop_root(tmp_path / "project") == override.resolve()


def test_default_source_drop_root_uses_project_parent_when_no_override(tmp_path: Path) -> None:
    project_root = tmp_path / "project"

    assert default_source_drop_root(project_root) == (tmp_path / "source-drop").resolve()


def test_get_provider_config_rejects_unknown_provider() -> None:
    with pytest.raises(KeyError, match="Unknown provider: unknown"):
        get_provider_config("unknown")


def test_instance_manifest_covers_native_cli_and_export_fixtures() -> None:
    expected = {
        "agy",
        "chatgpt",
        "claude",
        "codex",
        "gemini",
        "git-artifact",
        "grok",
        "opencode",
        "perplexity",
    }

    assert expected <= set(load_provider_manifest())
    assert "antigravity" in get_provider_config("agy")["source_family_aliases"]
    git_artifact = get_provider_config("git-artifact")
    assert git_artifact["adapter_type"] == "immutable-document"
    assert git_artifact["default_adapter_id"] == "session-meta-redacted-jsonl-v1"
    assert git_artifact["source_family_aliases"] == ["git-artifact"]
    assert git_artifact["authority_policy"] == "manifest-lane"


def test_runtime_manifest_accepts_renamed_and_new_providers_without_code_changes(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    renamed = provider_row("provider-renamed", "family-renamed")
    newly_added = provider_row("provider-new", "family-new")
    write_provider_manifest(first, [renamed, newly_added])
    write_provider_manifest(second, [newly_added, renamed])

    first_catalog = load_provider_manifest(first)
    second_catalog = load_provider_manifest(second)

    assert first_catalog == second_catalog
    assert list(first_catalog) == ["provider-new", "provider-renamed"]
    assert first_catalog["provider-new"]["source_family_aliases"] == ["family-new"]


def test_runtime_manifest_rejects_shared_alias_and_unsafe_inbox(tmp_path: Path) -> None:
    shared_alias = tmp_path / "shared-alias.json"
    write_provider_manifest(
        shared_alias,
        [provider_row("provider-a", "same-family"), provider_row("provider-b", "same-family")],
    )
    with pytest.raises(ProviderManifestError, match="source family alias"):
        load_provider_manifest(shared_alias)

    unsafe_inbox = tmp_path / "unsafe-inbox.json"
    row = provider_row("provider-a", "family-a")
    row["inbox_rel"] = "../outside"
    write_provider_manifest(unsafe_inbox, [row])
    with pytest.raises(ProviderManifestError, match="safe relative path"):
        load_provider_manifest(unsafe_inbox)


def test_reusable_provider_manifest_contains_no_configured_instance_names() -> None:
    repository = Path(__file__).resolve().parents[1]
    example = (
        repository
        / "src"
        / "conversation_corpus_engine"
        / "schemas"
        / "provider-manifest.example.v1.json"
    )
    serialized = example.read_text(encoding="utf-8").lower()

    for configured_name in ("organvm", "chatgpt", "claude", "gemini", "perplexity"):
        assert configured_name not in serialized


def test_provider_bootstrap_report_path_uses_reports_directory(tmp_path: Path) -> None:
    assert (
        provider_bootstrap_report_path(tmp_path / "project", "claude")
        == (tmp_path / "project" / "reports" / "claude-evaluation-bootstrap-latest.md").resolve()
    )


def test_conventional_corpus_root_uses_source_drop_parent(tmp_path: Path) -> None:
    source_drop_root = tmp_path / "source-drop"

    assert (
        conventional_corpus_root(source_drop_root, "chatgpt-history")
        == (tmp_path / "chatgpt-history").resolve()
    )


def test_provider_corpus_targets_prefers_viable_fallback_when_primary_missing(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    source_drop_root = tmp_path / "source-drop"
    fallback_root = tmp_path / "chatgpt-history"
    seed_contract(fallback_root)

    targets = provider_corpus_targets(
        project_root,
        "chatgpt",
        source_drop_root,
        registry=[
            {
                "corpus_id": "chatgpt-history",
                "root": str(fallback_root),
            }
        ],
    )

    assert [target["role"] for target in targets] == ["primary", "fallback"]
    assert targets[0]["selected"] is False
    assert targets[1]["selected"] is True
    assert targets[1]["corpus_id"] == "chatgpt-history"


def test_provider_corpus_targets_respects_explicit_primary_policy(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    source_drop_root = tmp_path / "source-drop"
    primary_root = tmp_path / "explicit-primary"
    fallback_root = tmp_path / "chatgpt-history"
    seed_contract(fallback_root)
    set_source_policy(
        project_root,
        "chatgpt",
        primary_root=primary_root,
        primary_corpus_id="chatgpt-history-memory",
        fallback_root=fallback_root,
        fallback_corpus_id="chatgpt-history",
        note="Keep the explicit primary selected until manually changed.",
    )

    targets = provider_corpus_targets(project_root, "chatgpt", source_drop_root)

    assert [target["role"] for target in targets] == ["primary", "fallback"]
    assert targets[0]["selected"] is True
    assert targets[1]["selected"] is False
