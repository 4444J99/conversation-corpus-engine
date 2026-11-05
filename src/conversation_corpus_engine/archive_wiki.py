"""The wiki checks of `cce archive verify`: does the static site behave like a wiki?

The site is script-free per conversation, so everything a reader (or a link from another page)
can land on is plain HTML. This module reads the built site back and holds it to the promises the
build makes: every message is an anchor, a page with enough headings has a table of contents, and
no link, fragment included, points at nothing. Later checks (backlinks, topic pages, recent
changes) add their own keys to the same report.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from . import archive_layout as layout
from .archive_ingest import ArchivePaths, load_index
from .archive_naming import InvalidName, conversation_uid

MESSAGE_ID_RE = re.compile(r"message-(\d+)")
# The most problems of one kind a report lists; the counts above them say how many there were.
PROBLEM_LIMIT = 20
# A page lists its headings as a table of contents from this many (the build and the check agree).
TOC_MIN_HEADINGS = 3


class PageFacts(HTMLParser):
    """What one HTML page holds that another page or a link can rely on."""

    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.duplicate_ids: list[str] = []
        self.hrefs: list[str] = []
        self.classes: dict[str, int] = {}
        self.heading_ids: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        anchor = values.get("id")
        if anchor:
            if anchor in self.ids:
                self.duplicate_ids.append(anchor)
            self.ids.add(anchor)
            if tag in {"h3", "h4", "h5", "h6"}:
                self.heading_ids.append(anchor)
        if tag == "a" and values.get("href") is not None:
            self.hrefs.append(str(values["href"]))
        if tag in {"nav", "section"} and values.get("class"):
            key = f"{tag}.{values['class']}"
            self.classes[key] = self.classes.get(key, 0) + 1


def parse_page(path: Path) -> PageFacts:
    facts = PageFacts()
    facts.feed(path.read_text(encoding="utf-8"))
    facts.close()
    return facts


def _conversation_of(path: Path, root: Path) -> tuple[str, bool] | None:
    """The uid a conversation page belongs to and whether it is the first page, from its place.

    The first page is `conversation-<uid>.html`; a continuation page sits in a folder named like
    that note. Any other page (a hub, the home page) belongs to no conversation.
    """
    relative = path.relative_to(root)
    if relative.parts[0] != layout.SITE_PAGES_DIRNAME:
        return None
    owner = next(
        (part for part in reversed(relative.parts[:-1]) if part.startswith("conversation-")), ""
    )
    stem = path.stem
    try:
        if stem.startswith(layout.CONVERSATION_PREFIX):
            return conversation_uid(stem), True
        if owner:
            return conversation_uid(owner), False
    except InvalidName:
        return None
    return None


def _check_links(
    path: Path,
    root: Path,
    facts: PageFacts,
    wanted: dict[Path, set[str]],
    problems: list[str],
) -> tuple[int, int]:
    """Resolve every relative link of one page; a fragment must exist on the page it names."""
    checked = dangling = 0
    relative = path.relative_to(root).as_posix()
    for href in facts.hrefs:
        parts = urlsplit(href)
        if parts.scheme or parts.netloc:
            continue
        checked += 1
        if not parts.path and not parts.fragment:
            dangling += 1
            problems.append(f"dangling link in {relative}: {href!r} points at nothing")
            continue
        if "=" in parts.fragment:
            # `#t=<term>` is state the home page's script reads, not an anchor of any page.
            if parts.path:
                target = (path.parent / unquote(parts.path)).resolve()
                if root.resolve() not in target.parents or not target.is_file():
                    dangling += 1
                    problems.append(f"dangling link in {relative}: {href} (no such page)")
            continue
        if not parts.path:
            if parts.fragment not in facts.ids:
                dangling += 1
                problems.append(f"dangling link in {relative}: {href} (no such anchor)")
            continue
        target = (path.parent / unquote(parts.path)).resolve()
        if root.resolve() not in target.parents or not target.is_file():
            dangling += 1
            problems.append(f"dangling link in {relative}: {href} (no such page)")
        elif parts.fragment:
            wanted.setdefault(target, set()).add(parts.fragment)
    return checked, dangling


def verify_site_wiki(paths: ArchivePaths) -> dict[str, Any]:
    """The wiki block of `verify`: counts, and `problems` (empty when the site is sound)."""
    root = paths.site
    problems: list[str] = []
    report: dict[str, Any] = {
        "pages": 0,
        "messages": 0,
        "messages_anchored": 0,
        "toc_pages": 0,
        "links_checked": 0,
        "dangling_links": 0,
        "problems": problems,
    }
    if not root.is_dir():
        problems.append(f"site missing: {root}")
        return report
    expected = {row["uid"]: int(row["message_count"]) for row in load_index(paths)}
    report["messages"] = sum(expected.values())
    anchored: dict[str, int] = {}  # a bitmask of the message numbers a conversation's pages carry
    wanted: dict[Path, set[str]] = {}
    link_problems: list[str] = []
    page_problems: list[str] = []
    for path in sorted(root.rglob("*.html")):
        facts = parse_page(path)
        relative = path.relative_to(root).as_posix()
        checked, dangling = _check_links(path, root, facts, wanted, link_problems)
        report["links_checked"] += checked
        report["dangling_links"] += dangling
        if facts.duplicate_ids:
            page_problems.append(f"duplicate anchor ids in {relative}: {facts.duplicate_ids[:3]}")
        owner = _conversation_of(path, root)
        if owner is None:
            continue
        uid, first = owner
        if first:
            report["pages"] += 1
        mask = anchored.get(uid, 0)
        for anchor in facts.ids:
            match = MESSAGE_ID_RE.fullmatch(anchor)
            if match:
                bit = 1 << int(match.group(1))
                if mask & bit:
                    page_problems.append(f"{uid}: message anchor {anchor} appears twice")
                mask |= bit
        anchored[uid] = mask
        if len(facts.heading_ids) >= TOC_MIN_HEADINGS:
            if facts.classes.get("nav.toc") == 1:
                report["toc_pages"] += 1
            else:
                page_problems.append(
                    f"{relative}: {len(facts.heading_ids)} headings but no table of contents"
                )
    for uid, count in sorted(expected.items()):
        mask = anchored.get(uid, 0)
        missing = [n for n in range(1, count + 1) if not mask >> n & 1]
        extra = mask.bit_length() - 1 > count
        report["messages_anchored"] += count - len(missing)
        if missing or extra:
            page_problems.append(
                f"{uid}: message anchor(s) missing or extra: missing {missing[:5]}"
                f"{' and more' if len(missing) > 5 else ''}"
                f"{', anchors past the last message' if extra else ''}"
            )
    for target, fragments in wanted.items():
        found = parse_page(target).ids
        for fragment in sorted(fragments - found):
            report["dangling_links"] += 1
            link_problems.append(
                f"dangling link to {target.relative_to(root).as_posix()}#{fragment} (no such anchor)"
            )
    problems += _limited(page_problems)
    problems += _limited(link_problems)
    return report


def _limited(items: list[str]) -> list[str]:
    if len(items) <= PROBLEM_LIMIT:
        return items
    return [*items[:PROBLEM_LIMIT], f"... and {len(items) - PROBLEM_LIMIT} more"]
