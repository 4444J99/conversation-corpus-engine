"""A local wiki reader for the published knowledge library (`cce library serve`).

The server renders the current library generation as linked pages so it can be browsed like a
wiki: a home page, ranked search, one page per record with a table of contents, paged messages
and "related" records that share repeated passages, and a revision history page. It reads the
same store through the same bounded code as `cce library search` and `cce library read`; nothing
is exported, copied or written, so there is no second copy of private text to keep in step.

Progressive disclosure is structural. A search page lists one line per record (title, lane,
provider); the excerpt opens on demand. A record page shows its metadata and contents first, and
only the requested messages, never a whole 10 MB body. Every response is bounded.

Safety: the server binds loopback addresses only and refuses any other ``Host`` header (so a
rebinding page cannot read it), answers GET and HEAD only, runs no script, and escapes all
stored text. Source text is shown as plain preformatted text and is never interpreted as HTML or
Markdown, so archived content cannot inject markup or links into a page.
"""

from __future__ import annotations

import html
import json
import re
import socket
import sqlite3
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

from .library_commands import (
    open_library,
    read_arguments,
    read_cursor,
)
from .library_store import (
    DEFAULT_SEARCH_SECONDS,
    MAX_SEARCH_SECONDS,
    SEARCH_PLANS,
    Library,
)

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
SEARCH_PAGE = 20
MESSAGES_PER_PAGE = 20
PAGE_CHARACTERS = 24000
RELATED_LIMIT = 20
# A record page waits this long for its related records before it links to the page that waits
# longer; a record page shows its contents inline only for a record this short, because reading
# the headings of a long record means decoding and scanning every message of it.
RELATED_INLINE_SECONDS = 1.0
RELATED_PAGE_SECONDS = 20.0
RELATED_PROGRESS_INSTRUCTIONS = 10000
CONTENTS_INLINE_MESSAGES = 300
CONTENTS_PAGE = 100
UI_SEARCH_SECONDS = 8.0
SUBPAGES = ("history", "contents", "related")
UID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,200}$")
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
}

STYLE = """
:root{color-scheme:light dark;--bg:#fbfbf9;--fg:#1d1d1b;--mute:#66665f;--line:#dcdcd4;
--accent:#1f5fbf;--card:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#17171a;--fg:#e8e8e3;--mute:#9a9a93;
--line:#34343a;--accent:#7fb0ff;--card:#1f1f24}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,sans-serif}
header,main{max-width:56rem;margin:0 auto;padding:0 1rem}
header{padding-top:1rem;display:flex;gap:1rem;align-items:center;flex-wrap:wrap}
header a.brand{font-weight:700;text-decoration:none;color:var(--fg)}
form.search{display:flex;gap:.5rem;flex:1;min-width:16rem}
input[type=search]{flex:1;padding:.45rem .6rem;border:1px solid var(--line);border-radius:6px;
background:var(--card);color:var(--fg);font:inherit}
button{padding:.45rem .9rem;border:1px solid var(--line);border-radius:6px;background:var(--card);
color:var(--fg);font:inherit;cursor:pointer}
a{color:var(--accent)}
h1{font-size:1.45rem;margin:1.2rem 0 .3rem}h2{font-size:1.1rem;margin:1.4rem 0 .4rem}
.mute{color:var(--mute);font-size:.9rem}
.hit{border-top:1px solid var(--line);padding:.55rem 0}
.hit .t{font-weight:600}
details>summary{cursor:pointer;color:var(--mute);font-size:.9rem}
pre.text{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--card);
border:1px solid var(--line);border-radius:6px;padding:.7rem;margin:.3rem 0;font:inherit;
font-size:.95rem}
.msg{margin:1rem 0}.msg .who{font-size:.85rem;color:var(--mute)}
.pill{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:0 .55rem;
font-size:.8rem;color:var(--mute);margin-right:.3rem}
.notice{border-left:3px solid var(--accent);padding:.2rem .7rem;margin:.8rem 0;
color:var(--mute);font-size:.9rem}
nav.pager{display:flex;justify-content:space-between;margin:1rem 0}
table{border-collapse:collapse}td,th{padding:.15rem .8rem .15rem 0;text-align:left;
font-weight:normal}
"""


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _link(path: str, **query: Any) -> str:
    pairs = {key: value for key, value in query.items() if value not in (None, "")}
    return path + (("?" + urlencode(pairs)) if pairs else "")


def record_path(uid: str) -> str:
    return "/record/" + quote(uid, safe="")


def _page(title: str, body: str, *, query: str = "") -> bytes:
    document = (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{_e(title)} - CCE library</title><style>{STYLE}</style></head><body>"
        "<header><a class=brand href='/'>CCE library</a>"
        "<form class=search action='/search' method=get role=search>"
        f"<input type=search name=q value='{_e(query)}' placeholder='Search the library' "
        "aria-label='Search the library' autofocus><button>Search</button></form></header>"
        f"<main>{body}</main></body></html>"
    )
    return document.encode("utf-8")


def _first(query: dict[str, list[str]], name: str, default: str = "") -> str:
    values = query.get(name)
    return values[0] if values else default


def _int(query: dict[str, list[str]], name: str, default: int, low: int, high: int) -> int:
    raw = _first(query, name)
    if raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None
    if not low <= value <= high:
        raise ValueError(f"{name} must be from {low} to {high}")
    return value


def _seconds(query: dict[str, list[str]]) -> float:
    raw = _first(query, "max_seconds")
    if raw == "":
        return UI_SEARCH_SECONDS
    try:
        value = float(raw)
    except ValueError:
        raise ValueError("max_seconds must be a number") from None
    if not 0 < value <= MAX_SEARCH_SECONDS:
        raise ValueError(f"max_seconds must be above 0 and at most {MAX_SEARCH_SECONDS:g}")
    return value


class _RelatedExpired(Exception):
    pass


def related_records_bounded(
    library: Library,
    uid: str,
    limit: int = RELATED_LIMIT,
    *,
    max_seconds: float | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Records that repeat passages of ``uid``, most shared first, and whether the list is
    complete (``[]`` before blocks exist).

    The relation is the library's own deterministic repeated-passage index; two records are
    related because they contain the same eligible prose block, not because of any inference.
    With ``max_seconds`` the query is interrupted when the time is up and the answer is
    ``([], False)``: a partial group count would rank records wrongly, so nothing is returned
    rather than a misleading list.
    """
    deadline = None if max_seconds is None else time.monotonic() + max_seconds

    def check() -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise _RelatedExpired

    try:
        with library.sql_progress(
            check if deadline is not None else None,
            instructions=RELATED_PROGRESS_INSTRUCTIONS,
        ):
            rows = library.connection.execute(
                "SELECT r.uid, r.title, r.provider, r.lane, COUNT(DISTINCT o2.digest) AS shared "
                "FROM occurrences o1 JOIN occurrences o2 ON o2.digest=o1.digest "
                "AND o2.uid<>o1.uid JOIN records r ON r.uid=o2.uid WHERE o1.uid=? "
                "GROUP BY r.uid ORDER BY shared DESC, r.uid LIMIT ?",
                (uid, limit),
            ).fetchall()
    except _RelatedExpired:
        return [], False
    return [dict(row) for row in rows], True


def related_records(library: Library, uid: str, limit: int = RELATED_LIMIT) -> list[dict[str, Any]]:
    """Records that repeat passages of ``uid`` (see ``related_records_bounded``), unbounded."""
    return related_records_bounded(library, uid, limit)[0]


def library_summary(library: Library) -> dict[str, Any]:
    """Cheap home-page counts (record totals by wing, lane and provider)."""
    connection = library.connection

    def counts(column: str) -> dict[str, int]:
        return {
            row[0]: row[1]
            for row in connection.execute(
                f"SELECT {column}, COUNT(*) FROM records GROUP BY {column} ORDER BY 2 DESC"
            )
        }

    total = connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    return {
        "records": total,
        "by_wing": counts("wing"),
        "by_lane": counts("lane"),
        "by_provider": counts("provider"),
    }


class LibraryServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], store_root: Path) -> None:
        if address[0] not in LOOPBACK_HOSTS:
            raise ValueError(
                f"The library server only binds loopback addresses, not {address[0]!r}"
            )
        self.store_root = store_root
        # A generation is immutable, so its home-page counts are read once per generation.
        self.summaries: dict[str, dict[str, Any]] = {}
        # "::1" needs an IPv6 socket; the family must be set before the base class binds.
        self.address_family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        super().__init__(address, LibraryHandler)

    def allowed_hosts(self) -> set[str]:
        port = self.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}


class _Missing(Exception):
    pass


class LibraryHandler(BaseHTTPRequestHandler):
    server: LibraryServer
    server_version = "cce-library"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str, *, as_json: bool, query: str = "") -> None:
        if as_json:
            self._json(status, {"error": message})
        else:
            body = f"<h1>{status.value} {_e(status.phrase)}</h1><p>{_e(message)}</p>"
            self._send(status, _page(status.phrase, body, query=query), "text/html; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        as_json = parts.path.startswith("/api/")
        if (self.headers.get("Host") or "") not in self.server.allowed_hosts():
            self._error(HTTPStatus.FORBIDDEN, "host not allowed", as_json=as_json)
            return
        query = parse_qs(parts.query, keep_blank_values=True)
        try:
            self._route(parts.path, query)
        except _Missing as error:
            self._error(HTTPStatus.NOT_FOUND, str(error) or "not found", as_json=as_json)
        except (ValueError, KeyError) as error:
            text = error.args[0] if error.args else "bad request"
            status = HTTPStatus.NOT_FOUND if isinstance(error, KeyError) else HTTPStatus.BAD_REQUEST
            self._error(status, str(text), as_json=as_json, query=_first(query, "q"))
        except sqlite3.Error as error:
            self._error(HTTPStatus.BAD_REQUEST, f"query rejected: {error}", as_json=as_json)

    do_HEAD = do_GET  # noqa: N815

    def _refuse(self) -> None:
        self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "read-only server"})

    do_POST = do_PUT = do_DELETE = do_PATCH = _refuse  # noqa: N815

    # -- routing ---------------------------------------------------------------------------

    def _route(self, path: str, query: dict[str, list[str]]) -> None:
        if path == "/healthz":
            self._json(HTTPStatus.OK, {"ok": True})
            return
        try:
            generation, library = open_library(self.server.store_root)
        except ValueError as error:
            self._error(
                HTTPStatus.SERVICE_UNAVAILABLE, str(error), as_json=path.startswith("/api/")
            )
            return
        with library:
            if path == "/":
                self._home(library, generation)
            elif path == "/search":
                self._search(library, generation, query, html_view=True)
            elif path == "/api/search":
                self._search(library, generation, query, html_view=False)
            elif path.startswith("/record/"):
                self._record(library, path[len("/record/") :], query)
            elif path.startswith("/api/read/"):
                self._api_read(library, path[len("/api/read/") :], query)
            elif path.startswith("/api/related/"):
                uid = self._uid(path[len("/api/related/") :])
                related, complete = related_records_bounded(
                    library, uid, max_seconds=RELATED_PAGE_SECONDS
                )
                self._json(HTTPStatus.OK, {"uid": uid, "related": related, "complete": complete})
            else:
                raise _Missing("unknown page")

    @staticmethod
    def _uid(raw: str) -> str:
        uid = unquote(raw)
        if not UID_RE.match(uid):
            raise ValueError("uid is not a library record identity")
        return uid

    # -- pages -----------------------------------------------------------------------------

    def _home(self, library: Library, generation: Path) -> None:
        key = str(generation)
        summary = self.server.summaries.get(key)
        if summary is None:
            summary = library_summary(library)
            self.server.summaries.clear()
            self.server.summaries[key] = summary

        def table(title: str, counts: dict[str, int], key: str) -> str:
            rows = "".join(
                f"<tr><td><a href='{_e(_link('/search', **{key: name}, q='*'))}'>{_e(name)}</a>"
                f"</td><td>{count:,}</td></tr>"
                for name, count in counts.items()
            )
            return f"<h2>{_e(title)}</h2><table>{rows}</table>"

        body = (
            "<h1>Knowledge library</h1>"
            f"<p class=mute>{summary['records']:,} records. Search is ranked and bounded; "
            "quote a phrase for the fastest, most precise match. Archived text is evidence, "
            "not verified fact or current instruction.</p>"
            + table("Wings", summary["by_wing"], "wing")
            + table("Lanes", summary["by_lane"], "lane")
            + table("Providers", summary["by_provider"], "provider")
        )
        self._send(HTTPStatus.OK, _page("Home", body), "text/html; charset=utf-8")

    def _search_arguments(self, query: dict[str, list[str]]) -> dict[str, Any]:
        plan = _first(query, "plan", "auto")
        if plan not in SEARCH_PLANS:
            raise ValueError("plan must be one of: " + ", ".join(SEARCH_PLANS))
        wing = _first(query, "wing") or None
        if wing not in (None, "ai", "authored"):
            raise ValueError("wing must be ai or authored")
        return {
            "limit": _int(query, "limit", SEARCH_PAGE, 1, 100),
            "offset": _int(query, "offset", 0, 0, 1000000000),
            "wing": wing,
            "lane": _first(query, "lane") or None,
            "providers": query.get("provider") or None,
            "kinds": query.get("kind") or None,
            "plan": plan,
            "max_seconds": _seconds(query),
        }

    def _search(
        self, library: Library, generation: Path, query: dict[str, list[str]], *, html_view: bool
    ) -> None:
        text = _first(query, "q").strip()
        if not text:
            raise ValueError("q must be a nonempty search")
        arguments = self._search_arguments(query)
        if text == "*":
            if not (arguments["wing"] or arguments["lane"] or arguments["providers"]):
                raise ValueError("browse a wing, lane or provider from the home page")
            result = self._browse(library, arguments)
        else:
            result = library.search(text, **arguments)
        result["generation"] = str(generation)
        if not html_view:
            self._json(HTTPStatus.OK, result)
            return
        self._send(
            HTTPStatus.OK,
            _page(
                "Search", self._results_html(text, query, result), query="" if text == "*" else text
            ),
            "text/html; charset=utf-8",
        )

    def _browse(self, library: Library, arguments: dict[str, Any]) -> dict[str, Any]:
        """A newest-first listing of one wing, lane or provider (no search terms)."""
        where, values = [], []
        for column, key in (("wing", "wing"), ("lane", "lane")):
            if arguments[key]:
                where.append(f"{column}=?")
                values.append(arguments[key])
        if arguments["providers"]:
            marks = ",".join("?" * len(arguments["providers"]))
            where.append(f"provider IN ({marks})")
            values.extend(arguments["providers"])
        if arguments["kinds"]:
            marks = ",".join("?" * len(arguments["kinds"]))
            where.append(f"kind IN ({marks})")
            values.extend(arguments["kinds"])
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        connection = library.connection
        total = connection.execute(f"SELECT COUNT(*) FROM records{clause}", values).fetchone()[0]
        rows = connection.execute(
            "SELECT uid,title,provider,kind,wing,lane,current_version AS version,body_status,"
            f"message_count,source_ref FROM records{clause} "
            "ORDER BY COALESCE(updated,created,'') DESC, uid LIMIT ? OFFSET ?",
            [*values, arguments["limit"], arguments["offset"]],
        ).fetchall()
        offset = arguments["offset"]
        more = offset + len(rows) < total
        return {
            "query": "*",
            "total": total,
            "total_label": str(total),
            "total_exact": True,
            "offset": offset,
            "limit": arguments["limit"],
            "next_offset": offset + len(rows) if more else None,
            "hits": [{**dict(row), "excerpt": ""} for row in rows],
            "plan": "browse",
            "partial": False,
            "ranked": False,
        }

    def _results_html(self, text: str, query: dict[str, list[str]], result: dict[str, Any]) -> str:
        persist = {
            key: _first(query, key) for key in ("wing", "lane", "plan", "max_seconds", "limit")
        }
        providers = query.get("provider") or []

        def paged(offset: int) -> str:
            pairs: list[tuple[str, str]] = [("q", text), ("offset", str(offset))]
            pairs += [(key, value) for key, value in persist.items() if value]
            pairs += [("provider", value) for value in providers]
            pairs += [("kind", value) for value in query.get("kind") or []]
            return "/search?" + urlencode(pairs)

        head = (
            f"<h1>{'Browse' if text == '*' else 'Results for ' + _e(text)}</h1>"
            f"<p class=mute>{_e(result.get('total_label', result.get('total')))} matches"
            f" &middot; plan {_e(result.get('plan'))}"
            f"{' &middot; ranked' if result.get('ranked') else ' &middot; not ranked'}</p>"
        )
        notes = ""
        for key in ("notice", "hint"):
            if result.get(key):
                notes += f"<div class=notice>{_e(result[key])}</div>"
        if result.get("partial"):
            notes += (
                "<div class=notice>Partial page: the time budget ran out, so these are not "
                "ranked and more may exist. Quote a phrase or add terms, or raise max_seconds."
                "</div>"
            )
        items = []
        for hit in result.get("hits", []):
            meta = " ".join(
                f"<span class=pill>{_e(hit.get(key))}</span>"
                for key in ("provider", "lane", "body_status")
                if hit.get(key)
            )
            excerpt = (
                f"<details><summary>Excerpt</summary><pre class=text>{_e(hit['excerpt'])}</pre>"
                "</details>"
                if hit.get("excerpt")
                else ""
            )
            target = _link(record_path(hit["uid"]), q=None if text == "*" else text)
            items.append(
                f"<div class=hit><a class=t href='{_e(target)}'>{_e(hit.get('title') or hit['uid'])}"
                f"</a><div>{meta}</div>{excerpt}</div>"
            )
        body = head + notes + ("".join(items) or "<p>No matches.</p>")
        previous, following = result.get("offset", 0), result.get("next_offset")
        limit = result.get("limit", SEARCH_PAGE)
        links = []
        if previous:
            links.append(f"<a href='{_e(paged(max(0, previous - limit)))}'>&larr; Previous</a>")
        else:
            links.append("<span></span>")
        if following is not None:
            links.append(f"<a href='{_e(paged(following))}'>Next &rarr;</a>")
        return body + f"<nav class=pager>{''.join(links)}</nav>"

    def _api_read(self, library: Library, raw: str, query: dict[str, list[str]]) -> None:
        uid = self._uid(raw)
        arguments: dict[str, Any] = {"uid": uid}
        for name in ("message", "offset", "limit", "message_end", "toc_offset", "toc_limit"):
            if _first(query, name) != "":
                arguments[name] = _int(query, name, 0, 0, 1000000000)
        for name in ("sequence", "around_query", "version", "digest", "toc_mode"):
            if _first(query, name):
                arguments[name] = _first(query, name)
        if _first(query, "toc") in ("1", "true"):
            arguments["toc"] = True
        self._json(HTTPStatus.OK, read_arguments(library, arguments, *read_cursor(arguments)))

    def _record(self, library: Library, raw: str, query: dict[str, list[str]]) -> None:
        parts = raw.split("/")
        uid = self._uid(parts[0])
        if len(parts) > 2 or (len(parts) == 2 and parts[1] not in SUBPAGES):
            raise _Missing("unknown page")
        if (
            library.connection.execute("SELECT 1 FROM records WHERE uid=?", (uid,)).fetchone()
            is None
        ):
            raise _Missing("no such record")
        sequence = _first(query, "sequence", "main")
        if sequence not in ("main", "alternate"):
            raise ValueError("sequence must be main or alternate")
        if len(parts) == 2:
            if parts[1] == "history":
                self._history(library, uid)
            elif parts[1] == "contents":
                self._contents(library, uid, query, sequence)
            else:
                self._related(library, uid)
            return
        around = _first(query, "q").strip()
        message = _int(query, "message", 0, 0, 10000000)
        offset = _int(query, "offset", 0, 0, 1000000000)
        base: dict[str, Any] = {"uid": uid, "sequence": sequence}
        if around and _first(query, "message") == "":
            base["around_query"] = around
        else:
            base["message"] = message
            base["message_end"] = message + MESSAGES_PER_PAGE
            base["limit"] = PAGE_CHARACTERS
            if offset:
                base["offset"] = offset
                del base["message_end"]
        # One decode serves the read and the contents: decoding a record is the cost of the
        # page, and a long record is tens of megabytes of JSON.
        payload = library.record(uid)
        data = read_arguments(library, base, *read_cursor(base), payload=payload)
        contents: dict[str, Any] | None = None
        if (data.get("message_count") or 0) <= CONTENTS_INLINE_MESSAGES:
            toc_arguments = {"uid": uid, "toc": True, "toc_limit": 60, "sequence": sequence}
            contents = read_arguments(
                library, toc_arguments, *read_cursor(toc_arguments), payload=payload
            )
        related, related_complete = related_records_bounded(
            library, uid, max_seconds=RELATED_INLINE_SECONDS
        )
        self._send(
            HTTPStatus.OK,
            _page(
                data.get("title") or uid,
                self._record_html(
                    uid,
                    data,
                    contents,
                    related,
                    related_complete=related_complete,
                    around=around,
                    sequence=sequence,
                ),
                query=around,
            ),
            "text/html; charset=utf-8",
        )

    def _record_html(
        self,
        uid: str,
        data: dict[str, Any],
        contents: dict[str, Any] | None,
        related: list[dict[str, Any]],
        *,
        related_complete: bool,
        around: str,
        sequence: str,
    ) -> str:
        here = record_path(uid)
        pills = "".join(
            f"<span class=pill>{_e(data.get(key))}</span>"
            for key in ("provider", "kind", "body_status")
            if data.get(key)
        )
        body = (
            f"<h1>{_e(data.get('title') or uid)}</h1><div>{pills}"
            f"<span class=pill>version {_e(data.get('version'))}</span>"
            f"<span class=pill>{_e(data.get('message_count'))} messages</span></div>"
            f"<p class=mute><a href='{_e(here)}/history'>Revision history</a> &middot; "
            f"<a href='{_e('/api/read/' + quote(uid, safe=''))}'>JSON</a>"
            f" &middot; <code>{_e(uid)}</code></p>"
            f"<div class=notice>{_e(data.get('source_notice'))}</div>"
        )
        if data.get("alternate_message_count"):
            other = "alternate" if sequence == "main" else "main"
            body += (
                f"<p class=mute>Viewing the {_e(sequence)} sequence. "
                f"<a href='{_e(_link(here, sequence=other))}'>View the {other} sequence "
                f"({_e(data.get('alternate_message_count' if other == 'alternate' else 'main_message_count'))}"
                " messages)</a></p>"
            )
        headings = (contents or {}).get("toc") or []
        if contents is None:
            body += (
                f"<p class=mute><a href='{_e(_link(here + '/contents', sequence=sequence))}'>"
                f"Contents of this {_e(data.get('message_count'))}-message record</a> "
                "(opens on demand: it reads every message)</p>"
            )
        if headings:
            entries = "".join(
                f"<li style='margin-left:{max(0, int(h.get('level', 1)) - 1)}rem'>"
                f"<a href='{_e(_link(here, message=h['message'], sequence=sequence))}#message-{int(h['message'])}'>"
                f"{_e(h.get('heading') or h.get('preview') or 'message ' + str(h['message']))}</a></li>"
                for h in headings
            )
            kind = "Contents" if (contents or {}).get("toc_kind") == "headings" else "Messages"
            body += f"<details><summary>{kind} ({len(headings)}{'+' if (contents or {}).get('toc_next_offset') is not None else ''})</summary><ul>{entries}</ul></details>"
        if data.get("match") is None and around:
            body += "<div class=notice>No match for the search words in this record.</div>"
        shown = data.get("messages") or [data]
        for item in shown:
            number = item.get("message", 0)
            text = item.get("text", "")
            match = data.get("match")
            if (
                match
                and match.get("message") == number
                and match.get("end", 0) > match.get("start", 0)
            ):
                start, end = (
                    match["start"] - item.get("offset", 0),
                    match["end"] - item.get("offset", 0),
                )
                if 0 <= start < end <= len(text):
                    rendered = (
                        f"{_e(text[:start])}<mark>{_e(text[start:end])}</mark>{_e(text[end:])}"
                    )
                else:
                    rendered = _e(text)
            else:
                rendered = _e(text)
            continued = ""
            body += (
                f"<div class=msg id='message-{int(number)}'><div class=who>message {int(number)}"
                f" &middot; {_e(item.get('role'))}"
                f"{' &middot; ' + _e(item.get('channel')) if item.get('channel') else ''}"
                f"</div><pre class=text>{rendered}</pre>{continued}</div>"
            )
        pager = []
        resume_message, resume_offset = data.get("next_message"), data.get("next_offset")
        first = shown[0].get("message", 0) if shown else 0
        last = shown[-1].get("message", 0) if shown else 0
        total = (
            data.get("message_count") if sequence == "main" else data.get("alternate_message_count")
        )
        if first > 0:
            previous = max(0, first - MESSAGES_PER_PAGE) if len(shown) > 1 else first - 1
            pager.append(
                f"<a href='{_e(_link(here, message=previous, sequence=sequence))}'>&larr; Earlier messages</a>"
            )
        else:
            pager.append("<span></span>")
        if resume_offset:
            # The read stopped inside a message: resume at the message the cursor names (the one
            # being read when a single message was asked for), at the offset it names.
            target = resume_message if resume_message is not None else last
            pager.append(
                f"<a href='{_e(_link(here, message=target, offset=resume_offset, sequence=sequence))}'>Continue this message &rarr;</a>"
            )
        else:
            following = resume_message if resume_message is not None else last + 1
            if isinstance(total, int) and following < total:
                pager.append(
                    f"<a href='{_e(_link(here, message=following, sequence=sequence))}'>Later messages &rarr;</a>"
                )
        body += f"<nav class=pager>{''.join(pager)}</nav>"
        if not related_complete:
            body += (
                f"<h2>Related records</h2><p class=mute>The list is not ready within "
                f"{RELATED_INLINE_SECONDS:g} s for this record. "
                f"<a href='{_e(here + '/related')}'>Compute it (up to "
                f"{RELATED_PAGE_SECONDS:g} s)</a>.</p>"
            )
        elif related:
            items = "".join(
                f"<li><a href='{_e(record_path(r['uid']))}'>{_e(r['title'] or r['uid'])}</a> "
                f"<span class=mute>{_e(r['provider'])} &middot; {int(r['shared'])} shared passages</span></li>"
                for r in related
            )
            body += f"<h2>Related records</h2><p class=mute>Records that repeat passages found here.</p><ul>{items}</ul>"
        return body

    def _contents(
        self, library: Library, uid: str, query: dict[str, list[str]], sequence: str
    ) -> None:
        """The headings (or a per-message index) of one record, a page at a time. It reads every
        message, so it is its own page and a long record's body does not wait for it."""
        start = _int(query, "toc_offset", 0, 0, 1000000000)
        arguments: dict[str, Any] = {
            "uid": uid,
            "toc": True,
            "toc_offset": start,
            "toc_limit": CONTENTS_PAGE,
            "sequence": sequence,
        }
        data = read_arguments(library, arguments, *read_cursor(arguments))
        here = record_path(uid)
        entries = "".join(
            f"<li><a href='{_e(_link(here, message=e['message'], sequence=sequence))}"
            f"#message-{int(e['message'])}'>{int(e['message'])}. "
            f"{_e(e.get('heading') or e.get('preview') or 'message ' + str(e['message']))}"
            f"</a></li>"
            for e in data.get("toc") or []
        )
        kind = "Contents" if data.get("toc_kind") == "headings" else "Messages"
        links = []
        if start:
            before = max(0, start - CONTENTS_PAGE)
            links.append(
                f"<a href='{_e(_link(here + '/contents', toc_offset=before or None, sequence=sequence))}'>"
                "&larr; Previous</a>"
            )
        else:
            links.append("<span></span>")
        following = data.get("toc_next_offset")
        if following is not None:
            links.append(
                f"<a href='{_e(_link(here + '/contents', toc_offset=following, sequence=sequence))}'>"
                "Next &rarr;</a>"
            )
        body = (
            f"<h1>{kind}</h1><p><a href='{_e(_link(here, sequence=sequence))}'>&larr; "
            f"{_e(data.get('title') or uid)}</a></p>"
            f"<p class=mute>{int(data.get('toc_total', 0)):,} entries</p>"
            f"<ol style='list-style:none;padding:0'>{entries}</ol>"
            f"<nav class=pager>{''.join(links)}</nav>"
        )
        self._send(
            HTTPStatus.OK,
            _page(f"{kind} of {data.get('title') or uid}", body),
            "text/html; charset=utf-8",
        )

    def _related(self, library: Library, uid: str) -> None:
        related, complete = related_records_bounded(library, uid, max_seconds=RELATED_PAGE_SECONDS)
        if not complete:
            body_text = (
                f"<div class=notice>The list was not ready within {RELATED_PAGE_SECONDS:g} s, "
                "so none is shown: a partial count would rank records wrongly.</div>"
            )
        elif related:
            body_text = (
                "<ul>"
                + "".join(
                    f"<li><a href='{_e(record_path(r['uid']))}'>{_e(r['title'] or r['uid'])}</a> "
                    f"<span class=mute>{_e(r['provider'])} &middot; {int(r['shared'])} shared passages</span></li>"
                    for r in related
                )
                + "</ul>"
            )
        else:
            body_text = "<p>No other record repeats a passage found here.</p>"
        body = (
            f"<h1>Related records</h1><p><a href='{_e(record_path(uid))}'>&larr; {_e(uid)}</a></p>"
            f"<p class=mute>Records that repeat eligible passages of this one.</p>{body_text}"
        )
        self._send(HTTPStatus.OK, _page("Related records", body), "text/html; charset=utf-8")

    def _history(self, library: Library, uid: str) -> None:
        revisions = library.history(uid)
        snapshots = library.snapshots(uid)
        rows = "".join(
            f"<tr><td>{_e(r['version'])}</td><td>{_e(r['change_kind'])}</td>"
            f"<td class=mute>{_e(r['digest'][:16])}</td></tr>"
            for r in revisions
        )
        body = (
            f"<h1>Revision history</h1><p><a href='{_e(record_path(uid))}'>&larr; {_e(uid)}</a></p>"
            f"<table><tr><th>Version</th><th>Change</th><th>Digest</th></tr>{rows}</table>"
            f"<p class=mute>{len(snapshots)} retained source snapshots.</p>"
        )
        self._send(HTTPStatus.OK, _page("History", body), "text/html; charset=utf-8")


def start_in_thread(store_root: Path, port: int = 0) -> tuple[LibraryServer, threading.Thread]:
    server = LibraryServer(("127.0.0.1", port), store_root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def serve(store_root: Path, *, host: str = "127.0.0.1", port: int = 8766) -> None:
    open_library(store_root)[1].close()
    server = LibraryServer((host, port), store_root)
    print(
        f"cce library: http://127.0.0.1:{server.server_address[1]}/  (Ctrl-C to stop)", flush=True
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


__all__ = [
    "DEFAULT_SEARCH_SECONDS",
    "LibraryServer",
    "related_records",
    "related_records_bounded",
    "library_summary",
    "serve",
    "start_in_thread",
]
