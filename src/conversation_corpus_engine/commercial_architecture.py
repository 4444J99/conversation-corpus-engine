from __future__ import annotations

import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .answering import write_json, write_markdown
from .paths import REPO_ROOT
from .provider_catalog import default_source_drop_root
from .surface_exports import (
    mcp_context_json_path,
    surface_bundle_json_path,
    surface_manifest_json_path,
)

COMMERCIAL_H1_CONTRACT = "conversation-corpus-engine-commercial-h1-readiness-v1"
COMMERCIAL_H1_CONTRACT_VERSION = 1


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def commercial_reports_dir(project_root: Path) -> Path:
    return project_root.resolve() / "reports" / "commercial"


def commercial_h1_json_path(project_root: Path) -> Path:
    return commercial_reports_dir(project_root) / "commercial-h1-readiness.json"


def commercial_h1_markdown_path(project_root: Path) -> Path:
    return commercial_reports_dir(project_root) / "commercial-h1-readiness.md"


def load_pyproject_metadata() -> dict[str, Any]:
    pyproject_path = REPO_ROOT / "pyproject.toml"
    payload = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    project = payload.get("project") or {}
    return {
        "path": str(pyproject_path),
        "name": project.get("name"),
        "version": project.get("version"),
        "description": project.get("description"),
        "readme": project.get("readme"),
        "requires_python": project.get("requires-python"),
        "dependencies": project.get("dependencies") or [],
        "optional_dependencies": project.get("optional-dependencies") or {},
        "scripts": project.get("scripts") or {},
        "urls": project.get("urls") or {},
    }


def package_readiness(metadata: dict[str, Any]) -> dict[str, Any]:
    scripts = metadata.get("scripts") or {}
    optional_dependencies = metadata.get("optional_dependencies") or {}
    checks = {
        "has_name": bool(metadata.get("name")),
        "has_version": metadata.get("version") == __version__,
        "has_description": bool(metadata.get("description")),
        "has_readme": bool(metadata.get("readme")),
        "has_repository_url": bool((metadata.get("urls") or {}).get("Repository")),
        "has_cli_script": scripts.get("cce") == "conversation_corpus_engine.cli:main",
        "has_mcp_script": scripts.get("cce-mcp") == "conversation_corpus_engine.mcp_server:main",
        "has_mcp_extra": "mcp" in optional_dependencies,
    }
    return {
        "checks": checks,
        "pypi_ready": all(
            checks[key]
            for key in (
                "has_name",
                "has_version",
                "has_description",
                "has_readme",
                "has_repository_url",
                "has_cli_script",
            )
        ),
        "mcp_distribution_ready": all(
            checks[key] for key in ("has_cli_script", "has_mcp_script", "has_mcp_extra")
        ),
    }


def build_signal_chain(project_root: Path) -> list[dict[str, str]]:
    return [
        {
            "from": "external",
            "signal": "ARCHIVE_PACKET",
            "to": "conversation-corpus-engine",
            "artifact": "provider export or local-session source",
        },
        {
            "from": "conversation-corpus-engine",
            "signal": "ANNOTATED_CORPUS",
            "to": "conversation-corpus-engine",
            "artifact": str(project_root / "corpus"),
        },
        {
            "from": "conversation-corpus-engine",
            "signal": "VALIDATION_RECORD",
            "to": "conversation-corpus--surfaces",
            "artifact": str(surface_manifest_json_path(project_root)),
        },
        {
            "from": "conversation-corpus-engine",
            "signal": "STATE_MODEL",
            "to": "conversation-corpus--surfaces",
            "artifact": str(mcp_context_json_path(project_root)),
        },
        {
            "from": "conversation-corpus--surfaces",
            "signal": "INTERFACE_CONTRACT",
            "to": "conversation-corpus--product",
            "artifact": "landing page, SDK, MCP packaging, API gateway",
        },
        {
            "from": "conversation-corpus--product",
            "signal": "STATE_MODEL:billing",
            "to": "customer",
            "artifact": "pricing, subscription, invoice, usage state",
        },
    ]


def build_interface_contract(project_root: Path) -> dict[str, Any]:
    return {
        "source_organ": "ORGAN-I",
        "source_repo": "conversation-corpus-engine",
        "target_organ": "ORGAN-II",
        "target_repo": "conversation-corpus--surfaces",
        "formation_type": "FORM",
        "consumes": ["STATE_MODEL", "VALIDATION_RECORD"],
        "expected_output_signal": "INTERFACE_CONTRACT",
        "unidirectional_flow": ["ORGAN-I", "ORGAN-II", "ORGAN-III"],
        "engine_artifacts": {
            "surface_manifest": str(surface_manifest_json_path(project_root)),
            "mcp_context": str(mcp_context_json_path(project_root)),
            "surface_bundle": str(surface_bundle_json_path(project_root)),
        },
        "downstream_contains": [
            "landing page with email capture",
            "Python SDK wrapper",
            "MCP server packaging",
            "API gateway",
            "web app shell",
        ],
    }


def h1_actions(package_status: dict[str, Any]) -> list[dict[str, Any]]:
    pypi_ready = bool(package_status["pypi_ready"])
    mcp_ready = bool(package_status["mcp_distribution_ready"])
    return [
        {
            "id": "h1-pypi-package",
            "title": "Push cce to PyPI with clean README",
            "status": "ready" if pypi_ready else "needs_repo_work",
            "repo_owned": True,
            "evidence": [
                "pyproject includes package metadata, README, repository URL, and cce CLI script."
            ],
            "next_action": "Build and upload the approved distribution from a release environment.",
        },
        {
            "id": "h1-mcp-server",
            "title": "Package MCP server as pipx install cce[mcp]",
            "status": "ready" if mcp_ready else "needs_repo_work",
            "repo_owned": True,
            "evidence": [
                "mcp optional extra is declared.",
                "cce-mcp script exposes the stdio MCP server.",
                "cce mcp serve provides the same server through the main CLI.",
            ],
            "next_action": 'Install with pipx install "conversation-corpus-engine[mcp]".',
        },
        {
            "id": "h1-show-hn",
            "title": 'Post on Hacker News: "Show HN: Search all your AI conversations from one place"',
            "status": "external_action_required",
            "repo_owned": False,
            "evidence": ["Message and product claim are captured in the H1 readiness contract."],
            "next_action": "Post from the operator account after package install smoke test passes.",
        },
        {
            "id": "h1-surfaces-repo",
            "title": "Create conversation-corpus--surfaces repo as ORGAN-II bridge",
            "status": "external_action_required",
            "repo_owned": False,
            "evidence": [
                "CCE emits surface-manifest, mcp-context, and surface-bundle artifacts.",
                "This contract defines the STATE_MODEL + VALIDATION_RECORD inputs.",
            ],
            "next_action": "Create the ORGAN-II repo and consume the exported CCE surface bundle.",
        },
        {
            "id": "h1-landing-page",
            "title": "Create landing page with email capture",
            "status": "external_action_required",
            "repo_owned": False,
            "evidence": ["Landing page belongs in conversation-corpus--surfaces."],
            "next_action": "Publish a one-page Loop 1 memory offer with email capture.",
        },
        {
            "id": "h1-lobehub-paid-tiers",
            "title": "Enable a-i--skills paid tiers on LobeHub",
            "status": "external_action_required",
            "repo_owned": False,
            "evidence": ["Paid-tier switch is outside the CCE repository boundary."],
            "next_action": "Enable tiers in the a-i--skills/LobeHub distribution channel.",
        },
    ]


def build_commercial_h1_readiness(
    project_root: Path,
    *,
    source_drop_root: Path | None = None,
) -> dict[str, Any]:
    resolved_project_root = project_root.resolve()
    resolved_source_drop_root = (
        source_drop_root or default_source_drop_root(resolved_project_root)
    ).resolve()
    package_metadata = load_pyproject_metadata()
    package_status = package_readiness(package_metadata)
    actions = h1_actions(package_status)
    repo_actions = [item for item in actions if item["repo_owned"]]
    external_actions = [item for item in actions if not item["repo_owned"]]
    repo_ready_count = sum(1 for item in repo_actions if item["status"] == "ready")
    return {
        "contract_name": COMMERCIAL_H1_CONTRACT,
        "contract_version": COMMERCIAL_H1_CONTRACT_VERSION,
        "generated_at": now_iso(),
        "project_root": str(resolved_project_root),
        "source_drop_root": str(resolved_source_drop_root),
        "architecture_refs": [
            str(
                REPO_ROOT
                / "docs/superpowers/specs/2026-03-31-cce-commercial-architecture-design.md"
            ),
            str(
                REPO_ROOT
                / "docs/superpowers/specs/2026-03-31-cce-commercial-architecture-expansion.md"
            ),
        ],
        "summary": {
            "horizon": "H1",
            "repo_action_count": len(repo_actions),
            "repo_ready_action_count": repo_ready_count,
            "external_action_count": len(external_actions),
            "commercial_h1_repo_ready": repo_ready_count == len(repo_actions),
            "external_actions_remaining": len(external_actions),
        },
        "package": {
            "metadata": package_metadata,
            "readiness": package_status,
        },
        "mcp_distribution": {
            "extra": "mcp",
            "pipx_install": 'pipx install "conversation-corpus-engine[mcp]"',
            "scripts": ["cce", "cce-mcp"],
            "serve_command": "cce mcp serve --project-root /path/to/project",
            "tools": ["cce_search", "cce_list_corpora", "cce_provider_readiness"],
        },
        "interface_contract": build_interface_contract(resolved_project_root),
        "signal_chain": build_signal_chain(resolved_project_root),
        "h1_actions": actions,
        "audience_offer": {
            "show_hn_title": "Show HN: Search all your AI conversations from one place",
            "first_customer_segment": "AI consulting firms or AI-native startups",
            "loop_1_offer": "Recall and search multi-provider AI conversation memory.",
            "professional_price": "$29/mo",
            "builder_price": "$100-500/mo",
        },
        "gate_evidence": {
            "REP-001": {
                "check": "At least 1 mechanism emits INTERFACE_CONTRACT",
                "state": "engine-ready",
                "evidence": "CCE emits STATE_MODEL and VALIDATION_RECORD artifacts for ORGAN-II surfaces.",
            },
            "REP-002": {
                "check": "digestive--measure can metabolize TRANSACTION signals",
                "state": "external-action-required",
                "evidence": "Stripe belongs in conversation-corpus--product, not this ORGAN-I repo.",
            },
            "REP-003": {
                "check": "At least 1 paying customer exists",
                "state": "external-action-required",
                "evidence": "Revenue status must be recorded after billing/product activation.",
            },
        },
    }


def render_commercial_h1_readiness(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    lines = [
        "# CCE Commercial H1 Readiness",
        "",
        f"- Generated: {payload.get('generated_at') or 'n/a'}",
        f"- Repo-ready actions: {summary.get('repo_ready_action_count', 0)}/{summary.get('repo_action_count', 0)}",
        f"- External actions remaining: {summary.get('external_actions_remaining', 0)}",
        f"- MCP install: {(payload.get('mcp_distribution') or {}).get('pipx_install') or 'n/a'}",
        "",
        "## H1 Actions",
        "",
    ]
    for item in payload.get("h1_actions") or []:
        lines.append(f"- {item['id']}: {item['status']} - {item['title']}")
        lines.append(f"  next: {item.get('next_action') or 'n/a'}")
    lines.extend(["", "## Interface Contract", ""])
    interface_contract = payload.get("interface_contract") or {}
    lines.append(
        "- "
        + f"{interface_contract.get('source_repo')} -> {interface_contract.get('target_repo')} "
        + f"({interface_contract.get('expected_output_signal')})"
    )
    lines.extend(["", "## Signal Chain", ""])
    for item in payload.get("signal_chain") or []:
        lines.append(f"- {item['from']} --{item['signal']}--> {item['to']}")
    return "\n".join(lines)


def write_commercial_h1_artifacts(
    project_root: Path,
    payload: dict[str, Any],
) -> dict[str, str]:
    json_path = commercial_h1_json_path(project_root)
    markdown_path = commercial_h1_markdown_path(project_root)
    write_json(json_path, payload)
    write_markdown(markdown_path, render_commercial_h1_readiness(payload))
    return {
        "json_path": str(json_path),
        "markdown_path": str(markdown_path),
    }
