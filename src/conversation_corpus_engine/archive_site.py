from __future__ import annotations

import html
import json
import posixpath
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
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
from .archive_shard import part_folder, plan_conversation_folders, plan_flat_folders
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
from .archive_split import fit_blocks, pack, paginate_rows, utf8_size
from .archive_text import render_markdown_html
from .archive_topics import load_enrichment
from .archive_vault import incoming_links
from .archive_wiki import (
    BACKLINK_LABEL_CHARS,
    BACKLINK_LIMIT,
    TOC_MIN_HEADINGS,
    Backlinks,
    backlinks_by_uid,
    hub_label,
)
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
# A page lists its headings as a table of contents from TOC_MIN_HEADINGS of them; it lists at most
# TOC_MAX_ENTRIES of them, each label cut to TOC_LABEL_CHARS (the body keeps every heading whole).
# TOC_RESERVE is the room both page budgets keep for it, so a page that gains one still fits.
TOC_MAX_ENTRIES = 40
TOC_LABEL_CHARS = 60
TOC_RESERVE = 8192
# The most a "What links here" section can weigh (BACKLINK_LIMIT labelled links and about a dozen
# hub names), kept out of the single-page shortcut so a conversation near the cap is measured.
BACKLINKS_RESERVE = 20480
SINGLE_PAGE_MARGIN = 8192 + TOC_RESERVE + BACKLINKS_RESERVE
HEADING_RE = re.compile(r'<h[3-6] id="([^"]+)">(.*?)</h[3-6]>')
TAG_RE = re.compile(r"<[^>]+>")
# A deliberately long relative path and a deep prefix, used to measure the largest a page's own
# links can be before the real folder plan exists.
WORST_PREFIX = "../" * 8
WORST_PATH = (
    f"{layout.SITE_PAGES_DIRNAME}/month-all-202506/day-all-20250614/bucket-all-b999/"
    f"conversation-{'x' * 72}.html"
)
WORST_TOPIC_PATH = (
    f"{layout.SITE_HUBS_DIRNAME}/bucket-all-b999/bucket-all-b999/topic-all-{'x' * 40}.html"
)


def _js(name: str, payload: Any) -> str:
    return f"window.{name} = {json.dumps(payload, ensure_ascii=True, sort_keys=True)};\n"


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _stamp(ts: str) -> str:
    return ts[:16].replace("T", " ") if ts else "undated"


def message_html_blocks(message: Message, max_block: int, number: int = 0) -> list[str]:
    """One message as `<article>` blocks that each fit `max_block` bytes; nothing is dropped.

    `number` is the message's 1-based place in its conversation. When given, the first block
    carries the id `message-<number>` (the same fragment the library records use) and a later
    piece of a split message `message-<number>-piece-<k>`, so every message is a link target and
    no id repeats.
    """
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
        anchor = ""
        if number:
            anchor = f"message-{number}" if index == 0 else f"message-{number}-piece-{index + 1}"
        body = f'<div class="body">{render_markdown_html(piece, anchor)}</div>'
        if index == 0:
            body += attachments
        tag = f"{label} · piece {index + 1}/{total}" if total > 1 else label
        head = f"{_esc(tag)} · {_esc(_stamp(message.ts))}"
        ident = f' id="{anchor}"' if anchor else ""
        if kind in COLLAPSED_KINDS:
            return (
                f'<article class="msg {_esc(message.role)}"{ident}><details>'
                f"<summary>{head}</summary>{body}</details></article>"
            )
        return (
            f'<article class="msg {_esc(message.role)}"{ident}><header>{head}</header>{body}'
            "</article>"
        )

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
    topics: list[tuple[str, str]],
    related_html: str,
    body: str,
    nav: str,
    toc: str = "",
    backlinks: str = "",
) -> str:
    """One page: the document around a group of message blocks.

    `topics` are (term, link) pairs: the link goes to the topic's page, or filters the home page
    for a term that has no page.
    """
    title = conversation.title or "Untitled"
    heading = title if parts == 1 else f"{title} — part {part} of {parts}"
    provenance = conversation.provenance
    topic_links = " ".join(
        f'<a class="topic" href="{_esc(href)}">{_esc(term)}</a>' for term, href in topics
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
        f"{related_html}\n{toc}{nav}\n{body}\n{nav}\n{backlinks}"
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


def _topic_chips(
    topics: list[str], home_href: str, topic_href: Callable[[str], str] | None
) -> list[tuple[str, str]]:
    """Each topic term with the link its chip carries: its topic page when it has one."""
    return [
        (term, (topic_href(term) if topic_href else "") or f"{home_href}#t={quote(term)}")
        for term in topics
    ]


def backlinks_html(
    backlinks: Backlinks,
    titles: dict[str, str],
    href_for: Callable[[str], str],
    hub_href: Callable[[str], str] | None = None,
) -> str:
    """The "What links here" section of a conversation's first page.

    Hub notes that list the conversation are named, and linked when the site has a page for them
    (a topic hub has; `hub_href` answers `""` for the rest); the conversations that link to it
    are links, the first `BACKLINK_LIMIT` of them, with the number left over. The section is
    always present.
    """
    shown = backlinks.conversations[:BACKLINK_LIMIT]
    total = len(backlinks.conversations)

    def hub_item(name: str) -> str:
        target = hub_href(name) if hub_href else ""
        label = _esc(hub_label(name))
        inner = f'<a href="{_esc(target)}">{label}</a>' if target else label
        return f'<li class="hub" data-note="{_esc(name)}">{inner}</li>'

    items = "".join(hub_item(name) for name in backlinks.hubs)
    items += "".join(
        f'<li class="page"><a href="{_esc(href_for(uid))}">'
        f"{_esc(_short_label(titles.get(uid, uid)))}</a></li>"
        for uid in shown
    )
    if not items:
        body = '<p class="muted">No other page links here.</p>'
    else:
        body = f"<ul>{items}</ul>"
        if total > len(shown):
            body += f'<p class="muted">… and {total - len(shown)} more page(s) link here.</p>'
    return f'<section class="backlinks" data-total="{total}"><h2>What links here</h2>{body}</section>\n'


def _short_label(text: str) -> str:
    label = " ".join(str(text).split())
    if len(label) > BACKLINK_LABEL_CHARS:
        label = label[: BACKLINK_LABEL_CHARS - 1].rstrip() + "…"
    return label or "Untitled"


def page_headings(body: str) -> list[tuple[str, str]]:
    """The (anchor, plain label) of every heading a page body carries, in reading order."""
    found = []
    for anchor, inner in HEADING_RE.findall(body):
        label = " ".join(html.unescape(TAG_RE.sub("", inner)).split())
        if len(label) > TOC_LABEL_CHARS:
            label = label[: TOC_LABEL_CHARS - 1].rstrip() + "…"
        found.append((anchor, label or "Untitled"))
    return found


def _toc_html(headings: list[tuple[str, str]], shown: int) -> str:
    """A table of contents for a page's headings; empty below `TOC_MIN_HEADINGS` headings.

    Only the list is cut (to `shown` entries); the headings themselves stay whole in the body.
    """
    if len(headings) < TOC_MIN_HEADINGS or shown < 1:
        return ""
    items = "".join(
        f'<li><a href="#{_esc(anchor)}">{_esc(label)}</a></li>'
        for anchor, label in headings[:shown]
    )
    more = len(headings) - min(shown, len(headings))
    if more:
        items += f'<li class="muted">… and {more} more heading(s) on this page</li>'
    return (
        f'<nav class="toc" aria-label="Contents"><strong>Contents</strong><ol>{items}</ol></nav>\n'
    )


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
    backlinks: Backlinks | None = None,
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
    worst_topic = WORST_PREFIX + WORST_TOPIC_PATH
    worst_topics = _topic_chips(topics, worst_home, lambda term: worst_topic)
    worst_backlinks = backlinks_html(
        backlinks or Backlinks(),
        titles,
        lambda _uid: worst_link,
        lambda name: worst_topic if name.startswith("topic-") else "",
    )
    first = _shell(
        conversation,
        part=1,
        parts=99999,
        home_href=worst_home,
        style_href=worst_style,
        topics=worst_topics,
        related_html=_related_html(related, titles, lambda _uid: WORST_PREFIX + WORST_PATH),
        body="",
        nav=worst_nav,
        backlinks=worst_backlinks,
    )
    later = _shell(
        conversation,
        part=99999,
        parts=99999,
        home_href=worst_home,
        style_href=worst_style,
        topics=worst_topics,
        related_html="",
        body="",
        nav=worst_nav,
    )
    reserve = NAV_RESERVE + min(TOC_RESERVE, note_bytes // 8)
    first_budget = note_bytes - utf8_size(first) - reserve
    budget = note_bytes - utf8_size(later) - reserve
    if budget < 1024:
        # Too small to split into linked pages, so it can only hold a conversation that fits one
        # page: measured without the split reserve (a lone page carries no navigation).
        alone = _shell(
            conversation,
            part=1,
            parts=1,
            home_href=worst_home,
            style_href=worst_style,
            topics=worst_topics,
            related_html=_related_html(related, titles, lambda _uid: worst_link),
            body="",
            nav="",
            backlinks=worst_backlinks,
        )
        room = note_bytes - utf8_size(alone)
        if room >= 1024:
            single = pack(
                [
                    block
                    for number, message in enumerate(conversation.messages, 1)
                    for block in message_html_blocks(message, room, number)
                ],
                room,
                room,
            )
            if len(single) <= 1:
                return single
        raise ValueError(f"note_bytes {note_bytes} leaves no room for content beside the page")
    blocks = [
        block
        for number, message in enumerate(conversation.messages, 1)
        for block in message_html_blocks(message, budget, number)
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
    backlinks: Backlinks | None = None,
    topic_page: Callable[[str], str | None] | None = None,
) -> list[str]:
    """The pages of a conversation, each at most `note_bytes`, linked to each other.

    `topic_page` answers the site-root path of a topic hub's page (`topic-all-<term>`), or None
    when the site has none; a topic chip and a backlink to a topic hub link there.
    """
    total = len(groups)
    pages: list[str] = []
    for index, group in enumerate(groups):
        source = paths[index]
        previous = _relative(paths[index - 1], source) if index > 0 else ""
        following = _relative(paths[index + 1], source) if index + 1 < total else ""
        body = "\n".join(group)
        headings = page_headings(body)
        shown = min(len(headings), TOC_MAX_ENTRIES)
        home_href = _relative(layout.SITE_HOME_FILENAME, source)

        def hub_href(name: str, source: str = source) -> str:
            target = topic_page(name) if topic_page and name.startswith("topic-") else None
            return _relative(target, source) if target else ""

        def topic_href(term: str) -> str:
            name = layout.topic_note(term)
            return hub_href(name) if name else ""

        while True:
            page = _shell(
                conversation,
                part=index + 1,
                parts=total,
                home_href=home_href,
                style_href=_relative(layout.SITE_STYLE_FILENAME, source),
                topics=_topic_chips(topics, home_href, topic_href),
                related_html=(
                    _related_html(related, titles, _href_from(source, page_of))
                    if index == 0
                    else ""
                ),
                body=body,
                nav=_nav_html(total, index, _relative(paths[0], source), previous, following),
                toc=_toc_html(headings, shown),
                backlinks=(
                    backlinks_html(
                        backlinks or Backlinks(), titles, _href_from(source, page_of), hub_href
                    )
                    if index == 0
                    else ""
                ),
            )
            # The table of contents is navigation, not record: when a page would pass the cap it
            # is shortened (then dropped) before the page is refused.
            if utf8_size(page) <= note_bytes or shown < 1:
                break
            shown //= 2
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
    backlinks: Backlinks | None = None,
    topic_page: Callable[[str], str | None] | None = None,
) -> list[str]:
    """A conversation as pages, for a caller that is not planning folders (a test, a preview).

    The pages sit in a single folder, so their links resolve against each other and the site root.
    """
    cap = note_bytes or Limits().note_bytes
    groups = page_groups(
        conversation,
        related=related,
        topics=topics,
        titles=titles,
        note_bytes=cap,
        backlinks=backlinks,
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
        backlinks=backlinks,
        topic_page=topic_page,
    )


def _hub_shell(
    heading: str, intro: str, rows: str, *, home_href: str, style_href: str, nav: str
) -> str:
    """One hub page: a heading, what it holds, and a list of conversations."""
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
        f'<p class="muted">{_esc(intro)}</p>'
        '<p class="banner">Conversation text is a record of speech acts, not of verified events.'
        "</p>"
        f'{nav}\n<ul class="hub-rows">\n{rows}\n</ul>\n{nav}\n'
        "</div></body></html>\n"
    )


def _hub_row(row: dict[str, Any], href: str) -> str:
    when = (row.get("created") or row.get("last_activity") or "")[:10] or "undated"
    return (
        f'<li class="row"><a href="{_esc(href)}">{_esc(_short_label(row["title"]))}</a> '
        f'<span class="muted">— {_esc(row["provider"])} · {row["message_count"]} msg · '
        f"{_esc(when)}</span></li>"
    )


def topic_hub_plans(
    by_topic: dict[str, list[dict[str, Any]]], first_page: dict[str, str], note_bytes: int
) -> list[tuple[str, str, str, list[list[dict[str, Any]]]]]:
    """For each topic hub: (note name, title, intro, the rows of each of its pages).

    Rows are the vault hub's rows in the vault hub's order. A page is measured with the longest
    links it could carry (the folder plan does not exist yet), so every page fits `note_bytes`
    once its real links are written, and a topic that outgrows a page continues on the next.
    """
    worst_home = f"{WORST_PREFIX}{layout.SITE_HOME_FILENAME}"
    worst_style = f"{WORST_PREFIX}{layout.SITE_STYLE_FILENAME}"
    worst_link = WORST_PREFIX + WORST_PATH
    worst_nav = _nav_html(3, 1, worst_link, worst_link, worst_link)
    plans = []
    for term in sorted(by_topic):
        name = layout.topic_note(term)
        if name is None:
            continue
        items = sorted(
            by_topic[term],
            key=lambda r: (r.get("created") or r.get("last_activity") or "", r["uid"]),
        )
        title = f"Topic: {term}"
        intro = (
            f"{len(items)} conversations across {len({r['provider'] for r in items})} provider(s)."
        )
        kwargs = {"home_href": worst_home, "style_href": worst_style, "nav": worst_nav}
        first_budget = note_bytes - utf8_size(_hub_shell(title, intro, "", **kwargs)) - NAV_RESERVE
        budget = (
            note_bytes
            - utf8_size(_hub_shell(f"{title} — continued", "", "", **kwargs))
            - NAV_RESERVE
        )
        if budget < 1024:
            raise ValueError(f"note_bytes {note_bytes} leaves no room for rows beside a topic page")
        lines = [_hub_row(row, WORST_PREFIX + first_page[row["uid"]]) for row in items]
        grouped, start = [], 0
        for page in paginate_rows([], lines, first_budget, budget):
            grouped.append(items[start : start + len(page)])
            start += len(page)
        plans.append((name, title, intro, grouped))
    return plans


def render_topic_pages(
    plans: list[tuple[str, str, str, list[list[dict[str, Any]]]]],
    first_page: dict[str, str],
    folders: dict[str, str],
    note_bytes: int,
) -> Iterator[tuple[str, str]]:
    """Every topic page as (site-root path, html); the first page of a hub has the hub's name."""
    for name, title, intro, grouped in plans:
        names = topic_page_names(name, len(grouped))
        rels = [
            f"{layout.SITE_HUBS_DIRNAME}/{folders[each]}/{each}.html"
            if folders[each]
            else f"{layout.SITE_HUBS_DIRNAME}/{each}.html"
            for each in names
        ]
        for index, page_rows in enumerate(grouped):
            source = rels[index]
            nav = _nav_html(
                len(grouped),
                index,
                _relative(rels[0], source),
                _relative(rels[index - 1], source) if index else "",
                _relative(rels[index + 1], source) if index + 1 < len(grouped) else "",
            )
            text = _hub_shell(
                title if index == 0 else f"{title} — continued",
                intro if index == 0 else "",
                "\n".join(_hub_row(r, _relative(first_page[r["uid"]], source)) for r in page_rows),
                home_href=_relative(layout.SITE_HOME_FILENAME, source),
                style_href=_relative(layout.SITE_STYLE_FILENAME, source),
                nav=nav,
            )
            if utf8_size(text) > note_bytes:
                raise ValueError(
                    f"{name}: page {index + 1} is {utf8_size(text)} bytes, cap is {note_bytes}"
                )
            yield source, text


def topic_page_names(name: str, pages: int) -> list[str]:
    """The file stems of a topic's pages: the hub's own name, then its `part-...` continuations."""
    return [name] + [layout.part_name("all", number, name) for number in range(2, pages + 1)]


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
    # "What links here" is read from the vault's own wikilink graph, so the site and the vault
    # cannot disagree about which notes link to a conversation.
    backlinks = backlinks_by_uid(incoming_links(paths), titles)
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
            backlinks=backlinks.get(conversation.uid),
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

    # One static page per topic hub, planned now (the conversation pages link to them) and written
    # beside the conversations in the structural `hubs` folder.
    by_topic: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        for term in dict.fromkeys(enrichment.get(row["uid"], {}).get("topics", [])):
            if layout.topic_note(term):
                by_topic[term].append(row)
    topic_plans = topic_hub_plans(by_topic, first_page, limits.note_bytes)
    topic_names = {
        stem: name
        for name, _, _, grouped in topic_plans
        for stem in topic_page_names(name, len(grouped))
    }
    topic_folders = plan_flat_folders(topic_names, limits.folder_entries)
    topic_first = {
        name: (
            f"{layout.SITE_HUBS_DIRNAME}/{topic_folders[name]}/{name}.html"
            if topic_folders[name]
            else f"{layout.SITE_HUBS_DIRNAME}/{name}.html"
        )
        for name, _, _, _ in topic_plans
    }
    for rel, text in render_topic_pages(topic_plans, first_page, topic_folders, limits.note_bytes):
        emit(rel, text)

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
            backlinks=backlinks.get(conversation.uid),
            topic_page=topic_first.get,
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
    return {
        "pages": pages,
        "parts": parts,
        "topic_pages": len(topic_plans),
        "files": len(written),
        "removed_stale": removed,
    }
