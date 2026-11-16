#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .answering import slugify, tokenize, write_json, write_markdown
from .corpus_store import git_visible_refusal
from .sharded_collection import (
    collection_storage_path,
    merge_entity_records,
    merge_ledger_records,
    write_corpus_collection,
)
from .source_lifecycle import build_source_snapshot

if TYPE_CHECKING:
    from .corpus_store import CorpusWriteAuthorization

DEFAULT_OUTPUT_ROOT = Path.cwd() / "markdown-document-memory"
CONTRACT_NAME = "conversation-corpus-engine.v2"
CONTRACT_VERSION = 2
H1_RE = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)
LIST_PREFIX_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)")
FOOTNOTE_RE = re.compile(r"\[\^[^\]]+\]")
MARKDOWN_DECORATION_RE = re.compile(r"[*_`~]+")
NOISE_CHUNK_RE = re.compile(r"(?:[\\/|]{4,}|[_]{4,}|[─]{4,})")
STOP_WORDS = {
    "a",
    "about",
    "all",
    "also",
    "an",
    "and",
    "any",
    "are",
    "as",
    "at",
    "be",
    "been",
    "being",
    "but",
    "by",
    "can",
    "could",
    "day",
    "do",
    "for",
    "from",
    "get",
    "going",
    "have",
    "how",
    "if",
    "in",
    "into",
    "is",
    "it",
    "just",
    "like",
    "make",
    "maybe",
    "more",
    "need",
    "not",
    "of",
    "on",
    "or",
    "our",
    "out",
    "say",
    "should",
    "so",
    "sort",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "they",
    "this",
    "to",
    "uh",
    "um",
    "want",
    "we",
    "what",
    "where",
    "will",
    "with",
    "www",
    "you",
    "your",
    "http",
    "https",
    "com",
    "org",
    "gov",
    "pdf",
}
ACTION_MARKERS = (
    "need to",
    "needs to",
    "should",
    "will",
    "want to",
    "want",
    "implement",
    "add",
    "build",
    "develop",
    "action step",
    "next step",
    "recommendation",
    "recommended",
    "do not",
    "minimize",
    "call ",
    "keep ",
    "use ",
)
UNRESOLVED_MARKERS = (
    "maybe",
    "perhaps",
    "whether",
    "unclear",
    "unknown",
    "open question",
    "decision remains",
    "which option",
    "what if",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import generic markdown files into a federation-compatible memory corpus."
    )
    parser.add_argument("input_path", type=Path)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--name")
    parser.add_argument("--corpus-id")
    parser.add_argument("--max-depth", type=int, default=None)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def collect_markdown_files(input_path: Path, *, max_depth: int | None = None) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    if max_depth is not None and max_depth <= 1:
        return sorted(path for path in input_path.glob("*.md") if path.is_file())
    return sorted(path for path in input_path.rglob("*.md") if path.is_file())


def clean_markdown(text: str) -> str:
    text = re.sub(r"<img[^>]*>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", text)
    text = FOOTNOTE_RE.sub("", text)
    lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line in {"---", "***"}:
            continue
        cleaned = normalize_line(line)
        if not cleaned or is_noise_line(cleaned):
            continue
        lines.append(cleaned)
    return "\n".join(lines).strip()


def split_sentences(text: str) -> list[str]:
    rough = re.split(r"(?:\n+|(?<=[.!?])\s+)", text)
    sentences = []
    for item in rough:
        sentence = " ".join(item.split()).strip()
        if sentence:
            sentences.append(sentence)
    return sentences


def top_keywords(text: str, *, limit: int = 12) -> list[str]:
    counts = Counter(
        token for token in tokenize(text) if token not in STOP_WORDS and len(token) > 2
    )
    return [token for token, _ in counts.most_common(limit)]


def vector_terms(text: str, *, limit: int = 18) -> dict[str, float]:
    counts = Counter(
        token for token in tokenize(text) if token not in STOP_WORDS and len(token) > 2
    )
    if not counts:
        return {}
    max_count = max(counts.values())
    result: dict[str, float] = {}
    for token, count in counts.most_common(limit):
        result[token] = round(count / max_count, 4)
    return result


def dedupe_preserve(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        lowered = value.lower()
        if not value or lowered in seen:
            continue
        seen.add(lowered)
        ordered.append(value)
    return ordered


def shorten(text: str, limit: int = 220) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 3)].rstrip() + "..."


def normalize_line(text: str) -> str:
    cleaned = text.strip()
    cleaned = FOOTNOTE_RE.sub("", cleaned)
    cleaned = cleaned.replace("\\&", "&")
    cleaned = re.sub(r"^#{1,6}\s*", "", cleaned)
    cleaned = re.sub(r"^>\s*", "", cleaned)
    cleaned = LIST_PREFIX_RE.sub("", cleaned)
    cleaned = MARKDOWN_DECORATION_RE.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" -")


def alpha_ratio(text: str) -> float:
    if not text:
        return 0.0
    non_space = [char for char in text if not char.isspace()]
    if not non_space:
        return 0.0
    alpha = [char for char in non_space if char.isalpha()]
    return len(alpha) / len(non_space)


def is_noise_line(text: str) -> bool:
    lowered = text.lower()
    if lowered in {"q:", "a:", "## q:", "## a:"}:
        return True
    if text.startswith("```") or text.endswith("```"):
        return True
    if text.count("|") >= 3:
        return True
    if NOISE_CHUNK_RE.search(text):
        return True
    return bool(alpha_ratio(text) < 0.35 and len(text) >= 18)


def is_candidate_sentence(text: str) -> bool:
    normalized = normalize_line(text)
    if not normalized:
        return False
    if is_noise_line(normalized):
        return False
    if len(normalized) < 24 or len(normalized) > 220:
        return False
    if "http" in normalized.lower():
        return False
    return not alpha_ratio(normalized) < 0.55


def extract_actions(sentences: list[str]) -> list[str]:
    candidates = []
    for sentence in sentences:
        normalized = normalize_line(sentence)
        lowered = normalized.lower()
        if not is_candidate_sentence(normalized):
            continue
        if any(marker in lowered for marker in ACTION_MARKERS):
            candidates.append(shorten(normalized, 180))
    return dedupe_preserve(candidates)[:5]


def extract_unresolved(sentences: list[str]) -> list[str]:
    candidates = []
    for sentence in sentences:
        normalized = normalize_line(sentence)
        lowered = normalized.lower()
        if not is_candidate_sentence(normalized):
            continue
        if normalized.endswith("?") or any(marker in lowered for marker in UNRESOLVED_MARKERS):
            candidates.append(shorten(normalized, 180))
    return dedupe_preserve(candidates)[:4]


def keyword_entities(title: str, keywords: list[str]) -> list[dict[str, str]]:
    entities: list[dict[str, str]] = []
    title_entity = title.replace("-", " ").strip()
    if title_entity:
        entities.append({"canonical_label": title_entity, "entity_type": "document"})
    for keyword in keywords[:3]:
        label = keyword.replace("-", " ").title()
        entities.append({"canonical_label": label, "entity_type": "concept"})
    return dedupe_entity_list(entities)


def dedupe_entity_list(entities: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    ordered: list[dict[str, str]] = []
    for entity in entities:
        label = entity["canonical_label"].strip()
        lowered = label.lower()
        if not label or lowered in seen:
            continue
        seen.add(lowered)
        ordered.append({"canonical_label": label, "entity_type": entity["entity_type"]})
    return ordered


def infer_title(markdown_file: Path, raw_text: str) -> str:
    match = H1_RE.search(raw_text)
    if match:
        return normalize_line(match.group(1).strip())
    return normalize_line(markdown_file.stem.replace("_", " ").strip())


def infer_tags(raw_text: str, title: str, keywords: list[str]) -> list[str]:
    tags = ["markdown-import", "markdown-document"]
    lowered = raw_text.lower()
    keyword_set = set(keywords)
    if "## q:" in lowered and "## a:" in lowered:
        tags.append("q-and-a")
    if "```" in raw_text or {"react", "python", "javascript", "code"} & keyword_set:
        tags.append("code-heavy")
    if {"plan", "roadmap", "workflow", "implementation"} & keyword_set:
        tags.append("planning")
    if {"research", "review", "funding", "grant", "blueprint"} & keyword_set:
        tags.append("research")
    if {"housing", "benefits", "support", "trauma", "recovery"} & keyword_set:
        tags.append("support")
    if "resume-me" in title.lower() or "branch" in title.lower():
        tags.append("branch-export")
    return dedupe_preserve(tags)


def timestamp_for_path(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()


def import_markdown_document_corpus(
    input_path: Path,
    output_root: Path,
    *,
    corpus_id: str | None = None,
    name: str | None = None,
    max_depth: int | None = None,
    authorization: CorpusWriteAuthorization | None = None,
) -> dict[str, Any]:
    markdown_files = collect_markdown_files(input_path, max_depth=max_depth)
    if not markdown_files:
        raise FileNotFoundError(f"No markdown files found under {input_path}")

    source_root = input_path if input_path.is_dir() else input_path.parent
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "source").mkdir(parents=True, exist_ok=True)
    (output_root / "corpus").mkdir(parents=True, exist_ok=True)

    threads: list[dict[str, Any]] = []
    semantic_threads: list[dict[str, Any]] = []
    pairs: list[dict[str, Any]] = []
    doctrine_briefs: list[dict[str, Any]] = []
    family_dossiers: list[dict[str, Any]] = []
    canonical_families: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    entities: list[dict[str, Any]] = []
    entity_aliases: list[dict[str, Any]] = []
    import_manifest: list[dict[str, str | int | None]] = []

    for markdown_file in markdown_files:
        relative = markdown_file.relative_to(source_root)
        source_copy = output_root / "source" / relative
        source_copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(markdown_file, source_copy)

        raw_text = markdown_file.read_text(encoding="utf-8", errors="ignore")
        cleaned = clean_markdown(raw_text)
        sentences = split_sentences(cleaned)
        keywords = top_keywords(cleaned)
        vectors = vector_terms(cleaned)
        title = infer_title(markdown_file, raw_text)
        tags = infer_tags(raw_text, title, keywords)
        actions_text = extract_actions(sentences)
        unresolved_text = extract_unresolved(sentences)
        slug = slugify(relative.as_posix().replace("/", "-"), limit=96)
        family_id = f"family-{slug}"
        thread_uid = f"thread-{slug}"
        pair_id = f"{thread_uid}-pair-001"
        summary = shorten(" ".join(sentences[:3]) or cleaned or title, 320)
        key_entities = keyword_entities(title, keywords)
        document_timestamp = timestamp_for_path(markdown_file)

        action_items = [
            {
                "action_key": f"action-{slugify(action, limit=64)}",
                "canonical_action": action,
                "status": "open",
                "family_ids": [family_id],
                "thread_uids": [thread_uid],
                "occurrence_count": 1,
            }
            for action in actions_text
        ]
        unresolved_items = [
            {
                "question_key": f"question-{slugify(question, limit=64)}",
                "canonical_question": question,
                "why_unresolved": "Imported markdown document still implies an open question, ambiguity, or unresolved branch.",
                "family_ids": [family_id],
                "thread_uids": [thread_uid],
                "occurrence_count": 1,
            }
            for question in unresolved_text
        ]

        semantic_themes = keywords[:8]
        semantic_entities = [entity["canonical_label"] for entity in key_entities]
        search_text = cleaned or raw_text

        threads.append(
            {
                "thread_uid": thread_uid,
                "conversation_id": slug,
                "title_normalized": title,
                "title_raw": title,
                "answer_state": "imported_markdown_document",
                "tags": tags,
                "semantic_tags": ["markdown-thread"],
                "keywords": keywords,
                "semantic_themes": keywords[:6],
                "semantic_v2_themes": semantic_themes,
                "semantic_v3_themes": semantic_themes,
                "semantic_v3_entities": semantic_entities,
                "semantic_summary": summary,
                "semantic_v2_summary": summary,
                "semantic_v3_summary": f"{title} imported markdown document centered on {', '.join(keywords[:4])}.",
                "action_count": len(action_items),
                "unresolved_question_count": len(unresolved_items),
                "audit_flags": [],
                "thread_path": str(source_copy),
                "family_ids": [family_id],
                "update_time_iso": document_timestamp,
            },
        )
        semantic_threads.append(
            {
                "thread_uid": thread_uid,
                "title": title,
                "summary": summary,
                "search_text": f"{title} {search_text}",
                "family_ids": [family_id],
                "vector_terms": vectors,
            },
        )
        pairs.append(
            {
                "pair_id": pair_id,
                "thread_uid": thread_uid,
                "title": title,
                "summary": f"Imported markdown pair about {', '.join(keywords[:4])}.",
                "search_text": f"{pair_id} {title} {search_text}",
                "themes": semantic_themes,
                "entities": semantic_entities,
                "family_ids": [family_id],
                "vector_terms": vectors,
            },
        )
        doctrine_briefs.append(
            {
                "family_id": family_id,
                "canonical_title": title,
                "canonical_thread_uid": thread_uid,
                "member_count": 1,
                "stable_themes": semantic_themes,
                "brief_text": f"{title} currently centers on {', '.join(keywords[:4])}. Imported from markdown document {relative}.",
                "search_text": f"{title} {' '.join(keywords[:10])} {search_text}",
                "vector_terms": vectors,
            },
        )
        family_dossiers.append(
            {
                "family_id": family_id,
                "canonical_title": title,
                "canonical_thread_uid": thread_uid,
                "member_count": 1,
                "stable_themes": semantic_themes,
                "doctrine_summary": f"{title} imported markdown document summary: {summary}",
                "search_text": f"{title} doctrine {' '.join(keywords[:10])} {search_text}",
                "actions": [
                    {"action_key": item["action_key"], "canonical_action": item["canonical_action"]}
                    for item in action_items
                ],
                "unresolved": [
                    {
                        "question_key": item["question_key"],
                        "canonical_question": item["canonical_question"],
                    }
                    for item in unresolved_items
                ],
                "key_entities": key_entities,
                "vector_terms": vectors,
            },
        )
        canonical_families.append(
            {
                "canonical_family_id": family_id,
                "canonical_title": title,
                "canonical_thread_uid": thread_uid,
                "thread_uids": [thread_uid],
            },
        )
        actions.extend(action_items)
        unresolved.extend(unresolved_items)
        for entity in key_entities:
            entity_id = f"entity-{slugify(entity['canonical_label'], limit=64)}"
            entities.append(
                {
                    "canonical_entity_id": entity_id,
                    "canonical_label": entity["canonical_label"],
                    "entity_type": entity["entity_type"],
                    "aliases": [title],
                },
            )
            entity_aliases.append(
                {
                    "canonical_label": entity["canonical_label"],
                    "labels": [title],
                },
            )
        import_manifest.append(
            {
                "source_markdown": str(markdown_file),
                "copied_source": str(source_copy),
                "family_id": family_id,
                "thread_uid": thread_uid,
                "pair_id": pair_id,
                "max_depth": max_depth,
            },
        )

    corpus_dir = output_root / "corpus"
    collection_scope = "top-level" if max_depth == 1 and input_path.is_dir() else "recursive"
    source_snapshot = build_source_snapshot(input_path, "markdown-document", collection_scope)
    actions = merge_ledger_records(
        actions,
        key_field="action_key",
        text_field="canonical_action",
    )
    unresolved = merge_ledger_records(
        unresolved,
        key_field="question_key",
        text_field="canonical_question",
    )
    entities = merge_entity_records(entities)
    entity_aliases = [
        {
            "canonical_label": entity["canonical_label"],
            "labels": entity.get("aliases") or [],
        }
        for entity in entities
        if entity.get("aliases")
    ]
    collections = {
        "threads-index.json": threads,
        "semantic-v3-index.json": semantic_threads,
        "pairs-index.json": pairs,
        "doctrine-briefs.json": doctrine_briefs,
        "family-dossiers.json": family_dossiers,
        "canonical-families.json": canonical_families,
        "action-ledger.json": actions,
        "unresolved-ledger.json": unresolved,
        "canonical-entities.json": entities,
        "entity-aliases.json": entity_aliases,
        "doctrine-timeline.json": [],
    }
    for filename, records in collections.items():
        write_corpus_collection(
            corpus_dir / filename,
            records,
            authorization=authorization,
        )
    write_json(corpus_dir / "source-snapshot.json", source_snapshot)
    write_json(
        corpus_dir / "contract.json",
        {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "adapter_type": "markdown-document",
            "corpus_id": slugify(corpus_id or output_root.name),
            "name": name or output_root.name,
            "generated_at": now_iso(),
            "required_files": [
                "corpus/threads-index.collection",
                "corpus/semantic-v3-index.collection",
                "corpus/pairs-index.collection",
                "corpus/doctrine-briefs.collection",
                "corpus/family-dossiers.collection",
            ],
            "counts": {
                "threads": len(threads),
                "families": len(canonical_families),
                "actions": len(actions),
                "unresolved": len(unresolved),
                "entities": len(entities),
            },
            "source_input": str(input_path),
            "collection_scope": collection_scope,
            "source_snapshot_path": "corpus/source-snapshot.json",
            "source_signature_fingerprint": source_snapshot.get("signature_fingerprint"),
            "source_content_fingerprint": source_snapshot.get("content_fingerprint"),
            "source_file_count": source_snapshot.get("file_count"),
            "source_total_bytes": source_snapshot.get("total_bytes"),
            "source_latest_mtime_ns": source_snapshot.get("latest_mtime_ns"),
        },
    )
    write_json(
        corpus_dir / "evaluation-summary.json",
        {
            "generated_at": now_iso(),
            "fixture_sources": {"manual": {"source": "unavailable", "count": 0}},
            "regression_gates": {"overall_state": "warn", "source_reliability_state": "warn"},
            "notes": ["Imported markdown document corpus has not been manually evaluated."],
        },
    )
    write_json(
        corpus_dir / "regression-gates.json",
        {
            "generated_at": now_iso(),
            "overall_state": "warn",
            "source_reliability_state": "warn",
            "source_notes": ["Imported markdown document corpus has not been manually evaluated."],
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
                "# Markdown Document Memory Corpus",
                "",
                f"- Generated: {now_iso()}",
                f"- Source input: {input_path}",
                f"- Max depth: {max_depth if max_depth is not None else 'recursive'}",
                f"- Markdown files imported: {len(markdown_files)}",
                f"- Thread count: {len(threads)}",
                f"- Action count: {len(actions)}",
                f"- Unresolved count: {len(unresolved)}",
                f"- Contract manifest: {corpus_dir / 'contract.json'}",
                "",
                "This corpus is federation-compatible but unevaluated. Its regression gate state is intentionally `warn` until manual fixtures are added.",
            ],
        ),
    )

    resolved_corpus_id = corpus_id or output_root.name
    return {
        "corpus_id": slugify(resolved_corpus_id),
        "name": name or output_root.name,
        "output_root": str(output_root),
        "thread_count": len(threads),
        "action_count": len(actions),
        "unresolved_count": len(unresolved),
        "manifest_path": str(collection_storage_path(output_root / "import-manifest.json")),
        "readme_path": str(output_root / "README.md"),
    }


def main() -> int:
    args = parse_args()
    # The corpus is the documents' text as plaintext, and --output-root defaults to the working
    # directory: refuse a folder Git can see before anything is read.
    if (refusal := git_visible_refusal(args.output_root)) is not None:
        print(f"output refused: {refusal}", file=sys.stderr)
        return 2
    result = import_markdown_document_corpus(
        args.input_path,
        args.output_root,
        corpus_id=args.corpus_id,
        name=args.name,
        max_depth=args.max_depth,
    )
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
