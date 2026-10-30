"""The author partition: who wrote a turn.

Every message of the normalized layer is one *turn*, and every turn has exactly one author:

* `human`: the person typed it. This is the channel `typed` on the role `user`, the same rule the
  prompt feed uses for an ask, so the human count of a provider equals the feed's rows plus its
  alternate-branch rows plus the asks the feed excluded for kind (notebooks and memories have no
  typed channel, so none).
* `machine`: everything else. Replies, reasoning, tool output, system text, project documents
  and memories. A project document or a memory may well have been written by the person, but the
  export cannot say so, and an author is only claimed where the export proves it.

The two values name the two containers of the turn store (`authored` and `machine`). The rule is a
pure function of the stored `role` and `channel`, so it never changes a conversation row: an
archive written before this module existed derives the same author for every old message.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Any

HUMAN = "human"
MACHINE = "machine"
AUTHORS: tuple[str, ...] = (HUMAN, MACHINE)

# The container (a top-level partition of the turn store) each author lives in.
CONTAINER_BY_AUTHOR: dict[str, str] = {HUMAN: "authored", MACHINE: "machine"}
AUTHOR_BY_CONTAINER: dict[str, str] = {name: author for author, name in CONTAINER_BY_AUTHOR.items()}

TYPED_CHANNEL = "typed"
USER_ROLE = "user"


def author_of(role: str, channel: str) -> str:
    """The author of a turn from its role and channel."""
    return HUMAN if role == USER_ROLE and channel == TYPED_CHANNEL else MACHINE


def container_of(author: str) -> str:
    """The container name for an author; an unknown author is an error, never a guess."""
    try:
        return CONTAINER_BY_AUTHOR[author]
    except KeyError as exc:
        raise ValueError(f"unknown author {author!r}; expected one of {AUTHORS}") from exc


def count_authors(messages: Iterable[Any]) -> Counter[str]:
    """Turns per author over objects that carry `role` and `channel`."""
    counts: Counter[str] = Counter()
    for message in messages:
        counts[author_of(message.role, message.channel)] += 1
    return counts


def empty_counts() -> dict[str, int]:
    return {author: 0 for author in AUTHORS}


def reconcile(authors: dict[str, int], messages: int) -> list[str]:
    """Problems with an author count against the message identity; empty means it reconciles.

    The identity is `human + machine == messages`: every turn has exactly one author.
    """
    problems = [f"unknown author key {key!r}" for key in authors if key not in AUTHORS]
    total = sum(authors.get(author, 0) for author in AUTHORS)
    if total != messages:
        problems.append(
            f"authors do not reconcile: human {authors.get(HUMAN, 0)} + "
            f"machine {authors.get(MACHINE, 0)} = {total}, messages {messages}"
        )
    return problems
