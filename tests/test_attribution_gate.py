"""The owner-review ledger, the per-(rule, stratum) precision gate and the review sample."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from conversation_corpus_engine.attribution import (
    ATTRIBUTION_SCHEMA,
    attributed_spans,
    owner_profile_eligible,
    text_digest,
)
from conversation_corpus_engine.attribution_rules import (
    EXCERPT_HEAD,
    EXCERPT_TAIL,
    GATE_MIN_PRECISION_PERCENT,
    GATE_MIN_REVIEWED,
    LEDGER_SCHEMA,
    RULE_BY_ID,
    RULES,
    RULES_VERSION,
    SAMPLE_PER_STRATUM,
    SAMPLE_SCHEMA,
    STRATA,
    AttributedSpan,
    RecordInfo,
    ReviewSampler,
    ReviewSummary,
    StratumReview,
    append_ledger,
    attribute_message,
    eligible_strata,
    ledger_row,
    parse_ledger,
    read_ledger,
    review_excerpt,
    review_statistics,
    stratum_of,
    to_annotation,
    validate_ledger_row,
)

CHAT = RecordInfo(provider="chatgpt", kind="chat", wing="ai")
STAMP = "2026-01-02T03:04:05Z"
OWNER_ID = "owner-1"
SHORT = "Typed words in my own voice."
MEDIUM = ("A medium typed paragraph about the quiet harbour at dawn. " * 11).strip()
LARGE = ("A large typed passage about long walks and the weather. " * 45).strip()


def spans_of(text: str) -> list[AttributedSpan]:
    return attribute_message(text, role="user", channel="typed", typed_chars=len(text), record=CHAT)


def verdict_rows(
    rule: str,
    verdicts: list[str],
    *,
    stratum: str = "S",
    start: int = 0,
    version: str = RULES_VERSION,
) -> list[dict]:
    return [
        ledger_row(
            f"{start + index:032x}",
            rule,
            verdict,
            OWNER_ID,
            STAMP,
            stratum=stratum,
            rules_version=version,
        )
        for index, verdict in enumerate(verdicts)
    ]


def matched_of(rows: list[dict]) -> dict[str, tuple[str, str]]:
    """The scan evidence that every row's span exists under the rule and stratum it names."""
    return {row["span_id"]: (row["rule"], row["stratum"]) for row in rows if "stratum" in row}


def summarize(rows: list[dict], **overrides) -> ReviewSummary:
    options = {"matched": matched_of(rows), "owner": OWNER_ID} | overrides
    return review_statistics(rows, **options)


def gate(rows: list[dict], rule: str = "typed_prefix", stratum: str = "S") -> StratumReview:
    return summarize(rows).strata[(rule, stratum)]


# --- strata ----------------------------------------------------------------------------------


def test_strata_split_spans_by_length_at_the_documented_boundaries() -> None:
    assert STRATA == ("S", "M", "L")
    assert [stratum_of(n) for n in (1, 240, 241, 2000, 2001, 10**6)] == [
        "S",
        "S",
        "M",
        "M",
        "L",
        "L",
    ]
    for text, expected in ((SHORT, "S"), (MEDIUM, "M"), (LARGE, "L")):
        (span,) = spans_of(text)
        assert span.stratum == expected == stratum_of(len(text))


# --- the gate --------------------------------------------------------------------------------


def test_gate_constants_state_the_documented_thresholds() -> None:
    assert GATE_MIN_REVIEWED == 100
    assert GATE_MIN_PRECISION_PERCENT == 95


def test_no_stratum_of_any_rule_is_eligible_without_a_ledger() -> None:
    summary = summarize([])
    assert set(summary.strata) == {(rule.id, stratum) for rule in RULES for stratum in STRATA}
    assert all(not review.eligible and review.reviewed == 0 for review in summary.strata.values())
    assert eligible_strata(summary) == frozenset()


def test_one_hundred_identical_yes_rows_for_one_span_are_one_review() -> None:
    rows = [
        ledger_row("a" * 32, "typed_prefix", "yes", OWNER_ID, STAMP, stratum="S")
        for _ in range(100)
    ]
    review = gate(rows)
    assert review.reviewed == 1 and review.confirmed == 1
    assert not review.eligible


@pytest.mark.parametrize("stratum", STRATA)
@pytest.mark.parametrize(
    ("yes", "no", "unsure", "eligible"),
    [
        (99, 0, 0, False),
        (100, 0, 0, True),
        (95, 5, 0, True),
        (94, 6, 0, False),
        (95, 0, 5, True),
        (94, 0, 6, False),
        (95, 0, 1, False),
        (181, 9, 0, True),
        (180, 10, 0, False),
        (0, 100, 0, False),
    ],
)
def test_gate_needs_one_hundred_reviewed_spans_and_ninety_five_percent_confirmed(
    stratum: str, yes: int, no: int, unsure: int, eligible: bool
) -> None:
    rows = verdict_rows(
        "typed_prefix", ["yes"] * yes + ["no"] * no + ["unsure"] * unsure, stratum=stratum
    )
    review = gate(rows, stratum=stratum)
    assert review.reviewed == yes + no + unsure
    assert (review.confirmed, review.rejected, review.unsure) == (yes, no, unsure)
    assert review.eligible is eligible
    assert review.precision == pytest.approx(yes / (yes + no + unsure))


def test_a_stratum_is_judged_alone_so_short_reviews_never_vouch_for_long_spans() -> None:
    rows = verdict_rows("typed_prefix", ["yes"] * 110, stratum="S")
    summary = summarize(rows)
    assert eligible_strata(summary) == frozenset({("typed_prefix", "S")})
    assert not summary.strata[("typed_prefix", "M")].eligible
    assert not summary.strata[("typed_prefix", "L")].eligible
    short, long_one = spans_of(SHORT)[0], spans_of(MEDIUM)[0]
    short_note = to_annotation(SHORT, [short], owner_id="rob", eligible=eligible_strata(summary))
    long_note = to_annotation(MEDIUM, [long_one], owner_id="rob", eligible=eligible_strata(summary))
    assert short_note["spans"][0]["profile_eligible"] is True
    assert long_note["spans"][0]["profile_eligible"] is False


def test_after_one_hundred_reviewed_medium_rows_only_the_medium_stratum_opens() -> None:
    rows = verdict_rows("typed_prefix", ["yes"] * 110, stratum="S") + verdict_rows(
        "typed_prefix", ["yes"] * 100, stratum="M", start=10_000
    )
    eligible = eligible_strata(summarize(rows))
    assert eligible == frozenset({("typed_prefix", "S"), ("typed_prefix", "M")})
    for text, expected in ((SHORT, True), (MEDIUM, True), (LARGE, False)):
        note = to_annotation(text, spans_of(text), owner_id="rob", eligible=eligible)
        assert note["spans"][0]["profile_eligible"] is expected


def test_the_same_stratum_of_another_rule_does_not_help() -> None:
    rows = verdict_rows("note_owner", ["yes"] * 100, stratum="M")
    assert eligible_strata(summarize(rows)) == frozenset({("note_owner", "M")})
    note = to_annotation(
        MEDIUM, spans_of(MEDIUM), owner_id="rob", eligible=eligible_strata(summarize(rows))
    )
    assert note["spans"][0]["profile_eligible"] is False


def test_the_last_verdict_for_a_span_wins_within_its_stratum() -> None:
    rows = verdict_rows("typed_prefix", ["yes"] * 100)
    rows += [ledger_row(f"{0:032x}", "typed_prefix", "no", OWNER_ID, STAMP, stratum="S")]
    review = gate(rows)
    assert (review.reviewed, review.confirmed, review.rejected) == (100, 99, 1)
    assert review.eligible
    rows += [
        ledger_row(f"{index:032x}", "typed_prefix", "no", OWNER_ID, STAMP, stratum="S")
        for index in range(1, 6)
    ]
    assert (gate(rows).confirmed, gate(rows).rejected) == (94, 6)
    assert not gate(rows).eligible
    recovered = [
        *rows,
        ledger_row(f"{1:032x}", "typed_prefix", "yes", OWNER_ID, STAMP, stratum="S"),
    ]
    assert (gate(recovered).confirmed, gate(recovered).rejected) == (95, 5)
    assert gate(recovered).eligible


def test_rows_from_another_rules_version_or_an_older_schema_are_ignored_not_counted() -> None:
    rows = verdict_rows("typed_prefix", ["yes"] * 100, version="cce.attribution-rules@0")
    legacy = {
        "schema": "cce.attribution-review-ledger.v1",
        "rules_version": RULES_VERSION,
        "span_id": "c" * 32,
        "rule": "typed_prefix",
        "verdict": "yes",
        "reviewer": "r",
        "reviewed_at": STAMP,
    }
    summary = summarize([*rows, validate_ledger_row(legacy)])
    assert summary.strata[("typed_prefix", "S")].reviewed == 0
    assert (summary.rows, summary.stale_rows) == (101, 101)
    assert eligible_strata(summary) == frozenset()


def test_eligible_strata_lists_only_what_passes() -> None:
    rows = verdict_rows("typed_prefix", ["yes"] * 100) + verdict_rows(
        "note_owner", ["yes"] * 90 + ["no"] * 10, start=1000
    )
    assert eligible_strata(summarize(rows)) == frozenset({("typed_prefix", "S")})


def test_one_hundred_fabricated_span_ids_count_for_nothing() -> None:
    rows = verdict_rows("typed_prefix", ["yes"] * 100)
    summary = summarize(rows, matched={})
    review = summary.strata[("typed_prefix", "S")]
    assert (review.reviewed, review.confirmed, review.eligible) == (0, 0, False)
    assert summary.orphan_rows == 100 and summary.non_owner_rows == 0
    assert eligible_strata(summary) == frozenset()


def test_a_row_whose_rule_or_stratum_was_edited_is_an_orphan() -> None:
    rows = verdict_rows("typed_prefix", ["yes"] * 100)
    real = {row["span_id"]: ("code", "S") for row in rows}
    edited_rule = summarize(rows, matched=real)
    assert edited_rule.orphan_rows == 100 and not edited_rule.strata[("typed_prefix", "S")].reviewed
    real = {row["span_id"]: ("typed_prefix", "M") for row in rows}
    edited_stratum = summarize(rows, matched=real)
    assert edited_stratum.orphan_rows == 100
    assert not eligible_strata(edited_stratum)
    partly = summarize(rows, matched=matched_of(rows[:60]))
    assert partly.orphan_rows == 40 and partly.strata[("typed_prefix", "S")].reviewed == 60


def test_only_rows_reviewed_by_the_owner_identity_count() -> None:
    owner_rows = verdict_rows("typed_prefix", ["yes"] * 60)
    agent_rows = [
        ledger_row(f"{index:032x}", "typed_prefix", "yes", "agent:codex", STAMP, stratum="S")
        for index in range(1000, 1060)
    ]
    rows = owner_rows + agent_rows
    summary = summarize(rows)
    assert summary.strata[("typed_prefix", "S")].reviewed == 60
    assert summary.non_owner_rows == 60 and summary.non_owner_reviewers == {"agent:codex": 60}
    assert not eligible_strata(summary)
    assert (
        summarize(agent_rows + verdict_rows("typed_prefix", ["yes"] * 100, start=5000))
        .strata[("typed_prefix", "S")]
        .eligible
    )
    nobody = summarize(rows, owner=None)
    assert nobody.non_owner_rows == 120 and not any(r.reviewed for r in nobody.strata.values())


def test_review_statistics_needs_scan_evidence_and_an_owner_identity() -> None:
    with pytest.raises(TypeError):
        review_statistics([])  # type: ignore[call-arg]


def test_a_row_is_stale_before_it_is_anything_else() -> None:
    rows = verdict_rows("typed_prefix", ["yes"] * 10, version="cce.attribution-rules@0")
    summary = summarize(rows, matched={}, owner=None)
    assert (summary.stale_rows, summary.orphan_rows, summary.non_owner_rows) == (10, 0, 0)


def test_a_span_id_changes_with_the_text_so_an_edited_message_invalidates_its_reviews() -> None:
    before = spans_of("Typed words in my own voice.")[0]
    after = spans_of("Typed words in my own voice!")[0]
    assert before.span_id != after.span_id
    row = ledger_row(before.span_id, before.rule, "yes", OWNER_ID, STAMP, stratum=before.stratum)
    scanned = {after.span_id: (after.rule, after.stratum)}
    assert summarize([row], matched=scanned).orphan_rows == 1


def test_a_row_carries_its_stratum_and_refuses_unknown_ones() -> None:
    row = ledger_row("b" * 32, "quote", "yes", OWNER_ID, STAMP, stratum="L")
    assert row["schema"] == LEDGER_SCHEMA and row["stratum"] == "L"
    for bad in ("X", "", None, "s", 1):
        with pytest.raises(ValueError, match="stratum"):
            ledger_row("b" * 32, "quote", "yes", OWNER_ID, STAMP, stratum=bad)
    missing = {key: value for key, value in row.items() if key != "stratum"}
    with pytest.raises(ValueError, match="keys"):
        validate_ledger_row(missing)


def test_ledger_rows_are_validated_strictly_with_line_numbers() -> None:
    good = json.dumps(ledger_row("b" * 32, "quote", "yes", OWNER_ID, STAMP, stratum="S"))
    assert len(parse_ledger([good, "", "   ", good])) == 2
    base = json.loads(good)
    bad = {
        "not json": "{oops",
        "not an object": "[1]",
        "schema": json.dumps({**base, "schema": "x"}),
        "verdict": json.dumps({**base, "verdict": "maybe"}),
        "reviewer": json.dumps({**base, "reviewer": " "}),
        "time": json.dumps({**base, "reviewed_at": "2026-01-02 03:04:05"}),
        "span id": json.dumps({**base, "span_id": "xyz"}),
        "unknown rule": json.dumps({**base, "rule": "nope"}),
        "extra": json.dumps({**base, "note": "x"}),
    }
    for name, line in bad.items():
        with pytest.raises(ValueError, match=r"ledger line 3"):
            parse_ledger([good, "", line])
        assert name


def test_a_span_cannot_be_reviewed_under_two_different_rules_or_strata() -> None:
    first = json.dumps(ledger_row("c" * 32, "quote", "yes", OWNER_ID, STAMP, stratum="S"))
    for rule, stratum in (("code", "S"), ("quote", "M")):
        second = json.dumps(ledger_row("c" * 32, rule, "yes", OWNER_ID, STAMP, stratum=stratum))
        with pytest.raises(ValueError, match="ledger line 2"):
            parse_ledger([first, second])


def test_ledger_row_refuses_bad_input_and_never_reads_the_clock() -> None:
    for kwargs in (
        {"verdict": "y"},
        {"reviewer": ""},
        {"reviewed_at": "yesterday"},
        {"span_id": "short"},
        {"rule": "unattributed"},
    ):
        arguments = {
            "span_id": "d" * 32,
            "rule": "quote",
            "verdict": "yes",
            "reviewer": "r",
            "reviewed_at": STAMP,
            "stratum": "S",
        } | kwargs
        with pytest.raises(ValueError, match="ledger"):
            ledger_row(**arguments)


def test_ledger_is_append_only_jsonl_and_refuses_symlinks(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    first = verdict_rows("quote", ["yes", "no"])
    assert append_ledger(path, first) == 2
    assert append_ledger(path, verdict_rows("quote", ["yes"], start=5)) == 1
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["span_id"] for line in lines] == [
        f"{0:032x}",
        f"{1:032x}",
        f"{5:032x}",
    ]
    assert read_ledger(path)[:2] == first
    before = path.read_bytes()
    with pytest.raises(ValueError, match="ledger"):
        append_ledger(path, [{**first[0], "verdict": "bogus"}])
    assert path.read_bytes() == before
    link = tmp_path / "link.jsonl"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="symlink"):
        append_ledger(link, first)
    with pytest.raises(ValueError, match="symlink"):
        read_ledger(link)
    with pytest.raises(FileNotFoundError):
        read_ledger(tmp_path / "missing.jsonl")


# --- annotations in the existing cce.span-attribution.v1 form ---------------------------------

MIXED = "My own words.\n\n> a quoted line\n\n<system-reminder>x</system-reminder> and more of mine."


def annotate(text: str, **kwargs) -> tuple[list[AttributedSpan], dict]:
    spans = spans_of(text)
    annotation = to_annotation(
        text,
        spans,
        owner_id=kwargs.get("owner_id"),
        eligible=kwargs.get("eligible", frozenset()),
    )
    return spans, annotation


def test_annotation_validates_against_the_existing_contract_and_is_never_eligible_by_default() -> (
    None
):
    spans, annotation = annotate(MIXED, owner_id="rob")
    assert annotation["schema"] == ATTRIBUTION_SCHEMA
    assert annotation["text_digest"] == text_digest(MIXED)
    checked = attributed_spans(MIXED, annotation)
    assert [(s["start"], s["end"], s["author"]) for s in checked if s["author"] != "unknown"] == [
        (span.start, span.end, span.author) for span in spans if span.author != "unknown"
    ]
    assert all(span["profile_eligible"] is False for span in checked)
    assert not any(owner_profile_eligible(span, "rob") for span in checked)
    owner = next(span for span in checked if span["author"] == "owner")
    assert owner["author_id"] == "rob" and owner["assertion_status"] == "asserted"
    assert owner["evidence"][0] == f"{RULES_VERSION}:rule:typed_prefix"
    assert "stratum:S" in owner["evidence"]
    assert any(item.startswith("span:") for item in owner["evidence"])
    assert all(span["author_id"] is None for span in checked if span["author"] != "owner")


def test_unknown_spans_are_left_to_the_contract_to_fill() -> None:
    _, annotation = annotate(MIXED, owner_id="rob")
    assert all(span["author"] != "unknown" for span in annotation["spans"])
    covered = sum(span["end"] - span["start"] for span in annotation["spans"])
    assert covered < len(MIXED)
    assert sum(len(s["text"]) for s in attributed_spans(MIXED, annotation)) == len(MIXED)


def test_only_owner_spans_of_a_gate_passing_stratum_with_an_owner_id_become_eligible() -> None:
    eligible = frozenset({("typed_prefix", "S"), ("quote", "S")})
    _, annotation = annotate(MIXED, owner_id="rob", eligible=eligible)
    checked = attributed_spans(MIXED, annotation)
    flagged = {
        span["evidence"][0].split(":rule:")[1]: span["profile_eligible"]
        for span in checked
        if span["author"] != "unknown"
    }
    assert flagged["typed_prefix"] is True
    assert flagged["quote"] is False
    assert flagged["inline_injection"] is False
    assert {span["text"] for span in checked if owner_profile_eligible(span, "rob")} == {
        "My own words.",
        "and more of mine.",
    }
    _, anonymous = annotate(MIXED, eligible=frozenset({("typed_prefix", "S")}))
    assert not any(span["profile_eligible"] for span in anonymous["spans"])
    _, other_rule = annotate(MIXED, owner_id="rob", eligible=frozenset({("note_owner", "S")}))
    assert not any(span["profile_eligible"] for span in other_rule["spans"])


def test_annotation_refuses_spans_of_a_different_text() -> None:
    spans = spans_of("some words")
    with pytest.raises(ValueError, match="partition"):
        to_annotation("other words", spans)


# --- the review sample -----------------------------------------------------------------------


def offer_all(sampler: ReviewSampler, texts: list[str]) -> None:
    for text in texts:
        for span in spans_of(text):
            sampler.offer(span, text[span.start : span.end])


def many_quotes(count: int) -> list[str]:
    return [f"> quoted line number {index} from elsewhere" for index in range(count)]


def varied(base: str, count: int) -> list[str]:
    return [f"Entry {index}. {base}" for index in range(count)]


def test_sample_draws_up_to_n_distinct_spans_per_rule_and_stratum() -> None:
    sampler = ReviewSampler(per_stratum=10)
    offer_all(sampler, [*many_quotes(50), *many_quotes(5)])
    offer_all(sampler, [f"owner sentence number {index}" for index in range(40)])
    offer_all(sampler, varied(MEDIUM, 25) + varied(LARGE, 25))
    rows = sampler.rows()
    counts = {(r, s): 0 for r in ("quote", "typed_prefix") for s in STRATA}
    for row in rows:
        counts[(row["rule"], row["stratum"])] += 1
    assert counts[("quote", "S")] == 10 and counts[("quote", "M")] == 0
    assert counts[("typed_prefix", "S")] == 10
    assert counts[("typed_prefix", "M")] == 10 and counts[("typed_prefix", "L")] == 10
    assert len({row["text"] for row in rows}) == len(rows)


def test_medium_and_large_spans_are_shown_as_a_bounded_excerpt_with_the_exact_length() -> None:
    sampler = ReviewSampler(per_stratum=1)
    offer_all(sampler, [MEDIUM, LARGE])
    by_stratum = {row["stratum"]: row for row in sampler.rows()}
    for stratum, text in (("M", MEDIUM), ("L", LARGE)):
        row = by_stratum[stratum]
        omitted = len(text) - EXCERPT_HEAD - EXCERPT_TAIL
        assert row["length"] == len(text) and row["excerpted"] is True
        assert row["text"] == (
            text[:EXCERPT_HEAD] + f" [...{omitted} chars omitted...] " + text[-EXCERPT_TAIL:]
        )
        assert len(row["text"]) < EXCERPT_HEAD + EXCERPT_TAIL + 40
    assert (EXCERPT_HEAD, EXCERPT_TAIL) == (240, 120)


def test_excerpt_helper_shows_up_to_three_hundred_sixty_characters_whole() -> None:
    assert review_excerpt("x" * 360) == ("x" * 360, False)
    shown, cut = review_excerpt("y" * 361)
    assert cut is True and shown.endswith("y" * 120) and "[...1 chars omitted...]" in shown


def test_sample_rows_show_only_the_span_its_rule_and_a_blank_verdict() -> None:
    sampler = ReviewSampler(per_stratum=3)
    offer_all(sampler, many_quotes(3))
    (row, *_) = sampler.rows()
    assert set(row) == {
        "schema",
        "rules_version",
        "span_id",
        "rule",
        "stratum",
        "claim",
        "rule_description",
        "length",
        "excerpted",
        "text",
        "verdict",
        "partial",
    }
    assert row["schema"] == SAMPLE_SCHEMA and row["verdict"] is None and row["partial"] is False
    assert (row["rule"], row["stratum"], row["claim"]) == ("quote", "S", "third_party")
    assert row["rule_description"] == RULE_BY_ID["quote"].description
    assert row["text"].startswith("> quoted line number") and row["excerpted"] is False
    assert all(item["partial"] is True for item in sampler.rows(partial=True))


def test_sample_is_deterministic_and_independent_of_offer_order() -> None:
    texts = many_quotes(60) + [f"I typed sentence {index} myself" for index in range(60)]
    texts += varied(MEDIUM, 30)
    first = ReviewSampler(per_stratum=7)
    offer_all(first, texts)
    shuffled = list(texts)
    random.Random(7).shuffle(shuffled)
    second = ReviewSampler(per_stratum=7)
    offer_all(second, shuffled)
    assert first.rows() == second.rows()
    different = ReviewSampler(per_stratum=7, seed="another-seed")
    offer_all(different, texts)
    assert different.rows() != first.rows()
    assert [(row["rule"], row["stratum"]) for row in first.rows()] == (
        [("quote", "S")] * 7 + [("typed_prefix", "S")] * 7 + [("typed_prefix", "M")] * 7
    )


def test_sample_skips_already_reviewed_spans_and_whitespace_and_unknown_spans() -> None:
    sampler = ReviewSampler(per_stratum=100)
    offer_all(sampler, many_quotes(5))
    reviewed = {row["span_id"] for row in sampler.rows()[:2]}
    again = ReviewSampler(per_stratum=100, exclude=reviewed)
    offer_all(again, many_quotes(5))
    assert {row["span_id"] for row in again.rows()}.isdisjoint(reviewed)
    assert len(again.rows()) == 3
    empty = ReviewSampler()
    for text in ("   \n\n  ", "plain words on an unclaimed channel"):
        for span in attribute_message(text, role="user", channel="project", record=CHAT):
            empty.offer(span, text[span.start : span.end])
    assert empty.rows() == []


def test_sampler_validates_its_parameters() -> None:
    for kwargs in ({"per_stratum": 0}, {"per_stratum": True}):
        with pytest.raises(ValueError, match="per_stratum"):
            ReviewSampler(**kwargs)
    assert SAMPLE_PER_STRATUM == 100
