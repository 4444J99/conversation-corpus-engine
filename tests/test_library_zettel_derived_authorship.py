"""Derived authorship for Zettel notes: the engine decides who wrote each quoted word.

The owner never types an owner id or a reviewer string. A quote is `owner` only when every
character of its range is classed owner by the deterministic attribution rules; the idea text is
agent-written by declared origin, or made only of the quotes. Synthetic conversations and invented
text only.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from test_library_zettel_notes import annotation, command, parse, ref, sha, zettel

from conversation_corpus_engine import library_commands
from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.attribution_rules import RULES_VERSION
from conversation_corpus_engine.library_commands import handle_library
from conversation_corpus_engine.library_store import Library
from conversation_corpus_engine.library_zettel_authorship import derive_quote_authors
from conversation_corpus_engine.library_zettel_notes import build_notes

TYPED = "Label every jar with the date before it goes on the shelf."
ASSISTANT = (
    "Dating the lids makes the oldest jar easy to find, so rotate the stock whenever you add "
    "a new batch. Keep the newest jars at the back of the shelf and the oldest at the front, "
    "then check the lids again at the start of every month so that nothing is ever left to "
    "spoil unnoticed at the very back of the cupboard."
)
OWN_LEAD = "Here is the note I copied:"
BLOCK = "> The pantry inventory is reviewed monthly by the committee and posted on the door."
PASTED = f"{OWN_LEAD}\n{BLOCK}\nThanks."
WRAPPED_OWN = "Keep the ledger tidy."
WRAPPER = "<system-reminder>Invented reminder text for the harness.</system-reminder>"
INJECTED = f"{WRAPPED_OWN} {WRAPPER} Then close up."
PARTIAL_TYPED = "Short typed words."
PARTIAL = f"{PARTIAL_TYPED} Tool appended trailer sentence."
PASTED_BACK = ASSISTANT


def make_pantry(tmp_path: Path) -> Library:
    library = Library.create(tmp_path / "library.sqlite")
    messages = [
        Message(role="user", text=TYPED, id="p0", channel="typed"),
        Message(role="assistant", text=ASSISTANT, id="p1"),
        Message(role="user", text=PASTED, id="p2", channel="typed"),
        Message(role="user", text=INJECTED, id="p3", channel="typed"),
        Message(
            role="user", text=PARTIAL, id="p4", channel="typed", typed_chars=len(PARTIAL_TYPED)
        ),
        Message(role="user", text=PASTED_BACK, id="p5", channel="typed"),
    ]
    library.put(
        Conversation(
            provider="chatgpt",
            source_id="pantry",
            title="Talk about the pantry",
            created="2026-02-01",
            messages=messages,
        )
    )
    return library


def span_of(library: Library, message: int, piece: str, *, whole: bool = False) -> dict[str, Any]:
    text = library.record("chatgpt-pantry")["messages"][message]["text"]
    start = text.index(piece)
    reference = ref(library, "pantry", message, start, start + len(piece))
    if whole:
        assert piece == text
    return reference


def idea(library: Library, number: int, reference: dict[str, Any], **changes: Any) -> dict:
    """A quoted-origin idea: its text is the quote, its title a piece of it."""
    text = library.record(reference["uid"])["messages"][reference["message"]]["text"]
    quote = text[reference["start"] : reference["end"]]
    plain = re.search(r"[A-Za-z][A-Za-z ]{8,60}[A-Za-z]", quote)
    assert plain
    title = changes.pop("title", plain.group(0))
    entry = zettel(library, number, [{"source": reference}], title=title)
    entry["body"] = quote
    entry["attribution"] = annotation(quote, "agent", "model")
    entry["title_attribution"] = annotation(title, "agent", "model")
    entry["text_origin"] = "quoted"
    entry.update(changes)
    return entry


def meta_of(note) -> dict[str, Any]:
    return parse_note(note.text)


def parse_note(text: str) -> dict[str, Any]:
    lines = text.split("\n")
    close = lines.index("---", 1)
    meta: dict[str, Any] = {}
    key = ""
    for line in lines[1:close]:
        if line.startswith("  - "):
            meta[key].append(json.loads(line[4:]))
        else:
            key, _, rest = line.partition(":")
            meta[key] = [] if rest.strip() in ("", "[]") else json.loads(rest)
    return meta


@pytest.fixture
def pantry(tmp_path):
    library = make_pantry(tmp_path)
    yield library
    library.close()


# A typed owner quote is human.


def test_a_typed_owner_quote_with_quoted_text_is_human_without_any_owner_id(pantry):
    entry = idea(pantry, 1, span_of(pantry, 0, TYPED))
    (note,) = build_notes(pantry, [entry])
    meta = meta_of(note)
    assert (meta["authorship"], meta["author"], meta["status"]) == ("human", "owner", "draft")
    assert note.authorship == "human"


def test_the_basis_is_recorded_honestly(pantry):
    (note,) = build_notes(pantry, [idea(pantry, 1, span_of(pantry, 0, TYPED))])
    meta = meta_of(note)
    assert meta["authorship_basis"] == "derived"
    assert meta["authorship_rules"] == RULES_VERSION
    assert "not reviewed by the owner" in meta["authorship_note"]
    assert "profile_eligible" in meta["authorship_note"]
    assert "rules-derived" in note.text


def test_the_reviewer_is_provenance_only_and_never_grants_or_blocks_human(pantry):
    for reviewer in ("rev", "someone-else", "rob"):
        entry = idea(pantry, 1, span_of(pantry, 0, TYPED))
        entry["review"]["reviewer"] = reviewer
        (note,) = build_notes(pantry, [entry])
        assert meta_of(note)["authorship"] == "human"
        assert f"(provenance only, grants no authorship): {reviewer}" in note.text


# Spans that are not typed owner text degrade the label.


@pytest.mark.parametrize(
    ("message", "piece", "label"),
    [
        (1, ASSISTANT[:80], "agent"),
        (2, BLOCK, "generated"),
        (3, WRAPPER, "agent"),
        (4, "Tool appended trailer sentence.", "agent"),
        (5, ASSISTANT[:200], "generated"),
    ],
)
def test_a_non_owner_quote_never_makes_the_note_human(pantry, message, piece, label):
    (note,) = build_notes(pantry, [idea(pantry, 1, span_of(pantry, message, piece))])
    assert note.authorship != "human"
    assert meta_of(note)["authorship"] == label


def test_a_mixed_range_takes_the_weakest_class_and_records_which(pantry):
    mixed = f"{OWN_LEAD}\n{BLOCK}"
    reference = span_of(pantry, 2, mixed)
    (derived,) = derive_quote_authors(pantry, [reference])
    assert derived.author == "third_party"
    assert {"owner", "third_party"} <= set(derived.classes)
    assert "typed_prefix" in derived.rules and "quote" in derived.rules
    (note,) = build_notes(pantry, [idea(pantry, 1, reference)])
    assert note.authorship == "generated"
    assert "third_party" in note.text and "owner" in note.text


def test_the_owner_part_of_a_pasted_message_is_still_owner(pantry):
    (derived,) = derive_quote_authors(pantry, [span_of(pantry, 2, OWN_LEAD)])
    assert derived.author == "owner"
    (inside,) = derive_quote_authors(pantry, [span_of(pantry, 3, WRAPPED_OWN)])
    assert inside.author == "owner"


def test_a_range_with_an_owner_part_and_an_agent_part_is_agent(pantry):
    text = library_text(pantry, 4)
    reference = span_of(pantry, 4, text)
    (derived,) = derive_quote_authors(pantry, [reference])
    assert derived.author == "agent"
    assert "owner" in derived.classes


def library_text(library: Library, message: int) -> str:
    return library.record("chatgpt-pantry")["messages"][message]["text"]


# Declared origin cannot be forged.


def test_a_forged_owner_envelope_does_not_become_human(pantry):
    reference = span_of(pantry, 1, ASSISTANT[:80])
    entry = zettel(pantry, 1, [{"source": reference}])
    entry["review"]["reviewer"] = "rob"
    entry["attribution"] = annotation(entry["body"], "owner", "rob")
    entry["title_attribution"] = annotation(entry["title"], "owner", "rob")
    entry["sources"][0]["attribution"] = annotation(ASSISTANT, "owner", "rob")
    (note,) = build_notes(pantry, [entry])
    assert note.authorship == "generated"
    assert meta_of(note)["authorship_basis"] == "unverified"


def test_a_declared_owner_text_without_derivation_degrades_to_generated(pantry):
    entry = zettel(pantry, 1, [{"source": span_of(pantry, 0, TYPED)}], text_origin="owner")
    (note,) = build_notes(pantry, [entry])
    assert note.authorship == "generated"
    assert meta_of(note)["authorship_basis"] == "unverified"


def test_a_quoted_origin_that_is_not_made_of_the_quotes_is_not_human(pantry):
    entry = zettel(pantry, 1, [{"source": span_of(pantry, 0, TYPED)}], text_origin="quoted")
    (note,) = build_notes(pantry, [entry])
    assert note.authorship == "generated"


def test_a_title_that_is_not_part_of_the_quote_is_not_human(pantry):
    entry = idea(pantry, 1, span_of(pantry, 0, TYPED), title="A different invented title")
    (note,) = build_notes(pantry, [entry])
    assert note.authorship == "generated"


def test_agent_written_text_is_agent_whatever_the_quotes_are(pantry):
    for message, piece in ((0, TYPED), (2, BLOCK), (1, ASSISTANT[:80])):
        entry = zettel(pantry, 1, [{"source": span_of(pantry, message, piece)}])
        entry["text_origin"] = "agent"
        (note,) = build_notes(pantry, [entry])
        meta = meta_of(note)
        assert (meta["authorship"], meta["author"]) == ("agent", "agent")
        assert meta["authorship_basis"] == "declared"


def test_the_legacy_agent_annotations_still_enter_as_agent_drafts(pantry):
    entry = zettel(pantry, 1, [{"source": span_of(pantry, 0, TYPED)}])
    (note,) = build_notes(pantry, [entry])
    assert meta_of(note)["authorship"] == "agent"


def test_an_unknown_text_origin_is_refused(pantry):
    entry = zettel(pantry, 1, [{"source": span_of(pantry, 0, TYPED)}], text_origin="robot")
    with pytest.raises(ValueError, match="text_origin"):
        build_notes(pantry, [entry])


def test_a_stale_revision_is_never_credited(pantry):
    reference = span_of(pantry, 0, TYPED)
    pantry.put(
        Conversation(
            provider="chatgpt",
            source_id="pantry",
            title="Talk about the pantry",
            created="2026-02-01",
            messages=[Message(role="user", text=TYPED, id="p0", channel="typed")]
            + [Message(role="assistant", text="A later reply to the same thread.", id="p9")],
        )
    )
    assert sha(TYPED) == reference["text_digest"]
    (derived,) = derive_quote_authors(pantry, [reference])
    assert derived.author == "unknown"


# Determinism and the command line.


def test_reruns_are_byte_identical(pantry):
    entries = [idea(pantry, 1, span_of(pantry, 0, TYPED))]
    first = build_notes(pantry, entries)
    second = build_notes(pantry, entries)
    assert [n.text for n in first] == [n.text for n in second]


def command_world(tmp_path, monkeypatch, entries):
    library = make_pantry(tmp_path)
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setattr(
        library_commands, "open_library", lambda root: (store, Library(tmp_path / "library.sqlite"))
    )
    path = tmp_path / "ideas.json"
    payload = {"schema": "cce.reviewed-zettels.v1", "zettels": entries(library)}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return library, store, path


def test_the_command_needs_no_owner_id(tmp_path, monkeypatch, capsys):
    library, store, path = command_world(
        tmp_path, monkeypatch, lambda lib: [idea(lib, 1, span_of(lib, 0, TYPED))]
    )
    out = tmp_path / "wiki"
    assert handle_library(command(out, store, path)) == 0
    report = json.loads(capsys.readouterr().out)
    assert [n["authorship"] for n in report["notes"]] == ["human"]
    meta = parse(next((out / "notes" / "zettel").glob("*.md")))[0]
    assert meta["authorship_basis"] == "derived"
    library.close()


def test_the_old_owner_id_flag_is_refused_with_a_clear_reason(tmp_path, monkeypatch, capsys):
    library, store, path = command_world(
        tmp_path, monkeypatch, lambda lib: [idea(lib, 1, span_of(lib, 0, TYPED))]
    )
    out = tmp_path / "wiki"
    assert handle_library(command(out, store, path, "--owner-id", "rob")) == 2
    error = capsys.readouterr().err
    assert "--owner-id is no longer used" in error
    assert not out.exists()
    library.close()
