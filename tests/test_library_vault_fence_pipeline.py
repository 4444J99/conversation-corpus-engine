"""The full profile's fence handling, end to end, against the legacy column-0 splitter.

Notes are CommonMark. A part may end only outside a fence, which shows as its trailing
"Next part" link rendering as a link. The vault passes ``container_fences=True`` to the splitter
so that a fence inside a list item closes at its own indentation; these tests hold that to the
one promise that matters: it never breaks a link the column-0 splitter rendered.
"""

from __future__ import annotations

import random
from collections.abc import Callable

import pytest
from markdown_it import MarkdownIt

from conversation_corpus_engine import library_vault
from conversation_corpus_engine.archive_split import pack, split_text
from conversation_corpus_engine.library_blocks import render_blocks, split_blocks

NO_BREAK_SPACE = chr(0xA0)
PARSER = MarkdownIt("commonmark")
NAV = "Part 1 of 9 - [Previous part](prev.md) - [Next part](next.md)"
MESSAGE = "\n## Message 1\n\nRole: user; channel: Not recorded\n\n"


def _unit(raw: str) -> str:
    return MESSAGE + render_blocks(split_blocks(raw), {}) + "\n"


def _renders_next_link(part: str) -> bool:
    tokens = PARSER.parse(f"{part}\n{NAV}\n")
    return any(
        child.type == "link_open" and child.attrGet("href") == "next.md"
        for token in tokens
        if token.type == "inline"
        for child in token.children or []
    )


@pytest.fixture
def choose_splitter(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], None]:
    """Switch the vault between the column-0 splitter (True) and the container-aware one."""
    real = library_vault.split_text

    def choose(column_zero: bool) -> None:
        monkeypatch.setattr(
            library_vault,
            "split_text",
            (lambda text, limit, container_fences=True: split_text(text, limit))
            if column_zero
            else real,
        )

    return choose


@pytest.mark.parametrize("column_zero", [False, True], ids=["container-aware", "column-zero"])
def test_a_tracker_opener_that_commonmark_rejects_cannot_swallow_a_part_link(
    choose_splitter: Callable[[bool], None], column_zero: bool
) -> None:
    # A no-break space is whitespace to the flat fence tracker but not indentation to CommonMark,
    # so the tracker takes the second line for the first fence's closer and the four-column line
    # for a new opener, while CommonMark keeps the first fence open. Only the column-0 closer
    # closes it, so the part link renders only if the splitter writes that one.
    choose_splitter(column_zero)
    raw = f"~~~\n{NO_BREAK_SPACE}~~~\n    ~~~\n" + "code line\n" * 300 + "    ~~~\n~~~\nafter\n"
    parts = [
        "".join(group) for group in pack(library_vault._fit_units([_unit(raw)], 1023), 1024, 1024)
    ]
    assert len(parts) > 2
    assert all(_renders_next_link(part) for part in parts[:-1])


WORDS = ["alpha", "beta", "gamma", "delta", "héllo", "日本語", "x"]
INDENTS = [
    "",
    "",
    "",
    " ",
    "  ",
    "   ",
    "    ",
    "      ",
    "\t",
    " \t",
    "\x0c",
    chr(0x3000),
    NO_BREAK_SPACE,
]
LIST_MARKERS = ["- ", "* ", "1. ", "10) ", "-   "]


def _prose(rng: random.Random) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(rng.randint(1, 12)))


def _marker(rng: random.Random) -> str:
    return rng.choice("`~") * rng.randint(3, 5)


def _random_document(rng: random.Random) -> str:
    """Synthetic Markdown dense in fences, lists, quotes and the indents that confuse them."""
    lines: list[str] = []
    open_marker = ""
    target = rng.randint(1500, 7000)
    while sum(len(line) + 1 for line in lines) < target:
        roll = rng.random()
        if roll < 0.2:
            marker = _marker(rng)
            lines.append(rng.choice(INDENTS) + marker + rng.choice(["", "", "python", " js"]))
            open_marker = open_marker or marker
        elif roll < 0.3 and open_marker:
            lines.append(rng.choice(INDENTS) + open_marker + rng.choice(["", "", " junk"]))
            open_marker = ""
        elif roll < 0.42:
            lines.append(rng.choice(INDENTS[:6]) + rng.choice(LIST_MARKERS) + _prose(rng))
        elif roll < 0.46:
            lines.append(rng.choice(["- ", "1. "]) + _marker(rng))
        elif roll < 0.52:
            lines.append(rng.choice(["> ", ">", "> > "]) + rng.choice([_marker(rng), _prose(rng)]))
        elif roll < 0.57:
            lines.append("")
        else:
            lines.append(rng.choice(INDENTS) + _prose(rng))
    return rng.choice(["\n", "\n", "\n", "\r\n"]).join(lines)


def _fitted(
    unit: str, budget: int, choose_splitter: Callable[[bool], None], column_zero: bool
) -> list[str]:
    choose_splitter(column_zero)
    return library_vault._fit_units([unit], budget - 1)


def test_container_aware_splitting_never_breaks_a_link_the_column_zero_splitter_rendered(
    choose_splitter: Callable[[bool], None],
) -> None:
    rng = random.Random(115)
    split = fenced = fixed = 0
    for _ in range(300):
        unit = _unit(_random_document(rng))
        budget = rng.choice([1024, 2048, 4096])
        old = _fitted(unit, budget, choose_splitter, column_zero=True)
        new = _fitted(unit, budget, choose_splitter, column_zero=False)
        assert len(new) == len(old), "the cut points moved"
        split += len(old) > 1
        for before, after in zip(old, new, strict=True):
            fenced += before != after
            broken_before = not _renders_next_link(before)
            broken_after = not _renders_next_link(after)
            assert broken_before or not broken_after, "a part link that rendered is now swallowed"
            fixed += broken_before and not broken_after
    # The generator must actually exercise the change, or the property above proves nothing.
    assert split > 150 and fenced > 20 and fixed > 0
