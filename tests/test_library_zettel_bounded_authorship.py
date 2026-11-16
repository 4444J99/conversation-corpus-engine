"""Derived Zettel authorship is bounded: per quoted conversation, no full-library pass by default.

The library-wide repeat census is an explicit opt-in (`repeat_census=True`). The default reads only
each quoted conversation, once, within a message and a character budget. Synthetic conversations
and invented text only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_library_zettel_derived_authorship import (
    ASSISTANT,
    PASTED_BACK,
    TYPED,
    idea,
    make_pantry,
    meta_of,
    span_of,
)
from test_library_zettel_notes import ref

from conversation_corpus_engine import attribution_commands, library_zettel_authorship
from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.cli import build_parser
from conversation_corpus_engine.library_store import Library
from conversation_corpus_engine.library_zettel_authorship import derive_quote_authors
from conversation_corpus_engine.library_zettel_notes import build_notes

TEMPLATE = (
    "Please keep every reply under three short paragraphs and list the open questions at the end "
    "of the answer, using plain words and no tables, so the summary is easy to scan on a phone. "
    "Name each jar by its contents first and by its date second, and never abbreviate the names."
)


@pytest.fixture
def pantry(tmp_path):
    library = make_pantry(tmp_path)
    yield library
    library.close()


def add_template_conversation(library: Library) -> None:
    library.put(
        Conversation(
            provider="chatgpt",
            source_id="template",
            title="Repeated template",
            created="2026-02-02",
            messages=[
                Message(role="user", text=TEMPLATE, id="t0", channel="typed"),
                Message(role="assistant", text="Understood, short replies from now on.", id="t1"),
                Message(role="user", text=TEMPLATE, id="t2", channel="typed"),
                Message(role="assistant", text="Understood again.", id="t3"),
                Message(role="user", text=TEMPLATE, id="t4", channel="typed"),
            ],
        )
    )


def template_span(library: Library, message: int) -> dict[str, Any]:
    text = library.record("chatgpt-template")["messages"][message]["text"]
    return ref(library, "template", message, 0, len(text))


def forbid_library_scans(monkeypatch) -> list[str]:
    calls: list[str] = []

    def refuse(name: str):
        def inner(*args: Any, **kwargs: Any):
            calls.append(name)
            raise AssertionError(f"{name} is a full-library scan")

        return inner

    monkeypatch.setattr(attribution_commands, "iter_messages", refuse("iter_messages"))
    monkeypatch.setattr(attribution_commands, "build_census", refuse("build_census"))
    monkeypatch.setattr(Library, "rows", refuse("Library.rows"))
    return calls


def test_the_default_derivation_never_scans_the_whole_library(pantry, monkeypatch):
    add_template_conversation(pantry)
    calls = forbid_library_scans(monkeypatch)
    (note,) = build_notes(pantry, [idea(pantry, 1, span_of(pantry, 0, TYPED))])
    assert note.authorship == "human"
    assert calls == []


def test_the_default_note_says_the_repeat_census_was_not_used(pantry):
    (note,) = build_notes(pantry, [idea(pantry, 1, span_of(pantry, 0, TYPED))])
    meta = meta_of(note)
    assert (
        "per-conversation derivation; library-wide repeat census not used"
        in (meta["authorship_note"])
    )
    assert meta["authorship_basis"] == "derived"
    assert "rules-derived, not owner-reviewed" in meta["authorship_note"]


def test_the_same_conversation_assistant_echo_still_degrades_a_pasted_back_quote(pantry):
    reference = span_of(pantry, 5, PASTED_BACK[:200])
    (derived,) = derive_quote_authors(pantry, [reference])
    assert derived.author != "owner"
    (note,) = build_notes(pantry, [idea(pantry, 1, reference)])
    assert note.authorship == "generated"
    # the same words typed with no assistant turn before them are the owner's
    (typed,) = derive_quote_authors(pantry, [span_of(pantry, 0, TYPED)])
    assert typed.author == "owner"


def test_a_quote_from_a_conversation_over_the_character_cap_is_unknown(pantry):
    reference = span_of(pantry, 0, TYPED)
    (derived,) = derive_quote_authors(pantry, [reference], max_conversation_chars=100)
    assert derived.author == "unknown" and "budget" in derived.reason
    (inside,) = derive_quote_authors(pantry, [reference])
    assert inside.author == "owner"
    (derived,) = derive_quote_authors(pantry, [reference], max_conversation_messages=2)
    assert derived.author == "unknown" and "budget" in derived.reason
    (derived,) = derive_quote_authors(pantry, [reference], max_quote_message_chars=10)
    assert derived.author == "unknown" and "over" in derived.reason


def test_the_default_character_cap_is_two_mebibytes():
    assert library_zettel_authorship.DEFAULT_MAX_CONVERSATION_CHARS == 2 * 1024 * 1024


@pytest.mark.parametrize("bad", [0, -1, True, 1.5])
def test_budgets_must_be_positive_integers(pantry, bad):
    with pytest.raises(ValueError, match="max_conversation_chars"):
        derive_quote_authors(pantry, [span_of(pantry, 0, TYPED)], max_conversation_chars=bad)


def test_a_conversation_is_read_once_for_several_quotes(pantry, monkeypatch):
    reads: list[str] = []
    real = Library.record

    def counting(self, uid, **kwargs):
        reads.append(uid)
        return real(self, uid, **kwargs)

    references = [
        span_of(pantry, 0, TYPED),
        span_of(pantry, 1, ASSISTANT[:80]),
        span_of(pantry, 5, PASTED_BACK[:200]),
        span_of(pantry, 0, TYPED[:20]),
    ]
    monkeypatch.setattr(Library, "record", counting)
    derived = derive_quote_authors(pantry, references)
    assert [item.author for item in derived][:1] == ["owner"]
    assert reads == ["chatgpt-pantry"]


def test_results_are_deterministic_and_idempotent(pantry):
    references = [span_of(pantry, 0, TYPED), span_of(pantry, 5, PASTED_BACK[:200])]
    assert derive_quote_authors(pantry, references) == derive_quote_authors(pantry, references)
    entries = [idea(pantry, 1, references[0])]
    assert [n.text for n in build_notes(pantry, entries)] == [
        n.text for n in build_notes(pantry, entries)
    ]


def test_a_missed_deadline_credits_nothing_more(pantry, monkeypatch):
    ticks = iter(range(0, 10_000, 10))
    real = attribution_commands.Deadline

    class Late(real):
        def __init__(self, seconds):
            super().__init__(seconds, clock=lambda: next(ticks))

    monkeypatch.setattr(attribution_commands, "Deadline", Late)
    (derived,) = derive_quote_authors(pantry, [span_of(pantry, 0, TYPED)], deadline_seconds=5)
    assert derived.author == "unknown"


def test_the_repeat_census_is_an_explicit_opt_in(pantry, monkeypatch):
    add_template_conversation(pantry)
    third = template_span(pantry, 4)
    (default,) = derive_quote_authors(pantry, [third])
    assert default.author == "owner"
    scans: list[str] = []
    real = attribution_commands.build_census

    def spy(*args: Any, **kwargs: Any):
        scans.append("census")
        return real(*args, **kwargs)

    monkeypatch.setattr(attribution_commands, "build_census", spy)
    (opted,) = derive_quote_authors(pantry, [third], repeat_census=True)
    assert scans == ["census"]
    assert opted.author == "third_party" and "boilerplate" in opted.rules
    (note,) = build_notes(pantry, [idea(pantry, 1, span_of(pantry, 0, TYPED))], repeat_census=True)
    assert note.authorship == "human"
    assert "library-wide repeat census used" in meta_of(note)["authorship_note"]
    assert "census not used" not in meta_of(note)["authorship_note"]


def test_the_census_flag_is_api_only(tmp_path):
    parsed = build_parser().parse_args(
        ["library", "zettels", "--input", "x.json", "--format", "obsidian-md", "--out", "o"]
    )
    assert not hasattr(parsed, "repeat_census")
    assert isinstance(tmp_path, Path)
