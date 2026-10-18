"""Reader for saved Microsoft Copilot research pages (SingleFile browser captures).

A provider root is a folder of captures. Each saved page whose SingleFile header names the
`copilot.microsoft.com` host becomes one `Conversation`; every other file is left alone (non-HTML
files are only counted by suffix, never opened, and other HTML files are skipped and counted).

Read, from a Copilot page: the vendor id and surface in the saved URL, the session title and mode
badge, and the text of each message item (`group/<role>-message-item`) rendered as markdown:
headings, paragraphs, emphasis, links, lists, quotes, tables, code and the numbered source cards,
which also fill `meta["citations"]`.

Not read: page chrome (sidebar, share bar, dialogs, the SingleFile infobar, styles, scripts),
inline citation pills (stripped and counted), images, and everything outside a message item.

The `saved date:` line of the SingleFile header is the ONLY timestamp a page carries. It is the
capture time, not a message time, so it fills `updated` and `extra["captured_at"]`; `created` and
`Message.ts` stay empty. The observed pages hold one assistant item and no user turn: none is
invented (the session title is the conversation title, never a prompt), and the `user` item form is
the symmetric name, unobserved in real captures.

Pages are saved with compressed HTML, so optional end tags are missing and a parsed tree would nest
siblings. The page is therefore converted from a flat, linear stream of start/end/data events.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from .archive_model import Conversation, Message, SourceTracker

PROVIDER = "copilot"
READER = "archive_read_copilot@1"
HOST = "copilot.microsoft.com"
HEAD_BYTES = 8192
PAGE_SUFFIXES = {".html", ".htm"}
SKIP_TAGS = frozenset({"button", "svg", "style", "script", "template", "noscript", "dialog"})
VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param"}
    | {"source", "track", "wbr"}
)
BLOCK_TAGS = frozenset(
    {"p", "div", "section", "article", "header", "footer", "main", "aside", "figure"}
    | {"figcaption", "details", "summary", "dl", "dt", "dd"}
)
HEADINGS = {f"h{n}": n for n in range(1, 7)}
MARKS = {"strong": "**", "b": "**", "em": "*", "i": "*", "code": "`"}
CELL_TAGS = frozenset({"thead", "tbody", "tfoot", "tr", "td", "th"})
FLOW = ("root", "quote")
MAX_MARKS = 32
MAX_DEPTH = 32
CARD_CLASSES = frozenset({"list-none", "w-full"})
MONTHS = {
    name: number
    for number, name in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
        start=1,
    )
}

Event = tuple[str, str, dict[str, str]]

_NO_ATTRS: dict[str, str] = {}
_SPACE = re.compile(r"\s+")
_URL = re.compile(r"^[ \t]*url:[ \t]*(\S+)", re.MULTILINE)
_SAVED = re.compile(r"^[ \t]*saved date:[ \t]*(.+?)[ \t]*$", re.MULTILINE)
_STAMP = re.compile(
    r"[A-Za-z]{3} ([A-Za-z]{3}) (\d{1,2}) (\d{4}) (\d{2}):(\d{2}):(\d{2}) GMT([+-])(\d{2})(\d{2})"
)
_ITEM = re.compile(r"group/(.+)-message-item")
_DIGITS = re.compile(r"[0-9]{1,9}")
_PILL_BEFORE_PUNCTUATION = re.compile(r" *\x00[\x00 ]*(?=[.,;:!?)\]}])")
_EDGE = " \n\x00"


# --- files ------------------------------------------------------------------------------------


def _files(root: Path) -> list[Path]:
    """Every file under root (names only), dot-prefixed names skipped, in relative-path order."""
    found: list[Path] = []
    if not root.is_dir():
        return found
    for folder, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        found.extend(Path(folder, name) for name in names if not name.startswith("."))
    return sorted(found, key=lambda path: path.relative_to(root).parts)


def _html(root: Path) -> Iterator[Path]:
    return (path for path in _files(root) if path.suffix.lower() in PAGE_SUFFIXES)


def _head(path: Path) -> bytes | None:
    try:
        if not path.is_file():
            return None
        with path.open("rb") as handle:
            return handle.read(HEAD_BYTES)
    except OSError:
        return None


def _page_header(head: bytes) -> tuple[str, str] | None:
    """(url, saved-date text) of a SingleFile page saved from the Copilot host, else None."""
    _, found, rest = head.decode("utf-8", errors="replace").partition("Page saved with SingleFile")
    comment = rest.split("-->", 1)[0]
    match = _URL.search(comment) if found else None
    if match is None:
        return None
    try:
        parts = urlsplit(match.group(1))
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or parts.netloc.lower() != HOST:
        return None
    saved = _SAVED.search(comment)
    return match.group(1), saved.group(1) if saved else ""


def _page_info(path: Path) -> tuple[str, str] | None:
    head = _head(path)
    return None if head is None else _page_header(head)


def _saved_iso(text: str) -> str:
    """JavaScript `Date.toString()` (`Www Mmm DD YYYY HH:MM:SS GMT+HHMM (...)`) as UTC ISO."""
    match = _STAMP.search(text)
    month = MONTHS.get(match.group(1)) if match else None
    if match is None or month is None:
        return ""
    day, year, hour, minute, second, sign, off_h, off_m = match.groups()[1:]
    try:
        offset = timedelta(hours=int(off_h), minutes=int(off_m))
        zone = timezone(-offset if sign == "-" else offset)
        stamp = datetime(
            int(year), month, int(day), int(hour), int(minute), int(second), tzinfo=zone
        )
    except ValueError:
        return ""
    utc = stamp.astimezone(timezone.utc).replace(tzinfo=None)
    return utc.isoformat(timespec="seconds") + "Z"


def detect(root: Path) -> bool:
    return any(_page_info(path) is not None for path in _html(root))


def channels(root: Path) -> dict[str, list[str]]:
    pages = unrecognized = 0
    other: Counter[str] = Counter()
    for path in _files(root):
        suffix = path.suffix.lower()
        if suffix not in PAGE_SUFFIXES:
            other[suffix or "(none)"] += 1
        elif _page_info(path) is not None:
            pages += 1
        else:
            unrecognized += 1
    read = [
        f"{pages} saved Copilot page(s) (SingleFile .html) — report text, headings, tables, "
        "lists and source cards; the capture date is the only timestamp"
    ]
    not_read = (
        [f"{unrecognized} .html file(s) — not recognized as saved Copilot pages (not read)"]
        if unrecognized
        else []
    )
    not_read += [
        f"{count} {suffix} file(s) — not saved Copilot pages (names and bytes not read)"
        for suffix, count in sorted(other.items())
    ]
    return {"read": read, "not_read": not_read}


# --- events -----------------------------------------------------------------------------------


class _Events(HTMLParser):
    """Record a page as flat ("s", tag, attrs) / ("e", tag, {}) / ("d", text, {}) events."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.events: list[Event] = []
        self._raw = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.events.append(("s", tag, {name: value or "" for name, value in attrs}))
        if tag in {"script", "style"}:
            self._raw = tag

    def handle_endtag(self, tag: str) -> None:
        if tag == self._raw:
            self._raw = ""
        if tag not in VOID_TAGS:
            self.events.append(("e", tag, _NO_ATTRS))

    def handle_data(self, data: str) -> None:
        data = data.replace("\x00", "")
        if data and not self._raw:
            self.events.append(("d", data, _NO_ATTRS))


def _match_end(events: Sequence[Event], start: int, limit: int) -> int:
    """Index of the end tag balancing the start tag at `start`; `limit` when it never arrives."""
    tag, depth = events[start][1], 0
    for index in range(start, limit):
        kind, name, _ = events[index]
        if kind != "d" and name == tag:
            depth += 1 if kind == "s" else -1
            if not depth:
                return index
    return limit


def _has_class(attrs: dict[str, str], name: str) -> bool:
    return name in attrs.get("class", "").split()


def _plain(events: Sequence[Event], lo: int, hi: int) -> str:
    """Collapsed text of events[lo:hi], skipped subtrees left out."""
    out: list[str] = []
    skip, depth = "", 0
    for kind, value, _ in events[lo:hi]:
        if skip:
            if kind != "d" and value == skip:
                depth += 1 if kind == "s" else -1
                skip = skip if depth else ""
        elif kind == "s" and value in SKIP_TAGS:
            skip, depth = value, 1
        elif kind == "d":
            out.append(value)
    return _SPACE.sub(" ", "".join(out)).strip()


@dataclass
class _Item:
    name: str
    lo: int
    hi: int
    terminated: bool


def _item_name(attrs: dict[str, str]) -> str | None:
    for token in attrs.get("class", "").split():
        match = _ITEM.fullmatch(token)
        if match:
            return match.group(1)
    return None


def _items(events: Sequence[Event]) -> list[_Item]:
    """Message items in document order; an item ends at the `</div>` balancing its own `<div>`."""
    found: list[_Item] = []
    index, total = 0, len(events)
    while index < total:
        kind, tag, attrs = events[index]
        name = _item_name(attrs) if kind == "s" and tag == "div" else None
        if name is None:
            index += 1
            continue
        end = _match_end(events, index, total)
        found.append(_Item(name, index + 1, end, end < total))
        index = end + 1
    return found


def _header(events: Sequence[Event], lo: int, hi: int) -> tuple[int, int] | None:
    """Span of the first direct-child div of the item that holds a `text-3xl` element."""
    index = lo
    while index < hi:
        kind, tag, _ = events[index]
        if kind != "s" or tag != "div":
            index += 1
            continue
        end = min(_match_end(events, index, hi) + 1, hi)
        if any(
            events[j][0] == "s" and _has_class(events[j][2], "text-3xl") for j in range(index, end)
        ):
            return index, end
        index = end
    return None


def _titles(events: Sequence[Event], span: tuple[int, int]) -> tuple[str, str]:
    """(session title, mode badge text) of a header block."""
    lo, hi = span
    for index in range(lo, hi):
        if events[index][0] == "s" and _has_class(events[index][2], "text-3xl"):
            end = _match_end(events, index, hi)
            badge = f"{_plain(events, lo, index)} {_plain(events, end + 1, hi)}"
            return _plain(events, index + 1, end), _SPACE.sub(" ", badge).strip()
    return "", ""


def _first_h1(events: Sequence[Event]) -> str:
    for index, (kind, tag, _) in enumerate(events):
        if kind == "s" and tag == "h1":
            return _plain(events, index + 1, _match_end(events, index, len(events)))
    return ""


# --- markdown ---------------------------------------------------------------------------------


def _is_http(href: str) -> bool:
    return href.strip().lower().startswith(("http://", "https://"))


def _start(attrs: dict[str, str]) -> int:
    try:
        return int(attrs.get("start", "1"))
    except ValueError:
        return 1


@dataclass
class _Mark:
    """An open inline element: emphasis, code or a link (`href` empty when it renders as text)."""

    tag: str
    href: str = ""
    parts: list[str] = field(default_factory=list)


@dataclass
class _Card:
    segments: list[str] = field(default_factory=list)
    url: str = ""


@dataclass
class _Frame:
    """An open container: the page root, a blockquote, a list (items) or a table (rows)."""

    kind: str
    blocks: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    ordered: bool = False
    counter: int = 1
    level: int = 0
    item: tuple[str, str] | None = None
    rows: list[list[str]] = field(default_factory=list)
    row: list[str] = field(default_factory=list)
    cell: bool = False
    nested: int = 0


def _wrap(mark: _Mark) -> str:
    """Delimit the stripped inner text; edge whitespace (and pill markers) stay outside."""
    content = "".join(mark.parts)
    inner = content.strip(_EDGE)
    if not inner or (mark.tag == "a" and not mark.href):
        return content
    start = len(content) - len(content.lstrip(_EDGE))
    lead, trail = content[:start], content[start + len(inner) :]
    if mark.tag == "a":
        return f"{lead}[{inner}]({mark.href}){trail}"
    delimiter = MARKS[mark.tag]
    return f"{lead}{delimiter}{inner}{delimiter}{trail}"


def _tidy(text: str) -> str:
    """Drop pill markers (and the space before punctuation they leave), collapse whitespace."""
    text = _PILL_BEFORE_PUNCTUATION.sub("", text).replace("\x00", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _quote(blocks: list[str]) -> str:
    lines: list[str] = []
    for number, block in enumerate(blocks):
        if number:
            lines.append(">")
        lines.extend(f"> {line}" if line else ">" for line in block.split("\n"))
    return "\n".join(lines)


class _Renderer:
    """Turn an item's events into markdown blocks with an explicit container stack."""

    def __init__(self) -> None:
        self.stack = [_Frame("root")]
        self.parts: list[str] = []
        self.marks: list[_Mark] = []
        self.heading = 0
        self.card: _Card | None = None
        self.cards: list[dict[str, object]] = []
        self.pre: list[str] | None = None
        self.lang = ""
        self.skip = ""
        self.skip_depth = 0
        self.pills = 0
        self.tables = 0

    def run(self, events: Sequence[Event]) -> str:
        for kind, value, attrs in events:
            if kind == "d":
                self.text(value)
            elif kind == "s":
                self.start(value, attrs)
            else:
                self.end(value)
        return self.finish()

    # events

    def text(self, data: str) -> None:
        if self.skip:
            return
        if self.pre is not None:
            self.pre.append(data)
            return
        flat = _SPACE.sub(" ", data)
        if self.card is not None:
            if flat.strip():
                self.card.segments.append(flat.strip())
            return
        if flat.strip():
            self._open()
        self._put(flat)

    def start(self, tag: str, attrs: dict[str, str]) -> None:
        if self.skip:
            if tag == self.skip:
                self.skip_depth += 1
            return
        if tag in SKIP_TAGS:
            self.skip, self.skip_depth = tag, 1
            if tag == "button":
                self.pills += 1
                if self.card is None and self.pre is None:
                    self._put("\x00")
            return
        if self.pre is not None:
            self._start_pre(tag, attrs)
            return
        if self.card is not None:
            if tag == "li":
                self._li(attrs)
            elif tag == "a" and not self.card.url and _is_http(attrs.get("href", "")):
                self.card.url = attrs["href"].strip()
            return
        top = self.stack[-1]
        if tag in MARKS or tag == "a":
            href = attrs.get("href", "").strip() if tag == "a" else ""
            if len(self.marks) < MAX_MARKS:
                self.marks.append(_Mark(tag, href if _is_http(href) else ""))
        elif tag == "br":
            self._put(" " if self.heading or top.kind == "table" else "\n")
        elif tag in HEADINGS:
            if top.kind in FLOW:
                self._emit()
                self.heading = HEADINGS[tag]
            else:
                self._put(" ")
        elif tag in {"ul", "ol"}:
            self._list(tag, attrs)
        elif tag == "li":
            self._li(attrs)
        elif tag == "table":
            self._table()
        elif tag in CELL_TAGS:
            self._cells(tag)
        elif tag == "blockquote":
            if self._flow() and len(self.stack) < MAX_DEPTH:
                self._emit()
                self.stack.append(_Frame("quote"))
            else:
                self._put(" ")
        elif tag == "pre":
            if self._flow():
                self._emit()
                self.pre, self.lang = [], ""
            else:
                self._put(" ")
        elif tag == "hr":
            if self._flow():
                self._emit()
                self._add("---")
            else:
                self._put(" ")
        elif tag in BLOCK_TAGS:
            self._boundary()

    def end(self, tag: str) -> None:
        if self.skip:
            if tag == self.skip:
                self.skip_depth -= 1
                self.skip = self.skip if self.skip_depth else ""
            return
        if self.pre is not None:
            if tag == "pre":
                self._fence()
            return
        if self.card is not None and tag not in {"li", "ul", "ol"}:
            return
        top = self.stack[-1]
        if tag in MARKS or tag == "a":
            self._unmark(tag)
        elif tag in HEADINGS:
            if self.heading:
                self._emit()
                self.heading = 0
        elif tag == "li":
            self._flush_item()
        elif tag in {"ul", "ol"}:
            self._close(("list",))
        elif tag == "blockquote":
            self._close(("quote",))
        elif tag == "table":
            if top.kind == "table" and top.nested:
                top.nested -= 1
            else:
                self._close(("table",))
        elif tag in {"td", "th"}:
            if top.kind == "table" and not top.nested:
                self._close_cell(top)
        elif tag in CELL_TAGS:
            if top.kind == "table" and not top.nested:
                self._close_row(top)
        elif tag in BLOCK_TAGS:
            self._boundary()

    def finish(self) -> str:
        if self.pre is not None:
            self._fence()
        while len(self.stack) > 1:
            self._pop()
        self._emit()
        return "\n\n".join(self.stack[0].blocks)

    # inline buffer

    def _put(self, text: str) -> None:
        (self.marks[-1].parts if self.marks else self.parts).append(text)

    def _unmark(self, tag: str) -> None:
        if not any(mark.tag == tag for mark in self.marks):
            return
        while True:
            mark = self.marks.pop()
            self._put(_wrap(mark))
            if mark.tag == tag:
                return

    def _take(self) -> str:
        """Finished text of the buffer; open marks stay open but start over empty."""
        for depth in range(len(self.marks) - 1, -1, -1):
            mark = self.marks[depth]
            (self.marks[depth - 1].parts if depth else self.parts).append(_wrap(mark))
            mark.parts = []
        text, self.parts = "".join(self.parts), []
        return _tidy(text)

    def _open(self) -> None:
        """Stray text outside an item or cell opens an implicit one."""
        top = self.stack[-1]
        if top.kind == "list" and top.item is None:
            top.item = ("  ", "  ")
        elif top.kind == "table" and not top.nested:
            top.cell = True

    # blocks

    def _flow(self) -> bool:
        return not self.heading and self.stack[-1].kind in FLOW

    def _add(self, block: str) -> None:
        self.stack[-1].blocks.append(block)

    def _emit(self) -> None:
        text = self._take()
        if text:
            self._add(f"{'#' * self.heading} {text}" if self.heading else text)

    def _boundary(self) -> None:
        if self._flow():
            self._emit()
        else:
            self._put(" ")

    def _settle(self) -> None:
        top = self.stack[-1]
        if top.kind == "list":
            self._flush_item()
        elif top.kind == "table":
            self._close_row(top)
        else:
            self._emit()

    def _pop(self) -> None:
        """Close the innermost container into one block of the container below it."""
        self._settle()
        frame = self.stack.pop()
        if frame.kind == "quote":
            block = _quote(frame.blocks)
        elif frame.kind == "table":
            block = self._grid(frame.rows)
        elif self.stack[-1].kind == "list":
            return
        else:
            block = "\n".join(frame.lines)
        if block:
            self._add(block)

    def _close(self, kinds: tuple[str, ...]) -> None:
        if not any(frame.kind in kinds for frame in self.stack[1:]):
            return
        while True:
            kind = self.stack[-1].kind
            self._pop()
            if kind in kinds:
                return

    # code

    def _start_pre(self, tag: str, attrs: dict[str, str]) -> None:
        if tag == "code" and not self.lang:
            for token in attrs.get("class", "").split():
                if token.startswith("language-"):
                    self.lang = token.removeprefix("language-")
                    break
        elif tag == "br" and self.pre is not None:
            self.pre.append("\n")

    def _fence(self) -> None:
        content = "".join(self.pre or []).replace("\r\n", "\n")
        content = content.removeprefix("\n").removesuffix("\n")
        lang, self.pre, self.lang = self.lang, None, ""
        if content.strip():
            self._add(f"```{lang}\n{content}\n```")

    # lists and source cards

    def _list(self, tag: str, attrs: dict[str, str]) -> None:
        top = self.stack[-1]
        if len(self.stack) >= MAX_DEPTH:
            self._put(" ")
            return
        if top.kind == "list":
            self._flush_item()
        elif self._flow():
            self._emit()
        else:
            self._put(" ")
            return
        frame = _Frame("list", ordered=tag == "ol", counter=_start(attrs))
        if top.kind == "list":
            frame.lines, frame.level = top.lines, top.level + 1
        self.stack.append(frame)

    def _li(self, attrs: dict[str, str]) -> None:
        top = self.stack[-1]
        if top.kind == "list":
            self._flush_item()
        elif self._flow():
            self._emit()
            top = _Frame("list")
            self.stack.append(top)
        else:
            self._put(" ")
            return
        if set(attrs.get("class", "").split()) >= CARD_CLASSES:
            self.card = _Card()
            return
        marker = f"{top.counter}. " if top.ordered else "- "
        top.counter += 1
        top.item = (marker, " " * len(marker))

    def _flush_item(self) -> None:
        if self.card is not None:
            self._flush_card()
            return
        top = self.stack[-1]
        if top.kind != "list":
            return
        text = self._take()
        item, top.item = top.item, None
        if not text:
            return
        marker, rest = item or ("  ", "  ")
        indent = "  " * top.level
        first, *more = text.split("\n")
        top.lines.append(f"{indent}{marker}{first}")
        top.lines.extend(f"{indent}{rest}{line}" for line in more if line)

    def _flush_card(self) -> None:
        card, self.card = self.card, None
        if card is None:
            return
        segments = list(card.segments)
        index = int(segments.pop(0)) if segments and _DIGITS.fullmatch(segments[0]) else None
        site = ""
        if segments and "." in segments[0] and not _SPACE.search(segments[0]):
            site = segments.pop(0)
        title = " ".join(segments)
        if not (title or site or card.url):
            return
        number = index if index is not None else len(self.cards) + 1
        tail = f" — {site}" if site else ""
        if title and card.url:
            line = f"{number}. [{title}]({card.url}){tail}"
        elif title:
            line = f"{number}. {title}{tail}"
        else:
            line = f"{number}. {site or card.url}"
        top = self.stack[-1]
        top.lines.append("  " * top.level + line)
        self.cards.append({"index": index, "site": site, "text": title, "url": card.url})

    # tables

    def _table(self) -> None:
        top = self.stack[-1]
        if top.kind == "table":
            top.nested += 1
            self._put(" ")
        elif self._flow():
            self._emit()
            self.stack.append(_Frame("table"))
        else:
            self._put(" ")

    def _cells(self, tag: str) -> None:
        top = self.stack[-1]
        if top.kind != "table" or top.nested:
            self._put(" ")
        elif tag in {"td", "th"}:
            self._close_cell(top)
            top.cell = True
        else:
            self._close_row(top)

    def _close_cell(self, frame: _Frame) -> None:
        if frame.cell:
            frame.row.append(self._take().replace("|", "\\|"))
            frame.cell = False

    def _close_row(self, frame: _Frame) -> None:
        self._close_cell(frame)
        if frame.row:
            frame.rows.append(frame.row)
        frame.row = []

    def _grid(self, rows: list[list[str]]) -> str:
        if not rows:
            return ""
        self.tables += 1
        width = max(len(row) for row in rows)
        lines = [f"| {' | '.join(row + [''] * (width - len(row)))} |" for row in rows]
        lines.insert(1, f"| {' | '.join(['---'] * width)} |")
        return "\n".join(lines)


# --- conversations ----------------------------------------------------------------------------


def _conversation(path: Path, header: tuple[str, str], tracker: SourceTracker) -> Conversation:
    url, saved = header
    parser = _Events()
    parser.feed(path.read_bytes().decode("utf-8", errors="replace"))
    parser.close()
    events = parser.events
    tracker.note("pages_read")

    segments = [part for part in urlsplit(url).path.split("/") if part]
    source_id = segments[-1] if len(segments) > 1 else ""
    if not source_id:
        tracker.note("pages_without_url")
    captured = _saved_iso(saved)
    if not captured:
        tracker.note("saved_date_unparsed")

    messages: list[Message] = []
    title = mode = fallback = ""
    cards = tables = 0
    for item in _items(events):
        if not item.terminated:
            tracker.note("items_unterminated")
        span = _header(events, item.lo, item.hi)
        body = (
            events[item.lo : span[0]] + events[span[1] : item.hi]
            if span
            else events[item.lo : item.hi]
        )
        if span and not title:
            title, mode = _titles(events, span)
        fallback = fallback or _first_h1(body)
        role = {"ai": "assistant", "user": "user"}.get(item.name, "system")
        if role == "system":
            tracker.note(f"unknown_item_roles:{item.name}")
        renderer = _Renderer()
        text = renderer.run(body)
        meta: dict[str, object] = {"kind": "text"}
        if renderer.cards:
            meta["citations"] = renderer.cards
        messages.append(Message(role=role, text=text, meta=meta))
        cards += len(renderer.cards)
        tables += renderer.tables
        for name, amount in (
            ("citation_pills_stripped", renderer.pills),
            ("source_cards", len(renderer.cards)),
            ("tables", renderer.tables),
        ):
            if amount:
                tracker.note(name, amount)
    if not messages:
        tracker.note("pages_without_messages")

    return Conversation(
        provider=PROVIDER,
        source_id=source_id,
        title=title or fallback or "Untitled",
        updated=captured,
        kind="search",
        collection=segments[0] if segments else "",
        messages=messages,
        provenance=tracker.provenance(path),
        extra={
            "capture": "singlefile",
            "captured_at": captured,
            "mode": mode,
            "source_url": url,
            "sources": cards,
            "tables": tables,
        },
    )


def iter_conversations(root: Path, tracker: SourceTracker) -> Iterator[Conversation]:
    for path in _html(root):
        header = _page_info(path)
        if header is None:
            tracker.note("pages_skipped_not_copilot")
            continue
        yield _conversation(path, header, tracker)
