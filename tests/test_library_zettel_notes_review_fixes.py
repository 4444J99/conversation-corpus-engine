"""Review findings on the Zettel note emitter: names, temp files, authorship and the owner binding.

Synthetic conversations and invented text only.
"""

from __future__ import annotations

import os
import unicodedata
from pathlib import Path
from typing import Any

import pytest
from test_library_zettel_notes import (
    annotation,
    make_library,
    parse_text,
    ref,
    source,
    zettel,
)

from conversation_corpus_engine import library_zettel_notes
from conversation_corpus_engine.library_store import Library
from conversation_corpus_engine.library_zettel_notes import (
    Note,
    build_notes,
    export_zettel_notes,
    write_notes,
)

OWNER = "owner-one"


def kiln_source(library: Library, author: str = "agent") -> dict:
    return source(library, ref(library, "kiln", 1), author)


# 1. Unicode-normalised titles


def test_titles_that_differ_only_in_unicode_normalisation_are_duplicates(tmp_path):
    nfc = unicodedata.normalize("NFC", "Café notes")
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd
    with make_library(tmp_path) as library:
        a = zettel(library, 1, [kiln_source(library)], title=nfc)
        b = zettel(library, 2, [source(library, ref(library, "kiln", 2))], title=nfd)
        with pytest.raises(ValueError, match="duplicate title"):
            build_notes(library, [a, b])


def test_a_title_is_written_as_one_normalised_file_name(tmp_path):
    nfd = unicodedata.normalize("NFD", "Café notes")
    with make_library(tmp_path) as library:
        (note,) = build_notes(library, [zettel(library, 1, [kiln_source(library)], title=nfd)])
    assert note.title == unicodedata.normalize("NFC", nfd)
    assert note.filename == unicodedata.normalize("NFC", nfd) + ".md"


def test_the_write_lookup_treats_normalisation_variants_as_the_same_file(tmp_path):
    nfc = unicodedata.normalize("NFC", "Café notes")
    nfd = unicodedata.normalize("NFD", nfc)
    folder = tmp_path / "wiki" / "notes" / "zettel"
    folder.mkdir(parents=True)
    other = folder / f"{nfd}.md"
    try:
        other.write_text('---\nid: "20200101-aaaaaa"\n---\nMine.\n', encoding="utf-8")
    except OSError:
        pytest.skip("file system rejects the name")
    note = Note(
        "20260304-bbbbbb", nfc, '---\nid: "20260304-bbbbbb"\n---\nTheirs.\n', "agent", 1, ()
    )
    with pytest.raises(ValueError, match="already holds a different note"):
        write_notes([note], tmp_path / "wiki")
    assert other.read_text(encoding="utf-8").endswith("Mine.\n")


# 2. Temporary file names


def test_temporary_files_are_unique_and_created_exclusively(tmp_path, monkeypatch):
    seen: list[tuple[str, int]] = []
    real = os.open

    def recording(path, flags, *args, **kwargs):
        if str(path).endswith(".tmp"):
            seen.append((Path(path).name, flags))
        return real(path, flags, *args, **kwargs)

    monkeypatch.setattr(library_zettel_notes.os, "open", recording)
    out = tmp_path / "wiki"
    for text in ("one", "two"):
        note = Note(
            "20260304-cccccc",
            "Same note",
            f'---\nid: "20260304-cccccc"\n---\n{text}\n',
            "agent",
            1,
            (),
        )
        write_notes([note], out)
    assert len(seen) == 2
    assert seen[0][0] != seen[1][0]
    assert all(flags & os.O_EXCL for _, flags in seen)


def test_a_failed_write_leaves_no_temporary_file(tmp_path, monkeypatch):
    out = tmp_path / "wiki"
    note = Note(
        "20260304-dddddd", "Failing note", '---\nid: "20260304-dddddd"\n---\nx\n', "agent", 1, ()
    )

    def broken(self, target):
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", broken)
    with pytest.raises(OSError, match="replace failed"):
        write_notes([note], out)
    assert [p.name for p in (out / "notes" / "zettel").iterdir()] == []


# 3. File name byte limit


@pytest.mark.parametrize("title", ["漢" * 90, "\U0001f600" * 70])
def test_a_title_whose_file_name_exceeds_255_bytes_is_refused_before_writing(tmp_path, title):
    assert len((title + ".md").encode()) > 255
    with make_library(tmp_path) as library:
        entry = zettel(library, 1, [kiln_source(library)], title=title)
        with pytest.raises(ValueError, match="255 bytes"):
            build_notes(library, [entry])
        out = tmp_path / "wiki"
        with pytest.raises(ValueError, match="255 bytes"):
            export_zettel_notes(library, [entry], out, store_root=tmp_path / "store")
        assert not out.exists()


def test_a_title_at_exactly_the_byte_limit_is_accepted(tmp_path):
    title = "漢" * 83 + "abc"
    assert len((title + ".md").encode()) == 255
    with make_library(tmp_path) as library:
        (note,) = build_notes(library, [zettel(library, 1, [kiln_source(library)], title=title)])
        assert note.title == title


# 4. Title spans


def with_title(entry: dict, kind: str) -> dict:
    title = entry["title"]
    if kind == "missing":
        entry.pop("title_attribution", None)
    elif kind == "agent":
        entry["title_attribution"] = annotation(title, "agent", "model")
    elif kind == "third_party":
        entry["title_attribution"] = annotation(title, "third_party", "someone")
    elif kind == "owner":
        entry["title_attribution"] = annotation(title, "owner", OWNER)
    elif kind == "disputed":
        entry["title_attribution"] = annotation(title, "owner", OWNER, assertion_status="disputed")
    return entry


@pytest.mark.parametrize("kind", ["missing", "third_party", "owner", "disputed"])
def test_an_agent_note_needs_an_agent_title(tmp_path, kind):
    with make_library(tmp_path) as library:
        entry = with_title(zettel(library, 1, [kiln_source(library)]), kind)
        (note,) = build_notes(library, [entry])
        assert note.authorship == "generated"


def test_an_agent_note_with_an_agent_title_is_agent(tmp_path):
    with make_library(tmp_path) as library:
        entry = with_title(zettel(library, 1, [kiln_source(library)]), "agent")
        (note,) = build_notes(library, [entry])
        assert note.authorship == "agent"


# 6. Agent author label


@pytest.mark.parametrize("claimed", [OWNER, "Some Person", "model"])
def test_an_agent_note_never_names_a_free_string_as_its_author(tmp_path, claimed):
    with make_library(tmp_path) as library:
        entry = zettel(library, 1, [kiln_source(library)])
        entry["attribution"] = annotation(entry["body"], "agent", claimed)
        entry["title_attribution"] = annotation(entry["title"], "agent", claimed)
        (note,) = build_notes(library, [entry])
        meta: dict[str, Any] = parse_text(note.text)[0]
        assert (meta["authorship"], meta["author"]) == ("agent", "agent")
