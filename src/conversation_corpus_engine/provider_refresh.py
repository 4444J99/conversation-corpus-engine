from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .answering import load_json, write_json, write_markdown
from .corpus_candidates import (
    load_corpus_candidate_manifest,
    promote_corpus_candidate,
    resolve_live_entry,
    review_corpus_candidate,
    stage_corpus_candidate,
)
from .corpus_store import (
    authorize_additional_corpus_path,
    authorize_corpus_write,
    resolve_configured_corpus_store_root,
)
from .evaluation import run_corpus_evaluation
from .provider_catalog import get_provider_config
from .provider_import import import_provider_corpus


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def timestamp_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def provider_refresh_runs_dir(project_root: Path) -> Path:
    return project_root.resolve() / "state" / "provider-refresh"


def provider_refresh_history_path(project_root: Path) -> Path:
    return project_root.resolve() / "state" / "provider-refresh-history.json"


def provider_refresh_latest_json_path(project_root: Path, provider: str) -> Path:
    return project_root.resolve() / "reports" / f"{provider}-refresh-latest.json"


def provider_refresh_latest_markdown_path(project_root: Path, provider: str) -> Path:
    return project_root.resolve() / "reports" / f"{provider}-refresh-latest.md"


def append_history(path: Path, entry: dict[str, Any]) -> dict[str, Any]:
    payload = load_json(path, default={"generated_at": None, "count": 0, "items": []}) or {
        "generated_at": None,
        "count": 0,
        "items": [],
    }
    payload.setdefault("items", []).append(entry)
    payload["generated_at"] = entry.get("generated_at")
    payload["count"] = len(payload["items"])
    payload["latest"] = payload["items"][-1] if payload["items"] else None
    write_json(path, payload)
    return payload


def infer_refresh_mode(
    provider: str,
    *,
    live_corpus_id: str,
    mode: str | None = None,
) -> tuple[str, str]:
    if mode is not None:
        return mode, "explicit"
    if provider != "claude":
        return "upload", "provider-default"
    config = get_provider_config(provider)
    if live_corpus_id == config.get("default_corpus_id"):
        return "local-session", "live-corpus-id"
    return "upload", "live-corpus-id"


def default_refresh_candidate_root(corpus_store_root: Path, provider: str, run_id: str) -> Path:
    return corpus_store_root / "provider-refresh" / provider / run_id / "candidate-corpus"


def default_refresh_receipt_root(corpus_store_root: Path, provider: str, run_id: str) -> Path:
    return corpus_store_root / "provider-refresh" / provider / run_id


def render_provider_refresh(payload: dict[str, Any]) -> str:
    evaluation = payload.get("evaluation") or {}
    candidate = payload.get("candidate") or {}
    promotion = payload.get("promotion") or {}
    lines = [
        f"# {payload.get('provider_name') or payload.get('provider', 'Unknown').title()} Refresh",
        "",
        f"- Run id: `{payload.get('run_id') or 'n/a'}`",
        f"- Generated: {payload.get('generated_at') or 'n/a'}",
        f"- Mode: {payload.get('mode') or 'n/a'}",
        f"- Live corpus id: `{payload.get('live_corpus_id') or 'n/a'}`",
        f"- Live root: {payload.get('live_root') or 'n/a'}",
        f"- Candidate root: {payload.get('candidate_root') or 'n/a'}",
        f"- Candidate status: {candidate.get('status') or 'n/a'}",
        f"- Candidate recommendation: {candidate.get('recommendation_state') or 'n/a'}",
        f"- Evaluation ran: {'yes' if evaluation.get('ran') else 'no'}",
        f"- Promoted: {'yes' if promotion else 'no'}",
    ]
    if payload.get("note"):
        lines.extend(["", "## Note", "", payload["note"]])
    return "\n".join(lines)


def refresh_provider_corpus(
    *,
    project_root: Path,
    provider: str,
    mode: str | None = None,
    source_drop_root: Path | None = None,
    source_path: Path | None = None,
    local_root: Path | None = None,
    live_corpus_id: str | None = None,
    candidate_root: Path | None = None,
    corpus_store_root: Path | None = None,
    bootstrap_eval: bool = True,
    run_eval: bool = True,
    approve: bool = False,
    promote: bool = False,
    note: str = "",
    throttle: float = 0.0,
) -> dict[str, Any]:
    resolved_project_root = project_root.resolve()
    configured_store_root = resolve_configured_corpus_store_root(corpus_store_root)
    run_id = f"{provider}-{timestamp_slug()}"
    requested_candidate_root = candidate_root or default_refresh_candidate_root(
        configured_store_root,
        provider,
        run_id,
    )
    preliminary_authorization = authorize_corpus_write(
        project_root=resolved_project_root,
        corpus_store_root=configured_store_root,
        destination=requested_candidate_root,
    )
    resolved_candidate_root = preliminary_authorization.destination
    live_entry = resolve_live_entry(
        resolved_project_root,
        live_corpus_id=live_corpus_id,
        provider=provider,
    )
    resolved_mode, mode_resolution = infer_refresh_mode(
        provider,
        live_corpus_id=live_entry["corpus_id"],
        mode=mode,
    )
    authorization = authorize_corpus_write(
        project_root=resolved_project_root,
        corpus_store_root=preliminary_authorization.store_root,
        destination=resolved_candidate_root,
        live_roots=(Path(live_entry["root"]),),
    )
    refresh_root = default_refresh_receipt_root(authorization.store_root, provider, run_id)
    resolved_refresh_root = authorize_additional_corpus_path(
        authorization,
        refresh_root,
    ).destination
    if resolved_refresh_root == resolved_candidate_root or resolved_refresh_root.is_relative_to(
        resolved_candidate_root
    ):
        raise ValueError("Refresh receipts must be written outside the candidate corpus.")

    import_result = import_provider_corpus(
        project_root=resolved_project_root,
        provider=provider,
        mode=resolved_mode,
        source_drop_root=source_drop_root,
        source_path=source_path,
        local_root=local_root,
        output_root=resolved_candidate_root,
        corpus_store_root=authorization.store_root,
        corpus_id=live_entry["corpus_id"],
        name=live_entry["name"],
        register=False,
        build=False,
        bootstrap_eval=bootstrap_eval,
        throttle=throttle,
    )

    scorecard = None
    evaluation_outputs: dict[str, str] = {}
    if run_eval:
        scorecard, outputs = run_corpus_evaluation(
            resolved_candidate_root,
            seed=True,
            authorization=authorization,
        )
        evaluation_outputs = {key: str(value) for key, value in outputs.items()}

    candidate_manifest = stage_corpus_candidate(
        resolved_project_root,
        candidate_root=resolved_candidate_root,
        live_corpus_id=live_entry["corpus_id"],
        provider=provider,
        note=note or f"Refresh candidate for {provider}.",
    )

    review_result = None
    promotion_result = None
    if approve or promote:
        review_result = review_corpus_candidate(
            resolved_project_root,
            candidate_manifest["candidate_id"],
            decision="approve",
            note=note or f"Approved during {provider} refresh orchestration.",
        )
    if promote:
        promotion_result = promote_corpus_candidate(
            resolved_project_root,
            candidate_manifest["candidate_id"],
            note=note or f"Promoted during {provider} refresh orchestration.",
        )

    latest_candidate_manifest = load_corpus_candidate_manifest(
        resolved_project_root,
        candidate_id=candidate_manifest["candidate_id"],
    )
    payload = {
        "run_id": run_id,
        "generated_at": now_iso(),
        "project_root": str(resolved_project_root),
        "provider": provider,
        "provider_name": get_provider_config(provider)["display_name"],
        "mode": resolved_mode,
        "mode_resolution": mode_resolution,
        "live_corpus_id": live_entry["corpus_id"],
        "live_root": str(Path(live_entry["root"]).resolve()),
        "candidate_root": str(resolved_candidate_root),
        "source_path": import_result.get("source_path"),
        "note": note,
        "import_result": import_result,
        "evaluation": {
            "ran": run_eval,
            "outputs": evaluation_outputs,
            "scorecard": scorecard,
        },
        "candidate": latest_candidate_manifest,
        "review": review_result,
        "promotion": promotion_result,
        "refresh_json_path": str(resolved_refresh_root / "refresh.json"),
        "refresh_markdown_path": str(resolved_refresh_root / "refresh.md"),
    }
    write_json(resolved_refresh_root / "refresh.json", payload)
    write_markdown(resolved_refresh_root / "refresh.md", render_provider_refresh(payload))
    write_json(provider_refresh_latest_json_path(resolved_project_root, provider), payload)
    write_markdown(
        provider_refresh_latest_markdown_path(resolved_project_root, provider),
        render_provider_refresh(payload),
    )
    append_history(
        provider_refresh_history_path(resolved_project_root),
        {
            "generated_at": payload["generated_at"],
            "run_id": run_id,
            "provider": provider,
            "mode": resolved_mode,
            "live_corpus_id": live_entry["corpus_id"],
            "candidate_root": str(resolved_candidate_root),
            "candidate_id": latest_candidate_manifest.get("candidate_id"),
            "candidate_status": latest_candidate_manifest.get("status"),
            "promotion": bool(promotion_result),
        },
    )
    return payload
