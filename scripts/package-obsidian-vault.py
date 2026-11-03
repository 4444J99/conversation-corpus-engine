#!/usr/bin/env python3
"""Build a size-bounded, byte-reversible Obsidian reading derivative.

The source vault is immutable. Navigation is rewritten outside literal code;
inverse edits and exact rendered byte spans reconstruct every source file.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import posixpath
import re
import shutil
import signal
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

SCHEMA = "cce.obsidian-package.v1"
META = ".cce-package"
CHUNK = 1024**2
FLOOR = 1024**3
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})([^\r\n]*)")
HEADING = re.compile(r"^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$")
LINK = re.compile(r"\]\((<?)([^\s<>\r\n)]+)(>?)\)")
BACKTICKS = re.compile(r"`+")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(path):
    require(not any(p.is_symlink() for p in (path, *path.parents)), "symlink path refused")


@contextmanager
def deadline(seconds):
    require(
        hasattr(signal, "setitimer") and threading.current_thread() is threading.main_thread(),
        "hard deadline requires a main-thread POSIX process",
    )
    previous = signal.getsignal(signal.SIGALRM)
    timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()

    def expired(_signal, _frame):
        raise TimeoutError("Obsidian package execution deadline exceeded")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if timer[0]:
            signal.setitimer(
                signal.ITIMER_REAL, max(0.001, timer[0] - time.monotonic() + started), timer[1]
            )


class Budget:
    def __init__(self, output, maximum, seconds):
        self.output = output
        self.maximum = maximum
        self.seconds = seconds
        self.started = time.monotonic()
        self.written = 0
        self.next_check = 0.0
        self.free = shutil.disk_usage(output.parent).free

    def check(self, growth=0):
        now = time.monotonic()
        if now - self.started > self.seconds:
            raise TimeoutError("Obsidian package execution deadline exceeded")
        require(self.written + growth <= self.maximum, "output byte budget exceeded")
        if now >= self.next_check:
            self.free = shutil.disk_usage(self.output.parent).free
            self.next_check = now + 0.25
        require(self.free - growth >= FLOOR, "free-space floor reached")

    def charge(self, size):
        self.check(size)
        self.written += size
        self.free -= size


def private_dir(path):
    if not path.exists():
        private_dir(path.parent)
        path.mkdir(mode=0o700)
    require(path.is_dir() and not path.is_symlink(), "unsafe output directory")


@contextmanager
def private_file(path):
    private_dir(path.parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        yield stream


def write(path, data, budget):
    budget.charge(len(data))
    with private_file(path) as stream:
        stream.write(data)
    return {
        "path": str(path.relative_to(budget.output)),
        "bytes": len(data),
        "sha256": digest(data),
    }


def hash_file(path, budget=None):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b""):
            if budget:
                budget.check()
            result.update(chunk)
    return result.hexdigest()


def read_source(path, maximum):
    require(path.stat().st_size <= maximum, "source file exceeds configured byte limit")
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    require(len(raw) <= maximum, "source file grew beyond configured byte limit")
    return raw


def database_limit(connection, budget, accounted):
    """Enforce the remaining output/free-space cap before SQLite allocates pages."""
    budget.check()
    available = min(budget.maximum - budget.written, budget.free - FLOOR)
    maximum_pages = (accounted + available) // 4096
    require(maximum_pages >= 2, "output byte budget cannot contain navigation database")
    connection.execute(f"PRAGMA max_page_count={maximum_pages}")


def inline_spans(line):
    """Only an entire, equally long backtick run closes an inline-code span."""
    spans = []
    opener = None
    for match in BACKTICKS.finditer(line):
        if opener is None:
            prefix = line[: match.start()]
            backslashes = len(prefix) - len(prefix.rstrip("\\"))
            if backslashes % 2:
                continue
            opener = match
        elif len(match[0]) == len(opener[0]):
            spans.append((opener.start(), match.end()))
            opener = None
    if opener is not None:
        # Conservatively leave source text after an unfinished delimiter literal.
        spans.append((opener.start(), len(line)))
    return spans


def annotate_inline_boundaries(data, segments, *, check=None):
    """Use actual CommonMark spans, including multiline/container paragraphs."""
    path = Path(__file__).with_name("obsidian_markdown.py")
    spec = importlib.util.spec_from_file_location("obsidian_markdown_context", path)
    context = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(context)
    return context.annotate(data, segments, check=check)


def source_files(source, agent_entry=None):
    for root, directories, files in os.walk(source, followlinks=False):
        directories.sort()
        files.sort()
        for name in directories:
            require(not (Path(root) / name).is_symlink(), "source symlink refused")
        if Path(root) == source:
            directories[:] = [name for name in directories if name != ".obsidian"]
        for name in files:
            path = Path(root) / name
            require(
                path.is_file() and not path.is_symlink(), "source symlink or special file refused"
            )
            relative = path.relative_to(source).as_posix()
            require(not relative.startswith(META + "/"), "source uses reserved metadata directory")
            yield relative, path
    if agent_entry:
        require(not (source / "agent-entry.md").exists(), "agent-entry output collision")
        require(agent_entry.is_file(), "agent entry missing")
        yield "agent-entry.md", agent_entry


def unquote_line(line):
    depth = 0
    while True:
        match = re.match(r"^ {0,3}> ?", line)
        if not match:
            return line, depth
        line = line[match.end() :]
        depth += 1


_LIST_MARKER = re.compile(r"^ {0,3}(?:[-*+]|\d{1,9}[.)]) ")
# Raw HTML block type 1 (CommonMark): pre, script, style, and textarea
# end only at their matching closing tag, never at a blank line.
_RAW_OPEN = re.compile(r"^ {0,3}<(pre|script|style|textarea)(?:[\s>]|$)", re.IGNORECASE)
_PRE_OPEN = _RAW_OPEN
_COMMENT_OPEN = re.compile(r"^ {0,3}<!--")


def _strip_list_markers(line):
    """Strip leading list markers; return (rest, content indent or None).

    The content indent is the offset where the list item's content starts,
    so a fence on a continuation line can be measured relative to it.
    """
    indent = 0
    while True:
        match = _LIST_MARKER.match(line)
        if not match:
            return line, (indent or None)
        indent += match.end()
        line = line[match.end() :]


def _leading_indent(line):
    spaces = len(line) - len(line.lstrip(" "))
    if line[spaces : spaces + 1] == "\t":
        return spaces + 4
    return spaces


def advance_fence(line, state):
    probe, depth = unquote_line(line)
    previous, previous_depth = unquote_line(state or "")
    if state and depth < previous_depth:
        # Leaving a blockquote also leaves its fence: a fence line that
        # follows (possibly a list item's fence opener) must be read fresh
        # rather than missed because the old quoted state was still set.
        state = None
    if state is None:
        # A fence may open on a list item's own line ("- ```python"). List
        # markers are stripped only when opening: once inside a fence, a
        # marker-prefixed line is fence content, not a closing fence, and
        # the close must be a plain (possibly quoted) fence line.
        probe, _ = _strip_list_markers(probe)
    match = FENCE.match(probe)
    if not match:
        return state
    marker, tail = match.groups()
    if state is None:
        if marker[0] == "`" and "`" in tail:
            return None
        return "> " * depth + marker
    if (
        depth == previous_depth
        and marker[0] == previous[0]
        and len(marker) >= len(previous)
        and not tail.strip()
    ):
        return None
    return state


class _FenceTracker:
    """Shared container-aware fence tracking for validation and rewriting.

    Plain ``advance_fence`` misses fences that live inside containers: a
    fence opened on a list item's line ("- ```python") was missed, so its
    closing fence was misread as an opener, and a fence on a list
    continuation line (indented to the item's content) was missed entirely.
    Fence markers inside a raw HTML block type 1 (``<pre>``, ``<script>``,
    ``<style>``, ``<textarea>``) are likewise not fences: such a block
    ends only at its matching closing tag, never at a blank line, and
    markers inside an HTML comment (``<!--`` to ``-->``) are comment
    text, not fences. Validation (``fence_state``) already used this
    logic while link rewriting, anchor collection, and navigation growth
    used plain ``advance_fence``, so the two disagreed: a valid note with
    a commented fence failed packaging, and links after a list-contained
    fence were silently left unrewritten. All four now share this tracker,
    so a line is literal for every consumer or for none of them.

    A literal block that starts inside a list item or blockquote ends
    when a non-blank line leaves that container (a sibling-or-outer list
    marker, a line indented below the item's content indent, or a
    blockquote-depth change), as well as at its closing tag. Without
    that boundary, removing the old blank-line exit for ``<pre>`` would
    let an unclosed block inside a list item swallow every fence and
    link that follows the item. A blank line alone never ends a
    literal block: it does not end its container either.

    This tracker is an approximation, not a full CommonMark parser: it
    follows one list content indent at a time, only recognizes the
    type-1 tags above and whole-line comments (other raw HTML block
    types, e.g. ``<div>``, are not recognized), and a segment that
    starts inside a literal block is tracked as plain text until the
    next container boundary, exactly as before: rendered parts carry no
    literal opener in their header, so carrying literal state across a
    split would disagree with the rendered part's own parse and break
    its fence verification. Only ``list_indent`` and the fence's list
    prefix are carried across a split, and only when the segment
    starts inside a fence, where the part header reopens that fence
    with its prefix.
    """

    def __init__(self, state=None, list_indent=None, fence_prefix=None):
        self.state = state
        # Only set from a carried segment when state is not None (see
        # above): a fence-open segment's closing line is measured from
        # its list item's content indent, not from column zero.
        self.list_indent = list_indent
        # List prefix of the fence open at the segment start, e.g. "-   "
        # for a fence opened on a list item line. A split part's header
        # reopens its carried fence with this prefix, so the part parses
        # the content-indented closing line as a close, exactly as the
        # source did; without it, the part's column-zero opener would
        # leave the indented closer as code and strand the links after it.
        self.fence_prefix = fence_prefix if state is not None else None
        self.literal_tag = None  # "comment" or a type-1 tag name
        self.literal_quote_depth = None
        self.literal_list_indent = None

    def _enter_literal(self, tag, quote_depth, content_indent):
        self.literal_tag = tag
        self.literal_quote_depth = quote_depth
        self.literal_list_indent = (
            content_indent if content_indent is not None else self.list_indent
        )

    def _literal_end_tag(self):
        return "-->" if self.literal_tag == "comment" else f"</{self.literal_tag}>"

    def advance(self, line):
        """Advance one line; return (before, after, literal).

        Also maintains ``fence_prefix``: when a fence opens, remember
        the list prefix that opened it (reconstructed from the item's
        content indent, so any marker type is equivalent); when it
        closes, drop it. Callers that split text carry the prefix with
        the fence marker so a later part can reopen the same fence.
        """
        before = self.state
        result = self._advance_inner(line)
        if self.state is None:
            self.fence_prefix = None
        elif before is None and self.fence_prefix is None:
            # Plain fences carry no prefix; list fences carry the item
            # prefix that makes their content-indented closer close.
            self.fence_prefix = "-" + " " * (self.list_indent - 1) if self.list_indent else ""
        return result

    def _advance_inner(self, line):
        """Advance one line; return (before, after, literal).

        ``literal`` means the line is inside a raw HTML block or an HTML
        comment, so no link or heading on it may be rewritten or collected.
        Fence lines themselves are not flagged literal: callers already
        skip them because ``before`` or ``after`` is not None.
        """
        before = self.state
        probe, quote_depth = unquote_line(line)
        stripped, content_indent = _strip_list_markers(probe)
        if self.literal_tag is not None:
            if not line.strip():
                return before, self.state, True
            if self._literal_end_tag() in line.lower():
                self.literal_tag = None
                return before, self.state, True
            # Leaving the originating container ends the literal block.
            if quote_depth != self.literal_quote_depth:
                self.literal_tag = None
            elif self.literal_list_indent is not None and line.strip():
                if content_indent is not None:
                    if content_indent <= self.literal_list_indent:
                        self.literal_tag = None
                elif _leading_indent(probe) < self.literal_list_indent:
                    self.literal_tag = None
            if self.literal_tag is not None:
                return before, self.state, True
            # Fell through: this line left the container, so process it
            # normally below (it may open a fence or a new literal block).
        if self.state is None:
            if _COMMENT_OPEN.match(stripped):
                # A comment closed on its opening line is not an open block;
                # either way the line itself is comment text, not a fence.
                if "-->" not in stripped:
                    self._enter_literal("comment", quote_depth, content_indent)
                return before, self.state, True
            raw = _RAW_OPEN.match(stripped)
            if raw:
                # A type-1 element closed on its own opening line is not
                # an open block.
                tag = raw.group(1).lower()
                if f"</{tag}>" not in stripped.lower():
                    self._enter_literal(tag, quote_depth, content_indent)
                return before, self.state, True
            if content_indent is not None:
                self.list_indent = content_indent
            elif (
                self.list_indent is not None
                and line.strip()
                and _leading_indent(probe) < self.list_indent
            ):
                self.list_indent = None
        if content_indent is None and self.list_indent is not None and line.strip():
            indent = _leading_indent(probe)
            if indent < self.list_indent:
                self.state = advance_fence(line, self.state)
            else:
                # Re-express the line relative to the list item's content
                # indent, so a fence opened or closed on a continuation
                # line is tracked and indented code (4+ past content)
                # still is not. The quote prefix is kept: dropping it
                # would make a quoted fence's content lines reset the
                # state and its closing fence reopen one. Only a line
                # that actually looks like a fence may move the state;
                # other continuation lines inside a fence are content.
                adjusted = " " * (indent - self.list_indent) + probe.lstrip(" \t")
                if self.state is None and _COMMENT_OPEN.match(adjusted):
                    if "-->" not in adjusted:
                        self._enter_literal("comment", quote_depth, None)
                    return before, self.state, True
                if self.state is None:
                    raw = _RAW_OPEN.match(adjusted)
                    if raw:
                        tag = raw.group(1).lower()
                        if f"</{tag}>" not in adjusted.lower():
                            self._enter_literal(tag, quote_depth, None)
                        return before, self.state, True
                if FENCE.match(adjusted):
                    self.state = advance_fence("> " * quote_depth + adjusted, self.state)
            return before, self.state, False
        self.state = advance_fence(line, self.state)
        return before, self.state, False


def tracker_after(text, state=None, starts_line=True, list_indent=None, fence_prefix=None):
    """Track a text run; return (fence marker, list indent, fence prefix).

    ``list_indent`` and ``fence_prefix`` are honoured only when ``state``
    is not None: they are the carried context of a fence opened in an
    earlier split segment, so that segment's content-indented closing
    line is recognized and its part header can reopen the same fence.
    Literal state is deliberately not carried (see _FenceTracker).
    """
    tracker = _FenceTracker(
        state,
        list_indent=list_indent if state is not None else None,
        fence_prefix=fence_prefix if state is not None else None,
    )
    for number, line in enumerate(text.splitlines(keepends=True)):
        if not (number or starts_line):
            continue
        tracker.advance(line)
    return tracker.state, tracker.list_indent, tracker.fence_prefix


def fence_state(text, state=None, starts_line=True, list_indent=None):
    """Track fence state, including list-item and raw-HTML containers."""
    marker, _, _ = tracker_after(text, state, starts_line, list_indent)
    return marker


def require_closed_fences(data: bytes, target: str) -> None:
    """Fail closed when rendered Markdown ends inside an open fence.

    Split parts are exempt by construction: their wrappers close any fence the
    segment carries over. Anything else that ends open is a source defect the
    package must not ship, so it fails here instead of being repaired silently.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"rendered Markdown is not valid UTF-8: {target}") from None
    require(
        fence_state(text) is None,
        f"rendered Markdown ends inside an open fence: {target}",
    )


def part_path(relative, number, kind="part"):
    path = Path(relative)
    stem = path.stem
    suffix = f".__{kind}{number:06d}.md"
    if len((stem + suffix).encode()) > 240:
        raw = stem.encode()[:180]
        while True:
            try:
                stem = raw.decode() + "-" + digest(path.name.encode())[:16]
                break
            except UnicodeDecodeError:
                raw = raw[:-1]
    return path.with_name(stem + suffix).as_posix()


def navigation_growth(text, relative, state=None, starts_line=True, list_indent=None):
    """Upper bound for same-directory part names, including fragment-only links.

    Links without a fragment are not retargeted to a part, but an
    agent-entry link is rebased and percent-encoded when rewritten, so
    its growth is budgeted too: the quoted form of its path minus the
    original path length. Rebasing only shortens the vault-relative
    path, so the encoding growth of the original path bounds it.
    """
    growth = 0
    tracker = _FenceTracker(state, list_indent=list_indent if state is not None else None)
    for number, line in enumerate(text.splitlines(keepends=True)):
        before = state
        literal = False
        if number or starts_line:
            before, state, literal = tracker.advance(line)
        if (
            literal
            or before is not None
            or state is not None
            or unquote_line(line)[0].startswith(("    ", "\t"))
        ):
            continue
        literal = inline_spans(line)
        for match in LINK.finditer(line):
            if any(start <= match.start() < end for start, end in literal):
                continue
            parsed = urlsplit(match[2])
            if parsed.scheme or parsed.netloc:
                continue
            if not parsed.fragment:
                # Agent-entry rebasing percent-encodes the path even
                # without a fragment; budget that expansion, nothing more.
                if parsed.path:
                    growth += max(
                        0,
                        len(quote(unquote(parsed.path), safe="/._-~").encode())
                        - len(parsed.path.encode()),
                    )
                continue
            growth += (
                len(quote(Path(part_path(relative, 1)).name, safe="._-~"))
                if not parsed.path
                else 32
                + max(
                    0,
                    len(quote(unquote(parsed.path), safe="/._-~").encode())
                    - len(parsed.path.encode()),
                )
            )
    return growth


def ranges(data, body_limit, *, relative=None, maximum=None):
    """Prefer whole lines, but bound even a single unbroken UTF-8 line."""
    offset = 0
    state = None
    list_indent = None
    fence_prefix = None
    starts_line = True
    result = []
    while offset < len(data):
        candidate = min(body_limit, len(data) - offset)
        while True:
            end = offset + candidate
            while end < len(data) and data[end] & 0xC0 == 0x80:
                end -= 1
            while end < len(data) and end > offset and data[end - 1 : end + 1] == b"``":
                end -= 1
            if end < len(data):
                newline = data.rfind(b"\n", offset + candidate // 2, end)
                if newline >= 0:
                    end = newline + 1
            require(end > offset, "note byte limit cannot contain one UTF-8 character")
            raw = data[offset:end].decode("utf-8")
            after, after_indent, after_prefix = tracker_after(
                raw, state, starts_line, list_indent, fence_prefix
            )
            if maximum is None:
                break
            wrapper = (
                256
                + len(quote(Path(relative).name, safe="._-~"))
                + len(state or "")
                + len(after or "")
            )
            if (
                end
                - offset
                + wrapper
                + navigation_growth(raw, relative, state, starts_line, list_indent)
                <= maximum
            ):
                break
            require(candidate > 4, "navigation or fence wrapper exceeds note byte limit")
            candidate //= 2
        result.append(
            {
                "start": offset,
                "end": end,
                "before": state,
                "after": after,
                "list_indent_before": list_indent if state is not None else None,
                "fence_prefix_before": fence_prefix if state is not None else None,
                "starts_line": starts_line,
            }
        )
        starts_line = data[end - 1 : end] in {b"\n", b"\r"}
        state = after
        list_indent = after_indent
        fence_prefix = after_prefix
        offset = end
    return result or [
        {
            "start": 0,
            "end": 0,
            "before": None,
            "after": None,
            "list_indent_before": None,
            "fence_prefix_before": None,
            "starts_line": True,
        }
    ]


def heading_anchor(title):
    title = re.sub(r"[*_`~]", "", title).strip().lower()
    return re.sub(r"\s+", "-", title)


def populate_anchors(connection, relative, data, segments):
    for number, segment in enumerate(segments, 1):
        state = segment["before"]
        tracker = _FenceTracker(
            state, list_indent=segment.get("list_indent_before") if state is not None else None
        )
        text = data[segment["start"] : segment["end"]].decode()
        for line_number, line in enumerate(text.splitlines(keepends=True)):
            eligible = line_number > 0 or segment["starts_line"]
            before = state
            literal = False
            if eligible:
                before, state, literal = tracker.advance(line)
            if eligible and before is None and state is None and not literal:
                match = HEADING.match(line.rstrip("\r\n"))
                if match:
                    connection.execute(
                        "INSERT OR IGNORE INTO anchors VALUES (?,?,?)",
                        (relative, heading_anchor(match[1]), part_path(relative, number)),
                    )


def rewritten_target(target, relative, plans, connection, *, source, agent_entry):
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or parsed.path.startswith("/"):
        return target
    decoded = unquote(parsed.path)
    if "\\" in decoded or any(ord(c) < 32 for c in decoded):
        return target
    current_parent = posixpath.dirname(relative)
    resolved = posixpath.normpath(posixpath.join(current_parent, decoded)) if decoded else relative
    changed = False
    if agent_entry is not None and relative == "agent-entry.md" and decoded:
        # The relocated entry's own links were written relative to the
        # generation directory (the entry's original parent), not the vault
        # root: "vault/Home.md" names Home.md, its sibling in the package.
        # Rebase only targets that land inside the source vault; anything
        # else falls through to the normal rules below.
        candidate = Path(posixpath.normpath(posixpath.join(str(agent_entry.parent), decoded)))
        if candidate.is_relative_to(source):
            resolved = candidate.relative_to(source).as_posix()
            changed = True
    if agent_entry and (source / resolved).resolve() == agent_entry:
        resolved = "agent-entry.md"
        changed = True
    if resolved in plans and parsed.fragment:
        found = connection.execute(
            "SELECT part FROM anchors WHERE path=? AND anchor=?",
            (resolved, unquote(parsed.fragment).lower()),
        ).fetchone()
        if found:
            resolved = found[0]
            changed = True
    if not changed:
        return target
    local = posixpath.relpath(resolved, current_parent or ".")
    return urlunsplit(("", "", quote(local, safe="/._-~"), parsed.query, parsed.fragment))


def rewrite(raw, relative, segment, plans, connection, *, source, agent_entry):
    text = raw.decode("utf-8")
    output = []
    edits = []
    written = 0
    state = segment["before"]
    tracker = _FenceTracker(
        state, list_indent=segment.get("list_indent_before") if state is not None else None
    )
    for number, line in enumerate(text.splitlines(keepends=True)):
        eligible = number > 0 or segment["starts_line"]
        before = state
        literal = False
        if eligible:
            before, state, literal = tracker.advance(line)
        if (
            not eligible
            or literal
            or before is not None
            or state is not None
            or unquote_line(line)[0].startswith(("    ", "\t"))
        ):
            part = line.encode()
            output.append(part)
            written += len(part)
            continue
        literal = inline_spans(line)
        cursor = 0
        for match in LINK.finditer(line):
            if any(start <= match.start() < end for start, end in literal):
                continue
            target = match[2]
            replacement = rewritten_target(
                target, relative, plans, connection, source=source, agent_entry=agent_entry
            )
            if replacement == target:
                continue
            prefix = line[cursor : match.start(2)].encode()
            output.append(prefix)
            written += len(prefix)
            new = replacement.encode()
            edits.append(
                {
                    "offset": written,
                    "bytes": len(new),
                    "original": base64.b64encode(target.encode()).decode(),
                }
            )
            output.append(new)
            written += len(new)
            cursor = match.end(2)
        part = line[cursor:].encode()
        output.append(part)
        written += len(part)
    return b"".join(output), edits


def inline_suffix(marker, *, at_line_start):
    """Close inline code without starting a fenced block on a new line."""
    prefix = b"Inline code continues: " if at_line_start and len(marker) >= 3 else b" "
    return prefix + marker + b"\n"


def render_part(body, relative, number, count, segment):
    filename = quote(Path(relative).name, safe="._-~")
    header = f"# Part {number} of {count}\n\n[Source note index]({filename})\n\n".encode()
    if segment["before"]:
        # Reopen a carried fence with its list prefix (if any), inserted
        # after any quote prefix the marker already carries, so a
        # content-indented closing line closes it in this part too.
        opener = segment["before"]
        prefix = segment.get("fence_prefix_before") or ""
        if prefix:
            quote_prefix = ""
            rest = opener
            while rest.startswith("> "):
                quote_prefix += "> "
                rest = rest[2:]
            opener = quote_prefix + prefix + rest
        header += (opener + "\n").encode()
    elif segment.get("inline_before"):
        header += ("Inline code continued: " + segment["inline_before"] + " ").encode()
    footer = b""
    if segment["after"]:
        separator = "" if body.endswith((b"\n", b"\r")) else "\n"
        footer = (separator + segment["after"] + "\n").encode()
    elif segment.get("inline_after"):
        footer = inline_suffix(
            segment["inline_after"].encode(), at_line_start=body.endswith((b"\n", b"\r"))
        )
    return header + body + footer, len(header)


def indexes(relative, count, maximum):
    heading = "# Split source note\n\nEvery source byte is preserved across the linked parts.\n\n"
    links = [
        f"- [Part {number} of {count}]({quote(Path(part_path(relative, number)).name, safe='._-~')})\n"
        for number in range(1, count + 1)
    ]
    pages = []
    current = heading
    for link in links:
        if len((current + link).encode()) > maximum:
            require(current != heading, "note index link exceeds note byte limit")
            pages.append(current)
            current = heading
        current += link
    pages.append(current)
    if len(pages) == 1:
        return [(relative, pages[0].encode())]
    result = []
    home = heading
    for number, page in enumerate(pages, 1):
        target = part_path(relative, number, "index")
        result.append((target, page.encode()))
        home += f"- [Parts page {number}]({quote(Path(target).name, safe='._-~')})\n"
    require(
        len(home.encode()) <= maximum,
        "source note needs more index levels than the configured limit",
    )
    return [(relative, home.encode()), *result]


def inverse(body, edits):
    cursor = 0
    for edit in edits:
        offset = edit["offset"]
        require(cursor <= offset <= len(body), "invalid inverse edit offset")
        yield body[cursor:offset]
        yield base64.b64decode(edit["original"], validate=True)
        cursor = offset + edit["bytes"]
        require(cursor <= len(body), "invalid inverse edit size")
    yield body[cursor:]


def recover_row(output, row):
    total = hashlib.sha256()
    size = 0
    for piece in row["pieces"]:
        path = output / piece["path"]
        safe_path(path)
        data = path.read_bytes()
        require(
            len(data) == piece["bytes"] and digest(data) == piece["sha256"],
            "rendered piece size or hash mismatch",
        )
        body = data[piece["body_start"] : piece["body_start"] + piece["body_bytes"]]
        source = b"".join(inverse(body, piece["edits"]))
        require(digest(source) == piece["source_sha256"], "reconstructed segment hash mismatch")
        require(
            len(source) == piece["source_end"] - piece["source_start"],
            "reconstructed segment size mismatch",
        )
        require(piece["source_start"] == size, "source segment gap or overlap")
        size += len(source)
        total.update(source)
        yield source
    require(
        size == row["bytes"] and total.hexdigest() == row["sha256"],
        "source reconstruction hash mismatch",
    )


def reconstruct(output, relative):
    output = Path(output)
    with (output / META / "ledger.jsonl").open() as stream:
        for line in stream:
            row = json.loads(line)
            if row["path"] == relative:
                yield from recover_row(output, row)
                return
    raise ValueError("source file is absent from ledger")


def verify_package(output, *, budget=None, expected=None):
    output = Path(output)
    manifest = expected or json.loads((output / META / "manifest.json").read_text())
    ledger_path = output / META / "ledger.jsonl"
    require(hash_file(ledger_path, budget) == manifest["ledger_sha256"], "ledger digest mismatch")
    require(
        hash_file(output / META / "sources.jsonl", budget) == manifest["source_inventory_sha256"],
        "source inventory digest mismatch",
    )
    require(
        hash_file(output / META / "navigation.sqlite", budget) == manifest["navigation_sha256"],
        "navigation database digest mismatch",
    )
    for path in output.rglob("*"):
        if not path.is_file() or path.suffix.lower() != ".md":
            continue
        if path.relative_to(output).parts[0] == ".obsidian":
            continue
        if budget:
            budget.check()
        require(
            path.stat().st_size <= manifest["max_note_bytes"],
            "rendered Markdown exceeds note byte limit",
        )
        require_closed_fences(path.read_bytes(), str(path.relative_to(output)))
    total = hashlib.sha256()
    count = 0
    with ledger_path.open() as stream:
        for line in stream:
            if budget:
                budget.check()
            row = json.loads(line)
            for _ in recover_row(output, row):
                pass
            for index in row["indexes"]:
                path = output / index["path"]
                require(
                    path.stat().st_size == index["bytes"]
                    and hash_file(path, budget) == index["sha256"],
                    "index size or digest mismatch",
                )
            total.update(
                encoded({"path": row["path"], "bytes": row["bytes"], "sha256": row["sha256"]})
            )
            count += 1
    require(total.hexdigest() == manifest["source_tree_sha256"], "source tree digest mismatch")
    require(count == manifest["source_files"], "source file count mismatch")
    for path in output.rglob("*"):
        if path.relative_to(output).parts[0] == ".obsidian":
            continue
        if budget:
            budget.check()
        require(not path.is_symlink(), "output symlink refused")
        require(
            stat.S_IMODE(path.stat().st_mode) & 0o077 == 0, "output permissions are not private"
        )
        if path.suffix.lower() == ".md":
            require(
                path.stat().st_size <= manifest["max_note_bytes"],
                "rendered Markdown exceeds note byte limit",
            )
    return {"status": "verified", "source_files": count}


def package(
    source,
    output,
    *,
    agent_entry=None,
    source_generation: str,
    max_note_bytes=256 * 1024,
    max_output_bytes=12 * 1024**3,
    max_seconds=1800,
    max_source_bytes=128 * 1024**2,
):
    source, output = Path(source).absolute(), Path(output).absolute()
    agent_entry = Path(agent_entry).absolute() if agent_entry else None
    safe_path(source)
    safe_path(output)
    if agent_entry:
        safe_path(agent_entry)
    require(source.is_dir(), "source vault missing")
    require(not output.exists(), "output already exists; replacement refused")
    require(
        not output.is_relative_to(source) and not source.is_relative_to(output),
        "source/output overlap: output may not be inside source",
    )
    require(
        not any(
            ancestor.parent.name == "generations" and ancestor.parent.parent.name == "library-all"
            for ancestor in (output, *output.parents)
        ),
        "derived output must stay outside the canonical generation",
    )
    require(
        max_note_bytes >= 2048
        and max_seconds > 0
        and max_output_bytes > 0
        and max_source_bytes > 0,
        "invalid packaging limits",
    )
    require(
        isinstance(source_generation, str) and source_generation.strip(),
        "source generation identity is required; pass --source-generation",
    )
    source_generation = source_generation.strip()
    require(output.parent.is_dir(), "output parent must exist")
    budget = Budget(output, max_output_bytes, max_seconds)
    budget.check(8192)
    with deadline(max_seconds):
        output.mkdir(mode=0o700)
        (output / META).mkdir(mode=0o700)
        db_path = output / META / "navigation.sqlite"
        with private_file(db_path):
            pass
        connection = sqlite3.connect(db_path)
        connection.execute("PRAGMA page_size=4096")
        connection.execute("PRAGMA journal_mode=MEMORY")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA cache_size=-16384")
        database_limit(connection, budget, 0)
        budget.charge(8192)
        connection.execute(
            "CREATE TABLE anchors (path TEXT, anchor TEXT, part TEXT, PRIMARY KEY(path,anchor)) WITHOUT ROWID"
        )
        database_bytes = db_path.stat().st_size
        require(database_bytes == 8192, "unexpected initial navigation database size")
        plans = {}
        source_digest = hashlib.sha256()
        source_count = 0
        source_bytes = 0
        inventory = output / META / "sources.jsonl"
        try:
            with private_file(inventory) as listing:
                for relative, path in source_files(source, agent_entry):
                    budget.check()
                    raw = read_source(path, max_source_bytes)
                    if path.suffix.lower() == ".md":
                        raw.decode("utf-8")
                    row = {"path": relative, "bytes": len(raw), "sha256": digest(raw)}
                    line = encoded(row)
                    budget.charge(len(line))
                    listing.write(line)
                    source_digest.update(line)
                    source_count += 1
                    source_bytes += len(raw)
                    needs_split = path.suffix.lower() == ".md" and (
                        len(raw) > max_note_bytes // 2
                        or len(raw) + navigation_growth(raw.decode(), relative) > max_note_bytes
                    )
                    if needs_split:
                        segments = ranges(
                            raw, max_note_bytes // 2, relative=relative, maximum=max_note_bytes
                        )
                        annotate_inline_boundaries(raw, segments, check=budget.check)
                        plans[relative] = segments
                        database_limit(connection, budget, database_bytes)
                        populate_anchors(connection, relative, raw, segments)
                        connection.commit()
                        actual = db_path.stat().st_size
                        budget.charge(actual - database_bytes)
                        database_bytes = actual
                connection.commit()
            # Split plans are stable before rewriting incoming anchor links.
            # Conservative ranges include worst-case anchor-link growth and wrappers.
            output_files = 0
            rendered_bytes = 0
            ledger_path = output / META / "ledger.jsonl"
            with private_file(ledger_path) as ledger, inventory.open() as listing:
                for line in listing:
                    budget.check()
                    row = json.loads(line)
                    relative = row["path"]
                    path = (
                        agent_entry
                        if agent_entry and relative == "agent-entry.md"
                        else source / relative
                    )
                    raw = read_source(path, max_source_bytes)
                    require(
                        digest(raw) == row["sha256"] and len(raw) == row["bytes"],
                        "source changed during packaging",
                    )
                    segments = plans.get(relative) or [
                        {
                            "start": 0,
                            "end": len(raw),
                            "before": None,
                            "after": None,
                            "starts_line": True,
                        }
                    ]
                    pieces = []
                    for number, segment in enumerate(segments, 1):
                        budget.check()
                        original = raw[segment["start"] : segment["end"]]
                        if path.suffix.lower() == ".md":
                            body, edits = rewrite(
                                original,
                                relative,
                                segment,
                                plans,
                                connection,
                                source=source,
                                agent_entry=agent_entry,
                            )
                        else:
                            body, edits = original, []
                        start = 0
                        rendered = body
                        target = relative
                        if relative in plans:
                            target = part_path(relative, number)
                            require(
                                not (source / target).exists(),
                                "generated part collides with source path",
                            )
                            rendered, start = render_part(
                                body, relative, number, len(segments), segment
                            )
                        if path.suffix.lower() == ".md":
                            require(
                                len(rendered) <= max_note_bytes,
                                "rewritten Markdown exceeds note byte limit; reduce raw segment size",
                            )
                            require_closed_fences(rendered, target)
                        entry = write(output / target, rendered, budget)
                        entry.update(
                            {
                                "source_start": segment["start"],
                                "source_end": segment["end"],
                                "source_sha256": digest(original),
                                "body_start": start,
                                "body_bytes": len(body),
                                "edits": edits,
                                "inline_before": segment.get("inline_before"),
                                "inline_after": segment.get("inline_after"),
                            }
                        )
                        pieces.append(entry)
                        output_files += 1
                        rendered_bytes += len(rendered)
                    row["pieces"] = pieces
                    row["indexes"] = []
                    if relative in plans:
                        for target, data in indexes(relative, len(segments), max_note_bytes):
                            require(
                                target == relative or not (source / target).exists(),
                                "generated index collides with source path",
                            )
                            require_closed_fences(data, target)
                            row["indexes"].append(write(output / target, data, budget))
                            output_files += 1
                            rendered_bytes += len(data)
                    payload = encoded(row)
                    budget.charge(len(payload))
                    ledger.write(payload)
            connection.close()
            connection = None
            after = hashlib.sha256()
            after_count = 0
            for relative, path in source_files(source, agent_entry):
                budget.check()
                after.update(
                    encoded(
                        {
                            "path": relative,
                            "bytes": path.stat().st_size,
                            "sha256": hash_file(path, budget),
                        }
                    )
                )
                after_count += 1
            require(
                after_count == source_count and after.hexdigest() == source_digest.hexdigest(),
                "source changed before final verification",
            )
            result = {
                "schema": SCHEMA,
                "status": "verified",
                "source_generation": source_generation,
                "source_files": source_count,
                "source_bytes": source_bytes,
                "source_tree_sha256": source_digest.hexdigest(),
                "source_tree_after_sha256": after.hexdigest(),
                "source_unchanged": True,
                "split_notes": len(plans),
                "rendered_files": output_files,
                "rendered_bytes": rendered_bytes,
                "max_note_bytes": max_note_bytes,
                "max_source_bytes": max_source_bytes,
                "max_output_bytes": max_output_bytes,
                "max_seconds": max_seconds,
                "excluded_runtime_directories": [".obsidian"]
                if (source / ".obsidian").exists()
                else [],
                "ledger_sha256": hash_file(ledger_path, budget),
                "source_inventory_sha256": hash_file(inventory, budget),
                "navigation_sha256": hash_file(db_path, budget),
                "native_obsidian_acceptance": "unverified",
            }
            verify_package(output, budget=budget, expected=result)
            result["elapsed_seconds"] = round(time.monotonic() - budget.started, 3)
            result["output_bytes_before_manifest"] = budget.written
            write(output / META / "manifest.json", encoded(result), budget)
            return result
        finally:
            if connection is not None:
                connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--agent-entry", type=Path)
    parser.add_argument(
        "--source-generation",
        required=True,
        help="identity of the library generation the source vault came from; "
        "recorded in META/manifest.json so a package never needs its "
        "generation derived from content hashes after the fact",
    )
    parser.add_argument("--max-note-bytes", type=int, default=256 * 1024)
    parser.add_argument("--max-output-bytes", type=int, default=12 * 1024**3)
    parser.add_argument("--max-source-bytes", type=int, default=128 * 1024**2)
    parser.add_argument("--max-seconds", type=int, default=1800)
    arguments = parser.parse_args()
    os.umask(0o077)
    os.nice(10)
    result = package(**vars(arguments))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
