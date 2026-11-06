"""The owner-review ledger, the per-(rule, stratum) precision gate and the review sample."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import random
from pathlib import Path

import pytest

from conversation_corpus_engine import attribution_rules
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
    LEDGER_LOCK_SECONDS,
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
    append_rows,
    attribute_message,
    eligible_strata,
    parse_ledger,
    parse_sample,
    read_ledger,
    review_excerpt,
    review_statistics,
    sample_digest,
    sample_header,
    sample_text,
    stratum_of,
    to_annotation,
    validate_ledger_row,
)
from conversation_corpus_engine.attribution_rules import (
    ledger_row as build_ledger_row,
)

CHAT = RecordInfo(provider="chatgpt", kind="chat", wing="ai")
STAMP = "2026-01-02T03:04:05Z"
OWNER_ID = "owner-1"
SAMPLE_DIGEST = "d" * 64
# Nesting deep enough to exhaust the JSON parser's recursion on every supported Python.
DEEP = 1_500_000
SHORT = "Typed words in my own voice."
MEDIUM = ("A medium typed paragraph about the quiet harbour at dawn. " * 11).strip()
LARGE = ("A large typed passage about long walks and the weather. " * 45).strip()


def spans_of(text: str) -> list[AttributedSpan]:
    return attribute_message(text, role="user", channel="typed", typed_chars=len(text), record=CHAT)


def text_hash(span_id: str) -> str:
    """The text hash a test row claims to have answered (the tests' scans agree with it)."""
    return hashlib.sha256(span_id.encode()).hexdigest()


def ledger_row(
    span_id: str,
    rule: str,
    verdict: str,
    reviewer: str,
    reviewed_at: str,
    *,
    stratum: str,
    **extra,
) -> dict:
    """A current-schema row of a one-row-per-span sample, unless the test says otherwise."""
    options = {"sample_digest": SAMPLE_DIGEST, "text_sha256": text_hash(span_id), "drawn": 1}
    return build_ledger_row(
        span_id, rule, verdict, reviewer, reviewed_at, stratum=stratum, **(options | extra)
    )


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


def matched_of(rows: list[dict]) -> dict[str, tuple[str, str, str]]:
    """The scan evidence that every row's span exists under the rule and stratum it names, with
    the very text the row answered."""
    return {
        row["span_id"]: (row["rule"], row["stratum"], row["text_sha256"])
        for row in rows
        if "text_sha256" in row
    }


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
    legacy_with_stratum = {
        **legacy,
        "schema": "cce.attribution-review-ledger.v2",
        "span_id": "e" * 32,
        "stratum": "S",
    }
    summary = summarize(
        [*rows, validate_ledger_row(legacy), validate_ledger_row(legacy_with_stratum)]
    )
    assert summary.strata[("typed_prefix", "S")].reviewed == 0
    assert (summary.rows, summary.stale_rows) == (102, 102)
    assert eligible_strata(summary) == frozenset()
    assert LEDGER_SCHEMA == "cce.attribution-review-ledger.v3"
    assert SAMPLE_SCHEMA == "cce.attribution-review-sample.v3"


PREVIOUS_VERSION = "cce.attribution-rules@2"


def test_rows_written_under_the_previous_rules_version_are_stale_and_never_counted() -> None:
    assert RULES_VERSION == "cce.attribution-rules@3"
    old = verdict_rows("typed_prefix", ["yes"] * 100, version=PREVIOUS_VERSION)
    summary = summarize(old)
    assert (summary.rows, summary.stale_rows) == (100, 100)
    assert summary.strata[("typed_prefix", "S")].reviewed == 0
    assert eligible_strata(summary) == frozenset()
    mixed = old + verdict_rows("typed_prefix", ["yes"] * 100, start=5000)
    assert summarize(mixed).stale_rows == 100 and gate(mixed).reviewed == 100


def test_span_ids_and_annotation_evidence_are_bound_to_the_rules_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (span,) = spans_of(SHORT)
    current = span.span_id
    evidence = to_annotation(SHORT, [span], owner_id="rob")["spans"][0]["evidence"]
    assert evidence[0] == f"{RULES_VERSION}:rule:typed_prefix"
    monkeypatch.setattr(attribution_rules, "RULES_VERSION", PREVIOUS_VERSION)
    assert span.span_id != current


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


def test_malformed_ledger_lines_are_quarantined_with_their_line_numbers_not_raised() -> None:
    good = json.dumps(ledger_row("b" * 32, "quote", "yes", OWNER_ID, STAMP, stratum="S"))
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
    clean = parse_ledger([good, "", "   ", good])
    assert len(clean.rows) == 2 and clean.quarantined == []
    for name, line in bad.items():
        contents = parse_ledger([good, "", line, good])
        assert len(contents.rows) == 2, name
        assert [number for number, _ in contents.quarantined] == [3], name
        assert contents.quarantined[0][1], name


def test_conflicting_rows_of_one_reviewer_are_quarantined_and_never_counted() -> None:
    rows = verdict_rows("typed_prefix", ["yes"] * 100)
    clash = ledger_row(rows[0]["span_id"], "code", "yes", OWNER_ID, STAMP, stratum="S")
    other_stratum = ledger_row(
        rows[1]["span_id"], "typed_prefix", "yes", OWNER_ID, STAMP, stratum="M"
    )
    summary = summarize([*rows, clash, other_stratum], matched=matched_of(rows))
    assert summary.quarantined_rows == 4
    assert summary.strata[("typed_prefix", "S")].reviewed == 98
    assert not summary.strata[("typed_prefix", "S")].eligible
    assert not any(review.reviewed for key, review in summary.strata.items() if key[0] == "code")


def test_a_conflicting_row_from_a_non_owner_cannot_poison_the_owners_reviews() -> None:
    rows = verdict_rows("typed_prefix", ["yes"] * 100)
    planted = ledger_row(rows[0]["span_id"], "code", "no", "agent:codex", STAMP, stratum="S")
    summary = summarize([*rows, planted], matched=matched_of(rows))
    assert summary.quarantined_rows == 0 and summary.non_owner_rows == 1
    assert summary.strata[("typed_prefix", "S")].eligible


def test_answering_a_span_again_under_the_same_rule_supersedes_the_earlier_answer() -> None:
    rows = verdict_rows("typed_prefix", ["no"] * 3)
    again = [ledger_row(rows[0]["span_id"], "typed_prefix", "yes", OWNER_ID, STAMP, stratum="S")]
    summary = summarize([*rows, *again])
    review = summary.strata[("typed_prefix", "S")]
    assert (review.reviewed, review.confirmed, review.rejected) == (3, 1, 2)
    assert summary.superseded_rows == 1 and summary.quarantined_rows == 0


def test_a_span_cannot_be_appended_under_two_different_rules_or_strata(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    first = ledger_row("c" * 32, "quote", "yes", OWNER_ID, STAMP, stratum="S")
    append_ledger(ledger, [first])
    before = ledger.read_bytes()
    for rule, stratum in (("code", "S"), ("quote", "M")):
        second = ledger_row("c" * 32, rule, "yes", OWNER_ID, STAMP, stratum=stratum)
        with pytest.raises(ValueError, match="conflict"):
            append_ledger(ledger, [second])
        assert ledger.read_bytes() == before
    batch = [
        ledger_row("d" * 32, "quote", "yes", OWNER_ID, STAMP, stratum="S"),
        ledger_row("d" * 32, "code", "yes", OWNER_ID, STAMP, stratum="S"),
    ]
    with pytest.raises(ValueError, match="conflict"):
        append_ledger(tmp_path / "other.jsonl", batch)
    assert not (tmp_path / "other.jsonl").exists()
    same_again = ledger_row("c" * 32, "quote", "no", OWNER_ID, STAMP, stratum="S")
    assert append_ledger(ledger, [same_again]) == 1
    elsewhere = ledger_row("c" * 32, "code", "yes", "agent:codex", STAMP, stratum="S")
    assert append_ledger(ledger, [elsewhere]) == 1


def test_a_ledger_with_malformed_or_conflicting_lines_can_still_be_appended_to(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger.jsonl"
    good = json.dumps(ledger_row("e" * 32, "quote", "yes", OWNER_ID, STAMP, stratum="S"))
    clash = json.dumps(ledger_row("e" * 32, "code", "yes", OWNER_ID, STAMP, stratum="S"))
    ledger.write_text(f"{good}\n{{garbage\n{clash}\n", encoding="utf-8")
    fresh = ledger_row("f" * 32, "quote", "yes", OWNER_ID, STAMP, stratum="S")
    assert append_ledger(ledger, [fresh]) == 1
    contents = read_ledger(ledger)
    assert len(contents.rows) == 3 and [n for n, _ in contents.quarantined] == [2]
    summary = summarize(contents.rows)
    assert summary.quarantined_rows == 2


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


def test_a_ledger_row_binds_the_sample_the_text_and_the_drawn_count() -> None:
    row = ledger_row("a" * 32, "quote", "yes", OWNER_ID, STAMP, stratum="S")
    assert set(row) == {
        "schema",
        "rules_version",
        "span_id",
        "rule",
        "stratum",
        "verdict",
        "reviewer",
        "reviewed_at",
        "sample_digest",
        "text_sha256",
        "drawn",
    }
    for bad in (
        {"sample_digest": "xyz"},
        {"sample_digest": "D" * 64},
        {"text_sha256": "1" * 63},
        {"drawn": 0},
        {"drawn": True},
        {"drawn": "5"},
    ):
        with pytest.raises(ValueError, match="ledger"):
            ledger_row("a" * 32, "quote", "yes", OWNER_ID, STAMP, stratum="S", **bad)
    for key in ("sample_digest", "text_sha256", "drawn"):
        without = {name: value for name, value in row.items() if name != key}
        with pytest.raises(ValueError, match="keys"):
            validate_ledger_row(without)


def test_a_row_counts_only_when_the_scans_span_has_the_text_the_row_answered() -> None:
    rows = verdict_rows("typed_prefix", ["yes"] * 100)
    assert gate(rows).eligible
    first = f"{0:032x}"
    rule, stratum, _ = matched_of(rows)[first]
    edited = matched_of(rows) | {first: (rule, stratum, "0" * 64)}
    summary = review_statistics(rows, matched=edited, owner=OWNER_ID)
    review = summary.strata[("typed_prefix", "S")]
    assert summary.text_mismatch_rows == 1 and summary.orphan_rows == 0
    assert review.confirmed == 99 and review.reviewed == 99 and not review.eligible
    assert summary.verdicts[first] == "unsure" and first in summary.verdicts


def test_precision_is_over_the_rows_a_sample_drew_not_the_rows_the_ledger_kept() -> None:
    kept = verdict_rows("typed_prefix", ["yes"] * 60)
    drew_a_hundred = [{**row, "drawn": 100} for row in kept]
    summary = summarize(drew_a_hundred)
    review = summary.strata[("typed_prefix", "S")]
    assert (review.reviewed, review.confirmed, review.unsure) == (100, 60, 40)
    assert review.missing == 40 and summary.missing_rows == 40
    assert not review.eligible and review.precision == 0.6
    untouched = summarize(kept).strata[("typed_prefix", "S")]
    assert untouched.reviewed == 60 and untouched.missing == 0
    full = verdict_rows("typed_prefix", ["yes"] * 96 + ["no"] * 4)
    assert gate([{**row, "drawn": 100} for row in full]).eligible


def test_a_drawn_row_that_is_only_an_orphan_is_present_not_missing() -> None:
    rows = [{**row, "drawn": 100} for row in verdict_rows("typed_prefix", ["yes"] * 100)]
    summary = review_statistics(rows, matched={}, owner=OWNER_ID)
    assert summary.orphan_rows == 100 and summary.missing_rows == 0
    assert summary.strata[("typed_prefix", "S")].reviewed == 0


def test_two_samples_are_judged_each_by_what_it_drew() -> None:
    first = [{**row, "drawn": 50} for row in verdict_rows("typed_prefix", ["yes"] * 50)]
    second = [
        {**row, "drawn": 50, "sample_digest": "e" * 64}
        for row in verdict_rows("typed_prefix", ["yes"] * 30, start=1000)
    ]
    review = gate(first + second)
    assert (review.reviewed, review.confirmed, review.missing) == (100, 80, 20)


def test_a_torn_last_line_becomes_one_quarantined_row_and_appending_goes_on(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger.jsonl"
    append_ledger(ledger, verdict_rows("quote", ["yes", "no"]))
    with ledger.open("ab") as handle:
        handle.write(b'{"schema": "cce.attribution-review-ledger.v3", "rules_ver')
    result = append_rows(ledger, verdict_rows("quote", ["yes"], start=7))
    assert (result.appended, result.repaired_torn_line) == (1, True)
    contents = read_ledger(ledger)
    assert [row["span_id"] for row in contents.rows] == [f"{0:032x}", f"{1:032x}", f"{7:032x}"]
    assert [number for number, _ in contents.quarantined] == [3]
    assert ledger.read_bytes().endswith(b"\n")
    again = append_rows(ledger, verdict_rows("quote", ["yes"], start=8))
    assert (again.appended, again.repaired_torn_line) == (1, False)
    assert append_ledger(ledger, verdict_rows("quote", ["yes"], start=9)) == 1


class VirtualTime:
    """A clock that only moves when the code under test sleeps."""

    def __init__(self, on_sleep=None) -> None:
        self.now = 0.0
        self.sleeps = 0
        self.on_sleep = on_sleep

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.sleeps += 1
        if self.on_sleep:
            self.on_sleep(self.sleeps)


def test_a_ledger_locked_by_another_process_is_waited_for_then_refused(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_bytes(b"")
    holder = os.open(ledger, os.O_RDWR)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        time = VirtualTime()
        with pytest.raises(ValueError, match="locked by another process"):
            append_rows(
                ledger,
                verdict_rows("quote", ["yes"]),
                lock_timeout=30.0,
                clock=time.clock,
                sleep=time.sleep,
            )
        assert 30.0 <= time.now < 31.0 and time.sleeps > 10
        assert ledger.read_bytes() == b""
        release = VirtualTime(
            lambda count: fcntl.flock(holder, fcntl.LOCK_UN) if count == 3 else None
        )
        result = append_rows(
            ledger,
            verdict_rows("quote", ["yes"]),
            lock_timeout=30.0,
            clock=release.clock,
            sleep=release.sleep,
        )
        assert result.appended == 1 and release.sleeps == 3
    finally:
        os.close(holder)


def test_the_default_lock_wait_is_thirty_seconds() -> None:
    assert LEDGER_LOCK_SECONDS == 30.0


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
    assert read_ledger(path).rows[:2] == first
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


@pytest.mark.parametrize("verdict", ["no", "unsure"])
def test_a_span_the_owner_rejected_or_doubted_is_never_eligible_in_an_open_stratum(
    verdict: str,
) -> None:
    eligible = frozenset({("typed_prefix", "S")})
    (span,) = spans_of(SHORT)
    annotation = to_annotation(
        SHORT, [span], owner_id="rob", eligible=eligible, verdicts={span.span_id: verdict}
    )
    (entry,) = annotation["spans"]
    assert entry["profile_eligible"] is False and entry["owner_verdict"] == verdict
    assert f"owner_verdict:{verdict}" in entry["evidence"]
    (checked,) = attributed_spans(SHORT, annotation)
    assert not owner_profile_eligible(checked, "rob")
    assert f"owner_verdict:{verdict}" in checked["evidence"]


def test_a_span_the_owner_confirmed_is_eligible_only_while_its_stratum_is_open() -> None:
    (span,) = spans_of(SHORT)
    verdicts = {span.span_id: "yes"}
    opened = to_annotation(
        SHORT,
        [span],
        owner_id="rob",
        eligible=frozenset({("typed_prefix", "S")}),
        verdicts=verdicts,
    )
    assert opened["spans"][0]["profile_eligible"] is True
    assert opened["spans"][0]["owner_verdict"] == "yes"
    closed = to_annotation(SHORT, [span], owner_id="rob", verdicts=verdicts)
    assert closed["spans"][0]["profile_eligible"] is False
    assert closed["spans"][0]["owner_verdict"] == "yes"
    unreviewed = to_annotation(
        SHORT, [span], owner_id="rob", eligible=frozenset({("typed_prefix", "S")})
    )
    assert unreviewed["spans"][0]["profile_eligible"] is True
    assert "owner_verdict" not in unreviewed["spans"][0]
    assert not any(item.startswith("owner_verdict:") for item in unreviewed["spans"][0]["evidence"])


def test_the_summary_hands_back_the_last_counted_verdict_of_every_span() -> None:
    rows = verdict_rows("typed_prefix", ["yes", "no", "unsure"])
    rows += verdict_rows("typed_prefix", ["no"], start=0)  # span 0 answered again: now no
    rows += [ledger_row("9" * 32, "typed_prefix", "no", "someone-else", STAMP, stratum="S")]
    summary = summarize(rows)
    assert summary.verdicts == {f"{0:032x}": "no", f"{1:032x}": "no", f"{2:032x}": "unsure"}


def test_a_span_the_ledger_cannot_count_cleanly_is_held_back() -> None:
    rows = verdict_rows("typed_prefix", ["yes", "yes"])
    clash = ledger_row(f"{0:032x}", "quote", "yes", OWNER_ID, STAMP, stratum="S")
    summary = summarize([*rows, clash])
    assert summary.quarantined_rows == 2
    assert summary.verdicts[f"{0:032x}"] == "unsure"
    assert f"{1:032x}" in summary.verdicts and summary.verdicts[f"{1:032x}"] == "yes"


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
        "text_sha256",
        "verdict",
        "partial",
    }
    assert row["text_sha256"] == text_digest(row["text"])
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


def test_the_sampler_excludes_only_the_exact_reviewed_span() -> None:
    drawn = ReviewSampler(per_stratum=100)
    offer_all(drawn, many_quotes(5))
    rows = drawn.rows()
    reviewed = {
        row["span_id"]: (row["rule"], row["stratum"], row["text_sha256"]) for row in rows[:2]
    }
    again = ReviewSampler(per_stratum=100, exclude=reviewed)
    offer_all(again, many_quotes(5))
    assert {row["span_id"] for row in again.rows()} == {row["span_id"] for row in rows[2:]}
    for name, edit in (
        ("another text", lambda kind: (kind[0], kind[1], "0" * 64)),
        ("another rule", lambda kind: ("code", kind[1], kind[2])),
        ("another stratum", lambda kind: (kind[0], "M", kind[2])),
    ):
        wrong = {span: edit(kind) for span, kind in reviewed.items()}
        sampler = ReviewSampler(per_stratum=100, exclude=wrong)
        offer_all(sampler, many_quotes(5))
        assert len(sampler.rows()) == 5, name


# --- the sample document: a header that binds the rows ----------------------------------------


def drawn_sample(count: int = 6) -> tuple[dict, list[dict]]:
    sampler = ReviewSampler(per_stratum=5)
    offer_all(sampler, many_quotes(count))
    rows = sampler.rows()
    header = sample_header(
        rows, library="f" * 64, owner_id=OWNER_ID, seed=sampler.seed, per_stratum=5, partial=False
    )
    return header, rows


def test_a_sample_document_round_trips_and_its_digest_ignores_the_answers() -> None:
    header, rows = drawn_sample()
    assert header["rows"] == len(rows) == 5 and header["drawn"] == {"quote:S": 5}
    assert header["sample_digest"] == sample_digest(rows) and header["owner_id"] == OWNER_ID
    parsed = parse_sample(sample_text(header, rows).splitlines())
    assert parsed.header == header and parsed.rows == rows
    answered = [{**row, "verdict": "yes"} for row in rows]
    assert sample_digest(answered) == header["sample_digest"]
    assert parse_sample(sample_text(header, answered).splitlines()).rows == answered
    assert sample_digest(rows) != sample_digest(rows[:-1])


def sample_lines(header: dict, rows: list[dict]) -> list[str]:
    return sample_text(header, rows).splitlines()


def test_a_tampered_or_hand_written_sample_is_refused() -> None:
    header, rows = drawn_sample()
    fake_excerpt = {**rows[0], "excerpted": True}
    edited_text = {**rows[0], "text": "an edited quote"}
    edited_text["length"] = len(edited_text["text"])
    edited_text["text_sha256"] = text_digest(edited_text["text"])
    other_id = {**rows[1], "span_id": "9" * 32}
    other_stratum = {**rows[1], "stratum": "M"}
    stale_row = {**rows[1], "rules_version": "cce.attribution-rules@2"}
    cases = {
        "an edited text with a repaired hash": (header, [edited_text, *rows[1:]], "altered"),
        "a text that no longer matches its hash": (
            header,
            [{**rows[0], "text": "changed"}, *rows[1:]],
            "hash",
        ),
        "another span id": (header, [rows[0], other_id, *rows[2:]], "altered"),
        "another stratum": (header, [rows[0], other_stratum, *rows[2:]], "altered"),
        "a dropped row": (header, rows[:-1], "counts 5 rows"),
        "a repeated row": ({**header, "rows": 6}, [*rows, rows[0]], "twice"),
        "a wrong digest": ({**header, "sample_digest": "0" * 64}, rows, "digest"),
        "wrong counts": ({**header, "drawn": {"quote:S": 4}}, rows, "per-stratum"),
        "an excerpt of a short span": (header, [fake_excerpt, *rows[1:]], "short"),
        "another rules version": (header, [stale_row, *rows[2:]], "rules version"),
        "a header of another version": (
            {**header, "rules_version": "cce.attribution-rules@2"},
            rows,
            "rules version",
        ),
    }
    for name, (head, body, message) in cases.items():
        with pytest.raises(ValueError, match=message):
            parse_sample(sample_lines(head, body))
        assert name
    with pytest.raises(ValueError, match="no header"):
        parse_sample([json.dumps(row) for row in rows])  # hand-written: rows without a header
    with pytest.raises(ValueError, match="empty"):
        parse_sample(["", "  "])
    with pytest.raises(ValueError, match="sample line 1: unreadable"):
        parse_sample(["[" * DEEP + "]" * DEEP])
    with pytest.raises(ValueError, match="sample line 2: not JSON"):
        parse_sample([json.dumps(header), "{broken"])
    missing_owner = {**header, "owner_id": " "}
    with pytest.raises(ValueError, match="owner_id"):
        parse_sample(sample_lines(missing_owner, rows))


def test_sampler_validates_its_parameters() -> None:
    for kwargs in ({"per_stratum": 0}, {"per_stratum": True}):
        with pytest.raises(ValueError, match="per_stratum"):
            ReviewSampler(**kwargs)
    assert SAMPLE_PER_STRATUM == 100
