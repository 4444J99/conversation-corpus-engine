"""CCE → aps brainstorm bridge.

The connector the pipeline was missing: it queries the federated conversation
corpora (ChatGPT, Claude, Perplexity — whatever is registered) for a subject's
brainstorm threads and materializes the matches as a markdown notes directory
that ``aps corpus ingest`` reads as provenance.

Re-runnable by design: run it again after new thinking lands and the notes dir
refreshes, so a living brainstorm keeps feeding an aps subject rather than being
pulled once and going stale.

The pulled threads are the owner's IP. This module never chooses where they
land — the caller passes ``--out`` (canonically an ARCA-sealed ``_*-private``
store, never a public tree, never gitignored).
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

from .answering import search_documents_v4


def _slugify(text: str, *, fallback: str = "thread") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (slug or fallback)[:80]


def gather_hits(
    corpus_roots: list[Path],
    queries: list[str],
    *,
    limit_per_query: int = 12,
    min_score: float = 0.0,
) -> dict[str, dict[str, Any]]:
    """Union of hits across every (root, query), deduped by thread_uid.

    A thread that matches several queries is kept once, at its highest score,
    with every matching query recorded so the note shows why it surfaced.
    """
    best: dict[str, dict[str, Any]] = {}
    for root in corpus_roots:
        corpus_id = root.name
        for query in queries:
            result = search_documents_v4(root, query, limit=limit_per_query)
            for hit in result.get("hits", []):
                score = float(hit.get("score") or 0.0)
                if score < min_score:
                    continue
                uid = hit.get("thread_uid") or hit.get("doc_id") or hit.get("title")
                if not uid:
                    continue
                existing = best.get(uid)
                if existing is None:
                    best[uid] = {
                        "uid": uid,
                        "title": hit.get("title") or uid,
                        "text": hit.get("text") or hit.get("snippet") or "",
                        "score": score,
                        "corpus": corpus_id,
                        "queries": {query},
                    }
                else:
                    existing["queries"].add(query)
                    if score > existing["score"]:
                        existing["score"] = score
                        existing["corpus"] = corpus_id
                        if len(hit.get("text") or "") > len(existing["text"]):
                            existing["text"] = hit.get("text") or existing["text"]
    return best


def write_notes(hits: dict[str, dict[str, Any]], out_dir: Path) -> list[Path]:
    """Write one markdown note per thread plus an index; return files written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    ordered = sorted(hits.values(), key=lambda h: h["score"], reverse=True)
    for hit in ordered:
        name = f"{_slugify(hit['title'])}--{_slugify(hit['uid'], fallback='t')[:12]}.md"
        path = out_dir / name
        queries = ", ".join(sorted(hit["queries"]))
        body = (
            f"# {hit['title']}\n\n"
            f"> Source corpus: **{hit['corpus']}** · relevance score: {hit['score']:.1f} · "
            f"matched query: _{queries}_\n"
            f"> Thread UID: `{hit['uid']}`\n\n"
            f"{hit['text'].strip()}\n"
        )
        path.write_text(body, encoding="utf-8")
        written.append(path)
    index = out_dir / "_brainstorm-index.md"
    lines = [
        "# Brainstorm bridge index",
        "",
        f"{len(ordered)} threads pulled from the conversation corpora by the CCE→aps bridge.",
        "Re-run the bridge to refresh; `aps corpus ingest` reads this dir as provenance.",
        "",
    ]
    for hit in ordered:
        lines.append(f"- **{hit['title']}** — {hit['corpus']} (score {hit['score']:.1f})")
    index.write_text("\n".join(lines) + "\n", encoding="utf-8")
    written.append(index)
    return written


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cce-aps-export",
        description="Export brainstorm threads from CCE corpora to an aps notes dir.",
    )
    parser.add_argument(
        "--root",
        dest="roots",
        action="append",
        required=True,
        type=Path,
        help="A materialized corpus root (repeatable). E.g. chatgpt-local-session-memory.",
    )
    parser.add_argument(
        "--query",
        dest="queries",
        action="append",
        required=True,
        help="A brainstorm query (repeatable). Threads matching any query are kept.",
    )
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="Output notes directory (an ARCA-sealed private store — never a public tree).",
    )
    parser.add_argument("--limit-per-query", type=int, default=12)
    parser.add_argument("--min-score", type=float, default=0.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    hits = gather_hits(
        args.roots,
        args.queries,
        limit_per_query=args.limit_per_query,
        min_score=args.min_score,
    )
    written = write_notes(hits, args.out)
    print(f"cce-aps-export: {len(hits)} threads → {len(written)} files in {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
