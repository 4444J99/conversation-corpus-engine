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
        "[Workflow harness — computed task] The task text below was computed at runtime",
        "[Workflow harness — user request] The harness relays, verbatim and indented below",
        "[SYSTEM NOTIFICATION - NOT USER INPUT]\nThis is an automated background-task event",
        "Another Claude session sent a message while you were working:\n<agent-message />",
        "The coordinator sent a message while you were working:\nThe verifier passed",
        "Background (from the session that started this one):\nThis is a one-shot probe.",
        'A session-scoped Stop hook is now active with condition: "get it all done"',
        "Goal check-in: «Complete everything in this work stream.» is still active",
        "Your response above was cut off mid-stream. Resume directly from where it stops",
        "Your response above was stopped by a safety classifier — this is not a tool or API error.",
        "Skill /workflow-authoring is already loaded above; instructions unchanged.",
        "The following is a <SYSTEM_MESSAGE> not actually sent by the user.",
        "You are a subagent spawned by another session. Use model opencode/inkling-2-small-free.",
        "You are a worker in the autonomous development team. Your mission: investigate",
        "# FLAME — the Continuity Kernel\n\n> This file is the portable *self* of VLTIMA.",
        "[Scheduled prompt #1]\nRun the next bounded slice of the github-universe mission",
        "[PR COMMENT FIX REQUEST - BATCH]\nYou must use task subagents to perform this request.",
        "[PR CHECK FIX REQUEST]\nYou must use a task subagent to perform this request.",
        "[structured-output-enforce] You MUST call the StructuredOutput tool to complete this request. Call this tool now.",
        "[handback-send-enforce] Your report has not been delivered. Call SubagentHandback({message: <your full report>}).",
        '[Cross-session idle notice] "resume external ssd work", which you asked to be notified about, is idle now.',
        "You are an independent code reviewer. You did not write this change and have no stake in it.",
        "You are a subagent handling one PR comment only. PR organvm-iii-ergon/public-record-data-scrapper#422 is already merged.",
        "You are working in a local workspace for a pull request this project opened. If you cannot push to its branch, report that.",
        "You are working in a git worktree at: /Users/4jp/Workspace/organvm/copilot-worktrees/peer-audited--behavioral-blockchain",
        "You are working in PR 422 workspace /Users/4jp/Workspace/organvm/copilot-worktrees/public-record-data-scrapper/4444j99-fantastic-telegram.",
        "Read-only web verification. Never send mail, never apply, never log in, never enter credentials.",
        "Independently review a docs-only change in the git worktree /Volumes/Archive4T/Runtime/tmp/claude-501/-Volumes-Archive4T",
        "Evaluate only these comments on merged PR organvm-iii-ergon/public-record-data-scrapper#422. Do not edit, commit, push.",
        "Name this chat from the user's first request.",
        'The user explicitly invoked the "/closeout" skill. Follow its instructions now.',
        "# Autonomous loop tick (dynamic pacing)\nRun the autonomous check using the loop instructions.",
        "# Autonomous loop check\nYou're being invoked on a timer.",
        "## Context Usage\n\n**Model:** claude-sonnet-5-5",
        "# Pull Request Creation\n\nThe user wants to open a pull request for the current session.",
    ):
        assert classify_user_turn(text, "root") == (False, "harness-text")


def test_a_tag_nobody_classified_is_machine_and_named_not_guessed() -> None:
    assert classify_user_turn("<brand_new_wrapper>x</brand_new_wrapper>", "root") == (
        False,
        "unclassified-tag:brand_new_wrapper",
    )


def test_a_root_session_with_a_parent_is_the_parents_prompts() -> None:
    # A spawned child can record session_kind=root; the parent wrote its prompts (#95).
    assert classify_user_turn("do the task", "root", "parent-1") == (False, "subagent-prompt")
    assert classify_user_turn("do the task", "", "parent-1") == (False, "subagent-prompt")
    assert classify_user_turn("do the task", "unknown", "parent-1") == (False, "subagent-prompt")
    # Without a parent, a root session stays the person's.
    assert classify_user_turn("do the task", "root") == (True, "typed")
    assert classify_user_turn("do the task", "root", "") == (True, "typed")


def test_reclassify_counts_a_kind_parent_disagreement_without_moving_basis_sums() -> None:
    disagreeing = Conversation(
        provider="geminicli",
        source_id="s3",
        title="t",
        extra={"session_kind": "subagent"},  # no parent recorded
        messages=[Message(role="user", text="do the thing", channel="typed")],
    )
    counts = reclassify_typed_turns(disagreeing)
    assert counts["diagnostic:kind-parent-disagreement"] == 1
    assert counts["machine:subagent-prompt"] == 1
    spawned = Conversation(
        provider="codex",
        source_id="s4",
        title="t",
        extra={"session_kind": "root", "parent_session_id": "parent-1"},
        messages=[Message(role="user", text="do the thing", channel="typed")],
    )
    counts = reclassify_typed_turns(spawned)
    assert counts["diagnostic:kind-parent-disagreement"] == 1
    assert counts["machine:subagent-prompt"] == 1
    assert spawned.messages[0].channel == "system"
    agreeing = Conversation(
        provider="codex",
        source_id="s5",
        title="t",
        extra={"session_kind": "subagent", "parent_session_id": "parent-1"},
        messages=[Message(role="user", text="do the thing", channel="typed")],
    )
    assert "diagnostic:kind-parent-disagreement" not in reclassify_typed_turns(agreeing)


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
        extra={"session_kind": "subagent", "parent_session_id": "p"},
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
