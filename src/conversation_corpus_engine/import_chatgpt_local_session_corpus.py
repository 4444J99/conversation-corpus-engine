#!/usr/bin/env python3
"""Import a ChatGPT local-session corpus.

Mirrors import_claude_local_session_corpus.py: discovers the ChatGPT desktop
app session via the binary cookie jar, fetches conversations through the
backend API, writes a bundle compatible with import_chatgpt_export_corpus,
then delegates to the existing ChatGPT import adapter for corpus generation.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .answering import load_json, write_json, write_markdown
from .chatgpt_local_session import (
    discover_chatgpt_local_session,
    fetch_chatgpt_local_session_bundle,
    load_prior_acquisition,
    now_iso,
    save_acquisition_state,
    scope_preflight_check,
)
from .import_chatgpt_export_corpus import import_chatgpt_export_corpus
from .sharded_collection import content_hash_key, write_collection
from .source_lifecycle import build_source_snapshot

if TYPE_CHECKING:
    from .corpus_store import CorpusWriteAuthorization

DEFAULT_OUTPUT_ROOT = Path.cwd() / "chatgpt-local-session-memory"
DEFAULT_CORPUS_ID = "chatgpt-local-session-memory"
DEFAULT_NAME = "ChatGPT Local Session Memory"


def write_local_session_bundle(bundle_root: Path, bundle: dict[str, Any]) -> None:
    bundle_root.mkdir(parents=True, exist_ok=True)
    user_payload = bundle.get("user") or {}
    write_json(bundle_root / "user.json", user_payload)
    write_collection(
        bundle_root / "conversations.json",
        bundle.get("conversations") or [],
        key=lambda record: (
            str(record.get("conversation_id") or record.get("id"))
            if isinstance(record, dict) and (record.get("conversation_id") or record.get("id"))
            else content_hash_key(record)
        ),
    )
    write_json(
        bundle_root / "conversation-summaries.json",
        bundle.get("conversation_summaries") or [],
    )
    write_json(
        bundle_root / "conversation-detail-failures.json",
        bundle.get("conversation_detail_failures") or [],
    )


def patch_contract_for_local_session(
    output_root: Path,
    *,
    cookie_jar: Path,
    discovery: dict[str, Any],
) -> None:
    corpus_dir = output_root / "corpus"
    contract_path = corpus_dir / "contract.json"
    contract = load_json(contract_path, default={}) or {}
    source_snapshot = build_source_snapshot(
        cookie_jar.parent, "chatgpt-local-session", "local-session"
    )
    contract.update(
        {
            "adapter_type": "chatgpt-local-session",
            "source_input": str(cookie_jar),
            "collection_scope": "local-session",
            "source_snapshot_path": "corpus/source-snapshot.json",
            "source_signature_fingerprint": source_snapshot.get("signature_fingerprint"),
            "source_content_fingerprint": source_snapshot.get("content_fingerprint"),
            "source_file_count": source_snapshot.get("file_count"),
            "source_total_bytes": source_snapshot.get("total_bytes"),
            "source_latest_mtime_ns": source_snapshot.get("latest_mtime_ns"),
            "local_session": {
                "discovered_at": discovery.get("generated_at") or now_iso(),
                "account_id": discovery.get("account_id"),
                "account_email": discovery.get("account_email"),
                "conversation_count": discovery.get("conversation_count"),
            },
        },
    )
    write_json(corpus_dir / "source-snapshot.json", source_snapshot)
    write_json(contract_path, contract)

    evaluation_summary = load_json(corpus_dir / "evaluation-summary.json", default={}) or {}
    evaluation_summary["notes"] = [
        "Imported ChatGPT local-session corpus has not been manually evaluated."
    ]
    write_json(corpus_dir / "evaluation-summary.json", evaluation_summary)

    regression_gates = load_json(corpus_dir / "regression-gates.json", default={}) or {}
    regression_gates["source_notes"] = [
        "Imported ChatGPT local-session corpus has not been manually evaluated."
    ]
    write_json(corpus_dir / "regression-gates.json", regression_gates)


def rewrite_readme_for_local_session(
    output_root: Path, *, cookie_jar: Path, bundle: dict[str, Any]
) -> None:
    conversation_failures = bundle.get("conversation_detail_failures") or []
    write_markdown(
        output_root / "README.md",
        "\n".join(
            [
                "# ChatGPT Local Session Memory Corpus",
                "",
                f"- Generated: {now_iso()}",
                f"- Source input: {cookie_jar}",
                "- Adapter type: chatgpt-local-session",
                f"- Account: {bundle.get('user', {}).get('email') or 'unknown'}",
                f"- Imported conversations: {len(bundle.get('conversations') or [])}",
                f"- Detail fetch failures: {len(conversation_failures)}",
                f"- Total available: {bundle.get('total_count', '?')}",
                f"- Contract manifest: {output_root / 'corpus' / 'contract.json'}",
                "",
                "This corpus was imported from the local signed-in ChatGPT desktop session.",
            ],
        ),
    )


def import_chatgpt_local_session_corpus(
    cookie_jar: Path,
    output_root: Path,
    *,
    corpus_id: str = DEFAULT_CORPUS_ID,
    name: str = DEFAULT_NAME,
    limit: int = 100,
    offset: int = 0,
    throttle: float = 0.0,
    authorization: CorpusWriteAuthorization | None = None,
) -> dict[str, Any]:
    cookie_jar = cookie_jar.resolve()
    output_root = output_root.resolve()
    discovery = discover_chatgpt_local_session(cookie_jar)

    preflight = scope_preflight_check(discovery["conversation_count"], output_root)

    prior_state = load_prior_acquisition(output_root)
    bundle = fetch_chatgpt_local_session_bundle(
        cookie_jar,
        limit=limit,
        offset=offset,
        prior_state=prior_state,
        output_root=output_root,
    )

    with tempfile.TemporaryDirectory(prefix="chatgpt-local-session-") as tmpdir:
        bundle_root = Path(tmpdir) / "chatgpt-local-bundle"
        write_local_session_bundle(bundle_root, bundle)
        result = import_chatgpt_export_corpus(
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
                "cookie_jar": str(cookie_jar),
                "account_id": discovery.get("account_id"),
                "account_email": discovery.get("account_email"),
                "detail_failure_count": len(bundle.get("conversation_detail_failures") or []),
                "fetched_count": bundle.get("fetched_count", 0),
                "reused_count": bundle.get("reused_count", 0),
                "total_count": bundle.get("total_count", 0),
                "acquisition_report": bundle.get("acquisition_report"),
            },
        )

    new_state: dict[str, dict[str, Any]] = {}
    for conv in bundle.get("conversations") or []:
        cid = conv.get("conversation_id") or ""
        if cid:
            new_state[cid] = {
                "update_time": conv.get("update_time"),
                "fetched_at": bundle.get("generated_at") or now_iso(),
            }
    save_acquisition_state(
        output_root,
        new_state,
        report=bundle.get("acquisition_report") or {},
    )

    patch_contract_for_local_session(output_root, cookie_jar=cookie_jar, discovery=discovery)
    rewrite_readme_for_local_session(output_root, cookie_jar=cookie_jar, bundle=bundle)

    result["source_type"] = "chatgpt-local-session"
    result["cookie_jar"] = str(cookie_jar)
    result["discovery_path"] = str(output_root / "source" / "local-session-discovery.json")
    result["detail_failure_count"] = len(bundle.get("conversation_detail_failures") or [])
    result["acquisition_report"] = bundle.get("acquisition_report")
    result["scope_preflight"] = preflight
    return result
