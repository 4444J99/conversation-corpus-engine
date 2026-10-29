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
    dedupe_preserve,
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

DEFAULT_OUTPUT_ROOT = Path.cwd() / "claude-history-memory"
CONTRACT_NAME = "conversation-corpus-engine.v2"
CONTRACT_VERSION = 2
REQUIRED_BUNDLE_FILES = ("conversations.json", "users.json")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import a Claude JSON export bundle into a federation-compatible memory corpus."
    )
    parser.add_argument("input_path", type=Path)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--name")
    parser.add_argument("--corpus-id")
    parser.add_argument("--json", action="store_true")
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
                f"Expected a Claude export directory or conversations.json file, got {input_path}"
            )
    else:
        bundle_root = input_path
    missing = [name for name in REQUIRED_BUNDLE_FILES if not bundle_file_exists(bundle_root, name)]
    if missing:
        raise FileNotFoundError(
            f"Claude export bundle at {bundle_root} is missing required files: {', '.join(missing)}"
        )
    return bundle_root


def discover_bundle_roots(input_path: Path) -> list[Path]:
    """Resolve one or more Claude export bundle roots from input_path.

    Mirrors the ChatGPT adapter's discovery contract (see
    import_chatgpt_export_corpus.discover_bundle_roots) for split exports:

    - File input pointing to a conversations.json: returns [parent].
    - Directory containing conversations.json + users.json: returns [dir] (single).
    - Directory whose immediate subdirectories each contain a valid bundle:
      returns the sorted list of subdirectories (multi-part split exports).
    """
    input_path = input_path.resolve()
    if input_path.is_file():
        if input_path.name == "conversations.json":
            return [input_path.parent]
        raise ValueError(
            f"Expected a Claude export directory or conversations.json file, got {input_path}"
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
        f"Claude export bundle at {input_path} is missing required files: "
        f"{', '.join(missing)} (no multi-part subdirectories with valid bundles found either)"
    )


def infer_title(conversation: dict[str, Any], fallback_index: int) -> str:
    for candidate in (
        conversation.get("name"),
        conversation.get("summary"),
    ):
        if isinstance(candidate, str) and normalize_whitespace(candidate):
            return normalize_whitespace(candidate)
    messages = conversation.get("chat_messages") or []
    for message in messages:
        extracted = extract_message_text(message)
        if extracted:
            return shorten(extracted, 120)
    return f"Claude Conversation {fallback_index:04d}"


def summarize_tool_use(item: dict[str, Any]) -> str:
    name = item.get("name") or "tool"
    payload = item.get("input") or {}
    title = payload.get("title") or payload.get("id") or payload.get("type")
    if title:
        return f"[tool_use:{name}:{title}]"
    return f"[tool_use:{name}]"


def summarize_tool_result(item: dict[str, Any]) -> str:
    name = item.get("name") or "tool"
    text_items = [
        value for value in collect_text_segments(item.get("content")) if value.upper() != "OK"
    ]
    if text_items:
        return shorten(f"[tool_result:{name}] " + " ".join(text_items), 200)
    if item.get("is_error"):
        return f"[tool_result:{name}:error]"
    return f"[tool_result:{name}]"


def collect_text_segments(payload: Any) -> list[str]:
    segments: list[str] = []
    if isinstance(payload, str):
        cleaned = normalize_whitespace(payload)
        if cleaned:
            segments.append(cleaned)
        return segments
    if isinstance(payload, list):
        for item in payload:
            segments.extend(collect_text_segments(item))
        return segments
    if not isinstance(payload, dict):
        return segments
    text_value = payload.get("text")
    if isinstance(text_value, str):
        cleaned = normalize_whitespace(text_value)
        if cleaned:
            segments.append(cleaned)
    for key in ("content", "value", "output", "stdout", "stderr"):
        if key in payload:
            segments.extend(collect_text_segments(payload.get(key)))
    return dedupe_preserve(segments)


def summarize_execution_output(payload: Any, *, prefix: str) -> str:
    text = normalize_whitespace(" ".join(collect_text_segments(payload)))
    if len(text) > 500:
        text = text[:497] + "..."
    if text:
        return f"[{prefix}: {text}]"
    return f"[{prefix}]"


def summarize_code_item(item: dict[str, Any]) -> str:
    language = item.get("language") or item.get("mime_type") or "text"
    raw_text = ""
    for candidate in (item.get("text"), item.get("content"), item.get("code"), item.get("value")):
        if isinstance(candidate, str) and candidate.strip():
            raw_text = candidate
            break
    if not raw_text:
        raw_text = "\n".join(collect_text_segments(item))
    if raw_text.strip():
        return f"```{language}\n{raw_text.strip()}\n```"
    return f"[code:{language}]"


def summarize_media_item(item: dict[str, Any]) -> str:
    name = item.get("file_name") or item.get("name") or item.get("title")
    width = item.get("width")
    height = item.get("height")
    size = f"{width}x{height}" if width and height else ""
    descriptor = name or size or item.get("mime_type") or "image"
    return f"[Image: {descriptor}]"


def extract_message_text(message: dict[str, Any]) -> str:
    segments: list[str] = []
    raw_text = normalize_whitespace(message.get("text") or "")
    if raw_text:
        segments.append(raw_text)
    for item in message.get("content") or []:
        item_type = (item.get("type") or "").lower()
        if item_type == "text":
            value = normalize_whitespace(item.get("text") or "")
            if value and value not in segments:
                segments.append(value)
        elif item_type == "code":
            segments.append(summarize_code_item(item))
        elif item_type == "tool_use":
            segments.append(summarize_tool_use(item))
        elif item_type == "tool_result":
            segments.append(summarize_tool_result(item))
        elif item_type in {"execution_output", "tool_output"}:
            segments.append(summarize_execution_output(item, prefix="Execution output"))
        elif item_type in {"image", "image_asset", "image_pointer"}:
            segments.append(summarize_media_item(item))
        elif item_type in {"attachment", "document", "file"}:
            name = item.get("file_name") or item.get("name") or item.get("title")
            if name:
                segments.append(f"[file:{name}]")
        else:
            for value in collect_text_segments(item):
                if value and value not in segments:
                    segments.append(value)
    for attachment in message.get("attachments") or []:
        name = attachment.get("file_name") or attachment.get("name")
        if name:
            segments.append(f"[attachment:{name}]")
    for file_entry in message.get("files") or []:
        name = file_entry.get("file_name") or file_entry.get("name")
        if name:
            segments.append(f"[file:{name}]")
    return normalize_whitespace(" ".join(segment for segment in segments if segment))


def timestamp_for_message(message: dict[str, Any]) -> str:
    return message.get("created_at") or message.get("updated_at") or now_iso()


def sorted_messages(conversation: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(conversation.get("chat_messages") or [], key=timestamp_for_message)


def build_pairs(
    messages: list[dict[str, Any]], thread_uid: str, family_id: str
) -> list[dict[str, Any]]:
    turns: list[tuple[str, str, str]] = []
    for message in messages:
        sender = (message.get("sender") or "").lower()
        text = extract_message_text(message)
        if not text:
            continue
        turns.append((PROMPT if sender == "human" else RESPONSE, text, ""))
    return build_pairs_from_turns(
        turns,
        thread_uid,
        family_id,
        label="Claude",
        make_pair_id=lambda index, _key: f"{thread_uid}-pair-{index:03d}",
    )


def build_thread_audit(
    messages: list[dict[str, Any]],
    extracted_texts: list[str],
    pairs: list[dict[str, Any]],
) -> dict[str, Any]:
    raw_sender_counts: Counter[str] = Counter()
    raw_content_type_counts: Counter[str] = Counter()
    skipped_count = 0
    empty_render_count = 0

    for message in messages:
        sender = (message.get("sender") or "none").lower()
        raw_sender_counts[sender] += 1
        raw_text = normalize_whitespace(message.get("text") or "")
        if raw_text:
            raw_content_type_counts["text_field"] += 1
        content_items = message.get("content") or []
        if content_items:
            for item in content_items:
                raw_content_type_counts[(item.get("type") or "none").lower()] += 1
        elif not raw_text:
            raw_content_type_counts["none"] += 1

        text = extract_message_text(message)
        if not text:
            skipped_count += 1
            if content_items or raw_text:
                empty_render_count += 1

    retained = len(extracted_texts)
    retention_ratio = round(retained / len(messages), 4) if messages else 0.0

    flags: list[str] = []
    if retention_ratio < 0.65 and len(messages) > 4:
        flags.append("low_retention")
    if empty_render_count > 5:
        flags.append("high_empty_render")
    if any(not pair.get("summary", "").strip() for pair in pairs):
        flags.append("pair_without_response")

    return {
        "message_count": len(messages),
        "retained_count": retained,
        "skipped_count": skipped_count,
        "empty_render_count": empty_render_count,
        "retention_ratio": retention_ratio,
        "raw_sender_counts": dict(raw_sender_counts),
        "raw_content_type_counts": dict(raw_content_type_counts),
        "path_sender_counts": dict(raw_sender_counts),
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
            str(record.get("uuid") or record.get("id"))
            if isinstance(record, dict) and (record.get("uuid") or record.get("id"))
            else content_hash_key(record)
        ),
        authorization=authorization,
    )
    copied.append(str(collection_storage_path(conversations_path)))
    return copied


def import_claude_export_corpus(
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
    # member ids. The legacy hand-built shaping was removed, not kept beside
    # the projector; only the importer's envelope (sources, manifest, README,
    # result) remains here.
    projected = project_export_bundle(
        "claude", bundle_roots, output_root, authorization=authorization
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
    # Projects, memories and users are account-scoped observations from the
    # primary bundle; the projector counts them as excluded kinds instead.
    projects = load_json_file(bundle_root / "projects.json", default=[])
    memories = load_json_file(bundle_root / "memories.json", default=[])
    users = load_json_file(bundle_root / "users.json", default=[])
    write_markdown(
        output_root / "README.md",
        "\n".join(
            [
                "# Claude History Memory Corpus",
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
                f"- Projects observed: {len(projects)}",
                f"- Memories observed: {len(memories)}",
                f"- Users observed: {len(users)}",
                f"- Copied source files: {len(copied_sources)}",
                f"- Contract manifest: {output_root / 'corpus' / 'contract.json'}",
                "",
                "Corpus shaped by the archive projector (corpus-claude-archive): "
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
    result = import_claude_export_corpus(
        args.input_path,
        args.output_root,
        corpus_id=args.corpus_id,
        name=args.name,
    )
    print(json.dumps(result, indent=2) if args.json else json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
