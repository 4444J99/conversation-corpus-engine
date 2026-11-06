"""Web captures: pages a capture tool fetched, read into the library as third-party records.

A capture tool that fetches saved web pages writes *shards*: JSONL files with one row per page
attempt, each row in the `capture-shard.v1` form (the fields are listed in `docs/knowledge-library.md`
under "Web captures"). `cce library build` reads them through the `web_captures` key of the source
manifest, the same intake that reads normalized archives and document roots, so the page text is
searchable like any other record and is bounded, receipted and published the same way.

What a shard row becomes:

* one record of provider `webcapture` (kind `page`, wing `ai`, lane `web`). Its identity comes from
  the row's page id, which the capture tool derives from the normalized URL, so reading the same
  page again is the same record and a changed page is a new revision of it;
* one message that carries the page text, role `tool`, channel `tool`: the text was put into the
  library by a fetch, it was not typed by anyone. The authorship engine goes further and labels the
  whole text `third_party` by the provider alone (see `attribution_rules`);
* provenance that keeps the requested and final URL, the fetch time, the content digest, the HTTP
  status, the extractor, the rights note and the shard the row came from. No absolute path and no
  file time enters the record, so moving the shard folder does not change a revision.

A failed fetch (a row with status `failed`) is kept as a record with no body and the failure class as
its body reason, never dropped. A row that cannot be trusted (not JSON, wrong shape, a digest or id
that does not follow from the row, text that is not valid, a line over the size bound) is refused
with a finding that names the file, the line and a fixed reason (never row-supplied text), and the
build goes on. Several rows for one page keep one winner: a capture beats a failure, then the latest
fetch time wins, then the larger content digest, then the larger digest of the canonical row, so the
result does not depend on which shard is read first. Only when two rows are byte-identical does the
shard name decide, and then the first name in sort order is the one the record names. The few best
rows of each page are kept: if the best one can no longer be read in the second pass, the next one
is used and the loss is a finding, never a silent drop.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .archive_model import Conversation, Message

PROVIDER = "webcapture"
KIND = "page"
WING = "ai"
LANE = "web"
READER = "web-capture@1.0.0"
SHARD_SCHEMA = "capture-shard.v1"
SOURCE = "web_capture"
SHARD_SUFFIX = ".jsonl"
DEFAULT_MAX_ROW_BYTES = 8 * 1024**2
MAX_URL_CHARS = 8192
MAX_TITLE_CHARS = 512
CHUNK = 64 * 1024
# A report keeps this many findings; the rest are counted in `findings_overflow`, not stored.
MAX_FINDINGS = 1000
# The best rows kept per page, so a changed shard can fall back to the next one.
MAX_CANDIDATES = 3
# A fetch time later than the clock by more than this is refused.
MAX_FUTURE_SKEW = timedelta(minutes=5)
FAILURE_CLASSES = (
    "dead_link",
    "login_wall",
    "paywall",
    "non_html",
    "rendered_empty",
    "timeout",
    "blocked",
)
ROW_FIELDS = frozenset(
    {
        "schema",
        "id",
        "key",
        "requested_url",
        "final_url",
        "status",
        "http_status",
        "content_digest",
        "captured_at",
        "extractor",
        "text",
        "failure_class",
        "license_note",
        "title",
        "tier",
    }
)
REQUIRED_FIELDS = ROW_FIELDS - {"title", "tier"}
_ID = re.compile(r"pg_[0-9a-f]{24}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_URL = re.compile(r"[hH][tT][tT][pP][sS]?://\S+")
# ASCII digits only: `\d` alone also matches other scripts' digits, which strptime accepts.
_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", re.ASCII)


class RowRefused(ValueError):
    """A row that cannot become a record; the message is the reason and never holds page text."""


def page_id(key: str) -> str:
    """The page id a capture tool derives from a normalized URL: `pg_` and 24 hex of its SHA-256."""
    return "pg_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def text_digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _string(row: dict[str, Any], name: str, *, empty: bool = False) -> str:
    value = row[name]
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise RowRefused(f"{name} must be a nonempty string")
    return value


def _url(row: dict[str, Any], name: str, *, optional: bool = False) -> str | None:
    value = row[name]
    if value is None and optional:
        return None
    if not isinstance(value, str) or not _URL.fullmatch(value):
        raise RowRefused(f"{name} must be an http or https URL without whitespace")
    if len(value) > MAX_URL_CHARS:
        raise RowRefused(f"{name} is longer than {MAX_URL_CHARS} characters")
    return value


def _locator(url: str) -> tuple[str, str] | None:
    """(host, path) of a URL or of a scheme-less key, case-folded; scheme, default port, query,
    fragment and a trailing slash do not count. None when it is not an http(s) locator."""
    try:
        parts = urlsplit(url if "://" in url else "http://" + url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    if host.startswith("www."):  # the capture tool's normalized key drops it too
        host = host[4:]
    if port is not None and port != (443 if scheme == "https" else 80):
        host = f"{host}:{port}"
    return host, parts.path.lower().rstrip("/")


def parse_stamp(stamp: str) -> datetime:
    """The UTC time of a checked stamp; `RowRefused` for a stamp that is not real."""
    if not _STAMP.fullmatch(stamp):
        raise RowRefused("captured_at must be a UTC time such as 2026-01-02T03:04:05Z")
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise RowRefused("captured_at is not a real date and time") from None


def check_row(row: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """The row if it is a trustworthy `capture-shard.v1` row, else `RowRefused` with the reason.

    Besides the field shapes this holds the rules a schema cannot state: the id follows the key,
    the key follows the page's URL, the fetch time is not in the future (later than `now`, the
    clock by default, plus a small skew), a captured row has text whose digest is the content
    digest and no failure class, a failed row has a failure class and no body. Every type is
    checked before a value is compared, so a list or object where a word belongs is a refusal.
    """
    if not isinstance(row, dict):
        raise RowRefused("row is not a JSON object")
    try:
        json.dumps(row, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        raise RowRefused("row holds text that is not valid Unicode") from None
    missing = sorted(REQUIRED_FIELDS - row.keys())
    if missing:
        raise RowRefused(f"missing field: {missing[0]}")
    unknown = sorted(row.keys() - ROW_FIELDS)
    if unknown:
        # The names come from the row, so the reason holds only how many there are.
        raise RowRefused(
            f"row has {len(unknown)} unexpected field{'s' if len(unknown) > 1 else ''}"
        )
    if row["schema"] != SHARD_SCHEMA:
        raise RowRefused(f"schema must be {SHARD_SCHEMA}")
    identity = row["id"]
    if not isinstance(identity, str) or not _ID.fullmatch(identity):
        raise RowRefused("id must be pg_ and 24 lowercase hex characters")
    key = _string(row, "key")
    if identity != page_id(key):
        raise RowRefused("id does not follow the key")
    requested = _url(row, "requested_url")
    final = _url(row, "final_url", optional=True)
    wanted = _locator(key)
    if wanted is None or wanted not in {
        place for place in (_locator(requested or ""), _locator(final or "")) if place
    }:
        raise RowRefused("key does not match the requested or final URL")
    status = row["status"]
    if not isinstance(status, str) or status not in ("captured", "failed"):
        raise RowRefused("status must be captured or failed")
    http_status = row["http_status"]
    if http_status is not None and (
        isinstance(http_status, bool) or not isinstance(http_status, int) or http_status < 100
    ):
        raise RowRefused("http_status must be null or an integer of at least 100")
    digest = row["content_digest"]
    if digest is not None and (not isinstance(digest, str) or not _DIGEST.fullmatch(digest)):
        raise RowRefused("content_digest must be null or sha256: and 64 lowercase hex characters")
    stamp = row["captured_at"]
    if not isinstance(stamp, str):
        raise RowRefused("captured_at must be a UTC time such as 2026-01-02T03:04:05Z")
    moment = parse_stamp(stamp)
    if moment > (now or datetime.now(timezone.utc)) + MAX_FUTURE_SKEW:
        raise RowRefused("captured_at is in the future")
    extractor = row["extractor"]
    if (
        not isinstance(extractor, dict)
        or set(extractor) != {"name", "version"}
        or any(not isinstance(value, str) or not value for value in extractor.values())
    ):
        raise RowRefused("extractor must hold a nonempty name and version")
    _string(row, "license_note")
    if "title" in row and not isinstance(row["title"], str):
        raise RowRefused("title must be a string")
    if "tier" in row and (not isinstance(row["tier"], str) or row["tier"] not in ("A", "B")):
        raise RowRefused("tier must be A or B")
    text = _string(row, "text", empty=True)
    failure = row["failure_class"]
    if failure is not None and (not isinstance(failure, str) or failure not in FAILURE_CLASSES):
        raise RowRefused("failure_class is not a known class")
    if status == "captured":
        if failure is not None:
            raise RowRefused("a captured row cannot carry a failure class")
        if not text.strip():
            raise RowRefused("a captured row needs text")
        if "\x00" in text:
            raise RowRefused("text holds a NUL character")
        if digest != text_digest(text):
            raise RowRefused("content_digest does not match the text")
    else:
        if failure is None:
            raise RowRefused("a failed row needs a failure class")
        if text or digest is not None:
            raise RowRefused("a failed row cannot carry a body or a digest")
    return row


def parse_row(raw: bytes, *, now: datetime | None = None) -> dict[str, Any]:
    """One shard line as a checked row; `RowRefused` for a line that is not valid JSON or a row."""
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        raise RowRefused("line is not valid JSON") from None
    return check_row(value, now=now)


def _rank(row: dict[str, Any]) -> tuple[bool, datetime, str, str]:
    """What orders the rows of one page, best last: a capture, the later fetch, the larger content
    digest, the larger digest of the canonical row. Every part comes from the row's own content,
    so no file name, line or read order can change the winner."""
    canonical = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (
        row["status"] == "captured",
        parse_stamp(row["captured_at"]),
        row["content_digest"] or "",
        hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def _clean_title(row: dict[str, Any], url: str) -> str:
    title = " ".join(str(row.get("title") or "").split())
    return (title or url)[:MAX_TITLE_CHARS]


def conversation_from_row(row: dict[str, Any], source_file: str) -> Conversation:
    """The library record of a checked row. Deterministic: the same row gives the same record."""
    tail = row["id"][len("pg_") :]
    url = row["final_url"] or row["requested_url"]
    stamp = row["captured_at"]
    captured = row["status"] == "captured"
    provenance: dict[str, Any] = {
        "source": SOURCE,
        "source_origin": SOURCE,
        "source_file": source_file,
        # The URL is the record's source reference: a hit and an index row show where the text is from.
        "source_path": url,
        "reader": READER,
        "reader_version": READER,
        "capture_schema": SHARD_SCHEMA,
        "capture_id": row["id"],
        "url_key": row["key"],
        "requested_url": row["requested_url"],
        "final_url": row["final_url"],
        "http_status": row["http_status"],
        "captured_at": stamp,
        "content_digest": row["content_digest"],
        "extractor": dict(row["extractor"]),
        "license_note": row["license_note"],
    }
    if "tier" in row:
        provenance["tier"] = row["tier"]
    extra = {
        "wing": WING,
        "lane": LANE,
        "body_status": "full" if captured else "metadata_only",
        "body_reason": "" if captured else row["failure_class"],
        "capture_status": row["status"],
        "reader_version": READER,
    }
    messages = (
        [
            Message(
                id=f"web-capture-{tail}",
                role="tool",
                text=row["text"],
                channel="tool",
                ts=stamp,
                typed_chars=0,
                meta={"kind": SOURCE},
            )
        ]
        if captured
        else []
    )
    return Conversation(
        provider=PROVIDER,
        source_id=f"pg-{tail}",
        title=_clean_title(row, url),
        created=stamp,
        updated=stamp,
        kind=KIND,
        messages=messages,
        provenance=provenance,
        extra=extra,
    )


def capture_summary(provenance: dict[str, Any]) -> dict[str, Any]:
    """The facts a search hit shows for a web capture: where the text is from and when it was fetched."""
    return {
        "url": provenance.get("requested_url"),
        "final_url": provenance.get("final_url"),
        "captured_at": provenance.get("captured_at"),
        "content_digest": provenance.get("content_digest"),
        "http_status": provenance.get("http_status"),
    }


@dataclass(frozen=True)
class _Candidate:
    """Where the best row of a page is, and what ranks it against the other rows of that page."""

    rank: tuple[bool, datetime, str, str]
    path: Path
    offset: int
    line: int
    relative: str


def _keep(pool: list[_Candidate], candidate: _Candidate) -> None:
    """Add a candidate to a page's pool: best first, then by shard name and offset; the pool keeps
    `MAX_CANDIDATES`, so memory does not grow with the number of rows of one page."""
    pool.append(candidate)
    pool.sort(key=lambda item: (item.relative, item.offset))
    pool.sort(key=lambda item: item.rank, reverse=True)
    del pool[MAX_CANDIDATES:]


def _finding(
    report: dict[str, Any], relative: str, code: str, detail: str = "", line: int = 0
) -> None:
    if len(report["findings"]) >= MAX_FINDINGS:
        report["findings_overflow"] = report.get("findings_overflow", 0) + 1
        return
    report["findings"].append(
        {
            "scope": "web_captures",
            "path": relative,
            "line": line,
            "code": code,
            "detail": detail,
        }
    )


def _lines(handle: Any, digest: Any, limit: int) -> Iterator[tuple[int, int, bytes | None]]:
    """(line number, byte offset, raw line) of every line; the raw line is None past `limit`.

    A line over the limit is read to its end in chunks and never held, so one huge row cannot
    exhaust memory. Every byte read goes into the file's digest.
    """
    offset = 0
    number = 0
    while raw := handle.readline(limit + 1):
        digest.update(raw)
        number += 1
        start = offset
        offset += len(raw)
        if len(raw) > limit:
            while not raw.endswith(b"\n"):
                raw = handle.readline(CHUNK)
                if not raw:
                    break
                digest.update(raw)
                offset += len(raw)
            yield number, start, None
        else:
            yield number, start, raw


def _open(path: Path) -> tuple[Any, os.stat_result]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("not a regular file")
        return os.fdopen(descriptor, "rb"), info
    except BaseException:
        os.close(descriptor)
        raise


def _shards(source: Path, report: dict[str, Any]) -> list[tuple[Path, str]]:
    """The shard files of a declared source, sorted by relative name: a file, or a folder's
    `*.jsonl` files at any depth. Symlinks are named in a finding and never followed."""
    if source.is_file() and not source.is_symlink():
        return [(source, source.name)]
    found: list[tuple[Path, str]] = []

    def unreadable(error: OSError) -> None:
        # os.walk skips a folder it cannot list unless it is told; a skipped folder is a finding.
        try:
            relative = Path(error.filename).relative_to(source).as_posix()
        except (TypeError, ValueError):
            relative = "."
        _finding(report, relative, "unreadable_file", type(error).__name__)

    for directory, dirs, files in os.walk(source, followlinks=False, onerror=unreadable):
        folder = Path(directory)
        for name in list(dirs):
            if (folder / name).is_symlink():
                _finding(
                    report, (folder / name).relative_to(source).as_posix(), "symlink_not_followed"
                )
                dirs.remove(name)
        dirs.sort()
        for name in sorted(files):
            path = folder / name
            if not name.lower().endswith(SHARD_SUFFIX):
                continue
            relative = path.relative_to(source).as_posix()
            if path.is_symlink():
                _finding(report, relative, "symlink_not_followed")
                continue
            found.append((path, relative))
    return sorted(found, key=lambda item: item[1])


def iter_web_captures(
    sources: list[str] | list[Path],
    *,
    report: dict[str, Any] | None = None,
    max_row_bytes: int = DEFAULT_MAX_ROW_BYTES,
    clock: Callable[[], datetime] | None = None,
) -> Iterator[Conversation]:
    """Read every declared shard file or folder; yield one record per page, in page-id order.

    `report` is filled as the iterator is consumed: `sources` (one entry per declared source and
    one per shard, with the shard's SHA-256), `counts`, `findings` (at most `MAX_FINDINGS`; the
    exact number of the rest is `findings_overflow`). The pages are chosen in a first pass that
    keeps only where each page's best few rows are, so memory does not grow with the text; a
    second pass reads just those rows, best first. A shard that changed between the passes yields
    a finding for each row it can no longer vouch for, and the page falls back to its next row;
    a page with no readable row yields `page_unavailable`. `clock` gives the time a fetch time is
    checked against (read once, so both passes agree); it defaults to the system clock.
    """
    report = report if report is not None else {}
    for key in ("sources", "shards", "findings"):
        report.setdefault(key, [])
    report.setdefault("findings_overflow", 0)
    counts = report.setdefault("counts", {})
    for key in ("files", "rows", "captured", "failed", "refused", "duplicates", "records"):
        counts.setdefault(key, 0)
    moment = (clock or (lambda: datetime.now(timezone.utc)))()
    best: dict[str, list[_Candidate]] = {}
    seen: dict[Path, os.stat_result] = {}
    for declared in sources:
        root = Path(declared).expanduser().absolute()
        entry = {"path": str(root), "status": "scanned"}
        report["sources"].append(entry)
        if any(part.is_symlink() for part in [root, *root.parents]):
            entry["status"] = "symlink_not_followed"
            _finding(report, str(root), entry["status"])
            continue
        if not root.exists():
            entry["status"] = "missing_source"
            _finding(report, str(root), entry["status"])
            continue
        if not root.is_dir() and not root.is_file():
            entry["status"] = "not_regular_file"
            _finding(report, str(root), entry["status"])
            continue
        for path, relative in _shards(root, report):
            try:
                handle, info = _open(path)
            except OSError as error:
                _finding(report, relative, "unreadable_file", type(error).__name__)
                continue
            digest = hashlib.sha256()
            rows = 0
            with handle:
                for number, offset, raw in _lines(handle, digest, max_row_bytes):
                    if raw is None:
                        counts["refused"] += 1
                        _finding(
                            report,
                            relative,
                            "row_too_large",
                            f"the line is over {max_row_bytes} bytes",
                            number,
                        )
                        continue
                    if not raw.strip():
                        continue
                    rows += 1
                    counts["rows"] += 1
                    try:
                        row = parse_row(raw, now=moment)
                    except RowRefused as error:
                        counts["refused"] += 1
                        _finding(report, relative, "malformed_row", str(error), number)
                        continue
                    counts["captured" if row["status"] == "captured" else "failed"] += 1
                    pool = best.setdefault(row["id"], [])
                    if pool:
                        counts["duplicates"] += 1
                    _keep(pool, _Candidate(_rank(row), path, offset, number, relative))
                after = path.stat()
            if (info.st_size, info.st_mtime_ns, info.st_ino) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ino,
            ):
                _finding(report, relative, "source_changed_during_read")
            seen.setdefault(path, info)
            counts["files"] += 1
            report["shards"].append(
                {
                    "source": str(root),
                    "path": relative,
                    "sha256": digest.hexdigest(),
                    "size_bytes": info.st_size,
                    "rows": rows,
                }
            )
    for identity in sorted(best):
        for candidate in best[identity]:
            try:
                row = _reread(candidate, identity, seen[candidate.path], max_row_bytes, moment)
            except (OSError, ValueError, RecursionError) as error:
                reason = str(error) if isinstance(error, RowRefused) else type(error).__name__
                counts["refused"] += 1
                _finding(report, candidate.relative, "malformed_row", reason, candidate.line)
                continue
            counts["records"] += 1
            yield conversation_from_row(row, candidate.relative)
            break
        else:
            first = best[identity][0]
            _finding(
                report,
                first.relative,
                "page_unavailable",
                "no kept row of the page could be read again",
                first.line,
            )


def _reread(
    candidate: _Candidate,
    identity: str,
    scanned: os.stat_result,
    max_row_bytes: int,
    moment: datetime,
) -> dict[str, Any]:
    """The row a candidate points at, if its shard and row are still what the first pass saw."""
    handle, now = _open(candidate.path)
    with handle:
        if (scanned.st_size, scanned.st_mtime_ns, scanned.st_ino) != (
            now.st_size,
            now.st_mtime_ns,
            now.st_ino,
        ):
            raise RowRefused("the shard changed after it was scanned")
        handle.seek(candidate.offset)
        row = parse_row(handle.readline(max_row_bytes + 1), now=moment)
        if row["id"] != identity or _rank(row) != candidate.rank:
            raise RowRefused("the row changed after it was scanned")
    return row
