"""Who wrote each quoted word of a Zettel note, derived by the authorship engine.

A note must not need the owner to type an identity. For every quoted span this module re-runs
the deterministic rules of `attribution_rules` on the stored message with the evidence the library
holds about it (role, channel, typed-character count, record family, wrappers) and reads the class
of every character of the quoted range. By default the derivation is bounded and per conversation:
the per-message rules plus the assistant-echo check over the earlier assistant turns of the SAME
conversation, read once per export (one conversation is read by uid, within a message and a
character budget). The library-wide repeated-text census (the boilerplate rule, which only
re-labels repeated templates as third party) is NOT run by default: it needs a pass over the whole
library, so it is an explicit opt-in (`repeat_census=True`) with full-library cost. The library
keeps no stored annotations (`attribute apply` writes a file), so the derivation always runs live,
bounded by one wall-clock deadline for the whole export.

A quote is `owner` only when every visible character of its range is classed owner. Otherwise it
takes the weakest class present (`third_party` or `agent`, then `unknown`; owner is the strongest)
and the classes and rules that decided it are recorded. Blank characters (spaces, line breaks,
zero-width marks) that no rule claims belong to nobody and neither grant nor remove credit.

The derivation is bound to the exact stored message: the source-message digest the reviewed
reference already carries must still name the scanned text, the cited revision must be the
record's current one, a conversation or message over its budget is `unknown` with a reason, and a
derivation that misses the deadline derives nothing more (`unknown`).

Everything the engine produces has `profile_eligible=False` until its precision gate is met, so a
derived owner class is rules-derived, not owner-reviewed. Nothing here reads the owner's identity
or the owner's review ledger.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .attribution import text_digest
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
    record_info,
)
from .library_store import Library
from .origin_hints import OriginHints

if TYPE_CHECKING:
    from .attribution_commands import ScannedMessage

# `attribution_commands` reaches `library_commands`, which imports the note emitter, so the scan
# helpers are imported where they are used.
DEFAULT_DEADLINE_SECONDS = 900.0
# Per-conversation budgets of the default derivation (a conversation over either is `unknown`).
DEFAULT_MAX_CONVERSATION_CHARS = 2 * 1024 * 1024
DEFAULT_MAX_CONVERSATION_MESSAGES = 50_000
# A single quoted message over this many characters is not attributed (`unknown`).
DEFAULT_MAX_QUOTE_MESSAGE_CHARS = 1024 * 1024
_DEADLINE_CHECK_INTERVAL = 256

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
    repeat_census: bool = False,
    max_conversation_chars: int = DEFAULT_MAX_CONVERSATION_CHARS,
    max_conversation_messages: int = DEFAULT_MAX_CONVERSATION_MESSAGES,
    max_quote_message_chars: int = DEFAULT_MAX_QUOTE_MESSAGE_CHARS,
    origin_hints: OriginHints | None = None,
) -> list[QuoteAuthorship]:
    """The derived author class of each validated source reference, in order.

    By default each quoted conversation is read once (by uid) and attributed with the per-message
    rules plus the assistant-echo check over its own earlier assistant turns; no other
    conversation is read. `repeat_census=True` opts in to the library-wide repeated-text census
    (a pass over the whole library; the conversation budgets do not apply to it).

    A reference to a revision that is no longer the record's current one, whose message digest no
    longer names the scanned text, whose conversation or message is over its budget, or whose
    derivation did not finish inside the deadline (one for the whole call), is `unknown`: nothing
    is credited that was not derived.
    """
    if origin_hints is not None:
        origin_hints.refuse_findings()
    if isinstance(deadline_seconds, bool) or not (
        isinstance(deadline_seconds, int | float) and deadline_seconds > 0
    ):
        raise ValueError("deadline_seconds must be a positive number")
    for name, value in (
        ("max_conversation_chars", max_conversation_chars),
        ("max_conversation_messages", max_conversation_messages),
        ("max_quote_message_chars", max_quote_message_chars),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    results: dict[int, QuoteAuthorship] = {}
    wanted: dict[Key, list[int]] = {}
    current: dict[str, str | None] = {}
    for number, reference in enumerate(references):
        uid = reference["uid"]
        if uid not in current:
            row = library.connection.execute(
                "SELECT current_version FROM records WHERE uid=?", (uid,)
            ).fetchone()
            current[uid] = row[0] if row else None
        if current[uid] != reference["version"]:
            results[number] = _unknown("the cited revision is not the current one")
            continue
        wanted.setdefault(_key(reference), []).append(number)
    if wanted:
        if repeat_census:
            spans: dict[Key, tuple[str, list[AttributedSpan]] | str] = dict(
                _spans_of(library, set(wanted), deadline_seconds, origin_hints)
            )
        else:
            spans = _conversation_spans(
                library,
                set(wanted),
                deadline_seconds,
                max_conversation_chars,
                max_conversation_messages,
                max_quote_message_chars,
                origin_hints,
            )
        for key, numbers in wanted.items():
            found = spans.get(key)
            for number in numbers:
                reference = references[number]
                if found is None:
                    results[number] = _unknown("the message could not be attributed in time")
                    continue
                if isinstance(found, str):
                    results[number] = _unknown(found)
                    continue
                text, partition = found
                if text_digest(text) != reference["text_digest"] or not 0 <= reference[
                    "start"
                ] < reference["end"] <= len(text):
                    results[number] = _unknown("the source message digest does not name the scan")
                    continue
                results[number] = classify_range(
                    text, partition, reference["start"], reference["end"]
                )
    return [results[number] for number in range(len(references))]


@dataclass(frozen=True)
class _Turn:
    index: int
    text: str
    role: str
    channel: str
    typed_chars: Any
    meta: Mapping[str, Any]


def _encodable(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _conversation_spans(
    library: Library,
    wanted: set[Key],
    deadline_seconds: float,
    max_chars: int,
    max_messages: int,
    max_quote_chars: int,
    origin_hints: OriginHints | None = None,
) -> dict[Key, tuple[str, list[AttributedSpan]] | str]:
    """Attribute the wanted messages from their own conversation only, one read per conversation.

    A string value is the reason a message was not attributed. A message the deadline cut off is
    absent. No library-wide census runs: the boilerplate rule is not applied.
    """
    from .attribution_commands import (  # noqa: PLC0415 - import cycle through library_commands
        USER_RUN_CHARACTERS,
        Deadline,
        DeadlineReached,
    )

    deadline = Deadline(deadline_seconds)
    found: dict[Key, tuple[str, list[AttributedSpan]] | str] = {}
    by_uid: dict[str, set[Key]] = {}
    for key in wanted:
        by_uid.setdefault(key[0], set()).add(key)
    try:
        for uid in sorted(by_uid):
            deadline.check()
            keys = by_uid[uid]
            payload = library.record(uid)
            extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
            sequences = {
                "main": payload.get("messages"),
                "alternate": extra.get("alternate_messages"),
            }
            texts = [
                item.get("text")
                for messages in sequences.values()
                if isinstance(messages, list)
                for item in messages
                if isinstance(item, dict)
            ]
            size = sum(len(text) for text in texts if isinstance(text, str))
            if size > max_chars or len(texts) > max_messages:
                reason = (
                    f"the conversation is over the per-conversation budget "
                    f"({max_chars} characters, {max_messages} messages)"
                )
                found.update(dict.fromkeys(keys, reason))
                continue
            info = record_info(payload, origin_hints)
            for sequence, messages in sequences.items():
                wanted_here = {key for key in keys if key[1] == sequence}
                if wanted_here and isinstance(messages, list):
                    _attribute_sequence(
                        messages,
                        uid,
                        sequence,
                        wanted_here,
                        info,
                        found,
                        deadline,
                        USER_RUN_CHARACTERS,
                        max_quote_chars,
                    )
    except DeadlineReached:
        # What finished stays derived; the rest is absent and reads as not attributed in time.
        return found
    return found


def _attribute_sequence(
    messages: list[Any],
    uid: str,
    sequence: str,
    wanted: set[Key],
    info: Any,
    found: dict[Key, tuple[str, list[AttributedSpan]] | str],
    deadline: Any,
    run_characters: int,
    max_quote_chars: int,
) -> None:
    pending: list[_Turn] = []
    waiting = 0
    echo = AssistantEcho()

    def attribute(turn: _Turn, pasted: Any) -> None:
        key = (uid, sequence, turn.index)
        if key not in wanted:
            return
        if len(turn.text) > max_quote_chars:
            found[key] = f"the quoted message is over {max_quote_chars} characters"
            return
        found[key] = (
            turn.text,
            attribute_message(
                turn.text,
                role=turn.role,
                channel=turn.channel,
                typed_chars=turn.typed_chars,
                meta=turn.meta,
                record=info,
                boilerplate=(),
                pasted_back=pasted,
            ),
        )

    def release() -> None:
        nonlocal waiting
        if pending:
            turns = echo.pasted_back_turns([item.text for item in pending])
            for item, pasted in zip(pending, turns, strict=True):
                attribute(item, pasted)
            pending.clear()
        waiting = 0

    counter = 0
    for index, item in enumerate(messages):
        text = item.get("text") if isinstance(item, dict) else None
        if not isinstance(text, str) or not text:
            continue
        counter += 1
        if counter % _DEADLINE_CHECK_INTERVAL == 0:
            deadline.check()
        if not text.isascii() and not _encodable(text):
            release()
            continue
        meta = item.get("meta")
        turn = _Turn(
            index=index,
            text=text,
            role=str(item.get("role") or ""),
            channel=str(item.get("channel") or ""),
            typed_chars=item.get("typed_chars"),
            meta=meta if isinstance(meta, dict) else {},
        )
        role = turn.role.strip().lower()
        if role == "user":
            pending.append(turn)
            waiting += len(text)
            if waiting > run_characters:
                release()
            continue
        release()
        if role in {"assistant", "model"}:
            echo.add_assistant(text)
        attribute(turn, ())
    release()


def _spans_of(
    library: Library,
    wanted: set[Key],
    deadline_seconds: float,
    origin_hints: OriginHints | None = None,
) -> dict[Key, tuple[str, list[AttributedSpan]]]:
    """Attribute the wanted messages exactly as `cce library attribute` would, no others.

    This is the opt-in `repeat_census=True` path: it runs the library-wide census (a full pass)."""
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
        for message in iter_messages(
            library, ScanStats(), limit_records=None, deadline=deadline, origin_hints=origin_hints
        ):
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
