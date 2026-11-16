#!/usr/bin/env python3
"""Import a Perplexity local-session corpus.

Mirrors import_chatgpt_local_session_corpus.py: discovers the Perplexity
desktop app session via the binary cookie jar, fetches threads through the
REST API, renders each normalized thread as a markdown document, then
delegates to the existing document-export import adapter (Perplexity's native
`document-export` discovery mode) for corpus generation and federation.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from .answering import load_json, slugify, write_json, write_markdown
from .import_document_export_corpus import import_document_export_corpus
from .perplexity_local_session import (
    discover_perplexity_local_session,
    fetch_perplexity_local_session_bundle,
    now_iso,
)
from .source_lifecycle import build_source_snapshot

DEFAULT_OUTPUT_ROOT = Path.cwd() / "perplexity-local-session-memory"
DEFAULT_CORPUS_ID = "perplexity-local-session-memory"
DEFAULT_NAME = "Perplexity Local Session Memory"


def render_thread_markdown(thread: dict[str, Any]) -> str:
    """Render one normalized thread record as a conversation markdown document."""
    title = (
        thread.get("title_normalized") or thread.get("title_raw") or "Untitled Perplexity Thread"
    )
    lines = [f"# {title}", ""]
    url = thread.get("url")
    if url:
        lines.extend([f"Source: {url}", ""])
    for message in thread.get("messages") or []:
        role = message.get("role", "")
        text = (message.get("text") or "").strip()
        if not text:
            continue
        label = "User" if role == "user" else "Assistant"
        lines.extend([f"## {label}", "", text, ""])
    return "\n".join(lines).rstrip() + "\n"


def write_thread_markdown_bundle(bundle_root: Path, bundle: dict[str, Any]) -> int:
    """Write each thread as a markdown file under ``bundle_root``. Returns count."""
    bundle_root.mkdir(parents=True, exist_ok=True)
    written = 0
    seen: dict[str, int] = {}
    for thread in bundle.get("threads") or []:
        if not (thread.get("messages") or []):
            continue
        uuid = thread.get("thread_uid") or ""
        title = thread.get("title_normalized") or thread.get("title_raw") or uuid
        slug = slugify(title, limit=80) or (uuid[:8] if uuid else "thread")
        # Disambiguate collisions deterministically (uuid suffix).
        base = f"{slug}--{uuid[:8]}" if uuid else slug
        if base in seen:
            seen[base] += 1
            base = f"{base}-{seen[base]}"
        else:
            seen[base] = 0
        (bundle_root / f"{base}.md").write_text(render_thread_markdown(thread), encoding="utf-8")
        written += 1
    return written


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
        cookie_jar.parent, "perplexity-local-session", "local-session"
    )
    contract.update(
        {
            "adapter_type": "perplexity-local-session",
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
                "account_username": discovery.get("account_username"),
                "conversation_count": discovery.get("conversation_count"),
            },
        },
    )
    write_json(corpus_dir / "source-snapshot.json", source_snapshot)
    write_json(contract_path, contract)

    evaluation_summary = load_json(corpus_dir / "evaluation-summary.json", default={}) or {}
    evaluation_summary["notes"] = [
        "Imported Perplexity local-session corpus has not been manually evaluated."
    ]
    write_json(corpus_dir / "evaluation-summary.json", evaluation_summary)

    regression_gates = load_json(corpus_dir / "regression-gates.json", default={}) or {}
    regression_gates["source_notes"] = [
        "Imported Perplexity local-session corpus has not been manually evaluated."
    ]
    write_json(corpus_dir / "regression-gates.json", regression_gates)


def rewrite_readme_for_local_session(
    output_root: Path, *, cookie_jar: Path, bundle: dict[str, Any]
) -> None:
    detail_failures = bundle.get("thread_detail_failures") or []
    write_markdown(
        output_root / "README.md",
        "\n".join(
            [
                "# Perplexity Local Session Memory Corpus",
                "",
                f"- Generated: {now_iso()}",
                f"- Source input: {cookie_jar}",
                "- Adapter type: perplexity-local-session",
                f"- Account: {bundle.get('account_email') or 'unknown'}",
                f"- Imported threads: {len(bundle.get('threads') or [])}",
                f"- Detail fetch failures: {len(detail_failures)}",
                f"- Total available: {bundle.get('total_count', '?')}",
                f"- Contract manifest: {output_root / 'corpus' / 'contract.json'}",
                "",
                "This corpus was imported from the local signed-in Perplexity desktop session.",
            ],
        ),
    )


def import_perplexity_local_session_corpus(
    cookie_jar: Path,
    output_root: Path,
    *,
    corpus_id: str = DEFAULT_CORPUS_ID,
    name: str = DEFAULT_NAME,
    limit: int = 50,
    throttle: float = 0.0,
) -> dict[str, Any]:
    cookie_jar = cookie_jar.resolve()
    output_root = output_root.resolve()
    discovery = discover_perplexity_local_session(cookie_jar)
    bundle = fetch_perplexity_local_session_bundle(cookie_jar, limit=limit)

    with tempfile.TemporaryDirectory(prefix="perplexity-local-session-") as tmpdir:
        bundle_root = Path(tmpdir) / "perplexity-local-bundle"
        thread_count = write_thread_markdown_bundle(bundle_root, bundle)
        if thread_count == 0:
            raise FileNotFoundError(
                "Perplexity local session did not yield any threads with content to import."
            )
        result = import_document_export_corpus(
            bundle_root,
            output_root,
            provider_slug="perplexity",
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
                "cookie_jar": str(cookie_jar),
                "account_id": discovery.get("account_id"),
                "account_email": discovery.get("account_email"),
                "account_username": discovery.get("account_username"),
                "detail_failure_count": len(bundle.get("thread_detail_failures") or []),
                "fetched_count": bundle.get("fetched_count", 0),
                "total_count": bundle.get("total_count", 0),
                "acquisition_report": bundle.get("acquisition_report"),
            },
        )

    patch_contract_for_local_session(output_root, cookie_jar=cookie_jar, discovery=discovery)
    rewrite_readme_for_local_session(output_root, cookie_jar=cookie_jar, bundle=bundle)

    result["source_type"] = "perplexity-local-session"
    result["cookie_jar"] = str(cookie_jar)
    result["discovery_path"] = str(output_root / "source" / "local-session-discovery.json")
    result["detail_failure_count"] = len(bundle.get("thread_detail_failures") or [])
    result["acquisition_report"] = bundle.get("acquisition_report")
    return result
