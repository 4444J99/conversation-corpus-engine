from __future__ import annotations

import html
import json
import posixpath
from collections import Counter
from collections.abc import Callable
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
from .archive_limits import Limits
from .archive_model import Conversation, Message, write_text_atomic
from .archive_naming import NameLedger
from .archive_shard import part_folder, plan_conversation_folders
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
from .archive_split import fit_blocks, pack, utf8_size
from .archive_text import render_markdown_html
from .archive_topics import load_enrichment
from .corpus_store import CorpusWriteAuthorization

SNIPPET_CHARS = 160
GRAPH_TOPICS = 250
GRAPH_EDGES = 1500
COLLAPSED_KINDS = {"reasoning", "tool", "source"}
ROLE_LABELS = {"user": "User", "assistant": "Assistant", "system": "System", "tool": "Tool"}
NAV_RESERVE = 1024
# HTML escaping turns one character into at most six (`&amp;` is five, `<br>` and the wrappers add
# a few per line), so text this far under the cap cannot reach it; such a conversation is one page
# and is not rendered twice to find that out.
HTML_GROWTH = 6
SINGLE_PAGE_MARGIN = 8192
# A deliberately long relative path and a deep prefix, used to measure the largest a page's own
# links can be before the real folder plan exists.
WORST_PREFIX = "../" * 8
WORST_PATH = (
    f"{layout.SITE_PAGES_DIRNAME}/month-all-202506/day-all-20250614/bucket-all-b999/"
    f"conversation-{'x' * 72}.html"
)


def _js(name: str, payload: Any) -> str:
    return f"window.{name} = {json.dumps(payload, ensure_ascii=True, sort_keys=True)};\n"


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _stamp(ts: str) -> str:
    return ts[:16].replace("T", " ") if ts else "undated"


def message_html_blocks(message: Message, max_block: int) -> list[str]:
    """One message as `<article>` blocks that each fit `max_block` bytes; nothing is dropped."""
    kind = str(message.meta.get("kind") or "text")
    label = ROLE_LABELS.get(message.role, message.role.title())
    if kind not in {"text", "code"}:
        label = f"{label} · {kind}"
    names = [a.name for a in message.attachments if a.name]
    attachments = ""
    if names:
        attachments = (
            '<p class="attach">Attachments (names only): '
            + ", ".join(f"<code>{_esc(name)}</code>" for name in names)
            + "</p>"
        )

    def render(piece: str, index: int, total: int) -> str:
        body = f'<div class="body">{render_markdown_html(piece)}</div>'
        if index == 0:
            body += attachments
        tag = f"{label} · piece {index + 1}/{total}" if total > 1 else label
        head = f"{_esc(tag)} · {_esc(_stamp(message.ts))}"
        if kind in COLLAPSED_KINDS:
            return (
                f'<article class="msg {_esc(message.role)}"><details><summary>{head}</summary>'
                f"{body}</details></article>"
            )
        return f'<article class="msg {_esc(message.role)}"><header>{head}</header>{body}</article>'

    return fit_blocks(message.text, render, max_block)


def render_message_html(message: Message, max_block: int | None = None) -> str:
    """The message as HTML, as one string (a very large message becomes several articles)."""
    return "\n".join(message_html_blocks(message, max_block or Limits().note_bytes // 2))


def _meta_line(conversation: Conversation) -> str:
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
    return " · ".join(meta)


def _shell(
    conversation: Conversation,
    *,
    part: int,
    parts: int,
    home_href: str,
    style_href: str,
    topics: list[str],
    related_html: str,
    body: str,
    nav: str,
) -> str:
    """One page: the document around a group of message blocks."""
    title = conversation.title or "Untitled"
    heading = title if parts == 1 else f"{title} — part {part} of {parts}"
    provenance = conversation.provenance
    topic_links = " ".join(
        f'<a class="topic" href="{home_href}#t={quote(term)}">{_esc(term)}</a>' for term in topics
    )
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta http-equiv="Content-Security-Policy" content="{_esc(CSP_PAGE)}">'
        '<meta name="referrer" content="no-referrer">'
        f"<title>{_esc(heading)}</title>"
        f'<link rel="stylesheet" href="{style_href}"></head>'
        f'<body class="page"><div class="wrap"><nav class="top"><a href="{home_href}">'
        f"← Archive</a></nav><h1>{_esc(heading)}</h1>"
        f'<p class="muted"><span class="dot {_esc(conversation.provider)}"></span>'
        f"{_esc(_meta_line(conversation))}</p>"
        f"<p>{topic_links}</p>"
        '<p class="banner">Conversation text is a record of speech acts, not of verified events.'
        "</p>"
        f"{related_html}\n{nav}\n{body}\n{nav}\n"
        f'<p class="muted">Source: {_esc(provenance.get("source_file", ""))} · sha256 '
        f"{_esc(str(provenance.get('source_sha256', ''))[:16])}…</p>"
        "</div></body></html>\n"
    )


def _related_html(
    related: list[dict[str, Any]], titles: dict[str, str], href_for: Callable[[str], str]
) -> str:
    if not related:
        return ""
    items = "".join(
        f'<li><a href="{_esc(href_for(row["uid"]))}">{_esc(titles.get(row["uid"], row["uid"]))}</a> '
        f'<span class="muted">({row["score"]:.2f})</span></li>'
        for row in related
        if row["uid"] in titles
    )
    return f'<section class="related"><h2>Related</h2><ul>{items}</ul></section>'


def _nav_html(total: int, index: int, start: str, previous: str, following: str) -> str:
    """The links that walk a split conversation: previous, the first page, next.

    Only the two neighbours and the first page are resolved by the caller, so a conversation of
    P pages costs P path computations, not P squared.
    """
    if total < 2:
        return ""
    pieces = []
    if index > 0:
        pieces.append(f'<a href="{_esc(previous)}">← Previous</a>')
    if index != 0:
        pieces.append(f'<a href="{_esc(start)}">Start</a>')
    if index + 1 < total:
        pieces.append(f'<a href="{_esc(following)}">Next →</a>')
    return f'<nav class="parts">{" · ".join(pieces)}</nav>'


def page_groups(
    conversation: Conversation,
    *,
    related: list[dict[str, Any]],
    topics: list[str],
    titles: dict[str, str],
    note_bytes: int,
) -> list[list[str]]:
    """Group a conversation's message blocks into pages that will fit `note_bytes` once wrapped.

    The wrapper's size is measured with the longest links it could carry, so the groups are
    final before the folder plan exists and a page can never outgrow the cap when its real
    (shorter) links are filled in.
    """
    worst_home = f"{WORST_PREFIX}{layout.SITE_HOME_FILENAME}"
    worst_style = f"{WORST_PREFIX}{layout.SITE_STYLE_FILENAME}"
    worst_link = WORST_PREFIX + WORST_PATH
    worst_nav = _nav_html(3, 1, worst_link, worst_link, worst_link)
    first = _shell(
        conversation,
        part=1,
        parts=99999,
        home_href=worst_home,
        style_href=worst_style,
        topics=topics,
        related_html=_related_html(related, titles, lambda _uid: WORST_PREFIX + WORST_PATH),
        body="",
        nav=worst_nav,
    )
    later = _shell(
        conversation,
        part=99999,
        parts=99999,
        home_href=worst_home,
        style_href=worst_style,
        topics=topics,
        related_html="",
        body="",
        nav=worst_nav,
    )
    first_budget = note_bytes - utf8_size(first) - NAV_RESERVE
    budget = note_bytes - utf8_size(later) - NAV_RESERVE
    if budget < 1024:
        raise ValueError(f"note_bytes {note_bytes} leaves no room for content beside the page")
    blocks = [
        block for message in conversation.messages for block in message_html_blocks(message, budget)
    ]
    return pack(blocks, first_budget, budget)


def single_page_certain(conversation: Conversation, note_bytes: int) -> bool:
    raw = sum(utf8_size(message.text) for message in conversation.messages)
    return raw * HTML_GROWTH + SINGLE_PAGE_MARGIN + 1024 * len(conversation.messages) < note_bytes


def page_paths(
    conversation: Conversation, folder: str, parts: int, folder_entries: int
) -> list[str]:
    """Where each page of a conversation lives, relative to the site root."""
    anchor = layout.conversation_note(conversation.provider, conversation.uid)
    base = f"{layout.SITE_PAGES_DIRNAME}/{folder}"
    paths = [f"{base}/{anchor}.html"]
    for position, number in enumerate(range(2, parts + 1)):
        sub = part_folder(position, parts - 1, folder_entries)
        name = layout.part_name(conversation.provider, number, anchor)
        paths.append(
            f"{base}/{anchor}/{sub}/{name}.html" if sub else f"{base}/{anchor}/{name}.html"
        )
    return paths


def _relative(target: str, source: str) -> str:
    return posixpath.relpath(target, start=posixpath.dirname(source))


def _href_from(source: str, page_of: Callable[[str], str | None]) -> Callable[[str], str]:
    """A link resolver for a page at `source`: the relative path to another conversation's page."""

    def href(uid: str) -> str:
        target = page_of(uid)
        return _relative(target, source) if target else "#"

    return href


def render_pages(
    conversation: Conversation,
    groups: list[list[str]],
    paths: list[str],
    *,
    related: list[dict[str, Any]],
    topics: list[str],
    titles: dict[str, str],
    page_of: Callable[[str], str | None],
    note_bytes: int,
) -> list[str]:
    """The pages of a conversation, each at most `note_bytes`, linked to each other."""
    total = len(groups)
    pages: list[str] = []
    for index, group in enumerate(groups):
        source = paths[index]
        previous = _relative(paths[index - 1], source) if index > 0 else ""
        following = _relative(paths[index + 1], source) if index + 1 < total else ""
        page = _shell(
            conversation,
            part=index + 1,
            parts=total,
            home_href=_relative(layout.SITE_HOME_FILENAME, source),
            style_href=_relative(layout.SITE_STYLE_FILENAME, source),
            topics=topics,
            related_html=(
                _related_html(related, titles, _href_from(source, page_of)) if index == 0 else ""
            ),
            body="\n".join(group),
            nav=_nav_html(total, index, _relative(paths[0], source), previous, following),
        )
        if utf8_size(page) > note_bytes:
            raise ValueError(
                f"{conversation.uid}: page {index + 1} is {utf8_size(page)} bytes, cap is "
                f"{note_bytes}"
            )
        pages.append(page)
    return pages


def render_page(
    conversation: Conversation,
    *,
    related: list[dict[str, Any]],
    topics: list[str],
    titles: dict[str, str],
    note_bytes: int | None = None,
) -> list[str]:
    """A conversation as pages, for a caller that is not planning folders (a test, a preview).

    The pages sit in a single folder, so their links resolve against each other and the site root.
    """
    cap = note_bytes or Limits().note_bytes
    groups = page_groups(
        conversation, related=related, topics=topics, titles=titles, note_bytes=cap
    )
    paths = page_paths(conversation, layout.UNDATED_DIRNAME, len(groups), Limits().folder_entries)
    return render_pages(
        conversation,
        groups,
        paths,
        related=related,
        topics=topics,
        titles=titles,
        page_of=lambda uid: (
            f"{layout.SITE_PAGES_DIRNAME}/{layout.UNDATED_DIRNAME}/"
            f"{layout.conversation_note_for_uid(uid)}.html"
        ),
        note_bytes=cap,
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


def count_conversation_pages(paths: ArchivePaths) -> int:
    """Conversation pages in the site (a split conversation counts once, not per part)."""
    if not paths.site_pages.is_dir():
        return 0
    return sum(1 for _ in paths.site_pages.rglob("conversation-*.html"))


def build_site(
    paths: ArchivePaths,
    authorization: CorpusWriteAuthorization,
    *,
    limits: Limits | None = None,
) -> dict[str, Any]:
    limits = limits or Limits()
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

    def groups_of(conversation: Conversation) -> list[list[str]]:
        extra = enrichment.get(conversation.uid, {})
        return page_groups(
            conversation,
            related=extra.get("similar", []),
            topics=extra.get("topics", []),
            titles=titles,
            note_bytes=limits.note_bytes,
        )

    # Pass one: how many pages each conversation takes, so the folder plan is exact.
    stamps = {row["uid"]: row["created"] or row["last_activity"] for row in rows}
    counts: dict[str, int] = {}
    for conversation in iter_store(paths):
        if single_page_certain(conversation, limits.note_bytes):
            counts[conversation.uid] = 1
        else:
            counts[conversation.uid] = len(groups_of(conversation))
    plan = plan_conversation_folders(
        [(uid, stamps.get(uid, ""), 1 if count == 1 else 2) for uid, count in counts.items()],
        limits.folder_entries,
    )
    first_page = {
        uid: f"{layout.SITE_PAGES_DIRNAME}/{plan[uid]}/{layout.conversation_note_for_uid(uid)}.html"
        for uid in counts
    }

    # Pass two: write.
    pages = parts = 0
    for conversation in iter_store(paths):
        extra = enrichment.get(conversation.uid, {})
        name = layout.conversation_note(conversation.provider, conversation.uid)
        ledger.register(name, conversation.uid)
        groups = groups_of(conversation)
        paths_ = page_paths(
            conversation, plan[conversation.uid], len(groups), limits.folder_entries
        )
        rendered = render_pages(
            conversation,
            groups,
            paths_,
            related=extra.get("similar", []),
            topics=extra.get("topics", []),
            titles=titles,
            page_of=first_page.get,
            note_bytes=limits.note_bytes,
        )
        for rel, text in zip(paths_, rendered, strict=True):
            emit(rel, text)
        pages += 1
        parts += len(rendered) - 1
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
            first_page.get(row["uid"], ""),
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
    return {"pages": pages, "parts": parts, "files": len(written), "removed_stale": removed}
