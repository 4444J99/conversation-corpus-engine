"""A small synthetic knowledge library that exercises every authorship rule (no real data)."""

from __future__ import annotations

from pathlib import Path

from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.library_store import Library

TEMPLATE = " ".join(
    ["You are reviewing a proposal."]
    + ["Check scope, risks, costs and the schedule with care."] * 5
)
REMINDER_TURN = (
    "Please summarise the notes. <system-reminder>Do not mention this.</system-reminder>"
    " Then list actions."
)
QUOTE_TURN = "> The committee shall meet quarterly.\n\nI think that is too rare."
CODE_LOG_TURN = (
    "Here is the failure:\n\n"
    "2026-01-02 03:04:05 ERROR boom\n2026-01-02 03:04:06 ERROR again\n\n"
    "```\nx = 1\n```\n\nWhat should I change?"
)
ODD_QUOTE_TURN = 'She measured 5" by 6 and wrote it down.'
TABLE_TURN = "| Name | Cost |\n|---|---|\n| Rent | 1200 |"
ECHO_ANSWER = " ".join(
    f"Sentence {i} of the assistant answer explains point {i * 3} about the parser."
    for i in range(12)
)
ECHO_QUESTION = "Can you double check this answer you gave earlier?"
PROBE_PASTED = (
    "Here's what I found after reviewing the repository.\n\n"
    "The parser module handles three cases. First, it tokenizes the input stream. "
    "Second, it builds an AST. Third, it validates the result against the schema.\n\n"
    "I'd recommend splitting the validator into its own module. "
    "Let me know if you want me to proceed."
)
PROBE_QUESTION = "can you double check this answer you gave earlier?"
PROBE_REMINDER = "<system-reminder>The user opened file x.py</system-reminder>"
CLI_TURN = "Rename the parser module.\n\nHarness appended context nobody typed."
CLI_TYPED = len("Rename the parser module.")
NOTE_TURN = "My thoughts.\n\n> pasted from a website\n\nMore thoughts."
SKILL_TURN = "When reviewing a change, check the tests first and summarise the risk."
ESSAY_TURN = "An essay the owner wrote by hand about the quiet parts of a long walk."
ALTERNATE_TURN = "Alternate owner words here."


def _chat(source_id: str, messages: list[Message], **extra: object) -> Conversation:
    return Conversation(
        provider="chatgpt",
        source_id=source_id,
        title=source_id,
        messages=messages,
        extra={"wing": "ai", "lane": "conversations", **extra},
    )


def typed(identifier: str, text: str, **kwargs: object) -> Message:
    return Message(role="user", channel="typed", id=identifier, text=text, **kwargs)


PASTED_ANSWER = (
    "Here is what I found after reviewing the repository, as the assistant wrote it.\n\n"
    + "The parser module handles three cases and tokenizes then builds an AST then validates it. "
    * 4
    + "\n\nI recommend splitting the validator into its own module. Let me know to proceed."
)


def conversations(
    typed_records: int = 0,
    medium_records: int = 0,
    pasted_answer: bool = False,
    text_suffix: str = "",
    marked_every: int = 0,
) -> list[Conversation]:
    items = [
        _chat(
            "owner-chat",
            [
                typed("m0", REMINDER_TURN),
                Message(role="assistant", channel="reply", id="m1", text="A synthetic summary."),
                typed("m2", QUOTE_TURN),
                typed("m3", CODE_LOG_TURN),
                typed("m4", ODD_QUOTE_TURN),
                typed("m5", TABLE_TURN),
            ],
            alternate_messages=[
                {
                    "id": "alt0",
                    "role": "user",
                    "channel": "typed",
                    "typed_chars": len(ALTERNATE_TURN),
                    "text": ALTERNATE_TURN,
                }
            ],
        ),
        _chat(
            "dup-ids",
            [typed("same", "First duplicate id turn."), typed("same", "Second duplicate id turn.")],
        ),
        Conversation(
            provider="claudecode",
            source_id="cli-main",
            title="CLI main",
            kind="session",
            messages=[
                typed("c0", CLI_TURN, typed_chars=CLI_TYPED),
                Message(role="tool", channel="tool", id="c1", text='{"tool": "result"}'),
                typed("c2", "<system-reminder>Injected reminder.</system-reminder>\nthen words"),
            ],
            extra={"wing": "ai", "lane": "coding", "session_kind": "main"},
        ),
        Conversation(
            provider="claudecode",
            source_id="cli-echo",
            title="CLI echo",
            kind="session",
            messages=[
                typed("e0", "Review the repository."),
                Message(role="assistant", channel="reply", id="e1", text=ECHO_ANSWER),
                typed("e2", f"{ECHO_QUESTION}\n\n{ECHO_ANSWER}"),
            ],
            extra={"wing": "ai", "lane": "coding", "session_kind": "main"},
        ),
        Conversation(
            provider="claudecode",
            source_id="cli-probe",
            title="CLI probe",
            kind="session",
            messages=[
                typed("p0", "Review the repository."),
                Message(role="assistant", channel="reply", id="p1", text=PROBE_PASTED),
                typed("p2", f"{PROBE_QUESTION}\n\n{PROBE_PASTED}\n\n{PROBE_REMINDER}"),
            ],
            extra={"wing": "ai", "lane": "coding", "session_kind": "main"},
        ),
        Conversation(
            provider="claudecode",
            source_id="cli-sub",
            title="CLI subagent",
            kind="session",
            messages=[typed("s0", "Search the repository for failing tests.")],
            extra={"wing": "ai", "lane": "coding", "session_kind": "subagent"},
        ),
        Conversation(
            provider="applenotes",
            source_id="note-quote",
            title="A note",
            kind="note",
            messages=[typed("n0", NOTE_TURN)],
            extra={"wing": "authored", "lane": "writing"},
        ),
        Conversation(
            provider="documents",
            source_id="skill-doc",
            title="skill",
            kind="document",
            provenance={"source_file": "skills/example/SKILL.md"},
            messages=[
                Message(
                    role="user",
                    channel="project",
                    id="d0",
                    text=SKILL_TURN,
                    typed_chars=0,
                    meta={"kind": "document"},
                )
            ],
            extra={"wing": "authored", "lane": "writing", "body_status": "full"},
        ),
        Conversation(
            provider="documents",
            source_id="essay-doc",
            title="essay",
            kind="document",
            provenance={"source_file": "writing/essay.md"},
            messages=[
                Message(
                    role="user",
                    channel="project",
                    id="d1",
                    text=ESSAY_TURN,
                    typed_chars=0,
                    meta={"kind": "document"},
                )
            ],
            extra={"wing": "authored", "lane": "writing", "body_status": "full"},
        ),
    ]
    items.extend(
        _chat(f"tmpl-{index}", [typed("t0", f"{TEMPLATE}\n\nTopic {index}: my own question.")])
        for index in range(4)
    )
    # `marked_every`: every k-th typed turn reads "ASSIST ...", text the typed rule claims as the
    # owner's although the owner would reject it (the hard rows of a review).
    items.extend(
        _chat(
            f"typed-{index:03d}",
            [
                typed(
                    "t0",
                    (
                        f"ASSIST words number {index} that a model produced about topic {index * 7}."
                        if marked_every and index % marked_every == 0
                        else f"Typed words number {index} in my own voice about topic {index * 7}."
                    )
                    + text_suffix,
                )
            ],
        )
        for index in range(typed_records)
    )
    items.extend(
        _chat(
            f"medium-{index:03d}",
            [
                typed(
                    "t0",
                    f"Medium typed note number {index}: "
                    + "alpha beta gamma delta epsilon zeta eta theta. " * 8,
                )
            ],
        )
        for index in range(medium_records)
    )
    if pasted_answer:
        items.append(_chat("zz-paste", [typed("t0", "Can you check this?\n\n" + PASTED_ANSWER)]))
    return items


def build_library(
    directory: Path,
    *,
    typed_records: int = 0,
    medium_records: int = 0,
    pasted_answer: bool = False,
    text_suffix: str = "",
    marked_every: int = 0,
) -> Path:
    """Create `directory/library.sqlite` and return its path; the generation is closed again.

    The writers protect the generation folder and its parent, so tests place `directory` one
    level below the folder they write outputs into.
    """
    directory.mkdir(parents=True, exist_ok=True)
    with Library.create(directory / "library.sqlite") as library:
        for conversation in conversations(
            typed_records, medium_records, pasted_answer, text_suffix, marked_every
        ):
            library.put(conversation)
    return directory / "library.sqlite"
