"""One notion of "the same text" for the review gate, the draw and the twin veto (synthetic data).

A reader sees one sentence however it is spelled; the gate must count it once, a later draw must
not show it again and a `no` must reject every spelling of it. These tests spell one sentence many
ways (invisible characters, spacing, line endings, Unicode form) and check all three.
"""

from __future__ import annotations

import hashlib
import random
import unicodedata

import pytest

from conversation_corpus_engine import attribution_rules as rules
from conversation_corpus_engine.attribution import text_digest

OWNER_ID = "owner-1"
STAMP = "2026-01-02T03:04:05Z"
SAMPLE_DIGEST = "d" * 64
CHAT = rules.RecordInfo(provider="chatgpt", kind="chat", wing="ai")
SENTENCE = "The quiet harbour opens at dawn and the boats go out slowly."
ACCENTED = "The café by the harbour opens at dawn and the naïve boats go out slowly."


def spelled_many_ways(sentence: str) -> dict[str, str]:
    """Spellings of one visible sentence."""
    first_space = sentence.index(" ")
    return {
        "plain": sentence,
        "zero width space": sentence[:10] + "\u200b" + sentence[10:],
        "word joiner": sentence[:7] + "\u2060" + sentence[7:],
        "soft hyphen": sentence[:15] + "\u00ad" + sentence[15:],
        "byte order mark": "\ufeff" + sentence,
        "bidirectional mark": sentence[:5] + "\u200e" + sentence[5:],
        "control character": sentence[:4] + "\x00" + sentence[4:],
        "double space": sentence.replace(" ", "  ", 1),
        "many spaces": sentence.replace(" ", " " * 9),
        "tab": sentence.replace(" ", "\t", 1),
        "carriage return and line feed": sentence[:first_space]
        + "\r\n"
        + sentence[first_space + 1 :],
        "bare carriage return": sentence[:first_space] + "\r" + sentence[first_space + 1 :],
        "line feed": sentence[:first_space] + "\n" + sentence[first_space + 1 :],
        "no-break space": sentence.replace(" ", "\u00a0", 1),
        "ideographic space": sentence.replace(" ", "\u3000", 1),
        "trailing spaces": sentence + "   \n",
        "leading spaces": "  \t" + sentence,
        "decomposed": unicodedata.normalize("NFD", sentence),
        "composed": unicodedata.normalize("NFC", sentence),
        "zero width between words": sentence.replace(" ", " \u200b ", 1),
    }


@pytest.mark.parametrize("sentence", [SENTENCE, ACCENTED])
def test_every_spelling_of_one_visible_text_has_one_text_key(sentence: str) -> None:
    variants = spelled_many_ways(sentence)
    keys = {name: rules.text_key(text) for name, text in variants.items()}
    assert len(set(keys.values())) == 1, keys
    assert len({text_digest(text) for text in variants.values()}) > 10  # exact digests all differ
    assert rules.visible_form(variants["plain"]) == sentence


def test_decomposed_and_composed_accents_agree() -> None:
    composed, decomposed = ACCENTED, unicodedata.normalize("NFD", ACCENTED)
    assert composed != decomposed
    assert rules.text_key(composed) == rules.text_key(decomposed)
    assert rules.text_key("ﬁne wine") == rules.text_key("fine wine")  # a compatibility ligature


def test_text_keys_keep_genuinely_different_texts_apart() -> None:
    different = [
        SENTENCE,
        SENTENCE.replace("quiet", "loud"),
        SENTENCE.lower(),
        SENTENCE.rstrip("."),
        SENTENCE + " Again.",
        SENTENCE.replace(" ", "", 1),  # a deleted space is a different word
        SENTENCE.replace("opens", "closes"),
        "The quiet harbour opens at dawn.",
    ]
    assert len({rules.text_key(text) for text in different}) == len(different)
    assert rules.text_key("a b") != rules.text_key("ab")  # real whitespace separates words
    assert rules.text_key("a\u200bb") == rules.text_key("ab")  # an invisible character does not


def test_text_key_is_a_sha256_of_the_visible_form() -> None:
    assert rules.text_key(SENTENCE) == hashlib.sha256(SENTENCE.encode()).hexdigest()
    assert rules.text_key("  a \t\n b ") == hashlib.sha256(b"a b").hexdigest()
    assert rules.text_key("") == rules.text_key(" \u200b ") == hashlib.sha256(b"").hexdigest()


def test_the_deleted_characters_are_the_format_and_control_characters() -> None:
    for char in (
        "\u200b",
        "\u200c",
        "\u200d",
        "\u2060",
        "\u00ad",
        "\ufeff",
        "\u200e",
        "\x00",
        "\x7f",
    ):
        assert rules.delete_invisible(f"a{char}b") == "ab", repr(char)
    for char in (" ", "\t", "\n", "\r", "\u00a0", "\u3000", "\x85"):
        assert rules.delete_invisible(f"a{char}b") == f"a{char}b", repr(char)
    # Marks and other invisible characters outside Cf and Cc are not touched (a stated limit).
    assert rules.visible_form("a\u034fb") != "ab"


def test_text_key_properties_over_random_texts() -> None:
    """Idempotence, spelling-invariance and separation, over many random texts."""
    rng = random.Random(61)
    alphabet = list("abcdeé fgh.\n\t") + [
        "\u0301",
        "\u200b",
        "\u2060",
        "\u00ad",
        "\ufeff",
        "ﬁ",
        "\r",
    ]
    for _ in range(600):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        form = rules.visible_form(text)
        assert rules.visible_form(form) == form, repr(text)
        assert rules.text_key(form) == rules.text_key(text), repr(text)
        assert form == form.strip() and "  " not in form
    words = [
        "garden",
        "river",
        "walk",
        "lunch",
        "notebook",
        "winter",
        "quiet",
        "street",
        "paint",
        "morning",
        "letter",
    ]
    for _ in range(300):
        sentence = " ".join(rng.choice(words) for _ in range(rng.randint(2, 12)))
        spelling = ""
        for char in sentence:
            if char == " ":
                spelling += rng.choice([" ", "  ", "\t", "\r\n", "\u00a0", " \u200b"])
            else:
                spelling += char + rng.choice(["", "", "", "\u200b", "\u2060", "\u00ad"])
        assert rules.text_key(spelling) == rules.text_key(sentence)
        other = sentence + " " + rng.choice(words)
        assert rules.text_key(other) != rules.text_key(sentence)


# --- the gate ------------------------------------------------------------------------------------


def row_of(text: str, index: int, verdict: str = "yes", *, rule: str = "typed_prefix") -> dict:
    """The ledger row of one answered span whose text is `text` (the span id is the index)."""
    return rules.ledger_row(
        f"{index:032x}",
        rule,
        verdict,
        OWNER_ID,
        STAMP,
        stratum="S",
        sample_digest=SAMPLE_DIGEST,
        text_sha256=text_digest(text),
        text_key=rules.text_key(text),
        drawn=1,
    )


def summary_of(texts_and_verdicts: list[tuple[str, str]]) -> rules.ReviewSummary:
    rows = [
        row_of(text, index, verdict) for index, (text, verdict) in enumerate(texts_and_verdicts)
    ]
    matched = {row["span_id"]: (row["rule"], row["stratum"], row["text_sha256"]) for row in rows}
    return rules.review_statistics(rows, matched=matched, owner=OWNER_ID)


def invisible_variants(sentence: str, count: int) -> list[str]:
    """`count` different exact texts that read as `sentence`: zero-width spaces at one place."""
    variants = []
    for index in range(count):
        place, copies = index % len(sentence), 1 + index // len(sentence)
        variants.append(sentence[:place] + "\N{ZERO WIDTH SPACE}" * copies + sentence[place:])
    return variants


def test_a_hundred_zero_width_variants_of_one_sentence_are_one_text_and_do_not_open_the_gate() -> (
    None
):
    variants = invisible_variants(SENTENCE, 100)
    assert len(set(variants)) == 100
    summary = summary_of([(text, "yes") for text in variants])
    review = summary.strata[("typed_prefix", "S")]
    assert (review.reviewed, review.confirmed, review.eligible) == (1, 1, False)
    assert summary.duplicate_text_rows == 99
    assert rules.eligible_strata(summary) == frozenset()


def test_a_hundred_distinct_visible_texts_still_open_the_gate() -> None:
    summary = summary_of([(f"{SENTENCE} Number {index}.", "yes") for index in range(100)])
    assert summary.strata[("typed_prefix", "S")].eligible


def test_every_spelling_of_a_rejected_text_is_rejected_with_it() -> None:
    summary = summary_of([(SENTENCE, "no")])
    assert summary.rejected_texts == {rules.text_key(SENTENCE)}
    eligible = frozenset({("typed_prefix", "S")})
    for name, text in spelled_many_ways(SENTENCE).items():
        spans = rules.attribute_message(
            text, role="user", channel="typed", typed_chars=len(text), record=CHAT
        )
        annotation = rules.to_annotation(
            text,
            spans,
            owner_id=OWNER_ID,
            eligible=eligible,
            rejected_texts=summary.rejected_texts,
        )
        (entry,) = annotation["spans"]  # the owner span; the unknown remainder is left out
        assert entry["author"] == "owner", name
        assert entry["profile_eligible"] is False, name
        assert rules.REJECTED_TEXT_EVIDENCE in entry["evidence"], name
    other = f"{SENTENCE} Different."
    spans = rules.attribute_message(
        other, role="user", channel="typed", typed_chars=len(other), record=CHAT
    )
    kept = rules.to_annotation(
        other, spans, owner_id=OWNER_ID, eligible=eligible, rejected_texts=summary.rejected_texts
    )
    assert kept["spans"][0]["profile_eligible"] is True


def test_an_unsure_answer_vetoes_spellings_too_and_across_rules_and_strata() -> None:
    rows = [
        rules.ledger_row(
            f"{900:032x}",
            "quote",
            "unsure",
            OWNER_ID,
            STAMP,
            stratum="L",
            sample_digest=SAMPLE_DIGEST,
            text_sha256=text_digest(SENTENCE),
            text_key=rules.text_key(SENTENCE),
            drawn=1,
        )
    ]
    summary = rules.review_statistics(rows, matched={}, owner=OWNER_ID)  # the span is long gone
    assert rules.text_key(SENTENCE + "\N{ZERO WIDTH SPACE}") in summary.rejected_texts
    text = "\N{WORD JOINER}" + SENTENCE
    spans = rules.attribute_message(
        text, role="user", channel="typed", typed_chars=len(text), record=CHAT
    )
    annotation = rules.to_annotation(
        text,
        spans,
        owner_id=OWNER_ID,
        eligible=frozenset({("typed_prefix", "S")}),
        rejected_texts=summary.rejected_texts,
    )
    assert annotation["spans"][0]["author"] == "owner"
    assert annotation["spans"][0]["profile_eligible"] is False


def test_orphaned_negatives_join_a_text_group_whatever_their_spelling() -> None:
    yes = [row_of(f"{SENTENCE} Number {index}.", index) for index in range(100)]
    matched = {row["span_id"]: (row["rule"], row["stratum"], row["text_sha256"]) for row in yes}
    orphan = row_of("\N{ZERO WIDTH SPACE}" + f"{SENTENCE} Number 3.  ", 500, "no")
    summary = rules.review_statistics([*yes, orphan], matched=matched, owner=OWNER_ID)
    review = summary.strata[("typed_prefix", "S")]
    assert (review.reviewed, review.confirmed, review.rejected) == (100, 99, 1)
    # The orphaned no is a text already in the scan: it turns that text into a no, once.
    assert summary.orphan_negative_rows == 1 and review.orphan_negative == 0
    assert review.eligible  # 99 of 100 texts confirmed


# --- the draw ------------------------------------------------------------------------------------


def offer(sampler: rules.ReviewSampler, texts: list[str]) -> None:
    for text in texts:
        for span in rules.attribute_message(
            text, role="user", channel="typed", typed_chars=len(text), record=CHAT
        ):
            sampler.offer(span, text[span.start : span.end])


def test_one_draw_shows_one_row_for_all_the_spellings_of_a_text() -> None:
    sampler = rules.ReviewSampler(per_stratum=100)
    offer(sampler, invisible_variants(SENTENCE, 100))
    rows = sampler.rows()
    assert len(rows) == 1
    assert rows[0]["text_key"] == rules.text_key(SENTENCE)
    assert rows[0]["text_sha256"] == text_digest(rows[0]["text"])


def test_a_hundred_spacing_variants_over_a_hundred_rounds_never_open_a_stratum() -> None:
    """The verifier's attack: a hundred interior-spacing variants of one sentence, a draw and a
    recording each round. The sentence is shown once and counted once, however many rounds."""
    words = SENTENCE.split(" ")
    variants = []
    for index in range(100):
        gaps = [" " * (1 + ((index >> bit) & 1) + (index % 3)) for bit in range(len(words) - 1)]
        variants.append("".join(word + gap for word, gap in zip(words, [*gaps, ""], strict=True)))
    assert len(set(variants)) > 50
    ledger: list[dict] = []
    shown = 0
    for round_number in range(100):
        answered = {
            (row["rule"], row["stratum"], row["text_key"])
            for row in ledger
            if row["reviewer"] == OWNER_ID
        }
        sampler = rules.ReviewSampler(per_stratum=100, exclude_texts=answered)
        offer(sampler, variants)
        rows = [row for row in sampler.rows() if row["rule"] == "typed_prefix"]
        shown += len(rows)
        for row in rows:
            ledger.append(
                rules.ledger_row(
                    row["span_id"],
                    row["rule"],
                    "yes",
                    OWNER_ID,
                    STAMP,
                    stratum=row["stratum"],
                    sample_digest=f"{round_number:064x}",
                    text_sha256=row["text_sha256"],
                    text_key=row["text_key"],
                    drawn=len(rows),
                )
            )
    assert shown == 1 and len(ledger) == 1
    matched = {row["span_id"]: (row["rule"], row["stratum"], row["text_sha256"]) for row in ledger}
    summary = rules.review_statistics(ledger, matched=matched, owner=OWNER_ID)
    assert summary.strata[("typed_prefix", "S")].reviewed == 1
    assert rules.eligible_strata(summary) == frozenset()


def test_a_later_draw_leaves_out_every_spelling_of_an_answered_text() -> None:
    key = ("typed_prefix", "S", rules.text_key(SENTENCE))
    sampler = rules.ReviewSampler(per_stratum=100, exclude_texts={key})
    offer(sampler, [*spelled_many_ways(SENTENCE).values(), f"{SENTENCE} A different one."])
    assert [row["text"] for row in sampler.rows()] == [f"{SENTENCE} A different one."]
    # Another rule's claim on the same text is a different review: it is still offered.
    elsewhere = rules.ReviewSampler(
        per_stratum=100, exclude_texts={("quote", "S", rules.text_key(SENTENCE))}
    )
    offer(elsewhere, [SENTENCE])
    assert len(elsewhere.rows()) == 1


# --- sample rows, the exclusion digest and the ledger row -----------------------------------------


def drawn_rows(count: int = 3) -> list[dict]:
    sampler = rules.ReviewSampler(per_stratum=count)
    offer(sampler, [f"{SENTENCE} Number {index}." for index in range(count)])
    return sampler.rows()


def sample_lines_of(rows: list[dict]) -> list[str]:
    header = rules.sample_header(
        rows,
        library="f" * 64,
        owner_id=OWNER_ID,
        seed=rules.SAMPLE_SEED,
        per_stratum=3,
        partial=False,
    )
    return rules.sample_text(header, rows).splitlines()


def test_sample_rows_carry_the_text_key_and_a_wrong_one_is_refused() -> None:
    rows = drawn_rows()
    assert {row["text_key"] for row in rows} == {rules.text_key(row["text"]) for row in rows}
    parsed = rules.parse_sample(sample_lines_of(rows))
    assert [row["text_key"] for row in parsed.rows] == [row["text_key"] for row in rows]
    forged = [{**rows[0], "text_key": rules.text_key("some other sentence")}, *rows[1:]]
    with pytest.raises(ValueError, match="text key"):
        rules.parse_sample(sample_lines_of(forged))
    for bad in ("", "xyz", "A" * 64, None, 5):
        with pytest.raises(ValueError, match="text_key"):
            rules.parse_sample(sample_lines_of([{**rows[0], "text_key": bad}, *rows[1:]]))
    missing = [{k: v for k, v in rows[0].items() if k != "text_key"}, *rows[1:]]
    with pytest.raises(ValueError, match="row keys"):
        rules.parse_sample(sample_lines_of(missing))


def test_the_exclusion_digest_names_the_text_key_of_every_excluded_row() -> None:
    rows = [row_of(f"{SENTENCE} Number {index}.", index) for index in range(3)]
    digest = rules.exclusion_digest(rows, OWNER_ID)
    other_key = {**rows[0], "text_key": rules.text_key("another sentence")}
    assert rules.exclusion_digest([other_key, *rows[1:]], OWNER_ID) != digest
    other_hash = {**rows[0], "text_sha256": "e" * 64}
    assert rules.exclusion_digest([other_hash, *rows[1:]], OWNER_ID) != digest


def test_a_ledger_row_carries_a_text_key_and_refuses_a_malformed_one() -> None:
    row = row_of(SENTENCE, 1)
    assert row["text_key"] == rules.text_key(SENTENCE) and row["text_sha256"] == text_digest(
        SENTENCE
    )
    spaced = row_of(SENTENCE.replace(" ", "  ", 1), 2)  # the exact text differs, the key does not
    assert spaced["text_sha256"] != row["text_sha256"] and spaced["text_key"] == row["text_key"]
    for bad in ("1" * 63, "A" * 64, "", None, 7):
        with pytest.raises(ValueError, match="text_key"):
            rules.validate_ledger_row({**row, "text_key": bad})


def test_rows_of_the_previous_rules_version_and_ledger_schema_are_stale() -> None:
    rows = [row_of(f"{SENTENCE} Number {index}.", index) for index in range(100)]
    matched = {row["span_id"]: (row["rule"], row["stratum"], row["text_sha256"]) for row in rows}
    assert (
        rules.review_statistics(rows, matched=matched, owner=OWNER_ID)
        .strata[("typed_prefix", "S")]
        .eligible
    )
    previous = [{**row, "rules_version": "cce.attribution-rules@5"} for row in rows]
    summary = rules.review_statistics(previous, matched=matched, owner=OWNER_ID)
    assert (summary.rows, summary.stale_rows) == (100, 100)
    assert rules.eligible_strata(summary) == frozenset()
    old_schema = [
        {
            **{key: value for key, value in row.items() if key != "text_key"},
            "schema": "cce.attribution-review-ledger.v5",
        }
        for row in rows
    ]
    summary = rules.review_statistics(old_schema, matched=matched, owner=OWNER_ID)
    assert (summary.rows, summary.stale_rows) == (100, 100)
    assert rules.eligible_strata(summary) == frozenset()
