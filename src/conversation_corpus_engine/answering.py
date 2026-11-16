#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .sharded_collection import load_corpus_collection

TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_-]+")
STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "how",
    "if",
    "in",
    "into",
    "is",
    "it",
    "of",
    "on",
    "or",
    "should",
    "that",
    "the",
    "this",
    "to",
    "what",
    "when",
    "where",
    "which",
    "why",
    "with",
}
QUERY_SYNONYMS = {
    "action": ["implement", "task", "todo", "pressure"],
    "actions": ["implement", "task", "todo", "pressure"],
    "drift": ["movement", "transition", "change", "doctrine"],
    "timeline": ["movement", "transition", "change", "lineage"],
    "unresolved": ["open", "ambiguity", "question", "unknown"],
    "question": ["unresolved", "open", "ambiguity"],
    "contradiction": ["opposition", "conflict", "axis"],
    "duplicate": ["same", "merge", "alias"],
}


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def write_markdown(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def tokenize(text: str) -> list[str]:
    return TOKEN_RE.findall((text or "").lower())


def unique_preserve(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def slugify(text: str, *, limit: int = 64) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    if not slug:
        slug = "query"
    return slug[:limit].rstrip("-")


def shorten(text: str, limit: int = 220) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 3)].rstrip() + "..."


def extract_snippet(text: str, query_tokens: list[str], *, width: int = 220) -> str:
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return ""
    lower = cleaned.lower()
    positions = [lower.find(token) for token in query_tokens if token and lower.find(token) >= 0]
    if not positions:
        return shorten(cleaned, width)
    position = min(positions)
    start = max(0, position - max(20, width // 3))
    end = min(len(cleaned), start + width)
    snippet = cleaned[start:end].strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(cleaned):
        snippet = snippet + "..."
    return snippet


def canonical_thread_for_families(
    family_ids: list[str] | None,
    family_canonical_thread_map: dict[str, str],
) -> str | None:
    for family_id in family_ids or []:
        canonical_thread_uid = family_canonical_thread_map.get(family_id)
        if canonical_thread_uid:
            return canonical_thread_uid
    return None


def build_documents(root: Path) -> dict[str, Any]:
    documents: list[dict[str, Any]] = []
    family_docs: list[dict[str, Any]] = []
    thread_docs: list[dict[str, Any]] = []
    pair_docs: list[dict[str, Any]] = []
    ledger_docs: list[dict[str, Any]] = []

    corpus_dir = root / "corpus"
    threads_index = load_corpus_collection(corpus_dir / "threads-index.json", default=[]) or []
    semantic_index = {
        "threads": load_corpus_collection(corpus_dir / "semantic-v3-index.json", default=[]) or []
    }
    pairs_index = load_corpus_collection(corpus_dir / "pairs-index.json", default=[]) or []
    doctrine_briefs = load_corpus_collection(corpus_dir / "doctrine-briefs.json", default=[]) or []
    family_dossiers = load_corpus_collection(corpus_dir / "family-dossiers.json", default=[]) or []
    action_ledger = load_corpus_collection(corpus_dir / "action-ledger.json", default=[]) or []
    unresolved_ledger = (
        load_corpus_collection(corpus_dir / "unresolved-ledger.json", default=[]) or []
    )
    doctrine_timeline = (
        load_corpus_collection(corpus_dir / "doctrine-timeline.json", default=[]) or []
    )
    canonical_entities = (
        load_corpus_collection(corpus_dir / "canonical-entities.json", default=[]) or []
    )
    entity_aliases = load_corpus_collection(corpus_dir / "entity-aliases.json", default=[]) or []

    thread_family_map: dict[str, list[str]] = {}
    thread_title_map: dict[str, str] = {}
    for item in threads_index:
        thread_uid = item["thread_uid"]
        thread_family_map[thread_uid] = item.get("family_ids", [])
        thread_title_map[thread_uid] = (
            item.get("title_normalized") or item.get("title_raw") or thread_uid
        )

    family_title_map: dict[str, str] = {}
    family_canonical_thread_map: dict[str, str] = {}
    family_theme_map: dict[str, list[str]] = {}
    family_entity_map: dict[str, list[str]] = {}
    for item in family_dossiers:
        family_id = item["family_id"]
        family_title_map[family_id] = item.get("canonical_title") or family_id
        family_canonical_thread_map[family_id] = item.get("canonical_thread_uid") or ""
        family_theme_map[family_id] = item.get("stable_themes", []) or item.get(
            "dominant_themes", []
        )
        family_entity_map[family_id] = [
            entry["canonical_label"] for entry in item.get("key_entities", [])
        ]

    for item in doctrine_briefs:
        document = {
            "kind": "family_brief",
            "doc_id": f"family-brief:{item['family_id']}",
            "title": item.get("canonical_title") or item["family_id"],
            "text": item.get("search_text") or item.get("brief_text") or "",
            "family_id": item["family_id"],
            "family_ids": [item["family_id"]],
            "canonical_thread_uid": item.get("canonical_thread_uid"),
            "thread_uid": item.get("canonical_thread_uid"),
            "citations": [
                f"family:{item['family_id']}",
                f"thread:{item.get('canonical_thread_uid')}",
            ],
            "vector_terms": item.get("vector_terms", {}),
            "payload": item,
        }
        documents.append(document)
        family_docs.append(document)

    for item in family_dossiers:
        document = {
            "kind": "family_dossier",
            "doc_id": f"family-dossier:{item['family_id']}",
            "title": item.get("canonical_title") or item["family_id"],
            "text": item.get("search_text") or item.get("doctrine_summary") or "",
            "family_id": item["family_id"],
            "family_ids": [item["family_id"]],
            "canonical_thread_uid": item.get("canonical_thread_uid"),
            "thread_uid": item.get("canonical_thread_uid"),
            "citations": [
                f"family:{item['family_id']}",
                f"thread:{item.get('canonical_thread_uid')}",
            ],
            "vector_terms": item.get("vector_terms", {}),
            "payload": item,
        }
        documents.append(document)
        family_docs.append(document)

    for item in threads_index:
        canonical_thread_uid = canonical_thread_for_families(
            item.get("family_ids", []),
            family_canonical_thread_map,
        )
        document = {
            "kind": "thread",
            "doc_id": f"thread:{item['thread_uid']}",
            "title": item.get("title_normalized") or item.get("title_raw") or item["thread_uid"],
            "text": " ".join(
                [
                    item.get("semantic_summary") or "",
                    item.get("semantic_v2_summary") or "",
                    item.get("semantic_v3_summary") or "",
                    " ".join(item.get("semantic_v3_themes") or []),
                    " ".join(item.get("semantic_v3_entities") or []),
                ],
            ).strip(),
            "family_ids": item.get("family_ids", []),
            "canonical_thread_uid": canonical_thread_uid,
            "thread_uid": item["thread_uid"],
            "citations": [f"thread:{item['thread_uid']}"],
            "vector_terms": item.get("vector_terms", {}),
            "payload": item,
        }
        documents.append(document)
        thread_docs.append(document)

    for item in semantic_index.get("threads", []):
        family_ids = item.get("family_ids") or thread_family_map.get(item["thread_uid"], [])
        canonical_thread_uid = canonical_thread_for_families(
            family_ids,
            family_canonical_thread_map,
        )
        document = {
            "kind": "thread_semantic",
            "doc_id": f"thread-semantic:{item['thread_uid']}",
            "title": item.get("title")
            or thread_title_map.get(item["thread_uid"], item["thread_uid"]),
            "text": item.get("search_text") or item.get("summary") or "",
            "family_ids": family_ids,
            "canonical_thread_uid": canonical_thread_uid,
            "thread_uid": item["thread_uid"],
            "citations": [f"thread:{item['thread_uid']}"],
            "vector_terms": item.get("vector_terms", {}),
            "payload": item,
        }
        documents.append(document)
        thread_docs.append(document)

    for item in pairs_index:
        family_ids = item.get("family_ids") or thread_family_map.get(
            item.get("thread_uid") or "", []
        )
        canonical_thread_uid = canonical_thread_for_families(
            family_ids,
            family_canonical_thread_map,
        )
        document = {
            "kind": "pair",
            "doc_id": f"pair:{item['pair_id']}",
            "title": item.get("title")
            or thread_title_map.get(item.get("thread_uid") or "", item["pair_id"]),
            "text": item.get("search_text") or item.get("summary") or "",
            "family_ids": family_ids,
            "canonical_thread_uid": canonical_thread_uid,
            "thread_uid": item.get("thread_uid"),
            "pair_id": item["pair_id"],
            "citations": [f"pair:{item['pair_id']}", f"thread:{item.get('thread_uid')}"],
            "vector_terms": item.get("vector_terms", {}),
            "payload": item,
        }
        documents.append(document)
        pair_docs.append(document)

    for item in action_ledger:
        document = {
            "kind": "action",
            "doc_id": f"action:{item['action_key']}",
            "title": item.get("canonical_action") or item["action_key"],
            "text": " ".join(
                [
                    item.get("canonical_action") or "",
                    " ".join(item.get("family_ids") or []),
                    " ".join(item.get("thread_uids") or []),
                ],
            ).strip(),
            "family_ids": item.get("family_ids", []),
            "thread_uid": (item.get("thread_uids") or [None])[0],
            "citations": [f"action:{item['action_key']}"],
            "vector_terms": item.get("vector_terms", {}),
            "payload": item,
        }
        documents.append(document)
        ledger_docs.append(document)

    for item in unresolved_ledger:
        document = {
            "kind": "unresolved",
            "doc_id": f"question:{item['question_key']}",
            "title": item.get("canonical_question") or item["question_key"],
            "text": " ".join(
                [
                    item.get("canonical_question") or "",
                    item.get("why_unresolved") or "",
                    " ".join(item.get("family_ids") or []),
                ],
            ).strip(),
            "family_ids": item.get("family_ids", []),
            "thread_uid": (item.get("thread_uids") or [None])[0],
            "citations": [f"question:{item['question_key']}"],
            "vector_terms": item.get("vector_terms", {}),
            "payload": item,
        }
        documents.append(document)
        ledger_docs.append(document)

    for family in doctrine_timeline:
        for index, item in enumerate(family.get("transitions", []), start=1):
            document = {
                "kind": "timeline",
                "doc_id": f"timeline:{family['canonical_family_id']}:{index:03d}",
                "title": family.get("canonical_title") or family["canonical_family_id"],
                "text": " ".join(
                    [
                        family.get("canonical_title") or "",
                        item.get("from_title") or "",
                        item.get("to_title") or "",
                        " ".join((item.get("theme_shift") or {}).get("added", [])),
                        " ".join((item.get("theme_shift") or {}).get("removed", [])),
                        item.get("decision_state") or "",
                    ],
                ).strip(),
                "family_id": family["canonical_family_id"],
                "family_ids": [family["canonical_family_id"]],
                "thread_uid": item.get("to_thread_uid"),
                "citations": [
                    f"family:{family['canonical_family_id']}",
                    f"thread:{item.get('to_thread_uid')}",
                ],
                "vector_terms": item.get("vector_terms", {}),
                "payload": item,
            }
            documents.append(document)
            ledger_docs.append(document)

    entity_alias_map: dict[str, list[str]] = defaultdict(list)
    for item in canonical_entities:
        label = item.get("canonical_label")
        if not label:
            continue
        entity_alias_map[label].extend(item.get("aliases") or [])
    for item in entity_aliases:
        entity_alias_map[item.get("canonical_label") or ""].extend(item.get("labels") or [])
    entity_alias_map = {
        key: unique_preserve(value) for key, value in entity_alias_map.items() if key
    }

    return {
        "documents": documents,
        "family_docs": family_docs,
        "thread_docs": thread_docs,
        "pair_docs": pair_docs,
        "ledger_docs": ledger_docs,
        "thread_family_map": thread_family_map,
        "family_canonical_thread_map": family_canonical_thread_map,
        "family_title_map": family_title_map,
        "family_theme_map": family_theme_map,
        "family_entity_map": family_entity_map,
        "entity_alias_map": entity_alias_map,
    }


def expand_query_tokens(query: str, corpus: dict[str, Any]) -> list[str]:
    base_tokens = tokenize(query)
    base_token_set = set(token for token in base_tokens if token not in STOP_WORDS)
    expanded = list(base_tokens)
    lower_query = query.lower()

    for token in base_tokens:
        expanded.extend(QUERY_SYNONYMS.get(token, []))

    for label, aliases in corpus.get("entity_alias_map", {}).items():
        label_tokens = tokenize(label)
        alias_text = " ".join(aliases).lower()
        label_token_overlap = base_token_set & set(
            token for token in label_tokens if token not in STOP_WORDS
        )
        alias_token_overlap = False
        for alias in aliases:
            alias_tokens = set(token for token in tokenize(alias) if token not in STOP_WORDS)
            if base_token_set & alias_tokens:
                alias_token_overlap = True
                break
        if (
            label.lower() in lower_query
            or label_token_overlap
            or alias_text
            and any(alias.lower() in lower_query for alias in aliases if alias)
            or alias_token_overlap
        ):
            expanded.extend(label_tokens)

    for family_id, themes in corpus.get("family_theme_map", {}).items():
        if family_id and any(theme in lower_query for theme in themes):
            expanded.extend(themes[:4])
            expanded.extend(
                tokenize(
                    corpus.get("family_entity_map", {}).get(family_id, [""])[0]
                    if corpus.get("family_entity_map", {}).get(family_id)
                    else ""
                )
            )
        for document in corpus.get("family_docs", []):
            if document.get("family_id") != family_id:
                continue
            title = document.get("title", "").lower()
            if title and title in lower_query:
                expanded.extend(themes[:4])
                for label in corpus.get("family_entity_map", {}).get(family_id, [])[:4]:
                    expanded.extend(tokenize(label))
                break

    filtered = [token for token in expanded if token not in STOP_WORDS]
    return unique_preserve(filtered)


def score_document(
    query: str,
    query_tokens: list[str],
    document: dict[str, Any],
    *,
    family_context: dict[str, float] | None = None,
    thread_context: dict[str, float] | None = None,
) -> tuple[float, dict[str, float]]:
    if not query_tokens:
        return 0.0, {}

    lower_query = query.lower().strip()
    lower_title = (document.get("title") or "").lower()
    lower_text = (document.get("text") or "").lower()
    vector_terms = document.get("vector_terms") or {}

    token_hits = sum(1 for token in query_tokens if token in lower_text or token in lower_title)
    title_hits = sum(1 for token in query_tokens if token in lower_title)
    text_hits = sum(1 for token in query_tokens if token in lower_text)
    coverage = token_hits / max(1, len(query_tokens))
    title_boost = title_hits / max(1, len(query_tokens))
    text_boost = text_hits / max(1, len(query_tokens))
    vector_score = sum(vector_terms.get(token, 0.0) for token in query_tokens)
    phrase_boost = (
        0.8 if lower_query and (lower_query in lower_title or lower_query in lower_text) else 0.0
    )

    family_prior = 0.0
    for family_id in document.get("family_ids", []):
        family_prior = max(family_prior, (family_context or {}).get(family_id, 0.0))
    thread_prior = (thread_context or {}).get(document.get("thread_uid") or "", 0.0)

    canonical_thread_boost = 0.0
    if (
        document.get("canonical_thread_uid")
        and document.get("thread_uid") == document.get("canonical_thread_uid")
        and (
            coverage > 0
            or vector_score > 0
            or phrase_boost > 0
            or family_prior > 0
            or thread_prior > 0
        )
    ):
        canonical_thread_boost = 0.25

    diagnostics = {
        "coverage": round(coverage, 4),
        "title_boost": round(title_boost, 4),
        "text_boost": round(text_boost, 4),
        "vector_score": round(vector_score, 4),
        "phrase_boost": round(phrase_boost, 4),
        "family_prior": round(family_prior, 4),
        "thread_prior": round(thread_prior, 4),
        "canonical_thread_boost": round(canonical_thread_boost, 4),
    }
    score = (
        (1.6 * coverage)
        + (0.9 * title_boost)
        + (0.35 * text_boost)
        + (0.6 * vector_score)
        + phrase_boost
        + (0.3 * family_prior)
        + (0.25 * thread_prior)
        + canonical_thread_boost
    )
    return round(score, 4), diagnostics


def rank_documents(
    query: str,
    query_tokens: list[str],
    documents: list[dict[str, Any]],
    *,
    limit: int,
    family_context: dict[str, float] | None = None,
    thread_context: dict[str, float] | None = None,
    kind_multiplier: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for document in documents:
        score, diagnostics = score_document(
            query,
            query_tokens,
            document,
            family_context=family_context,
            thread_context=thread_context,
        )
        multiplier = (kind_multiplier or {}).get(document["kind"], 1.0)
        score = round(score * multiplier, 4)
        if score <= 0:
            continue
        result = dict(document)
        result["score"] = score
        result["diagnostics"] = diagnostics
        result["snippet"] = extract_snippet(document.get("text") or "", query_tokens)
        results.append(result)
    results.sort(key=lambda item: (item["score"], item["kind"], item["doc_id"]), reverse=True)
    return results[:limit]


def lexical_support_for_tokens(tokens: list[str], document: dict[str, Any] | None) -> float:
    if not tokens or not document:
        return 0.0
    lower_title = (document.get("title") or "").lower()
    lower_text = (document.get("text") or "").lower()
    filtered_tokens = [token for token in tokens if token not in STOP_WORDS]
    if not filtered_tokens:
        return 0.0
    token_hits = sum(1 for token in filtered_tokens if token in lower_text or token in lower_title)
    title_hits = sum(1 for token in filtered_tokens if token in lower_title)
    text_hits = sum(1 for token in filtered_tokens if token in lower_text)
    size = max(1, len(filtered_tokens))
    return max(token_hits / size, title_hits / size, text_hits / size)


def matched_family_ids_for_query(query: str, corpus: dict[str, Any]) -> set[str]:
    lower_query = query.lower()
    query_token_set = set(token for token in tokenize(query) if token not in STOP_WORDS)
    matched: set[str] = set()
    for family_id, title in corpus.get("family_title_map", {}).items():
        if not title:
            continue
        title_lower = title.lower()
        title_tokens = [token for token in tokenize(title) if token not in STOP_WORDS]
        if title_lower and title_lower in lower_query:
            matched.add(family_id)
            continue
        if title_tokens and set(title_tokens).issubset(query_token_set):
            matched.add(family_id)
    return matched


def rerank_thread_hits(
    query: str,
    raw_query_tokens: list[str],
    thread_hits: list[dict[str, Any]],
    pair_hits: list[dict[str, Any]],
    *,
    pair_focused: bool,
) -> list[dict[str, Any]]:
    lower_query = query.lower()
    best_pair_score_by_thread: dict[str, float] = defaultdict(float)
    best_pair_support_by_thread: dict[str, float] = defaultdict(float)
    for item in pair_hits:
        thread_uid = item.get("thread_uid")
        if not thread_uid:
            continue
        best_pair_score_by_thread[thread_uid] = max(
            best_pair_score_by_thread[thread_uid], item.get("score", 0.0)
        )
        best_pair_support_by_thread[thread_uid] = max(
            best_pair_support_by_thread[thread_uid],
            lexical_support_for_tokens(raw_query_tokens, item),
        )

    reranked: list[dict[str, Any]] = []
    for item in thread_hits:
        thread_uid = item.get("thread_uid")
        raw_support = lexical_support_for_tokens(raw_query_tokens, item)
        pair_score = best_pair_score_by_thread.get(thread_uid or "", 0.0)
        pair_support = best_pair_support_by_thread.get(thread_uid or "", 0.0)
        canonical_bonus = 0.0
        if thread_uid and thread_uid == item.get("canonical_thread_uid"):
            canonical_bonus = 0.75 if pair_focused else 0.25
        title_phrase_bonus = 0.3 if (item.get("title") or "").lower() in lower_query else 0.0
        score = round(
            item.get("score", 0.0)
            + (0.22 * pair_score)
            + (0.75 * raw_support)
            + (0.35 * pair_support)
            + canonical_bonus
            + title_phrase_bonus,
            4,
        )
        result = dict(item)
        diagnostics = dict(item.get("diagnostics", {}))
        diagnostics.update(
            {
                "raw_query_support": round(raw_support, 4),
                "pair_feedback_score": round(pair_score, 4),
                "pair_feedback_support": round(pair_support, 4),
                "canonical_rerank_bonus": round(canonical_bonus, 4),
                "title_phrase_bonus": round(title_phrase_bonus, 4),
            },
        )
        result["score"] = score
        result["diagnostics"] = diagnostics
        reranked.append(result)
    reranked.sort(key=lambda item: (item["score"], item["kind"], item["doc_id"]), reverse=True)
    return reranked


def rerank_family_hits(
    query: str,
    raw_query_tokens: list[str],
    family_hits: list[dict[str, Any]],
    matched_family_ids: set[str],
) -> list[dict[str, Any]]:
    lower_query = query.lower().strip()
    max_base_score = max((item.get("score", 0.0) for item in family_hits), default=0.0)
    reranked: list[dict[str, Any]] = []
    for item in family_hits:
        raw_support = lexical_support_for_tokens(raw_query_tokens, item)
        title = (item.get("title") or "").lower().strip()
        matched_bonus = 0.0
        if item.get("family_id") in matched_family_ids:
            matched_bonus = max(2.5, max_base_score * 0.6)
            if title == lower_query:
                matched_bonus = max(matched_bonus + 0.8, max_base_score + 2.0)
        score = round(item.get("score", 0.0) + matched_bonus + (0.45 * raw_support), 4)
        result = dict(item)
        diagnostics = dict(item.get("diagnostics", {}))
        diagnostics.update(
            {
                "raw_query_support": round(raw_support, 4),
                "matched_family_bonus": round(matched_bonus, 4),
            },
        )
        result["score"] = score
        result["diagnostics"] = diagnostics
        reranked.append(result)
    reranked.sort(key=lambda item: (item["score"], item["kind"], item["doc_id"]), reverse=True)
    return reranked


def rerank_pair_hits(
    raw_query_tokens: list[str],
    pair_hits: list[dict[str, Any]],
    thread_hits: list[dict[str, Any]],
    *,
    pair_focused: bool,
) -> list[dict[str, Any]]:
    thread_score_map = {
        item.get("thread_uid"): item.get("score", 0.0)
        for item in thread_hits
        if item.get("thread_uid")
    }
    top_thread_uid = thread_hits[0].get("thread_uid") if thread_hits else None
    reranked: list[dict[str, Any]] = []
    for item in pair_hits:
        thread_uid = item.get("thread_uid")
        raw_support = lexical_support_for_tokens(raw_query_tokens, item)
        canonical_bonus = 0.0
        if thread_uid and thread_uid == item.get("canonical_thread_uid"):
            canonical_bonus = 0.65 if pair_focused else 0.2
        thread_bonus = 0.18 * thread_score_map.get(thread_uid, 0.0)
        top_thread_bonus = 0.35 if thread_uid and thread_uid == top_thread_uid else 0.0
        score = round(
            item.get("score", 0.0)
            + (0.7 * raw_support)
            + thread_bonus
            + canonical_bonus
            + top_thread_bonus,
            4,
        )
        result = dict(item)
        diagnostics = dict(item.get("diagnostics", {}))
        diagnostics.update(
            {
                "raw_query_support": round(raw_support, 4),
                "thread_feedback_score": round(thread_score_map.get(thread_uid, 0.0), 4),
                "canonical_rerank_bonus": round(canonical_bonus, 4),
                "top_thread_bonus": round(top_thread_bonus, 4),
            },
        )
        result["score"] = score
        result["diagnostics"] = diagnostics
        reranked.append(result)
    reranked.sort(key=lambda item: (item["score"], item["kind"], item["doc_id"]), reverse=True)
    return reranked


def merge_rankings(rankings: list[list[dict[str, Any]]], *, limit: int) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ranking in rankings:
        for item in ranking:
            if item["doc_id"] in seen:
                continue
            seen.add(item["doc_id"])
            merged.append(item)
    merged.sort(key=lambda item: (item["score"], item["kind"], item["doc_id"]), reverse=True)
    return merged[:limit]


def search_documents_v4(
    root: Path,
    query: str,
    *,
    limit: int = 8,
    mode: str | None = None,
    corpus: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if corpus is None:
        corpus = build_documents(root)
    raw_query_tokens = tokenize(query)
    query_tokens = expand_query_tokens(query, corpus)
    matched_family_ids = matched_family_ids_for_query(query, corpus)
    pair_focused = mode is None and "pair" in raw_query_tokens

    family_kind_multiplier = {"family_brief": 1.9, "family_dossier": 1.6}
    thread_kind_multiplier = {"thread_semantic": 1.1, "thread": 1.0}
    pair_kind_multiplier = {"pair": 1.05}
    ledger_kind_multiplier = {"action": 1.15, "unresolved": 1.1, "timeline": 1.05}

    family_hits = rank_documents(
        query,
        query_tokens,
        corpus["family_docs"],
        limit=max(limit, 6),
        kind_multiplier=family_kind_multiplier,
    )
    if matched_family_ids:
        hit_family_ids = {h.get("family_id") for h in family_hits if h.get("family_id")}
        for doc in corpus["family_docs"]:
            fid = doc.get("family_id")
            if fid in matched_family_ids and fid not in hit_family_ids:
                family_hits.append({**doc, "score": 0.0, "diagnostics": {}, "snippet": ""})
                hit_family_ids.add(fid)
    family_hits = rerank_family_hits(query, raw_query_tokens, family_hits, matched_family_ids)
    top_family_scores: dict[str, float] = {}
    for item in family_hits[:4]:
        if item.get("family_id"):
            top_family_scores[item["family_id"]] = item["score"]
    top_family_ids = list(top_family_scores.keys())

    top_thread_context: dict[str, float] = {}
    for family_id, score in top_family_scores.items():
        canonical_thread_uid = corpus["family_canonical_thread_map"].get(family_id)
        if canonical_thread_uid:
            top_thread_context[canonical_thread_uid] = max(
                top_thread_context.get(canonical_thread_uid, 0.0), score
            )

    if mode == "family_brief":
        merged_hits = family_hits[:limit]
        return {
            "query": query,
            "query_tokens": tokenize(query),
            "expanded_query_tokens": query_tokens,
            "family_hits": family_hits[:limit],
            "thread_hits": [],
            "pair_hits": [],
            "ledger_hits": [],
            "hits": merged_hits,
            "family_focus": top_family_ids,
        }

    thread_candidates = corpus["thread_docs"]
    if mode is None and top_family_ids:
        thread_candidates = [
            item
            for item in thread_candidates
            if set(item.get("family_ids", [])) & set(top_family_ids)
        ] or thread_candidates
    thread_hits = rank_documents(
        query,
        query_tokens,
        thread_candidates,
        limit=max(limit, 8),
        family_context=top_family_scores,
        thread_context=top_thread_context,
        kind_multiplier=thread_kind_multiplier,
    )
    thread_hits = rerank_thread_hits(
        query,
        raw_query_tokens,
        thread_hits,
        pair_hits=[],
        pair_focused=pair_focused and bool(matched_family_ids),
    )

    top_thread_scores: dict[str, float] = {}
    for item in thread_hits[:6]:
        if item.get("thread_uid"):
            top_thread_scores[item["thread_uid"]] = item["score"]

    pair_candidates = corpus["pair_docs"]
    if mode is None and top_family_ids:
        pair_candidates = [
            item
            for item in pair_candidates
            if set(item.get("family_ids", [])) & set(top_family_ids)
        ] or pair_candidates
    pair_hits = rank_documents(
        query,
        query_tokens,
        pair_candidates,
        limit=max(limit, 8),
        family_context=top_family_scores,
        thread_context=top_thread_scores,
        kind_multiplier=pair_kind_multiplier,
    )
    thread_hits = rerank_thread_hits(
        query,
        raw_query_tokens,
        thread_hits,
        pair_hits,
        pair_focused=pair_focused and bool(matched_family_ids),
    )
    pair_hits = rerank_pair_hits(
        raw_query_tokens,
        pair_hits,
        thread_hits,
        pair_focused=pair_focused and bool(matched_family_ids),
    )

    ledger_candidates = corpus["ledger_docs"]
    if mode == "action":
        ledger_candidates = [item for item in ledger_candidates if item["kind"] == "action"]
    elif mode == "unresolved":
        ledger_candidates = [item for item in ledger_candidates if item["kind"] == "unresolved"]
    elif mode == "timeline":
        ledger_candidates = [item for item in ledger_candidates if item["kind"] == "timeline"]
    elif mode is None and top_family_ids:
        filtered = [
            item
            for item in ledger_candidates
            if set(item.get("family_ids", [])) & set(top_family_ids)
        ]
        if filtered:
            ledger_candidates = filtered
    ledger_hits = rank_documents(
        query,
        query_tokens,
        ledger_candidates,
        limit=max(limit, 6),
        family_context=top_family_scores,
        thread_context=top_thread_scores,
        kind_multiplier=ledger_kind_multiplier,
    )

    if mode in {"action", "unresolved", "timeline"}:
        merged_hits = ledger_hits[:limit]
    else:
        merged_hits = merge_rankings(
            [family_hits, thread_hits, pair_hits, ledger_hits], limit=limit
        )

    return {
        "query": query,
        "query_tokens": tokenize(query),
        "expanded_query_tokens": query_tokens,
        "family_hits": family_hits[:limit],
        "thread_hits": thread_hits[:limit],
        "pair_hits": pair_hits[:limit],
        "ledger_hits": ledger_hits[:limit],
        "hits": merged_hits,
        "family_focus": top_family_ids,
    }


def select_primary_evidence(
    retrieval: dict[str, Any], *, mode: str | None = None
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    if mode in {"action", "unresolved", "timeline"}:
        candidates.extend(retrieval.get("ledger_hits", [])[:3])
    else:
        candidates.extend(retrieval.get("family_hits", [])[:2])
        candidates.extend(retrieval.get("thread_hits", [])[:2])
        candidates.extend(retrieval.get("pair_hits", [])[:2])
        candidates.extend(retrieval.get("ledger_hits", [])[:1])

    seen: set[str] = set()
    for item in candidates:
        if item["doc_id"] in seen:
            continue
        seen.add(item["doc_id"])
        evidence.append(
            {
                "kind": item["kind"],
                "doc_id": item["doc_id"],
                "title": item.get("title"),
                "score": item["score"],
                "snippet": item.get("snippet") or "",
                "citations": item.get("citations", []),
                "diagnostics": item.get("diagnostics", {}),
            },
        )
        if len(evidence) >= 4:
            break
    return evidence


def determine_answer_state(
    retrieval: dict[str, Any], *, mode: str | None = None
) -> tuple[str, float, str]:
    top_hit = (retrieval.get("hits") or [None])[0]
    top_family = (retrieval.get("family_hits") or [None])[0]
    top_thread = (retrieval.get("thread_hits") or [None])[0]
    top_pair = (retrieval.get("pair_hits") or [None])[0]
    raw_query_tokens = retrieval.get("query_tokens") or []
    diagnostics = (top_hit or {}).get("diagnostics", {})
    lexical_signal = max(
        diagnostics.get("coverage", 0.0),
        diagnostics.get("title_boost", 0.0),
        diagnostics.get("text_boost", 0.0),
    )
    lexical_signal = max(lexical_signal, lexical_support_for_tokens(raw_query_tokens, top_hit))
    semantic_signal = diagnostics.get("vector_score", 0.0) + diagnostics.get("phrase_boost", 0.0)

    if not top_hit or top_hit.get("score", 0.0) < 1.0:
        return "abstain", 0.2, "No document scored strongly enough for a reliable answer."
    if lexical_signal < 0.34 and semantic_signal <= 0.2:
        return (
            "abstain",
            0.18,
            "Top-ranked evidence lacks enough lexical or semantic support for the query.",
        )
    if mode in {"action", "unresolved", "timeline"}:
        if top_hit.get("score", 0.0) >= 1.3:
            return (
                "grounded",
                min(0.95, 0.55 + (top_hit["score"] / 6.0)),
                "Top ledger evidence is strong.",
            )
        return (
            "limited",
            min(0.8, 0.45 + (top_hit["score"] / 8.0)),
            "The ledger match is usable but not deep.",
        )
    if (
        top_family
        and top_thread
        and top_family.get("score", 0.0) >= 1.6
        and top_thread.get("score", 0.0) >= 1.3
    ):
        confidence = min(
            0.97,
            0.55
            + (
                (top_family["score"] + top_thread["score"] + (top_pair or {}).get("score", 0.0))
                / 8.5
            ),
        )
        return "grounded", round(confidence, 4), "Family and thread evidence align."
    if top_family and top_family.get("score", 0.0) >= 1.4:
        confidence = min(0.84, 0.45 + (top_family["score"] / 7.0))
        return (
            "limited",
            round(confidence, 4),
            "Family-level evidence is stronger than thread-level evidence.",
        )
    if top_thread and top_thread.get("score", 0.0) >= 1.2:
        confidence = min(0.78, 0.4 + (top_thread["score"] / 7.5))
        return (
            "limited",
            round(confidence, 4),
            "Thread evidence exists, but synthesis support is thin.",
        )
    return (
        "abstain",
        0.3,
        "Evidence is present but too weak or ambiguous to support a confident answer.",
    )


def build_answer(
    query: str, retrieval: dict[str, Any], *, mode: str | None = None
) -> dict[str, Any]:
    hits = retrieval.get("hits", [])
    if not hits:
        return {
            "query": query,
            "answer_state": "abstain",
            "confidence": 0.0,
            "answer": "No strong corpus evidence matched the query.",
            "corpus_facts": [],
            "inference": [],
            "ambiguity": ["No supporting corpus documents were retrieved."],
            "citations": [],
            "evidence": [],
            "top_hits": [],
            "retrieval": retrieval,
        }

    top_family = (retrieval.get("family_hits") or [None])[0]
    top_thread = (retrieval.get("thread_hits") or [None])[0]
    top_pair = (retrieval.get("pair_hits") or [None])[0]
    top_ledger = (retrieval.get("ledger_hits") or [None])[0]

    answer_state, confidence, state_reason = determine_answer_state(retrieval, mode=mode)
    citations: list[str] = []
    corpus_facts: list[str] = []
    inference: list[str] = []
    ambiguity: list[str] = []
    answer_lines: list[str] = []

    if mode in {"action", "unresolved", "timeline"}:
        top_hit = hits[0]
        payload = top_hit["payload"]
        citations.extend(top_hit.get("citations", []))
        if top_hit["kind"] == "action":
            corpus_facts.append(
                f"Top action pressure is {payload.get('canonical_action') or payload.get('action_key')}.",
            )
            if payload.get("family_ids"):
                corpus_facts.append(
                    f"It is linked to families {', '.join(payload['family_ids'][:4])}.",
                )
        elif top_hit["kind"] == "unresolved":
            corpus_facts.append(
                f"Top unresolved item is {payload.get('canonical_question') or payload.get('question_key')}.",
            )
            if payload.get("why_unresolved"):
                ambiguity.append(payload["why_unresolved"])
        else:
            corpus_facts.append(
                f"Relevant doctrine movement targets {payload.get('to_title') or payload.get('to_thread_uid')}.",
            )
            theme_shift = payload.get("theme_shift") or {}
            inference.append(
                f"Theme movement adds {', '.join(theme_shift.get('added', [])[:4]) or 'n/a'} and removes {', '.join(theme_shift.get('removed', [])[:4]) or 'n/a'}.",
            )
    else:
        if top_family:
            payload = top_family["payload"]
            companion_dossier_payload = next(
                (
                    item["payload"]
                    for item in retrieval.get("family_hits", []) + retrieval.get("hits", [])
                    if item.get("kind") == "family_dossier"
                    and item.get("family_id") == top_family.get("family_id")
                ),
                {},
            )
            citations.extend(top_family.get("citations", []))
            corpus_facts.append(
                f"Best matching family is {payload.get('canonical_title') or payload.get('family_id')} spanning {payload.get('member_count') or payload.get('member_count', 'n/a')} threads.",
            )
            family_text = payload.get("brief_text") or payload.get("doctrine_summary")
            if family_text:
                corpus_facts.append(shorten(family_text, 260))
            family_actions = (
                payload.get("actions") or companion_dossier_payload.get("actions") or []
            )
            family_unresolved = (
                payload.get("unresolved") or companion_dossier_payload.get("unresolved") or []
            )
            if family_actions:
                first_action = family_actions[0]
                citations.append(f"action:{first_action.get('action_key')}")
                ambiguity.append(
                    f"Action pressure remains around {first_action.get('canonical_action') or first_action.get('action_key')}.",
                )
            if family_unresolved:
                first_question = family_unresolved[0]
                citations.append(f"question:{first_question.get('question_key')}")
                ambiguity.append(
                    f"Open question: {first_question.get('canonical_question') or first_question.get('question_key')}.",
                )
        if top_thread:
            citations.extend(top_thread.get("citations", []))
            payload = top_thread["payload"]
            thread_summary = payload.get("semantic_v3_summary") or payload.get("summary")
            corpus_facts.append(
                f"Top thread evidence is {top_thread.get('title') or top_thread.get('thread_uid')}.",
            )
            if thread_summary:
                corpus_facts.append(shorten(thread_summary, 220))
        if top_pair:
            citations.extend(top_pair.get("citations", []))
            inference.append(
                f"Pair-level evidence surfaces {shorten(top_pair.get('snippet') or top_pair.get('text') or '', 180) or top_pair['doc_id']}.",
            )
        if not top_thread and top_family:
            ambiguity.append(
                "Family synthesis is stronger than thread-specific retrieval for this query."
            )
        if not top_pair and answer_state != "abstain":
            ambiguity.append("Pair-level evidence is sparse for this query.")
        if top_ledger and top_ledger["kind"] == "unresolved":
            payload = top_ledger["payload"]
            citations.extend(top_ledger.get("citations", []))
            ambiguity.append(
                payload.get("canonical_question")
                or payload.get("question_key")
                or "An unresolved item remains open."
            )

    if answer_state == "abstain":
        answer_lines.append("Evidence is too weak or ambiguous to answer confidently.")
        if hits:
            answer_lines.append(
                f"Closest match was {hits[0].get('title') or hits[0]['doc_id']} but it does not clear the confidence threshold.",
            )
    else:
        answer_lines.extend(corpus_facts[:2])
        if inference:
            answer_lines.append(f"Inference: {inference[0]}")
        if ambiguity:
            answer_lines.append(f"Ambiguity: {ambiguity[0]}")

    evidence = select_primary_evidence(retrieval, mode=mode)
    top_hits = [
        {
            "kind": item["kind"],
            "doc_id": item["doc_id"],
            "title": item.get("title"),
            "score": item["score"],
            "citations": item.get("citations", []),
            "snippet": item.get("snippet") or "",
            "diagnostics": item.get("diagnostics", {}),
        }
        for item in hits[:6]
    ]

    citations = unique_preserve([item for item in citations if item and item != "thread:None"])
    if answer_state == "abstain":
        citations = []
        evidence = []
    return {
        "query": query,
        "answer_state": answer_state,
        "confidence": confidence,
        "state_reason": state_reason,
        "answer": "\n".join(answer_lines).strip(),
        "corpus_facts": corpus_facts,
        "inference": inference,
        "ambiguity": ambiguity,
        "citations": citations,
        "evidence": evidence,
        "top_hits": top_hits,
        "retrieval": {
            "family_focus": retrieval.get("family_focus", []),
            "query_tokens": retrieval.get("query_tokens", []),
            "expanded_query_tokens": retrieval.get("expanded_query_tokens", []),
            "family_hits": [
                {
                    "doc_id": item["doc_id"],
                    "score": item["score"],
                    "citations": item.get("citations", []),
                }
                for item in retrieval.get("family_hits", [])[:4]
            ],
            "thread_hits": [
                {
                    "doc_id": item["doc_id"],
                    "score": item["score"],
                    "citations": item.get("citations", []),
                }
                for item in retrieval.get("thread_hits", [])[:4]
            ],
            "pair_hits": [
                {
                    "doc_id": item["doc_id"],
                    "score": item["score"],
                    "citations": item.get("citations", []),
                }
                for item in retrieval.get("pair_hits", [])[:4]
            ],
        },
    }


def render_answer_text(answer: dict[str, Any]) -> str:
    lines = [
        "Answer",
        "",
        f"State: {answer.get('answer_state') or 'unknown'}",
        f"Confidence: {answer.get('confidence', 0.0):.2f}",
        f"Reason: {answer.get('state_reason') or 'n/a'}",
        "",
        answer.get("answer") or "No answer generated.",
        "",
        "Citations",
        "",
    ]
    if answer.get("citations"):
        lines.extend(f"- {item}" for item in answer["citations"])
    else:
        lines.append("No citations.")
    lines.extend(["", "Evidence", ""])
    if answer.get("evidence"):
        for item in answer["evidence"]:
            lines.append(
                f"- {item['kind']} {item['doc_id']} | score={item['score']:.4f} | snippet={item.get('snippet') or 'n/a'}",
            )
    else:
        lines.append("No evidence.")
    lines.extend(["", "Top Hits", ""])
    if answer.get("top_hits"):
        for item in answer["top_hits"]:
            lines.append(
                f"- {item['kind']} {item['doc_id']} | score={item['score']:.4f} | citations={', '.join(item['citations']) or 'n/a'}",
            )
    else:
        lines.append("No hits.")
    return "\n".join(lines)


def render_answer_markdown(answer: dict[str, Any]) -> str:
    lines = [
        "# Answer Dossier",
        "",
        f"- Generated: {datetime.now(timezone.utc).isoformat()}",
        f"- Query: {answer.get('query') or 'n/a'}",
        f"- State: {answer.get('answer_state') or 'unknown'}",
        f"- Confidence: {answer.get('confidence', 0.0):.4f}",
        f"- Reason: {answer.get('state_reason') or 'n/a'}",
        "",
        "## Answer",
        "",
        answer.get("answer") or "No answer generated.",
        "",
        "## Corpus Facts",
        "",
    ]
    if answer.get("corpus_facts"):
        lines.extend(f"- {item}" for item in answer["corpus_facts"])
    else:
        lines.append("No corpus facts isolated.")
    lines.extend(["", "## Inference", ""])
    if answer.get("inference"):
        lines.extend(f"- {item}" for item in answer["inference"])
    else:
        lines.append("No inference lines.")
    lines.extend(["", "## Ambiguity", ""])
    if answer.get("ambiguity"):
        lines.extend(f"- {item}" for item in answer["ambiguity"])
    else:
        lines.append("No ambiguity lines.")
    lines.extend(["", "## Citations", ""])
    if answer.get("citations"):
        lines.extend(f"- {item}" for item in answer["citations"])
    else:
        lines.append("No citations.")
    lines.extend(["", "## Evidence", ""])
    if answer.get("evidence"):
        for item in answer["evidence"]:
            lines.append(
                f"- {item['kind']} {item['doc_id']} | score={item['score']:.4f} | citations={', '.join(item.get('citations', [])) or 'n/a'}",
            )
            lines.append(f"  snippet: {item.get('snippet') or 'n/a'}")
    else:
        lines.append("No evidence.")
    return "\n".join(lines)


def save_answer_dossier(root: Path, answer: dict[str, Any]) -> dict[str, str]:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    date_dir = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    slug = slugify(answer.get("query") or "query")
    answers_dir = root / "reports" / "answers" / date_dir
    json_path = answers_dir / f"{timestamp}-{slug}.json"
    md_path = answers_dir / f"{timestamp}-{slug}.md"
    write_json(json_path, answer)
    write_markdown(md_path, render_answer_markdown(answer))
    return {"json_path": str(json_path), "markdown_path": str(md_path)}
