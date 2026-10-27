from __future__ import annotations

import hashlib

import pytest

from conversation_corpus_engine.library_blocks import iter_blocks, render_blocks, split_blocks

PARAGRAPH = (
    "Every source keeps its original identity, speaker, sequence and provenance. "
    "Repeated material can point to its first occurrence while the exact original "
    "text remains available for review and for future interpretation."
)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "  \n\n",
        "First.\n\nSecond.\n",
        "Alpha\r\n\r\nBeta\rGamma",
        "A snowman: \u2603 and an emoji: \U0001f642.\n\n" + PARAGRAPH,
        "> Quote.\n>\n> More.\n",
        "Intro\n```python\n\nprint('x')\n```\n\nOutro",
    ],
)
def test_blocks_cover_exact_original_characters(text: str) -> None:
    blocks = split_blocks(text)
    assert "".join(block["text"] for block in blocks) == text
    position = 0
    for block in blocks:
        assert block["start"] == position
        assert text[block["start"] : block["end"]] == block["text"]
        assert block["end"] == block["start"] + len(block["text"])
        assert block["raw_digest"] == hashlib.sha256(block["text"].encode()).hexdigest()
        assert block["digest"] == hashlib.sha256(block["canonical_text"].encode()).hexdigest()
        position = block["end"]
    assert position == len(text)


def test_quote_and_newline_normalization_are_explicit_and_limited() -> None:
    raw = PARAGRAPH + "\nAn unchanged second line."
    quoted = "> > " + raw.replace("\n", "\r\n> > ") + "\r\n"
    ordinary = split_blocks(raw)[0]
    quotation = split_blocks(quoted)[0]
    assert quotation["text"] == quoted
    assert quotation["digest"] == ordinary["digest"]
    assert quotation["canonical_text"] == raw
    assert quotation["normalization"] == [
        "line-endings-lf",
        "common-quote-depth:2",
        "terminal-newline",
    ]
    assert quotation["eligible"] is True
    assert split_blocks(raw.replace("identity", "Identity"))[0]["digest"] != ordinary["digest"]
    assert split_blocks(raw.replace("keeps its", "keeps  its"))[0]["digest"] != ordinary["digest"]


def test_mixed_quote_and_plain_lines_are_not_unquoted() -> None:
    text = "> " + PARAGRAPH + "\nThis is not quoted."
    block = split_blocks(text)[0]
    assert block["canonical_text"] == text
    assert not any(item.startswith("common-quote-depth") for item in block["normalization"])


@pytest.mark.parametrize("text", ["\n\n\n", "  \n\n\n", "hello\n\n\n\nworld"])
def test_rendering_does_not_add_blank_lines(text: str) -> None:
    assert render_blocks(split_blocks(text), {}) == text


def test_short_text_is_never_replaced_even_when_referenced() -> None:
    blocks = split_blocks("A short repeated answer.")
    assert blocks[0]["eligible"] is False
    assert (
        render_blocks(blocks, {blocks[0]["digest"]: {"label": "Source", "href": "a.md"}})
        == "A short repeated answer."
    )


@pytest.mark.parametrize(
    "text",
    [
        "```text\n" + PARAGRAPH + "\n```",
        "````markdown\n```\n" + PARAGRAPH + "\n```\n````",
        "~~~\n" + PARAGRAPH + "\n~~~",
        "    " + PARAGRAPH,
        "\t" + PARAGRAPH,
        "> ```text\n> " + PARAGRAPH + "\n> ```",
        "```text\n" + PARAGRAPH,
    ],
)
def test_fenced_and_indented_code_never_becomes_a_reference(text: str) -> None:
    blocks = split_blocks(text)
    assert not any(block["eligible"] for block in blocks)
    references = {block["digest"]: {"label": "Source", "href": "a.md"} for block in blocks}
    assert "Repeated passage" not in render_blocks(blocks, references)


def test_inline_code_is_not_folded_with_prose() -> None:
    assert not split_blocks(PARAGRAPH + " Use `exact_bytes`.")[0]["eligible"]


def test_reference_only_replaces_the_verified_long_block() -> None:
    text = "Unique before.\n\n" + PARAGRAPH + "\n\nUnique after."
    blocks = split_blocks(text)
    long = next(block for block in blocks if block["eligible"])
    rendered = render_blocks(
        blocks,
        {long["digest"]: {"label": "Earlier source", "href": "../records/first.md#message-4"}},
    )
    assert rendered == (
        "Unique before.\n\n"
        "[Repeated passage: Earlier source](../records/first.md#message-4)\n\n"
        "Unique after."
    )
    assert "original identity" not in rendered
    assert "".join(block["text"] for block in blocks) == text


def test_rendering_neutralizes_untrusted_links_images_html_and_wikilinks() -> None:
    text = "# Heading\n[[Elsewhere]] ![pixel](https://tracker.example/x) [run](javascript:alert(1))\n<script>alert(1)</script>\n#tag"
    rendered = render_blocks(split_blocks(text), {})
    assert "[[Elsewhere]]" not in rendered
    assert "![pixel]" not in rendered
    assert "[run](" not in rendered
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert not rendered.startswith("# Heading")


def test_rendering_preserves_code_and_balances_an_unclosed_fence() -> None:
    text = "```html\n<img src='https://tracker.example/x'>\n[[shell]]"
    assert render_blocks(split_blocks(text), {}) == text + "\n```"


@pytest.mark.parametrize(
    "href",
    [
        "javascript:alert(1)",
        "data:text/html,hello",
        "https://example.com/source.md",
        "//example.com/source.md",
        "/etc/passwd",
        "file:///etc/passwd",
        "source.md\n![bad](https://example.com)",
        "source\\file.md",
        "source%0a.md",
        "javascript%3aalert(1)",
        "%2f%2fexample.com/source.md",
    ],
)
def test_renderer_rejects_unsafe_reference_targets(href: str) -> None:
    block = split_blocks(PARAGRAPH)[0]
    with pytest.raises(ValueError, match="reference"):
        render_blocks([block], {block["digest"]: {"label": "Source", "href": href}})


def test_reference_labels_and_paths_cannot_escape_the_generated_link() -> None:
    block = split_blocks(PARAGRAPH)[0]
    rendered = render_blocks(
        [block],
        {
            block["digest"]: {
                "label": "[x]\n![a](b) <img>",
                "href": "../records/a (draft).md#message-2",
            }
        },
    )
    assert "../records/a%20%28draft%29.md#message-2" in rendered
    assert "<img>" not in rendered
    assert "](" not in rendered.split("](../records/", 1)[0]


@pytest.mark.parametrize("prefix", ["    ", "\t", ">     "])
def test_indented_code_remains_copyable_literal_text(prefix: str) -> None:
    text = prefix + "values[0] < 2 and ![literal](image.png)\n"
    assert render_blocks(split_blocks(text), {}) == "\n```text\n" + text + "```\n"


def test_indented_source_cannot_close_its_explicit_wrapper() -> None:
    text = "    ````\n    [literal](https://example.invalid)\n    ```\n"
    assert render_blocks(split_blocks(text), {}) == "\n`````text\n" + text + "`````\n"


def test_literal_quote_fence_cannot_close_unquoted_code() -> None:
    text = "~~~markdown\n> ~~~\nvalues[0] < 2\n~~~\n"
    blocks = split_blocks(text)
    assert len(blocks) == 1 and blocks[0]["kind"] == "code"
    assert render_blocks(blocks, {}) == text


@pytest.mark.parametrize(
    "ending", ["\r\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]
)
def test_all_line_boundaries_keep_fences_and_paragraphs_separate(ending: str) -> None:
    text = "para" + ending + "```\n![x](https://example.invalid/p.png)\n```\n"
    assert render_blocks(split_blocks(text), {}) == text.replace(ending, "\n")
    assert render_blocks(split_blocks("A" + ending + ending + "B"), {}) == "A\n\nB"


def test_lazy_blocks_preserve_all_python_line_boundaries() -> None:
    text = "A\r\n\rB\v\vC\f\x1c\x1d\x1eD\x85\u2028\u2029End"
    blocks = list(iter_blocks(text))
    assert blocks == split_blocks(text)
    assert "".join(block["text"] for block in blocks) == text
    assert all(text[b["start"] : b["end"]] == b["text"] for b in blocks)


def test_lazy_blocks_stop_before_materializing_the_remaining_source():
    calls = 0

    def limited():
        nonlocal calls
        calls += 1
        if calls > 4:
            raise TimeoutError("bounded source scan")

    stream = iter_blocks("paragraph\n\n" * 100000, check_callback=limited)
    assert iter(stream) is stream
    assert next(stream)["text"] == "paragraph\n"
    with pytest.raises(TimeoutError, match="bounded source scan"):
        list(stream)
    assert calls == 5


def test_renderer_rejects_noncontiguous_or_modified_blocks() -> None:
    block = split_blocks(PARAGRAPH)[0]
    with pytest.raises(ValueError, match="contiguous"):
        render_blocks([{**block, "start": 1}], {})
    with pytest.raises(ValueError, match="digest"):
        render_blocks([{**block, "text": PARAGRAPH + "!", "end": len(PARAGRAPH) + 1}], {})
    with pytest.raises(ValueError, match="canonical"):
        render_blocks(
            [
                {
                    **block,
                    "canonical_text": "forged",
                    "digest": hashlib.sha256(b"forged").hexdigest(),
                }
            ],
            {},
        )
