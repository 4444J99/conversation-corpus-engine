"""A fence cut by a split closes and reopens at the indentation it was opened with.

`FenceTracker` is flat: it keeps the opener's marker, not where the opener sat. A bare column-0
closing line does not close a fence that lives inside a list item. It leaves the item, which ends
the fence, and then opens a new top-level fence that swallows everything after the cut, the
"Next part" link of a vault note included. With ``container_fences`` (CommonMark output) a fence
whose opener is indented one to three plain spaces, CommonMark's own limit for a fence relative to
its container, closes and reopens at that indentation.

Everything else keeps the column-0 lines: no indent, four columns or more (indented code in
CommonMark, which `FenceTracker` still reads as an opener), tabs and any other whitespace. The
flat tracker and CommonMark can disagree about which line opened a fence there, and an indented
closer then leaves a real fence open where the column-0 one happened to close it.

Every container-aware case is split, rejoined byte for byte, and each part is parsed by the
CommonMark parser the packager tests use: no part may end inside an open fence, which shows as the
trailing navigation link failing to render as a link.
"""

from __future__ import annotations

from collections.abc import Iterable

import pytest
from markdown_it import MarkdownIt
from markdown_it.token import Token

from conversation_corpus_engine.archive_split import Chunk, rejoin, split_text, utf8_size

NO_BREAK_SPACE = chr(0xA0)
IDEOGRAPHIC_SPACE = chr(0x3000)
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


# Fences whose opener is indented one to three plain spaces: closed and reopened in place.
CASES = {
    "plain": _document([], ""),
    "top-level-tilde": _document([], "", marker="~~~"),
    "top-level-indent-1": _document([], " "),
    "top-level-indent-2": _document([], "  "),
    "list-item-indent-2": _document(["- item"], "  "),
    "list-item-indent-3": _document(["1. item"], "   "),
    "tight-list": _document(["- first", "- second"], "  ", tail=["- third", "after"]),
    "tilde-fence": _document(["- item"], "  ", marker="~~~"),
    "info-string": _document(["- item"], "  ", info="python linenos=true"),
    "closer-longer-than-opener": _document(["- item"], "  ", closer="`````"),
    "crlf": _document(["- item"], "  ", eol="\r\n"),
}
# Fences the splitter keeps closing and reopening at column 0, as before the flag existed.
COLUMN_ZERO = {
    "list-item-indent-4": _document(["- item"], "    "),
    "nested-list-width-4": _document(["- outer", "  - inner"], "    "),
    "nested-list-width-6": _document(["- a", "  - b", "    - c"], "      "),
    "wide-indent-30": _document([], " " * 30),
    "tab": _document(["- item"], "\t"),
    "space-then-tab": _document(["- item"], " \t"),
    "form-feed": _document([], "\x0c"),
    "ideographic-space": _document([], IDEOGRAPHIC_SPACE),
    "no-break-space": _document([], NO_BREAK_SPACE),
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


def _renders_next_link(chunk: Chunk) -> bool:
    return "next.md" in _links(PARSER.parse(f"{chunk.text()}\n{NAV}\n"))


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
def test_a_cut_fence_closes_and_reopens_at_its_own_indentation(name: str) -> None:
    text = CASES[name]
    _assert_every_part_renders(text, split_text(text, LIMIT, container_fences=True))


@pytest.mark.parametrize("name", ["plain", "top-level-tilde"])
def test_a_fence_opened_at_column_zero_is_unchanged_by_the_flag(name: str) -> None:
    text = CASES[name]
    assert split_text(text, LIMIT, container_fences=True) == split_text(text, LIMIT)


@pytest.mark.parametrize("name", COLUMN_ZERO)
def test_other_openers_keep_the_column_zero_lines_with_the_flag(name: str) -> None:
    text = COLUMN_ZERO[name]
    chunks = split_text(text, LIMIT, container_fences=True)
    assert chunks == split_text(text, LIMIT)
    assert any(chunk.prefix for chunk in chunks), "no fence was cut"
    assert {chunk.prefix for chunk in chunks} == {"", "```"}
    assert {chunk.suffix for chunk in chunks} == {"", "```"}
    assert rejoin(chunks) == text
    assert all(utf8_size(chunk.text()) <= LIMIT for chunk in chunks)


def test_without_the_flag_the_synthetic_lines_stay_at_column_zero() -> None:
    chunks = split_text(CASES["list-item-indent-2"], LIMIT)
    cut = [chunk for chunk in chunks if chunk.prefix]
    assert cut and all(chunk.prefix == "```" for chunk in cut)
    assert all(chunk.suffix in {"", "```"} for chunk in chunks)
    assert split_text(CASES["list-item-indent-2"], LIMIT, container_fences=False) == chunks


@pytest.mark.parametrize(
    ("name", "indent"),
    [("top-level-indent-1", " "), ("list-item-indent-2", "  "), ("list-item-indent-3", "   ")],
)
def test_a_closer_and_a_reopener_repeat_the_opener_indentation(name: str, indent: str) -> None:
    chunks = split_text(CASES[name], LIMIT, container_fences=True)
    marker = "```"
    assert {chunk.suffix for chunk in chunks} == {"", indent + marker}
    assert {chunk.prefix for chunk in chunks} == {"", indent + marker}


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
    "text",
    [
        f"~~~\n{NO_BREAK_SPACE}~~~\n    ~~~\n" + "code line\n" * 60 + "    ~~~\n~~~\nafter\n",
        f"~~~\n{NO_BREAK_SPACE}~~~\n" + "x\n" * 3 + "    ~~~\n" + "code line\n" * 60,
    ],
    ids=["opener-after-a-misread-closer", "opener-at-the-end"],
)
@pytest.mark.parametrize("container_fences", [False, True])
def test_an_opener_the_tracker_reads_and_commonmark_does_not_cannot_swallow_the_link(
    text: str, container_fences: bool
) -> None:
    # A no-break space is whitespace to the flat tracker (``\s*``) but not indentation to
    # CommonMark. So the tracker takes the second line for the first fence's closer and the
    # four-column line after it for a new opener, while CommonMark keeps the first fence open all
    # along. Only a column-0 closer closes that real fence: the indented closer this splitter once
    # wrote for the four-column opener left it open and swallowed the navigation link.
    chunks = split_text(text, 512, container_fences=container_fences)
    assert len(chunks) > 1 and rejoin(chunks) == text
    assert _renders_next_link(chunks[0])
    if container_fences:
        legacy = split_text(text, 512)
        assert [chunk.body for chunk in chunks] == [chunk.body for chunk in legacy]
        for new, old in zip(chunks, legacy, strict=True):
            assert _renders_next_link(new) or not _renders_next_link(old)


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
