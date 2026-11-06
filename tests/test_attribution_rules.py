"""The authorship rules engine: deterministic, versioned, conservative (synthetic data only)."""

from __future__ import annotations

import pytest

from conversation_corpus_engine.attribution import text_digest
from conversation_corpus_engine.attribution_rules import (
    RULES,
    RULES_VERSION,
    AttributedSpan,
    MessageContext,
    RecordInfo,
    attribute_message,
    partition_problems,
    record_info,
    rule_agent_family,
    rule_code,
    rule_document_origin,
    rule_inline_injection,
    rule_log_trace,
    rule_quote,
    rule_typed_prefix,
    rule_typed_remainder,
)

CHAT = RecordInfo(provider="chatgpt", kind="chat", wing="ai")
CLI = RecordInfo(provider="claudecode", kind="session", wing="ai", session_kind="main")
NOTE = RecordInfo(provider="applenotes", kind="note", wing="authored", authored_family=True)


def ctx(
    text: str,
    *,
    role: str = "user",
    channel: str = "typed",
    typed: int | None | str = "all",
    meta: dict | None = None,
    record: RecordInfo = CHAT,
    boilerplate: tuple = (),
) -> MessageContext:
    return MessageContext(
        text=text,
        role=role,
        channel=channel,
        typed_chars=len(text) if typed == "all" else typed,
        meta=meta or {},
        record=record,
        boilerplate=boilerplate,
    )


def run(text: str, **kwargs) -> list[AttributedSpan]:
    context = ctx(text, **kwargs)
    return attribute_message(
        text,
        role=context.role,
        channel=context.channel,
        typed_chars=context.typed_chars,
        meta=context.meta,
        record=context.record,
        boilerplate=context.boilerplate,
    )


def view(text: str, spans: list[AttributedSpan]) -> list[tuple[str, str, str]]:
    return [(span.rule, span.author, text[span.start : span.end]) for span in spans]


# --- engine core -----------------------------------------------------------------------------


def test_every_owner_granting_rule_runs_after_every_other_rule() -> None:
    owners = [index for index, rule in enumerate(RULES) if rule.author == "owner"]
    others = [index for index, rule in enumerate(RULES) if rule.author != "owner"]
    assert owners and others
    assert max(others) < min(owners)
    assert len({rule.id for rule in RULES}) == len(RULES)


def test_empty_text_has_no_spans() -> None:
    assert run("") == []


def test_unclaimed_text_is_unknown_never_owner() -> None:
    text = "Plain words on a channel no rule vouches for."
    spans = run(text, channel="project")
    assert view(text, spans) == [("unattributed", "unknown", text)]


def test_spans_carry_range_rule_digest_and_default_ineligible() -> None:
    text = "Hello there."
    (span,) = run(text)
    assert (span.start, span.end) == (0, len(text))
    assert span.rule == "typed_prefix" and span.author == "owner"
    assert span.text_digest == text_digest(text)
    assert span.profile_eligible is False
    assert RULES_VERSION.startswith("cce.attribution-rules@")


def test_span_ids_are_stable_and_bound_to_the_exact_text() -> None:
    text = "Stable words for a stable id."
    (first,) = run(text)
    (second,) = run(text)
    assert first.span_id == second.span_id and len(first.span_id) == 32
    (changed,) = run(text[:-1] + "!")
    assert changed.span_id != first.span_id
    assert changed.text_digest != first.text_digest


def test_partition_problems_reports_gaps_overlaps_and_short_cover() -> None:
    text = "0123456789"
    digest = text_digest(text)

    def span(start: int, end: int) -> AttributedSpan:
        return AttributedSpan(start, end, "unknown", "unattributed", "", digest)

    assert not any(partition_problems(text, [span(0, 10)]).values())
    assert partition_problems(text, [span(0, 4), span(6, 10)])["gaps"] == 1
    assert partition_problems(text, [span(0, 4), span(6, 10)])["uncovered"] == 2
    assert partition_problems(text, [span(0, 6), span(4, 10)])["overlaps"] == 1
    assert partition_problems(text, [span(0, 6)])["uncovered"] == 4
    assert partition_problems(text, [span(0, 10), span(0, 10)])["overlaps"] == 1
    assert partition_problems(text, [span(0, 12)])["out_of_range"] == 1
    foreign = AttributedSpan(0, 10, "unknown", "unattributed", "", text_digest("other text"))
    assert partition_problems(text, [foreign])["digest_mismatch"] == 1


# --- i: agent families -----------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["chatgpt", "claude", "gemini", "claudecode", "applenotes"])
@pytest.mark.parametrize("role", ["assistant", "model", "tool", "system"])
def test_non_user_turns_of_every_provider_are_agent(provider: str, role: str) -> None:
    text = "Output the machine produced."
    record = RecordInfo(provider=provider, kind="chat", wing="ai")
    spans = run(text, role=role, channel="reply", record=record)
    assert view(text, spans) == [("agent_family", "agent", text)]


@pytest.mark.parametrize("channel", ["reply", "thinking", "tool", "system", "memory"])
def test_user_turns_on_machine_channels_are_agent(channel: str) -> None:
    text = "Text a harness put in a user message."
    assert view(text, run(text, channel=channel)) == [("agent_family", "agent", text)]


def test_unknown_role_is_unattributed() -> None:
    text = "A turn whose role the engine does not know."
    assert view(text, run(text, role="narrator")) == [("unattributed", "unknown", text)]


def test_subagent_prompts_and_harness_templates_are_agent() -> None:
    subagent = RecordInfo(provider="claudecode", kind="session", session_kind="subagent")
    text = "Search the repository for the failing call."
    assert view(text, run(text, record=subagent)) == [("agent_family", "agent", text)]
    template = "This session is being continued from a previous conversation that ran out."
    assert view(template, run(template, record=CLI)) == [("agent_family", "agent", template)]
    injected = "<system-reminder>Reminder text.</system-reminder>\nthen words"
    assert [s.rule for s in run(injected, record=CLI)] == ["agent_family"]
    typed = "Please rename the module."
    assert view(typed, run(typed, record=CLI)) == [("typed_prefix", "owner", typed)]


def test_message_metadata_marking_agent_origin_makes_the_turn_agent() -> None:
    text = "Text carrying a provider marker."
    spans = run(text, meta={"agent_authored": True})
    assert view(text, spans) == [("agent_family", "agent", text)]
    assert view(text, run(text, meta={"generated_by": "Claude Code"}))[0][:2] == (
        "agent_family",
        "agent",
    )
    assert view(text, run(text, meta={"generated_by": "pandoc"}))[0][:2] == (
        "typed_prefix",
        "owner",
    )


# --- h: document origin ----------------------------------------------------------------------


def payload(**changes) -> dict:
    base = {
        "provider": "documents",
        "kind": "document",
        "extra": {"wing": "authored", "lane": "writing"},
        "provenance": {},
    }
    base.update(changes)
    return base


def doc(text: str, record: RecordInfo) -> list[tuple[int, int, str]]:
    return rule_document_origin(ctx(text, channel="project", typed=0, record=record))


def test_document_with_agent_provenance_marker_is_machine() -> None:
    record = record_info(payload(provenance={"agent_authored": True}))
    assert doc("A document body.", record) == [(0, 16, "origin=agent_authored")]
    chat = record_info(payload(provider="chatgpt", kind="chat", provenance={"generated_by": "gpt"}))
    assert doc("A chat turn.", chat)[0][2] == "origin=generated_by"


@pytest.mark.parametrize(
    "path",
    [
        "skills/example/SKILL.md",
        "project/CLAUDE.md",
        "project/agents.md",
        "tools/.claude/settings-notes.md",
        "tools/.codex/prompts/review.md",
        ".github/copilot-instructions.md",
        "rules/style.mdc",
    ],
)
def test_agent_instruction_files_are_machine_documents(path: str) -> None:
    record = record_info(payload(provenance={"source_file": path}))
    claims = doc("Body of the instruction file.", record)
    assert [(s, e) for s, e, _ in claims] == [(0, 29)]
    assert claims[0][2].startswith("origin=path:")


def test_agent_paths_do_not_condemn_whole_coding_sessions() -> None:
    session = record_info(
        payload(
            provider="claudecode",
            kind="session",
            provenance={"source_file": "home/.claude/projects/p/session.jsonl"},
        )
    )
    assert doc("A typed prompt in a session stored under an agent folder.", session) == []


def test_plain_authored_document_is_not_agent_origin() -> None:
    record = record_info(payload(provenance={"source_file": "writing/essay.md"}))
    assert doc("An essay the owner wrote by hand.", record) == []


@pytest.mark.parametrize(
    "body",
    [
        "Notes.\n\nGenerated with Claude Code",
        "Notes.\n\nCo-Authored-By: Claude <noreply@example.invalid>",
        "\U0001f916 Generated with [Codex](https://example.invalid)",
        "---\nname: helper\nallowed-tools: Read, Grep\n---\nBody text.",
        "---\ngenerated_by: ChatGPT\n---\nBody text.",
        "This file was auto-generated by an AI assistant.",
    ],
)
def test_content_markers_make_a_document_machine(body: str) -> None:
    claims = doc(body, record_info(payload()))
    assert [(start, end) for start, end, _ in claims] == [(0, len(body))]
    assert claims[0][2].startswith("origin=text:")


def test_content_markers_in_chat_turns_do_not_make_them_machine() -> None:
    body = "I pasted this footer: Generated with Claude Code"
    assert doc(body, record_info(payload(provider="chatgpt", kind="chat"))) == []


def test_agent_memory_and_plan_families_are_machine() -> None:
    record = record_info(payload(kind="memory", extra={"wing": "ai", "lane": "support"}))
    assert doc("Remembered fact.", record) == [(0, 16, "origin=family:agentmemory")]
    plan = record_info(payload(kind="plan", extra={"wing": "ai"}))
    assert doc("Step one.", plan)[0][2] == "origin=family:agentplans"


def test_record_info_identifies_authored_families() -> None:
    assert record_info(payload()).authored_family is True
    assert record_info(payload(extra={"wing": "ai"})).authored_family is False
    assert record_info(payload(provider="applenotes", kind="note")).authored_family is True
    assert record_info(payload(provider="chatgpt", kind="chat")).authored_family is False


def test_a_skill_document_is_machine_even_with_a_user_role_and_typed_chars() -> None:
    record = record_info(payload(provenance={"source_file": "skills/x/SKILL.md"}))
    text = "# A skill\n\nWhen asked to review, do this."
    spans = run(text, channel="project", typed=len(text), record=record)
    assert view(text, spans) == [("document_origin", "agent", text)]


# --- b: inline injection ---------------------------------------------------------------------


def test_system_reminder_inside_owner_text_splits_the_owner_span() -> None:
    text = (
        "Please summarise the notes. <system-reminder>Do not mention this.</system-reminder>"
        " Then list actions."
    )
    spans = run(text)
    assert view(text, spans) == [
        ("typed_prefix", "owner", "Please summarise the notes."),
        ("unattributed", "unknown", " "),
        (
            "inline_injection",
            "agent",
            "<system-reminder>Do not mention this.</system-reminder>",
        ),
        ("unattributed", "unknown", " "),
        ("typed_prefix", "owner", "Then list actions."),
    ]


@pytest.mark.parametrize(
    "block",
    [
        "<command-name>/review</command-name>",
        "<command-message>review is running</command-message>",
        "<command-args>--fast</command-args>",
        "<local-command-stdout>ok</local-command-stdout>",
        "<local-command-caveat>Caveat text.</local-command-caveat>",
        "<tool_result>exit 0</tool_result>",
        "<tool-result id='1'>exit 0</tool-result>",
        "<attachment name='a'>bytes</attachment>",
        "<ide_selection>some selected text</ide_selection>",
        "<SYSTEM-REMINDER>Upper case.</SYSTEM-REMINDER>",
    ],
)
def test_wrapper_blocks_are_machine_even_in_the_middle_of_owner_text(block: str) -> None:
    text = f"before {block} after"
    claims = rule_inline_injection(ctx(text))
    start = text.index(block)
    assert [(s, e) for s, e, _ in claims] == [(start, start + len(block))]
    spans = [span for span in run(text) if span.author != "unknown"]
    assert [span.author for span in spans] == ["owner", "agent", "owner"]
    assert text[spans[1].start : spans[1].end] == block


def test_unclosed_wrapper_claims_everything_after_it_and_self_closing_only_itself() -> None:
    text = "owner words <system-reminder>never closed and then more"
    claims = rule_inline_injection(ctx(text))
    assert [(s, e) for s, e, _ in claims] == [(12, len(text))]
    text = "a <skill/> b"
    assert [(s, e) for s, e, _ in rule_inline_injection(ctx(text))] == [(2, 10)]


def test_unknown_tags_and_plain_angle_brackets_are_left_alone() -> None:
    text = "if a < b and <custom>keep</custom> then <b>bold</b> stays"
    assert rule_inline_injection(ctx(text)) == []


def test_several_blocks_are_each_claimed_separately() -> None:
    text = "<system-reminder>one</system-reminder> mid <command-name>/x</command-name> end"
    claims = rule_inline_injection(ctx(text))
    assert [(s, e) for s, e, _ in claims] == [(0, 38), (43, 74)]
    assert [detail for _, _, detail in claims] == ["tag=system-reminder", "tag=command-name"]


def test_self_closing_tag_with_attributes_is_claimed_alone() -> None:
    text = 'x <skill name="a"/> y <system-reminder>z</system-reminder>'
    claims = rule_inline_injection(ctx(text))
    assert [(s, e) for s, e, _ in claims] == [(2, 19), (22, 58)]


# --- a: typed prefix and remainder -----------------------------------------------------------


def test_only_the_typed_prefix_of_a_user_turn_is_owner() -> None:
    typed = "Fix the parser please."
    text = typed + "\n\nHarness appended context that nobody typed."
    spans = run(text, typed=len(typed))
    assert view(text, spans) == [
        ("typed_prefix", "owner", typed),
        ("typed_remainder", "agent", text[len(typed) :]),
    ]
    assert spans[0].detail == f"typed_chars={len(typed)}/{len(text)}"


def test_zero_typed_chars_means_the_whole_turn_is_machine() -> None:
    text = "Attachment text a reader appended."
    assert view(text, run(text, typed=0)) == [("typed_remainder", "agent", text)]


@pytest.mark.parametrize("typed", [None, -1, True, 3.5, "many"])
def test_missing_or_invalid_typed_chars_gives_no_owner_claim(typed) -> None:
    text = "A turn whose typed length is not stated."
    assert view(text, run(text, typed=typed)) == [("unattributed", "unknown", text)]


def test_typed_chars_beyond_the_text_is_clamped() -> None:
    text = "Short."
    assert view(text, run(text, typed=500)) == [("typed_prefix", "owner", text)]


def test_typed_rules_need_a_user_turn_on_the_typed_channel() -> None:
    text = "Some text of a given length."
    for channel in ("written", "project", ""):
        assert rule_typed_prefix(ctx(text, channel=channel)) == []
        assert rule_typed_remainder(ctx(text, channel=channel, typed=3)) == []
    assert rule_typed_prefix(ctx(text, role="assistant")) == []


def test_typed_rules_do_not_apply_to_authored_families() -> None:
    text = "A note whose reader overrode typed_chars."
    assert rule_typed_prefix(ctx(text, record=NOTE, typed=3)) == []
    assert rule_typed_remainder(ctx(text, record=NOTE, typed=3)) == []


def test_owner_spans_never_start_or_end_with_whitespace() -> None:
    text = "  owner words  \n\n<system-reminder>x</system-reminder>\n\n"
    spans = run(text)
    assert view(text, spans) == [
        ("unattributed", "unknown", "  "),
        ("typed_prefix", "owner", "owner words"),
        ("unattributed", "unknown", "  \n\n"),
        ("inline_injection", "agent", "<system-reminder>x</system-reminder>"),
        ("unattributed", "unknown", "\n\n"),
    ]
    blank = "   \n\n  "
    assert view(blank, run(blank)) == [("unattributed", "unknown", blank)]


def test_agent_family_rule_function_directly() -> None:
    assert rule_agent_family(ctx("x", role="assistant")) == [(0, 1, "role=assistant")]
    assert rule_agent_family(ctx("x")) == []


# --- c: quotes -------------------------------------------------------------------------------

LONG = "the quick brown fox jumps over the lazy dog by the river"


def ranges(claims: list[tuple[int, int, str]]) -> list[tuple[int, int]]:
    return [(start, end) for start, end, _ in claims]


def test_blockquote_then_owner_reply() -> None:
    text = (
        "> The committee shall meet quarterly.\n> Minutes are public.\n\nI think that is too rare."
    )
    assert view(text, run(text)) == [
        ("quote", "third_party", "> The committee shall meet quarterly.\n> Minutes are public."),
        ("unattributed", "unknown", "\n\n"),
        ("typed_prefix", "owner", "I think that is too rare."),
    ]


def test_owner_text_on_both_sides_of_an_indented_blockquote() -> None:
    text = "As the memo says:\n   > Budgets close in May.\nThat is wrong."
    assert view(text, run(text)) == [
        ("typed_prefix", "owner", "As the memo says:"),
        ("unattributed", "unknown", "\n"),
        ("quote", "third_party", "   > Budgets close in May."),
        ("unattributed", "unknown", "\n"),
        ("typed_prefix", "owner", "That is wrong."),
    ]


def test_blockquote_groups_are_contiguous_marker_lines_only() -> None:
    text = "> one\n>\n> two\n\n> three"
    assert ranges(rule_quote(ctx(text))) == [(0, 13), (15, 22)]
    assert rule_quote(ctx("    > four spaces is not a quote")) == []
    assert rule_quote(ctx("a > b is not a quote either")) == []


@pytest.mark.parametrize(
    ("opening", "closing"),
    [('"', '"'), ("“", "”"), ("«", "»"), ("„", "“")],
)
def test_long_passages_inside_quotation_markers_are_third_party(opening: str, closing: str) -> None:
    text = f"She wrote {opening}{LONG}{closing} in the margin."
    start = text.index(opening)
    end = text.index(closing, start + 1) + 1
    assert ranges(rule_quote(ctx(text))) == [(start, end)]


def test_short_unmatched_or_paragraph_crossing_quotes_are_left_alone() -> None:
    assert rule_quote(ctx('He said "hello" and left.')) == []
    assert rule_quote(ctx(f'one stray " mark then {LONG}')) == []
    assert rule_quote(ctx(f'"{LONG}\n\n{LONG}"')) == []


def test_pasted_content_wrapper_is_third_party_not_injection() -> None:
    text = "I got this: <pasted_content>some text from elsewhere</pasted_content> what now?"
    claims = rule_quote(ctx(text))
    start = text.index("<pasted_content>")
    assert claims == [(start, text.index(" what now?"), "tag=pasted_content")]
    assert rule_inline_injection(ctx(text)) == []


# --- d: code ---------------------------------------------------------------------------------


def test_fenced_code_is_third_party_and_owner_prose_survives() -> None:
    text = "Run this:\n```python\nprint('hi')\n```\nand tell me why."
    assert view(text, run(text)) == [
        ("typed_prefix", "owner", "Run this:"),
        ("unattributed", "unknown", "\n"),
        ("code", "third_party", "```python\nprint('hi')\n```"),
        ("unattributed", "unknown", "\n"),
        ("typed_prefix", "owner", "and tell me why."),
    ]


def test_unclosed_fence_runs_to_the_end_and_short_closers_do_not_close() -> None:
    text = "intro\n```\nstill code\nmore code"
    assert ranges(rule_code(ctx(text))) == [(6, len(text))]
    nested = "````\n```\nx\n````\nafter"
    assert ranges(rule_code(ctx(nested))) == [(0, nested.index("\nafter"))]
    short = "````\nx\n```\ny"
    assert ranges(rule_code(ctx(short))) == [(0, len(short))]
    tilde = "~~~\na\n~~~\nz"
    assert ranges(rule_code(ctx(tilde))) == [(0, 9)]


def test_backtick_fence_with_a_backtick_in_its_info_string_is_not_a_fence() -> None:
    assert rule_code(ctx("``` is not `a fence`\nplain words")) == []


def test_indented_code_needs_a_blank_line_before_and_two_indented_lines() -> None:
    text = "Example:\n\n    x = 1\n    y = 2\n\nDone."
    assert ranges(rule_code(ctx(text))) == [(text.index("    x"), text.index("\n\nDone."))]
    assert rule_code(ctx("Example:\n\n    x = 1\n\nDone.")) == []
    assert rule_code(ctx("Para text\n    x = 1\n    y = 2")) == []
    tabbed = "Intro\n\n\tx = 1\n\ty = 2"
    assert ranges(rule_code(ctx(tabbed))) == [(7, len(tabbed))]
    gapped = "Intro\n\n    a = 1\n\n    b = 2\n\nOut."
    assert ranges(rule_code(ctx(gapped))) == [(gapped.index("    a"), gapped.index("\n\nOut."))]


def test_code_inside_a_blockquote_is_claimed_by_the_quote_not_the_code_rule() -> None:
    text = "> ```\n> code\n> ```"
    assert rule_code(ctx(text)) == []
    assert view(text, run(text)) == [("quote", "third_party", text)]


# --- e: logs and traces ----------------------------------------------------------------------


def test_timestamped_log_lines_are_machine_even_alone() -> None:
    text = "It crashed:\n2026-01-02 03:04:05 INFO worker started\nthen I rebooted."
    assert view(text, run(text)) == [
        ("typed_prefix", "owner", "It crashed:"),
        ("unattributed", "unknown", "\n"),
        ("log_trace", "agent", "2026-01-02 03:04:05 INFO worker started"),
        ("unattributed", "unknown", "\n"),
        ("typed_prefix", "owner", "then I rebooted."),
    ]
    for line in (
        "[03:04:05] built",
        "Jan  2 03:04:05 host kernel: started",
        "2026-01-02T03:04:05Z up",
    ):
        assert ranges(rule_log_trace(ctx(line))) == [(0, len(line))]


def test_a_diary_line_without_seconds_is_not_a_log() -> None:
    assert rule_log_trace(ctx("2026-01-02 09:15 Went to the market.")) == []
    assert rule_log_trace(ctx("ERROR: I forgot the milk.")) == []


@pytest.mark.parametrize(
    "run_of_lines",
    [
        "ERROR: first\nERROR: second\nERROR: third",
        "src/a.py:10:3: unused import\nsrc/b.py:11:1: bad name\nsrc/c.py:2:9: worse",
        "$ ls\n$ pwd\n$ whoami",
        "@@ -1,2 +1,3 @@\n--- a/x\n+++ b/x",
        "TypeError: x is not a function\n    at run (a.js:1:2)\n    at main (b.js:3:4)",
        "java.lang.IllegalStateException: boom\n\tat com.example.Main.run(Main.java:12)\n"
        "\tat com.example.Main.main(Main.java:5)",
    ],
)
def test_tool_output_shaped_runs_of_three_lines_are_machine(run_of_lines: str) -> None:
    assert ranges(rule_log_trace(ctx(run_of_lines))) == [(0, len(run_of_lines))]


def test_two_lines_or_a_blank_line_break_a_weak_run() -> None:
    assert rule_log_trace(ctx("ERROR: first\nERROR: second")) == []
    assert rule_log_trace(ctx("ERROR: a\nERROR: b\n\nERROR: c\nERROR: d")) == []


def test_python_tracebacks_are_claimed_through_the_exception_line() -> None:
    trace = 'Traceback (most recent call last):\n  File "x.py", line 1, in <module>\n    boom()\nValueError: nope'
    text = f"I got:\n{trace}\nwhy?"
    start = text.index("Traceback")
    assert ranges(rule_log_trace(ctx(text))) == [(start, start + len(trace))]


def test_log_lines_quoted_or_fenced_are_not_double_claimed() -> None:
    assert rule_log_trace(ctx("> 2026-01-02 03:04:05 ERROR quoted")) == []
    fenced = "```\n2026-01-02 03:04:05 ERROR inside a fence\n```"
    assert view(fenced, run(fenced)) == [("code", "third_party", fenced)]


def test_a_message_mixing_owner_prose_logs_quotes_and_code() -> None:
    text = "\n".join(
        [
            "Here is the failure I saw:",
            "",
            "2026-01-02 03:04:05 ERROR boom",
            "2026-01-02 03:04:06 ERROR again",
            "",
            "> an earlier remark that I am quoting",
            "",
            "```",
            "x = 1",
            "```",
            "",
            "So what should I change?",
        ]
    )
    spans = [(span.rule, span.author) for span in run(text) if span.author != "unknown"]
    assert spans == [
        ("typed_prefix", "owner"),
        ("log_trace", "agent"),
        ("quote", "third_party"),
        ("code", "third_party"),
        ("typed_prefix", "owner"),
    ]
