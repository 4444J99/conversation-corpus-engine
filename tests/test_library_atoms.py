"""Atoms: deduplicated passages as linked, citable units, over the library and the local wiki.

Every record is synthetic. The wiki is driven over real loopback HTTP.
"""

from __future__ import annotations

import hashlib
import http.client
import json
from urllib.parse import quote

import pytest

from conversation_corpus_engine import library_atoms, library_serve
from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.library_store import Library

ONE = (
    "The first shared passage appears in two records and is long enough to be an atom because "
    "the library only keeps eligible prose of at least one hundred and twenty characters."
)
TWO = (
    "A second passage that only the first record holds, also long enough to be stored as an atom "
    "of its own once the block index has been built for this synthetic library generation."
)
HOSTILE = "<script>alert(1)</script> " + "padding words to make this passage long enough " * 4


def record(source_id, title, texts, provider="chatgpt"):
    return Conversation(
        provider=provider,
        source_id=source_id,
        title=title,
        messages=[Message(role="user", text=text) for text in texts],
    )


@pytest.fixture
def library_path(tmp_path):
    path = tmp_path / "library.sqlite"
    with Library.create(path) as library:
        library.put(record("a", "Alpha", [ONE + "\n\n" + TWO, "short"]))
        library.put(record("b", "Beta", [ONE, HOSTILE], provider="claude"))
        library.put(record("c", "Gamma", ["tiny only"]))
        library.index_blocks()
    return path


def digest_of(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_record_atoms_lists_passages_in_reading_order_with_sharing(library_path):
    uid = record("a", "Alpha", []).uid
    with Library(library_path) as library:
        page = library_atoms.record_atoms(library, uid)
    assert page["total"] == 2 and page["next_offset"] is None
    first, second = page["atoms"]
    assert [first["position"], second["position"]] == [0, 1]
    assert first["digest"] == digest_of(ONE) and first["shared_records"] == 1
    assert second["digest"] == digest_of(TWO) and second["shared_records"] == 0
    assert first["preview"] == ONE[: library_atoms.CARD_CHARS]


def test_record_atoms_pages_without_repeating(library_path):
    uid = record("a", "Alpha", []).uid
    with Library(library_path) as library:
        one = library_atoms.record_atoms(library, uid, limit=1)
        two = library_atoms.record_atoms(library, uid, limit=1, offset=one["next_offset"])
    assert one["next_offset"] == 1 and two["next_offset"] is None
    assert one["atoms"][0]["digest"] != two["atoms"][0]["digest"]


def test_a_record_with_no_atoms_has_total_zero(library_path):
    with Library(library_path) as library:
        page = library_atoms.record_atoms(library, record("c", "Gamma", []).uid)
    assert page == {
        "uid": record("c", "Gamma", []).uid,
        "total": 0,
        "offset": 0,
        "limit": 25,
        "next_offset": None,
        "atoms": [],
    }


def test_sharing_counts_are_skipped_when_the_time_is_up(library_path):
    uid = record("a", "Alpha", []).uid
    with Library(library_path) as library:
        page = library_atoms.record_atoms(library, uid, max_seconds=-1.0)
    assert [a["shared_records"] for a in page["atoms"]] == [None, None]
    assert [a["shared_exact"] for a in page["atoms"]] == [False, False]


def test_atom_reports_text_occurrences_and_neighbours(library_path):
    a_uid, b_uid = record("a", "Alpha", []).uid, record("b", "Beta", [], provider="claude").uid
    with Library(library_path) as library:
        data = library_atoms.atom(library, digest_of(ONE))
        inside_b = library_atoms.atom(library, digest_of(ONE), uid=b_uid, message=0, block=0)
    assert data["text"] == ONE and data["chars"] == len(ONE)
    assert [o["uid"] for o in data["occurrences"]] == sorted([a_uid, b_uid])
    assert data["occurrence_total"] == 2 and data["occurrence_total_exact"] is True
    # The first occurrence (by uid) supplies the neighbours: Alpha holds TWO right after ONE.
    assert data["context"]["uid"] == a_uid
    assert data["context"]["previous"] is None
    assert data["context"]["next"]["digest"] == digest_of(TWO)
    # In Beta the next message holds the hostile passage.
    assert inside_b["context"]["uid"] == b_uid and inside_b["context"]["previous"] is None
    assert inside_b["context"]["next"]["preview"].startswith("<script>")


def test_atom_refuses_bad_digests_unknown_atoms_and_wrong_occurrences(library_path):
    with Library(library_path) as library:
        with pytest.raises(ValueError):
            library_atoms.atom(library, "nothex")
        with pytest.raises(KeyError):
            library_atoms.atom(library, "0" * 64)
        with pytest.raises(ValueError, match="does not hold"):
            library_atoms.atom(
                library,
                digest_of(TWO),
                uid=record("b", "Beta", [], provider="claude").uid,
                message=0,
                block=0,
            )


def test_atom_text_is_clipped_and_says_so(library_path, monkeypatch):
    monkeypatch.setattr(library_atoms, "ATOM_TEXT_LIMIT", 40)
    with Library(library_path) as library:
        data = library_atoms.atom(library, digest_of(ONE))
    assert data["text"] == ONE[:40] and data["text_truncated"] is True


@pytest.fixture
def wiki(library_path, tmp_path, monkeypatch):
    monkeypatch.setattr(
        library_serve, "open_library", lambda root: (tmp_path, Library(library_path))
    )
    server, thread = library_serve.start_in_thread(tmp_path)
    port = server.server_address[1]

    def get(target):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        connection.request("GET", target, headers={"Host": f"127.0.0.1:{port}"})
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        connection.close()
        return response.status, body

    yield get
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_the_record_page_links_to_its_atoms_and_the_atoms_page_lists_them(wiki):
    uid = record("a", "Alpha", []).uid
    status, page = wiki("/record/" + quote(uid, safe=""))
    assert status == 200 and f"/record/{quote(uid, safe='')}/atoms" in page
    status, atoms = wiki(f"/record/{quote(uid, safe='')}/atoms")
    assert status == 200
    assert digest_of(ONE) in atoms and digest_of(TWO) in atoms
    assert "also in 1 other records" in atoms and "2 passages stored once each" in atoms


def test_the_atom_page_shows_text_places_and_neighbours_and_escapes_markup(wiki, library_path):
    b_uid = record("b", "Beta", [], provider="claude").uid
    status, page = wiki(f"/atom/{digest_of(ONE)}?uid={quote(b_uid, safe='')}&message=0&block=0")
    assert status == 200 and "Where it occurs (2)" in page and "Alpha" in page and "Beta" in page
    assert "&lt;script&gt;" in page and "<script>" not in page
    with Library(library_path) as library:
        hostile_digest = library_atoms.record_atoms(library, b_uid)["atoms"][1]["digest"]
    status, hostile = wiki(f"/atom/{hostile_digest}")
    assert status == 200 and "<script>alert" not in hostile and "&lt;script&gt;" in hostile


def test_the_atom_api_matches_the_library_and_bad_requests_fail_cleanly(wiki, library_path):
    status, body = wiki(f"/api/atom/{digest_of(TWO)}")
    assert status == 200
    data = json.loads(body)
    assert data["text"] == TWO and data["occurrence_total"] == 1
    assert wiki("/atom/" + "f" * 64)[0] == 404
    assert wiki("/atom/not-a-digest")[0] == 400
    assert wiki(f"/atom/{digest_of(TWO)}?uid=x&message=0&block=0")[0] == 400
    assert wiki(f"/record/{quote(record('a', 'Alpha', []).uid, safe='')}/atoms?offset=-1")[0] == 400
