"""Invisible format characters cannot hide pasted text from the echo, the echo hits or the census.

A user turn that pastes an assistant answer back with a zero-width space, a word joiner or a soft
hyphen every few characters reads the same to a person. The echo compares the match form of both
sides, and the repeated-text census compares collapsed text; both must delete such characters
(not read them as spaces, which cuts words in two) so the paste is carved exactly as the unmarked
paste is (synthetic data).
"""

from __future__ import annotations

import random

import pytest

from conversation_corpus_engine.attribution_rules import (
    OWNER,
    AssistantEcho,
    RecordInfo,
    RepeatCensus,
    attribute_message,
    boilerplate_ranges,
    match_form,
)

CLI = RecordInfo(provider="claudecode", kind="session", wing="ai")
WORDS = [
    "parser",
    "lexer",
    "token",
    "schema",
    "module",
    "branch",
    "commit",
    "review",
    "buffer",
    "stream",
    "cache",
    "index",
    "vector",
    "kernel",
    "driver",
    "socket",
    "thread",
    "queue",
    "worker",
    "handler",
    "router",
    "signal",
    "render",
    "layout",
    "window",
    "panel",
]
MARKERS = {
    "word joiner every 7": ("\N{WORD JOINER}", 7),
    "zero-width space every 15": ("\N{ZERO WIDTH SPACE}", 15),
    "soft hyphen every 15": ("\N{SOFT HYPHEN}", 15),
    "zero-width space every 40": ("\N{ZERO WIDTH SPACE}", 40),
}


def sentence(rng: random.Random, low: int, high: int) -> str:
    words = [rng.choice(WORDS) + str(rng.randint(0, 99)) for _ in range(rng.randint(low, high))]
    return " ".join(words).capitalize() + "."


def long_paragraphs(rng: random.Random) -> str:
    return "\n\n".join(" ".join(sentence(rng, 8, 14) for _ in range(3)) for _ in range(5))


def short_paragraphs(rng: random.Random) -> str:
    return "\n\n".join(sentence(rng, 3, 5) for _ in range(8))


def mark(text: str, marker: str, every: int) -> str:
    """The marker after every `every`-th character of each line, never beside a line break."""
    return "\n".join(
        marker.join(line[i : i + every] for i in range(0, len(line), every))
        for line in text.split("\n")
    )


def owner_share(text: str, pasted: tuple[tuple[int, int], ...]) -> float:
    spans = attribute_message(
        text, role="user", channel="typed", typed_chars=len(text), record=CLI, pasted_back=pasted
    )
    return sum(span.end - span.start for span in spans if span.author == OWNER) / len(text)


def test_match_form_deletes_format_characters_instead_of_reading_them_as_spaces() -> None:
    assert match_form("gar\N{WORD JOINER}den") == "garden"
    assert match_form("zero\N{ZERO WIDTH SPACE}width") == "zerowidth"
    assert match_form("soft\N{SOFT HYPHEN}hyphen") == "softhyphen"
    assert match_form("\N{ZERO WIDTH NO-BREAK SPACE}bom") == "bom"
    assert match_form("a\N{LEFT-TO-RIGHT MARK}b") == "ab"
    assert match_form("nul\x00l") == "null"
    # Real separators still separate, and the invisible character beside one changes nothing.
    assert match_form("a\N{ZERO WIDTH SPACE} b") == match_form("a b") == "a b"
    assert match_form("a\tb\u00a0c") == "a b c"
    assert match_form("## **Hello**,   World!") == "hello world"


@pytest.mark.parametrize("shape", [long_paragraphs, short_paragraphs])
@pytest.mark.parametrize("marker", MARKERS)
def test_a_pasted_answer_is_carved_whatever_invisible_characters_are_inside(
    shape, marker: str
) -> None:
    character, every = MARKERS[marker]
    marked = 0
    for seed in range(40):
        rng = random.Random(seed)
        answer = shape(rng)
        echo = AssistantEcho()
        echo.add_assistant(answer)
        assert owner_share(answer, echo.pasted_back(answer)) == 0.0  # the unmarked paste
        text = mark(answer, character, every)
        marked += text != answer  # a line shorter than `every` characters carries no marker
        assert owner_share(text, echo.pasted_back(text)) == 0.0, (shape.__name__, marker, seed)
    assert marked > 20


@pytest.mark.parametrize("marker", MARKERS)
def test_an_answer_pasted_back_across_user_turns_is_carved_through_invisible_characters(
    marker: str,
) -> None:
    character, every = MARKERS[marker]
    rng = random.Random(7)
    answer = short_paragraphs(rng)
    echo = AssistantEcho()
    echo.add_assistant(answer)
    turns = [mark(part, character, every) for part in answer.split("\n\n")]
    found = echo.pasted_back_turns(turns)
    assert all(found), marker
    assert all(owner_share(turn, pasted) == 0.0 for turn, pasted in zip(turns, found, strict=True))


def test_separator_lines_that_only_look_blank_do_not_merge_pasted_paragraphs() -> None:
    rng = random.Random(3)
    answer = short_paragraphs(rng)
    echo = AssistantEcho()
    echo.add_assistant(answer)
    for blank in ("\N{ZERO WIDTH SPACE}", "\N{WORD JOINER}", " \N{SOFT HYPHEN} ", "\x00"):
        text = answer.replace("\n\n", f"\n{blank}\n")
        assert text != answer
        assert owner_share(text, echo.pasted_back(text)) == 0.0, repr(blank)


def test_owner_prose_with_invisible_characters_is_still_the_owners() -> None:
    """The deletion only makes two spellings of one text agree; unrelated prose stays unmatched."""
    rng = random.Random(11)
    answer = long_paragraphs(rng)
    echo = AssistantEcho()
    echo.add_assistant(answer)
    for seed in range(40):
        mine = mark(long_paragraphs(random.Random(1000 + seed)), "\N{ZERO WIDTH SPACE}", 15)
        assert echo.pasted_back(mine) == ()
        assert owner_share(mine, ()) > 0.95


@pytest.mark.parametrize("marker", MARKERS)
def test_repeated_text_is_found_by_the_census_through_invisible_characters(marker: str) -> None:
    character, every = MARKERS[marker]
    found = 0
    for seed in range(40):
        rng = random.Random(10_000 + seed)
        paragraph = " ".join(sentence(rng, 8, 10) for _ in range(4))
        copies = [paragraph, *(mark(paragraph, character, every + copy) for copy in (1, 2))]
        assert len(set(copies)) == 3
        census = RepeatCensus()
        for copy in copies:
            census.observe(copy)
        later = [census.later_occurrences(copy) for copy in copies]
        assert later[0] == () and later[1] and later[2], (marker, seed)
        found += 1
    assert found == 40
    ranges = boilerplate_ranges(copies)
    assert [bool(item) for item in ranges] == [False, True, True]


def test_a_huge_repeated_paragraph_is_found_through_invisible_characters() -> None:
    """The chunked path of the census (a text of more than 130,000 characters) agrees too."""
    rng = random.Random(5)
    paragraph = " ".join(sentence(rng, 8, 10) for _ in range(1800))
    assert len(paragraph) > 140_000
    copies = [
        paragraph,
        mark(paragraph, "\N{ZERO WIDTH SPACE}", 31),
        mark(paragraph, "\N{WORD JOINER}", 17),
    ]
    ranges = boilerplate_ranges(copies)
    assert [bool(item) for item in ranges] == [False, True, True]
