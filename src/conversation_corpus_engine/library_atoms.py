"""Atoms: the library's passages as small, linked, citable units (a Zettelkasten view).

An atom is one deduplicated eligible prose block of the library's block index: a passage of at
least 120 characters that the library stores once however many records repeat it. Its identity is
the SHA-256 digest of the canonical text, so an atom is a stable address that survives rebuilds
for as long as its words do. Every occurrence of an atom names the record, message and character
span it came from, so an atom always leads back to its sources, and two atoms in one record are
neighbours by their position there.

This module only reads the library (through the connection `Library` already owns) and returns
plain data. It holds no state, writes nothing and never interprets a passage as markup. The
local wiki (`cce library serve`) renders it as `/record/UID/atoms`, `/atom/DIGEST` and
`/api/atom/DIGEST`.
"""

from __future__ import annotations

import re
import time
from typing import Any

from .library_store import Library

DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
# An atom card shows this much of the passage; the atom page shows up to ATOM_TEXT_LIMIT.
CARD_CHARS = 420
ATOM_TEXT_LIMIT = 24000
ATOMS_PER_PAGE = 25
MAX_ATOMS_PER_PAGE = 100
# Occurrence counts stop here, so a passage repeated in thousands of records is never counted
# in full: the page says "50+" and stays fast.
SHARED_COUNT_CAP = 50
OCCURRENCES_PER_PAGE = 50
PROGRESS_INSTRUCTIONS = 10000


class _Expired(Exception):
    pass


def valid_digest(value: str) -> bool:
    return bool(DIGEST_RE.fullmatch(value))


def _clip(text: str, limit: int) -> tuple[str, bool]:
    return (text[:limit], True) if len(text) > limit else (text, False)


def _shared_records(library: Library, digest: str, uid: str) -> tuple[int, bool]:
    """How many other records hold this atom, counted up to ``SHARED_COUNT_CAP``."""
    rows = library.connection.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT uid FROM occurrences WHERE digest=? AND uid<>? "
        "LIMIT ?)",
        (digest, uid, SHARED_COUNT_CAP + 1),
    ).fetchone()[0]
    return (SHARED_COUNT_CAP, False) if rows > SHARED_COUNT_CAP else (rows, True)


def record_atoms(
    library: Library,
    uid: str,
    *,
    offset: int = 0,
    limit: int = ATOMS_PER_PAGE,
    max_seconds: float | None = None,
) -> dict[str, Any]:
    """One page of the atoms of a record in reading order, each with a bounded preview.

    ``position`` counts the record's atoms from 0, so a card keeps its place across pages. A
    record the block index has not seen has ``total`` 0 (the index is built with the library).
    With ``max_seconds`` the per-atom sharing counts are abandoned when time runs out
    (``shared`` is then null for the atoms after the cut); the atoms themselves are never cut.
    """
    if not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a nonnegative integer")
    if not isinstance(limit, int) or not 1 <= limit <= MAX_ATOMS_PER_PAGE:
        raise ValueError(f"limit must be an integer from 1 to {MAX_ATOMS_PER_PAGE}")
    connection = library.connection
    total = connection.execute("SELECT COUNT(*) FROM occurrences WHERE uid=?", (uid,)).fetchone()[0]
    rows = connection.execute(
        "SELECT o.message_index, o.block_index, o.digest, o.start, o.end, b.chars "
        "FROM occurrences o JOIN blocks b ON b.digest=o.digest WHERE o.uid=? "
        "ORDER BY o.message_index, o.block_index LIMIT ? OFFSET ?",
        (uid, limit + 1, offset),
    ).fetchall()
    deadline = None if max_seconds is None else time.monotonic() + max_seconds
    atoms = []
    for position, row in enumerate(rows[:limit], start=offset):
        text, cut = _clip(library.block_text(row["digest"]), CARD_CHARS)
        shared: int | None
        shared_exact = True
        if deadline is not None and time.monotonic() >= deadline:
            shared, shared_exact = None, False  # unknown, not an exact count
        else:
            shared, shared_exact = _shared_records(library, row["digest"], uid)
        atoms.append(
            {
                "position": position,
                "digest": row["digest"],
                "message": row["message_index"],
                "block": row["block_index"],
                "start": row["start"],
                "end": row["end"],
                "chars": row["chars"],
                "preview": text,
                "preview_truncated": cut,
                "shared_records": shared,
                "shared_exact": shared_exact,
            }
        )
    return {
        "uid": uid,
        "total": total,
        "offset": offset,
        "limit": limit,
        "next_offset": offset + limit if len(rows) > limit else None,
        "atoms": atoms,
    }


def _neighbour(
    library: Library, uid: str, message: int, block: int, *, after: bool
) -> dict[str, Any] | None:
    comparison, order = (">", "ASC") if after else ("<", "DESC")
    row = library.connection.execute(
        "SELECT message_index, block_index, digest FROM occurrences WHERE uid=? AND "
        f"(message_index,block_index){comparison}(?,?) "
        f"ORDER BY message_index {order}, block_index {order} LIMIT 1",
        (uid, message, block),
    ).fetchone()
    if row is None:
        return None
    text, cut = _clip(library.block_text(row["digest"]), CARD_CHARS)
    return {
        "digest": row["digest"],
        "message": row["message_index"],
        "block": row["block_index"],
        "preview": text,
        "preview_truncated": cut,
    }


def atom(
    library: Library,
    digest: str,
    *,
    uid: str | None = None,
    message: int | None = None,
    block: int | None = None,
    occurrences_offset: int = 0,
    occurrences_limit: int = OCCURRENCES_PER_PAGE,
) -> dict[str, Any]:
    """One atom: its text, every place it occurs, and its neighbours in a chosen occurrence.

    ``uid``, ``message`` and ``block`` together pick the occurrence whose record supplies the
    previous and next atoms; without them the first occurrence is used. The text is clipped to
    ``ATOM_TEXT_LIMIT`` characters and says so. Raises ``KeyError`` for an unknown digest.
    """
    if not valid_digest(digest):
        raise ValueError("an atom digest is 64 lowercase hexadecimal characters")
    if (
        not isinstance(occurrences_offset, int)
        or occurrences_offset < 0
        or not isinstance(occurrences_limit, int)
        or not 1 <= occurrences_limit <= MAX_ATOMS_PER_PAGE
    ):
        raise ValueError("occurrence paging is out of range")
    connection = library.connection
    row = connection.execute("SELECT chars FROM blocks WHERE digest=?", (digest,)).fetchone()
    if row is None:
        raise KeyError(digest)
    text, cut = _clip(library.block_text(digest), ATOM_TEXT_LIMIT)
    found = connection.execute(
        "SELECT o.uid, r.title, r.provider, r.lane, o.message_index, o.block_index, o.start, o.end "
        "FROM occurrences o JOIN records r ON r.uid=o.uid WHERE o.digest=? "
        "ORDER BY o.uid, o.message_index, o.block_index LIMIT ? OFFSET ?",
        (digest, occurrences_limit + 1, occurrences_offset),
    ).fetchall()
    shown = [dict(item) for item in found[:occurrences_limit]]
    for item in shown:
        item["message"] = item.pop("message_index")
        item["block"] = item.pop("block_index")
    count = connection.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM occurrences WHERE digest=? LIMIT ?)",
        (digest, 100000 + 1),
    ).fetchone()[0]
    selected = None
    if uid is not None and message is not None and block is not None:
        selected = connection.execute(
            "SELECT uid, message_index, block_index FROM occurrences WHERE uid=? AND "
            "message_index=? AND block_index=? AND digest=?",
            (uid, message, block, digest),
        ).fetchone()
        if selected is None:
            raise ValueError("that occurrence does not hold this atom")
    elif shown:
        first = connection.execute(
            "SELECT uid, message_index, block_index FROM occurrences WHERE digest=? "
            "ORDER BY uid, message_index, block_index LIMIT 1",
            (digest,),
        ).fetchone()
        selected = first
    context = None
    if selected is not None:
        context = {
            "uid": selected["uid"],
            "message": selected["message_index"],
            "block": selected["block_index"],
            "previous": _neighbour(
                library,
                selected["uid"],
                selected["message_index"],
                selected["block_index"],
                after=False,
            ),
            "next": _neighbour(
                library,
                selected["uid"],
                selected["message_index"],
                selected["block_index"],
                after=True,
            ),
        }
    return {
        "digest": digest,
        "chars": row["chars"],
        "text": text,
        "text_truncated": cut,
        "occurrence_total": min(count, 100000),
        "occurrence_total_exact": count <= 100000,
        "occurrences_offset": occurrences_offset,
        "occurrences": shown,
        "next_occurrences_offset": (
            occurrences_offset + occurrences_limit if len(found) > occurrences_limit else None
        ),
        "context": context,
    }
