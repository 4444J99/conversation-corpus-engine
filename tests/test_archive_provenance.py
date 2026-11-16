"""Who typed a coding CLI's user turn: subagent prompts and harness text are machine."""

from __future__ import annotations

from conversation_corpus_engine.archive_author import HUMAN, MACHINE, author_of
from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.archive_provenance import (
    BASIS_KEY,
    classify_user_turn,
    reclassify_typed_turns,
)


def test_a_plain_prompt_in_a_root_session_is_the_persons() -> None:
    assert classify_user_turn("fix the failing test", "root") == (True, "typed")
    assert classify_user_turn("fix the failing test", "") == (True, "typed")


def test_every_user_turn_of_a_subagent_is_machine() -> None:
    assert classify_user_turn("fix the failing test", "subagent") == (False, "subagent-prompt")
    assert classify_user_turn("<command-name>/x</command-name>", "subagent")[0] is False


def test_slash_commands_shell_commands_and_pasted_text_are_the_persons() -> None:
    for text, basis in (
        ("<command-name>/compact</command-name>", "typed:command-name"),
        ("<command-message>closeout</command-message>", "typed:command-message"),
        ("<bash-input>ls -la</bash-input>", "typed:bash-input"),
        ("<pasted_content>notes</pasted_content>", "typed:pasted_content"),
    ):
        assert classify_user_turn(text, "root") == (True, basis)


def test_text_a_harness_injected_is_machine() -> None:
    for text, basis in (
        ("<system-reminder>be brief</system-reminder>", "injected:system-reminder"),
        ("<local-command-stdout>ok</local-command-stdout>", "injected:local-command-stdout"),
        ("<task-notification>done</task-notification>", "injected:task-notification"),
        ("<environment_context>cwd</environment_context>", "injected:environment_context"),
        ("<session_context>cwd</session_context>", "injected:session_context"),
        ("<ide_opened_file>a.py</ide_opened_file>", "injected:ide_opened_file"),
        ("  <system-reminder>indented</system-reminder>", "injected:system-reminder"),
    ):
        assert classify_user_turn(text, "root") == (False, basis)


def test_plain_text_templates_the_harness_writes_are_machine() -> None:
    for text in (
        "This session is being continued from a previous conversation that ran out of context.",
        "Base directory for this skill: /skills/closeout",
        "Stop hook feedback: [keep going]",
        "[Request interrupted by user for tool use]",
        "# AGENTS.md instructions for /repo",
        "A previous agent produced the plan below",
        "[3 prior /loop wakeups found nothing actionable]",
    ):
        assert classify_user_turn(text, "root") == (False, "harness-text")


def test_a_tag_nobody_classified_is_machine_and_named_not_guessed() -> None:
    assert classify_user_turn("<brand_new_wrapper>x</brand_new_wrapper>", "root") == (
        False,
        "unclassified-tag:brand_new_wrapper",
    )


def test_reclassify_moves_the_machine_turns_off_the_typed_channel_and_counts_bases() -> None:
    conversation = Conversation(
        provider="claudecode",
        source_id="s1",
        title="t",
        extra={"session_kind": "root"},
        messages=[
            Message(role="user", text="please refactor", channel="typed"),
            Message(role="user", text="<system-reminder>x</system-reminder>", channel="typed"),
            Message(role="assistant", text="done", channel="reply"),
            Message(role="tool", text="output", channel="tool"),
        ],
    )
    counts = reclassify_typed_turns(conversation)
    assert dict(counts) == {"human:typed": 1, "machine:injected:system-reminder": 1}
    human, injected, reply, tool = conversation.messages
    assert (human.channel, human.typed_chars, human.meta[BASIS_KEY]) == ("typed", 15, "typed")
    assert (injected.channel, injected.typed_chars) == ("system", 0)
    assert injected.meta[BASIS_KEY] == "injected:system-reminder"
    assert BASIS_KEY not in reply.meta and BASIS_KEY not in tool.meta
    assert [author_of(m.role, m.channel) for m in conversation.messages] == [
        HUMAN,
        MACHINE,
        MACHINE,
        MACHINE,
    ]


def test_a_subagent_session_has_no_human_turn() -> None:
    conversation = Conversation(
        provider="claudecode",
        source_id="s2",
        title="t",
        extra={"session_kind": "subagent"},
        messages=[Message(role="user", text="research the thing", channel="typed")],
    )
    assert dict(reclassify_typed_turns(conversation)) == {"machine:subagent-prompt": 1}
    assert author_of(conversation.messages[0].role, conversation.messages[0].channel) == MACHINE


def test_the_written_channel_is_human_only_for_a_user_turn() -> None:
    assert author_of("user", "written") == HUMAN
    assert author_of("user", "typed") == HUMAN
    assert author_of("assistant", "written") == MACHINE
    assert author_of("system", "written") == MACHINE
    assert author_of("user", "project") == MACHINE
    assert author_of("user", "memory") == MACHINE
