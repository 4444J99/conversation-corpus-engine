"""Lossless splitting and packing: how a human-facing file stays under its cap without dropping text.

Before this module the vault cut a note off at 400,000 characters and a message at 60,000, so
text the person wrote or read was silently missing from the file they opened, and the site had no
cap at all, so one thread became a 42 MB page. Nothing here truncates. A message that is too large
for one file is split at a line boundary (inside a fenced code block the fence is closed at the end
of the piece and reopened at the start of the next, so each piece renders on its own); a single
line longer than the cap is cut on a character boundary; and the pieces are packed into numbered
parts. `rejoin` puts the original text back byte for byte, which is the proof tests lean on.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .archive_text import FenceTracker

MIN_CHUNK_BYTES = 512
# Room kept inside a chunk for the synthetic lines that close and reopen a fence.
FENCE_SLACK = 256
MAX_FENCE_MARKER = 100
# A first guess at how much of a block budget a piece of raw text may use; the rest is left for
# the label, the quoting and the escaping a renderer adds. `fit_blocks` halves it until it fits.
FIRST_GUESS = 0.7


def utf8_size(text: str) -> int:
    return len(text.encode("utf-8"))


@dataclass(frozen=True)
class Chunk:
    """One piece of a split text.

    `body` is a verbatim slice. `prefix` and `suffix` are the synthetic fence lines added at a
    cut inside a fenced code block, and `continues_line` says the piece starts in the middle of
    the previous piece's last line (a line too long for one piece).
    """

    body: str
    prefix: str = ""
    suffix: str = ""
    continues_line: bool = False

    def text(self) -> str:
        out = self.body
        if self.prefix:
            out = f"{self.prefix}\n{out}"
        if self.suffix:
            out = f"{out}\n{self.suffix}"
        return out


def rejoin(chunks: Sequence[Chunk]) -> str:
    """The original text of a split, without the synthetic fence lines."""
    out: list[str] = []
    for index, chunk in enumerate(chunks):
        if index and not chunk.continues_line:
            out.append("\n")
        out.append(chunk.body)
    return "".join(out)


def _split_long_line(line: str, limit: int) -> list[str]:
    """Cut one line into pieces of at most `limit` bytes, only ever on a character boundary."""
    if limit < 4:
        raise ValueError("a piece must hold at least one four-byte character")
    data = line.encode("utf-8")
    pieces: list[str] = []
    start = 0
    while start < len(data):
        end = min(start + limit, len(data))
        while end < len(data) and (data[end] & 0xC0) == 0x80:
            end -= 1
        pieces.append(data[start:end].decode("utf-8"))
        start = end
    return pieces


def split_text(text: str, max_bytes: int) -> list[Chunk]:
    """Split `text` into chunks whose rendered text is at most `max_bytes` UTF-8 bytes each."""
    if max_bytes < MIN_CHUNK_BYTES:
        raise ValueError(f"max_bytes must be at least {MIN_CHUNK_BYTES}, got {max_bytes}")
    if utf8_size(text) <= max_bytes:
        return [Chunk(text)]
    budget = max_bytes - FENCE_SLACK
    chunks: list[Chunk] = []
    current: list[str] = []
    used = 0
    prefix = ""
    continues = False
    tracker = FenceTracker()

    def cut(mid_line: bool) -> None:
        nonlocal current, used, prefix, continues
        opener = tracker.opener if 0 < len(tracker.opener) <= MAX_FENCE_MARKER else ""
        chunks.append(
            Chunk("\n".join(current), prefix=prefix, suffix=opener, continues_line=continues)
        )
        prefix = opener
        continues = mid_line
        current = []
        used = 0

    for line in text.split("\n"):
        pieces = [line] if utf8_size(line) + 1 <= budget else _split_long_line(line, budget - 1)
        for index, piece in enumerate(pieces):
            piece_size = utf8_size(piece) + 1
            if current and used + piece_size > budget:
                cut(mid_line=index > 0)
            current.append(piece)
            used += piece_size
            if index == 0:
                tracker.feed(line)
    if current or not chunks:
        chunks.append(Chunk("\n".join(current), prefix=prefix, continues_line=continues))
    return chunks


def fit_blocks(
    text: str, render: Callable[[str, int, int], str], max_block_bytes: int
) -> list[str]:
    """Render `text` as blocks that each fit `max_block_bytes`, however much `render` expands it.

    `render(piece, index, total)` turns one piece into a block. The text is first split at a
    share of the budget; if any rendered block is still too large, the piece size is halved and
    the text split again, down to `MIN_CHUNK_BYTES`.
    """
    size = max(MIN_CHUNK_BYTES, int(max_block_bytes * FIRST_GUESS))
    while True:
        chunks = split_text(text, size)
        blocks = [render(chunk.text(), index, len(chunks)) for index, chunk in enumerate(chunks)]
        if all(utf8_size(block) <= max_block_bytes for block in blocks):
            return blocks
        if size <= MIN_CHUNK_BYTES:
            raise ValueError(
                f"a block cannot be made to fit {max_block_bytes} bytes (smallest piece "
                f"{MIN_CHUNK_BYTES} bytes still renders larger)"
            )
        size = max(MIN_CHUNK_BYTES, size // 2)


def pack(blocks: Sequence[str], first_budget: int, budget: int) -> list[list[str]]:
    """Group blocks, in order, into parts of at most `budget` bytes (`first_budget` for part one).

    Part one may be left empty when its budget cannot hold the first block (a long header); every
    later part holds at least one block. A block is never split here: `fit_blocks` guarantees each
    one fits `budget`.
    """
    parts: list[list[str]] = [[]]
    used = 0
    limit = first_budget
    for block in blocks:
        size = utf8_size(block) + 1
        if used + size > limit and (parts[-1] or len(parts) == 1):
            parts.append([])
            used = 0
            limit = budget
        parts[-1].append(block)
        used += size
    return parts


def paginate_rows(
    head: Sequence[str], rows: Sequence[str], first_budget: int, budget: int
) -> list[list[str]]:
    """Group list rows into pages: the first page opens with `head`, every row lands whole."""
    head_size = sum(utf8_size(line) + 1 for line in head)
    pages: list[list[str]] = [[]]
    used = head_size
    limit = first_budget
    for row in rows:
        size = utf8_size(row) + 1
        if used + size > limit and pages[-1]:
            pages.append([])
            used = 0
            limit = budget
        pages[-1].append(row)
        used += size
    return pages
