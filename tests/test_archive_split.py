"""Lossless splitting and packing: a cap with no text dropped."""

from __future__ import annotations

import random

import pytest

from conversation_corpus_engine.archive_split import (
    FENCE_SLACK,
    MIN_CHUNK_BYTES,
    Chunk,
    fit_blocks,
    pack,
    paginate_rows,
    rejoin,
    split_text,
    utf8_size,
)
from conversation_corpus_engine.archive_text import FenceTracker


def _text(seed: int, lines: int) -> str:
    """Invented, varied text: prose of uneven length, unicode, blank lines and fenced code."""
    rng = random.Random(seed)
    out: list[str] = []
    in_fence = False
    for number in range(lines):
        roll = rng.random()
        if roll < 0.05:
            out.append("```" if not in_fence else "```")
            in_fence = not in_fence
        elif roll < 0.12:
            out.append("")
        elif roll < 0.2:
            out.append("é" * rng.randint(1, 200) + " 漢字 🙂")
        else:
            out.append(f"line {number}: " + "w" * rng.randint(0, 300))
    if in_fence:
        out.append("```")
    return "\n".join(out)


@pytest.mark.parametrize("seed", range(12))
def test_every_chunk_fits_and_the_text_is_recovered_byte_for_byte(seed: int) -> None:
    text = _text(seed, 900)
    limit = 2048 + seed * 700
    chunks = split_text(text, limit)
    assert len(chunks) > 1
    assert all(utf8_size(chunk.text()) <= limit for chunk in chunks)
    assert rejoin(chunks) == text


@pytest.mark.parametrize("seed", range(6))
def test_a_fence_cut_by_a_split_is_closed_and_reopened(seed: int) -> None:
    text = _text(seed, 900)
    for chunk in split_text(text, 3000):
        tracker = FenceTracker()
        for line in chunk.text().split("\n"):
            tracker.feed(line)
        assert tracker.opener == "", "a chunk must not end inside a fence it opened"


def test_text_that_fits_is_one_chunk_and_unchanged() -> None:
    assert split_text("short", 1024) == [Chunk("short")]
    assert rejoin(split_text("", 1024)) == ""


def test_a_line_longer_than_the_cap_is_cut_on_a_character_boundary() -> None:
    line = "é漢🙂" * 5000  # 2, 3 and 4 byte characters, no newline anywhere
    chunks = split_text(line, 1024)
    assert len(chunks) > 10
    assert all(utf8_size(chunk.text()) <= 1024 for chunk in chunks)
    assert all(chunk.continues_line for chunk in chunks[1:])
    assert rejoin(chunks) == line


def test_a_lone_trailing_newline_and_blank_runs_survive() -> None:
    text = "a\n\n\n" + "b" * 700 + "\n\n" + "c" * 700 + "\n"
    chunks = split_text(text, 1024)
    assert rejoin(chunks) == text


def test_the_limit_must_leave_room_for_a_useful_chunk() -> None:
    assert MIN_CHUNK_BYTES > FENCE_SLACK
    with pytest.raises(ValueError, match="at least"):
        split_text("x" * 5000, MIN_CHUNK_BYTES - 1)


def test_fit_blocks_halves_the_pieces_until_an_expanding_renderer_fits() -> None:
    text = "\n".join("<&>" * 40 for _ in range(300))

    def render(piece: str, index: int, total: int) -> str:
        return f"[{index + 1}/{total}] " + piece.replace("&", "&amp;" * 4).replace("<", "&lt;")

    blocks = fit_blocks(text, render, 4096)
    assert all(utf8_size(block) <= 4096 for block in blocks)
    assert len(blocks) > 3


def test_fit_blocks_refuses_a_block_that_cannot_be_made_to_fit() -> None:
    with pytest.raises(ValueError, match="cannot be made to fit"):
        fit_blocks("x", lambda piece, index, total: "y" * 5000, 1024)


def test_pack_fills_parts_in_order_and_may_leave_the_first_empty() -> None:
    blocks = ["a" * 400, "b" * 400, "c" * 400, "d" * 400]
    assert pack(blocks, 1000, 1000) == [blocks[:2], blocks[2:]]
    parts = pack(blocks, 100, 1000)
    assert parts[0] == [] and parts[1:] == [blocks[:2], blocks[2:]]
    assert pack([], 100, 100) == [[]]


def test_pack_keeps_every_block_exactly_once_in_order() -> None:
    rng = random.Random(3)
    blocks = ["x" * rng.randint(10, 900) for _ in range(300)]
    parts = pack(blocks, 3000, 4000)
    assert [block for part in parts for block in part] == blocks
    assert all(sum(utf8_size(block) + 1 for block in part) <= 4000 for part in parts)


def test_paginate_rows_lands_every_row_whole() -> None:
    rows = [f"- row {number:04d} " + "z" * 50 for number in range(500)]
    pages = paginate_rows([], rows, 2000, 2500)
    assert [row for page in pages for row in page] == rows
    assert len(pages) > 5
    assert all(sum(utf8_size(row) + 1 for row in page) <= 2500 for page in pages)
