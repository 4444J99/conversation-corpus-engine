#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .answering import slugify, write_markdown
from .corpus_text import (
    PROMPT,
    RESPONSE,
    build_pairs_from_turns,
    normalize_whitespace,
    shorten,
)
from .export_projector import project_export_bundle, projected_rows
from .sharded_collection import (
    collection_exists,
    collection_storage_path,
    content_hash_key,
    load_collection,
    write_collection,
    write_corpus_collection,
)

if TYPE_CHECKING:
    from .corpus_store import CorpusWriteAuthorization

DEFAULT_OUTPUT_ROOT = Path.cwd() / "chatgpt-history-memory"
CONTRACT_NAME = "conversation-corpus-engine.v2"
CONTRACT_VERSION = 2
REQUIRED_BUNDLE_FILES = ("conversations.json", "user.json")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def epoch_to_iso(epoch: float | None) -> str:
    if epoch is None:
        return now_iso()
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import a ChatGPT JSON export bundle into a federation-compatible memory corpus."
    )
    parser.add_argument("input_path", type=Path)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--name")
    parser.add_argument("--corpus-id")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--throttle",
        type=float,
        default=0.0,
        help="CPU throttle for background runs: time.sleep(THROTTLE) every 50 iterations during near-duplicate detection (default: 0.0).",
    )
    return parser.parse_args()


def load_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def bundle_file_exists(bundle_root: Path, name: str) -> bool:
    path = bundle_root / name
    return collection_exists(path) if name == "conversations.json" else path.exists()


def resolve_bundle_root(input_path: Path) -> Path:
    input_path = input_path.resolve()
    if input_path.is_file():
        if input_path.name == "conversations.json":
            bundle_root = input_path.parent
        else:
            raise ValueError(
                f"Expected a ChatGPT export directory or conversations.json file, got {input_path}"
            )
    else:
        bundle_root = input_path
    missing = [name for name in REQUIRED_BUNDLE_FILES if not bundle_file_exists(bundle_root, name)]
    if missing:
        raise FileNotFoundError(
            f"ChatGPT export bundle at {bundle_root} is missing required files: {', '.join(missing)}"
        )
    return bundle_root


def discover_bundle_roots(input_path: Path) -> list[Path]:
    """Resolve one or more ChatGPT export bundle roots from input_path.

    - File input pointing to a conversations.json: returns [parent].
    - Directory containing conversations.json + user.json: returns [dir] (single).
    - Directory whose immediate subdirectories each contain a valid bundle:
      returns the sorted list of subdirectories (multi-part split exports).

    Single-bundle behavior matches resolve_bundle_root; multi-part is the new path.
    """
    input_path = input_path.resolve()
    if input_path.is_file():
        if input_path.name == "conversations.json":
            return [input_path.parent]
        raise ValueError(
            f"Expected a ChatGPT export directory or conversations.json file, got {input_path}"
        )
    if all(bundle_file_exists(input_path, name) for name in REQUIRED_BUNDLE_FILES):
        return [input_path]
    parts = sorted(
        child
        for child in input_path.iterdir()
        if child.is_dir() and all(bundle_file_exists(child, name) for name in REQUIRED_BUNDLE_FILES)
    )
    if parts:
        return parts
    missing = [name for name in REQUIRED_BUNDLE_FILES if not bundle_file_exists(input_path, name)]
    raise FileNotFoundError(
        f"ChatGPT export bundle at {input_path} is missing required files: "
        f"{', '.join(missing)} (no multi-part subdirectories with valid bundles found either)"
    )


SKIP_CONTENT_TYPES = {"user_editable_context", "model_editable_context", "thoughts"}


def extract_node_text(node: dict[str, Any]) -> str:
    """Extract text from a ChatGPT mapping node's message content.

    Handles text, code, execution_output, multimodal_text, and tool content types.
    """
    message = node.get("message")
    if not message:
        return ""
    content = message.get("content") or {}
    content_type = content.get("content_type") or "text"
    role = ((message.get("author") or {}).get("role") or "").lower()

    if content_type in SKIP_CONTENT_TYPES:
        return ""

    parts = content.get("parts") or []
    segments: list[str] = []

    if content_type == "multimodal_text":
        for part in parts:
            if isinstance(part, str):
                cleaned = normalize_whitespace(part)
                if cleaned:
                    segments.append(cleaned)
            elif isinstance(part, dict):
                part_type = part.get("content_type", "")
                if part_type == "text":
                    cleaned = normalize_whitespace(str(part.get("text", "")))
                    if cleaned:
                        segments.append(cleaned)
                elif part_type == "image_asset_pointer":
                    w, h = part.get("width"), part.get("height")
                    size = f"{w}x{h}" if w and h else "unknown"
                    segments.append(f"[Image: {size}]")
    elif content_type == "code":
        raw_text = content.get("text") or " ".join(p for p in parts if isinstance(p, str))
        lang = content.get("language") or "text"
        if raw_text.strip():
            segments.append(f"```{lang}\n{raw_text.strip()}\n```")
    elif content_type == "execution_output":
        raw_text = content.get("text") or " ".join(p for p in parts if isinstance(p, str))
        if raw_text.strip():
            text = raw_text.strip()
            if len(text) > 500:
                text = text[:497] + "..."
            segments.append(f"[Execution output: {text}]")
    else:
        for part in parts:
            if isinstance(part, str):
                cleaned = normalize_whitespace(part)
                if cleaned:
                    segments.append(cleaned)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                cleaned = normalize_whitespace(part["text"])
                if cleaned:
                    segments.append(cleaned)

    if role == "tool" and not segments:
        text_field = content.get("text")
        if isinstance(text_field, str) and text_field.strip():
            text = text_field.strip()
            if len(text) > 500:
                text = text[:497] + "..."
            segments.append(f"[Tool output: {text}]")

    return normalize_whitespace(" ".join(segments))


def walk_mapping_tree(mapping: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk the ChatGPT mapping tree in order, returning non-system message nodes.

    Iterative DFS — long conversations create deep linear chains that overflow
    Python's default recursion limit. ChatGPT exports with multi-thousand-message
    threads were crashing with RecursionError before this change.
    """
    roots = [node_id for node_id, node in mapping.items() if not node.get("parent")]

    ordered: list[dict[str, Any]] = []

    for root_id in roots:
        stack = [root_id]
        while stack:
            node_id = stack.pop()
            node = mapping.get(node_id)
            if node is None:
                continue
            message = node.get("message")
            if message is not None:
                role = (message.get("author") or {}).get("role", "")
                if role != "system":
                    ordered.append(node)
            children = node.get("children") or []
            for child_id in reversed(children):
                stack.append(child_id)

    # Sort by create_time if available to ensure proper order
    def node_create_time(node: dict[str, Any]) -> float:
        message = node.get("message") or {}
        return message.get("create_time") or 0.0

    ordered.sort(key=node_create_time)
    return ordered


def infer_title_chatgpt(
    conversation: dict[str, Any], nodes: list[dict[str, Any]], fallback_index: int
) -> str:
    title = conversation.get("title")
    if isinstance(title, str) and normalize_whitespace(title):
        return normalize_whitespace(title)
    # Fall back to first user message text
    for node in nodes:
        message = node.get("message") or {}
        role = (message.get("author") or {}).get("role", "")
        if role == "user":
            text = extract_node_text(node)
            if text:
                return shorten(text, 120)
    return f"ChatGPT Conversation {fallback_index:04d}"


def build_pairs_chatgpt(
    nodes: list[dict[str, Any]], thread_uid: str, family_id: str
) -> list[dict[str, Any]]:
    turns: list[tuple[str, str, str]] = []
    for node in nodes:
        message = node.get("message") or {}
        role = (message.get("author") or {}).get("role", "").lower()
        text = extract_node_text(node)
        if not text:
            continue
        if role == "user":
            turns.append((PROMPT, text, ""))
        elif role == "assistant":
            turns.append((RESPONSE, text, ""))
    return build_pairs_from_turns(
        turns,
        thread_uid,
        family_id,
        label="ChatGPT",
        make_pair_id=lambda index, _key: f"{thread_uid}-pair-{index:03d}",
    )


def build_thread_audit(
    mapping: dict[str, Any],
    nodes: list[dict[str, Any]],
    extracted_texts: list[str],
    pairs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build per-thread audit statistics: role/content_type counts, retention, quality flags."""
    raw_role_counts: Counter[str] = Counter()
    raw_content_type_counts: Counter[str] = Counter()
    for node in mapping.values():
        message = node.get("message") or {}
        role = ((message.get("author") or {}).get("role")) or "none"
        ct = (message.get("content") or {}).get("content_type") or "none"
        raw_role_counts[role] += 1
        raw_content_type_counts[ct] += 1

    path_role_counts: Counter[str] = Counter()
    skipped_count = 0
    empty_render_count = 0
    for node in nodes:
        message = node.get("message") or {}
        role = ((message.get("author") or {}).get("role")) or "none"
        path_role_counts[role] += 1
        text = extract_node_text(node)
        if not text:
            skipped_count += 1
            ct = (message.get("content") or {}).get("content_type") or "none"
            if ct not in SKIP_CONTENT_TYPES and role != "system":
                empty_render_count += 1

    retained = len(extracted_texts)
    retention_ratio = round(retained / len(nodes), 4) if nodes else 0.0

    flags: list[str] = []
    if retention_ratio < 0.65 and len(nodes) > 4:
        flags.append("low_retention")
    if empty_render_count > 5:
        flags.append("high_empty_render")
    if any(not p.get("summary", "").strip() for p in pairs):
        flags.append("pair_without_response")

    return {
        "mapping_node_count": len(mapping),
        "path_node_count": len(nodes),
        "retained_count": retained,
        "skipped_count": skipped_count,
        "empty_render_count": empty_render_count,
        "retention_ratio": retention_ratio,
        "raw_role_counts": dict(raw_role_counts),
        "raw_content_type_counts": dict(raw_content_type_counts),
        "path_role_counts": dict(path_role_counts),
        "quality_flags": flags,
    }


def copy_bundle_files(
    bundle_root: Path,
    output_root: Path,
    *,
    prefix: str | None = None,
    authorization: CorpusWriteAuthorization | None = None,
) -> list[str]:
    copied: list[str] = []
    source_root = output_root / "source"
    if prefix:
        source_root = source_root / prefix
    source_root.mkdir(parents=True, exist_ok=True)
    for path in sorted(bundle_root.rglob("*.json")):
        if (
            path == bundle_root / "conversations.json"
            or collection_storage_path(bundle_root / "conversations.json") in path.parents
        ):
            continue
        relative = path.relative_to(bundle_root)
        destination = source_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        copied.append(str(destination))
    conversations_path = source_root / "conversations.json"
    write_collection(
        conversations_path,
        load_collection(bundle_root / "conversations.json", default=[]),
        key=lambda record: (
            str(record.get("conversation_id") or record.get("id"))
            if isinstance(record, dict) and (record.get("conversation_id") or record.get("id"))
            else content_hash_key(record)
        ),
        authorization=authorization,
    )
    copied.append(str(collection_storage_path(conversations_path)))
    return copied


def import_chatgpt_export_corpus(
    input_path: Path,
    output_root: Path,
    *,
    corpus_id: str | None = None,
    name: str | None = None,
    throttle: float = 0.0,
    authorization: CorpusWriteAuthorization | None = None,
) -> dict[str, Any]:
    bundle_roots = discover_bundle_roots(input_path)
    bundle_root = bundle_roots[0]  # primary — used for the README source input
    is_multi_part = len(bundle_roots) > 1

    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "corpus").mkdir(parents=True, exist_ok=True)

    copied_sources: list[str] = []
    for part_root in bundle_roots:
        part_prefix = part_root.name if is_multi_part else None
        copied_sources.extend(
            copy_bundle_files(
                part_root,
                output_root,
                prefix=part_prefix,
                authorization=authorization,
            )
        )

    # The corpus shaping lives in the archive projector now (#78): the bundle
    # is ingested into a temporary normalized store and projected, so this
    # importer and `cce archive project corpus` write identical contracts and
    # member ids. The legacy hand-built shaping below was removed, not kept
    # beside the projector; only the importer's envelope (sources, manifest,
    # README, result) remains here.
    projected = project_export_bundle(
        "chatgpt", bundle_roots, output_root, authorization=authorization
    )
    rows = projected_rows(projected["corpus_dir"])
    threads = rows["threads-index.json"]
    pairs = rows["pairs-index.json"]
    actions = rows["action-ledger.json"]
    unresolved = rows["unresolved-ledger.json"]
    pair_counts: Counter[str] = Counter(
        pair.get("thread_uid") for pair in pairs if pair.get("thread_uid")
    )
    import_manifest = [
        {
            "conversation_uuid": thread["conversation_id"],
            "thread_uid": thread["thread_uid"],
            "family_id": thread["family_ids"][0] if thread.get("family_ids") else None,
            "pair_count": pair_counts.get(thread["thread_uid"], 0),
            "title": thread["title_normalized"],
        }
        for thread in threads
    ]
    write_corpus_collection(
        output_root / "import-manifest.json",
        import_manifest,
        authorization=authorization,
    )
    write_markdown(
        output_root / "README.md",
        "\n".join(
            [
                "# ChatGPT History Memory Corpus",
                "",
                f"- Generated: {now_iso()}",
                f"- Source input: {bundle_root}"
                + (f" (+{len(bundle_roots) - 1} additional parts)" if is_multi_part else ""),
                f"- Bundle parts: {len(bundle_roots)}"
                + (f" ({', '.join(p.name for p in bundle_roots)})" if is_multi_part else ""),
                f"- Duplicate conversations skipped across parts: {projected['duplicate_conversations_skipped']}",
                f"- Imported conversations: {projected['conversations_seen']}",
                f"- Projected threads: {len(threads)}",
                f"- Pair count: {len(pairs)}",
                f"- Action count: {len(actions)}",
                f"- Unresolved count: {len(unresolved)}",
                f"- Copied source files: {len(copied_sources)}",
                f"- Contract manifest: {output_root / 'corpus' / 'contract.json'}",
                "",
                "Corpus shaped by the archive projector (corpus-chatgpt-archive): "
                "contracts and member ids match `cce archive project corpus` over "
                "the same export. This corpus is federation-compatible but "
                "unevaluated. Its regression gate state is intentionally `warn` "
                "until manual fixtures are added.",
            ],
        ),
    )

    resolved_corpus_id = corpus_id or output_root.name
    return {
        "corpus_id": slugify(resolved_corpus_id),
        "name": name or output_root.name,
        "output_root": str(output_root),
        "thread_count": len(threads),
        "pair_count": len(pairs),
        "action_count": len(actions),
        "unresolved_count": len(unresolved),
        "empty_conversation_count": projected["excluded"].get("no-text", 0),
        "near_duplicate_count": 0,
        "flagged_thread_count": 0,
        "manifest_path": str(collection_storage_path(output_root / "import-manifest.json")),
        "readme_path": str(output_root / "README.md"),
        "bundle_part_count": len(bundle_roots),
        "bundle_part_names": [p.name for p in bundle_roots],
        "duplicate_conversations_skipped": projected["duplicate_conversations_skipped"],
        "projector_corpus_id": projected["contract"].get("corpus_id"),
        "projector_adapter_type": projected["contract"].get("adapter_type"),
        "projector_excluded": projected["excluded"],
        "conversations_seen": projected["conversations_seen"],
    }


def main() -> int:
    args = parse_args()
    result = import_chatgpt_export_corpus(
        args.input_path,
        args.output_root,
        corpus_id=args.corpus_id,
        name=args.name,
        throttle=args.throttle,
    )
    print(json.dumps(result, indent=2) if args.json else json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
