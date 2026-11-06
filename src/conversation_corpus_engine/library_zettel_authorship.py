"""Who wrote each quoted word of a Zettel note, derived by the authorship engine.

A note must not need the owner to type an identity. For every quoted span this module re-runs
the deterministic rules of `attribution_rules` on the stored message with the evidence the library
holds about it (role, channel, typed-character count, record family, wrappers) and reads the class
of every character of the quoted range. It follows the same path as `cce library attribute`:
the repeated-text census over the whole library, and the assistant-echo check over the earlier
turns of the same conversation. The library keeps no stored annotations (`attribute apply` writes
a file), so there is nothing to read back and the derivation always runs live.

A quote is `owner` only when every visible character of its range is classed owner. Otherwise it
takes the weakest class present (`third_party` or `agent`, then `unknown`; owner is the strongest)
and the classes and rules that decided it are recorded. Blank characters (spaces, line breaks,
zero-width marks) that no rule claims belong to nobody and neither grant nor remove credit.

Everything the engine produces has `profile_eligible=False` until its precision gate is met, so a
derived owner class is rules-derived, not owner-reviewed. Nothing here reads the owner's identity.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .attribution_rules import (
    AGENT,
    DEFAULT_REPEAT_MIN_CHARS,
    DEFAULT_REPEAT_MIN_OCCURRENCES,
    OWNER,
    THIRD_PARTY,
    UNATTRIBUTED,
    UNKNOWN,
    AssistantEcho,
    AttributedSpan,
    attribute_message,
    blank_text,
)
from .library_store import Library

if TYPE_CHECKING:
    from .attribution_commands import ScannedMessage

# `attribution_commands` reaches `library_commands`, which imports the note emitter, so the scan
# helpers are imported where they are used.
DEFAULT_DEADLINE_SECONDS = 900.0

# Weakest first. `agent` and `third_party` are equally weak; when both are present the quote is
# recorded as `third_party` (the class that is neither the owner's nor a machine's).
_WEAKNESS = (THIRD_PARTY, AGENT, UNKNOWN, OWNER)

Key = tuple[str, str, int]


@dataclass(frozen=True)
class QuoteAuthorship:
    """The derived author class of one quoted range, with what decided it."""

    author: str
    classes: tuple[str, ...]
    rules: tuple[str, ...]
    reason: str = ""


def weakest(classes: Sequence[str]) -> str:
    """The weakest class present: third_party, then agent, then unknown, then owner."""
    for name in _WEAKNESS:
        if name in classes:
            return name
    return UNKNOWN


def _unknown(reason: str) -> QuoteAuthorship:
    return QuoteAuthorship(UNKNOWN, (UNKNOWN,), (), reason)


def classify_range(
    text: str, spans: Sequence[AttributedSpan], start: int, end: int
) -> QuoteAuthorship:
    """The class of every character of `text[start:end]` under a partition of the message."""
    classes: set[str] = set()
    rules: set[str] = set()
    for span in spans:
        low, high = max(span.start, start), min(span.end, end)
        if low >= high:
            continue
        if span.rule == UNATTRIBUTED and blank_text(text[low:high]):
            continue
        classes.add(span.author)
        rules.add(span.rule)
    if not classes:
        return _unknown("no visible character of the range was attributed")
    return QuoteAuthorship(weakest(sorted(classes)), tuple(sorted(classes)), tuple(sorted(rules)))


def _key(reference: Mapping[str, Any]) -> Key:
    return (reference["uid"], reference["sequence"], reference["message"])


def derive_quote_authors(
    library: Library,
    references: Sequence[Mapping[str, Any]],
    *,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
) -> list[QuoteAuthorship]:
    """The derived author class of each validated source reference, in order.

    A reference to a revision that is no longer the record's current one, or whose scan did not
    finish inside the deadline, is `unknown`: the census and the assistant echo are defined over
    current revisions, and nothing is credited that was not derived.
    """
    results: dict[int, QuoteAuthorship] = {}
    wanted: dict[Key, list[int]] = {}
    current = {row["uid"]: row["current_version"] for row in library.rows()}
    for number, reference in enumerate(references):
        if current.get(reference["uid"]) != reference["version"]:
            results[number] = _unknown("the cited revision is not the current one")
            continue
        wanted.setdefault(_key(reference), []).append(number)
    if wanted:
        spans = _spans_of(library, set(wanted), deadline_seconds)
        for key, numbers in wanted.items():
            found = spans.get(key)
            for number in numbers:
                reference = references[number]
                if found is None:
                    results[number] = _unknown("the message could not be attributed")
                else:
                    text, partition = found
                    results[number] = classify_range(
                        text, partition, reference["start"], reference["end"]
                    )
    return [results[number] for number in range(len(references))]


def _spans_of(
    library: Library, wanted: set[Key], deadline_seconds: float
) -> dict[Key, tuple[str, list[AttributedSpan]]]:
    """Attribute the wanted messages exactly as `cce library attribute` would, no others."""
    from .attribution_commands import (  # noqa: PLC0415 - import cycle through library_commands
        USER_RUN_CHARACTERS,
        Deadline,
        DeadlineReached,
        ScanStats,
        build_census,
        iter_messages,
    )

    deadline = Deadline(deadline_seconds)
    found: dict[Key, tuple[str, list[AttributedSpan]]] = {}
    uids = {key[0] for key in wanted}
    try:
        census = build_census(
            library,
            limit_records=None,
            min_chars=DEFAULT_REPEAT_MIN_CHARS,
            min_occurrences=DEFAULT_REPEAT_MIN_OCCURRENCES,
            deadline=deadline,
        )
        pending: list[tuple[ScannedMessage, tuple[tuple[int, int], ...]]] = []
        waiting = 0
        echo, echo_key = AssistantEcho(), None
        left = set(uids)

        def attribute(
            message: ScannedMessage, later: tuple[tuple[int, int], ...], pasted: Any
        ) -> None:
            key = (message.uid, message.sequence, message.index)
            if key in wanted:
                found[key] = (
                    message.text,
                    attribute_message(
                        message.text,
                        role=message.role,
                        channel=message.channel,
                        typed_chars=message.typed_chars,
                        meta=message.meta,
                        record=message.record,
                        boilerplate=later,
                        pasted_back=pasted,
                    ),
                )

        def release() -> None:
            nonlocal waiting
            if pending:
                turns = echo.pasted_back_turns([item.text for item, _ in pending])
                for (item, later), pasted in zip(pending, turns, strict=True):
                    attribute(item, later, pasted)
                pending.clear()
            waiting = 0

        previous = None
        for message in iter_messages(library, ScanStats(), limit_records=None, deadline=deadline):
            if previous is not None and message.uid != previous:
                release()
                left.discard(previous)
                if not left:
                    break
            previous = message.uid
            if (message.uid, message.sequence) != echo_key:
                release()
                echo, echo_key = AssistantEcho(), (message.uid, message.sequence)
            if message.unreadable:
                release()
                continue
            # Every readable message advances the census in scan order, wanted or not.
            later = census.later_occurrences(message.text)
            if message.uid not in uids:
                continue
            role = message.role.strip().lower()
            if role == "user":
                pending.append((message, later))
                waiting += len(message.text)
                if waiting > USER_RUN_CHARACTERS:
                    release()
                continue
            release()
            if role in {"assistant", "model"}:
                echo.add_assistant(message.text)
            attribute(message, later, ())
        release()
    except DeadlineReached:
        return {}
    return found
