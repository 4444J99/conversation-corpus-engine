"""Browser links: the saved, read-later and visited links of every browser, as library documents.

The browser-state tool unifies every browser into one deduplicated canonical set: JSONL files
for bookmarks, the reading list, open tabs, Wikipedia visits and history, one row per URL. This
module turns that set into one small Markdown note per link, written under a private export
directory that a source manifest then declares as an ordinary `documents` root, so a link is
searchable by its title, address, folder and reading list like any other library record, and is
bounded, receipted and published the same way. Nothing here reads a browser store, fetches a page
or changes the canonical set. The page text of a link comes separately from the web capture
source; the note names the same page id (`pg_` plus 24 hex of the normalized URL), so the two meet.

What a row becomes:

* one note per normalized URL, however many lists or browsers hold it: a URL that is bookmarked,
  on the reading list and in the history is one note that says so. The file name is the page id,
  so refreshing an export rewrites the same file and a changed link is a new revision of one
  record, never a second record;
* the note is plain text in a fixed layout with no export time and no absolute path, so an
  unchanged link produces identical bytes and a refresh writes only what changed;
* the title is the page's own title, not the owner's words; the note says so. A URL with embedded
  credentials, a non-http(s) scheme, or no host is refused, and so is a row of the wrong shape.
  Each refusal is counted by code and named by file and line, never by the address.

History is the widest set and the most private, so it is opt in per run: `kinds` names what is
exported (bookmarks, reading list, tabs and Wikipedia by default), `min_visits` and
`exclude_hosts` narrow history, and the export directory must be outside every Git work tree
or ignored by it.

A refresh never deletes. A note whose link left the canonical set stays on disk and is counted
as retained, because removing a record's source is a decision the owner makes, not a side effect.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .corpus_store import require_git_private
from .library_export import _absolute, _no_symlinks
from .web_capture import MAX_TITLE_CHARS, MAX_URL_CHARS, normalized_url_key, page_id

SCHEMA = "cce.browser-links-export.v1"
RECEIPT_NAME = "browser-links-receipt.json"
NOTES_FOLDER = "links"
KINDS: dict[str, str] = {
    "bookmarks": "Bookmark",
    "reading-list": "Reading list",
    "tabs": "Open tab",
    "wikipedia": "Wikipedia visit",
    "history": "History",
}
DEFAULT_KINDS = ("bookmarks", "reading-list", "tabs", "wikipedia")
MAX_ROW_BYTES = 1024 * 1024
MAX_ROWS_PER_FILE = 2_000_000
MAX_FINDINGS = 1000
MAX_FOLDERS = 20
MAX_FOLDER_CHARS = 256
_URL = re.compile(r"https?://\S+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SPACE = re.compile(r"\s+")


class LinkRefused(ValueError):
    """A row that cannot become a note; the message is the reason and never holds the address."""


@dataclass
class _Link:
    key: str
    url: str
    title: str = ""
    kinds: set[str] = field(default_factory=set)
    folders: set[str] = field(default_factory=set)
    browsers: set[str] = field(default_factory=set)
    added: str = ""
    first_seen: str = ""
    visits: int = 0


def _clean(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return _SPACE.sub(" ", _CONTROL.sub(" ", value)).strip()[:limit]


def _strings(value: Any, limit: int, count: int) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned = (_clean(item, limit) for item in value if isinstance(item, str))
    return sorted({item for item in cleaned if item})[:count]


def _timestamp(value: Any) -> str:
    """A declared ISO date or the empty string. The 1601 epoch is a browser's unset date."""
    text = _clean(value, 40)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(T[0-9:.+\-Z]+)?", text) or text.startswith("1601-"):
        return ""
    return text


def check_row(row: Any) -> tuple[str, str]:
    """The cleaned address and its normalized key, or `LinkRefused` with the reason."""
    if not isinstance(row, dict):
        raise LinkRefused("row is not an object")
    url = row.get("url")
    if not isinstance(url, str) or not _URL.fullmatch(url):
        raise LinkRefused("url must be an http or https address without whitespace")
    if len(url) > MAX_URL_CHARS:
        raise LinkRefused(f"url is longer than {MAX_URL_CHARS} characters")
    try:
        parts = urlsplit(url)
        valid = (
            bool(parts.hostname)
            and parts.username is None
            and parts.password is None
            and (parts.port is None or 0 < parts.port < 65536)
        )
    except ValueError:
        valid = False
    if not valid:
        raise LinkRefused("url must have a valid host and no embedded credentials")
    return url, normalized_url_key(url)


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def _excluded(url: str, patterns: tuple[str, ...]) -> bool:
    host = _host(url)
    return any(host == item or host.endswith("." + item) for item in patterns)


def _open_rows(path: Path) -> Iterator[tuple[int, bytes]]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError(f"{path.name} is not a regular file")
        for number, line in enumerate(source, 1):
            if number > MAX_ROWS_PER_FILE:
                raise ValueError(f"{path.name} has more than {MAX_ROWS_PER_FILE} rows")
            yield number, line


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def collect_links(
    canonical: Path,
    *,
    kinds: Iterable[str] = DEFAULT_KINDS,
    min_visits: int = 1,
    exclude_hosts: Iterable[str] = (),
    report: dict[str, Any] | None = None,
) -> dict[str, _Link]:
    """Every accepted link of the chosen lists, merged by normalized URL, and a report of what
    was read and refused. A list file that is missing is a finding, never a silent zero."""
    chosen = tuple(dict.fromkeys(kinds))
    unknown = [kind for kind in chosen if kind not in KINDS]
    if unknown:
        raise ValueError(f"unknown link list: {', '.join(unknown)}; choose from {', '.join(KINDS)}")
    if isinstance(min_visits, bool) or not isinstance(min_visits, int) or min_visits < 0:
        raise ValueError("min_visits must be a nonnegative integer")
    hosts = tuple(
        sorted({_clean(item, 253).lower().removeprefix("www.") for item in exclude_hosts} - {""})
    )
    report = report if report is not None else {}
    findings: list[dict[str, Any]] = report.setdefault("findings", [])
    sources: list[dict[str, Any]] = report.setdefault("sources", [])
    counts: Counter[str] = Counter()
    links: dict[str, _Link] = {}
    for kind in chosen:
        path = canonical / f"{kind}.jsonl"
        entry: dict[str, Any] = {"list": kind, "file": path.name}
        sources.append(entry)
        if path.is_symlink() or not path.is_file():
            entry["status"] = "missing"
            findings.append({"file": path.name, "line": 0, "code": "list_missing"})
            continue
        entry["sha256"] = _file_digest(path)
        entry["status"] = "read"
        read = accepted = 0
        for number, raw in _open_rows(path):
            if not raw.strip():
                continue
            read += 1
            try:
                if len(raw) > MAX_ROW_BYTES:
                    raise LinkRefused("row is longer than the size bound")
                try:
                    row = json.loads(raw)
                except ValueError:
                    raise LinkRefused("row is not valid JSON") from None
                url, key = check_row(row)
                visits = row.get("visit_count", 0)
                visits = visits if isinstance(visits, int) and not isinstance(visits, bool) else 0
                if kind == "history" and visits < min_visits:
                    counts["below_min_visits"] += 1
                    continue
                if _excluded(url, hosts):
                    counts["excluded_host"] += 1
                    continue
            except LinkRefused as error:
                counts["refused"] += 1
                if len(findings) < MAX_FINDINGS:
                    findings.append(
                        {"file": path.name, "line": number, "code": "refused", "reason": str(error)}
                    )
                continue
            accepted += 1
            link = links.setdefault(key, _Link(key=key, url=url))
            link.kinds.add(kind)
            link.title = link.title or _clean(row.get("title"), MAX_TITLE_CHARS)
            if len(url) < len(link.url):
                link.url = url
            link.folders.update(_strings(row.get("folders"), MAX_FOLDER_CHARS, MAX_FOLDERS))
            link.browsers.update(_strings(row.get("sources"), 32, 12))
            added, first = _timestamp(row.get("added")), _timestamp(row.get("first_seen"))
            if added and (not link.added or added < link.added):
                link.added = added
            if first and (not link.first_seen or first < link.first_seen):
                link.first_seen = first
            link.visits = max(link.visits, visits)
        entry.update(rows=read, accepted=accepted)
    report["counts"] = {
        "links": len(links),
        "refused": counts["refused"],
        "below_min_visits": counts["below_min_visits"],
        "excluded_host": counts["excluded_host"],
    }
    return links


def render_note(link: _Link) -> str:
    """The note of one link: a fixed layout, deterministic, with no clock and no local path."""
    lists = ", ".join(KINDS[kind] for kind in KINDS if kind in link.kinds)
    lines = [
        f"# {link.title or link.url}",
        "",
        f"Link: {link.url}",
        f"Saved in: {lists}",
    ]
    if link.folders:
        lines.append("Folders: " + "; ".join(sorted(link.folders)))
    if link.browsers:
        lines.append("Browsers: " + ", ".join(sorted(link.browsers)))
    if link.added:
        lines.append(f"Added: {link.added}")
    if link.first_seen:
        lines.append(f"First visited: {link.first_seen}")
    if link.visits:
        lines.append(f"Visits: {link.visits}")
    lines.append(f"Page id: {page_id(link.key)}")
    lines += [
        "",
        "This is a saved link. The title is the page's own title, not the owner's words;"
        " the page text, when it was captured, is a separate web capture record with the same"
        " page id.",
        "",
    ]
    return "\n".join(lines)


def _note_path(destination: Path, link: _Link) -> Path:
    identifier = page_id(link.key)
    return destination / NOTES_FOLDER / identifier[3:5] / f"{identifier}.md"


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".link-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        Path(name).replace(path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def _existing(path: Path) -> bytes | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("an exported note path is not a regular file")
        return source.read(MAX_ROW_BYTES + 1)


def manifest_entry(destination: Path) -> dict[str, Any]:
    """The `documents` declaration that reads this export; paste it into a source manifest."""
    return {
        "id": "browser-links",
        "root": str(destination),
        "include": [f"{NOTES_FOLDER}/**/*.md"],
        "wing": "authored",
        "lane": "research",
        "kind": "document",
    }


def export_browser_links(
    canonical: Path,
    destination: Path,
    *,
    kinds: Iterable[str] = DEFAULT_KINDS,
    min_visits: int = 1,
    exclude_hosts: Iterable[str] = (),
) -> dict[str, Any]:
    """Write one note per link under ``destination`` and return the receipt (also written).

    The destination is private (outside every Git work tree, or ignored by it), has no symlink
    in its path, and is either new or the result of an earlier export. Notes are written
    atomically and only when their bytes change; nothing already there is deleted.
    """
    canonical = _absolute(canonical)
    destination = _absolute(destination)
    if not canonical.is_dir():
        raise ValueError("canonical must be the directory of the browser-state canonical set")
    _no_symlinks(destination)
    require_git_private(destination)
    receipt_path = destination / RECEIPT_NAME
    if destination.exists():
        if not destination.is_dir():
            raise ValueError("destination is not a directory")
        if any(destination.iterdir()) and not receipt_path.is_file():
            raise ValueError("destination is not empty and holds no earlier browser-links export")
    report: dict[str, Any] = {}
    chosen = tuple(dict.fromkeys(kinds))
    links = collect_links(
        canonical,
        kinds=chosen,
        min_visits=min_visits,
        exclude_hosts=exclude_hosts,
        report=report,
    )
    written = unchanged = 0
    current: set[str] = set()
    for key in sorted(links):
        link = links[key]
        path = _note_path(destination, link)
        data = render_note(link).encode("utf-8")
        current.add(str(path.relative_to(destination)))
        if _existing(path) == data:
            unchanged += 1
            continue
        _write_atomic(path, data)
        written += 1
    retained = 0
    folder = destination / NOTES_FOLDER
    if folder.is_dir():
        retained = sum(
            1 for note in folder.rglob("*.md") if str(note.relative_to(destination)) not in current
        )
    receipt = {
        "schema": SCHEMA,
        "kinds": list(chosen),
        "min_visits": min_visits,
        "exclude_hosts": sorted({_clean(item, 253).lower() for item in exclude_hosts} - {""}),
        "sources": report["sources"],
        "counts": {
            **report["counts"],
            "written": written,
            "unchanged": unchanged,
            "retained_not_in_source": retained,
        },
        "findings": report["findings"],
        "manifest_entry": manifest_entry(destination),
    }
    _write_atomic(
        receipt_path,
        (json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return receipt
