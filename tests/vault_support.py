"""Synthetic libraries for the vault profile tests (no real data, tmp paths only)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.library_store import Library

REPEATED = (
    "This is a shared paragraph explaining a recurring research idea, with enough precise "
    "original wording to remain a meaningful passage across independent records and source "
    "observations."
)
FLAVOURS = (
    ("chatgpt", "ai", "conversations", "conversation"),
    ("claude", "ai", "conversations", "conversation"),
    ("documents", "authored", "research", "document"),
)


def sentinel_lines(tag: str, lines: int) -> str:
    """Numbered lines that stay unique and in order wherever a note is cut."""
    return "\n".join(
        f"{tag} line {number:05d} lorem ipsum dolor sit amet" for number in range(lines)
    )


def synthetic_library(
    tmp_path: Path,
    count: int,
    *,
    lines: Callable[[int], int] = lambda number: 4,
    name: str = "library.sqlite",
) -> Library:
    """``count`` records over three providers and both wings; later numbers are newer."""
    library = Library.create(tmp_path / name)
    for number in range(count):
        provider, wing, lane, kind = FLAVOURS[number % len(FLAVOURS)]
        tag = f"R{number:05d}"
        library.put(
            Conversation(
                provider=provider,
                source_id=f"rec{number:05d}",
                title=f"Record {number:05d} about lattices",
                kind=kind,
                created="2026-01-01T00:00:00Z",
                updated=f"2026-01-01T00:{number // 60:02d}:{number % 60:02d}Z",
                messages=[
                    Message(
                        role="user",
                        text=REPEATED + "\n\n" + sentinel_lines(tag, lines(number)),
                        id=f"{tag}m0",
                    ),
                    Message(role="assistant", text=f"Answer to {tag}.", id=f"{tag}m1"),
                ],
                extra={"wing": wing, "lane": lane},
            )
        )
    library.index_blocks()
    return library
