from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .answering import load_json, write_json, write_markdown
from .corpus_candidates import (
    corpus_candidate_latest_json_path,
    corpus_live_pointer_path,
    corpus_promotion_latest_json_path,
)
from .corpus_store import CORPUS_STORE_ROOT_ENV
from .federated_canon import load_federated_review_queue
from .federation import (
    federation_report_path,
    load_corpus_surface,
    load_registry,
    registry_path,
)
from .governance_candidates import (
    policy_application_latest_json_path,
    policy_candidate_latest_json_path,
    policy_live_pointer_path,
)
from .governance_policy import load_or_create_promotion_policy, promotion_policy_path
from .governance_replay import policy_replay_latest_json_path
from .paths import REPO_ROOT
from .provider_catalog import (
    PROVIDER_CONFIG,
    default_source_drop_root,
    get_provider_config,
    provider_corpus_targets,
)
from .provider_readiness import build_provider_readiness
from .provider_refresh import provider_refresh_latest_json_path
from .schema_validation import list_schemas, validate_payload
from .source_policy import load_source_policy, source_policy_path

SURFACE_MANIFEST_CONTRACT = "conversation-corpus-engine-surface-manifest-v1"
MCP_CONTEXT_CONTRACT = "conversation-corpus-engine-mcp-context-v1"
SURFACE_BUNDLE_CONTRACT = "conversation-corpus-engine-surface-bundle-v1"
SURFACE_CONTRACT_VERSION = 1
CORPUS_STORE_ROOT_PLACEHOLDER = f"${{{CORPUS_STORE_ROOT_ENV}}}"
HISTORICAL_CORPUS_ROOT_PLACEHOLDER = "<historical-corpus-root>"

_CORPUS_EVENT_ROOT_FIELDS = {
    "candidate_root",
    "live_root",
    "output_root",
    "previous_root",
    "promoted_root",
    "restored_root",
    "target_root",
}
_REFRESH_RECEIPT_FIELDS = {
    "refresh_json_path",
    "refresh_markdown_path",
}

CLI_SURFACES = [
    {
        "command": "cce schema validate corpus-contract --path /path/to/corpus/contract.json",
        "purpose": "Validate canonical artifacts before promotion or export.",
        "audience": "engine",
    },
    {
        "command": "cce provider refresh --provider perplexity --project-root /path/to/project --source-drop-root /path/to/source-drop",
        "purpose": "Import, evaluate, and stage provider refresh candidates.",
        "audience": "engine",
    },
    {
        "command": "cce candidate promote --project-root /path/to/project --candidate-id latest",
        "purpose": "Promote reviewed corpus candidates into the live registry.",
        "audience": "engine",
    },
    {
        "command": "cce policy apply --project-root /path/to/project --candidate-id latest",
        "purpose": "Apply reviewed promotion-policy candidates.",
        "audience": "engine",
    },
    {
        "command": "cce surface bundle --project-root /path/to/project --source-drop-root /path/to/source-drop",
        "purpose": "Export Meta/MCP-facing manifests from the governed project state.",
        "audience": "mcp",
    },
]

COMMERCIAL_SPEC_REFERENCES = [
    {
        "id": "cce-commercial-architecture-design",
        "title": "CCE Commercial Architecture - Design Specification",
        "relative_path": "docs/superpowers/specs/2026-03-31-cce-commercial-architecture-design.md",
        "sections": [
            "Income Surface",
            "Concentric Rings",
            "Application Pipeline Symbiosis",
            "Gate Contract",
        ],
    },
    {
        "id": "cce-commercial-architecture-expansion",
        "title": "CCE Commercial Architecture - Expansion",
        "relative_path": "docs/superpowers/specs/2026-03-31-cce-commercial-architecture-expansion.md",
        "sections": [
            "Knowledge Intelligence Stack",
            "Five Compounding Loops",
            "ORGAN-III Delivery Vehicles",
            "Loop Activation by Horizon",
        ],
    },
]

COMMERCIAL_RINGS = [
    {
        "ring": 0,
        "name": "Engine",
        "surface": "CLI engine and open-source package",
        "revenue_model": "free-open-source",
        "horizon": "now",
    },
    {
        "ring": 1,
        "name": "SaaS web app",
        "surface": "Search and recall across providers",
        "revenue_model": "professional-subscription",
        "horizon": "H3",
    },
    {
        "ring": 2,
        "name": "Platform API and MCP server",
        "surface": "Schema contracts, API access, and MCP context",
        "revenue_model": "builder-usage",
        "horizon": "H1-H3",
    },
    {
        "ring": 3,
        "name": "Cross-module premium",
        "surface": "Narratological lenses and knowledge intelligence stack",
        "revenue_model": "premium-subscription",
        "horizon": "H4",
    },
    {
        "ring": 4,
        "name": "Enterprise services",
        "surface": "Governed corpus deployment and custom adapters",
        "revenue_model": "enterprise-services",
        "horizon": "H4",
    },
]

PIPELINE_BRIDGES = [
    {
        "pipeline_label": "Pillar 1",
        "pipeline_surface": "jobs",
        "income_band": "I",
        "horizon": "H1-H3",
        "cce_surface": "domain expertise",
        "cce_commercial_effect": "Builds market evidence for CCE enterprise pain.",
    },
    {
        "pipeline_label": "Pillar 2",
        "pipeline_surface": "grants",
        "income_band": "II",
        "horizon": "H1-H2",
        "cce_surface": "research credibility",
        "cce_commercial_effect": "Validates CCE as a research-grade corpus system.",
    },
    {
        "pipeline_label": "Pillar 3",
        "pipeline_surface": "consulting",
        "income_band": "III",
        "horizon": "H3",
        "cce_surface": "CCE Ring 4 enterprise services",
        "cce_commercial_effect": "Turns pipeline engagements into governed CCE deployments.",
    },
    {
        "pipeline_label": "Identity #9",
        "pipeline_surface": "founder-operator",
        "income_band": "III-IV",
        "horizon": "H3-H5",
        "cce_surface": "commercial persona",
        "cce_commercial_effect": "Frames the operator as the buyer-facing CCE founder.",
    },
    {
        "pipeline_label": "Identity #5",
        "pipeline_surface": "independent-engineer",
        "income_band": "I-III",
        "horizon": "H1-H4",
        "cce_surface": "engineering credibility",
        "cce_commercial_effect": "Makes implementation proof legible to employers and clients.",
    },
    {
        "pipeline_label": "SGO research",
        "pipeline_surface": "research-publication",
        "income_band": "II-IV",
        "horizon": "H2-H5",
        "cce_surface": "marketing and omega research",
        "cce_commercial_effect": "Turns conversation federation research into public proof.",
    },
]

REVENUE_EVOLUTION = [
    {
        "horizon": "H1-H2",
        "pipeline_role": "earn now through labor and awards",
        "cce_role": "package the engine and prove adoption",
        "commercial_band": "I-II",
    },
    {
        "horizon": "H3",
        "pipeline_role": "bridge consulting demand into CCE services",
        "cce_role": "launch recurring search/API surfaces",
        "commercial_band": "III-IV",
    },
    {
        "horizon": "H4",
        "pipeline_role": "source enterprise and research relationships",
        "cce_role": "sell governed deployments and premium knowledge loops",
        "commercial_band": "III-IV",
    },
    {
        "horizon": "H5",
        "pipeline_role": "make jobs optional",
        "cce_role": "operate as the studio revenue engine",
        "commercial_band": "IV-V",
    },
]

COMMERCIAL_GATES = [
    {
        "id": "REP-001",
        "check": "At least one mechanism emits INTERFACE_CONTRACT.",
        "evidence": "ORGAN-II surface exists and passes validation.",
    },
    {
        "id": "REP-002",
        "check": "Payment metabolism can process TRANSACTION signals.",
        "evidence": "Stripe or equivalent billing integration is functional.",
    },
    {
        "id": "REP-003",
        "check": "At least one paying customer exists.",
        "evidence": "Revenue status is live in the commercial registry.",
    },
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def surface_reports_dir(project_root: Path) -> Path:
    return project_root.resolve() / "reports" / "surfaces"


def surface_manifest_json_path(project_root: Path) -> Path:
    return surface_reports_dir(project_root) / "surface-manifest.json"


def surface_manifest_markdown_path(project_root: Path) -> Path:
    return surface_reports_dir(project_root) / "surface-manifest.md"


def mcp_context_json_path(project_root: Path) -> Path:
    return surface_reports_dir(project_root) / "mcp-context.json"


def mcp_context_markdown_path(project_root: Path) -> Path:
    return surface_reports_dir(project_root) / "mcp-context.md"


def surface_bundle_json_path(project_root: Path) -> Path:
    return surface_reports_dir(project_root) / "surface-bundle.json"


def surface_bundle_markdown_path(project_root: Path) -> Path:
    return surface_reports_dir(project_root) / "surface-bundle.md"


def optional_json(path: Path) -> Any:
    return load_json(path, default=None)


def _replace_path_prefix(value: str, path_prefix: str, replacement: str) -> str:
    normalized_prefix = path_prefix.rstrip("/") or "/"
    pattern = re.compile(
        rf"(?<![A-Za-z0-9._~/-]){re.escape(normalized_prefix)}"
        r"(?=$|/|[\s'\"`,:;)\]}])"
    )
    return pattern.sub(lambda _match: replacement, value)


def _redact_path_prefix(payload: Any, path_prefix: str, replacement: str) -> Any:
    if isinstance(payload, str):
        return _replace_path_prefix(payload, path_prefix, replacement)
    if isinstance(payload, list):
        return [_redact_path_prefix(item, path_prefix, replacement) for item in payload]
    if isinstance(payload, dict):
        return {
            key: _redact_path_prefix(value, path_prefix, replacement)
            for key, value in payload.items()
        }
    return payload


def _absolute_path_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        return None
    return str(path.resolve(strict=False))


def _collect_named_paths(
    payload: Any,
    *,
    field_names: set[str],
    receipt_fields: set[str] | None = None,
) -> set[str]:
    paths: set[str] = set()
    if isinstance(payload, list):
        for item in payload:
            paths.update(
                _collect_named_paths(
                    item,
                    field_names=field_names,
                    receipt_fields=receipt_fields,
                )
            )
        return paths
    if not isinstance(payload, dict):
        return paths
    for key, value in payload.items():
        if key in field_names:
            resolved = _absolute_path_string(value)
            if resolved is not None:
                paths.add(resolved)
        if receipt_fields and key in receipt_fields:
            resolved = _absolute_path_string(value)
            if resolved is not None:
                paths.add(str(Path(resolved).parent))
        paths.update(
            _collect_named_paths(
                value,
                field_names=field_names,
                receipt_fields=receipt_fields,
            )
        )
    return paths


def _classified_corpus_roots(payload: Any) -> set[str]:
    if not isinstance(payload, dict):
        return set()
    roots: set[str] = set()

    registry = payload.get("registry")
    if isinstance(registry, dict):
        for entry in registry.get("corpora") or []:
            if isinstance(entry, dict):
                resolved = _absolute_path_string(entry.get("root"))
                if resolved is not None:
                    roots.add(resolved)

    for provider in payload.get("providers") or []:
        if not isinstance(provider, dict):
            continue
        for field in ("selected_target",):
            target = provider.get(field)
            if isinstance(target, dict):
                resolved = _absolute_path_string(target.get("root"))
                if resolved is not None:
                    roots.add(resolved)
        for field in ("selected_targets", "targets"):
            for target in provider.get(field) or []:
                if isinstance(target, dict):
                    resolved = _absolute_path_string(target.get("root"))
                    if resolved is not None:
                        roots.add(resolved)
        policy = provider.get("source_policy")
        if isinstance(policy, dict):
            for field in ("primary_root", "fallback_root"):
                resolved = _absolute_path_string(policy.get(field))
                if resolved is not None:
                    roots.add(resolved)

    governance = payload.get("governance")
    if isinstance(governance, dict):
        for field in ("latest_corpus_candidate", "latest_corpus_promotion"):
            roots.update(
                _collect_named_paths(
                    governance.get(field),
                    field_names=_CORPUS_EVENT_ROOT_FIELDS,
                )
            )

    latest_events = payload.get("latest_events")
    if isinstance(latest_events, dict):
        roots.update(
            _collect_named_paths(
                latest_events.get("latest_corpus_live_pointer"),
                field_names=_CORPUS_EVENT_ROOT_FIELDS,
            )
        )
        roots.update(
            _collect_named_paths(
                latest_events.get("latest_provider_refreshes"),
                field_names=_CORPUS_EVENT_ROOT_FIELDS,
                receipt_fields=_REFRESH_RECEIPT_FIELDS,
            )
        )
    return roots


def redact_corpus_store_paths(payload: Any, registry: dict[str, Any]) -> Any:
    registration = registry.get("corpus_store")
    active_store_root = (
        str(Path(registration["root"]).resolve())
        if (
            isinstance(registration, dict)
            and registration.get("status") == "active"
            and isinstance(registration.get("root"), str)
        )
        else None
    )
    redacted = payload
    if active_store_root is not None:
        redacted = _redact_path_prefix(
            redacted,
            active_store_root,
            CORPUS_STORE_ROOT_PLACEHOLDER,
        )

    classified_roots = _classified_corpus_roots(payload)
    historical_roots = {
        root
        for root in classified_roots
        if not (
            active_store_root
            and (
                Path(root) == Path(active_store_root)
                or Path(root).is_relative_to(Path(active_store_root))
            )
        )
    }
    for historical_root in sorted(historical_roots, key=len):
        redacted = _redact_path_prefix(
            redacted,
            historical_root,
            HISTORICAL_CORPUS_ROOT_PLACEHOLDER,
        )
    return redacted


def build_commercial_awareness_payload() -> dict[str, Any]:
    source_specs = []
    for spec in COMMERCIAL_SPEC_REFERENCES:
        relative_path = spec["relative_path"]
        source_specs.append(
            {
                "id": spec["id"],
                "title": spec["title"],
                "relative_path": relative_path,
                "path": str((REPO_ROOT / relative_path).resolve()),
                "sections": list(spec["sections"]),
            },
        )
    return {
        "contract_name": "conversation-corpus-engine-commercial-awareness-v1",
        "contract_version": 1,
        "source_specs": source_specs,
        "relationship": {
            "cce_repo": "organvm-i-theoria/conversation-corpus-engine",
            "pipeline_repo": "4444J99/application-pipeline",
            "relationship_type": "symbiotic-income-surface",
            "same_income_equation": True,
            "summary": (
                "Pipeline labor, awards, consulting, and founder identity earn now while "
                "CCE matures into reusable product, API, and enterprise revenue."
            ),
        },
        "commercial_position": {
            "organ": "ORGAN-I",
            "mechanism": "mneme--remember",
            "system_role": "knowledge intelligence engine",
            "revenue_model": "open-source engine with SaaS, API, premium, and enterprise rings",
            "revenue_status": "architecture-defined",
        },
        "consumer_contract": {
            "intended_consumers": [
                "application-pipeline",
                "conversation-corpus--surfaces",
                "conversation-corpus--product",
                "commerce--meta",
                "organvm-mcp-server",
            ],
            "pipeline_consumption": (
                "Use pipeline_bridges and revenue_evolution to align jobs, grants, "
                "consulting, founder positioning, and research output with CCE maturity."
            ),
            "surface_signal": "STATE_MODEL + VALIDATION_RECORD",
            "interface_signal": "INTERFACE_CONTRACT",
            "flow_constraint": (
                "Provider exports -> CCE -> ORGAN-II surfaces -> ORGAN-III product. "
                "The commercial path must not skip the ORGAN-II interface bridge."
            ),
        },
        "signal_chain": [
            {
                "stage": "provider_exports",
                "organ": "external",
                "emits": "ARCHIVE_PACKET",
                "consumed_by": "conversation-corpus-engine",
            },
            {
                "stage": "conversation-corpus-engine",
                "organ": "ORGAN-I",
                "emits": "ANNOTATED_CORPUS, VALIDATION_RECORD, STATE_MODEL",
                "consumed_by": "conversation-corpus--surfaces",
            },
            {
                "stage": "conversation-corpus--surfaces",
                "organ": "ORGAN-II",
                "emits": "INTERFACE_CONTRACT",
                "consumed_by": "conversation-corpus--product",
            },
            {
                "stage": "conversation-corpus--product",
                "organ": "ORGAN-III",
                "emits": "billing STATE_MODEL",
                "consumed_by": "customers and commercial governance",
            },
        ],
        "commercial_rings": [dict(item) for item in COMMERCIAL_RINGS],
        "pipeline_bridges": [dict(item) for item in PIPELINE_BRIDGES],
        "revenue_evolution": [dict(item) for item in REVENUE_EVOLUTION],
        "activation_gates": [dict(item) for item in COMMERCIAL_GATES],
    }


def registry_snapshot(project_root: Path) -> dict[str, Any]:
    registry = load_registry(project_root)
    entries = registry.get("corpora", [])
    surfaces = [load_corpus_surface(entry) for entry in entries]
    summaries = [surface["summary"] for surface in surfaces]
    active_summaries = [item for item in summaries if item.get("status", "active") == "active"]
    default_entry = next(
        (
            entry
            for entry in entries
            if entry.get("status", "active") == "active" and entry.get("default")
        ),
        None,
    )
    return {
        "registry": registry,
        "summaries": summaries,
        "active_summaries": active_summaries,
        "default_corpus_id": default_entry.get("corpus_id") if default_entry else None,
    }


def provider_manifest_rows(
    project_root: Path,
    source_drop_root: Path,
    *,
    readiness_payload: dict[str, Any],
    registry_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    readiness_by_provider = {
        item["provider"]: item for item in readiness_payload.get("providers") or []
    }
    rows: list[dict[str, Any]] = []
    for provider in sorted(PROVIDER_CONFIG):
        config = get_provider_config(provider)
        readiness_item = readiness_by_provider.get(provider) or {}
        targets = provider_corpus_targets(
            project_root,
            provider,
            source_drop_root,
            registry=registry_entries,
        )
        rows.append(
            {
                "provider": provider,
                "display_name": config["display_name"],
                "adapter_type": config["adapter_type"],
                "discovery_mode": config["discovery_mode"],
                "calibration_only": bool(config.get("calibration_only", False)),
                "selected_targets": [
                    {
                        "role": target["role"],
                        "corpus_id": target["corpus_id"],
                        "root": target["root"],
                        "selected": bool(target.get("selected")),
                    }
                    for target in targets
                ],
                "source_policy": load_source_policy(project_root, provider) or None,
                "readiness_status": readiness_item.get("overall_state", "unknown"),
                "next_command": readiness_item.get("next_command", "ready"),
                "notes": config.get("notes", []),
            },
        )
    return rows


def mcp_provider_rows(readiness_payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in readiness_payload.get("providers") or []:
        rows.append(
            {
                "provider": item["provider"],
                "display_name": item["display_name"],
                "overall_state": item["overall_state"],
                "next_command": item["next_command"],
                "selected_target": item["selected_target"],
                "targets": item["targets"],
                "policy": item.get("policy"),
                "discovery": item["discovery"],
                "notes": item.get("notes", []),
            },
        )
    return rows


def latest_provider_refreshes(project_root: Path) -> dict[str, Any]:
    return {
        provider: optional_json(provider_refresh_latest_json_path(project_root, provider))
        for provider in sorted(PROVIDER_CONFIG)
    }


def render_surface_manifest(payload: dict[str, Any]) -> str:
    registry = payload.get("registry") or {}
    commercial = payload.get("commercial_awareness") or {}
    relationship = commercial.get("relationship") or {}
    lines = [
        "# Surface Manifest",
        "",
        f"- Generated: {payload.get('generated_at') or 'n/a'}",
        f"- Package: {((payload.get('engine') or {}).get('package')) or 'n/a'}",
        f"- Version: {((payload.get('engine') or {}).get('version')) or 'n/a'}",
        f"- Project root: {((payload.get('project') or {}).get('project_root')) or 'n/a'}",
        f"- Source-drop root: {((payload.get('project') or {}).get('source_drop_root')) or 'n/a'}",
        f"- Registered corpora: {registry.get('corpus_count', 0)}",
        f"- Active corpora: {registry.get('active_corpus_count', 0)}",
        f"- Providers: {len(payload.get('providers') or [])}",
        "",
        "## Registry",
        "",
    ]
    for item in registry.get("corpora") or []:
        lines.append(
            f"- {item['corpus_id']}: status={item.get('status')} gate={item.get('evaluation_overall_state') or 'n/a'} freshness={item.get('source_freshness_state') or 'n/a'}",
        )
    lines.extend(["", "## Providers", ""])
    for item in payload.get("providers") or []:
        lines.append(
            f"- {item['provider']}: readiness={item.get('readiness_status')} next={item.get('next_command')}",
        )
    if commercial:
        lines.extend(
            [
                "",
                "## Commercial Awareness",
                "",
                f"- Relationship: {relationship.get('relationship_type') or 'n/a'}",
                f"- Pipeline repo: {relationship.get('pipeline_repo') or 'n/a'}",
                f"- Commercial rings: {len(commercial.get('commercial_rings') or [])}",
                f"- Pipeline bridges: {len(commercial.get('pipeline_bridges') or [])}",
            ],
        )
    return "\n".join(lines)


def render_mcp_context(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    commercial = payload.get("commercial_awareness") or {}
    relationship = commercial.get("relationship") or {}
    lines = [
        "# MCP Context",
        "",
        f"- Generated: {payload.get('generated_at') or 'n/a'}",
        f"- Active corpora: {summary.get('active_corpus_count', 0)}",
        f"- Providers: {summary.get('provider_count', 0)}",
        f"- Healthy providers: {summary.get('healthy_provider_count', 0)}",
        f"- Refresh recommended: {summary.get('refresh_recommended_count', 0)}",
        f"- Open review items: {summary.get('open_review_count', 0)}",
        "",
        "## Provider States",
        "",
    ]
    for item in payload.get("providers") or []:
        lines.append(
            f"- {item['provider']}: {item.get('overall_state')} next={item.get('next_command')}",
        )
    if commercial:
        lines.extend(
            [
                "",
                "## Commercial Awareness",
                "",
                f"- CCE repo: {relationship.get('cce_repo') or 'n/a'}",
                f"- Pipeline repo: {relationship.get('pipeline_repo') or 'n/a'}",
                f"- Same income equation: {'yes' if relationship.get('same_income_equation') else 'no'}",
            ],
        )
    return "\n".join(lines)


def render_surface_bundle(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    lines = [
        "# Surface Bundle",
        "",
        f"- Generated: {payload.get('generated_at') or 'n/a'}",
        f"- Overall valid: {'yes' if summary.get('valid') else 'no'}",
        f"- Error count: {summary.get('error_count', 0)}",
        f"- Manifest path: {((payload.get('manifest') or {}).get('path')) or 'n/a'}",
        f"- Context path: {((payload.get('context') or {}).get('path')) or 'n/a'}",
        "",
        "## Validation",
        "",
        f"- Surface manifest: {'pass' if (payload.get('manifest') or {}).get('valid') else 'fail'}",
        f"- MCP context: {'pass' if (payload.get('context') or {}).get('valid') else 'fail'}",
        f"- Bundle envelope: {'pass' if (payload.get('bundle_validation') or {}).get('valid') else 'fail'}",
    ]
    return "\n".join(lines)


def build_surface_manifest(
    project_root: Path,
    *,
    source_drop_root: Path | None = None,
) -> dict[str, Any]:
    resolved_project_root = project_root.resolve()
    resolved_source_drop_root = (
        source_drop_root or default_source_drop_root(resolved_project_root)
    ).resolve()
    snapshot = registry_snapshot(resolved_project_root)
    readiness_payload = build_provider_readiness(
        resolved_project_root,
        resolved_source_drop_root,
    )
    registry = snapshot["registry"]
    payload = {
        "contract_name": SURFACE_MANIFEST_CONTRACT,
        "contract_version": SURFACE_CONTRACT_VERSION,
        "generated_at": now_iso(),
        "engine": {
            "package": "conversation-corpus-engine",
            "version": __version__,
            "repo_root": str(REPO_ROOT.resolve()),
        },
        "project": {
            "project_root": str(resolved_project_root),
            "source_drop_root": str(resolved_source_drop_root),
            "organ": "ORGAN-I",
            "system_role": "conversation-corpus-engine",
        },
        "schemas": list_schemas(),
        "cli_surfaces": CLI_SURFACES,
        "registry": {
            "registry_version": registry.get("registry_version", 1),
            "corpus_count": len(registry.get("corpora") or []),
            "active_corpus_count": len(snapshot["active_summaries"]),
            "default_corpus_id": snapshot["default_corpus_id"],
            "corpora": snapshot["summaries"],
        },
        "providers": provider_manifest_rows(
            resolved_project_root,
            resolved_source_drop_root,
            readiness_payload=readiness_payload,
            registry_entries=registry.get("corpora") or [],
        ),
        "artifacts": {
            "registry_path": str(registry_path(resolved_project_root)),
            "promotion_policy_path": str(promotion_policy_path(resolved_project_root)),
            "federation_summary_path": str(federation_report_path(resolved_project_root)),
            "policy_replay_latest_json_path": str(
                policy_replay_latest_json_path(resolved_project_root)
            ),
            "policy_candidate_latest_json_path": str(
                policy_candidate_latest_json_path(resolved_project_root)
            ),
            "policy_application_latest_json_path": str(
                policy_application_latest_json_path(resolved_project_root)
            ),
            "corpus_candidate_latest_json_path": str(
                corpus_candidate_latest_json_path(resolved_project_root)
            ),
            "corpus_promotion_latest_json_path": str(
                corpus_promotion_latest_json_path(resolved_project_root)
            ),
            "corpus_live_pointer_path": str(corpus_live_pointer_path(resolved_project_root)),
            "source_policy_paths": {
                provider: str(source_policy_path(resolved_project_root, provider))
                for provider in sorted(PROVIDER_CONFIG)
            },
            "provider_refresh_latest_json_paths": {
                provider: str(provider_refresh_latest_json_path(resolved_project_root, provider))
                for provider in sorted(PROVIDER_CONFIG)
            },
        },
        "commercial_awareness": build_commercial_awareness_payload(),
    }
    return redact_corpus_store_paths(payload, registry)


def build_mcp_context_payload(
    project_root: Path,
    *,
    source_drop_root: Path | None = None,
) -> dict[str, Any]:
    resolved_project_root = project_root.resolve()
    resolved_source_drop_root = (
        source_drop_root or default_source_drop_root(resolved_project_root)
    ).resolve()
    snapshot = registry_snapshot(resolved_project_root)
    readiness_payload = build_provider_readiness(
        resolved_project_root,
        resolved_source_drop_root,
    )
    open_review_items = [
        item
        for item in (load_federated_review_queue(resolved_project_root).get("items") or [])
        if item.get("status") == "open"
    ]
    providers = mcp_provider_rows(readiness_payload)
    healthy_provider_count = sum(
        1 for item in providers if item.get("overall_state") == "healthy-federation"
    )
    refresh_recommended_count = sum(
        1 for item in providers if "cce provider refresh" in (item.get("next_command") or "")
    )
    payload = {
        "contract_name": MCP_CONTEXT_CONTRACT,
        "contract_version": SURFACE_CONTRACT_VERSION,
        "generated_at": now_iso(),
        "project_root": str(resolved_project_root),
        "source_drop_root": str(resolved_source_drop_root),
        "summary": {
            "registered_corpus_count": len(snapshot["registry"].get("corpora") or []),
            "active_corpus_count": len(snapshot["active_summaries"]),
            "provider_count": len(providers),
            "healthy_provider_count": healthy_provider_count,
            "refresh_recommended_count": refresh_recommended_count,
            "open_review_count": len(open_review_items),
        },
        "registry": {
            "default_corpus_id": snapshot["default_corpus_id"],
            "corpora": snapshot["summaries"],
        },
        "providers": providers,
        "governance": {
            "promotion_policy": load_or_create_promotion_policy(resolved_project_root),
            "latest_policy_replay": optional_json(
                policy_replay_latest_json_path(resolved_project_root)
            ),
            "latest_policy_candidate": optional_json(
                policy_candidate_latest_json_path(resolved_project_root)
            ),
            "latest_policy_application": optional_json(
                policy_application_latest_json_path(resolved_project_root)
            ),
            "latest_corpus_candidate": optional_json(
                corpus_candidate_latest_json_path(resolved_project_root)
            ),
            "latest_corpus_promotion": optional_json(
                corpus_promotion_latest_json_path(resolved_project_root)
            ),
        },
        "latest_events": {
            "latest_corpus_live_pointer": optional_json(
                corpus_live_pointer_path(resolved_project_root)
            ),
            "latest_policy_live_pointer": optional_json(
                policy_live_pointer_path(resolved_project_root)
            ),
            "latest_provider_refreshes": latest_provider_refreshes(resolved_project_root),
        },
        "review_queue": {
            "open_count": len(open_review_items),
            "items": open_review_items[:25],
        },
        "schema_catalog": list_schemas(),
        "commercial_awareness": build_commercial_awareness_payload(),
    }
    return redact_corpus_store_paths(payload, snapshot["registry"])


def write_surface_manifest_artifacts(
    project_root: Path,
    payload: dict[str, Any],
) -> dict[str, str]:
    json_path = surface_manifest_json_path(project_root)
    markdown_path = surface_manifest_markdown_path(project_root)
    write_json(json_path, payload)
    write_markdown(markdown_path, render_surface_manifest(payload))
    return {
        "json_path": str(json_path),
        "markdown_path": str(markdown_path),
    }


def write_mcp_context_artifacts(
    project_root: Path,
    payload: dict[str, Any],
) -> dict[str, str]:
    json_path = mcp_context_json_path(project_root)
    markdown_path = mcp_context_markdown_path(project_root)
    write_json(json_path, payload)
    write_markdown(markdown_path, render_mcp_context(payload))
    return {
        "json_path": str(json_path),
        "markdown_path": str(markdown_path),
    }


def export_surface_bundle(
    project_root: Path,
    *,
    source_drop_root: Path | None = None,
) -> dict[str, Any]:
    resolved_project_root = project_root.resolve()
    resolved_source_drop_root = (
        source_drop_root or default_source_drop_root(resolved_project_root)
    ).resolve()
    manifest_payload = build_surface_manifest(
        resolved_project_root,
        source_drop_root=resolved_source_drop_root,
    )
    context_payload = build_mcp_context_payload(
        resolved_project_root,
        source_drop_root=resolved_source_drop_root,
    )
    manifest_paths = write_surface_manifest_artifacts(resolved_project_root, manifest_payload)
    context_paths = write_mcp_context_artifacts(resolved_project_root, context_payload)
    manifest_validation = validate_payload("surface-manifest", manifest_payload)
    context_validation = validate_payload("mcp-context", context_payload)
    bundle = {
        "contract_name": SURFACE_BUNDLE_CONTRACT,
        "contract_version": SURFACE_CONTRACT_VERSION,
        "generated_at": now_iso(),
        "project_root": str(resolved_project_root),
        "source_drop_root": str(resolved_source_drop_root),
        "summary": {
            "valid": manifest_validation["valid"] and context_validation["valid"],
            "error_count": manifest_validation["error_count"] + context_validation["error_count"],
        },
        "manifest": {
            "schema_name": "surface-manifest",
            "path": manifest_paths["json_path"],
            "markdown_path": manifest_paths["markdown_path"],
            "valid": manifest_validation["valid"],
            "error_count": manifest_validation["error_count"],
            "errors": manifest_validation["errors"],
        },
        "context": {
            "schema_name": "mcp-context",
            "path": context_paths["json_path"],
            "markdown_path": context_paths["markdown_path"],
            "valid": context_validation["valid"],
            "error_count": context_validation["error_count"],
            "errors": context_validation["errors"],
        },
    }
    bundle["bundle_validation"] = {
        "schema": "surface-bundle",
        "schema_path": "",
        "valid": False,
        "error_count": 0,
        "errors": [],
    }
    bundle_validation = validate_payload("surface-bundle", bundle)
    bundle["bundle_validation"] = bundle_validation
    write_json(surface_bundle_json_path(resolved_project_root), bundle)
    write_markdown(
        surface_bundle_markdown_path(resolved_project_root), render_surface_bundle(bundle)
    )
    return bundle
