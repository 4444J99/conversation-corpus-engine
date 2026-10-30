"""The author partition: who wrote a turn.

Every message of the normalized layer is one *turn*, and every turn has exactly one author:

* `human`: the person typed or wrote it. That is the role `user` on the channel `typed` (a
  prompt typed into a chat or a coding CLI, the same rule the prompt feed uses for an ask, so the
  human count of a provider equals the feed's rows, less the rows the feed took from abandoned
  branches, plus the typed turns the feed skipped for holding no text) or on the channel
  `written` (text the person authored or supplied outside a chat: a note, a Drive document, a
  file they wrote, a project instruction or a knowledge document they uploaded).
* `machine`: everything else. Replies, reasoning, tool output, system text, memories and plans an
  agent wrote, text a harness injected into a user message, and every prompt a parent agent wrote
  for a subagent.

An author is claimed where the source proves it. Where a source cannot prove who wrote a class
of text, the reader classifies by provenance (who put the text into the system) and records the
basis in the turn's `meta.author_basis`, so the doubt stays visible and countable.

The two values name the two containers of the turn store (`authored` and `machine`). The rule is a
pure function of the stored `role` and `channel`, so it never changes a conversation row: an
archive written before the `written` channel existed derives the same author for every old
message.
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
WRITTEN_CHANNEL = "written"
USER_ROLE = "user"
# The channels on which a user turn is the person's own words.
HUMAN_CHANNELS: frozenset[str] = frozenset({TYPED_CHANNEL, WRITTEN_CHANNEL})


def author_of(role: str, channel: str) -> str:
    """The author of a turn from its role and channel."""
    return HUMAN if role == USER_ROLE and channel in HUMAN_CHANNELS else MACHINE


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
