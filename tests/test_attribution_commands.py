"""`cce library attribute`: report, annotation export, owner review and the gate (synthetic data)."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import unicodedata
import zlib
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
from attribution_support import (
    ECHO_ANSWER,
    ECHO_QUESTION,
    PROBE_PASTED,
    PROBE_QUESTION,
    PROBE_REMINDER,
    build_library,
    conversations,
    typed,
)

from conversation_corpus_engine import attribution_commands, attribution_rules
from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.attributed_knowledge import collect_owner_segments
from conversation_corpus_engine.attribution import attributed_spans, owner_profile_eligible
from conversation_corpus_engine.attribution_commands import (
    Deadline,
    build_census,
    handle_attribute,
    scan,
)
from conversation_corpus_engine.attribution_rules import (
    RULES,
    RULES_VERSION,
    SAMPLE_SCHEMA,
    SAMPLE_SEED,
    append_ledger,
    drawn_counts,
    exclusion_digest,
    ledger_row,
    read_ledger,
    sample_digest,
    sample_text,
    text_key,
)
from conversation_corpus_engine.cli import build_parser, main
from conversation_corpus_engine.library_store import Library

STAMP = "2026-01-02T03:04:05Z"
# Nesting deep enough to exhaust the JSON parser's recursion on every supported Python.
DEEP_NESTING = 1_500_000
SAMPLE_KEYS = {
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
    "text_key",
    "verdict",
    "partial",
}


def run_cli(capsys: pytest.CaptureFixture[str], *argv: str | Path) -> tuple[int, str, str]:
    args = build_parser().parse_args(["library", "attribute", *map(str, argv)])
    code = handle_attribute(args)
    out, err = capsys.readouterr()
    return code, out, err


def report_of(capsys: pytest.CaptureFixture[str], library: Path, *extra: str | Path) -> dict:
    code, out, err = run_cli(capsys, "--report", "--json", "--library", library, *extra)
    assert code == 0, err
    return json.loads(out)


def fingerprint(directory: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in directory.iterdir()
    }


def library_characters(path: Path) -> int:
    total = 0
    with Library(path) as library:
        for row in library.rows():
            payload = library.record(row["uid"])
            for sequence in (payload["messages"], payload["extra"].get("alternate_messages", [])):
                total += sum(len(item["text"]) for item in sequence)
    return total


def message_text(library: Library, source: dict) -> str:
    payload = library.record(source["uid"], version=source["version"])
    messages = (
        payload["messages"]
        if source["sequence"] == "main"
        else payload["extra"]["alternate_messages"]
    )
    assert messages[source["message"]]["id"] == source["message_id"]
    return messages[source["message"]]["text"]


@pytest.fixture
def library_path(tmp_path: Path) -> Path:
    return build_library(tmp_path / "store" / "generation")


# --- --report --------------------------------------------------------------------------------


def test_report_partitions_every_character_and_every_rule_fires(
    library_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # One fetched page, so the rule for third-party families fires too.
    with Library(library_path, writable=True) as library:
        library.put(
            Conversation(
                provider="webcapture",
                source_id="pg-" + "b" * 24,
                title="A saved page",
                kind="page",
                messages=[
                    Message(
                        role="tool",
                        channel="tool",
                        id="web-capture-page",
                        text="Words from a saved page that someone else wrote.",
                        typed_chars=0,
                    )
                ],
                extra={"wing": "ai", "lane": "web", "body_status": "full"},
            )
        )
    report = report_of(capsys, library_path)
    total = library_characters(library_path)
    partition = report["partition"]
    assert partition["characters"] == partition["covered"] == total
    assert partition["coverage"] == 1.0
    assert (partition["overlaps"], partition["gaps"], partition["uncovered"]) == (0, 0, 0)
    assert partition["ok"] is True
    assert [row["rule"] for row in report["rules"]] == [rule.id for rule in RULES] + [
        "unattributed"
    ]
    fired = {row["rule"] for row in report["rules"] if row["spans"]}
    assert fired >= {rule.id for rule in RULES}
    assert sum(row["characters"] for row in report["rules"]) == total
    assert sum(entry["characters"] for entry in report["authors"].values()) == total
    assert report["scan"]["complete"] is True
    assert report["scan"]["records_scanned"] == report["scan"]["records_total"]


def test_report_has_no_eligible_rule_without_a_ledger(
    library_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = report_of(capsys, library_path)
    assert report["profile_eligible"] == []
    for row in report["rules"]:
        assert row["eligible_strata"] == []
        assert set(row["strata"]) == {"S", "M", "L"}
        assert all(
            item["eligible"] is False and item["reviewed"] == 0 for item in row["strata"].values()
        )
    assert report["gate"] == {
        "min_reviewed": 100,
        "min_precision_percent": 95,
        "ledger": None,
        "ledger_rows": 0,
        "stale_rows": 0,
        "orphan_rows": 0,
        "text_mismatch_rows": 0,
        "missing_rows": 0,
        "orphan_negative_rows": 0,
        "duplicate_text_rows": 0,
        "non_owner_rows": 0,
        "non_owner_reviewers": {},
        "quarantined_rows": 0,
        "superseded_rows": 0,
    }


def test_report_lists_the_five_largest_categories_by_characters(
    library_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = report_of(capsys, library_path)
    largest = report["largest_categories"]
    assert len(largest) == 5
    sizes = [item["characters"] for item in largest]
    assert sizes == sorted(sizes, reverse=True)
    by_rule = {row["rule"]: row["characters"] for row in report["rules"]}
    assert set(by_rule[item["rule"]] for item in largest) == set(sizes)
    assert min(sizes) >= sorted(by_rule.values(), reverse=True)[4]


def test_report_text_is_the_default_and_names_the_gate(
    library_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run_cli(capsys, "--report", "--library", library_path)
    assert code == 0
    assert "partition:" in out and "100.00%" in out and "0 overlaps, 0 gaps" in out
    assert "largest categories:" in out
    for rule in RULES:
        assert rule.id in out
    assert "profile-eligible: none" in out
    assert not out.lstrip().startswith("{")


def test_report_counts_the_boilerplate_copies_and_honours_thresholds(
    library_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def boilerplate_spans(*extra: str) -> int:
        report = report_of(capsys, library_path, *extra)
        return next(row["spans"] for row in report["rules"] if row["rule"] == "boilerplate")

    assert boilerplate_spans() == 3
    assert boilerplate_spans("--repeat-min-occurrences", "5") == 0
    assert boilerplate_spans("--repeat-min-chars", "100000") == 0
    # With two occurrences enough, the answer pasted back into the CLI session repeats too.
    assert boilerplate_spans("--repeat-min-occurrences", "2") == 4


def test_report_accepts_a_generation_folder(
    library_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert report_of(capsys, library_path.parent)["partition"]["ok"] is True


def test_limit_records_scans_a_flagged_slice_and_keeps_the_partition_whole(
    library_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = report_of(capsys, library_path, "--limit-records", "3")
    assert report["scan"]["records_scanned"] == 3
    assert report["scan"]["limited"] is True and report["scan"]["complete"] is False
    assert report["scan"]["records_total"] > 3
    assert report["partition"]["ok"] is True
    assert report["partition"]["characters"] < library_characters(library_path)


def test_a_command_must_name_what_it_does(
    library_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = run_cli(capsys, "--library", library_path)
    assert code == 2 and "choose --report" in err
    out = library_path.parent / "o.json"
    code, _, err = run_cli(capsys, "--report", "apply", "--out", out, "--library", library_path)
    assert code == 2 and "cannot be combined" in err
    code, _, err = run_cli(capsys, "--report")
    assert code == 2 and "store-root" in err


def test_the_library_is_never_modified(
    library_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = fingerprint(library_path.parent)
    report_of(capsys, library_path)
    assert run_cli(capsys, "apply", "--library", library_path, "--out", tmp_path / "a.json")[0] == 0
    sample = tmp_path / "sample.jsonl"
    assert (
        run_cli(
            capsys, "review-sample", "--owner-id", "rob", "--library", library_path, "--out", sample
        )[0]
        == 0
    )
    assert fingerprint(library_path.parent) == before


# --- bounds ----------------------------------------------------------------------------------


class Ticking:
    """A fake clock that moves one second per reading."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


def test_a_deadline_during_the_census_stops_the_command_and_writes_nothing(
    library_path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        attribution_commands, "Deadline", lambda seconds: Deadline(seconds, clock=Ticking())
    )
    out = tmp_path / "annotations.json"
    code, _, err = run_cli(
        capsys, "apply", "--library", library_path, "--out", out, "--deadline-seconds", "3"
    )
    assert code == 3 and "deadline" in err
    assert not out.exists() and not out.with_name("annotations.json.partial").exists()
    code, _, err = run_cli(capsys, "--report", "--library", library_path, "--deadline-seconds", "3")
    assert code == 3 and "deadline" in err


def test_apply_cut_short_by_the_deadline_keeps_its_file_under_the_partial_name(
    library_path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readings = {"census": 0}

    def counting() -> float:
        readings["census"] += 1
        return 0.0

    with Library(library_path) as library:
        build_census(
            library,
            limit_records=None,
            min_chars=200,
            min_occurrences=3,
            deadline=Deadline(10.0, clock=counting),
        )
    allowed = readings["census"] + 3
    seen = {"count": 0}

    def clock() -> float:
        seen["count"] += 1
        return 0.0 if seen["count"] <= allowed else 1e6

    monkeypatch.setattr(
        attribution_commands, "Deadline", lambda seconds: Deadline(seconds, clock=clock)
    )
    out = tmp_path / "annotations.json"
    code, stdout, err = run_cli(capsys, "apply", "--library", library_path, "--out", out, "--json")
    assert code == 3 and "partial" in err
    result = json.loads(stdout)
    partial = out.with_name("annotations.json.partial")
    assert result["deadline_reached"] is True and result["complete"] is False
    assert result["partial"] is True and result["out"] == str(partial)
    assert not out.exists()
    entries = json.loads(partial.read_text(encoding="utf-8"))
    assert isinstance(entries, list) and len(entries) == result["entries"] > 0


def test_a_deadline_during_the_scan_pass_returns_a_flagged_partial_result(
    library_path: Path,
) -> None:
    calls = {"count": 0}

    def clock() -> float:
        calls["count"] += 1
        return 0.0 if calls["count"] <= 4 else 1e6

    with Library(library_path) as library:
        census = build_census(
            library,
            limit_records=None,
            min_chars=200,
            min_occurrences=3,
            deadline=Deadline(10.0, clock=lambda: 0.0),
        )
        stats, tally = scan(
            library, census, limit_records=None, deadline=Deadline(10.0, clock=clock)
        )
    assert stats.deadline_reached and not stats.complete
    assert 0 < stats.records_scanned < stats.records_total
    assert tally.messages > 0 and not any(
        tally.problems[name] for name in ("gaps", "overlaps", "uncovered")
    )


def test_limits_must_be_positive(library_path: Path) -> None:
    for flag, value in (("--limit-records", "0"), ("--deadline-seconds", "0")):
        with pytest.raises(SystemExit):
            build_parser().parse_args(
                ["library", "attribute", "--report", flag, value, "--library", str(library_path)]
            )


# --- apply -----------------------------------------------------------------------------------


def apply_entries(
    capsys: pytest.CaptureFixture[str], library: Path, out: Path, *extra: str | Path
) -> tuple[list[dict], dict]:
    code, stdout, err = run_cli(
        capsys, "apply", "--library", library, "--out", out, "--json", *extra
    )
    assert code == 0, err
    result = json.loads(stdout)
    # A slice that was cut short keeps the .partial name; a complete export has the requested one.
    assert Path(result["out"]) == (
        out if result["complete"] else out.with_name(out.name + ".partial")
    )
    return json.loads(Path(result["out"]).read_text(encoding="utf-8")), result


def test_apply_writes_valid_digest_bound_annotations_in_the_profile_input_form(
    library_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "annotations.json"
    entries, result = apply_entries(capsys, library_path, out, "--owner-id", "rob")
    assert isinstance(entries, list) and len(entries) == result["entries"] > 5
    assert not out.with_name("annotations.json.partial").exists()
    assert result["complete"] is True and result["profile_eligible_spans"] == 0
    assert result["unreferenceable_messages"] == 2
    sequences = set()
    with Library(library_path) as library:
        for entry in entries:
            source = entry["source"]
            assert set(source) == {
                "uid",
                "version",
                "revision_digest",
                "sequence",
                "message",
                "message_id",
                "text_digest",
            }
            assert not source["uid"].endswith("dup-ids")
            sequences.add(source["sequence"])
            assert any(
                row["version"] == source["version"] and row["digest"] == source["revision_digest"]
                for row in library.history(source["uid"])
            )
            text = message_text(library, source)
            annotation = entry["attribution"]
            assert annotation["text_digest"] == source["text_digest"]
            assert source["text_digest"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
            checked = attributed_spans(text, annotation)
            assert all(span["profile_eligible"] is False for span in checked)
            assert not any(owner_profile_eligible(span, "rob") for span in checked)
    assert sequences == {"main", "alternate"}


def test_apply_output_feeds_the_strict_profile_with_no_owner_text_until_the_gate_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library_path = build_library(tmp_path / "store" / "g", typed_records=110)
    entries, _ = apply_entries(capsys, library_path, tmp_path / "plain.json", "--owner-id", "rob")
    with Library(library_path) as library:
        owner, other = collect_owner_segments(library, entries, "rob")
    assert owner == [] and other

    ledger = approve(capsys, library_path, tmp_path)
    gated, result = apply_entries(
        capsys,
        library_path,
        tmp_path / "gated.json",
        "--owner-id",
        "rob",
        "--ledger",
        ledger,
    )
    assert result["eligible_strata"] == ["typed_prefix:S"]
    expected: list[str] = []
    with Library(library_path) as library:
        for entry in gated:
            text = message_text(library, entry["source"])
            for span in attributed_spans(text, entry["attribution"]):
                if owner_profile_eligible(span, "rob"):
                    assert span["evidence"][0].endswith(":rule:typed_prefix")
                    expected.append(span["text"])
        owner, _ = collect_owner_segments(library, gated, "rob")
    assert expected and sorted(owner) == sorted(expected)
    assert "My thoughts." not in owner and "More thoughts." not in owner
    assert result["profile_eligible_spans"] == len(expected)


def test_apply_without_an_owner_id_never_marks_a_span_eligible(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library_path = build_library(tmp_path / "store" / "g", typed_records=110)
    ledger = approve(capsys, library_path, tmp_path)
    _, result = apply_entries(capsys, library_path, tmp_path / "a.json", "--ledger", ledger)
    assert result["eligible_strata"] == []
    assert result["profile_eligible_spans"] == 0
    assert result["non_owner_rows"] == result["ledger_rows"] > 100


def test_fabricated_ledger_rows_count_for_nothing_in_report_and_apply(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library_path = build_library(tmp_path / "store" / "g", typed_records=110)
    forged = tmp_path / "forged.jsonl"
    append_ledger(
        forged,
        [
            ledger_row(
                f"{index:032x}",
                "typed_prefix",
                "yes",
                "rob",
                STAMP,
                stratum="S",
                sample_digest="a" * 64,
                text_sha256="b" * 64,
                text_key="b" * 64,
                drawn=100,
            )
            for index in range(100)
        ],
    )
    report = report_of(capsys, library_path, "--ledger", forged, "--owner-id", "rob")
    assert report["gate"]["orphan_rows"] == 100 and report["profile_eligible"] == []
    row = next(item for item in report["rules"] if item["rule"] == "typed_prefix")
    assert row["strata"]["S"]["reviewed"] == 0
    entries, result = apply_entries(
        capsys, library_path, tmp_path / "f.json", "--ledger", forged, "--owner-id", "rob"
    )
    assert result["eligible_strata"] == [] and result["orphan_rows"] == 100
    with Library(library_path) as library:
        owner, _ = collect_owner_segments(library, entries, "rob")
    assert owner == []


def test_rows_from_a_non_owner_reviewer_are_listed_and_never_counted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library_path = build_library(tmp_path / "store" / "g", typed_records=110)
    ledger = approve(capsys, library_path, tmp_path, reviewer="agent:codex")
    report = report_of(capsys, library_path, "--ledger", ledger, "--owner-id", "rob")
    gate_info = report["gate"]
    assert gate_info["non_owner_rows"] == gate_info["ledger_rows"] > 100
    assert gate_info["orphan_rows"] == 0
    assert gate_info["non_owner_reviewers"] == {"agent:codex": gate_info["ledger_rows"]}
    assert report["profile_eligible"] == []
    owner_report = report_of(capsys, library_path, "--ledger", ledger, "--owner-id", "agent:codex")
    assert owner_report["profile_eligible"] == ["typed_prefix:S"]


def test_editing_the_text_of_a_message_orphans_every_review_of_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library_path = build_library(tmp_path / "store" / "g", typed_records=110)
    ledger = approve(capsys, library_path, tmp_path)
    ok = report_of(capsys, library_path, "--ledger", ledger, "--owner-id", "rob")
    assert ok["profile_eligible"] == ["typed_prefix:S"]
    edited = build_library(tmp_path / "other-store" / "edited", typed_records=110, text_suffix="!")
    report = report_of(capsys, edited, "--ledger", ledger, "--owner-id", "rob")
    assert report["profile_eligible"] == []
    # Only the fixture messages whose text did not change keep their reviews.
    counted = sum(item["reviewed"] for row in report["rules"] for item in row["strata"].values())
    assert report["gate"]["orphan_rows"] >= 80
    assert counted + report["gate"]["orphan_rows"] == report["gate"]["ledger_rows"]


def forge_sample(source: Path, target: Path, rows: list[dict], ledger: Path | None = None) -> None:
    """A sample file with its own consistent header (what a careful forger would write), naming
    the state of `ledger` as the one it was drawn against."""
    header = {**sample_header(source), "rows": len(rows), "drawn": drawn_counts(rows)}
    if ledger is not None:
        header["exclusion_digest"] = exclusion_digest(read_ledger(ledger).rows, "rob")
    header["sample_digest"] = sample_digest(header, rows)
    target.write_text(sample_text(header, rows), encoding="utf-8")


def test_a_conflicting_answer_is_refused_and_cannot_brick_the_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "g", typed_records=3)
    sample, answers, ledger = (tmp_path / name for name in ("s.jsonl", "a.jsonl", "l.jsonl"))
    run_cli(capsys, "review-sample", "--owner-id", "rob", "--library", library, "--out", sample)
    real = next(row for row in sample_rows(sample) if row["rule"] == "typed_prefix")
    answers.write_text(json.dumps({**real, "verdict": "yes"}) + "\n", encoding="utf-8")
    assert record_answers(capsys, library, ledger, sample, answers)[0] == 0
    before = ledger.read_bytes()

    # A second sample, internally consistent, that names the same span as another rule: it is not
    # the draw (the ledger-level refusal of a span under two rules is tested in the gate tests).
    forged_sample = tmp_path / "forged-sample.jsonl"
    forged_row = {**real, "rule": "note_owner", "claim": "owner", "verdict": None}
    forge_sample(sample, forged_sample, [forged_row], ledger=ledger)
    answers.write_text(json.dumps({**forged_row, "verdict": "yes"}) + "\n", encoding="utf-8")
    code, _, err = record_answers(capsys, library, ledger, forged_sample, answers)
    assert code == 2 and "not the draw" in err
    assert ledger.read_bytes() == before

    # A sample is recorded once; the ledger itself still keeps the last answer of a span.
    first = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
    append_ledger(ledger, [{**first, "verdict": "no"}])
    report = report_of(capsys, library, "--ledger", ledger, "--owner-id", "rob")
    assert report["gate"]["superseded_rows"] == 1
    assert report["gate"]["quarantined_rows"] == 0


def test_a_hand_damaged_ledger_is_reported_not_fatal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "g", typed_records=110)
    ledger = approve(capsys, library, tmp_path)
    first = next(
        row
        for row in map(json.loads, ledger.read_text(encoding="utf-8").splitlines())
        if (row["rule"], row["stratum"]) == ("typed_prefix", "S")
    )
    clash = {**first, "rule": "note_owner"}
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write("{truncated\n" + json.dumps(clash) + "\n")
    report = report_of(capsys, library, "--ledger", ledger, "--owner-id", "rob")
    assert report["gate"]["quarantined_rows"] == 3
    row = next(item for item in report["rules"] if item["rule"] == "typed_prefix")
    assert row["strata"]["S"]["reviewed"] == 99
    assert report["profile_eligible"] == []
    code, _, err = run_cli(capsys, "--report", "--library", library, "--ledger", ledger)
    assert code == 0, err


def test_an_answer_pasted_back_into_a_cli_session_is_not_owner_text(
    library_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = report_of(capsys, library_path)
    assert next(r for r in report["rules"] if r["rule"] == "pasted_back")["spans"] == 2
    entries, _ = apply_entries(capsys, library_path, tmp_path / "echo.json", "--owner-id", "rob")
    with Library(library_path) as library:
        found = [
            (entry, message_text(library, entry["source"]))
            for entry in entries
            if entry["source"]["uid"].endswith("cli-echo") and entry["source"]["message"] == 2
        ]
    ((entry, text),) = found
    spans = attributed_spans(text, entry["attribution"])
    owner = [span["text"] for span in spans if span["author"] == "owner"]
    assert owner == [ECHO_QUESTION]
    pasted = [
        span
        for span in spans
        if span["evidence"] and span["evidence"][0].endswith(":rule:pasted_back")
    ]
    assert [span["text"] for span in pasted] == [ECHO_ANSWER]
    assert pasted[0]["author"] == "third_party"


def test_the_verifiers_probe_pasted_into_a_cli_session_is_carved_out(
    library_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    entries, _ = apply_entries(capsys, library_path, tmp_path / "probe.json", "--owner-id", "rob")
    with Library(library_path) as library:
        found = [
            (entry, message_text(library, entry["source"]))
            for entry in entries
            if entry["source"]["uid"].endswith("cli-probe") and entry["source"]["message"] == 2
        ]
    ((entry, text),) = found
    spans = [s for s in attributed_spans(text, entry["attribution"]) if s["author"] != "unknown"]
    by_rule = {span["evidence"][0].split(":rule:")[1]: span for span in spans}
    assert [span["text"] for span in spans if span["author"] == "owner"] == [PROBE_QUESTION]
    assert by_rule["pasted_back"]["text"] == PROBE_PASTED
    assert by_rule["pasted_back"]["author"] == "third_party"
    assert by_rule["inline_injection"]["text"] == PROBE_REMINDER


ANSWER_LINES = [f"Step {i}: check the parser module stage {i * 7}." for i in range(14)]


def build_chunked_paste_library(directory: Path) -> Path:
    """One conversation pastes an answer back in three consecutive messages; the other has an
    assistant turn between the same three messages."""

    def chat(source_id: str, messages: list[Message]) -> Conversation:
        return Conversation(
            provider="chatgpt",
            source_id=source_id,
            title=source_id,
            messages=messages,
            extra={"wing": "ai", "lane": "conversations"},
        )

    def assistant(identifier: str, text: str) -> Message:
        return Message(role="assistant", channel="reply", id=identifier, text=text)

    answer = "\n\n".join(ANSWER_LINES)
    chunks = ["\n\n".join(ANSWER_LINES[start : start + 2]) for start in (0, 2, 4)]
    together = chat(
        "chunked",
        [
            typed("u0", "Please review the repository."),
            assistant("a0", answer),
            *(typed(f"u{n}", chunk) for n, chunk in enumerate(chunks, start=1)),
        ],
    )
    apart = chat(
        "separated",
        [
            typed("u0", "Please review the repository."),
            assistant("a0", answer),
            typed("u1", chunks[0]),
            assistant("a1", "An unrelated reply about the weather and the walk home."),
            typed("u2", chunks[1]),
            assistant("a2", "Another unrelated reply about breakfast and the garden."),
            typed("u3", chunks[2]),
        ],
    )
    directory.mkdir(parents=True, exist_ok=True)
    with Library.create(directory / "library.sqlite") as library:
        library.put(together)
        library.put(apart)
    return directory / "library.sqlite"


def pasted_back_spans(library: Path) -> dict[str, list[tuple[int, str]]]:
    found: dict[str, list[tuple[int, str]]] = {}
    with Library(library) as handle:
        deadline = Deadline(60)
        census = build_census(
            handle, limit_records=None, min_chars=200, min_occurrences=3, deadline=deadline
        )

        def visit(message, spans) -> None:
            for span in spans:
                if span.rule == "pasted_back":
                    found.setdefault(message.uid.removeprefix("chatgpt-"), []).append(
                        (message.index, message.text[span.start : span.end])
                    )

        scan(handle, census, limit_records=None, deadline=deadline, visit=visit)
    return found


def test_an_answer_pasted_back_in_consecutive_messages_is_found_as_one_stretch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_chunked_paste_library(tmp_path / "store" / "g")
    found = pasted_back_spans(library)
    # Each message holds two short paragraphs, which are not enough on their own (77 characters);
    # the three consecutive messages together are. The separated copy has an assistant turn
    # between its messages, so each stays a stretch of its own.
    assert found == {
        "chunked": [
            (index, line)
            for index, line in (
                (2, ANSWER_LINES[0]),
                (2, ANSWER_LINES[1]),
                (3, ANSWER_LINES[2]),
                (3, ANSWER_LINES[3]),
                (4, ANSWER_LINES[4]),
                (4, ANSWER_LINES[5]),
            )
        ]
    }
    report = report_of(capsys, library)
    assert report["partition"]["ok"] is True
    assert report["scan"]["messages"] == 12


def test_a_limited_scan_counts_only_the_spans_it_saw(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library_path = build_library(tmp_path / "store" / "g", typed_records=110)
    ledger = approve(capsys, library_path, tmp_path)
    report = report_of(
        capsys, library_path, "--ledger", ledger, "--owner-id", "rob", "--limit-records", "40"
    )
    assert report["profile_eligible"] == [] and report["gate"]["orphan_rows"] > 0


def test_apply_refuses_to_overwrite_or_to_write_into_the_library(
    library_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "taken.json"
    out.write_text("keep me", encoding="utf-8")
    code, _, err = run_cli(capsys, "apply", "--library", library_path, "--out", out)
    assert code == 2 and "already exists" in err
    assert out.read_text(encoding="utf-8") == "keep me"
    inside = library_path.parent / "annotations.json"
    code, _, err = run_cli(capsys, "apply", "--library", library_path, "--out", inside)
    assert code == 2 and "outside the library" in err
    assert not inside.exists()


def test_apply_limit_records_flags_the_slice(
    library_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, result = apply_entries(capsys, library_path, tmp_path / "slice.json", "--limit-records", "2")
    assert result["records_scanned"] == 2 and result["limited"] is True
    assert result["complete"] is False and result["partial"] is True
    assert not (tmp_path / "slice.json").exists()


# --- review sample, record and the gate ------------------------------------------------------


def sample_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def sample_header(path: Path) -> dict:
    header, *_ = sample_lines(path)
    assert header["schema"] == "cce.attribution-review-sample-header.v6"
    return header


def sample_rows(path: Path) -> list[dict]:
    """The rows of a sample (or of an answers file): everything after the header, if any."""
    return [row for row in sample_lines(path) if row["schema"] == SAMPLE_SCHEMA]


def test_review_sample_is_deterministic_bounded_and_shows_only_the_span_and_its_rule(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "g", typed_records=130)
    first, second = tmp_path / "s1.jsonl", tmp_path / "s2.jsonl"
    assert (
        run_cli(capsys, "review-sample", "--owner-id", "rob", "--library", library, "--out", first)[
            0
        ]
        == 0
    )
    assert (
        run_cli(
            capsys, "review-sample", "--owner-id", "rob", "--library", library, "--out", second
        )[0]
        == 0
    )
    assert first.read_bytes() == second.read_bytes()
    rows = sample_rows(first)
    per_stratum = Counter((row["rule"], row["stratum"]) for row in rows)
    assert per_stratum[("typed_prefix", "S")] == 100 and max(per_stratum.values()) <= 100
    assert all(set(row) == SAMPLE_KEYS and row["verdict"] is None for row in rows)
    assert all(0 < len(row["text"]) <= 240 for row in rows if row["stratum"] == "S")
    assert all(0 < len(row["text"]) < 400 for row in rows)
    blob = first.read_text(encoding="utf-8")
    assert "chatgpt-" not in blob and "claudecode" not in blob and "owner-chat" not in blob
    few = tmp_path / "few.jsonl"
    run_cli(
        capsys,
        "review-sample",
        "--owner-id",
        "rob",
        "--library",
        library,
        "--out",
        few,
        "--per-rule",
        "4",
    )
    assert max(Counter((r["rule"], r["stratum"]) for r in sample_rows(few)).values()) == 4
    also = tmp_path / "also.jsonl"
    run_cli(
        capsys,
        "review-sample",
        "--owner-id",
        "rob",
        "--library",
        library,
        "--out",
        also,
        "--per-stratum",
        "4",
    )
    assert also.read_bytes() == few.read_bytes()


def test_review_sample_streams_to_standard_output_and_reports_the_short_span_bias(
    library_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run_cli(
        capsys, "review-sample", "--owner-id", "rob", "--library", library_path
    )
    assert code == 0
    assert all(json.loads(line)["schema"] for line in out.splitlines())
    header, *body = map(json.loads, out.splitlines())
    assert header["schema"] == "cce.attribution-review-sample-header.v6"
    summary = json.loads(err.strip().splitlines()[-1])
    assert summary["sampled"] == len(body) == header["rows"]
    assert summary["sample_digest"] == header["sample_digest"] and header["owner_id"] == "rob"
    strata = {key.split(":")[1] for key in summary["per_stratum"]}
    assert strata == {"S", "M"}
    medium = [row for row in body if row["stratum"] == "M"]
    assert medium and all(240 < row["length"] <= 2000 for row in medium)
    assert all(row["excerpted"] is (row["length"] > 360) for row in medium)


def answered(sample: Path, target: Path, yes: int, stratum: str = "S") -> None:
    lines, seen = [], 0
    for row in sample_rows(sample):
        if row["rule"] == "typed_prefix" and row["stratum"] == stratum:
            row["verdict"] = "yes" if seen < yes else "no"
            seen += 1
        lines.append(json.dumps(row))
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


def record_answers(
    capsys: pytest.CaptureFixture[str],
    library: Path,
    ledger: Path,
    sample: Path,
    answers: Path,
    *extra: str,
    reviewer: str = "rob",
) -> tuple[int, str, str]:
    return run_cli(
        capsys,
        "review-record",
        "--library",
        library,
        "--ledger",
        ledger,
        "--sample",
        sample,
        "--answers",
        answers,
        "--reviewer",
        reviewer,
        "--reviewed-at",
        STAMP,
        *extra,
    )


def approve(
    capsys: pytest.CaptureFixture[str],
    library: Path,
    tmp_path: Path,
    *,
    yes: int = 100,
    reviewer: str = "rob",
    name: str = "approved",
) -> Path:
    """Sample, answer the short typed_prefix rows and record them: a ledger of real reviews."""
    sample, answers, ledger = (tmp_path / f"{name}-{kind}.jsonl" for kind in ("s", "a", "l"))
    drawn = run_cli(
        capsys, "review-sample", "--owner-id", reviewer, "--library", library, "--out", sample
    )
    assert drawn[0] == 0
    answered(sample, answers, yes)
    code, _, err = record_answers(capsys, library, ledger, sample, answers, reviewer=reviewer)
    assert code == 0, err
    return ledger


@pytest.mark.parametrize(("yes", "eligible"), [(95, True), (94, False)])
def test_sampling_answering_and_reporting_close_the_gate_loop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], yes: int, eligible: bool
) -> None:
    library = build_library(tmp_path / "store" / "g", typed_records=130)
    sample, answers, ledger = (tmp_path / name for name in ("s.jsonl", "a.jsonl", "l.jsonl"))
    run_cli(capsys, "review-sample", "--owner-id", "rob", "--library", library, "--out", sample)
    answered(sample, answers, yes)
    code, out, err = record_answers(capsys, library, ledger, sample, answers, "--json")
    assert code == 0, err
    recorded = json.loads(out)
    drawn = sample_rows(sample)
    # Every row the sample drew is recorded; the ones nobody answered are recorded as unsure.
    assert recorded["appended"] == len(drawn) > 100
    assert recorded["unanswered"] == len(drawn) - 100 > 0
    assert recorded["strata"]["typed_prefix:S"]["appended"] == 100
    report = report_of(capsys, library, "--ledger", ledger, "--owner-id", "rob")
    row = next(item for item in report["rules"] if item["rule"] == "typed_prefix")
    short = row["strata"]["S"]
    assert (short["reviewed"], short["confirmed"], short["eligible"]) == (100, yes, eligible)
    assert not row["strata"]["M"]["eligible"] and not row["strata"]["L"]["eligible"]
    assert report["profile_eligible"] == (["typed_prefix:S"] if eligible else [])
    assert report["gate"]["ledger_rows"] == len(drawn)
    assert (report["gate"]["orphan_rows"], report["gate"]["non_owner_rows"]) == (0, 0)
    _, result = apply_entries(
        capsys, library, tmp_path / "out.json", "--ledger", ledger, "--owner-id", "rob"
    )
    assert (result["profile_eligible_spans"] > 0) is eligible
    again = tmp_path / "again.jsonl"
    run_cli(
        capsys,
        "review-sample",
        "--owner-id",
        "rob",
        "--library",
        library,
        "--ledger",
        ledger,
        "--out",
        again,
    )
    reviewed = {row["span_id"] for row in drawn}
    assert len(reviewed) == len(drawn)
    assert reviewed.isdisjoint(row["span_id"] for row in sample_rows(again))


def eligible_owner_texts(library_path: Path, entries: list[dict]) -> list[str]:
    texts: list[str] = []
    with Library(library_path) as library:
        for entry in entries:
            text = message_text(library, entry["source"])
            texts.extend(
                span["text"]
                for span in attributed_spans(text, entry["attribution"])
                if owner_profile_eligible(span, "rob")
            )
    return texts


def test_a_long_pasted_answer_is_not_eligible_until_its_own_stratum_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(
        tmp_path / "store" / "g", typed_records=110, medium_records=105, pasted_answer=True
    )
    sample, ledger = tmp_path / "sample.jsonl", tmp_path / "ledger.jsonl"
    run_cli(capsys, "review-sample", "--owner-id", "rob", "--library", library, "--out", sample)

    short_answers = tmp_path / "short.jsonl"
    answered(sample, short_answers, 100, stratum="S")
    assert record_answers(capsys, library, ledger, sample, short_answers)[0] == 0
    entries, result = apply_entries(
        capsys, library, tmp_path / "one.json", "--ledger", ledger, "--owner-id", "rob"
    )
    assert result["eligible_strata"] == ["typed_prefix:S"]
    texts = eligible_owner_texts(library, entries)
    assert texts and not any("tokenizes then builds" in text for text in texts)
    assert all(len(text) <= 240 for text in texts)

    # A sample is recorded once, so the medium answers are given in the one recording of the
    # same draw into another ledger: a row left blank would have been recorded as unsure.
    medium_answers, both_ledger = tmp_path / "medium.jsonl", tmp_path / "both-ledger.jsonl"
    answered(sample, medium_answers, 100, stratum="M")
    both = [json.loads(line) for line in medium_answers.read_text(encoding="utf-8").splitlines()]
    short_by_id = {row["span_id"]: row["verdict"] for row in sample_rows(short_answers)}
    for row in both:
        row["verdict"] = row["verdict"] or short_by_id[row["span_id"]]
    medium_answers.write_text("".join(json.dumps(row) + "\n" for row in both), encoding="utf-8")
    assert record_answers(capsys, library, both_ledger, sample, medium_answers)[0] == 0
    entries, result = apply_entries(
        capsys, library, tmp_path / "two.json", "--ledger", both_ledger, "--owner-id", "rob"
    )
    assert result["eligible_strata"] == ["typed_prefix:M", "typed_prefix:S"]
    texts = eligible_owner_texts(library, entries)
    assert any("tokenizes then builds" in text for text in texts)
    assert max(len(text) for text in texts) <= 2000


def span_id_of(span: dict) -> str:
    return next(item.split("span:")[1] for item in span["evidence"] if item.startswith("span:"))


def test_spans_the_owner_answered_no_are_never_eligible_once_their_stratum_opens(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "g", typed_records=130)
    sample, answers, ledger = (tmp_path / name for name in ("s.jsonl", "a.jsonl", "l.jsonl"))
    run_cli(capsys, "review-sample", "--owner-id", "rob", "--library", library, "--out", sample)
    answered(sample, answers, 97)
    rejected = {row["span_id"] for row in sample_rows(answers) if row["verdict"] == "no"}
    assert len(rejected) == 3
    assert record_answers(capsys, library, ledger, sample, answers)[0] == 0
    entries, result = apply_entries(
        capsys, library, tmp_path / "out.json", "--ledger", ledger, "--owner-id", "rob"
    )
    assert result["eligible_strata"] == ["typed_prefix:S"]
    assert result["profile_eligible_spans"] > 0
    assert result["spans_withheld_by_verdict"] == 3
    seen = {
        span_id_of(span): span
        for entry in entries
        for span in entry["attribution"]["spans"]
        if span_id_of(span) in rejected
    }
    assert set(seen) == rejected
    assert all(span["profile_eligible"] is False for span in seen.values())
    assert all(span["owner_verdict"] == "no" for span in seen.values())
    confirmed = {row["span_id"] for row in sample_rows(answers) if row["verdict"] == "yes"}
    flags = {
        span_id_of(span): span["profile_eligible"]
        for entry in entries
        for span in entry["attribution"]["spans"]
        if span_id_of(span) in confirmed
    }
    assert flags and all(flags.values())
    refused_texts = {row["text"] for row in sample_rows(answers) if row["verdict"] == "no"}
    owned = set(eligible_owner_texts(library, entries))
    assert owned and refused_texts.isdisjoint(owned)


def answers_for(sample: Path, target: Path, decide) -> None:
    """An answers file: the sample's rows, each with the verdict `decide(row)` (None: blank)."""
    rows = sample_rows(sample)
    for row in rows:
        row["verdict"] = decide(row)
    target.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_skipping_the_hard_rows_and_drawing_again_cannot_open_a_stratum(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A quarter of the typed turns read like a model's: the owner leaves exactly those blank.
    library = build_library(tmp_path / "store" / "g", typed_records=330, marked_every=4)
    ledger = tmp_path / "ledger.jsonl"
    seen: set[str] = set()
    for number in range(3):
        sample, answers = tmp_path / f"s{number}.jsonl", tmp_path / f"a{number}.jsonl"
        extra = ("--ledger", ledger) if ledger.exists() else ()
        drawn = run_cli(
            capsys,
            "review-sample",
            "--owner-id",
            "rob",
            "--library",
            library,
            *extra,
            "--out",
            sample,
        )
        assert drawn[0] == 0
        # A blank row stays in the ledger as unsure, so the next sample never draws it again.
        ids = {row["span_id"] for row in sample_rows(sample)}
        assert ids and seen.isdisjoint(ids)
        seen |= ids
        answers_for(sample, answers, lambda row: None if "ASSIST" in row["text"] else "yes")
        assert record_answers(capsys, library, ledger, sample, answers)[0] == 0
        report = report_of(capsys, library, "--ledger", ledger, "--owner-id", "rob")
        assert "typed_prefix:S" not in report["profile_eligible"], number
    short = next(item for item in report["rules"] if item["rule"] == "typed_prefix")["strata"]["S"]
    assert short["reviewed"] >= 250 and short["precision"] < 0.8
    entries, result = apply_entries(
        capsys, library, tmp_path / "out.json", "--ledger", ledger, "--owner-id", "rob"
    )
    assert result["eligible_strata"] == [] and result["profile_eligible_spans"] == 0
    flagged = [span for entry in entries for span in entry["attribution"]["spans"]]
    assert not any(span["profile_eligible"] for span in flagged)


def test_every_row_of_a_sample_is_recorded_and_a_blank_row_is_unsure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "g", typed_records=6)
    sample, answers, ledger = (tmp_path / name for name in ("s.jsonl", "a.jsonl", "l.jsonl"))
    run_cli(capsys, "review-sample", "--owner-id", "rob", "--library", library, "--out", sample)
    rows = sample_rows(sample)
    first = next(row for row in rows if row["rule"] == "typed_prefix")
    answers.write_text(json.dumps({**first, "verdict": "yes"}) + "\n", encoding="utf-8")
    code, out, err = record_answers(capsys, library, ledger, sample, answers, "--json")
    assert code == 0, err
    result = json.loads(out)
    recorded = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert len(recorded) == len(rows) == result["appended"] > 1
    assert {row["span_id"] for row in recorded} == {row["span_id"] for row in rows}
    verdicts = {row["span_id"]: row["verdict"] for row in recorded}
    assert verdicts.pop(first["span_id"]) == "yes"
    assert set(verdicts.values()) == {"unsure"} and result["unanswered"] == len(rows) - 1
    header = sample_header(sample)
    assert {row["sample_digest"] for row in recorded} == {header["sample_digest"]}
    by_id = {row["span_id"]: row for row in rows}
    for row in recorded:
        assert row["text_sha256"] == by_id[row["span_id"]]["text_sha256"]
        assert row["drawn"] == header["drawn"][f"{row['rule']}:{row['stratum']}"]


def test_there_is_no_skip_and_a_span_cannot_be_answered_twice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "g", typed_records=3)
    sample, answers, ledger = (tmp_path / name for name in ("s.jsonl", "a.jsonl", "l.jsonl"))
    run_cli(capsys, "review-sample", "--owner-id", "rob", "--library", library, "--out", sample)
    first = next(row for row in sample_rows(sample) if row["rule"] == "typed_prefix")
    for verdict in ("skip", "n/a", "maybe"):
        answers.write_text(json.dumps({**first, "verdict": verdict}) + "\n", encoding="utf-8")
        code, _, err = record_answers(capsys, library, ledger, sample, answers)
        assert code == 2 and "answers line 1" in err and "no skip" in err, verdict
        assert not ledger.exists()
    answers.write_text(
        json.dumps({**first, "verdict": "yes"})
        + "\n"
        + json.dumps({**first, "verdict": "no"})
        + "\n",
        encoding="utf-8",
    )
    code, _, err = record_answers(capsys, library, ledger, sample, answers)
    assert code == 2 and "answers line 2" in err and "twice" in err and not ledger.exists()
    answers.write_text("", encoding="utf-8")
    assert record_answers(capsys, library, ledger, sample, answers)[0] == 0
    recorded = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert recorded and {row["verdict"] for row in recorded} == {"unsure"}


def test_review_record_refuses_a_tampered_hand_written_or_foreign_sample(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "g", typed_records=3)
    other = build_library(tmp_path / "other" / "g", typed_records=4)
    sample, answers = tmp_path / "s.jsonl", tmp_path / "a.jsonl"
    run_cli(capsys, "review-sample", "--owner-id", "rob", "--library", library, "--out", sample)
    answers_for(sample, answers, lambda row: "yes")
    lines = sample.read_text(encoding="utf-8").splitlines()
    typed_line = next(i for i, line in enumerate(lines) if '"rule": "typed_prefix"' in line)
    cases = {
        "an edited text": (
            [
                *lines[:typed_line],
                lines[typed_line].replace('"text": "', '"text": "Edited '),
                *lines[typed_line + 1 :],
            ],
            library,
            "rob",
            "does not match",
        ),
        "a dropped row": (
            [*lines[:typed_line], *lines[typed_line + 1 :]],
            library,
            "rob",
            "altered",
        ),
        "no header": (lines[1:], library, "rob", "no header"),
        "another library": (lines, other, "rob", "different library"),
        "another owner": (lines, library, "someone-else", "another owner"),
    }
    for name, (body, target, reviewer, message) in cases.items():
        forged, ledger = tmp_path / f"{len(name)}.jsonl", tmp_path / f"{len(name)}-l.jsonl"
        forged.write_text("\n".join(body) + "\n", encoding="utf-8")
        code, _, err = record_answers(capsys, target, ledger, forged, answers, reviewer=reviewer)
        assert code == 2 and message in err, (name, err)
        assert not ledger.exists(), name


def test_review_sample_needs_an_owner_identity_and_binds_the_sample_to_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "g", typed_records=3)
    code, _, err = run_cli(capsys, "review-sample", "--library", library)
    assert code == 2 and "--owner-id" in err
    ledger = tmp_path / "l.jsonl"
    ledger.write_text("", encoding="utf-8")
    code, _, err = run_cli(capsys, "review-sample", "--library", library, "--ledger", ledger)
    assert code == 2 and "--owner-id" in err
    sample = tmp_path / "s.jsonl"
    assert (
        run_cli(
            capsys, "review-sample", "--owner-id", "rob", "--library", library, "--out", sample
        )[0]
        == 0
    )
    header = sample_header(sample)
    assert header["owner_id"] == "rob" and header["rules_version"] == RULES_VERSION
    assert header["rows"] == len(sample_rows(sample)) and header["partial"] is False
    with Library(library) as handle:
        assert header["library"] == attribution_commands.library_fingerprint(handle)


def test_review_sample_hides_only_spans_the_owner_already_reviewed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "g", typed_records=130)
    fresh = tmp_path / "fresh.jsonl"
    run_cli(capsys, "review-sample", "--owner-id", "rob", "--library", library, "--out", fresh)
    # Somebody else's reviews of these very spans, and rows of the owner that name no span.
    others = approve(capsys, library, tmp_path, reviewer="agent:codex", name="other")
    orphans = tmp_path / "orphans.jsonl"
    append_ledger(
        orphans,
        [
            ledger_row(
                f"{index:032x}",
                "typed_prefix",
                "yes",
                "rob",
                STAMP,
                stratum="S",
                sample_digest="a" * 64,
                text_sha256="b" * 64,
                text_key="b" * 64,
                drawn=50,
            )
            for index in range(50)
        ],
    )
    noise = tmp_path / "noise.jsonl"
    noise.write_text(
        others.read_text(encoding="utf-8") + orphans.read_text(encoding="utf-8"), encoding="utf-8"
    )
    again = tmp_path / "again.jsonl"
    code, _, err = run_cli(
        capsys,
        "review-sample",
        "--owner-id",
        "rob",
        "--library",
        library,
        "--ledger",
        noise,
        "--out",
        again,
    )
    assert code == 0, err
    # The same rows; only the header's ledger state differs (the owner's orphan rows are in it).
    assert sample_rows(again) == sample_rows(fresh)
    assert sample_header(again)["exclusion_digest"] != sample_header(fresh)["exclusion_digest"]
    mine = approve(capsys, library, tmp_path, name="mine")
    later = tmp_path / "later.jsonl"
    run_cli(
        capsys,
        "review-sample",
        "--owner-id",
        "rob",
        "--library",
        library,
        "--ledger",
        mine,
        "--out",
        later,
    )
    reviewed = {row["span_id"] for row in sample_rows(fresh)}
    assert reviewed and reviewed.isdisjoint(row["span_id"] for row in sample_rows(later))


def test_a_row_counts_only_if_the_scans_span_has_the_text_it_answered(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "g", typed_records=110)
    ledger = approve(capsys, library, tmp_path)
    assert report_of(capsys, library, "--ledger", ledger, "--owner-id", "rob")[
        "profile_eligible"
    ] == ["typed_prefix:S"]
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    target = next(row for row in rows if (row["rule"], row["stratum"]) == ("typed_prefix", "S"))
    target["text_sha256"] = "0" * 64
    forged = tmp_path / "forged-ledger.jsonl"
    forged.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    report = report_of(capsys, library, "--ledger", forged, "--owner-id", "rob")
    assert report["gate"]["text_mismatch_rows"] == 1 and report["gate"]["orphan_rows"] == 0
    short = next(item for item in report["rules"] if item["rule"] == "typed_prefix")["strata"]["S"]
    assert short["reviewed"] == 99 and short["eligible"] is False
    assert report["profile_eligible"] == []
    _, result = apply_entries(
        capsys, library, tmp_path / "f.json", "--ledger", forged, "--owner-id", "rob"
    )
    assert result["text_mismatch_rows"] == 1 and result["eligible_strata"] == []


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        (("--seed", "another-seed"), "another seed"),
        (("--limit-records", "2"), "only part of the library"),
        (("--repeat-min-chars", "100"), "repeat-census"),
        (("--repeat-min-occurrences", "5"), "repeat-census"),
    ],
)
def test_only_the_default_draw_over_the_whole_library_can_be_recorded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], extra: tuple[str, ...], reason: str
) -> None:
    library = build_library(tmp_path / "store" / "g", typed_records=5)
    sample, answers, ledger = (tmp_path / name for name in ("s.jsonl", "a.jsonl", "l.jsonl"))
    code, _, err = run_cli(
        capsys,
        "review-sample",
        "--owner-id",
        "rob",
        "--library",
        library,
        "--out",
        sample,
        *extra,
    )
    assert code == 0, err
    preview = sample if sample.exists() else sample.with_name("s.jsonl.partial")
    answers.write_text("", encoding="utf-8")
    code, _, err = record_answers(capsys, library, ledger, preview, answers)
    assert code == 2 and "cannot be recorded" in err and reason in err, err
    assert not ledger.exists()


def test_a_bigger_or_smaller_batch_of_the_default_draw_can_still_be_recorded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "g", typed_records=30)
    ledger = tmp_path / "l.jsonl"
    seen: set[str] = set()
    for number, size in enumerate(("3", "7")):
        sample, answers = tmp_path / f"s{number}.jsonl", tmp_path / f"a{number}.jsonl"
        extra = ("--ledger", ledger) if ledger.exists() else ()
        run_cli(
            capsys,
            "review-sample",
            "--owner-id",
            "rob",
            "--library",
            library,
            *extra,
            "--per-stratum",
            size,
            "--out",
            sample,
        )
        ids = {row["span_id"] for row in sample_rows(sample)}
        assert ids and seen.isdisjoint(ids)
        seen |= ids
        answers.write_text("", encoding="utf-8")
        assert record_answers(capsys, library, ledger, sample, answers)[0] == 0


def test_a_sample_can_be_recorded_once_in_order_into_the_ledger_it_was_drawn_against(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The ledger-shopping recipe: draw against a throwaway ledger, record every draw there so
    the next draw moves on, and record only the good-looking draws into the real ledger."""
    library = build_library(tmp_path / "store" / "g", typed_records=40)
    throwaway, real = tmp_path / "throwaway.jsonl", tmp_path / "real.jsonl"
    first, second, third = (tmp_path / f"s{n}.jsonl" for n in (1, 2, 3))
    answers = [tmp_path / f"a{n}.jsonl" for n in (1, 2, 3)]

    def draw(sample: Path, *extra: str | Path) -> None:
        code, _, err = run_cli(
            capsys,
            "review-sample",
            "--owner-id",
            "rob",
            "--library",
            library,
            "--per-stratum",
            "2",
            *extra,
            "--out",
            sample,
        )
        assert code == 0, err

    draw(first)
    answers_for(first, answers[0], lambda row: None)
    assert record_answers(capsys, library, throwaway, first, answers[0])[0] == 0
    draw(second, "--ledger", throwaway)
    answers_for(second, answers[1], lambda row: "yes")
    # The second draw was made against the throwaway ledger: the real one is another state.
    code, _, err = record_answers(capsys, library, real, second, answers[1])
    assert code == 2 and "different ledger state" in err and not real.exists()
    assert record_answers(capsys, library, throwaway, second, answers[1])[0] == 0
    # Recorded once: the ledger moved on, so the same sample is a draw against an older state.
    code, _, err = record_answers(capsys, library, throwaway, second, answers[1])
    assert code == 2 and "different ledger state" in err
    # The first draw, made against nothing, still fits an empty ledger (and only that).
    assert record_answers(capsys, library, real, first, answers[0])[0] == 0
    draw(third, "--ledger", real)
    answers_for(third, answers[2], lambda row: "yes")
    code, _, err = record_answers(capsys, library, throwaway, third, answers[2])
    assert code == 2 and "different ledger state" in err
    assert record_answers(capsys, library, real, third, answers[2])[0] == 0


def test_rows_of_other_reviewers_and_of_other_versions_do_not_change_the_draw(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "g", typed_records=20)
    others = approve(capsys, library, tmp_path, reviewer="agent:codex", name="other")
    sample, answers = tmp_path / "s.jsonl", tmp_path / "a.jsonl"
    run_cli(
        capsys, "review-sample", "--owner-id", "rob", "--library", library, "--ledger", others,
        "--out", sample,
    )  # fmt: skip
    assert sample_header(sample)["exclusion_digest"] == hashlib.sha256(b"[]").hexdigest()
    answered(sample, answers, 5)
    fresh = tmp_path / "fresh.jsonl"
    assert record_answers(capsys, library, fresh, sample, answers)[0] == 0
    assert record_answers(capsys, library, others, sample, answers, reviewer="rob")[0] == 0


def test_a_hand_cut_sample_under_a_rebuilt_header_is_not_the_draw_and_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The header can be rebuilt with a correct digest, library fingerprint and ledger state; the
    rows are still checked against a fresh draw, so rows cannot be cherry-picked."""
    library = build_library(tmp_path / "store" / "g", typed_records=130, marked_every=4)
    sample, answers, ledger = (tmp_path / name for name in ("s.jsonl", "a.jsonl", "l.jsonl"))
    run_cli(capsys, "review-sample", "--owner-id", "rob", "--library", library, "--out", sample)
    rows = sample_rows(sample)
    hard = [row for row in rows if row["rule"] == "typed_prefix" and "ASSIST" in row["text"]]
    assert hard  # the rows a cherry-picker would like to leave out
    cut = [row for row in rows if row not in hard]
    forged = tmp_path / "cut.jsonl"
    forge_sample(sample, forged, cut)
    assert parse_sample_file(forged)  # internally consistent: the integrity checks pass
    answers_for(forged, answers, lambda row: "yes")
    code, _, err = record_answers(capsys, library, ledger, forged, answers)
    assert code == 2 and "not the draw" in err and f"lacks {len(hard)} row" in err, err
    assert not ledger.exists()
    # Padding with a row the draw would not give, and reordering the rows, are refused as well.
    reordered = tmp_path / "reordered.jsonl"
    swapped = [rows[1], rows[0], *rows[2:]]
    forge_sample(sample, reordered, swapped)
    answers_for(reordered, answers, lambda row: "yes")
    code, _, err = record_answers(capsys, library, ledger, reordered, answers)
    assert code == 2 and "not in the order of the draw" in err and not ledger.exists()
    edited = tmp_path / "edited.jsonl"
    forge_sample(sample, edited, [{**rows[0], "length": rows[0]["length"] + 1}, *rows[1:]])
    code, _, err = record_answers(capsys, library, ledger, edited, answers)
    assert code == 2 and not ledger.exists()
    # The honest sample, whole, is recorded.
    answers_for(sample, answers, lambda row: "yes")
    assert record_answers(capsys, library, ledger, sample, answers)[0] == 0


def parse_sample_file(path: Path) -> object:
    return attribution_rules.parse_sample(path.read_text(encoding="utf-8").split("\n"))


def test_a_sample_drawn_before_the_library_changed_is_refused_and_says_why(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "g", typed_records=20)
    sample, answers, ledger = (tmp_path / name for name in ("s.jsonl", "a.jsonl", "l.jsonl"))
    run_cli(capsys, "review-sample", "--owner-id", "rob", "--library", library, "--out", sample)
    answers_for(sample, answers, lambda row: "yes" if row["claim"] == "owner" else None)
    with Library(library, writable=True) as handle:
        handle.put(
            Conversation(
                provider="chatgpt",
                source_id="late-arrival",
                title="late",
                messages=[typed("m0", "A message imported after the draw.")],
                extra={"wing": "ai", "lane": "conversations"},
            )
        )
    code, _, err = record_answers(capsys, library, ledger, sample, answers)
    assert code == 2 and "different library" in err and "draw a new sample" in err, err
    assert not ledger.exists()


def test_the_draw_is_checked_again_while_the_ledger_is_locked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    library = build_library(tmp_path / "store" / "g", typed_records=130)
    ledger = approve(capsys, library, tmp_path, yes=100)
    sample, answers = tmp_path / "s2.jsonl", tmp_path / "a2.jsonl"
    run_cli(
        capsys,
        "review-sample",
        "--owner-id",
        "rob",
        "--library",
        library,
        "--ledger",
        ledger,
        "--out",
        sample,
    )
    answers_for(sample, answers, lambda row: "yes" if row["claim"] == "owner" else None)
    real_scan = attribution_commands.scan
    seen: list[str] = []

    def watching(*args: object, **kwargs: object) -> object:
        # Another process trying to take the ledger now must be turned away.
        probe = os.open(ledger, os.O_RDWR)
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            seen.append("free")
        except BlockingIOError:
            seen.append("locked")
        finally:
            os.close(probe)
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(attribution_commands, "scan", watching)
    code, _, err = record_answers(capsys, library, ledger, sample, answers)
    assert code == 0, err
    assert seen == ["locked"]


def test_drawing_the_sample_again_is_bounded_by_the_deadline_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "g", typed_records=20)
    sample, answers, ledger = (tmp_path / name for name in ("s.jsonl", "a.jsonl", "l.jsonl"))
    run_cli(capsys, "review-sample", "--owner-id", "rob", "--library", library, "--out", sample)
    answers_for(sample, answers, lambda row: "yes" if row["claim"] == "owner" else None)
    code, _, err = record_answers(
        capsys, library, ledger, sample, answers, "--deadline-seconds", "1e-9"
    )
    assert code == 3 and "nothing was written" in err and not ledger.exists()
    assert record_answers(capsys, library, ledger, sample, answers)[0] == 0


def edit_header(source: Path, target: Path, **changes: object) -> Path:
    """The sample file with some header fields changed and nothing else recomputed."""
    lines = source.read_text(encoding="utf-8").splitlines()
    header = {**json.loads(lines[0]), **changes}
    lines[0] = json.dumps(header, ensure_ascii=False, sort_keys=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def test_a_shopped_sample_cannot_be_made_recordable_by_editing_its_header(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "g", typed_records=30)
    previewed, answers = tmp_path / "shopped.jsonl", tmp_path / "a.jsonl"
    code, _, err = run_cli(
        capsys,
        "review-sample",
        "--owner-id",
        "rob",
        "--library",
        library,
        "--seed",
        "shopping-seed-17",
        "--limit-records",
        "20",
        "--out",
        previewed,
    )
    assert code == 0, err
    preview = previewed.with_name("shopped.jsonl.partial")
    answered(preview, answers, 3)
    default = tmp_path / "default.jsonl"
    run_cli(capsys, "review-sample", "--owner-id", "rob", "--library", library, "--out", default)
    edits = {
        "seed": SAMPLE_SEED,
        "partial": False,
        "repeat_min_chars": 200,
        "library": sample_header(default)["library"],
        "exclusion_digest": "1" * 64,
        "per_stratum": 7,
    }
    for key, value in edits.items():
        forged = edit_header(preview, tmp_path / f"forged-{key}.jsonl", **{key: value})
        ledger = tmp_path / f"ledger-{key}.jsonl"
        code, _, err = record_answers(capsys, library, ledger, forged, answers)
        assert code == 2 and not ledger.exists(), key
        assert "altered" in err or "cannot be recorded" in err, (key, err)
    # Editing the header of a real, recordable sample is refused as well.
    answered(default, answers, 3)
    for key, value in (("owner_id", "someone"), ("library", "e" * 64), ("rows", 1), ("seed", "s")):
        forged = edit_header(default, tmp_path / f"real-{key}.jsonl", **{key: value})
        ledger = tmp_path / f"real-ledger-{key}.jsonl"
        code, _, err = record_answers(capsys, library, ledger, forged, answers)
        assert code == 2 and not ledger.exists(), key
    assert record_answers(capsys, library, tmp_path / "ok.jsonl", default, answers)[0] == 0


def typed_index(text: str) -> int:
    return int(re.search(r"Typed words number (\d+) ", text).group(1))


def test_rejected_texts_that_are_re_imported_do_not_reopen_a_closed_stratum(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "g", typed_records=140)
    sample, answers, ledger = (tmp_path / name for name in ("s.jsonl", "a.jsonl", "l.jsonl"))
    run_cli(
        capsys, "review-sample", "--owner-id", "rob", "--library", library, "--per-stratum", "106",
        "--out", sample,
    )  # fmt: skip
    short = [
        row for row in sample_rows(sample) if (row["rule"], row["stratum"]) == ("typed_prefix", "S")
    ]
    assert len(short) == 106
    rejected = [row["text"] for row in short if row["text"].startswith("Typed words number")][:9]
    assert len(rejected) == 9
    answers_for(sample, answers, lambda row: "no" if row["text"] in rejected else "yes")
    assert record_answers(capsys, library, ledger, sample, answers)[0] == 0
    report = report_of(capsys, library, "--ledger", ledger, "--owner-id", "rob")
    assert report["profile_eligible"] == []  # 97 of 106 confirmed
    # A re-import changes six of the nine rejected texts, so their span ids leave the scan.
    edited = {typed_index(text) for text in rejected[:6]}
    reimported = build_library(tmp_path / "other" / "g", typed_records=140, edited=edited)
    report = report_of(capsys, reimported, "--ledger", ledger, "--owner-id", "rob")
    gate_info = report["gate"]
    assert gate_info["orphan_negative_rows"] == 6 and gate_info["orphan_rows"] >= 6
    short_row = next(item for item in report["rules"] if item["rule"] == "typed_prefix")["strata"][
        "S"
    ]
    assert short_row["reviewed"] == 106 and short_row["confirmed"] == 97
    assert report["profile_eligible"] == []
    entries, result = apply_entries(
        capsys, reimported, tmp_path / "re.json", "--ledger", ledger, "--owner-id", "rob"
    )
    assert result["eligible_strata"] == [] and result["orphan_negative_rows"] == 6
    assert result["profile_eligible_spans"] == 0


def twin_library(directory: Path, texts: list[str]) -> Path:
    """Every text twice as an owner span: alone, and ahead of a quoted line. The two messages
    differ, so the two spans have the same text but not the same span id."""
    directory.mkdir(parents=True, exist_ok=True)
    with Library.create(directory / "library.sqlite") as handle:
        for copy, tail in (("a", ""), ("b", "\n\n> a quoted line from somewhere else")):
            for index, text in enumerate(texts):
                handle.put(
                    Conversation(
                        provider="chatgpt",
                        source_id=f"{copy}-{index:03d}",
                        title="t",
                        messages=[typed("m0", text + tail)],
                        extra={"wing": "ai", "lane": "conversations"},
                    )
                )
    return directory / "library.sqlite"


def test_the_identical_twin_of_a_rejected_span_is_never_eligible(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    texts = [
        f"My own note {index} on the river walk and step {index * 11}." for index in range(130)
    ]
    library = twin_library(tmp_path / "store" / "g", texts)
    sample, answers, ledger = (tmp_path / name for name in ("s.jsonl", "a.jsonl", "l.jsonl"))
    run_cli(capsys, "review-sample", "--owner-id", "rob", "--library", library, "--out", sample)
    drawn = [row for row in sample_rows(sample) if row["rule"] == "typed_prefix"]
    assert len(drawn) == 100  # one row per distinct text: its twin is not offered
    rejected = {row["text"] for row in drawn[:3]}
    answers_for(sample, answers, lambda row: "no" if row["text"] in rejected else "yes")
    assert record_answers(capsys, library, ledger, sample, answers)[0] == 0
    entries, result = apply_entries(
        capsys, library, tmp_path / "out.json", "--ledger", ledger, "--owner-id", "rob"
    )
    assert result["eligible_strata"] == ["typed_prefix:S"]
    owned = eligible_owner_texts(library, entries)
    assert owned and rejected.isdisjoint(owned)
    # Every confirmed text is eligible in both of its messages; a rejected text in neither.
    confirmed = {row["text"] for row in drawn} - rejected
    assert sum(text in confirmed for text in owned) == 2 * len(confirmed)
    assert result["spans_withheld_as_rejected_twin"] == len(rejected)
    assert result["spans_withheld_by_verdict"] == len(rejected)
    with Library(library) as handle:
        segments, _ = collect_owner_segments(handle, entries, "rob")
    assert rejected.isdisjoint(segments) and sorted(segments) == sorted(owned)


def fill_in_place(path: Path, decide) -> list[dict]:
    """Fill the verdicts of a sample file in place, header untouched (what the owner does)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines[1:]]
    for row in rows:
        row["verdict"] = decide(row)
    path.write_text(
        "\n".join([lines[0], *(json.dumps(row, sort_keys=True) for row in rows)]) + "\n",
        encoding="utf-8",
    )
    return rows


def test_the_sample_file_filled_in_can_be_passed_as_the_sample_and_as_the_answers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "g", typed_records=30)
    sample, ledger = tmp_path / "sample.jsonl", tmp_path / "ledger.jsonl"
    run_cli(capsys, "review-sample", "--owner-id", "rob", "--library", library, "--out", sample)
    header_line = sample.read_text(encoding="utf-8").splitlines()[0]
    rows = fill_in_place(sample, lambda row: "yes" if row["rule"] == "typed_prefix" else None)
    assert sample.read_text(encoding="utf-8").splitlines()[0] == header_line
    code, out, err = record_answers(capsys, library, ledger, sample, sample, "--json")
    assert code == 0, err
    recorded = json.loads(out)
    assert recorded["appended"] == len(rows)
    assert recorded["unanswered"] == sum(row["verdict"] is None for row in rows)
    verdicts = {
        row["span_id"]: row["verdict"]
        for row in map(json.loads, ledger.read_text(encoding="utf-8").splitlines())
    }
    assert {verdicts[row["span_id"]] for row in rows if row["rule"] == "typed_prefix"} == {"yes"}
    assert {verdicts[row["span_id"]] for row in rows if row["rule"] != "typed_prefix"} == {"unsure"}


def test_an_answers_file_with_a_header_must_carry_the_header_of_this_sample(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "g", typed_records=30)
    first, second = tmp_path / "first.jsonl", tmp_path / "second.jsonl"
    run_cli(capsys, "review-sample", "--owner-id", "rob", "--library", library, "--out", first)
    run_cli(
        capsys, "review-sample", "--owner-id", "rob", "--library", library, "--per-stratum", "3",
        "--out", second,
    )  # fmt: skip
    fill_in_place(first, lambda row: "yes")
    fill_in_place(second, lambda row: "yes")
    mixed = tmp_path / "mixed.jsonl"
    mixed.write_text(
        second.read_text(encoding="utf-8").splitlines()[0]
        + "\n"
        + "\n".join(first.read_text(encoding="utf-8").splitlines()[1:])
        + "\n",
        encoding="utf-8",
    )
    ledger = tmp_path / "ledger.jsonl"
    code, _, err = record_answers(capsys, library, ledger, first, mixed)
    assert code == 2 and "header" in err and not ledger.exists()
    edited = edit_header(first, tmp_path / "edited.jsonl", seed="another")
    code, _, err = record_answers(capsys, library, ledger, first, edited)
    assert code == 2 and "header" in err and not ledger.exists()
    # Only a header and no rows: nothing was answered, so every row is recorded as unsure.
    header_only = tmp_path / "header-only.jsonl"
    header_only.write_text(
        first.read_text(encoding="utf-8").splitlines()[0] + "\n", encoding="utf-8"
    )
    code, out, err = record_answers(capsys, library, ledger, first, header_only, "--json")
    assert code == 0, err
    recorded = json.loads(out)
    assert recorded["unanswered"] == recorded["appended"] > 0


def twin_text_library(directory: Path, texts: list[str], copies: int = 2) -> Path:
    """Each owner text typed `copies` times, every message ending in another injected reminder:
    the same owner span text, a different whole message, so a different span id each time."""
    directory.mkdir(parents=True, exist_ok=True)
    with Library.create(directory / "library.sqlite") as handle:
        for copy in range(copies):
            for index, text in enumerate(texts):
                reminder = (
                    f"<system-reminder>reminder number {copy} of text {index}</system-reminder>"
                )
                handle.put(
                    Conversation(
                        provider="chatgpt",
                        source_id=f"c{copy}-{index:03d}",
                        title="t",
                        messages=[typed("m0", f"{text}\n{reminder}", typed_chars=len(text))],
                        extra={"wing": "ai", "lane": "conversations"},
                    )
                )
    return directory / "library.sqlite"


def draw_and_record(
    capsys: pytest.CaptureFixture[str], library: Path, ledger: Path, tmp_path: Path, name: str
) -> list[dict]:
    """One round: draw against the ledger, confirm every owner row, record; the rows drawn."""
    sample, answers = tmp_path / f"{name}.jsonl", tmp_path / f"{name}-answers.jsonl"
    extra = ("--ledger", ledger) if ledger.exists() else ()
    code, _, err = run_cli(
        capsys, "review-sample", "--owner-id", "rob", "--library", library, *extra, "--out", sample
    )
    assert code == 0, err
    answers_for(sample, answers, lambda row: "yes" if row["claim"] == "owner" else None)
    assert record_answers(capsys, library, ledger, sample, answers)[0] == 0
    return [row for row in sample_rows(sample) if row["rule"] == "typed_prefix"]


def test_a_text_the_owner_answered_is_not_drawn_again_in_another_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    texts = [
        f"Owner sentence {index} about the garden plan number {index * 3}." for index in range(100)
    ]
    library = twin_text_library(tmp_path / "store" / "g", texts)
    ledger = tmp_path / "ledger.jsonl"
    first = draw_and_record(capsys, library, ledger, tmp_path, "one")
    assert {row["text"] for row in first} == set(texts)  # one row per distinct text
    second = draw_and_record(capsys, library, ledger, tmp_path, "two")
    assert second == []  # every text was answered, so no copy of it is shown again
    report = report_of(capsys, library, "--ledger", ledger, "--owner-id", "rob")
    short = next(item for item in report["rules"] if item["rule"] == "typed_prefix")["strata"]["S"]
    assert (short["reviewed"], short["confirmed"]) == (100, 100)
    assert short["eligible"] is True  # a hundred distinct texts, honestly reviewed
    assert report["gate"]["duplicate_text_rows"] == 0


def test_a_hundred_answers_over_fifty_distinct_texts_do_not_open_the_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    texts = [
        f"Owner sentence {index} about the garden plan number {index * 3}." for index in range(50)
    ]
    library = twin_text_library(tmp_path / "store" / "g", texts)
    ledger = tmp_path / "ledger.jsonl"
    first = draw_and_record(capsys, library, ledger, tmp_path, "one")
    second = draw_and_record(capsys, library, ledger, tmp_path, "two")
    assert len(first) == 50 and second == []
    entries, result = apply_entries(
        capsys, library, tmp_path / "out.json", "--ledger", ledger, "--owner-id", "rob"
    )
    assert result["eligible_strata"] == [] and result["profile_eligible_spans"] == 0
    short = next(
        item
        for item in report_of(capsys, library, "--ledger", ledger, "--owner-id", "rob")["rules"]
        if item["rule"] == "typed_prefix"
    )["strata"]["S"]
    assert (short["reviewed"], short["confirmed"], short["eligible"]) == (50, 50, False)


@pytest.mark.parametrize("forged_key", ["true", "invented"])
def test_answers_to_copies_of_a_text_forged_into_a_ledger_still_count_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], forged_key: str
) -> None:
    """Even if both copies of every text were answered (by a draw from before this rule, or by
    hand), the gate counts each text once; and a row that claims a made-up `text_key` for a span
    the scan finds is counted under the text key the scan computes, not the one it claims."""
    texts = [
        f"Owner sentence {index} about the garden plan number {index * 3}." for index in range(50)
    ]
    library = twin_text_library(tmp_path / "store" / "g", texts)
    first = tmp_path / "s1.jsonl"
    run_cli(capsys, "review-sample", "--owner-id", "rob", "--library", library, "--out", first)
    rows = [row for row in sample_rows(first) if row["rule"] == "typed_prefix"]
    assert len(rows) == 50
    ledger = tmp_path / "ledger.jsonl"
    answers_for(first, tmp_path / "a1.jsonl", lambda row: "yes")
    assert record_answers(capsys, library, ledger, first, tmp_path / "a1.jsonl")[0] == 0
    # The twins of those rows, answered by hand: the other span id of every text.
    with Library(library) as handle:
        census = build_census(
            handle, limit_records=None, min_chars=200, min_occurrences=3, deadline=Deadline(60)
        )
        found: dict[str, list] = {}

        def visit(message, spans) -> None:
            for span in spans:
                if span.rule == "typed_prefix":
                    text = message.text[span.start : span.end]
                    found.setdefault(text, []).append((span.span_id, span.stratum))

        scan(handle, census, limit_records=None, deadline=Deadline(60), visit=visit)
    known = {row["span_id"] for row in rows}
    pairs = [
        (text, span, stratum)
        for text, spans in found.items()
        for span, stratum in spans
        if span not in known
    ]
    twins = [
        ledger_row(
            span,
            "typed_prefix",
            "yes",
            "rob",
            STAMP,
            stratum=stratum,
            sample_digest="a" * 64,
            text_sha256=hashlib.sha256(text.encode()).hexdigest(),
            text_key=text_key(text) if forged_key == "true" else f"{number:064x}",
            drawn=50,
        )
        for number, (text, span, stratum) in enumerate(pairs)
    ]
    assert len(twins) == 50
    append_ledger(ledger, twins)
    report = report_of(capsys, library, "--ledger", ledger, "--owner-id", "rob")
    short = next(item for item in report["rules"] if item["rule"] == "typed_prefix")["strata"]["S"]
    assert (short["reviewed"], short["confirmed"], short["eligible"]) == (50, 50, False)
    assert report["gate"]["duplicate_text_rows"] == 50


SPELLED_SENTENCE = "The quiet harbour opens at dawn and the boats go out slowly."


def spelled_apart(sentence: str, count: int) -> list[str]:
    """`count` exact texts that all read as `sentence`: zero-width spaces at one place each."""
    spellings = []
    for index in range(count):
        place, copies = index % len(sentence), 1 + index // len(sentence)
        spellings.append(sentence[:place] + "\N{ZERO WIDTH SPACE}" * copies + sentence[place:])
    assert len(set(spellings)) == count
    return spellings


def test_a_hundred_spellings_of_one_sentence_are_drawn_once_and_counted_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = twin_text_library(
        tmp_path / "store" / "g", spelled_apart(SPELLED_SENTENCE, 100), copies=1
    )
    ledger = tmp_path / "ledger.jsonl"
    first = draw_and_record(capsys, library, ledger, tmp_path, "one")
    assert len(first) == 1 and first[0]["text_key"] == text_key(SPELLED_SENTENCE)
    assert draw_and_record(capsys, library, ledger, tmp_path, "two") == []
    report = report_of(capsys, library, "--ledger", ledger, "--owner-id", "rob")
    short = next(item for item in report["rules"] if item["rule"] == "typed_prefix")["strata"]["S"]
    assert (short["reviewed"], short["confirmed"], short["eligible"]) == (1, 1, False)
    assert report["profile_eligible"] == []


def test_a_no_on_one_spelling_rejects_every_other_spelling_of_the_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = "The caf\N{LATIN SMALL LETTER E WITH ACUTE} opens at dawn and na\N{LATIN SMALL LETTER I WITH DIAERESIS}ve boats go out."
    spellings = [
        bad,
        bad.replace("opens", "opens\N{ZERO WIDTH SPACE}"),
        bad.replace(" ", "  ", 1),
        bad.replace("dawn and", "dawn\r\nand"),
        unicodedata.normalize("NFD", bad),
        "\N{WORD JOINER}" + bad,
    ]
    good = [
        f"Owner sentence {index} about the garden plan number {index * 3}." for index in range(99)
    ]
    library = twin_text_library(tmp_path / "store" / "g", [*good, *spellings], copies=1)
    sample, answers, ledger = (tmp_path / name for name in ("s.jsonl", "a.jsonl", "l.jsonl"))
    run_cli(capsys, "review-sample", "--owner-id", "rob", "--library", library, "--out", sample)
    rows = [row for row in sample_rows(sample) if row["rule"] == "typed_prefix"]
    assert len(rows) == 100  # 99 sentences and ONE row for the six spellings of the bad one
    answers_for(
        sample,
        answers,
        lambda row: (
            None
            if row["rule"] != "typed_prefix"
            else "no"
            if row["text_key"] == text_key(bad)
            else "yes"
        ),
    )
    assert record_answers(capsys, library, ledger, sample, answers)[0] == 0
    entries, result = apply_entries(
        capsys, library, tmp_path / "out.json", "--ledger", ledger, "--owner-id", "rob"
    )
    assert result["eligible_strata"] == ["typed_prefix:S"]  # 99 of 100 texts confirmed
    texts = eligible_owner_texts(library, entries)
    assert sorted(texts) == sorted(good)  # no spelling of the rejected text is eligible
    assert result["spans_withheld_as_rejected_twin"] + result["spans_withheld_by_verdict"] >= 6


def test_review_record_refuses_a_ledger_that_is_the_sample_or_the_answers_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "g", typed_records=5)
    sample, answers = tmp_path / "sample.jsonl", tmp_path / "answers.jsonl"
    run_cli(capsys, "review-sample", "--owner-id", "rob", "--library", library, "--out", sample)
    answers_for(sample, answers, lambda row: "yes" if row["claim"] == "owner" else None)
    sample_bytes, answer_bytes = sample.read_bytes(), answers.read_bytes()
    alias = tmp_path / "alias.jsonl"
    os.link(sample, alias)  # another name for the very same file
    spelled_differently = tmp_path / "." / "sample.jsonl"
    cases = {
        "the sample": sample,
        "the answers file": answers,
        "another name for the sample": alias,
        "a roundabout spelling of the sample": spelled_differently,
    }
    for name, ledger in cases.items():
        code, _, err = record_answers(capsys, library, ledger, sample, answers)
        assert code == 2 and "same file" in err and "ledger" in err, (name, err)
        assert sample.read_bytes() == sample_bytes and answers.read_bytes() == answer_bytes, name
    # The filled-in sample may be the sample and the answers at once; the ledger must be a file
    # of its own.
    code, _, err = record_answers(capsys, library, sample, sample, sample)
    assert code == 2 and "same file" in err and sample.read_bytes() == sample_bytes
    assert record_answers(capsys, library, tmp_path / "ledger.jsonl", sample, answers)[0] == 0


def test_a_ledger_spelled_in_another_case_is_still_the_sample_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    if not case_insensitive(tmp_path):
        pytest.skip("this volume is case sensitive: the test above covers the identity check")
    library = build_library(tmp_path / "store" / "g", typed_records=5)
    sample, answers = tmp_path / "sample.jsonl", tmp_path / "answers.jsonl"
    run_cli(capsys, "review-sample", "--owner-id", "rob", "--library", library, "--out", sample)
    answers_for(sample, answers, lambda row: "yes" if row["claim"] == "owner" else None)
    before = sample.read_bytes()
    code, _, err = record_answers(capsys, library, tmp_path / "SAMPLE.JSONL", sample, answers)
    assert code == 2 and "same file" in err and sample.read_bytes() == before


def test_review_record_needs_the_sample_it_answers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = tmp_path / "answers.jsonl"
    answers.write_text("", encoding="utf-8")
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            [
                "library",
                "attribute",
                "review-record",
                "--ledger",
                str(tmp_path / "l.jsonl"),
                "--answers",
                str(answers),
                "--reviewer",
                "rob",
            ]
        )
    capsys.readouterr()


def test_review_record_refuses_answers_that_are_not_in_the_sample(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "g", typed_records=3)
    sample, answers, ledger = (tmp_path / name for name in ("s.jsonl", "a.jsonl", "l.jsonl"))
    run_cli(capsys, "review-sample", "--owner-id", "rob", "--library", library, "--out", sample)
    real = next(row for row in sample_rows(sample) if row["rule"] == "typed_prefix")
    real["verdict"] = "yes"
    forgeries = {
        "fabricated id": {**real, "span_id": "f" * 32},
        "edited rule": {**real, "rule": "note_owner"},
        "edited stratum": {**real, "stratum": "M"},
        "missing stratum": {key: value for key, value in real.items() if key != "stratum"},
        "other rules version": {**real, "rules_version": "cce.attribution-rules@0"},
    }
    for name, forged in forgeries.items():
        answers.write_text(json.dumps(real) + "\n" + json.dumps(forged) + "\n", encoding="utf-8")
        code, _, err = record_answers(capsys, library, ledger, sample, answers)
        assert code == 2 and "answers line 2" in err and "sample" in err, name
        assert not ledger.exists(), name
    answers.write_text(json.dumps(real) + "\n", encoding="utf-8")
    assert record_answers(capsys, library, ledger, sample, answers)[0] == 0


def test_a_sample_drawn_under_the_previous_rules_version_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "g", typed_records=3)
    sample, old_sample, answers, ledger = (
        tmp_path / name for name in ("s.jsonl", "old.jsonl", "a.jsonl", "l.jsonl")
    )
    run_cli(capsys, "review-sample", "--owner-id", "rob", "--library", library, "--out", sample)
    rows = [row for row in sample_rows(sample) if row["rule"] == "typed_prefix"]
    stale = [{**row, "rules_version": "cce.attribution-rules@1"} for row in rows]
    old_sample.write_text("".join(json.dumps(row) + "\n" for row in stale), encoding="utf-8")
    answers.write_text(
        "".join(json.dumps({**row, "verdict": "yes"}) + "\n" for row in stale), encoding="utf-8"
    )
    code, _, err = record_answers(capsys, library, ledger, old_sample, answers)
    assert code == 2 and "rules version" in err and "draw a new sample" in err
    assert not ledger.exists()


@pytest.mark.parametrize(
    "previous_version",
    [
        "cce.attribution-rules@7",
        "cce.attribution-rules@8",
        "cce.attribution-rules@9",
        "cce.attribution-rules@10",
    ],
)
def test_ledger_rows_from_the_previous_rules_version_are_reported_as_stale(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], previous_version: str
) -> None:
    library = build_library(tmp_path / "store" / "g", typed_records=110)
    ledger = approve(capsys, library, tmp_path)
    assert report_of(capsys, library, "--ledger", ledger, "--owner-id", "rob")[
        "profile_eligible"
    ] == ["typed_prefix:S"]
    rewritten = tmp_path / "old-ledger.jsonl"
    rewritten.write_text(
        ledger.read_text(encoding="utf-8").replace("cce.attribution-rules@11", previous_version),
        encoding="utf-8",
    )
    report = report_of(capsys, library, "--ledger", rewritten, "--owner-id", "rob")
    assert report["gate"]["stale_rows"] == report["gate"]["ledger_rows"] > 100
    assert report["profile_eligible"] == [] and report["gate"]["orphan_rows"] == 0
    _, result = apply_entries(
        capsys, library, tmp_path / "stale.json", "--ledger", rewritten, "--owner-id", "rob"
    )
    assert result["stale_rows"] == result["ledger_rows"] > 100
    assert result["eligible_strata"] == []
    assert result["rules_version"] == "cce.attribution-rules@11"


def test_review_record_rejects_bad_answers_before_writing_anything(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "g", typed_records=3)
    sample, answers, ledger = (tmp_path / name for name in ("s.jsonl", "a.jsonl", "l.jsonl"))
    run_cli(capsys, "review-sample", "--owner-id", "rob", "--library", library, "--out", sample)
    good = next(row for row in sample_rows(sample) if row["rule"] == "typed_prefix")
    good["verdict"] = "yes"
    answers.write_text(
        json.dumps(good) + "\n" + json.dumps({**good, "verdict": "maybe"}) + "\n", encoding="utf-8"
    )
    code, _, err = record_answers(capsys, library, ledger, sample, answers)
    assert code == 2 and "answers line 2" in err
    assert not ledger.exists()
    answers.write_text("{bad\n", encoding="utf-8")
    code, _, err = record_answers(capsys, library, ledger, sample, answers)
    assert code == 2 and "answers line 1" in err and not ledger.exists()


def test_review_record_stamps_the_current_utc_time_only_when_none_is_given(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "g", typed_records=3)
    sample, answers, ledger = (tmp_path / name for name in ("s.jsonl", "a.jsonl", "l.jsonl"))
    run_cli(capsys, "review-sample", "--owner-id", "rob", "--library", library, "--out", sample)
    row = next(item for item in sample_rows(sample) if item["rule"] == "typed_prefix")
    answers.write_text(json.dumps({**row, "verdict": "YES"}) + "\n", encoding="utf-8")
    code, _, _ = run_cli(
        capsys,
        "review-record",
        "--library",
        library,
        "--ledger",
        ledger,
        "--sample",
        sample,
        "--answers",
        answers,
        "--reviewer",
        "rob",
    )
    assert code == 0
    recorded = {
        entry["span_id"]: entry
        for entry in map(json.loads, ledger.read_text(encoding="utf-8").splitlines())
    }
    assert recorded[row["span_id"]]["verdict"] == "yes"
    assert {entry["reviewer"] for entry in recorded.values()} == {"rob"}
    assert len(recorded) == len(sample_rows(sample))
    stamps = {entry["reviewed_at"] for entry in recorded.values()}
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", stamp) for stamp in stamps)


# --- messages that cannot be encoded -----------------------------------------------------------


def poison_one_message(database: Path, uid_suffix: str, message: int, text: str) -> None:
    """Rewrite one stored message with `text`, written the way an ensure_ascii writer would, so
    it reads back with a lone surrogate (the library's own writer cannot store one)."""
    connection = sqlite3.connect(database)
    try:
        uid = next(
            row[0]
            for row in connection.execute("SELECT uid FROM records")
            if row[0].endswith(uid_suffix)
        )
        digest = connection.execute(
            "SELECT current_digest FROM records WHERE uid=?", (uid,)
        ).fetchone()[0]
        payload = json.loads(
            zlib.decompress(
                connection.execute(
                    "SELECT payload FROM objects WHERE digest=?", (digest,)
                ).fetchone()[0]
            )
        )
        payload["messages"][message]["text"] = text
        packed = zlib.compress(json.dumps(payload, ensure_ascii=True).encode("ascii"))
        connection.execute("UPDATE objects SET payload=? WHERE digest=?", (packed, digest))
        connection.commit()
    finally:
        connection.close()


def test_a_message_with_a_lone_surrogate_is_counted_and_never_aborts_a_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "gen")
    poison_one_message(library, "owner-chat", 0, "before \ud800 after")
    with Library(library) as handle:
        assert (
            "\ud800"
            in handle.record(
                next(r["uid"] for r in handle.rows() if r["uid"].endswith("owner-chat"))
            )["messages"][0]["text"]
        )
    report = report_of(capsys, library)
    assert report["scan"]["unreadable_messages"] == 1
    assert report["partition"]["ok"] is True
    code, text_report, _ = run_cli(capsys, "--report", "--library", library)
    assert code == 0 and "could not be encoded" in text_report
    unattributed = next(r for r in report["rules"] if r["rule"] == "unattributed")
    assert unattributed["characters"] >= len("before \ud800 after")
    out = tmp_path / "a.json"
    entries, result = apply_entries(capsys, library, out, "--owner-id", "rob")
    assert result["unreadable_messages"] == 1 and entries
    assert not any(
        entry["source"]["uid"].endswith("owner-chat")
        and entry["source"]["sequence"] == "main"
        and entry["source"]["message"] == 0
        for entry in entries
    )
    sample = tmp_path / "s.jsonl"
    assert (
        run_cli(
            capsys, "review-sample", "--owner-id", "rob", "--library", library, "--out", sample
        )[0]
        == 0
    )
    assert "ud800" not in sample.read_text(encoding="utf-8")


def test_the_census_skips_unencodable_messages_instead_of_failing(tmp_path: Path) -> None:
    library = build_library(tmp_path / "store" / "gen")
    poison_one_message(library, "owner-chat", 0, "before \ud800 after " + "x" * 300)
    with Library(library) as handle:
        census = build_census(
            handle,
            limit_records=None,
            min_chars=200,
            min_occurrences=3,
            deadline=Deadline(60.0),
        )
    assert census.later_occurrences("fine words") == ()


# --- where the writers may write --------------------------------------------------------------


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def real_store(tmp_path: Path) -> tuple[Path, Path]:
    """A library at the layout the builder publishes: store/library-all/generations/<name>."""
    store = tmp_path / "store"
    generation = store / "library-all" / "generations" / "20260101T000000Z"
    generation.mkdir(parents=True)
    with Library.create(generation / "library.sqlite") as library:
        for conversation in conversations(typed_records=3):
            library.put(conversation)
    return store, generation / "library.sqlite"


def sample_and_answers(
    capsys: pytest.CaptureFixture[str], library: Path, tmp_path: Path
) -> tuple[Path, Path]:
    sample, answers = tmp_path / "s.jsonl", tmp_path / "a.jsonl"
    assert (
        run_cli(
            capsys, "review-sample", "--owner-id", "rob", "--library", library, "--out", sample
        )[0]
        == 0
    )
    row = next(item for item in sample_rows(sample) if item["rule"] == "typed_prefix")
    answers.write_text(json.dumps({**row, "verdict": "yes"}) + "\n", encoding="utf-8")
    return sample, answers


def test_no_writer_may_write_inside_the_generation_or_anywhere_in_the_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store, library = real_store(tmp_path)
    sample, answers = sample_and_answers(capsys, library, tmp_path)
    targets = {
        "generation": library.parent / "x.out",
        "generations folder": library.parent.parent / "x.out",
        "store root": store / "x.out",
        "elsewhere in the store": store / "notes" / "x.out",
    }
    for name, target in targets.items():
        for argv in (
            ("apply", "--library", library, "--out", target),
            ("review-sample", "--owner-id", "rob", "--library", library, "--out", target),
            (
                "review-record",
                "--library",
                library,
                "--ledger",
                target,
                "--sample",
                sample,
                "--answers",
                answers,
                "--reviewer",
                "rob",
            ),
        ):
            code, _, err = run_cli(capsys, *argv)
            assert code == 2 and "library store" in err, (name, argv[0])
            assert not target.exists(), (name, argv[0])
    assert not (store / "notes").exists()


class FakeStat:
    """Stands in for `os.stat`: names (any spelling) map to a device and inode."""

    def __init__(self, table: dict[str, tuple[int, int]]) -> None:
        self.table = table

    def __call__(self, path: str | Path) -> SimpleNamespace:
        key = str(path)
        if key not in self.table:
            raise FileNotFoundError(key)
        device, inode = self.table[key]
        return SimpleNamespace(st_dev=device, st_ino=inode)


def test_the_identity_comparison_finds_a_path_that_differs_only_in_spelling() -> None:
    stat = FakeStat(
        {
            "/": (1, 2),
            "/a": (1, 3),
            "/a/store": (1, 8),
            "/a/STORE": (1, 8),
            "/a/store/lib": (1, 9),
            "/a/STORE/LIB": (1, 9),
            "/a/store/lib/gen": (1, 10),
            "/a/STORE/LIB/GEN": (1, 10),
            "/a/elsewhere": (1, 77),
        }
    )
    roots = [Path("/a/store/lib/gen"), Path("/a/store/lib")]
    inside = attribution_commands.inside_by_identity
    # the generation under another spelling: the very same directory
    assert inside(Path("/a/STORE/LIB/GEN/case.json"), roots, stat=stat) == roots[0]
    # a path whose tail does not exist yet: the nearest existing ancestor decides
    assert inside(Path("/a/STORE/LIB/new/dir/x.json"), roots, stat=stat) == roots[1]
    assert inside(Path("/a/STORE/LIB"), roots, stat=stat) == roots[1]
    # a sibling and a parent of the roots are not inside them
    assert inside(Path("/a/elsewhere/x.json"), roots, stat=stat) is None
    assert inside(Path("/a/STORE/x.json"), roots, stat=stat) is None
    # roots that do not exist protect nothing
    assert inside(Path("/a/elsewhere/x.json"), [Path("/nope")], stat=stat) is None
    # the same inode number on another device is another file
    other_device = FakeStat({**stat.table, "/a/STORE/LIB/GEN": (2, 10)})
    assert inside(Path("/a/STORE/LIB/GEN/case.json"), roots[:1], stat=other_device) is None


def case_insensitive(directory: Path) -> bool:
    (directory / "probe.case").touch()
    return (directory / "PROBE.CASE").exists()


def test_a_path_spelled_in_another_case_is_still_inside_the_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    if not case_insensitive(tmp_path):
        pytest.skip("this volume is case sensitive: the unit test above covers the comparison")
    library = build_library(tmp_path / "store" / "gen", typed_records=3)
    out = tmp_path / "STORE" / "GEN" / "case.json"
    code, _, err = run_cli(capsys, "apply", "--library", library, "--out", out)
    assert code == 2 and "outside the library" in err
    generation = tmp_path / "store" / "gen"
    assert not (generation / "case.json").exists()
    assert not (generation / "case.json.partial").exists()
    sample = tmp_path / "STORE" / "s.jsonl"
    code, _, err = run_cli(
        capsys, "review-sample", "--owner-id", "rob", "--library", library, "--out", sample
    )
    assert (
        code == 2 and "outside the library" in err and not (tmp_path / "store" / "s.jsonl").exists()
    )
    ledger = tmp_path / "STORE" / "led.jsonl"
    other = tmp_path / "other-sample.jsonl"
    run_cli(capsys, "review-sample", "--owner-id", "rob", "--library", library, "--out", other)
    answers = tmp_path / "answers.jsonl"
    answers.write_text("", encoding="utf-8")
    code, _, err = record_answers(capsys, library, ledger, other, answers)
    assert (
        code == 2
        and "outside the library" in err
        and not (tmp_path / "store" / "led.jsonl").exists()
    )
    shouting = tmp_path / "STORE" / "GEN" / "library.sqlite"
    code, _, err = run_cli(
        capsys, "apply", "--library", shouting, "--out", tmp_path / "store" / "x.json"
    )
    assert code == 2 and "outside the library" in err
    assert not (tmp_path / "store" / "x.json").exists()


def test_review_record_must_know_the_library_so_the_ledger_stays_out_of_its_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "gen")
    sample, answers = sample_and_answers(capsys, library, tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    code, _, err = run_cli(
        capsys,
        "review-record",
        "--ledger",
        ledger,
        "--sample",
        sample,
        "--answers",
        answers,
        "--reviewer",
        "rob",
    )
    assert code == 2 and "store-root" in err and not ledger.exists()


def test_a_bare_generation_protects_its_folder_and_its_parent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "gen")
    for target in (library.parent / "x.json", library.parent.parent / "x.json"):
        code, _, err = run_cli(capsys, "apply", "--library", library, "--out", target)
        assert code == 2 and "library store" in err and not target.exists()
    ok = tmp_path / "outside.json"
    assert run_cli(capsys, "apply", "--library", library, "--out", ok)[0] == 0


def test_an_explicit_store_root_is_protected_too(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "somewhere" / "lib")
    configured = tmp_path / "configured-store"
    configured.mkdir()
    target = configured / "x.json"
    code, _, err = run_cli(
        capsys, "apply", "--library", library, "--store-root", configured, "--out", target
    )
    assert code == 2 and "library store" in err and not target.exists()


def test_a_git_visible_destination_is_refused_before_anything_is_written(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "gen")
    sample, answers = sample_and_answers(capsys, library, tmp_path)
    repo = tmp_path / "repo"
    (repo / "visible").mkdir(parents=True)
    (repo / "private").mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text("private/\n", encoding="utf-8")
    for argv in (
        ("apply", "--library", library, "--out", repo / "visible" / "a.json"),
        (
            "review-sample",
            "--owner-id",
            "rob",
            "--library",
            library,
            "--out",
            repo / "visible" / "s.jsonl",
        ),
        (
            "review-record",
            "--library",
            library,
            "--ledger",
            repo / "visible" / "l.jsonl",
            "--sample",
            sample,
            "--answers",
            answers,
            "--reviewer",
            "rob",
        ),
    ):
        code, _, err = run_cli(capsys, *argv)
        assert code == 2 and "Git-visible" in err, argv[0]
    assert sorted(p.name for p in (repo / "visible").iterdir()) == []
    ok = repo / "private" / "a.json"
    assert run_cli(capsys, "apply", "--library", library, "--out", ok)[0] == 0
    assert ok.exists()


def test_every_output_is_private_and_new_directories_are_0700(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "gen")
    sample = tmp_path / "fresh" / "nested" / "s.jsonl"
    old = os.umask(0)
    try:
        assert (
            run_cli(
                capsys, "review-sample", "--owner-id", "rob", "--library", library, "--out", sample
            )[0]
            == 0
        )
        assert mode(sample) == 0o600
        assert mode(sample.parent) == 0o700 and mode(sample.parent.parent) == 0o700
        out = tmp_path / "other" / "a.json"
        assert run_cli(capsys, "apply", "--library", library, "--out", out)[0] == 0
        assert mode(out) == 0o600 and mode(out.parent) == 0o700
        row = next(item for item in sample_rows(sample) if item["rule"] == "typed_prefix")
        answers = tmp_path / "a.jsonl"
        answers.write_text(json.dumps({**row, "verdict": "yes"}) + "\n", encoding="utf-8")
        ledger = tmp_path / "ledgers" / "l.jsonl"
        assert record_answers(capsys, library, ledger, sample, answers)[0] == 0
        assert mode(ledger) == 0o600 and mode(ledger.parent) == 0o700
        partial = tmp_path / "p" / "cut.json"
        assert (
            run_cli(
                capsys, "apply", "--library", library, "--out", partial, "--limit-records", "2"
            )[0]
            == 0
        )
        assert mode(partial.with_name("cut.json.partial")) == 0o600
    finally:
        os.umask(old)


def test_a_slice_or_deadline_cut_output_is_written_under_the_partial_name(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = build_library(tmp_path / "store" / "gen")
    out = tmp_path / "a.json"
    code, out_text, _ = run_cli(
        capsys, "apply", "--library", library, "--out", out, "--limit-records", "2", "--json"
    )
    result = json.loads(out_text)
    assert code == 0 and result["partial"] is True and result["limited"] is True
    assert not out.exists() and Path(result["out"]) == out.with_name("a.json.partial")
    assert isinstance(json.loads(Path(result["out"]).read_text(encoding="utf-8")), list)

    sample = tmp_path / "s.jsonl"
    code, out_text, _ = run_cli(
        capsys,
        "review-sample",
        "--owner-id",
        "rob",
        "--library",
        library,
        "--out",
        sample,
        "--limit-records",
        "2",
        "--json",
    )
    assert code == 0 and json.loads(out_text)["partial"] is True
    assert not sample.exists()
    rows = sample_rows(sample.with_name("s.jsonl.partial"))
    assert rows and all(row["partial"] is True for row in rows)

    readings = {"count": 0}

    def counting() -> float:
        readings["count"] += 1
        return 0.0

    with Library(library) as handle:
        build_census(
            handle,
            limit_records=None,
            min_chars=200,
            min_occurrences=3,
            deadline=Deadline(10.0, clock=counting),
        )
    allowed = readings["count"] + 3
    seen = {"count": 0}

    def clock() -> float:
        seen["count"] += 1
        return 0.0 if seen["count"] <= allowed else 1e6

    monkeypatch.setattr(
        attribution_commands, "Deadline", lambda seconds: Deadline(seconds, clock=clock)
    )
    cut = tmp_path / "cut.jsonl"
    code, out_text, err = run_cli(
        capsys, "review-sample", "--owner-id", "rob", "--library", library, "--out", cut, "--json"
    )
    assert code == 3 and "partial" in err
    assert json.loads(out_text)["partial"] is True and not cut.exists()
    assert all(row["partial"] is True for row in sample_rows(cut.with_name("cut.jsonl.partial")))


def test_a_complete_scan_leaves_no_partial_marker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "gen")
    sample = tmp_path / "s.jsonl"
    code, out_text, _ = run_cli(
        capsys,
        "review-sample",
        "--owner-id",
        "rob",
        "--library",
        library,
        "--out",
        sample,
        "--json",
    )
    assert code == 0 and json.loads(out_text)["partial"] is False
    assert sample.exists() and not sample.with_name("s.jsonl.partial").exists()
    assert all(row["partial"] is False for row in sample_rows(sample))


def test_a_leftover_partial_file_is_refused_before_any_work_starts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = build_library(tmp_path / "store" / "gen")

    def forbidden(*args, **kwargs):
        raise AssertionError("the census must not start")

    monkeypatch.setattr(attribution_commands, "build_census", forbidden)
    for command, name in (("apply", "a.json"), ("review-sample", "s.jsonl")):
        out = tmp_path / name
        out.with_name(name + ".partial").write_text("old", encoding="utf-8")
        code, _, err = run_cli(
            capsys, command, "--owner-id", "rob", "--library", library, "--out", out
        )
        assert code == 2 and "leftover" in err and ".partial" in err
        assert len(err.strip().splitlines()) == 1
        assert out.with_name(name + ".partial").read_text(encoding="utf-8") == "old"


@pytest.mark.parametrize("command", ["apply", "review-sample"])
def test_a_file_created_at_the_output_during_the_run_is_never_replaced(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    library = build_library(tmp_path / "store" / "gen", typed_records=3)
    out = tmp_path / "result.out"
    real_scan = attribution_commands.scan

    def scan_then_somebody_creates_the_output(*args, **kwargs):
        done = real_scan(*args, **kwargs)
        out.write_text("A FILE THE OWNER MADE DURING THE RUN\n", encoding="utf-8")
        return done

    monkeypatch.setattr(attribution_commands, "scan", scan_then_somebody_creates_the_output)
    code, _, err = run_cli(capsys, command, "--owner-id", "rob", "--library", library, "--out", out)
    assert code == 2 and "already exists" in err
    assert out.read_text(encoding="utf-8") == "A FILE THE OWNER MADE DURING THE RUN\n"
    assert not out.with_name("result.out.partial").exists()


def test_deeply_nested_json_is_refused_as_unreadable_not_a_crash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "g", typed_records=3)
    sample, answers, ledger = (tmp_path / name for name in ("s.jsonl", "a.jsonl", "l.jsonl"))
    run_cli(capsys, "review-sample", "--owner-id", "rob", "--library", library, "--out", sample)
    answered(sample, answers, 1)
    deep = tmp_path / "deep.jsonl"
    deep.write_text("[" * DEEP_NESTING + "]" * DEEP_NESTING + "\n", encoding="utf-8")
    code, _, err = record_answers(capsys, library, ledger, sample, deep)
    assert code == 2 and "answers line 1" in err and "unreadable" in err
    code, _, err = record_answers(capsys, library, ledger, deep, answers)
    assert code == 2 and "sample line 1" in err and "unreadable" in err
    assert not ledger.exists()
    hostile = tmp_path / "hostile-ledger.jsonl"
    hostile.write_text(deep.read_text(encoding="utf-8"), encoding="utf-8")
    report = report_of(capsys, library, "--ledger", hostile, "--owner-id", "rob")
    assert report["gate"]["quarantined_rows"] == 1


def test_review_record_repairs_a_torn_ledger_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "g", typed_records=3)
    sample, answers, ledger = (tmp_path / name for name in ("s.jsonl", "a.jsonl", "l.jsonl"))
    run_cli(capsys, "review-sample", "--owner-id", "rob", "--library", library, "--out", sample)
    answered(sample, answers, 1)
    ledger.write_text('{"schema": "cce.attribution-review-ledger.v6", "rul', encoding="utf-8")
    code, out, err = record_answers(capsys, library, ledger, sample, answers, "--json")
    assert code == 0, err
    result = json.loads(out)
    assert result["repaired_torn_line"] is True
    report = report_of(capsys, library, "--ledger", ledger, "--owner-id", "rob")
    assert report["gate"]["quarantined_rows"] == 1
    assert report["gate"]["ledger_rows"] == result["appended"]
    again, more = tmp_path / "s2.jsonl", tmp_path / "a2.jsonl"
    run_cli(
        capsys, "review-sample", "--owner-id", "rob", "--library", library, "--ledger", ledger,
        "--out", again,
    )  # fmt: skip
    answered(again, more, 1)
    code, out, _ = record_answers(capsys, library, ledger, again, more, "--json")
    assert code == 0 and json.loads(out)["repaired_torn_line"] is False


def test_review_record_waits_for_a_locked_ledger_and_then_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    library = build_library(tmp_path / "store" / "g", typed_records=3)
    sample, answers, ledger = (tmp_path / name for name in ("s.jsonl", "a.jsonl", "l.jsonl"))
    run_cli(capsys, "review-sample", "--owner-id", "rob", "--library", library, "--out", sample)
    answered(sample, answers, 1)
    monkeypatch.setattr(attribution_rules, "LEDGER_LOCK_SECONDS", 0.2)
    ledger.write_bytes(b"")
    holder = os.open(ledger, os.O_RDWR)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        code, _, err = record_answers(capsys, library, ledger, sample, answers)
    finally:
        os.close(holder)
    assert code == 2 and "locked by another process" in err
    assert ledger.read_bytes() == b""
    assert record_answers(capsys, library, ledger, sample, answers)[0] == 0


AGENT_NAMES = (
    "claude",
    "chatgpt",
    "gpt",
    "gemini",
    "copilot",
    "codex",
    "cursor",
    "opencode",
    "assistant",
    "agent",
    "system",
    "bot",
)


@pytest.mark.parametrize(
    "identity",
    [
        "",
        "   ",
        "\u200b",
        "\u200b\ufeff\u2060",
        "\u00ad\u200d",
        "\u0301\u0301",
        "\u20dd",
        " \u200b ",
        *AGENT_NAMES,
        "Claude",
        "ChatGPT",
        "GPT",
        "GEMINI",
        " codex ",
        "Assistant",
        "SYSTEM",
        "Bot",
    ],
)
def test_an_owner_identity_must_be_visible_and_must_not_be_an_agent_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], identity: str
) -> None:
    library = build_library(tmp_path / "store" / "g", typed_records=3)
    for argv in (
        ("--report", "--library", library, "--owner-id", identity),
        ("apply", "--library", library, "--out", tmp_path / "o.json", "--owner-id", identity),
        ("review-sample", "--library", library, "--owner-id", identity),
    ):
        code, _, err = run_cli(capsys, *argv)
        assert code == 2 and "--owner-id" in err, (argv[0], identity)
    assert not (tmp_path / "o.json").exists()


@pytest.mark.parametrize("identity", ["\u200b", "\u0301", " ", *AGENT_NAMES, "CLAUDE", " Gpt "])
def test_a_reviewer_identity_must_be_visible_and_must_not_be_an_agent_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], identity: str
) -> None:
    library = build_library(tmp_path / "store" / "g", typed_records=3)
    sample, answers, ledger = (tmp_path / name for name in ("s.jsonl", "a.jsonl", "l.jsonl"))
    run_cli(capsys, "review-sample", "--owner-id", "rob", "--library", library, "--out", sample)
    answered(sample, answers, 1)
    code, _, err = record_answers(capsys, library, ledger, sample, answers, reviewer=identity)
    assert code == 2 and "--reviewer" in err and not ledger.exists()


def test_people_and_prefixed_agent_identities_are_still_accepted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "g", typed_records=3)
    for identity in ("rob", "Claude Monet", "agent:codex", "owner-1", "Émile"):
        code, _, err = run_cli(capsys, "--report", "--library", library, "--owner-id", identity)
        assert code == 0, (identity, err)


@pytest.mark.parametrize(
    "value", ["inf", "-inf", "Infinity", "nan", "-1", "0", "-0.5", "1e999", "soon", ""]
)
def test_a_deadline_must_be_a_positive_finite_number(
    capsys: pytest.CaptureFixture[str], value: str
) -> None:
    for argv in (
        ["library", "attribute", "--report", f"--deadline-seconds={value}"],
        ["library", "attribute", "apply", "--out", "o.json", f"--deadline-seconds={value}"],
    ):
        with pytest.raises(SystemExit) as stopped:
            build_parser().parse_args(argv)
        assert stopped.value.code == 2
        assert "positive" in capsys.readouterr().err


def test_a_finite_deadline_is_accepted() -> None:
    args = build_parser().parse_args(
        ["library", "attribute", "--report", "--deadline-seconds", "0.5"]
    )
    assert args.deadline_seconds == 0.5


def test_an_existing_output_is_refused_for_every_writer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "gen")
    out = tmp_path / "taken.out"
    out.write_text("keep", encoding="utf-8")
    for command in ("apply", "review-sample"):
        code, _, err = run_cli(
            capsys, command, "--owner-id", "rob", "--library", library, "--out", out
        )
        assert code == 2 and "already exists" in err
    link = tmp_path / "link.out"
    link.symlink_to(tmp_path / "target.out")
    assert run_cli(capsys, "apply", "--library", library, "--out", link)[0] == 2
    assert not (tmp_path / "target.out").exists()
    assert out.read_text(encoding="utf-8") == "keep"


# --- registration ----------------------------------------------------------------------------


def test_corpus_store_root_is_an_alias_of_store_root_on_every_attribute_command() -> None:
    parser = build_parser()
    for words in (
        ["--report"],
        ["apply", "--out", "o.json"],
        ["review-sample"],
        ["review-record", "--ledger", "l", "--sample", "s", "--answers", "a", "--reviewer", "r"],
    ):
        args = parser.parse_args(["library", "attribute", *words, "--corpus-store-root", "/x"])
        assert args.store_root == Path("/x"), words
        args = parser.parse_args(["library", "attribute", *words, "--store-root", "/y"])
        assert args.store_root == Path("/y"), words


def test_a_first_sample_binds_to_a_ledger_state_not_to_one_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A sample drawn against no rows of the owner can be recorded into any ledger that holds
    none (the docs say so); it cannot be recorded twice into the same ledger."""
    library = build_library(tmp_path / "store" / "g", typed_records=5)
    sample, answers = tmp_path / "sample.jsonl", tmp_path / "answers.jsonl"
    run_cli(capsys, "review-sample", "--owner-id", "rob", "--library", library, "--out", sample)
    answers_for(sample, answers, lambda row: "yes" if row["claim"] == "owner" else None)
    first, second = tmp_path / "ledger-a.jsonl", tmp_path / "ledger-b.jsonl"
    assert record_answers(capsys, library, first, sample, answers)[0] == 0
    assert record_answers(capsys, library, second, sample, answers)[0] == 0
    code, _, err = record_answers(capsys, library, first, sample, answers)
    assert code == 2 and "different ledger state" in err
    # Rows of other reviewers do not change the state, so such a ledger takes the sample too.
    other = tmp_path / "ledger-c.jsonl"
    foreign = [
        {**json.loads(line), "reviewer": "someone-else"}
        for line in first.read_text(encoding="utf-8").splitlines()
    ]
    other.write_text("".join(json.dumps(row) + "\n" for row in foreign), encoding="utf-8")
    assert record_answers(capsys, library, other, sample, answers)[0] == 0


def test_parser_registers_attribute_and_keeps_options_given_before_a_subcommand() -> None:
    parser = build_parser()
    args = parser.parse_args(["library", "attribute", "--report", "--json", "--library", "x"])
    assert (args.group, args.action) == ("library", "attribute")
    assert args.report and args.json and args.attribute_action is None
    args = parser.parse_args(
        ["library", "attribute", "--limit-records", "2", "apply", "--out", "o.json"]
    )
    assert args.attribute_action == "apply" and args.limit_records == 2
    args = parser.parse_args(
        ["library", "attribute", "apply", "--limit-records", "3", "--out", "o.json"]
    )
    assert args.limit_records == 3
    assert parser.parse_args(["library", "status"]).action == "status"


def test_main_routes_library_attribute(
    library_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["cce", "library", "attribute", "--report", "--json", "--library", str(library_path)],
    )
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 0
    assert json.loads(capsys.readouterr().out)["partition"]["ok"] is True
