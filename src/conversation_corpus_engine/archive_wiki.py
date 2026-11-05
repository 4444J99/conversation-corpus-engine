"""The wiki checks of `cce archive verify`: does the static site behave like a wiki?

The site is script-free per conversation, so everything a reader (or a link from another page)
can land on is plain HTML. This module reads the built site back and holds it to the promises the
build makes: every message is an anchor, a page with enough headings has a table of contents, and
no link, fragment included, points at nothing. Later checks (backlinks, topic pages, recent
changes) add their own keys to the same report.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from . import archive_layout as layout
from .archive_author import AUTHORS
from .archive_ingest import ArchivePaths, load_index, load_manifest
from .archive_layout import CONVERSATION_PREFIX
from .archive_lineage import CHANGE_KINDS, history_by_uid, load_lineage
from .archive_naming import InvalidName, conversation_uid, parse_name
from .archive_vault import incoming_links, part_owners

MESSAGE_ID_RE = re.compile(r"message-(\d+)")
# A page lists at most this many conversations under "What links here" (and says how many more).
BACKLINK_LIMIT = 25
BACKLINK_LABEL_CHARS = 80


@dataclass(frozen=True)
class Backlinks:
    """What links to one conversation, from the vault's wikilink graph.

    `hubs` are the hub notes that list it (provider, month, topic, collection), by note name.
    `conversations` are the uids of the conversations whose notes link to it, in the order a page
    lists them (title, then uid); a page shows the first `BACKLINK_LIMIT`.
    """

    hubs: tuple[str, ...] = ()
    conversations: tuple[str, ...] = ()


# A page lists this many of its own revisions (and says how many earlier ones there were); the
# recent-changes page carries a row for each of the last RECENT_GENERATIONS generations, and a
# revision links to its row there while it is still in that window.
HISTORY_LIMIT = 8
RECENT_GENERATIONS = 20


@dataclass(frozen=True)
class History:
    """One conversation's revisions, newest first, from the lineage of generations.

    `entries` are (generation, kind) pairs, kind being `added`, `changed` or `removed`; `shown`
    are the generations the recent-changes page has a row for, so a revision can link to it.
    """

    entries: tuple[tuple[int, str], ...] = ()
    shown: frozenset[int] = frozenset()


def hub_label(name: str) -> str:
    """A reader's name for a hub note: `topic · lantern`, `month · 2025-06`, `provider · claude`."""
    parts = parse_name(name)
    subject = parts.subject or parts.scope or name
    if parts.kind == "month" and len(subject) == 6:
        subject = f"{subject[:4]}-{subject[4:]}"
    return f"{parts.kind} · {subject}"


def backlinks_by_uid(incoming: dict[str, set[str]], titles: dict[str, str]) -> dict[str, Backlinks]:
    """Turn the vault graph into one `Backlinks` per conversation that has a page.

    A source conversation with no page (it is not in the index) is left out, so every
    conversation listed has somewhere to point.
    """
    found: dict[str, Backlinks] = {}
    for target_note, sources in incoming.items():
        target = conversation_uid(target_note)
        hubs = tuple(sorted(name for name in sources if not name.startswith(CONVERSATION_PREFIX)))
        uids = [
            conversation_uid(name)
            for name in sources
            if name.startswith(CONVERSATION_PREFIX) and conversation_uid(name) in titles
        ]
        uids.sort(key=lambda uid: (titles[uid].casefold(), uid))
        found[target] = Backlinks(hubs=hubs, conversations=tuple(uids))
    return found


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
        # The "What links here" section: its total, its hub notes and the pages it links.
        self.backlink_total = -1
        self.backlink_hubs: list[str] = []
        self.backlink_pages: list[str] = []
        self._in_backlinks = False
        self._in_page_item = False
        # Message articles: how many carry no single author class, and the author of each
        # message's first piece (a split message has one first piece), by anchor.
        self.unlabelled_articles = 0
        self.authors: dict[str, str] = {}
        # The "History" section of a conversation (its total and (generation, kind) revisions),
        # and the rows of a recent-changes page as (uid, kind).
        self.history_total = -1
        self.revisions: list[tuple[int, str]] = []
        self.changes: list[tuple[str, str]] = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "li":
            self._in_page_item = False
        elif tag == "section":
            self._in_backlinks = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "article":
            found = [
                item.removeprefix("author-")
                for item in str(values.get("class") or "").split()
                if item.startswith("author-")
            ]
            if len(found) != 1 or found[0] not in AUTHORS:
                self.unlabelled_articles += 1
            elif MESSAGE_ID_RE.fullmatch(str(values.get("id"))):
                self.authors[str(values["id"])] = found[0]
        if tag == "section" and values.get("class") == "backlinks":
            self._in_backlinks = True
            self.backlink_total = int(values.get("data-total") or -1)
        elif self._in_backlinks and tag == "li":
            self._in_page_item = values.get("class") == "page"
            if values.get("class") == "hub":
                self.backlink_hubs.append(str(values.get("data-note")))
        elif self._in_backlinks and self._in_page_item and tag == "a":
            self.backlink_pages.append(str(values.get("href")))
        if tag == "section" and values.get("class") == "history":
            self.history_total = int(values.get("data-total") or -1)
        elif tag == "li" and values.get("class") == "revision":
            self.revisions.append(
                (int(values.get("data-generation") or -1), str(values.get("data-change")))
            )
        elif tag == "li" and values.get("class") == "change":
            self.changes.append((str(values.get("data-uid")), str(values.get("data-change"))))
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
        "authors": {author: 0 for author in AUTHORS},
        "topic_pages": 0,
        "topic_hubs": 0,
        "backlink_pages": 0,
        "history_pages": 0,
        "generations": 0,
        "recent_changes": 0,
        "links_listed": 0,
        "links_checked": 0,
        "dangling_links": 0,
        "problems": problems,
    }
    if not root.is_dir():
        problems.append(f"site missing: {root}")
        return report
    rows = load_index(paths)
    expected = {row["uid"]: int(row["message_count"]) for row in rows}
    report["messages"] = sum(expected.values())
    seen_backlinks: dict[str, PageFacts] = {}
    history_seen: dict[str, PageFacts] = {}
    hub_pages: dict[str, set[str]] = {}  # hub page file stem -> the conversations it lists
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
        if relative.startswith(f"{layout.SITE_HUBS_DIRNAME}/"):
            hub_pages[path.stem] = {
                _listed_uid(href)
                for href in facts.hrefs
                if Path(urlsplit(href).path).stem.startswith(CONVERSATION_PREFIX)
            }
            continue
        owner = _conversation_of(path, root)
        if owner is None:
            continue
        uid, first = owner
        if first:
            report["pages"] += 1
            count = facts.classes.get("section.backlinks", 0)
            if count == 1:
                report["backlink_pages"] += 1
                seen_backlinks[uid] = facts
            else:
                page_problems.append(f"{uid}: {count} backlinks section(s), expected exactly one")
            count = facts.classes.get("section.history", 0)
            if count == 1:
                report["history_pages"] += 1
                history_seen[uid] = facts
            else:
                page_problems.append(f"{uid}: {count} history section(s), expected exactly one")
        if facts.unlabelled_articles:
            page_problems.append(
                f"{uid}: {facts.unlabelled_articles} message article(s) in {relative} lack "
                "exactly one author class"
            )
        for author in facts.authors.values():
            report["authors"][author] += 1
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
    try:
        counted = load_manifest(paths).get("totals", {}).get("authors")
    except (FileNotFoundError, ValueError):
        counted = None
    if counted and {author: counted.get(author, 0) for author in AUTHORS} != report["authors"]:
        page_problems.append(
            f"site labels {report['authors']} turns by author, the manifest counts {counted}"
        )
    incoming = incoming_links(paths)
    page_problems += _backlink_problems(incoming, rows, seen_backlinks, report)
    page_problems += _topic_problems(hub_pages, incoming, report)
    page_problems += _lineage_problems(paths, root, history_seen, report)
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


def _listed_uid(href: str) -> str:
    try:
        return conversation_uid(Path(unquote(urlsplit(href).path)).stem)
    except InvalidName:
        return href


def _lineage_problems(
    paths: ArchivePaths, root: Path, history_seen: dict[str, PageFacts], report: dict[str, Any]
) -> list[str]:
    """Hold the recent-changes page and each page's history to the lineage of generations.

    The lineage must exist, chain from generation to generation and end at the manifest it
    belongs to; the recent-changes page must list exactly what the latest generation added,
    changed and removed; and every page's history must be the lineage's own account of it.
    """
    ledger = load_lineage(paths)
    report["generations"] = len(ledger)
    problems = []
    if not paths.lineage.is_file():
        problems.append("generation lineage missing: run `cce archive ingest`")
    for index, record in enumerate(ledger, 1):
        parent = ledger[index - 2]["fingerprint"] if index > 1 else ""
        if record.get("generation") != index or record.get("parent") != parent:
            problems.append(f"generation lineage is broken at generation {index}")
            break
    try:
        fingerprint = load_manifest(paths)["source_fingerprint"]
    except (FileNotFoundError, ValueError, KeyError):
        fingerprint = ""
    if ledger and fingerprint and ledger[-1]["fingerprint"] != fingerprint:
        problems.append(
            f"generation lineage is behind the manifest: its last generation is for "
            f"{ledger[-1]['fingerprint'][:12]}, the manifest is {fingerprint[:12]}"
        )
    first = root / layout.SITE_CHANGES_PAGE_FILENAME
    if not first.is_file():
        problems.append(f"recent changes page missing: {first.name}")
    else:
        pages = [first]
        number = 2
        while (
            part := root / f"{layout.part_name('all', number, layout.SITE_CHANGES_NAME)}.html"
        ).is_file():
            pages.append(part)
            number += 1
        listed: list[tuple[str, str]] = []
        window_ids: set[str] = set()
        for page in pages:
            facts = parse_page(page)
            listed += facts.changes
            window_ids |= {item for item in facts.ids if re.fullmatch(r"g\d+", item)}
        report["recent_changes"] = len(listed)
        if ledger:
            latest = ledger[-1]
            want = {(uid, kind) for kind in CHANGE_KINDS for uid in latest[kind]}
            if len(listed) != len(set(listed)) or set(listed) != want:
                problems.append(
                    f"recent changes lists {len(listed)} row(s), the latest generation "
                    f"({latest['generation']}) has {len(want)}: "
                    f"{len(want - set(listed))} missing, {len(set(listed) - want)} extra"
                )
            window = {f"g{record['generation']}" for record in ledger[-RECENT_GENERATIONS:]}
            if window_ids != window:
                problems.append("recent changes generation table differs from the lineage")
    revisions = history_by_uid(ledger)
    for uid, facts in sorted(history_seen.items()):
        want_entries = revisions.get(uid, [])
        if (
            facts.history_total != len(want_entries)
            or facts.revisions != want_entries[:HISTORY_LIMIT]
        ):
            problems.append(
                f"{uid}: history differs from the lineage (page lists {facts.revisions} of "
                f"{facts.history_total}; the lineage has {want_entries[:HISTORY_LIMIT]} of "
                f"{len(want_entries)})"
            )
    return problems


def _topic_problems(
    hub_pages: dict[str, set[str]], incoming: dict[str, set[str]], report: dict[str, Any]
) -> list[str]:
    """Hold the site's topic pages to the vault's topic hubs: the same set, each the same rows.

    A continuation page belongs to the topic whose name its own name hashes (the same rule the
    vault's continuation notes follow). The vault's rows are its hub notes' wikilinks.
    """
    owners = part_owners(set(hub_pages))
    on_site: dict[str, set[str]] = {}
    problems = []
    for stem, uids in sorted(hub_pages.items()):
        topic = stem if stem.startswith("topic-") else owners.get(stem, "")
        if not topic:
            problems.append(f"hub page {stem} belongs to no topic page")
            continue
        on_site.setdefault(topic, set()).update(uids)
    in_vault: dict[str, set[str]] = {}
    for target, sources in incoming.items():
        for source in sources:
            if source.startswith("topic-"):
                in_vault.setdefault(source, set()).add(conversation_uid(target))
    report["topic_pages"] = len(on_site)
    report["topic_hubs"] = len(in_vault)
    for name in sorted(in_vault.keys() - on_site.keys()):
        problems.append(f"topic hub {name} has no topic page on the site")
    for name in sorted(on_site.keys() - in_vault.keys()):
        problems.append(f"topic page {name} matches no topic hub in the vault")
    for name in sorted(on_site.keys() & in_vault.keys()):
        if on_site[name] != in_vault[name]:
            problems.append(
                f"topic page {name} lists {len(on_site[name])} conversation(s), its hub lists "
                f"{len(in_vault[name])} ({len(in_vault[name] - on_site[name])} missing, "
                f"{len(on_site[name] - in_vault[name])} extra)"
            )
    return problems


def _backlink_problems(
    incoming: dict[str, set[str]],
    rows: list[dict[str, Any]],
    seen: dict[str, PageFacts],
    report: dict[str, Any],
) -> list[str]:
    """Hold each page's "What links here" to the vault's own wikilink graph."""
    titles = {row["uid"]: row["title"] for row in rows}
    wanted = backlinks_by_uid(incoming, titles)
    problems = []
    for uid, facts in sorted(seen.items()):
        expected = wanted.get(uid, Backlinks())
        listed = [_listed_uid(href) for href in facts.backlink_pages]
        report["links_listed"] += len(listed)
        want = list(expected.conversations[:BACKLINK_LIMIT])
        if (
            facts.backlink_total != len(expected.conversations)
            or listed != want
            or sorted(facts.backlink_hubs) != sorted(expected.hubs)
        ):
            problems.append(
                f"{uid}: backlinks differ from the vault graph (page lists {len(listed)} of "
                f"{facts.backlink_total} and {len(facts.backlink_hubs)} hub(s); the vault has "
                f"{len(expected.conversations)} and {len(expected.hubs)} hub(s))"
            )
    return problems


def _limited(items: list[str]) -> list[str]:
    if len(items) <= PROBLEM_LIMIT:
        return items
    return [*items[:PROBLEM_LIMIT], f"... and {len(items) - PROBLEM_LIMIT} more"]
