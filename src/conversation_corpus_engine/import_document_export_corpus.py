#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import html
import json
import shutil
import sys
import tempfile
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .answering import load_json, slugify, write_json, write_markdown
from .corpus_store import git_visible_refusal
from .import_markdown_document_corpus import import_markdown_document_corpus
from .provider_catalog import get_provider_config
from .provider_exports import collect_supported_export_files
from .sharded_collection import collection_storage_path, write_corpus_collection
from .source_lifecycle import build_source_snapshot

DEFAULT_OUTPUT_ROOT = Path.cwd() / "document-export-memory"
CONTRACT_NAME = "conversation-corpus-engine.v2"
CONTRACT_VERSION = 2
SUPPORTED_PLAINTEXT_SUFFIXES = {".md", ".markdown", ".txt"}
SUPPORTED_HTML_SUFFIXES = {".html", ".htm"}
SUPPORTED_JSON_SUFFIXES = {".json"}
SUPPORTED_CSV_SUFFIXES = {".csv"}

if TYPE_CHECKING:
    from .corpus_store import CorpusWriteAuthorization


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import a document-style provider export into a corpus."
    )
    parser.add_argument("provider", choices=["gemini", "grok", "perplexity", "copilot"])
    parser.add_argument("input_path", type=Path)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--name")
    parser.add_argument("--corpus-id")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def display_name(value: str) -> str:
    return (
        " ".join(
            part.capitalize() for part in value.replace("-", " ").replace("_", " ").split()
        ).strip()
        or value
    )


def prepare_source_root(
    input_path: Path,
    *,
    provider_slug: str,
) -> tuple[Path, str | None, tempfile.TemporaryDirectory[str] | None]:
    input_path = input_path.resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"{provider_slug} export input does not exist: {input_path}")
    if input_path.is_file() and input_path.suffix.lower() == ".zip":
        tempdir = tempfile.TemporaryDirectory(prefix=f"{provider_slug}-export-")
        extract_root = Path(tempdir.name) / "extracted"
        extract_root.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(input_path) as archive:
            archive.extractall(extract_root)
        return extract_root, "zip", tempdir
    return input_path, None, None


def strip_html(raw_text: str) -> str:
    text = raw_text
    for start, end in (("<script", "</script>"), ("<style", "</style>")):
        lowered = text.lower()
        while True:
            start_index = lowered.find(start)
            if start_index < 0:
                break
            end_index = lowered.find(end, start_index)
            if end_index < 0:
                text = text[:start_index]
                break
            text = text[:start_index] + text[end_index + len(end) :]
            lowered = text.lower()
    for marker in (
        "</p>",
        "</div>",
        "</section>",
        "</article>",
        "<br>",
        "<br/>",
        "<br />",
        "</li>",
        "</tr>",
    ):
        text = text.replace(marker, f"{marker}\n")
    text = html.unescape(text)
    text = "".join("\n" if char == "\r" else char for char in text)
    stripped = []
    inside_tag = False
    for char in text:
        if char == "<":
            inside_tag = True
            continue
        if char == ">":
            inside_tag = False
            continue
        if not inside_tag:
            stripped.append(char)
    lines = [" ".join(line.split()) for line in "".join(stripped).splitlines()]
    return "\n".join(line for line in lines if line).strip()


def flatten_json(value: Any, *, prefix: str = "") -> list[str]:
    lines: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            next_prefix = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(item, (dict, list)):
                lines.extend(flatten_json(item, prefix=next_prefix))
            elif item not in (None, "", [], {}):
                lines.append(f"{next_prefix}: {item}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            next_prefix = f"{prefix}[{index}]" if prefix else f"[{index}]"
            if isinstance(item, (dict, list)):
                lines.extend(flatten_json(item, prefix=next_prefix))
            elif item not in (None, "", [], {}):
                lines.append(f"{next_prefix}: {item}")
    elif value not in (None, "", [], {}):
        lines.append(f"{prefix or 'value'}: {value}")
    return lines


def text_from_json(path: Path) -> str:
    raw_text = path.read_text(encoding="utf-8", errors="ignore")
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return raw_text.strip()
    return "\n".join(flatten_json(payload)).strip()


def text_from_csv(path: Path) -> str:
    rows: list[str] = []
    with path.open(encoding="utf-8", errors="ignore", newline="") as handle:
        reader = csv.reader(handle)
        for row in reader:
            cleaned = [item.strip() for item in row if item.strip()]
            if cleaned:
                rows.append(" | ".join(cleaned))
    return "\n".join(rows).strip()


def title_from_json(path: Path) -> str | None:
    raw_text = path.read_text(encoding="utf-8", errors="ignore")
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict):
        for key in ("title", "name", "query", "question", "prompt"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def render_markdown_document(source_file: Path) -> tuple[str, str]:
    suffix = source_file.suffix.lower()
    if suffix in SUPPORTED_PLAINTEXT_SUFFIXES:
        raw_text = source_file.read_text(encoding="utf-8", errors="ignore")
        if suffix in {".md", ".markdown"}:
            return raw_text, display_name(source_file.stem)
        return f"# {display_name(source_file.stem)}\n\n{raw_text.strip()}\n", display_name(
            source_file.stem
        )
    if suffix in SUPPORTED_HTML_SUFFIXES:
        body = strip_html(source_file.read_text(encoding="utf-8", errors="ignore"))
        return f"# {display_name(source_file.stem)}\n\n{body}\n", display_name(source_file.stem)
    if suffix in SUPPORTED_JSON_SUFFIXES:
        title = title_from_json(source_file) or display_name(source_file.stem)
        body = text_from_json(source_file)
        return f"# {title}\n\n{body}\n", title
    if suffix in SUPPORTED_CSV_SUFFIXES:
        body = text_from_csv(source_file)
        return f"# {display_name(source_file.stem)}\n\n{body}\n", display_name(source_file.stem)
    raise ValueError(f"Unsupported export file: {source_file}")


def relative_to_root(path: Path, root: Path) -> Path:
    return path.relative_to(root) if path.is_relative_to(root) else Path(path.name)


def build_raw_source_copy_map(
    *,
    input_path: Path,
    prepared_root: Path,
    output_root: Path,
    source_files: list[Path],
) -> dict[str, str]:
    raw_root = output_root / "raw-source"
    raw_root.mkdir(parents=True, exist_ok=True)
    copied: dict[str, str] = {}
    if input_path.is_file():
        target = raw_root / input_path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(input_path, target)
        copied[str(input_path.resolve())] = str(target)
        return copied

    for source_file in source_files:
        relative = relative_to_root(source_file, prepared_root)
        target = raw_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, target)
        copied[str(source_file.resolve())] = str(target)
    return copied


def prepare_staging_markdown(
    *,
    prepared_root: Path,
    staging_root: Path,
    source_files: list[Path],
) -> tuple[list[dict[str, Any]], Counter[str]]:
    manifest: list[dict[str, Any]] = []
    format_counts: Counter[str] = Counter()
    for source_file in source_files:
        relative = relative_to_root(source_file, prepared_root)
        rendered_markdown, title = render_markdown_document(source_file)
        if not rendered_markdown.strip():
            continue
        normalized_relative = relative.with_suffix(".md")
        staging_path = staging_root / normalized_relative
        staging_path.parent.mkdir(parents=True, exist_ok=True)
        staging_path.write_text(rendered_markdown.rstrip() + "\n", encoding="utf-8")
        detected_format = source_file.suffix.lower().lstrip(".")
        format_counts[detected_format or "unknown"] += 1
        manifest.append(
            {
                "source_export": str(source_file.resolve()),
                "normalized_markdown": str(staging_path),
                "relative_path": str(relative),
                "normalized_relative_path": str(normalized_relative),
                "detected_format": detected_format or "unknown",
                "title_hint": title,
            },
        )
    return manifest, format_counts


def build_import_manifest(
    *,
    normalized_manifest: list[dict[str, Any]],
    staging_root: Path,
    output_root: Path,
    raw_copy_map: dict[str, str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in normalized_manifest:
        normalized_path = Path(item["normalized_markdown"])
        relative = normalized_path.relative_to(staging_root)
        slug = slugify(relative.as_posix().replace("/", "-"), limit=96)
        thread_uid = f"thread-{slug}"
        pair_id = f"{thread_uid}-pair-001"
        family_id = f"family-{slug}"
        rows.append(
            {
                "source_export": item["source_export"],
                "raw_source_copy": raw_copy_map.get(item["source_export"]),
                "normalized_markdown": item["normalized_markdown"],
                "copied_source": str((output_root / "source" / relative).resolve()),
                "relative_path": item["relative_path"],
                "normalized_relative_path": item["normalized_relative_path"],
                "detected_format": item["detected_format"],
                "title_hint": item["title_hint"],
                "family_id": family_id,
                "thread_uid": thread_uid,
                "pair_id": pair_id,
            },
        )
    return rows


def import_document_export_corpus(
    input_path: Path,
    output_root: Path,
    *,
    provider_slug: str,
    corpus_id: str | None = None,
    name: str | None = None,
    throttle: float = 0.0,
    authorization: CorpusWriteAuthorization | None = None,
) -> dict[str, Any]:
    config = get_provider_config(provider_slug)
    provider_name = config["display_name"]
    adapter_type = config["adapter_type"]
    default_name = name or config["default_corpus_name"]

    input_path = input_path.resolve()
    prepared_root, archive_type, tempdir = prepare_source_root(
        input_path, provider_slug=provider_slug
    )
    try:
        source_files = [
            path
            for path in collect_supported_export_files(prepared_root)
            if path.suffix.lower() != ".zip"
        ]
        if not source_files:
            raise FileNotFoundError(
                f"No supported {provider_name} export files were found under {prepared_root}. "
                "Supported files end in .md, .markdown, .txt, .html, .htm, .json, or .csv.",
            )

        output_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f"{provider_slug}-staging-") as staging_dir:
            staging_root = Path(staging_dir)
            normalized_manifest, format_counts = prepare_staging_markdown(
                prepared_root=prepared_root,
                staging_root=staging_root,
                source_files=source_files,
            )
            if not normalized_manifest:
                raise FileNotFoundError(
                    f"{provider_name} export under {input_path} did not yield any non-empty text documents."
                )

            import_result = import_markdown_document_corpus(
                staging_root,
                output_root,
                corpus_id=corpus_id,
                name=default_name,
                authorization=authorization,
            )
            raw_copy_map = build_raw_source_copy_map(
                input_path=input_path,
                prepared_root=prepared_root,
                output_root=output_root,
                source_files=source_files,
            )
            import_manifest = build_import_manifest(
                normalized_manifest=normalized_manifest,
                staging_root=staging_root,
                output_root=output_root,
                raw_copy_map=raw_copy_map,
            )

        corpus_dir = output_root / "corpus"
        contract = load_json(corpus_dir / "contract.json", default={}) or {}
        source_snapshot = build_source_snapshot(input_path, adapter_type, "export-bundle")
        contract.update(
            {
                "contract_name": CONTRACT_NAME,
                "contract_version": CONTRACT_VERSION,
                "adapter_type": adapter_type,
                "corpus_id": slugify(corpus_id or output_root.name),
                "name": default_name,
                "generated_at": now_iso(),
                "source_input": str(input_path),
                "collection_scope": "export-bundle",
                "source_snapshot_path": "corpus/source-snapshot.json",
                "source_signature_fingerprint": source_snapshot.get("signature_fingerprint"),
                "source_content_fingerprint": source_snapshot.get("content_fingerprint"),
                "source_file_count": source_snapshot.get("file_count"),
                "source_total_bytes": source_snapshot.get("total_bytes"),
                "source_latest_mtime_ns": source_snapshot.get("latest_mtime_ns"),
                "source_format_counts": dict(format_counts),
                "source_archive_type": archive_type,
            },
        )
        write_json(corpus_dir / "source-snapshot.json", source_snapshot)
        write_json(corpus_dir / "contract.json", contract)
        write_json(
            corpus_dir / "evaluation-summary.json",
            {
                "generated_at": now_iso(),
                "fixture_sources": {"manual": {"source": "unavailable", "count": 0}},
                "regression_gates": {"overall_state": "warn", "source_reliability_state": "warn"},
                "notes": [
                    f"Imported {provider_name} export corpus has not been manually evaluated."
                ],
            },
        )
        write_json(
            corpus_dir / "regression-gates.json",
            {
                "generated_at": now_iso(),
                "overall_state": "warn",
                "source_reliability_state": "warn",
                "source_notes": [
                    f"Imported {provider_name} export corpus has not been manually evaluated."
                ],
                "gates": [],
            },
        )
        write_corpus_collection(
            output_root / "import-manifest.json",
            import_manifest,
            authorization=authorization,
        )
        write_markdown(
            output_root / "README.md",
            "\n".join(
                [
                    f"# {default_name} Corpus",
                    "",
                    f"- Generated: {now_iso()}",
                    f"- Provider: {provider_name}",
                    f"- Source input: {input_path}",
                    f"- Archive type: {archive_type or 'none'}",
                    f"- Supported export files imported: {len(source_files)}",
                    f"- Format counts: {dict(format_counts)}",
                    f"- Thread count: {contract.get('counts', {}).get('threads', 0)}",
                    f"- Action count: {contract.get('counts', {}).get('actions', 0)}",
                    f"- Unresolved count: {contract.get('counts', {}).get('unresolved', 0)}",
                    f"- Contract manifest: {corpus_dir / 'contract.json'}",
                    "",
                    "This corpus is calibration-only by default and remains unevaluated until manual fixtures are added.",
                ],
            ),
        )
        return {
            **import_result,
            "provider": provider_slug,
            "corpus_id": contract["corpus_id"],
            "name": contract["name"],
            "source_file_count": len(source_files),
            "format_counts": dict(format_counts),
            "archive_type": archive_type,
            "manifest_path": str(collection_storage_path(output_root / "import-manifest.json")),
            "readme_path": str(output_root / "README.md"),
        }
    finally:
        if tempdir is not None:
            tempdir.cleanup()


def main() -> int:
    args = parse_args()
    # The corpus is the export's text as plaintext, and --output-root defaults to the working
    # directory: refuse a folder Git can see before the export is read.
    if (refusal := git_visible_refusal(args.output_root)) is not None:
        print(f"output refused: {refusal}", file=sys.stderr)
        return 2
    result = import_document_export_corpus(
        args.input_path,
        args.output_root,
        provider_slug=args.provider,
        corpus_id=args.corpus_id,
        name=args.name,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
