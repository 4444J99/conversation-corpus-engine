"""The authorship rules engine: deterministic, versioned, conservative (synthetic data only)."""

from __future__ import annotations

import random
import time

import pytest

from conversation_corpus_engine.attribution import text_digest
from conversation_corpus_engine.attribution_rules import (
    RULES,
    RULES_VERSION,
    AssistantEcho,
    AttributedSpan,
    MessageContext,
    RecordInfo,
    RepeatCensus,
    attribute_message,
    boilerplate_ranges,
    partition_problems,
    record_info,
    rule_agent_family,
    rule_ambiguous,
    rule_boilerplate,
    rule_code,
    rule_document_origin,
    rule_inline_injection,
    rule_log_trace,
    rule_note_owner,
    rule_pasted_back,
    rule_pasted_block,
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
    pasted_back: tuple = (),
) -> MessageContext:
    return MessageContext(
        text=text,
        role=role,
        channel=channel,
        typed_chars=len(text) if typed == "all" else typed,
        meta=meta or {},
        record=record,
        boilerplate=boilerplate,
        pasted_back=pasted_back,
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
        pasted_back=context.pasted_back,
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


def test_a_generated_by_footer_deep_in_a_document_is_found_wherever_it_sits() -> None:
    body = "a" * 5000 + "\nGenerated with Claude\n" + "b" * 5000
    claims = doc(body, record_info(payload()))
    assert [(start, end) for start, end, _ in claims] == [(0, len(body))]
    assert view(body, run(body, channel="project", typed=0, record=record_info(payload())))[0][
        :2
    ] == (
        "document_origin",
        "agent",
    )


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


def test_nested_reminders_end_at_their_matching_close_so_no_tail_leaks() -> None:
    text = "hi <system-reminder>a <system-reminder>b</system-reminder> TAIL</system-reminder> after"
    claims = rule_inline_injection(ctx(text))
    end = text.index("</system-reminder> after") + len("</system-reminder>")
    assert ranges(claims) == [(3, end)]
    owner = [text[s.start : s.end] for s in run(text) if s.author == "owner"]
    assert owner == ["hi", "after"]


def test_an_unclosed_nested_reminder_makes_the_rest_of_the_message_machine() -> None:
    text = "hi <system-reminder>a <system-reminder>b</system-reminder> TAIL and more"
    assert ranges(rule_inline_injection(ctx(text))) == [(3, len(text))]
    assert [text[s.start : s.end] for s in run(text) if s.author == "owner"] == ["hi"]


def test_a_self_closing_inner_tag_does_not_deepen_the_nesting() -> None:
    text = "a <system-reminder>x <system-reminder/> y</system-reminder> TAIL"
    end = text.index(" TAIL")
    assert ranges(rule_inline_injection(ctx(text))) == [(2, end)]


@pytest.mark.parametrize(
    "opening",
    [
        '<system-reminder note="a<b">',
        '<system-reminder note="a>b">',
        "<system-reminder note='a<b>c'>",
        "<system-reminder  a=\"1\"   b='2' >",
        '<system-reminder note="x < y" other="p > q">',
    ],
)
def test_angle_brackets_inside_quoted_attribute_values_do_not_hide_a_reminder(
    opening: str,
) -> None:
    text = f"hi {opening}SECRET</system-reminder> after"
    end = text.index("</system-reminder> after") + len("</system-reminder>")
    assert ranges(rule_inline_injection(ctx(text))) == [(3, end)]
    assert not any("SECRET" in text[s.start : s.end] for s in run(text) if s.author == "owner")


def test_a_malformed_opening_tag_of_a_wrapper_still_claims_the_block() -> None:
    text = 'hi <system-reminder note="oops>SECRET</system-reminder> after'
    claims = rule_inline_injection(ctx(text))
    assert claims and claims[0][0] == 3 and claims[0][1] >= text.index("SECRET")
    assert not any("SECRET" in text[s.start : s.end] for s in run(text) if s.author == "owner")


def test_a_wrapper_tag_inside_another_wrapper_is_part_of_the_outer_block() -> None:
    text = "x <system-reminder>a <command-name>y</command-name> b</system-reminder> TAIL"
    end = text.index(" TAIL")
    assert ranges(rule_inline_injection(ctx(text))) == [(2, end)]


def test_pathological_wrapper_input_is_linear_and_never_owner() -> None:
    started = time.monotonic()
    unclosed = "<system-reminder>" * 60_000
    assert not any(span.author == "owner" for span in run(unclosed))
    junk = "<b x " * 60_000 + "<system-reminder>z"
    assert [s.rule for s in run(junk) if s.rule == "inline_injection"] == ["inline_injection"]
    assert time.monotonic() - started < 10


def test_reminder_variants_never_leak_owner_text_over_seeded_cases() -> None:
    rng = random.Random(4004)
    shapes = [
        "<system-reminder>{b}</system-reminder>",
        "<SYSTEM-REMINDER>{b}</SYSTEM-REMINDER>",
        '<system-reminder priority="high" id="x">{b} a > b</system-reminder>',
        "<system-reminder>\n```\ncode > here\n```\n{b}\n</system-reminder>",
        "<system-reminder>outer {b} <system-reminder>inner</system-reminder> LEAK {b}</system-reminder>",
        '<system-reminder note="a<b">{b}</system-reminder>',
        "<system-reminder >{b}</system-reminder >",
        "<system-reminder>outer <system-reminder>mid <system-reminder>deep</system-reminder> LEAK"
        "</system-reminder> LEAK {b}</system-reminder>",
    ]
    for _ in range(300):
        body = prose(rng)
        block = rng.choice(shapes).format(b=body)
        text = f"Owner opening words. {block} Owner closing words."
        owner_text = " ".join(text[s.start : s.end] for s in run(text) if s.author == "owner")
        assert "LEAK" not in owner_text and body not in owner_text
        assert "Owner opening words." in owner_text and "Owner closing words." in owner_text


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
    text = "As the memo says:\n   > Budgets close in May.\n\nThat is wrong."
    assert view(text, run(text)) == [
        ("typed_prefix", "owner", "As the memo says:"),
        ("unattributed", "unknown", "\n"),
        ("quote", "third_party", "   > Budgets close in May."),
        ("unattributed", "unknown", "\n\n"),
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


def test_one_stray_ascii_quote_makes_its_whole_paragraph_unknown_never_owner() -> None:
    text = 'He wrote "' + "x" * 60 + 'QUOTED" and 5" is small.'
    assert rule_quote(ctx(text)) == []
    assert ranges(rule_ambiguous(ctx(text))) == [(0, len(text))]
    assert view(text, run(text)) == [("ambiguous", "unknown", text)]
    assert not [s for s in run(text) if s.author == "owner"]


def test_an_odd_quote_paragraph_does_not_taint_its_neighbours() -> None:
    odd = 'She measured 5" by 6 and wrote it down.'
    text = f"{odd}\n\nA clean paragraph of my own words.\n\n{odd}"
    spans = view(text, run(text))
    assert [(r, a) for r, a, _ in spans if a == "owner"] == [("typed_prefix", "owner")]
    assert [t for r, a, t in spans if a == "owner"] == ["A clean paragraph of my own words."]
    assert [r for r, a, _ in spans if r == "ambiguous"] == ["ambiguous", "ambiguous"]


def test_even_quote_counts_keep_pairing_and_a_stronger_rule_keeps_its_characters() -> None:
    even = f'He wrote "{LONG}" and that is all.'
    assert ranges(rule_quote(ctx(even))) == [(9, 9 + len(LONG) + 2)]
    assert rule_ambiguous(ctx(even)) == []
    fenced = '```\nodd " quote\n```'
    assert [s.rule for s in run(fenced)] == ["code"]
    quoted = '> a quoted line with an odd " mark'
    assert [s.rule for s in run(quoted)] == ["quote"]
    assert rule_ambiguous(ctx("single quotes ' and it's fine")) == []


def test_lazy_blockquote_continuation_lines_belong_to_the_quote() -> None:
    text = "> quoted line\nLAZYCONT line\nmore lazy words\n\nMy reply."
    assert ranges(rule_quote(ctx(text))) == [(0, text.index("\n\nMy reply."))]
    assert view(text, run(text))[-1] == ("typed_prefix", "owner", "My reply.")
    for interrupter in ("```", "# Heading", "- item", "1. item", "---", "~~~"):
        broken = f"> quoted\nlazy\n{interrupter}\nafter"
        assert ranges(rule_quote(ctx(broken))) == [(0, len("> quoted\nlazy"))], interrupter


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


@pytest.mark.parametrize(
    "short_trace",
    [
        "java.lang.IllegalStateException: boom\n\tat com.x.Y.m(Y.java:10)",
        "Caused by: java.io.IOException: closed\n\tat com.x.Z.n(Z.java:20)",
        'Exception in thread "main" java.lang.Error: x\n\tat com.x.Main.main(Main.java:5)',
        "TypeError: x is not a function\n    at run (a.js:1:2)",
        "    at run (a.js:1:2)\n    at main (b.js:3:4)",
    ],
)
def test_a_two_line_stack_trace_is_machine_output_even_below_three_lines(short_trace: str) -> None:
    assert ranges(rule_log_trace(ctx(short_trace))) == [(0, len(short_trace))]
    text = f"It failed:\n{short_trace}\nwhy?"
    assert [s.author for s in run(text) if s.rule == "log_trace"] == ["agent"]
    assert not any("Y.java" in text[s.start : s.end] for s in run(text) if s.author == "owner")


def test_a_lone_frame_or_header_and_ordinary_two_line_runs_are_still_left_alone() -> None:
    assert rule_log_trace(ctx("    at run (a.js:1:2)")) == []
    assert rule_log_trace(ctx("TypeError: x is not a function")) == []
    assert rule_log_trace(ctx("TypeError: x\nValueError: y")) == []
    assert rule_log_trace(ctx("    at run (a.js:1:2)\nplain words after it")) == []


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


# --- f: boilerplate --------------------------------------------------------------------------

TEMPLATE = " ".join(
    ["You are reviewing a proposal."]
    + ["Check scope, risks, costs and the schedule with care."] * 5
)


def test_the_template_fixture_is_long_enough() -> None:
    assert len(TEMPLATE) >= 200


def test_the_same_boilerplate_in_four_conversations_leaves_only_the_first_eligible() -> None:
    texts = [f"{TEMPLATE}\n\nTopic {index}: my own question." for index in range(4)]
    found = boilerplate_ranges(texts)
    size = len(TEMPLATE)
    assert found == [(), ((0, size),), ((0, size),), ((0, size),)]
    later = attribute_message(
        texts[2],
        role="user",
        channel="typed",
        typed_chars=len(texts[2]),
        record=CHAT,
        boilerplate=found[2],
    )
    assert view(texts[2], later) == [
        ("boilerplate", "third_party", TEMPLATE),
        ("unattributed", "unknown", "\n\n"),
        ("typed_prefix", "owner", "Topic 2: my own question."),
    ]
    first = attribute_message(
        texts[0],
        role="user",
        channel="typed",
        typed_chars=len(texts[0]),
        record=CHAT,
        boilerplate=found[0],
    )
    assert [(span.rule, span.author) for span in first] == [("typed_prefix", "owner")]


def test_repeats_are_matched_after_whitespace_normalization() -> None:
    variants = [TEMPLATE, TEMPLATE.replace(" ", "  "), TEMPLATE.replace(". ", ".\n")]
    found = boilerplate_ranges(variants)
    assert found[0] == ()
    assert found[1] == ((0, len(variants[1])),)
    assert found[2] == ((0, len(variants[2])),)


def test_thresholds_are_configurable_with_the_documented_defaults() -> None:
    size = len(TEMPLATE)
    assert boilerplate_ranges([TEMPLATE, TEMPLATE]) == [(), ()]
    assert boilerplate_ranges([TEMPLATE, TEMPLATE], min_occurrences=2) == [(), ((0, size),)]
    short = "Same short line repeated."
    assert boilerplate_ranges([short] * 5) == [()] * 5
    flagged = boilerplate_ranges([short] * 5, min_chars=10)
    assert flagged == [(), *[((0, len(short)),)] * 4]
    for bad in ({"min_chars": 0}, {"min_occurrences": 1}, {"min_chars": True}):
        with pytest.raises(ValueError, match="positive|at least"):
            RepeatCensus(**bad)


def test_a_paragraph_repeated_inside_one_message_keeps_only_its_first_copy() -> None:
    text = f"{TEMPLATE}\n\nmiddle words\n\n{TEMPLATE}\n\n{TEMPLATE}"
    size = len(TEMPLATE)
    second = text.index(TEMPLATE, size)
    third = text.index(TEMPLATE, second + size)
    assert boilerplate_ranges([text]) == [((second, second + size), (third, third + size))]


def test_a_repeated_multi_paragraph_message_is_one_unit() -> None:
    whole = "Alpha " * 20 + "\n\n" + "Beta " * 20
    whole = whole.replace("Alpha \n", "Alpha\n").rstrip()
    found = boilerplate_ranges([whole] * 3)
    assert found == [(), ((0, len(whole)),), ((0, len(whole)),)]


def test_first_means_first_in_the_order_the_slice_is_scanned() -> None:
    texts = [f"{TEMPLATE}\n\nSuffix {index}." for index in range(3)]
    census = RepeatCensus()
    for text in texts:
        census.observe(text)
    assert census.later_occurrences(texts[1]) == ()
    assert census.later_occurrences(texts[0]) == ((0, len(TEMPLATE)),)
    with pytest.raises(RuntimeError, match="observe"):
        census.observe("late text")


def test_rewinding_the_census_makes_the_next_pass_see_first_occurrences_again() -> None:
    texts = [f"{TEMPLATE}\n\nSuffix {index}." for index in range(3)]
    census = RepeatCensus()
    for text in texts:
        census.observe(text)
    first_pass = [census.later_occurrences(text) for text in texts]
    assert first_pass[0] == () and first_pass[1] and first_pass[2]
    assert census.later_occurrences(texts[0]) != ()
    census.rewind()
    assert [census.later_occurrences(text) for text in texts] == first_pass


def test_text_the_census_never_saw_is_never_boilerplate() -> None:
    assert RepeatCensus().later_occurrences(TEMPLATE) == ()


def test_boilerplate_rule_passes_the_census_ranges_through() -> None:
    assert rule_boilerplate(ctx("abcdefgh", boilerplate=((0, 4), (6, 8)))) == [
        (0, 4, "verbatim-repeat"),
        (6, 8, "verbatim-repeat"),
    ]
    assert rule_boilerplate(ctx("abcdefgh")) == []


# --- pasted-back assistant text ---------------------------------------------------------------

ANSWER = " ".join(
    f"Sentence {i} of the assistant answer explains point {i * 3} about the parser."
    for i in range(12)
)


def shared_with_answer(length: int, filler: int = 60) -> str:
    """A 600+ character paragraph that shares exactly `length` characters with ANSWER."""
    start = next(
        i
        for i in range(200, 400)
        if ANSWER[i - 1] != " " and ANSWER[i] != " " and ANSWER[i + length] != " "
    )
    middle = ANSWER[start : start + length]
    padding = " ".join(f"unrelated{i}" for i in range(filler))
    return f"{padding} #{middle}# {padding}"


def test_a_long_paragraph_pasted_back_from_an_earlier_assistant_turn_is_third_party() -> None:
    assert len(ANSWER) >= 600
    echo = AssistantEcho()
    echo.add_assistant(ANSWER)
    text = f"Please check this:\n\n{ANSWER}\n\nThanks."
    start = text.index(ANSWER)
    assert echo.pasted_back(text) == ((start, start + len(ANSWER)),)
    spans = attribute_message(
        text,
        role="user",
        channel="typed",
        typed_chars=len(text),
        record=CHAT,
        pasted_back=echo.pasted_back(text),
    )
    assert [(s.rule, s.author, text[s.start : s.end]) for s in spans if s.author != "unknown"] == [
        ("typed_prefix", "owner", "Please check this:"),
        ("pasted_back", "third_party", ANSWER),
        ("typed_prefix", "owner", "Thanks."),
    ]
    assert rule_pasted_back(ctx("abcdefgh", pasted_back=((0, 4), (6, 8)))) == [
        (0, 4, "pasted-back"),
        (6, 8, "pasted-back"),
    ]
    assert rule_pasted_back(ctx("abcdefgh")) == []


def test_pasted_back_text_needs_600_characters_and_100_shared_characters() -> None:
    echo = AssistantEcho()
    echo.add_assistant(ANSWER)
    assert echo.pasted_back(f"short:\n\n{ANSWER[:500]}") == ()
    shared_99 = shared_with_answer(99)
    shared_100 = shared_with_answer(100)
    assert len(shared_99) >= 600 and len(shared_100) >= 600
    assert echo.pasted_back(shared_99) == ()
    assert echo.pasted_back(shared_100) == ((0, len(shared_100)),)
    reflowed = ANSWER.replace(". ", ".\n")
    assert echo.pasted_back(reflowed) == ((0, len(reflowed)),)


def test_only_earlier_assistant_turns_of_the_same_conversation_count() -> None:
    echo = AssistantEcho()
    assert echo.pasted_back(ANSWER) == ()
    echo.add_assistant(ANSWER)
    assert echo.pasted_back(ANSWER) != ()
    assert AssistantEcho().pasted_back(ANSWER) == ()


def test_the_echo_remembers_a_bounded_amount_of_assistant_text() -> None:
    echo = AssistantEcho(history_limit=1000)
    echo.add_assistant(ANSWER)
    echo.add_assistant(" ".join(f"Another unrelated reply sentence {i}." for i in range(30)))
    assert echo.pasted_back(ANSWER) == ()
    with pytest.raises(ValueError, match="positive"):
        AssistantEcho(min_paragraph=0)


# --- notes: agent sections, pasted emails and tables ------------------------------------------


def note_payload(*texts: str, **changes) -> dict:
    return payload(
        provider="applenotes",
        kind="note",
        messages=[
            {"id": f"n{index}", "role": "user", "channel": "typed", "text": t}
            for index, t in enumerate(texts)
        ],
        **changes,
    )


def note_owner_text(text: str, record: RecordInfo = NOTE) -> list[str]:
    return [text[s.start : s.end] for s in run(text, record=record) if s.author == "owner"]


@pytest.mark.parametrize(
    "marked",
    [
        "Mine.\n\n## Summary (written by ChatGPT)\nAGENTTEXT here.",
        "This section is AI-generated.\nAITEXT here.",
        "Drafted by Claude: AGENTTEXT.",
        "AGENTTEXT, edited using Gemini last week.",
        "I wrote this with the help of ChatGPT. AGENTTEXT.",
        "As an AI language model, AGENTTEXT.",
        "AGENTTEXT (generated by an AI assistant)",
        "Summarised by GPT-4: AGENTTEXT",
        "a" * 6000 + "\nGenerated with Claude Code\n" + "AGENTTEXT",
    ],
)
def test_any_ai_marker_anywhere_in_a_note_makes_the_whole_note_not_owner(marked: str) -> None:
    assert note_owner_text(marked) == []
    assert rule_note_owner(ctx(marked, record=NOTE)) == []
    assert all(s.author != "owner" for s in run(marked, record=NOTE))


def test_a_marker_in_one_message_of_a_note_taints_every_message_of_the_record() -> None:
    record = record_info(
        note_payload("Perfectly ordinary words of mine.", "Later: written by ChatGPT, see above.")
    )
    assert record.ai_marker
    clean = "Perfectly ordinary words of mine."
    assert view(clean, run(clean, record=record))[0][:2] == ("ambiguous", "unknown")


@pytest.mark.parametrize(
    "plain",
    [
        "Poem written by my mother.",
        "Compiled by the build server last night.",
        "He is an assistant manager at the shop.",
        "Generated with love and patience.",
        "The ai in rain is not a marker; neither is said.",
    ],
)
def test_ordinary_authorship_words_do_not_taint_a_note(plain: str) -> None:
    assert note_owner_text(plain) == [plain]


def test_ai_markers_only_taint_notes_and_documents_not_chat_turns() -> None:
    text = "As an AI would say, this is my own typed remark."
    assert [text[s.start : s.end] for s in run(text, record=CHAT) if s.author == "owner"] == [text]


@pytest.mark.parametrize(
    "mail",
    [
        "From: Alice <a@example.com>\nSubject: Re: lease\n\nEMAILBODY ok",
        "To: me\nDate: Mon, 2 Jan 2026\n\nEMAILBODY ok",
        "-----Original Message-----\nEMAILBODY ok",
        "---------- Forwarded message ---------\nEMAILBODY ok",
        "Begin forwarded message:\n\nEMAILBODY ok",
        "On Mon, Jan 2, 2026 at 3:04 PM Alice <a@example.com> wrote:\nEMAILBODY ok",
    ],
)
def test_a_pasted_email_block_is_third_party_to_the_end_of_the_note(mail: str) -> None:
    text = f"My own preface to the thread.\n\n{mail}"
    spans = view(text, run(text, record=NOTE))
    assert spans[0] == ("note_owner", "owner", "My own preface to the thread.")
    assert all(a != "owner" for _, a, _ in spans[1:])
    assert spans[-1][:2] == ("pasted_block", "third_party")
    assert "EMAILBODY" in spans[-1][2]


def test_a_single_header_looking_line_is_not_an_email() -> None:
    text = "Subject: my plans for the weekend are fluid."
    assert rule_pasted_block(ctx(text)) == []


TABLE = "| Name | Cost |\n|---|---|\n| TABLEROW | 1 |"


def test_a_markdown_table_with_a_separator_row_is_third_party() -> None:
    text = f"Budget notes of mine.\n\n{TABLE}\n\nAnd my conclusion."
    assert view(text, run(text, record=NOTE)) == [
        ("note_owner", "owner", "Budget notes of mine."),
        ("unattributed", "unknown", "\n\n"),
        ("pasted_block", "third_party", TABLE),
        ("unattributed", "unknown", "\n\n"),
        ("note_owner", "owner", "And my conclusion."),
    ]
    aligned = "| a | b |\n| :--- | ---: |\n| 1 | 2 |"
    assert ranges(rule_pasted_block(ctx(aligned))) == [(0, len(aligned))]


def test_pipes_without_a_separator_row_are_not_a_table() -> None:
    assert rule_pasted_block(ctx("a | b | c\nd | e | f")) == []
    assert rule_pasted_block(ctx("one line | with a pipe")) == []
    assert rule_pasted_block(ctx("|---|---|")) == []


# --- g: notes default ------------------------------------------------------------------------


def test_a_note_body_is_owner_by_default_whatever_typed_chars_says() -> None:
    text = "Buy milk.\nCall the plumber about the leak."
    for typed in (0, 3, "all", None):
        assert view(text, run(text, record=NOTE, channel="typed", typed=typed)) == [
            ("note_owner", "owner", text)
        ]
    assert view(text, run(text, record=NOTE, channel="project", typed=0)) == [
        ("note_owner", "owner", text)
    ]


def test_a_pasted_quote_in_a_note_stays_third_party() -> None:
    text = "My thoughts.\n\n> pasted from a website\n\nMore thoughts."
    assert view(text, run(text, record=NOTE)) == [
        ("note_owner", "owner", "My thoughts."),
        ("unattributed", "unknown", "\n\n"),
        ("quote", "third_party", "> pasted from a website"),
        ("unattributed", "unknown", "\n\n"),
        ("note_owner", "owner", "More thoughts."),
    ]


def test_code_logs_and_injected_blocks_in_a_note_are_not_owner() -> None:
    text = (
        "Setup notes\n```\nmake all\n```\n2026-01-02 03:04:05 INFO done\n"
        "<system-reminder>x</system-reminder>\nEnd."
    )
    claimed = {span.rule for span in run(text, record=NOTE) if span.author != "owner"}
    assert {"code", "log_trace", "inline_injection"} <= claimed


def test_provider_metadata_marking_agent_origin_keeps_a_note_from_the_owner() -> None:
    text = "Summary an assistant wrote into the note."
    assert view(text, run(text, record=NOTE, meta={"generated_by": "ChatGPT"})) == [
        ("agent_family", "agent", text)
    ]
    assert rule_note_owner(ctx(text, record=NOTE, meta={"agent_authored": True})) == []


def test_note_rule_needs_an_authored_family_note_or_document_user_turn() -> None:
    text = "Body text."
    plan = RecordInfo(provider="documents", kind="plan", wing="authored", authored_family=True)
    assert rule_note_owner(ctx(text, record=plan)) == []
    assert rule_note_owner(ctx(text, record=CHAT)) == []
    assert rule_note_owner(ctx(text, record=NOTE, role="assistant")) == []
    drive = RecordInfo(provider="googledrive", kind="document", authored_family=True)
    assert rule_note_owner(ctx(text, record=drive)) == [(0, 10, "family=googledrive")]


def test_a_chat_user_turn_on_the_written_channel_is_not_owner() -> None:
    text = "An uploaded knowledge document filed as a user turn."
    assert view(text, run(text, channel="written", record=CHAT)) == [
        ("unattributed", "unknown", text)
    ]


def test_ai_written_skill_document_is_machine_and_a_plain_authored_document_is_owner() -> None:
    skill = record_info(payload(provenance={"source_file": "skills/example/SKILL.md"}))
    plain = record_info(payload(provenance={"source_file": "writing/essay.md"}))
    body = "When reviewing a change, check the tests first and summarise the risk."
    assert view(body, run(body, channel="project", typed=0, record=skill)) == [
        ("document_origin", "agent", body)
    ]
    assert view(body, run(body, channel="project", typed=0, record=plain)) == [
        ("note_owner", "owner", body)
    ]


def test_the_rule_table_has_one_documented_order() -> None:
    assert [(rule.id, rule.author) for rule in RULES] == [
        ("agent_family", "agent"),
        ("document_origin", "agent"),
        ("inline_injection", "agent"),
        ("typed_remainder", "agent"),
        ("code", "third_party"),
        ("quote", "third_party"),
        ("log_trace", "agent"),
        ("pasted_block", "third_party"),
        ("boilerplate", "third_party"),
        ("pasted_back", "third_party"),
        ("ambiguous", "unknown"),
        ("typed_prefix", "owner"),
        ("note_owner", "owner"),
    ]
    assert all(rule.description for rule in RULES)


# --- the partition property ------------------------------------------------------------------

WORDS = ["alpha", "beta", "gamma", "delta", "budget", "review", "notes", "plan", "cost", "risk"]


def prose(rng: random.Random) -> str:
    sentence = " ".join(rng.choice(WORDS) for _ in range(rng.randint(1, 12)))
    return sentence + rng.choice([".", "?", "", "!"])


def block(rng: random.Random) -> str:
    body = prose(rng)
    quoted = " ".join(rng.choice(WORDS) for _ in range(rng.randint(9, 14)))
    makers = [
        lambda: body,
        lambda: body,
        lambda: body,
        lambda: body,
        lambda: f"<system-reminder>{body}</system-reminder>",
        lambda: f"<command-name>/{rng.choice(WORDS)}</command-name>",
        lambda: f"<tool_result>{body}</tool_result>",
        lambda: f"<local-command-stdout>{body}",
        lambda: "<skill/>",
        lambda: "\n".join(f"> {prose(rng)}" for _ in range(rng.randint(1, 3))),
        lambda: f'"{quoted}"',
        lambda: f"“{quoted}”",
        lambda: f"```\n{body}\n```",
        lambda: f"```python\n{body}",
        lambda: "\n".join(f"    {prose(rng)}" for _ in range(rng.randint(1, 3))),
        lambda: f"2026-01-0{rng.randint(1, 9)} 03:04:0{rng.randint(0, 9)} INFO {body}",
        lambda: (
            'Traceback (most recent call last):\n  File "a.py", line 1\n    x()\nValueError: '
            + body
        ),
        lambda: "\n".join(f"ERROR: {prose(rng)}" for _ in range(rng.randint(1, 4))),
        lambda: " " * rng.randint(1, 4),
        lambda: "a < b > c",
        lambda: "\t" + body,
        lambda: f"<pasted_content>{body}</pasted_content>",
        lambda: f"<unknown-tag>{body}</unknown-tag>",
        lambda: f'He said "{quoted} and left.',
        lambda: "From: Pat <p@example.invalid>\nSubject: Re: " + body + "\n\n" + prose(rng),
        lambda: "| a | b |\n|---|---|\n| " + body + " | 1 |",
        lambda: f"> {body}\n{prose(rng)}",
    ]
    return rng.choice(makers)()


def check_partition(context: MessageContext, spans: list[AttributedSpan]) -> None:
    text = context.text
    length = len(text)
    assert not any(partition_problems(text, spans).values())
    owner_of = [None] * length
    covered = [0] * length
    for span in spans:
        assert span.profile_eligible is False
        assert span.text_digest == text_digest(text)
        for index in range(span.start, span.end):
            covered[index] += 1
            owner_of[index] = span
    assert covered == [1] * length
    raw = {}
    for rule in RULES:
        chars: set[int] = set()
        for start, end, _ in rule.claims(context):
            chars.update(range(max(0, start), min(length, end)))
        raw[rule.id] = chars
    non_owner = set().union(*(raw[rule.id] for rule in RULES if rule.author != "owner"))
    owner_raw = set().union(*(raw[rule.id] for rule in RULES if rule.author == "owner"))
    for span in spans:
        chars = set(range(span.start, span.end))
        if span.author == "owner":
            assert span.rule in {"typed_prefix", "note_owner"}
            assert not chars & non_owner
            assert chars <= raw[span.rule]
            assert text[span.start].strip()
            assert text[span.end - 1].strip()
        if span.rule == "unattributed":
            assert span.author == "unknown"
            assert not chars & non_owner
        else:
            assert chars <= raw[span.rule]
    for index in range(length):
        claimed = index in non_owner or index in owner_raw
        if not claimed:
            assert owner_of[index].rule == "unattributed"
        elif index not in non_owner and text[index].strip():
            assert owner_of[index].author == "owner"


def test_partition_covers_every_character_once_and_owner_never_outranks_a_higher_rule() -> None:
    rng = random.Random(20261105)
    records = [
        CHAT,
        CHAT,
        CHAT,
        CLI,
        NOTE,
        NOTE,
        RecordInfo(provider="claudecode", kind="session", session_kind="subagent"),
        RecordInfo(provider="documents", kind="document", wing="authored", authored_family=True),
        record_info(payload(provenance={"source_file": "skills/x/SKILL.md"})),
    ]
    owner_spans = other_spans = 0
    for _ in range(200):
        pieces = [
            block(rng) + rng.choice(["\n", "\n\n", " ", ""]) for _ in range(rng.randint(1, 9))
        ]
        text = "".join(pieces)
        if not text:
            continue
        length = len(text)
        boilerplate = ()
        if rng.random() < 0.3:
            starts = [rng.randrange(length) for _ in range(rng.randint(1, 3))]
            boilerplate = tuple((s, rng.randint(s + 1, length)) for s in starts)
        context = MessageContext(
            text=text,
            role=rng.choice(["user"] * 16 + ["assistant", "tool", "system", ""]),
            channel=rng.choice(["typed"] * 10 + ["written", "project", "reply", "system"]),
            typed_chars=rng.choice(
                [length, length, length, rng.randint(0, length + 3), None, -1, length // 2]
            ),
            meta={"agent_authored": True} if rng.random() < 0.05 else {},
            record=rng.choice(records),
            boilerplate=boilerplate,
        )
        spans = attribute_message(
            text,
            role=context.role,
            channel=context.channel,
            typed_chars=context.typed_chars,
            meta=context.meta,
            record=context.record,
            boilerplate=context.boilerplate,
        )
        check_partition(context, spans)
        again = attribute_message(
            text,
            role=context.role,
            channel=context.channel,
            typed_chars=context.typed_chars,
            meta=context.meta,
            record=context.record,
            boilerplate=context.boilerplate,
        )
        assert again == spans
        owner_spans += sum(span.author == "owner" for span in spans)
        other_spans += sum(span.author not in {"owner", "unknown"} for span in spans)
    # The generator must exercise both sides, or the property would pass vacuously.
    assert owner_spans >= 30
    assert other_spans >= 100
