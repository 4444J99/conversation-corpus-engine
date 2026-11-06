"""The library table of contents (`cce_library_read` with `toc`) on awkward markdown.

Every record here is synthetic. The expected headings come from an independent CommonMark parser
(markdown-it-py), never from the code under test: an entry must sit at the exact character offset
of a top-level ATX heading that is not inside fenced code, and nothing else may be listed.
"""

from __future__ import annotations

import re
import time

import pytest
from markdown_it import MarkdownIt

from conversation_corpus_engine import library_mcp
from conversation_corpus_engine.archive_model import Conversation, Message
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
