"""The local library wiki (`cce library serve`) on a synthetic library.

Every record here is synthetic. The server is started on an ephemeral loopback port and driven
over real HTTP, so the Host check, headers, escaping and paging are exercised as a browser would.
"""

from __future__ import annotations

import http.client
import json
from urllib.parse import quote

import pytest

from conversation_corpus_engine import library_serve
from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.library_store import Library

HOSTILE = "<script>alert(1)</script> & <img src=x onerror=alert(2)>"
SHARED = (
    "The shared passage that appears in two records and is long enough to be a block because "
    "the library only condenses eligible prose of at least one hundred and twenty characters.\n\n"
)


def _record(source_id: str, title: str, texts: list[str], provider: str = "chatgpt"):
    return Conversation(
        provider=provider,
        source_id=source_id,
        title=title,
        messages=[
            Message(role="user" if index % 2 == 0 else "assistant", text=text)
            for index, text in enumerate(texts)
        ],
    )


@pytest.fixture
def wiki(tmp_path, monkeypatch):
    path = tmp_path / "library.sqlite"
    many = [f"filler message {n} about nothing" for n in range(45)]
    records = [
        _record("a", "Lattice notes", ["# Heading One\nlattice ledger entries", SHARED + "tail"]),
        _record("b", "Second record", [SHARED + "other tail", "plain"], provider="claude"),
        _record("c", HOSTILE, [HOSTILE, "ledger " + HOSTILE]),
        _record("d", "Long thread", ["needle in the first message", *many, "needle again late"]),
    ]
    with Library.create(path) as library:
        for record in records:
            library.put(record)
        library.index_blocks()
    monkeypatch.setattr(library_serve, "open_library", lambda root: (tmp_path, Library(path)))
    server, thread = library_serve.start_in_thread(tmp_path)
    port = server.server_address[1]

    def get(target, host=None, method="GET"):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        connection.request(method, target, headers={"Host": host or f"127.0.0.1:{port}"})
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        headers = dict(response.getheaders())
        connection.close()
        return response.status, body, headers

    get.records = {r.source_id: r.uid for r in records}
    get.port = port
    yield get
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_home_lists_counts_and_browse_links(wiki):
    status, body, headers = wiki("/")
    assert status == 200
    assert "4 records" in body
    assert "chatgpt" in body and "claude" in body
    assert "no-store" in headers["Cache-Control"]
    assert "script" not in headers["Content-Security-Policy"].replace("default-src 'none'", "")


def test_search_returns_a_one_line_hit_with_collapsed_excerpt(wiki):
    status, body, _ = wiki("/search?q=lattice")
    assert status == 200
    assert "Lattice notes" in body
    assert "<details><summary>Excerpt</summary>" in body
    assert quote(wiki.records["a"], safe="") in body


def test_search_json_matches_the_cli_shape(wiki):
    status, body, headers = wiki("/api/search?q=lattice")
    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    payload = json.loads(body)
    assert payload["hits"][0]["uid"] == wiki.records["a"]
    assert payload["plan"] in {"and", "phrase"}


def test_hostile_text_is_escaped_everywhere(wiki):
    for target in (
        "/search?q=ledger",
        f"/record/{quote(wiki.records['c'], safe='')}",
        "/search?q=" + quote("<script>alert(1)</script>"),
    ):
        status, body, _ = wiki(target)
        assert "<script" not in body.lower()
        assert "onerror=alert" not in body.replace("&quot;", '"').replace("&#x27;", "'") or (
            "&lt;img" in body
        )
        assert "<img" not in body.lower()
    assert (
        "&lt;script&gt;alert(1)&lt;/script&gt;"
        in wiki(f"/record/{quote(wiki.records['c'], safe='')}")[1]
    )


def test_record_page_shows_contents_messages_and_related(wiki):
    status, body, _ = wiki(f"/record/{quote(wiki.records['a'], safe='')}")
    assert status == 200
    assert "Heading One" in body
    assert "id='message-0'" in body and "id='message-1'" in body
    assert "Related records" in body
    assert "Second record" in body
    assert "Revision history" in body


def test_record_pages_long_threads_with_earlier_and_later_links(wiki):
    uid = quote(wiki.records["d"], safe="")
    status, first, _ = wiki(f"/record/{uid}")
    assert status == 200
    assert "id='message-19'" in first and "id='message-20'" not in first
    assert "Later messages" in first and "Earlier messages" not in first
    _, second, _ = wiki(f"/record/{uid}?message=20")
    assert "id='message-20'" in second and "id='message-39'" in second
    assert "Earlier messages" in second and "Later messages" in second
    _, last, _ = wiki(f"/record/{uid}?message=40")
    assert "id='message-46'" in last and "Later messages" not in last


def test_around_query_jumps_to_the_match_and_marks_it(wiki):
    uid = quote(wiki.records["d"], safe="")
    status, body, _ = wiki(f"/record/{uid}?q=late")
    assert status == 200
    assert "<mark>" in body
    assert "needle again late" in body.replace("<mark>", "").replace("</mark>", "")


def test_history_page(wiki):
    status, body, _ = wiki(f"/record/{quote(wiki.records['a'], safe='')}/history")
    assert status == 200
    assert "1.0.0" in body and "initial" in body


def test_browse_a_provider_and_page(wiki):
    status, body, _ = wiki("/search?q=*&provider=claude")
    assert status == 200 and "Second record" in body and "Lattice notes" not in body
    status, body, _ = wiki("/search?q=*&wing=ai&limit=1")
    assert status == 200 and "Next" in body
    status, body, _ = wiki("/search?q=*")
    assert status == 400


def test_bad_requests_are_refused_without_a_traceback(wiki):
    assert wiki("/search?q=")[0] == 400
    assert wiki("/search?q=x&plan=bogus")[0] == 400
    assert wiki("/search?q=x&limit=500")[0] == 400
    assert wiki("/search?q=x&max_seconds=0")[0] == 400
    assert wiki("/record/does-not-exist")[0] == 404
    assert wiki("/record/..%2F..%2Fetc%2Fpasswd")[0] == 400
    assert wiki("/record/x/y/z")[0] in {400, 404}
    assert wiki("/nope")[0] == 404
    assert wiki("/api/read/" + quote(wiki.records["a"], safe="") + "?message=-1")[0] == 400


def test_read_api_serves_the_toc(wiki):
    status, body, _ = wiki(f"/api/read/{quote(wiki.records['a'], safe='')}?toc=1")
    assert status == 200
    assert json.loads(body)["toc"][0]["heading"] == "Heading One"


def test_related_api(wiki):
    status, body, _ = wiki(f"/api/related/{quote(wiki.records['a'], safe='')}")
    assert status == 200
    assert [r["uid"] for r in json.loads(body)["related"]] == [wiki.records["b"]]


def test_host_header_must_be_loopback(wiki):
    assert wiki("/", host="evil.example")[0] == 403
    assert wiki("/api/search?q=x", host="evil.example")[0] == 403
    assert wiki("/", host=f"localhost:{wiki.port}")[0] == 200


def test_read_only_methods(wiki):
    connection = http.client.HTTPConnection("127.0.0.1", wiki.port, timeout=10)
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        connection.request(method, "/search?q=x", headers={"Host": f"127.0.0.1:{wiki.port}"})
        response = connection.getresponse()
        response.read()
        assert response.status == 405
    connection.close()
    assert wiki("/", method="HEAD")[1] == ""


def test_binds_loopback_only(tmp_path):
    with pytest.raises(ValueError, match="loopback"):
        library_serve.LibraryServer(("0.0.0.0", 0), tmp_path)


def test_unpublished_store_is_a_503(tmp_path):
    server, thread = library_serve.start_in_thread(tmp_path)
    try:
        port = server.server_address[1]
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        connection.request("GET", "/", headers={"Host": f"127.0.0.1:{port}"})
        response = connection.getresponse()
        assert response.status == 503
        assert "no library generation" in response.read().decode()
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
