"""The library table of contents (`cce_library_read` with `toc`) on awkward markdown.

Every record here is synthetic. The expected headings come from an independent CommonMark parser
(markdown-it-py), never from the code under test: an entry must sit at the exact character offset
of a top-level ATX heading that is not inside fenced code, and nothing else may be listed.
"""

from __future__ import annotations

import random
import re
import time

import pytest
from markdown_it import MarkdownIt

from conversation_corpus_engine import library_mcp
from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.library_commands import _headings
from conversation_corpus_engine.library_mcp import READ_TOOL
from conversation_corpus_engine.library_store import Library

LINE_END = re.compile(r"\r\n|\r|\n")


def expected_headings(text: str) -> list[tuple[int, int]]:
    """(character offset of the line, level) of each top-level ATX heading, per CommonMark."""
    starts = [0] + [match.end() for match in LINE_END.finditer(text)]
    return [
        (starts[token.map[0]], len(token.markup))
        for token in MarkdownIt().parse(text)
        if token.type == "heading_open" and token.level == 0 and token.markup.startswith("#")
    ]


@pytest.fixture
def toc(tmp_path, monkeypatch):
    """Build a one-record library from message texts; return (uid, call) for its toc."""

    def build(*texts: str, alternates: list[str] | None = None):
        path = tmp_path / "toc-library.sqlite"
        branch = [{"role": "assistant", "text": text} for text in alternates or []]
        record = Conversation(
            provider="chatgpt",
            source_id="toc-probe",
            title="Toc probe",
            messages=[Message(role="user", text=text) for text in texts],
            extra={"alternate_messages": branch} if branch else {},
        )
        with Library.create(path) as library:
            library.put(record)
        monkeypatch.setattr(library_mcp, "open_library", lambda root: (tmp_path, Library(path)))

        def call(**arguments):
            return library_mcp.call_tool(
                tmp_path, READ_TOOL, {"uid": record.uid, "toc": True, **arguments}
            )

        return record.uid, call

    return build


def all_entries(call, **arguments) -> list[dict]:
    """Every TOC entry, paged by the tool's own cursor."""
    found, cursor = [], 0
    for _ in range(10000):
        page = call(toc_offset=cursor, **arguments)
        found += page["toc"]
        cursor = page["toc_next_offset"]
        if cursor is None:
            assert len(found) == page["toc_total"]
            return found
    raise AssertionError("the table of contents never ended")


def listed(entries: list[dict], message: int = 0) -> list[tuple[int, int]]:
    return [(e["offset"], e["level"]) for e in entries if e["message"] == message]


FENCE_CASES = {
    "backtick-fence-holds-hash-lines": (
        "# Top\nintro\n```\n# comment in code\n## also code\n```\n## After\ntext\n"
    ),
    "tilde-fence-is-not-closed-by-backticks": (
        "# Top\n~~~\n# inside\n```\n# still inside the tilde fence\n~~~\n## After\n"
    ),
    "four-backtick-fence-holds-a-three-backtick-block": (
        "# Top\n````\n```\n# inside\n```\n# still inside\n````\n### After\n"
    ),
    "info-string-line-inside-a-fence-is-content": (
        "# Top\n```text\n```python\n# not a heading\n```\n## Real\n"
    ),
    "longer-closing-fence": ("# Top\n```\n# inside\n`````\n## After\n"),
    "unclosed-fence-hides-the-rest": ("# Top\n```\n# never closed\n## nor this\n"),
    "indented-fence-in-a-list": (
        "# Top\n1. Step\n    ```bash\n    # a comment\n    ```\n## After\n"
    ),
}


@pytest.mark.parametrize("name", FENCE_CASES)
def test_headings_are_fence_aware_the_way_commonmark_is(toc, name):
    text = FENCE_CASES[name]
    _, call = toc(text)
    result = call()
    assert result["toc_kind"] == "headings"
    assert listed(result["toc"]) == expected_headings(text)
    assert result["toc_total"] == len(expected_headings(text)) and result["toc_exact"] is True


def test_every_entry_offset_is_exactly_the_start_of_its_heading_line(toc, tmp_path):
    texts = [FENCE_CASES[name] for name in FENCE_CASES]
    uid, call = toc(*texts)
    entries = all_entries(call)
    assert entries, "the records hold headings"
    for entry in entries:
        text = texts[entry["message"]]
        line = text[entry["offset"] :].split("\n", 1)[0]
        assert line.startswith("#" * entry["level"])
        assert entry["offset"] == 0 or text[entry["offset"] - 1] == "\n"
        arguments = {
            "uid": uid,
            "message": entry["message"],
            "offset": entry["offset"],
            "limit": len(line),
        }
        assert library_mcp.call_tool(tmp_path, READ_TOOL, arguments)["text"] == line


def test_unicode_line_separators_do_not_start_a_heading(toc):
    """Only \\n, \\r\\n and \\r end a CommonMark line. U+2028, form feed and the C0 separators
    that Python's str.splitlines honours are ordinary characters inside a paragraph."""
    text = "intro # not a heading\x0c## nor this\x1c### nor this\x85#### nor this\n# Real\n"
    _, call = toc(text)
    assert expected_headings(text) == [(text.index("# Real"), 1)]
    assert listed(call()["toc"]) == expected_headings(text)


def test_a_closing_sequence_needs_whitespace_before_it(toc):
    text = "# Learning C#\n## Closed ##\n### Spaced   ###   \n#### Hash#tag\n"
    _, call = toc(text)
    titles = [entry["heading"] for entry in call()["toc"]]
    assert titles == ["Learning C#", "Closed", "Spaced", "Hash#tag"]


def test_crlf_and_lone_cr_line_endings_keep_exact_offsets(toc):
    text = "# One\r\nbody\r\n## Two\rbody\r### Three\r\n"
    _, call = toc(text)
    assert listed(call()["toc"]) == expected_headings(text)
    assert len(expected_headings(text)) == 3


def test_a_very_long_line_is_listed_in_linear_time(toc):
    """A heading line made mostly of '#' characters must not make the scan quadratic."""
    text = "# " + "#" * 40000 + "x\n## after\n"
    _, call = toc(text)
    started = time.perf_counter()
    result = call()
    assert time.perf_counter() - started < 1.0
    assert listed(result["toc"]) == expected_headings(text)
    assert result["toc"][0]["heading"] == "#" * 100 and result["toc"][1]["heading"] == "after"


@pytest.mark.parametrize("opener", [">", "> ", "- ", "1. ", "> - "])
def test_a_line_of_thousands_of_nested_containers_is_scanned_in_linear_time(toc, opener):
    """Each quote or item marker opens a container; the depth is bounded so a hostile line of
    twenty thousand of them costs a line's worth of work, not its square. (markdown-it-py stops
    reading a document at its own nesting limit, so it is not the reference here: the next line
    is a heading outside every container and is listed.)"""
    text = opener * 20000 + "# not top level\n# top level\n"
    _, call = toc(text)
    started = time.perf_counter()
    result = call()
    assert time.perf_counter() - started < 1.0
    assert listed(result["toc"]) == [(text.index("# top level"), 1)]
    assert [entry["heading"] for entry in result["toc"]] == ["top level"]


def test_a_very_long_heading_title_is_clipped_but_its_offset_is_exact(toc):
    text = "intro\n# " + "long title " * 400 + "\n## short\n"
    _, call = toc(text)
    entries = call()["toc"]
    assert listed(entries) == expected_headings(text)
    assert len(entries[0]["heading"]) == 100 and entries[1]["heading"] == "short"


def test_quoted_and_setext_headings_are_not_listed_and_nothing_false_is(toc):
    """The table lists ATX headings that open a line, as the site does. A quoted heading, a
    setext heading and a line indented four or more spaces are not entries (none is invented)."""
    text = (
        "> # quoted heading\n> body\n\n"
        "Setext title\n============\n\nSecond setext\n-------------\n\n"
        "    # indented code, not a heading\n\n"
        "# Real heading\n"
    )
    _, call = toc(text)
    assert listed(call()["toc"]) == [(text.index("# Real heading"), 1)]
    assert [entry["heading"] for entry in call()["toc"]] == ["Real heading"]


def test_a_record_without_headings_falls_back_to_a_message_index(toc):
    _, call = toc("plain words only", "```\n# only inside a fence\n```", "more plain words")
    auto = call()
    assert auto["toc_kind"] == "messages" and auto["toc_total"] == 3
    assert [entry["message"] for entry in auto["toc"]] == [0, 1, 2]
    assert auto["toc"][0]["preview"] == "plain words only"
    forced = call(toc_mode="headings")
    assert forced["toc_kind"] == "headings" and forced["toc"] == [] and forced["toc_total"] == 0
    assert forced["toc_next_offset"] is None and forced["toc_exact"] is True


def test_paging_the_table_is_stable_and_exact(toc):
    texts = [
        "".join(f"{'#' * (1 + (n % 6))} Section {message}-{n}\ntext\n" for n in range(40))
        for message in range(30)
    ]
    _, call = toc(*texts)
    everything = all_entries(call)
    assert len(everything) == 1200
    for size in (1, 7, 50, 499, 500):
        pages, cursor = [], 0
        while cursor is not None:
            page = call(toc_offset=cursor, toc_limit=size)
            assert page["toc_total"] == 1200 and len(page["toc"]) <= size
            pages += page["toc"]
            cursor = page["toc_next_offset"]
        assert pages == everything, size
    assert call(toc_offset=1200)["toc"] == [] and call(toc_offset=1199)["toc_next_offset"] is None
    for message, text in enumerate(texts[:3]):
        assert listed(everything, message) == expected_headings(text)


def test_the_alternate_branch_has_its_own_fence_aware_table(toc):
    alternate = "# Branch\n~~~\n# inside\n```\n# still inside\n~~~\n## Branch two\n"
    _, call = toc("# Main only\n", alternates=[alternate])
    assert listed(call(sequence="alternate")["toc"]) == expected_headings(alternate)
    assert [entry["heading"] for entry in call()["toc"]] == ["Main only"]


# --- fences are fences only where CommonMark says so: indentation and list context ---------------

INDENT_CASES = {
    "four-space-fence-is-indented-code": "# Top\n\n    ```\n# After\n    ```\n## Last\n",
    "tab-fence-is-indented-code": "# Top\n\n\t```\n# After\n\t```\n## Last\n",
    "paragraph-text-before-an-indented-fence-line": "# Top\n\ntext\n    ```\n## Last\n",
    "line-separator-before-backticks-is-text": "# Top\n\n\u2028```\n# After\n```\n## Last\n",
    "form-feed-before-backticks-is-text": "# Top\n\n\x0c```\n# After\n```\n## Last\n",
    "no-break-space-before-backticks-is-text": "# Top\n\n\u00a0```\n# After\n```\n## Last\n",
    "three-space-fence-is-a-fence": "# Top\n\n   ```\n# inside\n   ```\n## Last\n",
    "closing-fence-indented-four-does-not-close": (
        "# Top\n\n```\n# inside\n    ```\n# still inside\n```\n## Last\n"
    ),
    "list-item-fence": "# Top\n\n- item\n\n  ```\n  # inside\n  ```\n\n## Last\n",
    "ordered-item-fence-indented-past-the-marker": (
        "# Top\n\n1. step\n\n       ```\n       # inside\n       ```\n\n## Last\n"
    ),
    "item-fence-ends-with-its-item": (
        "# Top\n\n- item\n\n  ```\n  # inside\n## Last\n  # not a heading\n"
    ),
    "outdented-line-after-a-list-leaves-it": ("- item\n\n    ```\n# After\n    ```\n## Last\n"),
    "indented-fence-after-the-list-ended-is-code": (
        "- item\n\npara\n\n    ```\n# After\n    ```\n"
    ),
    "item-first-line-opens-a-fence": "- ```\n  # inside\n  ```\n\n# After\n",
}


@pytest.mark.parametrize("name", INDENT_CASES)
def test_fence_indentation_and_list_context_follow_commonmark(toc, name):
    text = INDENT_CASES[name]
    _, call = toc(text)
    result = call()
    want = expected_headings(text)
    assert listed(result["toc"]) == want, (name, want)
    assert result["toc_total"] == len(want)


WORDS = ["alpha", "beta", "gamma", "delta", "kappa", "lumen", "onyx", "prism", "quartz", "rune"]
ODD_LINES = [" ```", "\x0c```", " # x", "\x0c# not", " ```", "　# y", "\u0085```"]


def phrase(rng: random.Random) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(rng.randint(1, 4)))


def fence_lines(rng: random.Random, outer: str = "", extra: int = 0) -> list[str]:
    """A fenced block whose lines all sit `outer` columns in (a list item's content offset)."""
    char, count = rng.choice("`~"), rng.randint(3, 5)
    opener = " " * (extra if outer else rng.randint(0, 3))
    info = rng.choice(["", "", "python", "text x"])
    body = [
        rng.choice(
            ["# fake heading", "## another", "text", "```", "~~~", "````", "~~~~~", "``` py", ""]
        )
        for _ in range(rng.randint(0, 4))
    ]
    lines = [outer + opener + char * count + info] + [outer + line if line else "" for line in body]
    if rng.random() < 0.85:
        closer = " " * (extra if outer else rng.randint(0, 3))
        lines.append(outer + closer + char * (count + rng.randint(0, 2)))
    return lines


def list_lines(rng: random.Random) -> list[str]:
    ordered = rng.random() < 0.4
    start = rng.choice([1, 1, 2, 7, 10])
    lines: list[str] = []
    for position in range(rng.randint(1, 3)):
        marker = f"{start + position}." if ordered else rng.choice("-*+")
        lead = " " * rng.randint(0, 2)
        gap = " " * rng.randint(1, 3)
        offset = len(lead) + len(marker) + len(gap)
        lines.append(f"{lead}{marker}{gap}{phrase(rng)}")
        for _ in range(rng.randint(0, 2)):
            kind = rng.choice(["fence", "fence", "heading", "text", "code"])
            lines.append("")
            inner = " " * offset
            if kind == "fence":
                lines += fence_lines(rng, inner, rng.randint(0, 3))
            elif kind == "heading":
                lines.append(inner + "# " + phrase(rng))  # a heading inside the item
            elif kind == "text":
                lines.append(inner + phrase(rng))
            else:
                lines.append(inner + "    " + rng.choice(["```", "# code", "plain"]))
    return lines


def block_lines(rng: random.Random) -> list[str]:
    kind = rng.choices(
        ["paragraph", "heading", "fence", "lookalike", "odd", "list", "quote", "rule", "setext"],
        weights=[3, 4, 4, 3, 2, 3, 1, 1, 1],
    )[0]
    if kind == "heading":
        level = rng.randint(1, 6)
        closing = rng.choice(["", "", " ##", " #####"])
        return [" " * rng.randint(0, 3) + "#" * level + " " + phrase(rng) + closing]
    if kind == "fence":
        return fence_lines(rng)
    if kind == "lookalike":
        prefix = rng.choice(["    ", "\t", "     ", "   \t"])
        return [
            prefix + rng.choice(["```", "~~~", "# not a heading", "code"])
            for _ in range(rng.randint(1, 3))
        ]
    if kind == "odd":
        return [rng.choice(ODD_LINES), *(phrase(rng) for _ in range(rng.randint(0, 1)))]
    if kind == "list":
        return list_lines(rng)
    if kind == "quote":
        return ["> " + rng.choice(["# quoted", "text", "```", "~~~", "code"]) for _ in range(2)]
    if kind == "rule":
        return [rng.choice(["---", "***", "___", "- - -", " ***"])]
    if kind == "setext":
        return [phrase(rng), rng.choice(["=====", "-----"])]
    return [phrase(rng) for _ in range(rng.randint(1, 3))]


def generated_document(index: int) -> str:
    rng = random.Random(index)
    lines: list[str] = []
    for _ in range(rng.randint(2, 9)):
        block = block_lines(rng)
        if lines and not (lines[-1].lstrip(" ").startswith("#") and rng.random() < 0.1):
            lines.append("")
        lines += block
    ending = rng.choices(["\n", "\r\n", "\r"], weights=[70, 15, 15])[0]
    return ending.join(lines) + (ending if rng.random() < 0.7 else "")


def oracle_entries(text: str) -> list[tuple[int, int, str]]:
    """(character offset, level, title) of each top-level ATX heading, from markdown-it-py."""
    starts = [0] + [match.end() for match in LINE_END.finditer(text)]
    tokens = MarkdownIt().parse(text)
    return [
        (starts[token.map[0]], len(token.markup), tokens[number + 1].content)
        for number, token in enumerate(tokens)
        if token.type == "heading_open" and token.level == 0 and token.markup.startswith("#")
    ]


def test_headings_agree_with_an_independent_commonmark_parser_over_generated_documents():
    """1,500 documents built from headings, fenced code (both characters, any length, indented 0-3
    columns), indented code, lines that only look indented (tab, U+2028, form feed, no-break
    space), lists with fences nested at their content offset, quotes, rules and setext headings,
    with LF, CRLF and CR line ends. Every top-level ATX heading must come out at the same offset,
    level and title as markdown-it-py reports, and nothing else may."""
    mismatches = []
    for index in range(1500):
        text = generated_document(index)
        if _headings(text) != oracle_entries(text):
            mismatches.append(index)
    assert not mismatches, (
        f"{len(mismatches)} of 1500 documents differ; first: "
        f"{[(i, generated_document(i)) for i in mismatches[:2]]}"
    )


# --- list items and quotes the way markdown-it-py reads them, on a second family of documents ---


def commonmark_entries(text: str) -> list[tuple[int, int, str]]:
    """(character offset, level, title with its whitespace stripped) of each non-empty top-level
    ATX heading, from markdown-it-py (the code under test lists none with an empty title)."""
    return [
        (offset, level, title.strip()) for offset, level, title in oracle_entries(text) if title
    ]


def listed_entries(text: str) -> list[tuple[int, int, str]]:
    return [(offset, level, title.strip()) for offset, level, title in _headings(text)]


# Each of these once differed from markdown-it-py (or exercises the same rule): a list marker
# followed by five or more columns makes the first line indented code, which a following
# unindented line cannot continue; an empty item ends at a blank line; a quote line is a block of
# its own, never a lazy continuation of a list item's paragraph, and never one of its own
# fence's lines.
HARD_CASES = {
    "marker-then-five-columns": "-     text\r\npara text\r\n   ###\tT U\r\n",
    "ordered-marker-then-five-columns": "2)     item\n more words\n   ###\tend #  \n",
    "plus-marker-then-five-columns": "+     text\npara text\r\n   #  A b\r",
    "marker-then-five-columns-not-a-fence": "-     ```\r  ####### end #  \rmore words\r  ##  A b\r",
    "empty-item-then-blank-line": "2)     \n\n   ##### \\# esc\n",
    "empty-item-then-tab-line": "+ \r\t\r  ######\tx #\r",
    "quote-is-not-a-lazy-continuation": "- text\n > quote\n  ##### x #\n",
    "empty-quote-line-ends-the-paragraph": "> \r2)     \r   ### A b\n",
    "fence-in-a-quote-ends-with-the-quote": "2)  \r   > ```\rpara text\r   ~~~~~~\t\r###   nb\r",
    "heading-in-a-quote-is-not-a-paragraph": "1. text\n> # qh\n   ~~~~~~ js x\n##  T U\n",
    "empty-item-then-quote-with-a-fence": "-\t\r   > ```\r `inline`\r   ~~~~~~python\r ######\tT\x0cV\r",
    "heading-inside-an-item-stays-inside": "- item\n  # inside\n# outside\n",
    "setext-dash-underline-ends-a-paragraph": "- a\n  b\n  ---\n# top\n",
    "quoted-list-item-continues-in-the-quote": "> - a\n>   # inside\n# top\n",
}


@pytest.mark.parametrize("name", sorted(HARD_CASES))
def test_headings_agree_with_markdown_it_on_the_cases_that_once_differed(name):
    text = HARD_CASES[name]
    assert listed_entries(text) == commonmark_entries(text)


def probe_document(rng: random.Random) -> str:
    """A document of up to 30 lines of fences, headings, list markers followed by 1 to 5 columns
    or a tab, quotes, blank lines, rules, tab-indented code and text, with LF, CRLF, CR or mixed
    line ends. No HTML block is ever generated: `_headings` does not model them."""

    def piece() -> str:
        kind = rng.random()
        indent = " " * rng.choice([0, 0, 0, 1, 2, 3, 4, 5, 6, 8])
        if kind < 0.18:
            info = rng.choice(["", "python", " js x", "a`b", "~~", "\t"])
            return f"{indent}{rng.choice('`~') * rng.randint(3, 6)}{info}"
        if kind < 0.40:
            title = rng.choice(
                ["Title", "A b", "x #", "x ##", "#", "", "T U", "T\fV", " nb", "t\tt", "\\# esc"]
                + ["end #  ", "T U", "no break"]
            )
            return f"{indent}{'#' * rng.randint(1, 7)}{rng.choice([' ', chr(9), '', '  '])}{title}"
        if kind < 0.55:
            marker = rng.choice(["-", "*", "+", "1.", "2)", "10."])
            gap = rng.choice([" ", "  ", "\t", "     "])
            return f"{indent}{marker}{gap}{rng.choice(['item', '# H', '```', '~~~~', '', 'text'])}"
        if kind < 0.62:
            return f"{indent}> {rng.choice(['quote', '# qh', '```', ''])}"
        if kind < 0.68:
            return rng.choice(["", "   ", "\t", "\f", " ", " "])
        if kind < 0.72:
            return f"{indent}{rng.choice(['---', '***', '___', '===', '- - -', '* * *'])}"
        if kind < 0.76:
            return "\t" + rng.choice(["```", "# tab", "~~~"])
        return f"{indent}{rng.choice(['para text', 'more words', '`inline`', 'x'])}"

    ending = rng.choice(["\n", "\r\n", "\r", "mixed"])
    text = ""
    for _ in range(rng.randint(1, 30)):
        text += piece() + (rng.choice(["\n", "\r\n", "\r"]) if ending == "mixed" else ending)
    return text.rstrip("\r\n") if rng.random() < 0.2 else text


def test_headings_agree_with_markdown_it_over_3000_documents_without_html_blocks():
    """Offset, level and title (whitespace stripped, so a no-break space at an edge compares as
    markdown-it-py's own strip would) of every top-level ATX heading, on documents whose list
    markers are followed by 1 to 5 columns, whose quotes hold fences and empty lines, and whose
    items start empty. HTML blocks are the one thing `_headings` does not model, so none is
    generated."""
    rng = random.Random(7)
    differing = []
    for _ in range(3000):
        text = probe_document(rng)
        if listed_entries(text) != commonmark_entries(text):
            differing.append(text)
    assert not differing, (
        f"{len(differing)} of 3000 documents differ; first: {differing[0]!r}: "
        f"{listed_entries(differing[0])} against {commonmark_entries(differing[0])}"
    )
