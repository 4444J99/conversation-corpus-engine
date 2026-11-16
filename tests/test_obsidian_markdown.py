"""Actual CommonMark inline-code positions must map back to original UTF-8 bytes."""

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def context():
    path = Path(__file__).parents[1] / "scripts/obsidian_markdown.py"
    assert path.exists(), "CommonMark source mapping is not implemented"
    spec = importlib.util.spec_from_file_location("obsidian_markdown_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "raw",
    [
        b"prefix ``alpha\nbeta ![pixel](https://example.invalid/)\ngamma`` rest\n",
        b"> prefix ``alpha\r\n> beta <img src=x>\r\n> gamma`` rest\r\n",
        b"1. prefix ``alpha\n   beta ![pixel](https://example.invalid/)\n   gamma`` rest\n",
        b"- prefix ``alpha\n\tbeta <img src=x>\n\tgamma`` rest\n",
        "> \u03bb \U0001f30d ``alpha\r\n> beta <tag>\r\n> gamma``\r\n".encode(),
    ],
)
def test_multiline_containers_keep_exact_raw_offsets(context, raw):
    cut = raw.index(b"beta") + 2
    spans, stats = context.code_spans(raw, [cut])
    assert len(spans) == 1
    span = spans[0]
    assert span["span_start"] == raw.index(b"``")
    assert span["span_end"] == raw.rindex(b"``") + 2
    assert span["content_start"] == span["span_start"] + 2
    assert span["content_end"] == span["span_end"] - 2
    assert span["boundaries"] == [cut]
    assert stats["inline_blocks_parsed"] == 1


def test_code_cannot_cross_paragraph_or_fenced_block_boundaries(context):
    raw = b"`first paragraph\n\nsecond paragraph`\n\n```text\n`inside\nblock`\n```\n"
    spans, _ = context.code_spans(raw, [raw.index(b"second"), raw.index(b"block")])
    assert spans == []


def test_parser_does_not_treat_backticks_in_html_attributes_as_code(context):
    raw = b'<span title="`alpha">outside\nnext</span> `actual\ncode`\n'
    cut = raw.index(b"code") + 1
    spans, _ = context.code_spans(raw, [cut])
    assert len(spans) == 1
    assert spans[0]["span_start"] == raw.index(b"`actual")


def test_only_inline_blocks_crossing_actual_cuts_are_parsed(context):
    raw = b"`irrelevant`\n\n" + b"plain " * 100 + b"\n\n`target\ncontinued`\n"
    spans, stats = context.code_spans(raw, [raw.index(b"continued") + 2])
    assert len(spans) == 1
    assert stats["inline_blocks_seen"] == 3
    assert stats["inline_blocks_parsed"] == 1


def test_source_cap_and_deadline_callback_are_enforced(context):
    with pytest.raises(ValueError, match="source.*byte"):
        context.code_spans(b"`123456`", [4], max_source_bytes=4)

    def expired():
        raise TimeoutError("bounded audit deadline")

    with pytest.raises(TimeoutError, match="deadline"):
        context.code_spans(b"`123456`", [4], check=expired)


def test_a_boundary_inside_a_delimiter_is_reported(context):
    raw = b"before ```alpha\nbeta``` after"
    cut = raw.index(b"```") + 1
    spans, _ = context.code_spans(raw, [cut])
    assert spans[0]["boundary_inside_delimiter"] is True
