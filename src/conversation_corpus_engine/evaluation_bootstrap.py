#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .answering import load_json, write_markdown
from .corpus_store import (
    CorpusWriteAuthorization,
    authorize_corpus_write,
    load_corpus_store_registry,
    resolve_configured_corpus_store_root,
)
from .evaluation import run_corpus_evaluation, seed_gold
from .paths import default_project_root
from .provider_catalog import (
    default_source_drop_root,
    get_provider_config,
    provider_bootstrap_report_path,
    provider_corpus_targets,
)
from .source_policy import source_policy_path

DEFAULT_PROJECT_ROOT = default_project_root()
DEFAULT_CLAUDE_POLICY_PATH = source_policy_path(DEFAULT_PROJECT_ROOT, "claude")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed and scaffold manual evaluation for a provider corpus.",
    )
    parser.add_argument("--project-root", type=Path, default=DEFAULT_PROJECT_ROOT)
    parser.add_argument("--corpus-store-root", type=Path)
    parser.add_argument("--provider")
    parser.add_argument("--target-root", type=Path)
    parser.add_argument("--policy-path", type=Path, default=DEFAULT_CLAUDE_POLICY_PATH)
    parser.add_argument(
        "--full-eval",
        action="store_true",
        help="Run the seeded corpus evaluation after scaffolding.",
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def resolve_target_root(
    *,
    project_root: Path,
    provider: str | None,
    explicit_target_root: Path | None,
    policy_path: Path | None = None,
    registry: list[dict[str, Any]] | None = None,
) -> tuple[Path, dict[str, Any]]:
    if explicit_target_root is not None:
        return explicit_target_root, {
            "provider": provider,
            "selection": "explicit",
            "policy_path": str(policy_path.resolve()) if policy_path else None,
            "policy": None,
        }

    if provider is None:
        raise ValueError("Provide --provider or --target-root.")

    resolved_project_root = project_root.resolve()
    if policy_path is not None and policy_path.is_file():
        policy = load_json(policy_path, default={}) or {}
        primary_root = policy.get("primary_root")
        if primary_root:
            return Path(primary_root), {
                "provider": provider,
                "selection": "primary",
                "policy_path": str(policy_path.resolve()),
                "policy": policy,
            }
    source_drop_root = default_source_drop_root(resolved_project_root)
    targets = provider_corpus_targets(
        resolved_project_root, provider, source_drop_root, registry=registry
    )
    target = next((item for item in targets if item.get("selected")), targets[0])
    return Path(target["root"]).resolve(), {
        "provider": provider,
        "selection": target.get("role") or "primary",
        "policy_path": str(policy_path.resolve()) if policy_path is not None else None,
        "policy": target.get("policy"),
    }


def corpus_metadata(target_root: Path) -> dict[str, Any]:
    contract = load_json(target_root / "corpus" / "contract.json", default={}) or {}
    evaluation = load_json(target_root / "corpus" / "evaluation-summary.json", default={}) or {}
    gates = load_json(target_root / "corpus" / "regression-gates.json", default={}) or {}
    return {
        "corpus_id": contract.get("corpus_id"),
        "name": contract.get("name"),
        "adapter_type": contract.get("adapter_type"),
        "evaluation_overall_state": gates.get("overall_state")
        or (evaluation.get("regression_gates") or {}).get("overall_state"),
        "source_reliability_state": gates.get("source_reliability_state")
        or (evaluation.get("regression_gates") or {}).get("source_reliability_state"),
    }


def render_manual_guide(
    *,
    provider_slug: str | None,
    provider_name: str,
    target_root: Path,
    project_root: Path,
    corpus_store_root: Path,
    full_eval: bool,
) -> str:
    quoted_target_root = shlex.quote(str(target_root))
    quoted_project_root = shlex.quote(str(project_root))
    quoted_store_root = shlex.quote(str(corpus_store_root))
    rerun_command = (
        f"cce evaluation run --root {quoted_target_root} "
        f"--project-root {quoted_project_root} "
        f"--corpus-store-root {quoted_store_root}"
    )
    lines = [
        f"# {provider_name} Manual Evaluation Guide",
        "",
        f"- Generated: {datetime.now(timezone.utc).isoformat()}",
        f"- Target root: {target_root}",
        f"- Full eval ran during bootstrap: {'yes' if full_eval else 'no'}",
        "",
        "## Next Manual Steps",
        "",
        "- Review `eval/gold/manual/detectors.json` and confirm or reject seeded detector truth.",
        "- Review `eval/gold/manual/families.json` and confirm canonical family membership.",
        "- Review `eval/fixtures/manual/retrieval.json` and replace seeded retrieval prompts with provider-specific cases.",
        "- Review `eval/gold/manual/answers.json` and replace seeded answer fixtures with grounded expectations.",
        f"- Re-run `{rerun_command}` after manual edits.",
    ]
    if provider_slug is not None:
        refresh_command = (
            "cce provider bootstrap-eval "
            f"--provider {shlex.quote(provider_slug)} "
            f"--project-root {quoted_project_root} "
            f"--target-root {quoted_target_root} "
            f"--corpus-store-root {quoted_store_root} --full-eval"
        )
        lines.append(
            f"- Use `{refresh_command}` only when you want the seeded baseline refreshed and re-scored."
        )
    return "\n".join(lines)


def bootstrap_provider_evaluation(
    *,
    project_root: Path,
    provider: str | None,
    target_root: Path | None = None,
    policy_path: Path | None = None,
    full_eval: bool = False,
    corpus_store_root: Path | None = None,
    authorization: CorpusWriteAuthorization | None = None,
) -> dict[str, Any]:
    resolved_project_root = project_root.resolve()
    configured_store_root = (
        authorization.store_root
        if authorization is not None
        else resolve_configured_corpus_store_root(corpus_store_root)
    )
    registry_entries = None
    if target_root is None and provider is not None:
        registry_entries = load_corpus_store_registry(resolved_project_root)["corpora"]
    resolved_target_root, resolution = resolve_target_root(
        project_root=resolved_project_root,
        provider=provider,
        explicit_target_root=target_root,
        policy_path=policy_path,
        registry=registry_entries,
    )
    if not resolved_target_root.exists():
        raise FileNotFoundError(f"Target corpus root does not exist: {resolved_target_root}")
    resolved_authorization = authorize_corpus_write(
        project_root=resolved_project_root,
        corpus_store_root=configured_store_root,
        destination=resolved_target_root,
    )
    resolved_target_root = resolved_authorization.destination

    if provider is not None:
        provider_name = get_provider_config(provider)["display_name"]
    else:
        provider_name = (
            load_json(resolved_target_root / "corpus" / "contract.json", default={}) or {}
        ).get(
            "name",
        ) or resolved_target_root.name

    seeded_paths = seed_gold(resolved_target_root)
    scorecard = None
    outputs: dict[str, str] = {}
    if full_eval:
        scorecard, resolved_outputs = run_corpus_evaluation(
            resolved_target_root,
            authorization=resolved_authorization,
        )
        outputs = {key: str(value) for key, value in resolved_outputs.items()}

    metadata = corpus_metadata(resolved_target_root)
    guidance_path = resolved_target_root / "eval" / "manual-review-guide.md"
    write_markdown(
        guidance_path,
        render_manual_guide(
            provider_slug=provider,
            provider_name=provider_name,
            target_root=resolved_target_root,
            project_root=resolved_project_root,
            corpus_store_root=resolved_authorization.store_root,
            full_eval=full_eval,
        ),
    )

    report_provider = provider or "external"
    report_path = provider_bootstrap_report_path(resolved_project_root, report_provider)
    policy = resolution.get("policy") or {}
    seeded_lines = [f"- {key}: {path}" for key, path in seeded_paths.items()]
    if outputs:
        seeded_lines.extend(
            [
                "",
                "## Evaluation Outputs",
                "",
                *[f"- {key}: {path}" for key, path in outputs.items()],
            ],
        )
    write_markdown(
        report_path,
        "\n".join(
            [
                f"# {provider_name} Evaluation Bootstrap",
                "",
                f"- Provider: {report_provider}",
                f"- Target root: {resolved_target_root}",
                f"- Full eval ran: {'yes' if full_eval else 'no'}",
                f"- Corpus id: {metadata.get('corpus_id') or 'n/a'}",
                f"- Adapter type: {metadata.get('adapter_type') or 'n/a'}",
                f"- Evaluation overall state: {metadata.get('evaluation_overall_state') or 'n/a'}",
                f"- Source reliability state: {metadata.get('source_reliability_state') or 'n/a'}",
                f"- Manual guide: {guidance_path}",
                f"- Resolution source: {resolution.get('selection')}",
                f"- Policy primary corpus: {policy.get('primary_corpus_id') or 'n/a'}",
                "",
                "## Seeded Paths",
                "",
                *seeded_lines,
            ],
        ),
    )

    payload = {
        "provider": report_provider,
        "provider_name": provider_name,
        "target_root": str(resolved_target_root),
        "policy_path": resolution.get("policy_path"),
        "resolution_source": resolution.get("selection"),
        "policy_primary_corpus_id": policy.get("primary_corpus_id"),
        "seeded_paths": {key: str(path) for key, path in seeded_paths.items()},
        "manual_guide_path": str(guidance_path),
        "report_path": str(report_path),
        "full_eval_ran": full_eval,
        "evaluation_overall_state": metadata.get("evaluation_overall_state"),
        "source_reliability_state": metadata.get("source_reliability_state"),
        "corpus_id": metadata.get("corpus_id"),
        "adapter_type": metadata.get("adapter_type"),
        "outputs": outputs,
    }
    if scorecard is not None:
        payload["scorecard"] = scorecard
    return payload


def bootstrap_claude_evaluation(
    *,
    project_root: Path,
    policy_path: Path = DEFAULT_CLAUDE_POLICY_PATH,
    target_root: Path | None = None,
    full_eval: bool = False,
    corpus_store_root: Path | None = None,
) -> dict[str, Any]:
    payload = bootstrap_provider_evaluation(
        project_root=project_root,
        provider="claude",
        target_root=target_root,
        policy_path=policy_path,
        full_eval=full_eval,
        corpus_store_root=corpus_store_root,
    )
    return {
        "target_root": payload["target_root"],
        "policy_path": payload["policy_path"],
        "policy_primary_corpus_id": payload.get("policy_primary_corpus_id"),
        "seeded_paths": payload["seeded_paths"],
        "manual_guide_path": payload["manual_guide_path"],
        "report_path": payload["report_path"],
        "full_eval_ran": payload["full_eval_ran"],
        "evaluation_overall_state": payload.get("evaluation_overall_state"),
        "source_reliability_state": payload.get("source_reliability_state"),
        "outputs": payload.get("outputs", {}),
    }


def main() -> int:
    args = parse_args()
    payload = bootstrap_provider_evaluation(
        project_root=args.project_root,
        provider=args.provider,
        target_root=args.target_root,
        policy_path=args.policy_path,
        full_eval=args.full_eval,
        corpus_store_root=args.corpus_store_root,
    )
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
