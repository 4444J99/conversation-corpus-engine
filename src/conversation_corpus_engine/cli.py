from __future__ import annotations

import argparse
import json
from pathlib import Path

from .archive_commands import add_archive_parser, handle_archive
from .chatgpt_local_session import (
    discover_chatgpt_projects,
    fetch_chatgpt_project,
    load_project_registry,
    merge_project_discovery,
    render_project_status,
    save_project_registry,
    set_project_route,
    sync_chatgpt_projects,
)
from .corpus_candidates import (
    corpus_candidate_history_path,
    load_corpus_candidate_manifest,
    promote_corpus_candidate,
    review_corpus_candidate,
    rollback_corpus_promotion,
    stage_corpus_candidate,
)
from .corpus_store import register_corpus_store
from .dashboard import build_dashboard, render_dashboard_text
from .evaluation import run_corpus_evaluation
from .evaluation_bootstrap import bootstrap_provider_evaluation
from .federated_canon import (
    load_federated_review_history,
    load_federated_review_queue,
    resolve_federated_review_item,
)
from .federation import build_federation, list_registered_corpora, upsert_corpus
from .governance_candidates import (
    apply_policy_candidate,
    review_policy_candidate,
    rollback_policy_application,
    stage_policy_candidate,
)
from .governance_policy import load_or_create_promotion_policy
from .governance_replay import build_policy_replay_payload, write_policy_replay_artifacts
from .migration import migrate_corpus_v2, seed_registry_from_staging
from .paths import default_project_root
from .persona_extract import extract_persona_lexicon, write_persona_extract_artifacts
from .provider_catalog import default_source_drop_root
from .provider_discovery import discover_provider_uploads, render_provider_discovery_text
from .provider_import import import_provider_corpus
from .provider_readiness import (
    build_provider_readiness,
    render_provider_readiness_text,
    write_provider_readiness_reports,
)
from .provider_refresh import refresh_provider_corpus
from .schema_validation import (
    SCHEMA_CATALOG,
    list_schemas,
    load_schema,
    schema_path,
    validate_json_file,
)
from .source_lifecycle import compute_source_freshness
from .source_policy import load_source_policy, set_source_policy, source_policy_history_path
from .surface_exports import (
    build_mcp_context_payload,
    build_surface_manifest,
    export_surface_bundle,
    write_mcp_context_artifacts,
    write_surface_manifest_artifacts,
)
from .triage import (
    STANDARD_ENTITY_ALIAS_REVIEW_CAMPAIGN_SCENARIOS,
    build_entity_alias_reject_stage,
    build_entity_alias_review_apply_plan,
    build_entity_alias_review_assist,
    build_entity_alias_review_campaign,
    build_entity_alias_review_campaign_index,
    build_entity_alias_review_rollup,
    build_entity_alias_review_scoreboard,
    build_triage_plan,
    compare_entity_alias_review_sample_to_proposal,
    execute_triage_plan,
    filter_entity_alias_review_assist_groups,
    hydrate_entity_alias_review_sample_packet,
    propose_entity_alias_review_sample,
    render_entity_alias_reject_stage,
    render_entity_alias_review_apply_plan,
    render_entity_alias_review_assist,
    render_entity_alias_review_campaign,
    render_entity_alias_review_campaign_index,
    render_entity_alias_review_packet_hydration,
    render_entity_alias_review_rollup,
    render_entity_alias_review_sample,
    render_entity_alias_review_sample_comparison,
    render_entity_alias_review_sample_proposal,
    render_entity_alias_review_sample_summary,
    render_entity_alias_review_scoreboard,
    sample_entity_alias_review_assist_groups,
    select_entity_alias_review_assist_batch,
    summarize_entity_alias_review_sample,
    write_entity_alias_reject_stage_artifacts,
    write_entity_alias_review_apply_plan_artifacts,
    write_entity_alias_review_assist_artifacts,
    write_entity_alias_review_campaign_artifacts,
    write_entity_alias_review_campaign_index_artifacts,
    write_entity_alias_review_packet_hydration_artifacts,
    write_entity_alias_review_rollup_artifacts,
    write_entity_alias_review_sample_artifacts,
    write_entity_alias_review_sample_comparison_artifacts,
    write_entity_alias_review_sample_proposal_artifacts,
    write_entity_alias_review_sample_summary_artifacts,
    write_entity_alias_review_scoreboard_artifacts,
)


def parse_threshold_overrides(values: list[str] | None) -> dict[str, float]:
    overrides: dict[str, float] = {}
    for item in values or []:
        key, sep, raw_value = item.partition("=")
        if not sep or not key.strip() or not raw_value.strip():
            raise ValueError(f"Threshold override must be KEY=VALUE, got: {item}")
        overrides[key.strip()] = float(raw_value.strip())
    return overrides


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Conversation corpus engine")
    subparsers = parser.add_subparsers(dest="group", required=True)

    corpus = subparsers.add_parser("corpus", help="Manage registered corpora")
    corpus_sub = corpus.add_subparsers(dest="action", required=True)

    corpus_list = corpus_sub.add_parser("list", help="List registered corpora")
    corpus_list.add_argument("--project-root", type=Path, default=default_project_root())
    corpus_list.add_argument("--json", action="store_true")

    corpus_register = corpus_sub.add_parser("register", help="Register a corpus root")
    corpus_register.add_argument("corpus_root", type=Path)
    corpus_register.add_argument("--project-root", type=Path, default=default_project_root())
    corpus_register.add_argument("--corpus-id")
    corpus_register.add_argument("--name")
    corpus_register.add_argument("--default", action="store_true")

    corpus_store = subparsers.add_parser("corpus-store", help="Manage private corpus custody")
    corpus_store_sub = corpus_store.add_subparsers(dest="action", required=True)
    corpus_store_register = corpus_store_sub.add_parser(
        "register", help="Register the active private corpus-store root"
    )
    corpus_store_register.add_argument("--root", type=Path, required=True)
    corpus_store_register.add_argument("--project-root", type=Path, default=default_project_root())

    corpus_extract = corpus_sub.add_parser("persona-extract", help="Extract persona lexicons")
    corpus_extract.add_argument("--persona", required=True, help="Persona ID (e.g. rob, claude)")
    corpus_extract.add_argument("--source-corpus", type=Path, help="Path to session transcripts")
    corpus_extract.add_argument("--project-root", type=Path, default=default_project_root())
    corpus_extract.add_argument("--dry-run", action="store_true")
    corpus_extract.add_argument("--write", action="store_true")
    corpus_extract.add_argument("--json", action="store_true")

    federation = subparsers.add_parser("federation", help="Build federated outputs")
    federation_sub = federation.add_subparsers(dest="action", required=True)
    federation_build = federation_sub.add_parser("build", help="Build federation artifacts")
    federation_build.add_argument("--project-root", type=Path, default=default_project_root())

    migration = subparsers.add_parser("migration", help="Migration helpers")
    migration_sub = migration.add_subparsers(dest="action", required=True)
    migration_seed = migration_sub.add_parser(
        "seed-from-staging", help="Register corpora from a staging root"
    )
    migration_seed.add_argument("staging_root", type=Path)
    migration_seed.add_argument("--project-root", type=Path, default=default_project_root())
    migration_seed.add_argument("--prefer-default", default="chatgpt-history")

    migration_review_ids = migration_sub.add_parser(
        "review-ids", help="Migrate review IDs to fingerprinted format"
    )
    migration_review_ids.add_argument("--project-root", type=Path, default=default_project_root())
    migration_review_ids.add_argument("--dry-run", action="store_true")
    migration_review_ids.add_argument("--json", action="store_true")

    migration_corpus_v2 = migration_sub.add_parser(
        "corpus-v2", help="Plan or write an authorized v1-to-v2 corpus migration"
    )
    migration_corpus_v2.add_argument("--source-root", type=Path, required=True)
    migration_corpus_v2.add_argument("--destination-root", type=Path, required=True)
    migration_corpus_v2.add_argument("--project-root", type=Path, default=default_project_root())
    migration_corpus_v2.add_argument("--corpus-store-root", type=Path)
    migration_corpus_v2.add_argument("--write", action="store_true")
    migration_corpus_v2.add_argument("--json", action="store_true")

    provider = subparsers.add_parser("provider", help="Inspect provider intake and readiness")
    provider_sub = provider.add_subparsers(dest="action", required=True)
    provider_discover = provider_sub.add_parser(
        "discover", help="Inspect provider source-drop inboxes"
    )
    provider_discover.add_argument("--project-root", type=Path, default=default_project_root())
    provider_discover.add_argument("--source-drop-root", type=Path)
    provider_discover.add_argument("--json", action="store_true")
    provider_readiness = provider_sub.add_parser(
        "readiness", help="Build provider readiness summary"
    )
    provider_readiness.add_argument("--project-root", type=Path, default=default_project_root())
    provider_readiness.add_argument("--source-drop-root", type=Path)
    provider_readiness.add_argument("--json", action="store_true")
    provider_readiness.add_argument("--write", action="store_true")
    provider_import = provider_sub.add_parser(
        "import", help="Import a provider source into a corpus"
    )
    provider_import.add_argument("--project-root", type=Path, default=default_project_root())
    provider_import.add_argument(
        "--provider",
        choices=[
            "chatgpt",
            "claude",
            "gemini",
            "grok",
            "perplexity",
            "copilot",
            "deepseek",
            "mistral",
        ],
        required=True,
    )
    provider_import.add_argument("--mode", choices=["upload", "local-session"], default="upload")
    provider_import.add_argument("--source-drop-root", type=Path)
    provider_import.add_argument("--source-path", type=Path)
    provider_import.add_argument("--local-root", type=Path)
    provider_import.add_argument("--output-root", type=Path)
    provider_import.add_argument("--corpus-store-root", type=Path)
    provider_import.add_argument("--corpus-id")
    provider_import.add_argument("--name")
    provider_import.add_argument("--register", action="store_true")
    provider_import.add_argument("--build", action="store_true")
    provider_import.add_argument("--no-bootstrap-eval", action="store_true")
    provider_import.add_argument("--json", action="store_true")
    provider_bootstrap = provider_sub.add_parser(
        "bootstrap-eval", help="Scaffold manual evaluation files for a provider corpus"
    )
    provider_bootstrap.add_argument(
        "--provider",
        choices=[
            "chatgpt",
            "claude",
            "gemini",
            "grok",
            "perplexity",
            "copilot",
            "deepseek",
            "mistral",
        ],
        required=True,
    )
    provider_bootstrap.add_argument("--project-root", type=Path, default=default_project_root())
    provider_bootstrap.add_argument("--target-root", type=Path)
    provider_bootstrap.add_argument("--corpus-store-root", type=Path)
    provider_bootstrap.add_argument("--policy-path", type=Path)
    provider_bootstrap.add_argument("--full-eval", action="store_true")
    provider_bootstrap.add_argument("--json", action="store_true")
    provider_refresh = provider_sub.add_parser(
        "refresh", help="Import, evaluate, and stage a refreshed provider corpus"
    )
    provider_refresh.add_argument(
        "--provider",
        choices=[
            "chatgpt",
            "claude",
            "gemini",
            "grok",
            "perplexity",
            "copilot",
            "deepseek",
            "mistral",
        ],
        required=True,
    )
    provider_refresh.add_argument("--project-root", type=Path, default=default_project_root())
    provider_refresh.add_argument("--mode", choices=["upload", "local-session"])
    provider_refresh.add_argument("--source-drop-root", type=Path)
    provider_refresh.add_argument("--source-path", type=Path)
    provider_refresh.add_argument("--local-root", type=Path)
    provider_refresh.add_argument("--live-corpus-id")
    provider_refresh.add_argument("--candidate-root", type=Path)
    provider_refresh.add_argument("--corpus-store-root", type=Path)
    provider_refresh.add_argument("--no-bootstrap-eval", action="store_true")
    provider_refresh.add_argument("--no-eval", action="store_true")
    provider_refresh.add_argument("--approve", action="store_true")
    provider_refresh.add_argument("--promote", action="store_true")
    provider_refresh.add_argument("--note", default="")
    provider_refresh.add_argument(
        "--throttle",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="Voluntary CPU yield interval in hot loops (0=disabled, 0.001=recommended for background)",
    )
    provider_refresh.add_argument("--json", action="store_true")

    project = subparsers.add_parser("project", help="ChatGPT project extraction and lifecycle")
    project_sub = project.add_subparsers(dest="action", required=True)
    project_extract = project_sub.add_parser(
        "extract", help="Extract a ChatGPT project's files and conversations to a local directory"
    )
    project_extract.add_argument("--project-id", required=True, help="ChatGPT project ID (g-p-...)")
    project_extract.add_argument("--output", type=Path, required=True, help="Output directory")
    project_extract.add_argument("--json", action="store_true")
    project_discover = project_sub.add_parser(
        "discover", help="Scan ChatGPT projects and update the registry"
    )
    project_discover.add_argument("--project-root", type=Path, default=default_project_root())
    project_discover.add_argument("--json", action="store_true")
    project_status = project_sub.add_parser("status", help="Show project extraction status")
    project_status.add_argument("--project-root", type=Path, default=default_project_root())
    project_status.add_argument("--json", action="store_true")
    project_route = project_sub.add_parser(
        "route", help="Set the delivery destination for a project"
    )
    project_route.add_argument("--project-id", required=True, help="ChatGPT project ID (g-p-...)")
    project_route.add_argument("--destination", required=True, help="Output directory path")
    project_route.add_argument("--organ", default="", help="Target organ (e.g. ORGAN-III)")
    project_route.add_argument("--repo", default="", help="Target repo name")
    project_route.add_argument("--project-root", type=Path, default=default_project_root())
    project_route.add_argument("--json", action="store_true")
    project_sync = project_sub.add_parser(
        "sync", help="Extract queued projects to their routed destinations"
    )
    project_sync.add_argument("--project-root", type=Path, default=default_project_root())
    project_sync.add_argument("--batch-size", type=int, default=5)
    project_sync.add_argument("--json", action="store_true")

    schema = subparsers.add_parser("schema", help="Inspect and validate published artifact schemas")
    schema_sub = schema.add_subparsers(dest="action", required=True)
    schema_list = schema_sub.add_parser("list", help="List published schema contracts")
    schema_list.add_argument("--json", action="store_true")
    schema_show = schema_sub.add_parser("show", help="Show a schema contract")
    schema_show.add_argument("schema_name", choices=sorted(SCHEMA_CATALOG))
    schema_show.add_argument("--json", action="store_true")
    schema_validate = schema_sub.add_parser(
        "validate", help="Validate a JSON artifact against a schema"
    )
    schema_validate.add_argument("schema_name", choices=sorted(SCHEMA_CATALOG))
    schema_validate.add_argument("--path", type=Path, required=True)
    schema_validate.add_argument("--json", action="store_true")

    surface = subparsers.add_parser("surface", help="Export Meta/MCP-facing surface artifacts")
    surface_sub = surface.add_subparsers(dest="action", required=True)
    surface_manifest = surface_sub.add_parser(
        "manifest", help="Write the engine-facing surface manifest"
    )
    surface_manifest.add_argument("--project-root", type=Path, default=default_project_root())
    surface_manifest.add_argument("--source-drop-root", type=Path)
    surface_context = surface_sub.add_parser("context", help="Write the MCP-facing context payload")
    surface_context.add_argument("--project-root", type=Path, default=default_project_root())
    surface_context.add_argument("--source-drop-root", type=Path)
    surface_bundle = surface_sub.add_parser(
        "bundle", help="Write both exported surfaces and validation bundle"
    )
    surface_bundle.add_argument("--project-root", type=Path, default=default_project_root())
    surface_bundle.add_argument("--source-drop-root", type=Path)

    source_policy = subparsers.add_parser(
        "source-policy", help="Manage provider source authority policies"
    )
    source_policy_sub = source_policy.add_subparsers(dest="action", required=True)
    source_policy_show = source_policy_sub.add_parser("show", help="Show a provider source policy")
    source_policy_show.add_argument("--project-root", type=Path, default=default_project_root())
    source_policy_show.add_argument(
        "--provider",
        choices=[
            "chatgpt",
            "claude",
            "gemini",
            "grok",
            "perplexity",
            "copilot",
            "deepseek",
            "mistral",
        ],
        required=True,
    )
    source_policy_show.add_argument("--json", action="store_true")
    source_policy_set = source_policy_sub.add_parser("set", help="Set a provider source policy")
    source_policy_set.add_argument("--project-root", type=Path, default=default_project_root())
    source_policy_set.add_argument(
        "--provider",
        choices=[
            "chatgpt",
            "claude",
            "gemini",
            "grok",
            "perplexity",
            "copilot",
            "deepseek",
            "mistral",
        ],
        required=True,
    )
    source_policy_set.add_argument("--primary-root", type=Path, required=True)
    source_policy_set.add_argument("--primary-corpus-id", required=True)
    source_policy_set.add_argument("--fallback-root", type=Path)
    source_policy_set.add_argument("--fallback-corpus-id")
    source_policy_set.add_argument("--decision", default="manual")
    source_policy_set.add_argument("--note", default="")
    source_policy_set.add_argument("--json", action="store_true")
    source_policy_history = source_policy_sub.add_parser(
        "history", help="Show source policy history"
    )
    source_policy_history.add_argument("--project-root", type=Path, default=default_project_root())
    source_policy_history.add_argument("--json", action="store_true")

    policy = subparsers.add_parser("policy", help="Replay and govern promotion policy")
    policy_sub = policy.add_subparsers(dest="action", required=True)
    policy_show = policy_sub.add_parser("show", help="Show the live promotion policy")
    policy_show.add_argument("--project-root", type=Path, default=default_project_root())
    policy_show.add_argument("--json", action="store_true")
    policy_replay = policy_sub.add_parser(
        "replay", help="Replay the live or overridden policy against active corpora"
    )
    policy_replay.add_argument("--project-root", type=Path, default=default_project_root())
    policy_replay.add_argument("--set-threshold", action="append", default=[])
    policy_replay.add_argument("--write", action="store_true")
    policy_replay.add_argument("--json", action="store_true")
    policy_stage = policy_sub.add_parser("stage", help="Stage a policy candidate")
    policy_stage.add_argument("--project-root", type=Path, default=default_project_root())
    policy_stage.add_argument("--set-threshold", action="append", required=True)
    policy_stage.add_argument("--note", default="")
    policy_stage.add_argument("--json", action="store_true")
    policy_review = policy_sub.add_parser("review", help="Review a staged policy candidate")
    policy_review.add_argument("--project-root", type=Path, default=default_project_root())
    policy_review.add_argument("--candidate-id", default="latest")
    policy_review.add_argument("--decision", choices=["approve", "reject"], required=True)
    policy_review.add_argument("--note", default="")
    policy_review.add_argument("--json", action="store_true")
    policy_apply = policy_sub.add_parser("apply", help="Apply an approved policy candidate")
    policy_apply.add_argument("--project-root", type=Path, default=default_project_root())
    policy_apply.add_argument("--candidate-id", default="latest")
    policy_apply.add_argument("--note", default="")
    policy_apply.add_argument("--json", action="store_true")
    policy_rollback = policy_sub.add_parser("rollback", help="Roll back the live promotion policy")
    policy_rollback.add_argument("--project-root", type=Path, default=default_project_root())
    policy_rollback.add_argument("--target", default="previous")
    policy_rollback.add_argument("--note", default="")
    policy_rollback.add_argument("--json", action="store_true")

    candidate = subparsers.add_parser("candidate", help="Stage and promote corpus candidates")
    candidate_sub = candidate.add_subparsers(dest="action", required=True)
    candidate_show = candidate_sub.add_parser("show", help="Show a corpus candidate manifest")
    candidate_show.add_argument("--project-root", type=Path, default=default_project_root())
    candidate_show.add_argument("--candidate-id", default="latest")
    candidate_show.add_argument("--json", action="store_true")
    candidate_history = candidate_sub.add_parser("history", help="Show corpus candidate history")
    candidate_history.add_argument("--project-root", type=Path, default=default_project_root())
    candidate_history.add_argument("--json", action="store_true")
    candidate_stage = candidate_sub.add_parser(
        "stage", help="Stage a candidate corpus against the live baseline"
    )
    candidate_stage.add_argument("--project-root", type=Path, default=default_project_root())
    candidate_stage.add_argument("--candidate-root", type=Path, required=True)
    candidate_stage.add_argument("--live-corpus-id")
    candidate_stage.add_argument(
        "--provider",
        choices=[
            "chatgpt",
            "claude",
            "gemini",
            "grok",
            "perplexity",
            "copilot",
            "deepseek",
            "mistral",
        ],
    )
    candidate_stage.add_argument("--note", default="")
    candidate_stage.add_argument("--json", action="store_true")
    candidate_review = candidate_sub.add_parser("review", help="Review a staged corpus candidate")
    candidate_review.add_argument("--project-root", type=Path, default=default_project_root())
    candidate_review.add_argument("--candidate-id", default="latest")
    candidate_review.add_argument("--decision", choices=["approve", "reject"], required=True)
    candidate_review.add_argument("--note", default="")
    candidate_review.add_argument("--json", action="store_true")
    candidate_promote = candidate_sub.add_parser(
        "promote", help="Promote an approved corpus candidate"
    )
    candidate_promote.add_argument("--project-root", type=Path, default=default_project_root())
    candidate_promote.add_argument("--candidate-id", default="latest")
    candidate_promote.add_argument("--note", default="")
    candidate_promote.add_argument("--json", action="store_true")
    candidate_rollback = candidate_sub.add_parser(
        "rollback", help="Roll back the most recent corpus promotion"
    )
    candidate_rollback.add_argument("--project-root", type=Path, default=default_project_root())
    candidate_rollback.add_argument("--target", default="previous")
    candidate_rollback.add_argument("--note", default="")
    candidate_rollback.add_argument("--json", action="store_true")

    evaluation = subparsers.add_parser("evaluation", help="Seed and run corpus evaluation")
    evaluation_sub = evaluation.add_subparsers(dest="action", required=True)
    evaluation_run = evaluation_sub.add_parser("run", help="Run evaluation for a corpus root")
    evaluation_run.add_argument("--root", type=Path, required=True)
    evaluation_run.add_argument("--project-root", type=Path, default=default_project_root())
    evaluation_run.add_argument("--corpus-store-root", type=Path)
    evaluation_run.add_argument("--seed", action="store_true")
    evaluation_run.add_argument("--markdown-output", type=Path)
    evaluation_run.add_argument("--json-output", type=Path)
    evaluation_run.add_argument("--json", action="store_true")

    review = subparsers.add_parser("review", help="Inspect and resolve federated review items")
    review_sub = review.add_subparsers(dest="action", required=True)
    review_queue = review_sub.add_parser("queue", help="Show the current federated review queue")
    review_queue.add_argument("--project-root", type=Path, default=default_project_root())
    review_queue.add_argument("--json", action="store_true")
    review_queue.add_argument("--limit", type=int, default=50)
    review_history = review_sub.add_parser("history", help="Show resolved federated review history")
    review_history.add_argument("--project-root", type=Path, default=default_project_root())
    review_history.add_argument("--json", action="store_true")
    review_history.add_argument("--limit", type=int, default=50)
    review_resolve = review_sub.add_parser("resolve", help="Resolve a federated review item")
    review_resolve.add_argument("review_id")
    review_resolve.add_argument("--project-root", type=Path, default=default_project_root())
    review_resolve.add_argument(
        "--decision", choices=["accepted", "rejected", "deferred"], required=True
    )
    review_resolve.add_argument("--note", required=True)
    review_resolve.add_argument("--canonical-subject")
    review_triage = review_sub.add_parser("triage", help="Auto-resolve review items by policy")
    review_triage.add_argument("--project-root", type=Path, default=default_project_root())
    review_triage.add_argument("--execute", action="store_true", help="Apply the triage plan")
    review_triage.add_argument("--json", action="store_true")
    review_assist = review_sub.add_parser(
        "assist",
        help="Build grouped operator guidance for the remaining entity-alias queue",
    )
    review_assist.add_argument("--project-root", type=Path, default=default_project_root())
    review_assist.add_argument("--batch-size", type=int, default=25)
    review_assist.add_argument("--group-limit", type=int, default=10)
    review_assist.add_argument(
        "--relation",
        dest="relations",
        action="append",
        help="Filter assist output to one or more relation types",
    )
    review_assist.add_argument("--source-pair", help="Filter assist output to a single source pair")
    review_assist.add_argument(
        "--anchor-contains",
        help="Filter assist output to anchors containing this normalized text",
    )
    review_assist.add_argument("--batch-id", help="Select a single precomputed assist batch")
    review_assist.add_argument(
        "--bucket",
        dest="buckets",
        action="append",
        help="Filter assist output to one or more review buckets",
    )
    review_assist.add_argument(
        "--sample-groups",
        type=int,
        help="Select a cross-batch sample of matching groups",
    )
    review_assist.add_argument(
        "--sample-batches",
        type=int,
        help="Restrict cross-batch sampling to the first N matching batches",
    )
    review_assist.add_argument(
        "--batch-offset",
        type=int,
        default=0,
        help="Skip the first N matching batches before sampling",
    )
    review_assist.add_argument(
        "--write",
        action="store_true",
        help="Write latest JSON/Markdown assist artifacts under reports/",
    )
    review_assist.add_argument("--json", action="store_true")
    review_campaign = review_sub.add_parser(
        "campaign",
        help="Build the standard multi-window entity-alias evidence campaign",
    )
    review_campaign.add_argument("--project-root", type=Path, default=default_project_root())
    review_campaign.add_argument("--batch-size", type=int, default=25)
    review_campaign.add_argument(
        "--scenario",
        dest="scenarios",
        action="append",
        choices=sorted(entry["label"] for entry in STANDARD_ENTITY_ALIAS_REVIEW_CAMPAIGN_SCENARIOS),
        help="Restrict the campaign to one or more named scenarios",
    )
    review_campaign.add_argument("--write", action="store_true")
    review_campaign.add_argument("--json", action="store_true")
    review_campaign_index = review_sub.add_parser(
        "campaign-index",
        help="Inventory generated review campaign packets, comparisons, and campaign manifests",
    )
    review_campaign_index.add_argument("--project-root", type=Path, default=default_project_root())
    review_campaign_index.add_argument("--write", action="store_true")
    review_campaign_index.add_argument("--json", action="store_true")
    review_packet_hydrate = review_sub.add_parser(
        "packet-hydrate",
        help="Hydrate and validate a completed review sample packet",
    )
    review_packet_hydrate.add_argument("--path", type=Path, required=True)
    review_packet_hydrate.add_argument("--project-root", type=Path, default=default_project_root())
    review_packet_hydrate.add_argument("--write", action="store_true")
    review_packet_hydrate.add_argument("--json", action="store_true")
    review_scoreboard = review_sub.add_parser(
        "campaign-scoreboard",
        help="Rank incomplete packets by how quickly they can unlock the reject-stage gate",
    )
    review_scoreboard.add_argument("--project-root", type=Path, default=default_project_root())
    review_scoreboard.add_argument("--min-reject-precision", type=float, default=0.95)
    review_scoreboard.add_argument("--min-adjudicated", type=int, default=20)
    review_scoreboard.add_argument("--write", action="store_true")
    review_scoreboard.add_argument("--json", action="store_true")
    review_rollup = review_sub.add_parser(
        "campaign-rollup",
        help="Aggregate comparison evidence across multiple review packets",
    )
    review_rollup.add_argument("--project-root", type=Path, default=default_project_root())
    review_rollup.add_argument(
        "--status",
        dest="statuses",
        action="append",
        choices=["pending", "partial", "complete", "empty"],
        help="Restrict the rollup to one or more packet adjudication states",
    )
    review_rollup.add_argument(
        "--scenario",
        dest="scenarios",
        action="append",
        choices=sorted(entry["label"] for entry in STANDARD_ENTITY_ALIAS_REVIEW_CAMPAIGN_SCENARIOS),
        help="Restrict the rollup to one or more named scenarios",
    )
    review_rollup.add_argument("--packet-id", dest="packet_ids", action="append")
    review_rollup.add_argument("--campaign-id", dest="campaign_ids", action="append")
    review_rollup.add_argument("--write", action="store_true")
    review_rollup.add_argument("--json", action="store_true")
    review_reject_stage = review_sub.add_parser(
        "reject-stage",
        help="Build a non-applying staged reject manifest from adjudicated review packets",
    )
    review_reject_stage.add_argument("--project-root", type=Path, default=default_project_root())
    review_reject_stage.add_argument(
        "--status",
        dest="statuses",
        action="append",
        choices=["pending", "partial", "complete", "empty"],
        help="Restrict the stage input to one or more packet adjudication states",
    )
    review_reject_stage.add_argument(
        "--scenario",
        dest="scenarios",
        action="append",
        choices=sorted(entry["label"] for entry in STANDARD_ENTITY_ALIAS_REVIEW_CAMPAIGN_SCENARIOS),
        help="Restrict the stage input to one or more named scenarios",
    )
    review_reject_stage.add_argument("--packet-id", dest="packet_ids", action="append")
    review_reject_stage.add_argument("--campaign-id", dest="campaign_ids", action="append")
    review_reject_stage.add_argument("--min-reject-precision", type=float, default=0.95)
    review_reject_stage.add_argument("--min-adjudicated", type=int, default=20)
    review_reject_stage.add_argument("--write", action="store_true")
    review_reject_stage.add_argument("--json", action="store_true")
    review_apply_plan = review_sub.add_parser(
        "apply-plan",
        help="Render the disabled pre-apply snapshot and rollback contract for future queue mutation",
    )
    review_apply_plan.add_argument("--project-root", type=Path, default=default_project_root())
    review_apply_plan.add_argument(
        "--status",
        dest="statuses",
        action="append",
        choices=["pending", "partial", "complete", "empty"],
        help="Restrict the plan input to one or more packet adjudication states",
    )
    review_apply_plan.add_argument(
        "--scenario",
        dest="scenarios",
        action="append",
        choices=sorted(entry["label"] for entry in STANDARD_ENTITY_ALIAS_REVIEW_CAMPAIGN_SCENARIOS),
        help="Restrict the plan input to one or more named scenarios",
    )
    review_apply_plan.add_argument("--packet-id", dest="packet_ids", action="append")
    review_apply_plan.add_argument("--campaign-id", dest="campaign_ids", action="append")
    review_apply_plan.add_argument("--min-reject-precision", type=float, default=0.95)
    review_apply_plan.add_argument("--min-adjudicated", type=int, default=20)
    review_apply_plan.add_argument("--write", action="store_true")
    review_apply_plan.add_argument("--json", action="store_true")
    review_sample_summary = review_sub.add_parser(
        "sample-summary",
        help="Summarize a completed entity-alias review sample packet",
    )
    review_sample_summary.add_argument("--path", type=Path, required=True)
    review_sample_summary.add_argument("--project-root", type=Path, default=default_project_root())
    review_sample_summary.add_argument("--write", action="store_true")
    review_sample_summary.add_argument("--json", action="store_true")
    review_sample_propose = review_sub.add_parser(
        "sample-propose",
        help="Generate assistant proposals for a review sample packet without mutating it",
    )
    review_sample_propose.add_argument("--path", type=Path, required=True)
    review_sample_propose.add_argument("--project-root", type=Path, default=default_project_root())
    review_sample_propose.add_argument("--write", action="store_true")
    review_sample_propose.add_argument("--json", action="store_true")
    review_sample_compare = review_sub.add_parser(
        "sample-compare",
        help="Compare manual sample outcomes against assistant proposal outcomes",
    )
    review_sample_compare.add_argument("--sample-path", type=Path, required=True)
    review_sample_compare.add_argument("--proposal-path", type=Path, required=True)
    review_sample_compare.add_argument("--project-root", type=Path, default=default_project_root())
    review_sample_compare.add_argument("--write", action="store_true")
    review_sample_compare.add_argument("--json", action="store_true")

    source = subparsers.add_parser("source", help="Inspect source freshness")
    source_sub = source.add_subparsers(dest="action", required=True)
    source_freshness = source_sub.add_parser("freshness", help="Compute corpus source freshness")
    source_freshness.add_argument("corpus_root", type=Path)

    dashboard = subparsers.add_parser("dashboard", help="Operator-facing health summary")
    dashboard.add_argument("--project-root", type=Path, default=default_project_root())
    dashboard.add_argument("--source-drop-root", type=Path)
    dashboard.add_argument("--json", action="store_true")

    add_archive_parser(subparsers)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.group == "archive":
        raise SystemExit(handle_archive(args))

    if args.group == "corpus" and args.action == "list":
        corpora = list_registered_corpora(args.project_root)
        if args.json:
            print(json.dumps(corpora, indent=2))
            return
        for entry in corpora:
            marker = "*" if entry.get("default") else "-"
            print(
                f"{marker} {entry['corpus_id']}: {entry['name']} [{entry.get('status', 'active')}]"
            )
            print(f"  root: {entry['root']}")
        return

    if args.group == "corpus" and args.action == "persona-extract":
        payload = extract_persona_lexicon(
            args.project_root,
            args.persona,
            source_corpus=args.source_corpus,
            dry_run=args.dry_run,
        )
        if args.write and not args.dry_run:
            artifacts = write_persona_extract_artifacts(args.project_root, payload)
            payload["artifacts_written"] = [str(p) for p in artifacts]
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"persona: {payload['persona_id']}  status: {payload['status']}")
            print(f"  archetype: {payload.get('archetypal_pattern') or '-'}")
            print(f"  yearning: {payload.get('ideal_yearning') or '-'}")
            print(f"  lexicon terms: {len(payload.get('vocabulary') or [])}")
            print(f"  forbidden terms: {len(payload.get('forbidden_terms') or [])}")
            for path in payload.get("artifacts_written", []):
                print(f"  wrote: {path}")
        return

    if args.group == "corpus" and args.action == "register":
        entry = upsert_corpus(
            args.project_root,
            args.corpus_root,
            corpus_id=args.corpus_id,
            name=args.name,
            make_default=args.default,
        )
        print(json.dumps(entry, indent=2))
        return

    if args.group == "corpus-store" and args.action == "register":
        registration = register_corpus_store(args.project_root, args.root)
        print(json.dumps(registration, indent=2))
        return

    if args.group == "federation" and args.action == "build":
        result = build_federation(args.project_root)
        print(json.dumps(result, indent=2))
        return

    if args.group == "migration" and args.action == "seed-from-staging":
        result = seed_registry_from_staging(
            args.project_root,
            args.staging_root,
            prefer_default=args.prefer_default,
        )
        print(json.dumps(result, indent=2))
        return

    if args.group == "migration" and args.action == "review-ids":
        from .federated_canon import migrate_review_ids  # noqa: PLC0415

        result = migrate_review_ids(args.project_root, dry_run=args.dry_run)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            stats = result.get("stats", {})
            print(f"Review-ID migration {'(dry run)' if args.dry_run else ''}")
            print(f"  Queue migrated:     {stats.get('queue_migrated', 0)}")
            print(f"  History migrated:   {stats.get('history_migrated', 0)}")
            print(f"  Decisions migrated: {stats.get('decisions_migrated', 0)}")
            print(f"  Unchanged:          {stats.get('unchanged', 0)}")
            print(f"  Unique mappings:    {result.get('id_count', 0)}")
        return

    if args.group == "migration" and args.action == "corpus-v2":
        result = migrate_corpus_v2(
            project_root=args.project_root,
            source_root=args.source_root,
            destination_root=args.destination_root,
            corpus_store_root=args.corpus_store_root,
            write=args.write,
        )
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(
                f"Corpus v2 migration ({result['mode']}): {result['collection_count']} collections"
            )
            print(f"  Source:      {result['source_root']}")
            print(f"  Destination: {result['destination_root']}")
        return

    if args.group == "provider" and args.action == "discover":
        source_drop_root = args.source_drop_root or default_source_drop_root(args.project_root)
        result = discover_provider_uploads(args.project_root, source_drop_root)
        if args.json:
            print(json.dumps(result, indent=2))
            return
        print(render_provider_discovery_text(result))
        return

    if args.group == "provider" and args.action == "readiness":
        source_drop_root = args.source_drop_root or default_source_drop_root(args.project_root)
        result = build_provider_readiness(args.project_root, source_drop_root)
        if args.write:
            report_paths = write_provider_readiness_reports(args.project_root, result)
            result = {**result, "report_paths": report_paths}
        if args.json:
            print(json.dumps(result, indent=2))
            return
        print(render_provider_readiness_text(result))
        return

    if args.group == "provider" and args.action == "import":
        result = import_provider_corpus(
            project_root=args.project_root,
            provider=args.provider,
            mode=args.mode,
            source_drop_root=args.source_drop_root,
            source_path=args.source_path,
            local_root=args.local_root,
            output_root=args.output_root,
            corpus_store_root=args.corpus_store_root,
            corpus_id=args.corpus_id,
            name=args.name,
            register=args.register,
            build=args.build,
            bootstrap_eval=not args.no_bootstrap_eval,
        )
        print(json.dumps(result, indent=2))
        return

    if args.group == "provider" and args.action == "bootstrap-eval":
        payload = bootstrap_provider_evaluation(
            project_root=args.project_root,
            provider=args.provider,
            target_root=args.target_root,
            policy_path=args.policy_path,
            full_eval=args.full_eval,
            corpus_store_root=args.corpus_store_root,
        )
        print(json.dumps(payload, indent=2))
        return

    if args.group == "provider" and args.action == "refresh":
        payload = refresh_provider_corpus(
            project_root=args.project_root,
            provider=args.provider,
            mode=args.mode,
            source_drop_root=args.source_drop_root,
            source_path=args.source_path,
            local_root=args.local_root,
            live_corpus_id=args.live_corpus_id,
            candidate_root=args.candidate_root,
            corpus_store_root=args.corpus_store_root,
            bootstrap_eval=not args.no_bootstrap_eval,
            run_eval=not args.no_eval,
            approve=args.approve or args.promote,
            promote=args.promote,
            note=args.note,
            throttle=args.throttle,
        )
        print(json.dumps(payload, indent=2))
        return

    if args.group == "project" and args.action == "extract":
        payload = fetch_chatgpt_project(
            args.project_id,
            args.output,
        )
        if args.json:
            print(json.dumps(payload, indent=2))
            return
        print(f"Project: {payload['project_name']}")
        print(f"Output: {payload['output_root']}")
        print(f"Files: {payload['file_count']}/{payload['total_files']}")
        print(f"Conversations: {payload['conversation_count']}")
        return

    if args.group == "project" and args.action == "discover":
        discovered = discover_chatgpt_projects()
        registry = load_project_registry(args.project_root)
        registry = merge_project_discovery(registry, discovered)
        path = save_project_registry(args.project_root, registry)
        if args.json:
            print(json.dumps(registry, indent=2))
            return
        print(f"Discovered {len(discovered)} projects, registry has {registry['project_count']}")
        print(f"Written to: {path}")
        return

    if args.group == "project" and args.action == "status":
        registry = load_project_registry(args.project_root)
        if args.json:
            print(json.dumps(registry, indent=2))
            return
        print(render_project_status(registry))
        return

    if args.group == "project" and args.action == "route":
        entry = set_project_route(
            args.project_root,
            args.project_id,
            args.destination,
            organ=args.organ,
            repo=args.repo,
        )
        if args.json:
            print(json.dumps(entry, indent=2))
            return
        print(f"Routed {args.project_id} -> {args.destination}")
        return

    if args.group == "project" and args.action == "sync":
        payload = sync_chatgpt_projects(args.project_root, batch_size=args.batch_size)
        if args.json:
            print(json.dumps(payload, indent=2))
            return
        print(f"Extracted: {payload['extracted_count']}")
        print(f"Failed: {payload['failed_count']}")
        print(f"Remaining: {payload['skipped_count']}")
        return

    if args.group == "schema" and args.action == "list":
        payload = {"count": len(SCHEMA_CATALOG), "schemas": list_schemas()}
        if args.json:
            print(json.dumps(payload, indent=2))
            return
        for entry in payload["schemas"]:
            print(f"- {entry['name']}: {entry['description']}")
            print(f"  path: {entry['path']}")
        return

    if args.group == "schema" and args.action == "show":
        payload = {
            "name": args.schema_name,
            "description": SCHEMA_CATALOG[args.schema_name]["description"],
            "path": str(schema_path(args.schema_name)),
            "schema": load_schema(args.schema_name),
        }
        print(json.dumps(payload if args.json else payload["schema"], indent=2))
        return

    if args.group == "schema" and args.action == "validate":
        payload = validate_json_file(args.schema_name, args.path)
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            status = "PASS" if payload["valid"] else "FAIL"
            print(f"{status} {args.schema_name} {payload['path']}")
            for issue in payload["errors"]:
                print(f"- {issue['path']}: {issue['message']}")
        if not payload["valid"]:
            raise SystemExit(1)
        return

    if args.group == "surface" and args.action == "manifest":
        payload = build_surface_manifest(
            args.project_root,
            source_drop_root=args.source_drop_root,
        )
        artifacts = write_surface_manifest_artifacts(args.project_root, payload)
        print(json.dumps({**payload, "artifacts_written": artifacts}, indent=2))
        return

    if args.group == "surface" and args.action == "context":
        payload = build_mcp_context_payload(
            args.project_root,
            source_drop_root=args.source_drop_root,
        )
        artifacts = write_mcp_context_artifacts(args.project_root, payload)
        print(json.dumps({**payload, "artifacts_written": artifacts}, indent=2))
        return

    if args.group == "surface" and args.action == "bundle":
        payload = export_surface_bundle(
            args.project_root,
            source_drop_root=args.source_drop_root,
        )
        print(json.dumps(payload, indent=2))
        return

    if args.group == "source-policy" and args.action == "show":
        payload = load_source_policy(args.project_root, args.provider)
        print(json.dumps(payload, indent=2))
        return

    if args.group == "source-policy" and args.action == "set":
        payload = set_source_policy(
            args.project_root,
            args.provider,
            primary_root=args.primary_root,
            primary_corpus_id=args.primary_corpus_id,
            fallback_root=args.fallback_root,
            fallback_corpus_id=args.fallback_corpus_id,
            decision=args.decision,
            note=args.note,
        )
        print(json.dumps(payload, indent=2))
        return

    if args.group == "source-policy" and args.action == "history":
        path = source_policy_history_path(args.project_root)
        payload = (
            json.loads(path.read_text(encoding="utf-8"))
            if path.exists()
            else {"generated_at": None, "count": 0, "items": []}
        )
        print(json.dumps(payload, indent=2))
        return

    if args.group == "policy" and args.action == "show":
        payload = load_or_create_promotion_policy(args.project_root)
        print(json.dumps(payload, indent=2))
        return

    if args.group == "policy" and args.action == "replay":
        payload = build_policy_replay_payload(
            args.project_root,
            threshold_overrides=parse_threshold_overrides(args.set_threshold) or None,
        )
        if args.write:
            payload = {
                **payload,
                "artifacts": write_policy_replay_artifacts(args.project_root, payload),
            }
        print(json.dumps(payload, indent=2))
        return

    if args.group == "policy" and args.action == "stage":
        payload = stage_policy_candidate(
            args.project_root,
            threshold_overrides=parse_threshold_overrides(args.set_threshold),
            note=args.note,
        )
        print(json.dumps(payload, indent=2))
        return

    if args.group == "policy" and args.action == "review":
        payload = review_policy_candidate(
            args.project_root,
            args.candidate_id,
            decision=args.decision,
            note=args.note,
        )
        print(json.dumps(payload, indent=2))
        return

    if args.group == "policy" and args.action == "apply":
        payload = apply_policy_candidate(
            args.project_root,
            args.candidate_id,
            note=args.note,
        )
        print(json.dumps(payload, indent=2))
        return

    if args.group == "policy" and args.action == "rollback":
        payload = rollback_policy_application(
            args.project_root,
            target=args.target,
            note=args.note,
        )
        print(json.dumps(payload, indent=2))
        return

    if args.group == "candidate" and args.action == "show":
        payload = load_corpus_candidate_manifest(args.project_root, candidate_id=args.candidate_id)
        print(json.dumps(payload, indent=2))
        return

    if args.group == "candidate" and args.action == "history":
        path = corpus_candidate_history_path(args.project_root)
        payload = (
            json.loads(path.read_text(encoding="utf-8"))
            if path.exists()
            else {"generated_at": None, "count": 0, "items": []}
        )
        print(json.dumps(payload, indent=2))
        return

    if args.group == "candidate" and args.action == "stage":
        payload = stage_corpus_candidate(
            args.project_root,
            candidate_root=args.candidate_root,
            live_corpus_id=args.live_corpus_id,
            provider=args.provider,
            note=args.note,
        )
        print(json.dumps(payload, indent=2))
        return

    if args.group == "candidate" and args.action == "review":
        payload = review_corpus_candidate(
            args.project_root,
            args.candidate_id,
            decision=args.decision,
            note=args.note,
        )
        print(json.dumps(payload, indent=2))
        return

    if args.group == "candidate" and args.action == "promote":
        payload = promote_corpus_candidate(
            args.project_root,
            args.candidate_id,
            note=args.note,
        )
        print(json.dumps(payload, indent=2))
        return

    if args.group == "candidate" and args.action == "rollback":
        payload = rollback_corpus_promotion(
            args.project_root,
            target=args.target,
            note=args.note,
        )
        print(json.dumps(payload, indent=2))
        return

    if args.group == "evaluation" and args.action == "run":
        scorecard, outputs = run_corpus_evaluation(
            args.root,
            seed=args.seed,
            markdown_output=args.markdown_output,
            json_output=args.json_output,
            project_root=args.project_root,
            corpus_store_root=args.corpus_store_root,
        )
        payload = {
            "root": str(args.root.resolve()),
            "outputs": {key: str(value) for key, value in outputs.items()},
            "scorecard": scorecard,
        }
        if args.json:
            print(json.dumps(payload, indent=2))
            return
        print(json.dumps(payload["outputs"], indent=2))
        return

    if args.group == "review" and args.action == "queue":
        result = load_federated_review_queue(args.project_root)
        if args.json:
            print(json.dumps(result, indent=2))
            return
        items = [item for item in result.get("items", []) if item.get("status") == "open"]
        print(f"Open review items: {len(items)}")
        for item in items[: max(args.limit, 0)]:
            print(f"- {item['review_id']} [{item['review_type']}] score={item.get('score')}")
        if len(items) > max(args.limit, 0):
            print(f"... {len(items) - max(args.limit, 0)} more")
        return

    if args.group == "review" and args.action == "history":
        result = load_federated_review_history(args.project_root)
        if args.json:
            print(json.dumps(result, indent=2))
            return
        print(f"Resolved review items: {len(result.get('items', []))}")
        for item in result.get("items", [])[: max(args.limit, 0)]:
            print(f"- {item['review_id']} [{item['decision']}] {item.get('recorded_at')}")
        if len(result.get("items", [])) > max(args.limit, 0):
            print(f"... {len(result.get('items', [])) - max(args.limit, 0)} more")
        return

    if args.group == "review" and args.action == "resolve":
        result = resolve_federated_review_item(
            args.project_root,
            args.review_id,
            args.decision,
            args.note,
            canonical_subject=args.canonical_subject,
        )
        print(json.dumps(result, indent=2))
        return

    if args.group == "review" and args.action == "triage":
        plan = build_triage_plan(args.project_root)
        if args.execute:
            result = execute_triage_plan(args.project_root, plan)
            payload = {**plan, "execution": result}
        else:
            payload = plan
        if args.json:
            print(json.dumps(payload, indent=2))
            return
        s = payload["summary"]
        print(f"Review queue triage: {payload['total_open']} open items")
        print(f"  Auto-resolvable: {payload['auto_resolvable']}")
        print(f"    Accept: {s['accepted']}  Reject: {s['rejected']}  Defer: {s['deferred']}")
        print(f"  Requires manual: {s['manual']}")
        for policy, count in sorted(payload.get("policy_counts", {}).items()):
            print(f"  Policy {policy}: {count}")
        if args.execute:
            ex = payload.get("execution", {})
            print(
                f"\nExecuted: {ex.get('resolved', 0)} resolved, {ex.get('remaining_open', '?')} remaining"
            )
            if ex.get("errors"):
                for err in ex["errors"][:5]:
                    print(f"  Error: {err}")
        elif payload["auto_resolvable"] > 0:
            print("\nRun with --execute to apply.")
        return

    if args.group == "review" and args.action == "assist":
        payload = build_entity_alias_review_assist(
            args.project_root,
            batch_size=args.batch_size,
            relation_filters=args.relations,
            source_pair=args.source_pair,
            anchor_contains=args.anchor_contains,
        )
        if args.batch_id:
            try:
                payload = select_entity_alias_review_assist_batch(payload, args.batch_id)
            except ValueError as exc:
                parser.error(str(exc))
        payload = filter_entity_alias_review_assist_groups(
            payload,
            review_bucket_filters=args.buckets,
        )
        if args.sample_groups:
            payload = sample_entity_alias_review_assist_groups(
                payload,
                sample_groups=args.sample_groups,
                sample_batches=args.sample_batches,
                batch_offset=args.batch_offset,
            )
        artifacts = None
        if args.write:
            if args.sample_groups:
                artifacts = write_entity_alias_review_sample_artifacts(args.project_root, payload)
            else:
                artifacts = write_entity_alias_review_assist_artifacts(args.project_root, payload)
        if args.json:
            print(json.dumps({**payload, "artifacts": artifacts}, indent=2))
            return
        if args.sample_groups:
            print(render_entity_alias_review_sample(payload))
        else:
            print(render_entity_alias_review_assist(payload, group_limit=args.group_limit))
        if artifacts:
            print("")
            print("Artifacts:")
            for key, path in artifacts.items():
                print(f"  {key.removesuffix('_path')}: {path}")
        return

    if args.group == "review" and args.action == "campaign":
        try:
            payload = build_entity_alias_review_campaign(
                args.project_root,
                batch_size=args.batch_size,
                scenario_labels=args.scenarios,
            )
        except ValueError as exc:
            parser.error(str(exc))
        artifacts = (
            write_entity_alias_review_campaign_artifacts(args.project_root, payload)
            if args.write
            else None
        )
        if args.json:
            print(json.dumps({**payload, "artifacts": artifacts}, indent=2))
            return
        print(render_entity_alias_review_campaign(payload))
        if artifacts:
            print("")
            print("Artifacts:")
            for key, value in artifacts.items():
                if key == "scenario_artifacts":
                    for label, scenario_artifacts in value.items():
                        print(f"  scenario {label}:")
                        for artifact_group, paths in scenario_artifacts.items():
                            for path_key, path in paths.items():
                                print(
                                    f"    {artifact_group}.{path_key.removesuffix('_path')}: {path}"
                                )
                    continue
                print(f"  {key.removesuffix('_path')}: {value}")
        return

    if args.group == "review" and args.action == "campaign-index":
        payload = build_entity_alias_review_campaign_index(args.project_root)
        artifacts = (
            write_entity_alias_review_campaign_index_artifacts(args.project_root, payload)
            if args.write
            else None
        )
        if args.json:
            print(json.dumps({**payload, "artifacts": artifacts}, indent=2))
            return
        print(render_entity_alias_review_campaign_index(payload))
        if artifacts:
            print("")
            print("Artifacts:")
            for key, path in artifacts.items():
                print(f"  {key.removesuffix('_path')}: {path}")
        return

    if args.group == "review" and args.action == "packet-hydrate":
        payload = hydrate_entity_alias_review_sample_packet(args.path)
        artifacts = (
            write_entity_alias_review_packet_hydration_artifacts(args.project_root, payload)
            if args.write
            else None
        )
        if args.json:
            print(json.dumps({**payload, "artifacts": artifacts}, indent=2))
            return
        print(render_entity_alias_review_packet_hydration(payload))
        if artifacts:
            print("")
            print("Artifacts:")
            for key, path in artifacts.items():
                print(f"  {key.removesuffix('_path')}: {path}")
        return

    if args.group == "review" and args.action == "campaign-scoreboard":
        payload = build_entity_alias_review_scoreboard(
            args.project_root,
            min_reject_precision=args.min_reject_precision,
            min_adjudicated=args.min_adjudicated,
        )
        artifacts = (
            write_entity_alias_review_scoreboard_artifacts(args.project_root, payload)
            if args.write
            else None
        )
        if args.json:
            print(json.dumps({**payload, "artifacts": artifacts}, indent=2))
            return
        print(render_entity_alias_review_scoreboard(payload))
        if artifacts:
            print("")
            print("Artifacts:")
            for key, path in artifacts.items():
                print(f"  {key.removesuffix('_path')}: {path}")
        return

    if args.group == "review" and args.action == "campaign-rollup":
        payload = build_entity_alias_review_rollup(
            args.project_root,
            packet_statuses=args.statuses,
            scenario_labels=args.scenarios,
            packet_ids=args.packet_ids,
            campaign_ids=args.campaign_ids,
        )
        artifacts = (
            write_entity_alias_review_rollup_artifacts(args.project_root, payload)
            if args.write
            else None
        )
        if args.json:
            print(json.dumps({**payload, "artifacts": artifacts}, indent=2))
            return
        print(render_entity_alias_review_rollup(payload))
        if artifacts:
            print("")
            print("Artifacts:")
            for key, path in artifacts.items():
                print(f"  {key.removesuffix('_path')}: {path}")
        return

    if args.group == "review" and args.action == "reject-stage":
        payload = build_entity_alias_reject_stage(
            args.project_root,
            packet_statuses=args.statuses,
            scenario_labels=args.scenarios,
            packet_ids=args.packet_ids,
            campaign_ids=args.campaign_ids,
            min_reject_precision=args.min_reject_precision,
            min_adjudicated=args.min_adjudicated,
        )
        artifacts = (
            write_entity_alias_reject_stage_artifacts(args.project_root, payload)
            if args.write
            else None
        )
        if args.json:
            print(json.dumps({**payload, "artifacts": artifacts}, indent=2))
            return
        print(render_entity_alias_reject_stage(payload))
        if artifacts:
            print("")
            print("Artifacts:")
            for key, path in artifacts.items():
                print(f"  {key.removesuffix('_path')}: {path}")
        return

    if args.group == "review" and args.action == "apply-plan":
        payload = build_entity_alias_review_apply_plan(
            args.project_root,
            packet_statuses=args.statuses,
            scenario_labels=args.scenarios,
            packet_ids=args.packet_ids,
            campaign_ids=args.campaign_ids,
            min_reject_precision=args.min_reject_precision,
            min_adjudicated=args.min_adjudicated,
        )
        artifacts = (
            write_entity_alias_review_apply_plan_artifacts(args.project_root, payload)
            if args.write
            else None
        )
        if args.json:
            print(json.dumps({**payload, "artifacts": artifacts}, indent=2))
            return
        print(render_entity_alias_review_apply_plan(payload))
        if artifacts:
            print("")
            print("Artifacts:")
            for key, path in artifacts.items():
                print(f"  {key.removesuffix('_path')}: {path}")
        return

    if args.group == "review" and args.action == "sample-summary":
        payload = summarize_entity_alias_review_sample(args.path)
        artifacts = (
            write_entity_alias_review_sample_summary_artifacts(args.project_root, payload)
            if args.write
            else None
        )
        if args.json:
            print(json.dumps({**payload, "artifacts": artifacts}, indent=2))
            return
        print(render_entity_alias_review_sample_summary(payload))
        if artifacts:
            print("")
            print("Artifacts:")
            for key, path in artifacts.items():
                print(f"  {key.removesuffix('_path')}: {path}")
        return

    if args.group == "review" and args.action == "sample-propose":
        payload = propose_entity_alias_review_sample(args.path)
        artifacts = (
            write_entity_alias_review_sample_proposal_artifacts(args.project_root, payload)
            if args.write
            else None
        )
        if args.json:
            print(json.dumps({**payload, "artifacts": artifacts}, indent=2))
            return
        print(render_entity_alias_review_sample_proposal(payload))
        if artifacts:
            print("")
            print("Artifacts:")
            for key, path in artifacts.items():
                print(f"  {key.removesuffix('_path')}: {path}")
        return

    if args.group == "review" and args.action == "sample-compare":
        payload = compare_entity_alias_review_sample_to_proposal(
            args.sample_path,
            args.proposal_path,
        )
        artifacts = (
            write_entity_alias_review_sample_comparison_artifacts(args.project_root, payload)
            if args.write
            else None
        )
        if args.json:
            print(json.dumps({**payload, "artifacts": artifacts}, indent=2))
            return
        print(render_entity_alias_review_sample_comparison(payload))
        if artifacts:
            print("")
            print("Artifacts:")
            for key, path in artifacts.items():
                print(f"  {key.removesuffix('_path')}: {path}")
        return

    if args.group == "source" and args.action == "freshness":
        result = compute_source_freshness(args.corpus_root)
        print(json.dumps(result, indent=2))
        return

    if args.group == "dashboard":
        source_drop_root = getattr(args, "source_drop_root", None) or default_source_drop_root(
            args.project_root
        )
        payload = build_dashboard(args.project_root, source_drop_root)
        if args.json:
            print(json.dumps(payload, indent=2))
            return
        print(render_dashboard_text(payload))
        return

    parser.error("unsupported command")


if __name__ == "__main__":
    main()
