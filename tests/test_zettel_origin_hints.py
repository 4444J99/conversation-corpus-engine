"""Synthetic git-history hints must reach both quote-derivation paths."""

import pytest
from test_library_zettel_derived_authorship import idea, meta_of
from test_library_zettel_notes import sha
from test_origin_hints import hints_of, row

from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.cli import build_parser
from conversation_corpus_engine.library_commands import handle_library
from conversation_corpus_engine.library_store import Library
from conversation_corpus_engine.library_zettel_authorship import derive_quote_authors
from conversation_corpus_engine.library_zettel_notes import build_notes


@pytest.mark.parametrize("repeat_census", [False, True])
@pytest.mark.parametrize(
    "origin,expected",
    [("agent", "agent"), ("mixed", "unknown"), ("human", "owner"), ("ambiguous", "unknown")],
)
def test_document_hints_reach_quote_and_note_authorship(tmp_path, repeat_census, origin, expected):
    text = "Keep a dated inventory of the jars stored on the pantry shelf."
    with Library.create(tmp_path / "library.sqlite") as library:
        library.put(
            Conversation(
                provider="documents",
                source_id="pantry",
                kind="document",
                title="Pantry",
                provenance={"source_file": "backup/project/docs/pantry.md"},
                extra={"wing": "authored"},
                messages=[Message(role="user", channel="project", id="m0", text=text)],
            )
        )
        history = library.history("documents-pantry")[-1]
        reference = dict(
            uid="documents-pantry",
            version=history["version"],
            revision_digest=history["digest"],
            sequence="main",
            message=0,
            message_id="m0",
            text_digest=sha(text),
            start=0,
            end=len(text),
        )
        rows = [row("project", "docs/pantry.md", "human" if origin == "ambiguous" else origin)]
        if origin == "ambiguous":
            rows.append(row("project", "docs/pantry.md", "agent"))
        hints = hints_of(*rows)
        (derived,) = derive_quote_authors(
            library, [reference], origin_hints=hints, repeat_census=repeat_census
        )
        assert derived.author == expected
        entry = idea(library, 1, reference)
        (note,) = build_notes(library, [entry], origin_hints=hints, repeat_census=repeat_census)
        assert (meta_of(note)["authorship"] == "human") == (expected == "owner")


def test_malformed_hints_are_refused_even_for_an_empty_export(tmp_path):
    hints = hints_of()
    hints.note("invalid row")
    with (
        Library.create(tmp_path / "library.sqlite") as library,
        pytest.raises(ValueError, match="malformed"),
    ):
        build_notes(library, [], origin_hints=hints)


def test_cli_refuses_malformed_hints_before_reading_library_or_writing(tmp_path, capsys):
    hints = tmp_path / "hints.jsonl"
    hints.write_text("{}\n")
    out = tmp_path / "notes"
    args = build_parser().parse_args(
        [
            "library",
            "zettels",
            "--input",
            str(tmp_path / "absent.json"),
            "--store-root",
            str(tmp_path / "absent-store"),
            "--format",
            "obsidian-md",
            "--out",
            str(out),
            "--origin-hints",
            str(hints),
        ]
    )
    assert handle_library(args) == 2
    assert "malformed" in capsys.readouterr().err
    assert not out.exists()
