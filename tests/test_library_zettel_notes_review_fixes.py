"""Review findings on the Zettel note emitter: names, temp files, authorship and the owner binding.

Synthetic conversations and invented text only.
"""

from __future__ import annotations

import json
import os
import unicodedata
from pathlib import Path
from typing import Any

import pytest
from test_library_zettel_notes import (
    annotation,
    command,
    make_library,
    parse_text,
    ref,
    source,
    zettel,
)

from conversation_corpus_engine import library_commands, library_zettel_notes
from conversation_corpus_engine.cli import build_parser
from conversation_corpus_engine.library_commands import handle_library
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


def owned(library: Library, number: int = 1, *, reviewer: str = OWNER, who: str = OWNER) -> dict:
    """An idea whose title, text and quote are all asserted by `who`, reviewed by `reviewer`."""
    entry = zettel(library, number, [source(library, ref(library, "kiln", 1), "owner")])
    entry["review"]["reviewer"] = reviewer
    entry["attribution"] = annotation(entry["body"], "owner", who)
    entry["title_attribution"] = annotation(entry["title"], "owner", who)
    entry["sources"][0]["attribution"] = annotation(
        library.record("chatgpt-kiln")["messages"][1]["text"], "owner", who
    )
    return entry


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
    elif kind == "owner_no_evidence":
        entry["title_attribution"] = annotation(title, "owner", OWNER, evidence=[])
    elif kind == "owner_not_eligible":
        entry["title_attribution"] = annotation(title, "owner", OWNER, profile_eligible=False)
    elif kind == "owner_other_id":
        entry["title_attribution"] = annotation(title, "owner", "someone-else")
    elif kind == "disputed":
        entry["title_attribution"] = annotation(title, "owner", OWNER, assertion_status="disputed")
    return entry


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "agent",
        "third_party",
        "owner_no_evidence",
        "owner_not_eligible",
        "owner_other_id",
        "disputed",
    ],
)
def test_an_owner_note_needs_an_owner_title(tmp_path, kind):
    with make_library(tmp_path) as library:
        entry = with_title(owned(library), kind)
        (note,) = build_notes(library, [entry], owner_id=OWNER)
        assert note.authorship == "generated"
        assert parse_text(note.text)[0]["authorship"] == "generated"


def test_an_owner_note_with_an_owner_title_is_human(tmp_path):
    with make_library(tmp_path) as library:
        (note,) = build_notes(library, [with_title(owned(library), "owner")], owner_id=OWNER)
        meta = parse_text(note.text)[0]
        assert (meta["authorship"], meta["author"]) == ("human", OWNER)


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


# 5. Owner identity


def test_nothing_is_human_without_an_owner_id(tmp_path):
    with make_library(tmp_path) as library:
        (note,) = build_notes(library, [owned(library)])
        assert note.authorship == "generated"
        (blank,) = build_notes(library, [owned(library)], owner_id=None)
        assert blank.authorship == "generated"


def test_a_blank_owner_id_is_refused(tmp_path):
    with make_library(tmp_path) as library, pytest.raises(ValueError, match="owner id"):
        build_notes(library, [owned(library)], owner_id="   ")


@pytest.mark.parametrize(
    ("owner_id", "reviewer", "who"),
    [
        ("someone-else", OWNER, OWNER),
        (OWNER, "bot", OWNER),
        ("bot", "bot", OWNER),
        ("bot", "bot", "bot-two"),
    ],
)
def test_human_needs_the_configured_owner_as_author_and_reviewer(tmp_path, owner_id, reviewer, who):
    with make_library(tmp_path) as library:
        entry = owned(library, reviewer=reviewer, who=who)
        (note,) = build_notes(library, [entry], owner_id=owner_id)
        assert note.authorship == "generated"


def test_a_self_declared_reviewer_who_is_not_the_configured_owner_is_not_human(tmp_path):
    with make_library(tmp_path) as library:
        entry = owned(library, reviewer="codex-bot", who="codex-bot")
        (note,) = build_notes(library, [entry], owner_id=OWNER)
        assert note.authorship == "generated"


def test_the_command_grants_human_only_with_owner_id(tmp_path, monkeypatch, capsys):
    library = make_library(tmp_path)
    store = tmp_path / "store"
    store.mkdir()
    database = tmp_path / "library.sqlite"
    monkeypatch.setattr(library_commands, "open_library", lambda root: (store, Library(database)))
    entry = with_title(owned(library), "owner")
    path = tmp_path / "ideas.json"
    path.write_text(
        json.dumps({"schema": "cce.reviewed-zettels.v1", "zettels": [entry]}), encoding="utf-8"
    )

    def labels(out: Path, *extra: str) -> list[str]:
        code = handle_library(command(out, store, path, *extra))
        report = json.loads(capsys.readouterr().out)
        assert code == 0
        return [n["authorship"] for n in report["notes"]]

    assert labels(tmp_path / "plain") == ["generated"]
    assert labels(tmp_path / "owned", "--owner-id", OWNER) == ["human"]
    assert labels(tmp_path / "other", "--owner-id", "someone-else") == ["generated"]
    library.close()


def test_owner_id_goes_with_the_obsidian_format_only(tmp_path, capsys):
    parsed = build_parser().parse_args(
        ["library", "zettels", "--input", str(tmp_path / "x.json"), "--owner-id", OWNER]
    )
    assert handle_library(parsed) == 2
    assert "--owner-id goes with --format obsidian-md" in capsys.readouterr().err


# 6. Agent author label


@pytest.mark.parametrize("claimed", [OWNER, "Some Person", "model"])
def test_an_agent_note_never_names_a_free_string_as_its_author(tmp_path, claimed):
    with make_library(tmp_path) as library:
        entry = zettel(library, 1, [kiln_source(library)])
        entry["attribution"] = annotation(entry["body"], "agent", claimed)
        entry["title_attribution"] = annotation(entry["title"], "agent", claimed)
        (note,) = build_notes(library, [entry], owner_id=OWNER)
        meta: dict[str, Any] = parse_text(note.text)[0]
        assert (meta["authorship"], meta["author"]) == ("agent", "agent")
