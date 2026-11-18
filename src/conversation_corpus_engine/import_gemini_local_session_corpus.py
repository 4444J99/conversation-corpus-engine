#!/usr/bin/env python3
"""Import a Gemini local-session corpus.

Discovers the Gemini local session (local app cache full-content path and/or
Chrome cookie session), fetches conversations, writes a bundle compatible with
the document-export import adapter, then delegates to import_document_export_corpus.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from .answering import load_json, slugify, write_json, write_markdown
from .gemini_local_session import (
    DEFAULT_GEMINI_STORE,
    discover_gemini_local_session,
    fetch_gemini_local_session_bundle,
    now_iso,
)
from .import_document_export_corpus import import_document_export_corpus
from .source_lifecycle import build_source_snapshot

DEFAULT_OUTPUT_ROOT = Path.cwd() / "gemini-local-session-memory"
DEFAULT_CORPUS_ID = "gemini-local-session-memory"
DEFAULT_NAME = "Gemini Local Session Memory"


def render_conversation_markdown(conversation: dict[str, Any]) -> str:
    title = conversation.get("title") or "Untitled Gemini Conversation"
    lines = [f"# {title}", ""]
    conv_id = conversation.get("conversation_id") or conversation.get("uuid") or ""
    if conv_id:
        lines.extend([f"Conversation: {conv_id}", ""])
    for message in conversation.get("messages") or []:
        role = message.get("role", "")
        text = (message.get("text") or "").strip()
        if not text:
            continue
        label = "User" if role == "user" else "Assistant"
        lines.extend([f"## {label}", "", text, ""])
    return "\n".join(lines).rstrip() + "\n"


def write_conversation_markdown_bundle(bundle_root: Path, bundle: dict[str, Any]) -> int:
    bundle_root.mkdir(parents=True, exist_ok=True)
    written = 0
    seen: dict[str, int] = {}
    for conv in bundle.get("conversations") or []:
        if not (conv.get("messages") or []):
            continue
        cid = conv.get("conversation_id") or conv.get("uuid") or ""
        title = conv.get("title") or cid or "conversation"
        slug = slugify(title, limit=80) or (cid[:8] if cid else "conversation")
        base = f"{slug}--{cid[:8]}" if cid else slug
        if base in seen:
            seen[base] += 1
            base = f"{base}-{seen[base]}"
        else:
            seen[base] = 0
        (bundle_root / f"{base}.md").write_text(render_conversation_markdown(conv), encoding="utf-8")
        written += 1
    return written


def patch_contract_for_local_session(
    output_root: Path,
    *,
    source: Path,
    discovery: dict[str, Any],
) -> None:
    corpus_dir = output_root / "corpus"
    contract_path = corpus_dir / "contract.json"
    contract = load_json(contract_path, default={}) or {}
    snapshot_root = source if source.is_dir() else source.parent
    source_snapshot = build_source_snapshot(snapshot_root, "gemini-local-session", "local-session")
    contract.update(
        {
            "adapter_type": "gemini-local-session",
            "source_input": str(source),
            "collection_scope": "local-session",
            "source_snapshot_path": "corpus/source-snapshot.json",
            "source_signature_fingerprint": source_snapshot.get("signature_fingerprint"),
            "source_content_fingerprint": source_snapshot.get("content_fingerprint"),
            "source_file_count": source_snapshot.get("file_count"),
            "source_total_bytes": source_snapshot.get("total_bytes"),
            "source_latest_mtime_ns": source_snapshot.get("latest_mtime_ns"),
            "local_session": {
                "discovered_at": discovery.get("generated_at") or now_iso(),
                "conversation_count": discovery.get("conversation_count"),
                "conversations_with_content": discovery.get("conversations_with_content"),
                "note": discovery.get("note"),
            },
        },
    )
    write_json(corpus_dir / "source-snapshot.json", source_snapshot)
    write_json(contract_path, contract)

    evaluation_summary = load_json(corpus_dir / "evaluation-summary.json", default={}) or {}
    evaluation_summary["notes"] = [
        "Imported Gemini local-session corpus has not been manually evaluated."
    ]
    write_json(corpus_dir / "evaluation-summary.json", evaluation_summary)

    regression_gates = load_json(corpus_dir / "regression-gates.json", default={}) or {}
    regression_gates["source_notes"] = [
        "Imported Gemini local-session corpus has not been manually evaluated."
    ]
    write_json(corpus_dir / "regression-gates.json", regression_gates)


def rewrite_readme_for_local_session(
    output_root: Path, *, source: Path, bundle: dict[str, Any]
) -> None:
    detail_failures = bundle.get("conversation_detail_failures") or []
    write_markdown(
        output_root / "README.md",
        "\n".join(
            [
                "# Gemini Local Session Memory Corpus",
                "",
                f"- Generated: {now_iso()}",
                f"- Source input: {source}",
                "- Adapter type: gemini-local-session",
                f"- Imported conversations: {len(bundle.get('conversations') or [])}",
                f"- Conversations with content: {bundle.get('conversations_with_content', 0)}",
                f"- Detail fetch failures: {len(detail_failures)}",
                f"- Total available: {bundle.get('total_count', '?')}",
                f"- Contract manifest: {output_root / 'corpus' / 'contract.json'}",
                "",
                "This corpus was imported from the local Gemini session/full-content path.",
            ],
        ),
    )


def import_gemini_local_session_corpus(
    source: Path,
    output_root: Path,
    *,
    corpus_id: str = DEFAULT_CORPUS_ID,
    name: str = DEFAULT_NAME,
    limit: int = 0,
    throttle: float = 0.0,
) -> dict[str, Any]:
    source = (source or DEFAULT_GEMINI_STORE).resolve()
    output_root = output_root.resolve()
    discovery = discover_gemini_local_session(source)
    bundle = fetch_gemini_local_session_bundle(source, limit=limit)

    with tempfile.TemporaryDirectory(prefix="gemini-local-session-") as tmpdir:
        bundle_root = Path(tmpdir) / "gemini-local-bundle"
        conv_count = write_conversation_markdown_bundle(bundle_root, bundle)
        if conv_count == 0:
            raise FileNotFoundError(
                "Gemini local session did not yield any conversations with content to import."
            )
        result = import_document_export_corpus(
            bundle_root,
            output_root,
            provider_slug="gemini",
            corpus_id=corpus_id,
            name=name,
            throttle=throttle,
        )
        source_root = output_root / "source"
        source_root.mkdir(parents=True, exist_ok=True)
        write_json(source_root / "local-session-discovery.json", discovery)
        write_json(
            source_root / "local-session-metadata.json",
            {
                "generated_at": bundle.get("generated_at") or now_iso(),
                "source": str(source),
                "detail_failure_count": len(bundle.get("conversation_detail_failures") or []),
                "fetched_count": bundle.get("fetched_count", 0),
                "total_count": bundle.get("total_count", 0),
                "acquisition_report": bundle.get("acquisition_report"),
                "conversations_with_content": bundle.get("conversations_with_content", 0),
            },
        )

    patch_contract_for_local_session(output_root, source=source, discovery=discovery)
    rewrite_readme_for_local_session(output_root, source=source, bundle=bundle)

    result["source_type"] = "gemini-local-session"
    result["source"] = str(source)
    result["discovery_path"] = str(output_root / "source" / "local-session-discovery.json")
    result["detail_failure_count"] = len(bundle.get("conversation_detail_failures") or [])
    result["acquisition_report"] = bundle.get("acquisition_report")
    return result
