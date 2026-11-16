from __future__ import annotations

import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .answering import write_json, write_markdown
from .federation import load_corpus_surface, load_registry
from .provider_catalog import PROVIDER_CONFIG, get_provider_config, provider_corpus_targets
from .provider_discovery import discover_provider_uploads


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def inspect_target(
    target: dict[str, Any],
    registry_by_corpus_id: dict[str, dict[str, Any]],
    registry_by_root: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    root = Path(target["root"]).resolve()
    contract_path = root / "corpus" / "contract.json"
    manual_guide_path = root / "eval" / "manual-review-guide.md"
    entry = registry_by_corpus_id.get(target["corpus_id"]) or registry_by_root.get(str(root))
    payload: dict[str, Any] = {
        "role": target["role"],
        "selected": bool(target.get("selected")),
        "corpus_id": target["corpus_id"],
        "corpus_name": target.get("corpus_name"),
        "root": str(root),
        "exists": root.exists(),
        "contract_present": contract_path.exists(),
        "manual_guide_path": str(manual_guide_path),
        "manual_guide_present": manual_guide_path.exists(),
        "registered": bool(entry),
        "registry_status": entry.get("status") if entry else None,
        "default": bool(entry.get("default")) if entry else False,
    }
    if contract_path.exists():
        surface = load_corpus_surface(
            {
                "corpus_id": target["corpus_id"],
                "name": target.get("corpus_name") or target["corpus_id"],
                "root": str(root),
            },
        )
        payload["summary"] = surface["summary"]
        payload["contract_manifest"] = surface["contract_manifest"]
    else:
        payload["summary"] = {}
        payload["contract_manifest"] = {}
    return payload


def target_readiness_state(target: dict[str, Any]) -> str:
    if not target["exists"]:
        return "missing"
    if not target["contract_present"]:
        return "missing-contract"
    summary = target.get("summary") or {}
    eval_state = summary.get("evaluation_overall_state")
    if not target["manual_guide_present"]:
        return "imported-needs-bootstrap"
    if eval_state != "pass":
        return "manual-eval-pending" if eval_state else "manual-eval-unrun"
    if target["registered"] and target.get("registry_status") == "active":
        return "healthy-federation"
    return "calibration-pass"


def target_register_command(target: dict[str, Any]) -> str:
    corpus_name = (
        target.get("contract_manifest", {}).get("name")
        or target.get("corpus_name")
        or target["corpus_id"]
    )
    return (
        f"cce corpus register {shlex.quote(target['root'])} "
        f"--corpus-id {shlex.quote(target['corpus_id'])} --name {shlex.quote(corpus_name)}"
    )


def corpus_store_register_command(project_root: Path) -> str:
    return (
        "cce corpus-store register "
        f"--project-root {shlex.quote(str(project_root.resolve()))} "
        "--root /path/to/private-corpus-store"
    )


def corpus_writer_command(
    command: str,
    *,
    project_root: Path,
    corpus_store_root: Path | None,
) -> str:
    if corpus_store_root is None:
        return corpus_store_register_command(project_root)
    return f"{command} --corpus-store-root {shlex.quote(str(corpus_store_root.resolve()))}"


def target_is_store_backed(target: dict[str, Any], corpus_store_root: Path | None) -> bool:
    if corpus_store_root is None:
        return False
    return Path(target["root"]).resolve().is_relative_to(corpus_store_root.resolve())


def legacy_target_guidance(
    target: dict[str, Any],
    corpus_store_root: Path,
) -> str:
    destination = corpus_store_root.resolve() / target["corpus_id"]
    return (
        f"Migrate or re-import {target['corpus_id']} into the active corpus store "
        f"at {destination} before writing evaluation assets."
    )


def determine_next_command(
    provider: str,
    discovery: dict[str, Any],
    selected_target: dict[str, Any],
    *,
    project_root: Path,
    corpus_store_root: Path | None,
) -> str:
    config = get_provider_config(provider)
    state = target_readiness_state(selected_target)
    source_drop_root = discovery["inbox_root"].rsplit(f"/{provider}/inbox", 1)[0]
    if state == "missing":
        if discovery.get("upload_state") == "ready":
            return corpus_writer_command(
                f"cce provider import --provider {provider} "
                f"--project-root {shlex.quote(str(project_root.resolve()))} "
                f"--source-drop-root {shlex.quote(source_drop_root)} --register --build",
                project_root=project_root,
                corpus_store_root=corpus_store_root,
            )
        return f"Place a supported {config['display_name']} export in {discovery['inbox_root']}"
    if (
        state
        in {
            "missing-contract",
            "imported-needs-bootstrap",
            "manual-eval-pending",
            "manual-eval-unrun",
        }
        and corpus_store_root is not None
        and not target_is_store_backed(selected_target, corpus_store_root)
    ):
        return legacy_target_guidance(selected_target, corpus_store_root)
    if state == "missing-contract":
        return f"Create corpus contract and index files under {selected_target['root']}/corpus"
    if state == "imported-needs-bootstrap":
        return corpus_writer_command(
            f"cce provider bootstrap-eval --provider {provider} "
            f"--project-root {shlex.quote(str(project_root.resolve()))} "
            f"--target-root {shlex.quote(selected_target['root'])}",
            project_root=project_root,
            corpus_store_root=corpus_store_root,
        )
    if state in {"manual-eval-pending", "manual-eval-unrun"}:
        return corpus_writer_command(
            f"cce evaluation run --root {shlex.quote(selected_target['root'])} "
            f"--project-root {shlex.quote(str(project_root.resolve()))}",
            project_root=project_root,
            corpus_store_root=corpus_store_root,
        )
    if state == "calibration-pass":
        return target_register_command(selected_target)
    if state == "healthy-federation" and discovery.get("upload_state") == "ready":
        return corpus_writer_command(
            f"cce provider refresh --provider {provider} "
            f"--project-root {shlex.quote(str(project_root.resolve()))} "
            f"--source-drop-root {shlex.quote(source_drop_root)}",
            project_root=project_root,
            corpus_store_root=corpus_store_root,
        )
    return "ready"


def summarize_provider_readiness(
    provider: str,
    discovery: dict[str, Any],
    registry_by_corpus_id: dict[str, dict[str, Any]],
    registry_by_root: dict[str, dict[str, Any]],
    project_root: Path,
    source_drop_root: Path,
    corpus_store_root: Path | None,
) -> dict[str, Any]:
    config = get_provider_config(provider)
    registry = list(registry_by_corpus_id.values())
    provider_targets = provider_corpus_targets(
        project_root, provider, source_drop_root, registry=registry
    )
    targets = [
        inspect_target(target, registry_by_corpus_id, registry_by_root)
        for target in provider_targets
    ]
    for item in targets:
        item["readiness_state"] = target_readiness_state(item)
    selected_target = next((item for item in targets if item.get("selected")), targets[0])
    policy = next(
        (target.get("policy") for target in provider_targets if target.get("policy")), None
    )
    return {
        "provider": provider,
        "display_name": config["display_name"],
        "adapter_state": config["adapter_state"],
        "calibration_only": config.get("calibration_only", False),
        "discovery": discovery,
        "targets": targets,
        "selected_target": selected_target,
        "overall_state": selected_target["readiness_state"],
        "next_command": determine_next_command(
            provider,
            discovery,
            selected_target,
            project_root=project_root,
            corpus_store_root=corpus_store_root,
        ),
        "notes": config.get("notes", []),
        "policy": {
            "decision": policy.get("decision") if policy else None,
            "primary_corpus_id": policy.get("primary_corpus_id") if policy else None,
            "fallback_corpus_id": policy.get("fallback_corpus_id") if policy else None,
        }
        if policy
        else None,
    }


def build_provider_readiness(project_root: Path, source_drop_root: Path) -> dict[str, Any]:
    project_root = project_root.resolve()
    source_drop_root = source_drop_root.resolve()
    discovery_payload = discover_provider_uploads(project_root, source_drop_root)
    discovery_by_provider = {item["provider"]: item for item in discovery_payload["providers"]}
    registry_payload = load_registry(project_root)
    registry = registry_payload.get("corpora") or []
    registry_by_corpus_id = {item["corpus_id"]: item for item in registry}
    registry_by_root = {str(Path(item["root"]).resolve()): item for item in registry}
    store_registration = registry_payload.get("corpus_store")
    corpus_store_root = (
        Path(store_registration["root"])
        if isinstance(store_registration, dict)
        and store_registration.get("status") == "active"
        and isinstance(store_registration.get("root"), str)
        else None
    )
    providers = [
        summarize_provider_readiness(
            provider,
            discovery_by_provider[provider],
            registry_by_corpus_id,
            registry_by_root,
            project_root,
            source_drop_root,
            corpus_store_root,
        )
        for provider in PROVIDER_CONFIG
    ]
    return {
        "generated_at": now_iso(),
        "project_root": str(project_root),
        "source_drop_root": str(source_drop_root),
        "counts": {
            "providers": len(providers),
            "corpora_present": sum(
                1 for item in providers if item["selected_target"].get("contract_present")
            ),
            "bootstrap_ready": sum(
                1 for item in providers if item["overall_state"] == "imported-needs-bootstrap"
            ),
            "manual_pass": sum(
                1
                for item in providers
                if item["selected_target"].get("summary", {}).get("evaluation_overall_state")
                == "pass"
            ),
            "registered_active": sum(
                1
                for item in providers
                if item["selected_target"].get("registered")
                and item["selected_target"].get("registry_status") == "active"
            ),
        },
        "providers": providers,
    }


def render_provider_readiness_text(payload: dict[str, Any]) -> str:
    lines = [
        f"Provider readiness for {payload['project_root']}",
        f"Generated: {payload['generated_at']}",
        f"Source-drop root: {payload['source_drop_root']}",
        "",
    ]
    for item in payload["providers"]:
        selected = item["selected_target"]
        lines.append(f"{item['display_name']}  state={item['overall_state']}")
        lines.append(f"  selected_corpus: {selected.get('corpus_id') or 'n/a'}")
        lines.append(f"  selected_root: {selected['root']}")
        lines.append(f"  inbox_state: {item['discovery'].get('upload_state')}")
        lines.append(f"  next_command: {item['next_command']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def render_provider_readiness_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Provider Readiness",
        "",
        f"- Generated: {payload['generated_at']}",
        f"- Providers: {payload['counts']['providers']}",
        f"- Corpora present: {payload['counts']['corpora_present']}",
        f"- Bootstrap-ready corpora: {payload['counts']['bootstrap_ready']}",
        f"- Manual-pass corpora: {payload['counts']['manual_pass']}",
        f"- Registered active corpora: {payload['counts']['registered_active']}",
        "",
    ]
    for item in payload["providers"]:
        selected = item["selected_target"]
        lines.extend(
            [
                f"## {item['display_name']}",
                "",
                f"- Overall state: {item['overall_state']}",
                f"- Selected corpus: {selected.get('corpus_id') or 'n/a'}",
                f"- Selected root: {selected['root']}",
                f"- Adapter type: {selected.get('summary', {}).get('adapter_type') or selected.get('contract_manifest', {}).get('adapter_type') or 'n/a'}",
                f"- Evaluation gate: {selected.get('summary', {}).get('evaluation_overall_state') or 'n/a'}",
                f"- Registered: {'yes' if selected.get('registered') else 'no'}",
                f"- Inbox state: {item['discovery'].get('upload_state')}",
                f"- Next command: `{item['next_command']}`",
            ],
        )
        if item.get("policy"):
            lines.append(f"- Source policy: {item['policy'].get('decision') or 'n/a'}")
            lines.append(
                f"- Primary/fallback: {item['policy'].get('primary_corpus_id') or 'n/a'} / {item['policy'].get('fallback_corpus_id') or 'n/a'}",
            )
        for note in item["notes"]:
            lines.append(f"- Note: {note}")
        lines.append("")
        lines.append("### Targets")
        lines.append("")
        for target in item["targets"]:
            lines.append(
                f"- {target['role']}: {target['corpus_id']}  state={target['readiness_state']}  registered={'yes' if target.get('registered') else 'no'}",
            )
            lines.append(f"  root: {target['root']}")
            if target.get("summary", {}).get("evaluation_overall_state"):
                lines.append(
                    f"  gate: {target['summary']['evaluation_overall_state']}  freshness: {target['summary'].get('source_freshness_state') or 'n/a'}",
                )
        lines.append("")
    return "\n".join(lines).rstrip()


def write_provider_readiness_reports(project_root: Path, payload: dict[str, Any]) -> dict[str, str]:
    reports_dir = project_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = reports_dir / "provider-readiness-latest.json"
    md_path = reports_dir / "provider-readiness-latest.md"
    write_json(json_path, payload)
    write_markdown(md_path, render_provider_readiness_markdown(payload))
    return {"json_path": str(json_path), "markdown_path": str(md_path)}
