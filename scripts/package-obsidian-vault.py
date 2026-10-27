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


def advance_fence(line, state):
    probe, depth = unquote_line(line)
    previous, previous_depth = unquote_line(state or "")
    if state and depth < previous_depth:
        state = None
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


def fence_state(text, state=None, starts_line=True):
    for number, line in enumerate(text.splitlines(keepends=True)):
        if number or starts_line:
            state = advance_fence(line, state)
    return state


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


def navigation_growth(text, relative, state=None, starts_line=True):
    """Upper bound for same-directory part names, including fragment-only links."""
    growth = 0
    for number, line in enumerate(text.splitlines(keepends=True)):
        before = state
        if number or starts_line:
            state = advance_fence(line, state)
        if (
            before is not None
            or state is not None
            or unquote_line(line)[0].startswith(("    ", "\t"))
        ):
            continue
        literal = inline_spans(line)
        for match in LINK.finditer(line):
            if any(start <= match.start() < end for start, end in literal):
                continue
            parsed = urlsplit(match[2])
            if parsed.scheme or parsed.netloc or not parsed.fragment:
                continue
            growth += (
                len(quote(Path(part_path(relative, 1)).name, safe="._-~"))
                if not parsed.path
                else 32
            )
    return growth


def ranges(data, body_limit, *, relative=None, maximum=None):
    """Prefer whole lines, but bound even a single unbroken UTF-8 line."""
    offset = 0
    state = None
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
            after = fence_state(raw, state, starts_line)
            if maximum is None:
                break
            wrapper = (
                256
                + len(quote(Path(relative).name, safe="._-~"))
                + len(state or "")
                + len(after or "")
            )
            if (
                end - offset + wrapper + navigation_growth(raw, relative, state, starts_line)
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
                "starts_line": starts_line,
            }
        )
        starts_line = data[end - 1 : end] in {b"\n", b"\r"}
        state = after
        offset = end
    return result or [{"start": 0, "end": 0, "before": None, "after": None, "starts_line": True}]


def heading_anchor(title):
    title = re.sub(r"[*_`~]", "", title).strip().lower()
    return re.sub(r"\s+", "-", title)


def populate_anchors(connection, relative, data, segments):
    for number, segment in enumerate(segments, 1):
        state = segment["before"]
        text = data[segment["start"] : segment["end"]].decode()
        for line_number, line in enumerate(text.splitlines(keepends=True)):
            eligible = line_number > 0 or segment["starts_line"]
            before = state
            if eligible:
                state = advance_fence(line, state)
            if eligible and before is None and state is None:
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
    for number, line in enumerate(text.splitlines(keepends=True)):
        eligible = number > 0 or segment["starts_line"]
        before = state
        if eligible:
            state = advance_fence(line, state)
        if (
            not eligible
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


def render_part(body, relative, number, count, segment):
    filename = quote(Path(relative).name, safe="._-~")
    header = f"# Part {number} of {count}\n\n[Source note index]({filename})\n\n".encode()
    if segment["before"]:
        header += (segment["before"] + "\n").encode()
    elif segment.get("inline_before"):
        header += ("Inline code continued: " + segment["inline_before"] + " ").encode()
    footer = b""
    if segment["after"]:
        separator = "" if body.endswith((b"\n", b"\r")) else "\n"
        footer = (separator + segment["after"] + "\n").encode()
    elif segment.get("inline_after"):
        footer = (" " + segment["inline_after"] + "\n").encode()
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
