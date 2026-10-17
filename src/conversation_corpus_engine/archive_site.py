from __future__ import annotations

import html
import json
from collections import Counter
from itertools import combinations
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import archive_layout as layout
from .archive_ingest import (
    PROVIDER_SPECS,
    ArchivePaths,
    iter_store,
    load_index,
    load_manifest,
    require_inside,
)
from .archive_model import Conversation, Message, write_text_atomic
from .archive_naming import NameLedger
from .archive_site_assets import (
    APP_JS,
    CSP_APP,
    CSP_PAGE,
    GRAPH_HTML,
    GRAPH_JS,
    INDEX_HTML,
    STYLE_CSS,
    fill_names,
)
from .archive_text import render_markdown_html
from .archive_topics import load_enrichment
from .corpus_store import CorpusWriteAuthorization

MESSAGE_CHARS = 200_000
SNIPPET_CHARS = 160
GRAPH_TOPICS = 250
GRAPH_EDGES = 1500
COLLAPSED_KINDS = {"reasoning", "tool", "source"}
ROLE_LABELS = {"user": "User", "assistant": "Assistant", "system": "System", "tool": "Tool"}


def _js(name: str, payload: Any) -> str:
    return f"window.{name} = {json.dumps(payload, ensure_ascii=True, sort_keys=True)};\n"


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _stamp(ts: str) -> str:
    return ts[:16].replace("T", " ") if ts else "undated"


def render_message_html(message: Message, limit: int = MESSAGE_CHARS) -> str:
    text = message.text
    notice = ""
    if len(text) > limit:
        notice = f'<p class="muted">{len(text) - limit} characters omitted on this page.</p>'
        text = text[:limit]
    kind = str(message.meta.get("kind") or "text")
    label = ROLE_LABELS.get(message.role, message.role.title())
    if kind not in {"text", "code"}:
        label = f"{label} · {kind}"
    attachments = ""
    names = [a.name for a in message.attachments if a.name]
    if names:
        attachments = (
            '<p class="attach">Attachments (names only): '
            + ", ".join(f"<code>{_esc(name)}</code>" for name in names)
            + "</p>"
        )
    body = f'<div class="body">{render_markdown_html(text)}</div>{notice}{attachments}'
    head = f"{_esc(label)} · {_esc(_stamp(message.ts))}"
    if kind in COLLAPSED_KINDS:
        return (
            f'<article class="msg {_esc(message.role)}"><details><summary>{head}</summary>'
            f"{body}</details></article>"
        )
    return f'<article class="msg {_esc(message.role)}"><header>{head}</header>{body}</article>'


def render_page(
    conversation: Conversation,
    *,
    related: list[dict[str, Any]],
    topics: list[str],
    titles: dict[str, str],
    limit: int = MESSAGE_CHARS,
) -> str:
    created = conversation.created or conversation.last_activity
    meta = [
        PROVIDER_SPECS[conversation.provider].display,
        conversation.kind,
        created[:10] or "undated",
        f"{conversation.message_count} messages",
        f"{conversation.word_count} words",
    ]
    if conversation.model:
        meta.append(conversation.model)
    if conversation.archived:
        meta.append("archived at source")
    home = f"../{layout.SITE_HOME_FILENAME}"
    topic_links = " ".join(
        f'<a class="topic" href="{home}#t={quote(term)}">{_esc(term)}</a>' for term in topics
    )
    related_html = ""
    if related:
        items = "".join(
            f'<li><a href="{_esc(layout.conversation_note_for_uid(row["uid"]))}.html">'
            f"{_esc(titles.get(row['uid'], row['uid']))}</a> "
            f'<span class="muted">({row["score"]:.2f})</span></li>'
            for row in related
            if row["uid"] in titles
        )
        related_html = f'<section class="related"><h2>Related</h2><ul>{items}</ul></section>'
    messages = "\n".join(render_message_html(m, limit) for m in conversation.messages)
    provenance = conversation.provenance
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta http-equiv="Content-Security-Policy" content="{_esc(CSP_PAGE)}">'
        '<meta name="referrer" content="no-referrer">'
        f"<title>{_esc(conversation.title or 'Untitled')}</title>"
        f'<link rel="stylesheet" href="../{layout.SITE_STYLE_FILENAME}"></head>'
        f'<body class="page"><div class="wrap"><nav class="top"><a href="{home}">'
        f"← Archive</a></nav><h1>{_esc(conversation.title or 'Untitled')}</h1>"
        f'<p class="muted"><span class="dot {_esc(conversation.provider)}"></span>'
        f"{_esc(' · '.join(meta))}</p>"
        f"<p>{topic_links}</p>"
        '<p class="banner">Conversation text is a record of speech acts, not of verified events.'
        "</p>"
        f"{related_html}\n{messages}\n"
        f'<p class="muted">Source: {_esc(provenance.get("source_file", ""))} · sha256 '
        f"{_esc(str(provenance.get('source_sha256', ''))[:16])}…</p>"
        "</div></body></html>\n"
    )


def _provider_summary(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for name, info in manifest["providers"].items():
        channels = info.get("channels") or {"read": [], "not_read": []}
        out.append(
            {
                "name": name,
                "display": PROVIDER_SPECS[name].display,
                "status": info.get("status", ""),
                "conversations": info.get("conversations", 0),
                "messages": info.get("messages", 0),
                "first": (info.get("created_min") or "")[:10],
                "last": (info.get("last_activity_max") or "")[:10],
                "read": channels["read"],
                "not_read": channels["not_read"],
            }
        )
    return out


def _topic_graph(
    rows: list[dict[str, Any]], enrichment: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    docs: Counter[str] = Counter()
    providers: dict[str, Counter[str]] = {}
    for row in rows:
        for term in enrichment.get(row["uid"], {}).get("topics", []):
            docs[term] += 1
            providers.setdefault(term, Counter())[row["provider"]] += 1
    chosen = [
        term for term, _ in sorted(docs.items(), key=lambda kv: (-kv[1], kv[0]))[:GRAPH_TOPICS]
    ]
    index = {term: i for i, term in enumerate(chosen)}
    pairs: Counter[tuple[int, int]] = Counter()
    for row in rows:
        present = sorted(
            index[t] for t in enrichment.get(row["uid"], {}).get("topics", []) if t in index
        )
        for a, b in combinations(present, 2):
            pairs[(a, b)] += 1
    edges = [
        [a, b, w]
        for (a, b), w in sorted(pairs.items(), key=lambda kv: (-kv[1], kv[0]))[:GRAPH_EDGES]
        if w >= 2
    ]
    nodes = [
        {
            "id": term,
            "label": term,
            "n": docs[term],
            "dominant": max(providers[term], key=lambda p: (providers[term][p], p)),
        }
        for term in chosen
    ]
    return {"nodes": nodes, "edges": edges}


def _prune(root: Path, written: set[str]) -> int:
    removed = 0
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_file() and str(path.relative_to(root)) not in written:
            path.unlink()
            removed += 1
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    return removed


def build_site(
    paths: ArchivePaths,
    authorization: CorpusWriteAuthorization,
    *,
    message_chars: int = MESSAGE_CHARS,
) -> dict[str, Any]:
    root = require_inside(authorization, paths.site)
    manifest = load_manifest(paths)
    rows = load_index(paths)
    titles = {row["uid"]: row["title"] for row in rows}
    enrichment = load_enrichment(paths)
    written: set[str] = set()
    ledger = NameLedger()

    def emit(rel: str, content: str) -> None:
        write_text_atomic(root / rel, content)
        written.add(rel)

    pages = 0
    for conversation in iter_store(paths):
        extra = enrichment.get(conversation.uid, {})
        name = layout.conversation_note(conversation.provider, conversation.uid)
        ledger.register(name, conversation.uid)
        emit(
            f"{layout.SITE_PAGES_DIRNAME}/{name}.html",
            render_page(
                conversation,
                related=extra.get("similar", []),
                topics=extra.get("topics", []),
                titles=titles,
                limit=message_chars,
            ),
        )
        pages += 1
    compact = [
        [
            row["uid"],
            row["provider"],
            row["kind"],
            row["title"],
            (row["created"] or row["last_activity"]),
            row["message_count"],
            row["snippet"][:SNIPPET_CHARS],
            enrichment.get(row["uid"], {}).get("topics", []),
        ]
        for row in rows
    ]
    emit(
        layout.SITE_INDEX_DATA_FILENAME,
        _js(
            "CCE",
            {
                "index": compact,
                "providers": _provider_summary(manifest),
                "totals": manifest["totals"],
                "fingerprint": manifest["source_fingerprint"],
            },
        ),
    )
    emit(layout.SITE_TOPIC_DATA_FILENAME, _js("CCE_GRAPH", _topic_graph(rows, enrichment)))
    emit(layout.SITE_HOME_FILENAME, fill_names(INDEX_HTML).replace("__CSP__", _esc(CSP_APP)))
    emit(layout.SITE_GRAPH_PAGE_FILENAME, fill_names(GRAPH_HTML).replace("__CSP__", _esc(CSP_APP)))
    emit(layout.SITE_STYLE_FILENAME, STYLE_CSS)
    emit(layout.SITE_HOME_SCRIPT_FILENAME, fill_names(APP_JS))
    emit(layout.SITE_GRAPH_SCRIPT_FILENAME, fill_names(GRAPH_JS))
    removed = _prune(root, written)
    return {"pages": pages, "files": len(written), "removed_stale": removed}
