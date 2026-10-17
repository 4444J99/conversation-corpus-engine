from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Any

from .archive_ingest import ArchivePaths, iter_store
from .archive_model import Conversation, iter_jsonl, write_json_atomic, write_jsonl_atomic
from .archive_text import term_counts

MIN_DF = 3
MAX_DF_RATIO = 0.12
TOP_TERMS = 8
VECTOR_TERMS = 40
SIMILAR_LIMIT = 5
SIMILAR_MIN = 0.30
HUB_MIN_DOCS = 3
HUB_LIMIT = 600
MESSAGE_CHARS = 20_000
DOCUMENT_CHARS = 300_000
TITLE_WEIGHT = 3
SKIPPED_KINDS = {"reasoning", "tool"}


@dataclass
class _Doc:
    uid: str
    provider: str
    counts: Counter[str]


def document_terms(conversation: Conversation) -> Counter[str]:
    pieces: list[str] = []
    budget = DOCUMENT_CHARS
    for message in conversation.messages:
        if message.meta.get("kind") in SKIPPED_KINDS or budget <= 0:
            continue
        chunk = message.text[: min(MESSAGE_CHARS, budget)]
        pieces.append(chunk)
        budget -= len(chunk)
    counts = term_counts("\n".join(pieces))
    for term, count in term_counts(conversation.title).items():
        counts[term] += count * TITLE_WEIGHT
    return Counter({term: count for term, count in counts.items() if count >= 2})


def _vector(counts: Counter[str], df: Counter[str], total: int, max_df: int) -> dict[str, float]:
    scored: dict[str, float] = {}
    for term, tf in counts.items():
        frequency = df[term]
        if frequency < MIN_DF or frequency > max_df:
            continue
        scored[term] = (1.0 + math.log(tf)) * math.log(total / frequency)
    return scored


def build_analysis(paths: ArchivePaths) -> dict[str, Any]:
    docs: list[_Doc] = []
    df: Counter[str] = Counter()
    for conversation in iter_store(paths):
        counts = document_terms(conversation)
        docs.append(_Doc(conversation.uid, conversation.provider, counts))
        df.update(counts.keys())
    total = len(docs)
    max_df = max(MIN_DF + 1, int(total * MAX_DF_RATIO))
    tops: dict[str, list[str]] = {}
    vectors: dict[str, dict[str, float]] = {}
    for doc in docs:
        scored = _vector(doc.counts, df, total, max_df)
        ranked = sorted(scored.items(), key=lambda item: (-item[1], item[0]))
        tops[doc.uid] = [term for term, _ in ranked[:TOP_TERMS]]
        head = dict(ranked[:VECTOR_TERMS])
        norm = math.sqrt(sum(weight * weight for weight in head.values())) or 1.0
        vectors[doc.uid] = {term: weight / norm for term, weight in head.items()}
    members: dict[str, list[str]] = {}
    for uid in sorted(tops):
        for term in tops[uid]:
            members.setdefault(term, []).append(uid)
    hub_terms = sorted(
        (term for term, uids in members.items() if len(uids) >= HUB_MIN_DOCS),
        key=lambda term: (-len(members[term]), term),
    )[:HUB_LIMIT]
    hubs = set(hub_terms)
    providers = {doc.uid: doc.provider for doc in docs}
    postings: dict[str, list[tuple[str, float]]] = {}
    for uid in sorted(vectors):
        for term, weight in vectors[uid].items():
            postings.setdefault(term, []).append((uid, weight))
    rows: list[dict[str, Any]] = []
    for uid in sorted(vectors):
        scores: Counter[str] = Counter()
        for term, weight in vectors[uid].items():
            for other, other_weight in postings[term]:
                if other != uid:
                    scores[other] += weight * other_weight
        ranked_similar = sorted(
            ((other, score) for other, score in scores.items() if score >= SIMILAR_MIN),
            key=lambda item: (-item[1], item[0]),
        )[:SIMILAR_LIMIT]
        rows.append(
            {
                "uid": uid,
                "topics": [term for term in tops[uid] if term in hubs],
                "terms": tops[uid],
                "similar": [
                    {"uid": other, "score": round(score, 4)} for other, score in ranked_similar
                ],
            }
        )
    write_jsonl_atomic(paths.analysis / "enrichment.jsonl", rows)
    hub_table = {
        term: {
            "documents": len(members[term]),
            "providers": dict(sorted(Counter(providers[u] for u in members[term]).items())),
        }
        for term in hub_terms
    }
    summary = {
        "documents": total,
        "vocabulary": len(df),
        "max_df": max_df,
        "hubs": len(hub_table),
        "documents_with_topics": sum(1 for row in rows if row["topics"]),
        "documents_with_similar": sum(1 for row in rows if row["similar"]),
        "parameters": {
            "min_df": MIN_DF,
            "max_df_ratio": MAX_DF_RATIO,
            "top_terms": TOP_TERMS,
            "similar_min": SIMILAR_MIN,
        },
    }
    write_json_atomic(paths.analysis / "topics.json", {"hubs": hub_table, "summary": summary})
    return summary


def load_enrichment(paths: ArchivePaths) -> dict[str, dict[str, Any]]:
    path = paths.analysis / "enrichment.jsonl"
    if not path.is_file():
        return {}
    return {row["uid"]: row for row in iter_jsonl(path)}
