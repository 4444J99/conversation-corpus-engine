from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.request
from html.parser import HTMLParser

import pytest
from archive_support import XSS, conv, topical_corpus, write_store

from conversation_corpus_engine import archive_site
from conversation_corpus_engine.archive_database import build_database
from conversation_corpus_engine.archive_ingest import ingest
from conversation_corpus_engine.archive_limits import Limits
from conversation_corpus_engine.archive_model import Message
from conversation_corpus_engine.archive_serve import ArchiveServer, start_in_thread
from conversation_corpus_engine.archive_site import build_site, render_message_html, render_page
from conversation_corpus_engine.archive_topics import build_analysis


def test_page_navigation_resolves_only_the_neighbours(monkeypatch) -> None:
    calls: list[int] = []
    real = archive_site._relative

    def counting(target: str, source: str) -> str:
        calls.append(1)
        return real(target, source)

    monkeypatch.setattr(archive_site, "_relative", counting)
    count = 60
    pages = archive_site.render_pages(
        conv("chatgpt", "long", "Long thread"),
        [[f"<p>{number}</p>"] for number in range(count)],
        [f"pages/undated/page-{number:03d}.html" for number in range(count)],
        related=[],
        topics=[],
        titles={},
        page_of=lambda _uid: None,
        note_bytes=Limits().note_bytes,
    )
    assert len(pages) == count
    assert len(calls) <= 6 * count, "navigation links must not cost a path per page per page"
    assert "Previous" not in pages[0] and "Next" in pages[0]
    assert "Next" not in pages[-1] and "Previous" in pages[-1]
    assert '<a href="page-004.html">← Previous</a>' in pages[5]
    assert '<a href="page-006.html">Next →</a>' in pages[5]


def test_a_small_page_cap_still_holds_a_conversation_that_fits_one_page() -> None:
    pages = render_page(
        conv("chatgpt", "tiny", "test"), related=[], topics=[], titles={}, note_bytes=4096
    )
    assert len(pages) == 1 and len(pages[0].encode("utf-8")) <= 4096
    heavy = conv("chatgpt", "heavy", "test", [("user", "word " * 2000), ("assistant", "ok")])
    with pytest.raises(ValueError, match="no room"):
        render_page(heavy, related=[], topics=[], titles={}, note_bytes=4096)


class _Scripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[dict[str, str | None]] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self.scripts.append(dict(attrs))
        if tag == "a" and dict(attrs).get("href"):
            self.links.append(str(dict(attrs)["href"]))


@pytest.fixture
def built(archive_env):
    ingest(export_root=archive_env.export, paths=archive_env.paths, authorization=archive_env.auth)
    build_analysis(archive_env.paths)
    build_database(archive_env.paths, archive_env.auth)
    summary = build_site(archive_env.paths, archive_env.auth)
    return archive_env, summary


def test_site_has_shell_data_and_one_page_per_conversation(built) -> None:
    env, summary = built
    root = env.paths.site
    assert summary["pages"] == 24
    for name in (
        "home-all.html",
        "graph-all.html",
        "site-all.css",
        "home-all.js",
        "graph-all.js",
        "index-all-conversations.js",
        "index-all-topic-graph.js",
    ):
        assert (root / name).is_file(), name
    page = env.paths.site_page("chatgpt", "chatgpt-aaaa1111")
    assert page.is_file() and page.parent.parent == root / "conversations"
    assert page.parent.name == "month-all-202311"
    payload = (root / "index-all-conversations.js").read_text(encoding="utf-8")
    assert payload.startswith("window.CCE = ")
    data = json.loads(payload.removeprefix("window.CCE = ").rstrip(";\n"))
    assert len(data["index"]) == 24
    assert {row[1] for row in data["index"]} == {
        "chatgpt",
        "claude",
        "gemini",
        "notebooklm",
        "grok",
        "perplexity",
    }
    assert data["totals"]["conversations"] == 24
    assert next(p for p in data["providers"] if p["name"] == "gemini")["not_read"]
    graph = json.loads(
        (root / "index-all-topic-graph.js")
        .read_text(encoding="utf-8")
        .removeprefix("window.CCE_GRAPH = ")
        .rstrip(";\n")
    )
    assert graph["nodes"] and all(
        0 <= a < len(graph["nodes"]) for a, b, _ in graph["edges"] for a in (a, b)
    )


def test_conversation_pages_escape_hostile_content_and_ship_no_scripts(built) -> None:
    env, _ = built
    page = env.paths.site_page("chatgpt", "chatgpt-aaaa1111").read_text(encoding="utf-8")
    assert XSS not in page
    assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;" in page
    parser = _Scripts()
    parser.feed(page)
    assert parser.scripts == []
    assert "script-src" not in page
    assert "default-src &#x27;none&#x27;" in page
    assert "Attachments (names only)" in page and "<code>diagram.png</code>" in page
    assert "<pre><code>print(1)</code></pre>" in page


def test_every_internal_link_resolves(built) -> None:
    env, _ = built
    root = env.paths.site
    checked = 0
    for html_file in root.rglob("*.html"):
        parser = _Scripts()
        parser.feed(html_file.read_text(encoding="utf-8"))
        for href in parser.links:
            if href.startswith(("http://", "https://", "#")):
                continue
            target = (html_file.parent / href.split("#")[0]).resolve()
            assert target.is_file(), f"{html_file.name} -> {href}"
            checked += 1
    assert checked > 20


def test_app_scripts_are_external_only_and_dom_safe(built) -> None:
    env, _ = built
    index = env.paths.site_home.read_text(encoding="utf-8")
    parser = _Scripts()
    parser.feed(index)
    assert all(s.get("src") for s in parser.scripts) and len(parser.scripts) == 2
    assert [s["src"] for s in parser.scripts] == ["index-all-conversations.js", "home-all.js"]
    app = (env.paths.site / "home-all.js").read_text(encoding="utf-8")
    assert "innerHTML" not in app and "document.write" not in app and "eval(" not in app
    graph_js = (env.paths.site / "graph-all.js").read_text(encoding="utf-8")
    assert "innerHTML" not in graph_js
    assert "script-src &#x27;self&#x27;" in index


def test_collapsed_kinds_use_details_and_hostile_titles_are_escaped() -> None:
    message = Message(
        role="assistant", text="inner", ts="2025-01-01T00:00:00Z", meta={"kind": "reasoning"}
    )
    assert "<details><summary>Assistant · reasoning" in render_message_html(message)
    conversation = conv("grok", "t", '"><img src=x onerror=alert(1)>')
    page = "\n".join(render_page(conversation, related=[], topics=['x"><b>'], titles={}))
    assert "<img" not in page and "onerror=alert" not in page.replace(
        "&quot;&gt;&lt;img src=x onerror=alert(1)&gt;", ""
    )
    assert "<b>" not in page


def test_rebuild_prunes_pages_for_removed_conversations(built) -> None:
    env, first = built
    stale = env.paths.site_pages / "conversation-chatgpt-stale.html"
    stale.write_text("old", encoding="utf-8")
    again = build_site(env.paths, env.auth)
    assert again["removed_stale"] == 1 and not stale.exists()
    assert again["pages"] == first["pages"]


def test_site_build_is_deterministic(built) -> None:
    env, _ = built
    first = {
        p.relative_to(env.paths.site): p.read_bytes()
        for p in env.paths.site.rglob("*")
        if p.is_file()
    }
    build_site(env.paths, env.auth)
    assert {
        p.relative_to(env.paths.site): p.read_bytes()
        for p in env.paths.site.rglob("*")
        if p.is_file()
    } == first


@pytest.fixture
def server(built):
    env, _ = built
    srv, thread = start_in_thread(env.paths)
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


def _get(server: ArchiveServer, path: str) -> tuple[int, dict[str, str], bytes]:
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


def test_server_serves_static_pages_and_security_headers(server) -> None:
    status, headers, body = _get(server, "/")
    assert status == 200 and b"AI Archive" in body
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Cache-Control"] == "no-store"
    status, headers, _ = _get(server, "/home-all.js")
    assert status == 200 and headers["Content-Type"].startswith("text/javascript")
    page = server.paths.site_page("chatgpt", "chatgpt-aaaa1111")
    assert _get(server, "/" + page.relative_to(server.paths.site).as_posix())[0] == 200
    assert _get(server, "/index.html")[0] == 404, "the old entry-point name is gone"


def test_server_blocks_traversal_bad_hosts_and_writes(server) -> None:
    manifest = "manifest-all.json"
    assert (server.paths.root / manifest).is_file(), "the probes aim at a file that exists"
    for attempt in (
        f"/../{manifest}",
        f"/%2e%2e/{manifest}",
        f"/..%2f..%2f{manifest}",
        f"/conversations/../../{manifest}",
    ):
        assert _get(server, attempt)[0] == 404
    port = server.server_address[1]
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request("GET", "/api/ping", headers={"Host": "evil.example"})
    assert connection.getresponse().status == 403
    connection.close()
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request("POST", "/api/search", body="{}")
    assert connection.getresponse().status == 405
    connection.close()


def test_server_refuses_non_loopback_binding(built) -> None:
    env, _ = built
    with pytest.raises(ValueError, match="loopback"):
        ArchiveServer(("0.0.0.0", 0), env.paths)


def test_api_search_returns_best_hits_and_validates_input(server) -> None:
    status, _, body = _get(server, "/api/ping")
    assert status == 200 and json.loads(body) == {"ok": True}
    status, _, body = _get(server, "/api/search?q=kubernetes+cluster&limit=5")
    payload = json.loads(body)
    assert status == 200 and payload["results"]
    assert all(re.search(r"\[.+\]", hit["excerpt"]) for hit in payload["results"])
    status, _, body = _get(server, "/api/search?q=sourdough&provider=claude")
    assert {h["provider"] for h in json.loads(body)["results"]} == {"claude"}
    assert _get(server, "/api/search?q=x&limit=abc")[0] == 400
    assert json.loads(_get(server, "/api/search?q=%22")[2])["results"] == []
    assert json.loads(_get(server, "/api/search?q=OR+AND+NEAR(")[2])["results"] == []
    assert _get(server, "/api/titles?q=rollout")[0] == 200
    assert _get(server, "/api/nope")[0] == 404
    big = json.loads(_get(server, "/api/search?q=kubernetes&limit=9999")[2])
    assert len(big["results"]) <= 50


def test_api_reports_missing_search_database(archive_env) -> None:
    write_store(archive_env.paths, topical_corpus())
    archive_env.paths.site.mkdir(parents=True)
    archive_env.paths.site_home.write_text("x", encoding="utf-8")
    srv, thread = start_in_thread(archive_env.paths)
    try:
        assert _get(srv, "/api/search?q=kubernetes")[0] == 503
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def test_pages_survive_empty_conversations_and_missing_fields() -> None:
    conversation = conv("grok", "e", "")
    conversation.messages = []
    (page,) = render_page(conversation, related=[], topics=[], titles={})
    assert "<title>Untitled</title>" in page
    assert "0 messages" in page
