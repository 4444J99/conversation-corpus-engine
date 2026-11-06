#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .answering import load_json, write_json, write_markdown
from .claude_local_session import (
    DEFAULT_CLAUDE_LOCAL_ROOT,
    discover_claude_local_session,
    fetch_claude_local_session_bundle,
    load_prior_acquisition,
    save_acquisition_state,
)
from .corpus_store import git_visible_refusal
from .import_claude_export_corpus import import_claude_export_corpus, now_iso
from .sharded_collection import content_hash_key, write_collection
from .source_lifecycle import build_source_snapshot

if TYPE_CHECKING:
    from .corpus_store import CorpusWriteAuthorization

DEFAULT_OUTPUT_ROOT = Path.cwd() / "claude-local-session-memory"
DEFAULT_CORPUS_ID = "claude-local-session-memory"
DEFAULT_NAME = "Claude Local Session Memory"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import the local signed-in Claude desktop session into a federation-compatible calibration corpus.",
    )
    parser.add_argument("--local-root", type=Path, default=DEFAULT_CLAUDE_LOCAL_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--corpus-id", default=DEFAULT_CORPUS_ID)
    parser.add_argument("--name", default=DEFAULT_NAME)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def write_local_session_bundle(bundle_root: Path, bundle: dict[str, Any]) -> None:
    bundle_root.mkdir(parents=True, exist_ok=True)
    write_json(bundle_root / "bootstrap.json", bundle.get("bootstrap") or {})
    write_json(bundle_root / "organizations.json", bundle.get("organizations") or [])
    write_json(bundle_root / "projects.json", bundle.get("projects") or [])
    write_json(bundle_root / "memories.json", bundle.get("memories") or [])
    write_json(bundle_root / "users.json", bundle.get("users") or [])
    write_json(
        bundle_root / "conversation-summaries.json", bundle.get("conversation_summaries") or []
    )
    write_json(
        bundle_root / "conversation-detail-failures.json",
        bundle.get("conversation_detail_failures") or [],
    )
    write_collection(
        bundle_root / "conversations.json",
        bundle.get("conversations") or [],
        key=lambda record: (
            str(record.get("uuid") or record.get("id"))
            if isinstance(record, dict) and (record.get("uuid") or record.get("id"))
            else content_hash_key(record)
        ),
    )
    details_root = bundle_root / "conversation-details"
    details_root.mkdir(parents=True, exist_ok=True)
    for conversation in bundle.get("conversations") or []:
        conversation_uuid = conversation.get("uuid")
        if not conversation_uuid:
            continue
        write_json(details_root / f"{conversation_uuid}.json", conversation)


def patch_contract_for_local_session(
    output_root: Path,
    *,
    local_root: Path,
    discovery: dict[str, Any],
) -> None:
    corpus_dir = output_root / "corpus"
    contract_path = corpus_dir / "contract.json"
    contract = load_json(contract_path, default={}) or {}
    source_snapshot = build_source_snapshot(local_root, "claude-local-session", "local-session")
    contract.update(
        {
            "adapter_type": "claude-local-session",
            "source_input": str(local_root),
            "collection_scope": "local-session",
            "source_snapshot_path": "corpus/source-snapshot.json",
            "source_signature_fingerprint": source_snapshot.get("signature_fingerprint"),
            "source_content_fingerprint": source_snapshot.get("content_fingerprint"),
            "source_file_count": source_snapshot.get("file_count"),
            "source_total_bytes": source_snapshot.get("total_bytes"),
            "source_latest_mtime_ns": source_snapshot.get("latest_mtime_ns"),
            "local_session": {
                "discovered_at": discovery.get("generated_at") or now_iso(),
                "safe_storage_service": discovery.get("safe_storage_service"),
                "active_org_uuid": discovery.get("active_org_uuid"),
                "account_uuid": discovery.get("account_uuid"),
                "account_email": discovery.get("account_email"),
                "conversation_count": discovery.get("conversation_count"),
                "project_count": discovery.get("project_count"),
            },
        },
    )
    write_json(corpus_dir / "source-snapshot.json", source_snapshot)
    write_json(contract_path, contract)

    evaluation_summary = load_json(corpus_dir / "evaluation-summary.json", default={}) or {}
    evaluation_summary["notes"] = [
        "Imported Claude local-session corpus has not been manually evaluated."
    ]
    write_json(corpus_dir / "evaluation-summary.json", evaluation_summary)

    regression_gates = load_json(corpus_dir / "regression-gates.json", default={}) or {}
    regression_gates["source_notes"] = [
        "Imported Claude local-session corpus has not been manually evaluated."
    ]
    write_json(corpus_dir / "regression-gates.json", regression_gates)


def rewrite_readme_for_local_session(
    output_root: Path, *, local_root: Path, bundle: dict[str, Any]
) -> None:
    conversation_failures = bundle.get("conversation_detail_failures") or []
    write_markdown(
        output_root / "README.md",
        "\n".join(
            [
                "# Claude Local Session Memory Corpus",
                "",
                f"- Generated: {now_iso()}",
                f"- Source input: {local_root}",
                "- Adapter type: claude-local-session",
                f"- Active org UUID: {bundle.get('active_org_uuid') or 'unknown'}",
                f"- Imported conversations: {len(bundle.get('conversations') or [])}",
                f"- Detail fetch failures: {len(conversation_failures)}",
                f"- Project count: {len(bundle.get('projects') or [])}",
                f"- User count: {len(bundle.get('users') or [])}",
                f"- Contract manifest: {output_root / 'corpus' / 'contract.json'}",
                "",
                "This corpus was imported from the local signed-in Claude desktop session in calibration mode.",
            ],
        ),
    )


def import_claude_local_session_corpus(
    local_root: Path,
    output_root: Path,
    *,
    corpus_id: str = DEFAULT_CORPUS_ID,
    name: str = DEFAULT_NAME,
    throttle: float = 0.0,
    authorization: CorpusWriteAuthorization | None = None,
) -> dict[str, Any]:
    local_root = local_root.resolve()
    discovery = discover_claude_local_session(local_root)

    # Delta-aware: load prior state and pass to bundle fetcher
    prior_state = load_prior_acquisition(output_root)
    bundle = fetch_claude_local_session_bundle(
        local_root, prior_state=prior_state, output_root=output_root
    )

    # Persist acquisition state for delta-sync on next run
    conversations_state: dict[str, dict[str, Any]] = {}
    for conv in bundle.get("conversations") or []:
        cuuid = conv.get("uuid")
        if cuuid:
            conversations_state[cuuid] = {
                "updated_at": conv.get("updated_at"),
                "fetched_at": now_iso(),
            }
    save_acquisition_state(
        output_root,
        conversations_state,
        report=bundle.get("acquisition_report") or {},
    )

    with tempfile.TemporaryDirectory(prefix="claude-local-session-") as tmpdir:
        bundle_root = Path(tmpdir) / "claude-local-bundle"
        write_local_session_bundle(bundle_root, bundle)
        result = import_claude_export_corpus(
            bundle_root,
            output_root,
            corpus_id=corpus_id,
            name=name,
            throttle=throttle,
            authorization=authorization,
        )
        source_root = output_root / "source"
        source_root.mkdir(parents=True, exist_ok=True)
        write_json(source_root / "local-session-discovery.json", discovery)
        write_json(
            source_root / "local-session-metadata.json",
            {
                "generated_at": bundle.get("generated_at") or now_iso(),
                "local_root": str(local_root),
                "active_org_uuid": bundle.get("active_org_uuid"),
                "safe_storage_service": bundle.get("safe_storage_service"),
                "cookie_names": bundle.get("cookie_names") or [],
                "detail_failure_count": len(bundle.get("conversation_detail_failures") or []),
            },
        )

    patch_contract_for_local_session(output_root, local_root=local_root, discovery=discovery)
    rewrite_readme_for_local_session(output_root, local_root=local_root, bundle=bundle)

    result["source_type"] = "claude-local-session"
    result["local_root"] = str(local_root)
    result["discovery_path"] = str(output_root / "source" / "local-session-discovery.json")
    result["detail_failure_count"] = len(bundle.get("conversation_detail_failures") or [])
    return result


def main() -> int:
    args = parse_args()
    # The corpus is the signed-in session's conversations as plaintext, and --output-root
    # defaults to the working directory: refuse a folder Git can see before the session is read.
    if (refusal := git_visible_refusal(args.output_root)) is not None:
        print(f"output refused: {refusal}", file=sys.stderr)
        return 2
    payload = import_claude_local_session_corpus(
        args.local_root,
        args.output_root,
        corpus_id=args.corpus_id,
        name=args.name,
    )
    print(json.dumps(payload, indent=2) if args.json else json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
