"""A fence cut by a split closes and reopens in the container it was opened in.

`FenceTracker` is flat: it keeps the opener's marker, not where the opener sat. A bare column-0
closing line does not close a fence that lives inside a list item. It leaves the item, which ends
the fence, and then opens a new top-level fence that swallows everything after the cut, the
"Next part" link of a vault note included. With ``container_fences`` (CommonMark output) the part
closes with the opener's own indentation and the next part reopens in an equivalent container.

Every case below is split, rejoined byte for byte, and each part is parsed by the CommonMark
parser the packager tests use: no part may end inside an open fence, which shows as the trailing
navigation link failing to render as a link.
"""

from __future__ import annotations

from collections.abc import Iterable

import pytest
from markdown_it import MarkdownIt
from markdown_it.token import Token

from conversation_corpus_engine.archive_split import (
    MAX_FENCE_INDENT,
    Chunk,
    rejoin,
    split_text,
    utf8_size,
)

LIMIT = 1024  # small, so one fenced block is cut into several parts
NAV = "Part 1 of 3 - [Next part](next.md)"
PARSER = MarkdownIt("commonmark", {"html": True})


def _document(
    head: Iterable[str],
    indent: str,
    *,
    marker: str = "```",
    info: str = "sh",
    closer: str | None = None,
    tail: Iterable[str] = ("after the block",),
    eol: str = "\n",
    count: int = 60,
) -> str:
    """Prose, then one fenced block of ``count`` code lines that the split must cut."""
    code = [f"{indent}L line {number:04d} lorem ipsum dolor sit amet" for number in range(count)]
    lines = [*head, f"{indent}{marker}{info}", *code, f"{indent}{closer or marker}", *tail]
    return eol.join(lines)


CASES = {
    "plain": _document([], ""),
    "list-item-indent-2": _document(["- item"], "  "),
    "list-item-indent-3": _document(["1. item"], "   "),
    "list-item-indent-4": _document(["- item"], "    "),
    "tight-list": _document(["- first", "- second"], "  ", tail=["- third", "after"]),
    "nested-list-width-4": _document(["- outer", "  - inner"], "    "),
    "nested-list-width-6": _document(["- a", "  - b", "    - c"], "      "),
    "tilde-fence": _document(["- item"], "  ", marker="~~~"),
    "top-level-tilde": _document([], "", marker="~~~"),
    "info-string": _document(["- item"], "  ", info="python linenos=true"),
    "closer-longer-than-opener": _document(["- item"], "  ", closer="`````"),
    "crlf": _document(["- item"], "  ", eol="\r\n"),
    "tab-indented-fence": _document(["- item"], "\t"),
}


def _links(tokens: list[Token]) -> list[str]:
    return [
        child.attrGet("href")
        for token in tokens
        for child in token.children or []
        if child.type == "link_open"
    ]


def _fence_text(tokens: list[Token]) -> str:
    return "\n".join(token.content for token in tokens if token.type == "fence")


def _assert_every_part_renders(text: str, chunks: list[Chunk]) -> None:
    assert len(chunks) > 2 and any(chunk.prefix for chunk in chunks), "no fence was cut"
    assert rejoin(chunks) == text
    for chunk in chunks:
        assert utf8_size(chunk.text()) <= LIMIT
        tokens = PARSER.parse(f"{chunk.text()}\n{NAV}\n")
        assert "next.md" in _links(tokens), "the navigation link was swallowed by an open fence"
        if chunk.prefix and chunk.body.lstrip().startswith("L line"):
            assert "L line" in _fence_text(tokens), "the continuation is not code"


@pytest.mark.parametrize("name", CASES)
def test_a_cut_fence_closes_and_reopens_in_its_own_container(name: str) -> None:
    text = CASES[name]
    _assert_every_part_renders(text, split_text(text, LIMIT, container_fences=True))


@pytest.mark.parametrize("name", ["plain", "top-level-tilde"])
def test_a_top_level_fence_is_unchanged_by_the_flag(name: str) -> None:
    text = CASES[name]
    assert split_text(text, LIMIT, container_fences=True) == split_text(text, LIMIT)


def test_without_the_flag_the_synthetic_lines_stay_at_column_zero() -> None:
    chunks = split_text(CASES["list-item-indent-2"], LIMIT)
    cut = [chunk for chunk in chunks if chunk.prefix]
    assert cut and all(chunk.prefix == "```" for chunk in cut)
    assert all(chunk.suffix in {"", "```"} for chunk in chunks)
    assert split_text(CASES["list-item-indent-2"], LIMIT, container_fences=False) == chunks


def test_a_closer_repeats_the_opener_indentation_and_a_reopener_nests_wide_fences() -> None:
    narrow = split_text(CASES["list-item-indent-2"], LIMIT, container_fences=True)
    assert {chunk.suffix for chunk in narrow} == {"", "  ```"}
    assert {chunk.prefix for chunk in narrow} == {"", "  ```"}
    wide = split_text(CASES["nested-list-width-6"], LIMIT, container_fences=True)
    assert {chunk.suffix for chunk in wide} == {"", "      ```"}
    assert {chunk.prefix for chunk in wide} == {"", "- - - ```"}


def test_an_indent_wider_than_the_limit_falls_back_to_column_zero() -> None:
    at_limit = " " * MAX_FENCE_INDENT
    chunks = split_text(_document([], at_limit), LIMIT, container_fences=True)
    assert {chunk.suffix for chunk in chunks} == {"", at_limit + "```"}
    assert {chunk.prefix for chunk in chunks} == {"", "- " * (MAX_FENCE_INDENT // 2) + "```"}
    assert all(utf8_size(chunk.text()) <= LIMIT for chunk in chunks)

    text = _document([], " " * (MAX_FENCE_INDENT + 1))
    chunks = split_text(text, LIMIT, container_fences=True)
    assert {chunk.suffix for chunk in chunks} == {"", "```"}
    assert {chunk.prefix for chunk in chunks} == {"", "```"}
    assert rejoin(chunks) == text
    assert all(utf8_size(chunk.text()) <= LIMIT for chunk in chunks)


def test_each_fence_uses_its_own_indentation() -> None:
    first = _document(["- item"], "  ", count=3, tail=["", "between the blocks", ""])
    second = _document([], "", count=60, info="py")
    text = first + "\n" + second
    chunks = split_text(text, LIMIT, container_fences=True)
    cut = [chunk for chunk in chunks if chunk.prefix]
    assert cut and {chunk.prefix for chunk in cut} == {"```"}
    _assert_every_part_renders(text, chunks)


def test_a_fence_that_is_not_cut_is_left_alone() -> None:
    text = _document(["- item"], "  ", count=4) + "\n" + "filler line\n" * 120
    chunks = split_text(text, LIMIT, container_fences=True)
    assert len(chunks) > 1 and not any(chunk.prefix or chunk.suffix for chunk in chunks)
    assert rejoin(chunks) == text


@pytest.mark.parametrize(
    ("opener", "lead", "closer"),
    [("> ```sh", "> ", "> ```"), ("- ```sh", "  ", "  ```")],
    ids=["blockquote", "list-marker-line"],
)
def test_a_fence_in_a_blockquote_or_on_a_list_marker_line_is_not_tracked(
    opener: str, lead: str, closer: str
) -> None:
    # Stated limit (docs/knowledge-library.md): the splitter tracks a fence only when its opener
    # line starts with optional indentation and the marker. Nothing is added at a cut, and
    # nothing is lost either.
    code = [f"{lead}L line {number:04d} lorem ipsum dolor sit amet" for number in range(60)]
    text = "\n".join(["intro", opener, *code, closer, "after the block"])
    chunks = split_text(text, LIMIT, container_fences=True)
    assert len(chunks) > 2 and rejoin(chunks) == text
    assert not any(chunk.prefix or chunk.suffix for chunk in chunks)
