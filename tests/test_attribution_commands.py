"""`cce library attribute`: report, annotation export, owner review and the gate (synthetic data)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import zlib
from collections import Counter
from pathlib import Path

import pytest
from attribution_support import ECHO_ANSWER, ECHO_QUESTION, build_library, conversations

from conversation_corpus_engine import attribution_commands
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
    append_ledger,
    ledger_row,
)
from conversation_corpus_engine.cli import build_parser, main
from conversation_corpus_engine.library_store import Library

STAMP = "2026-01-02T03:04:05Z"
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
    assert run_cli(capsys, "review-sample", "--library", library_path, "--out", sample)[0] == 0
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
    assert result["non_owner_rows"] == 100


def test_fabricated_ledger_rows_count_for_nothing_in_report_and_apply(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library_path = build_library(tmp_path / "store" / "g", typed_records=110)
    forged = tmp_path / "forged.jsonl"
    append_ledger(
        forged,
        [
            ledger_row(f"{index:032x}", "typed_prefix", "yes", "rob", STAMP, stratum="S")
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
    assert gate_info["non_owner_rows"] == 100 and gate_info["orphan_rows"] == 0
    assert gate_info["non_owner_reviewers"] == {"agent:codex": 100}
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
    row = next(item for item in report["rules"] if item["rule"] == "typed_prefix")
    assert report["gate"]["orphan_rows"] >= 90
    assert row["strata"]["S"]["reviewed"] + report["gate"]["orphan_rows"] == 100


def test_a_conflicting_answer_is_refused_and_cannot_brick_the_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "g", typed_records=3)
    sample, answers, ledger = (tmp_path / name for name in ("s.jsonl", "a.jsonl", "l.jsonl"))
    run_cli(capsys, "review-sample", "--library", library, "--out", sample)
    real = next(row for row in sample_rows(sample) if row["rule"] == "typed_prefix")
    answers.write_text(json.dumps({**real, "verdict": "yes"}) + "\n", encoding="utf-8")
    assert record_answers(capsys, library, ledger, sample, answers)[0] == 0
    before = ledger.read_bytes()

    # A second sample file that names the same span as another rule (a forged or edited one).
    forged_sample = tmp_path / "forged-sample.jsonl"
    forged_sample.write_text(json.dumps({**real, "rule": "note_owner"}) + "\n", encoding="utf-8")
    answers.write_text(
        json.dumps({**real, "rule": "note_owner", "verdict": "yes"}) + "\n", encoding="utf-8"
    )
    code, _, err = record_answers(capsys, library, ledger, forged_sample, answers)
    assert code == 2 and "conflict" in err and "nothing was written" in err
    assert ledger.read_bytes() == before

    answers.write_text(json.dumps({**real, "verdict": "no"}) + "\n", encoding="utf-8")
    assert record_answers(capsys, library, ledger, sample, answers)[0] == 0
    report = report_of(capsys, library, "--ledger", ledger, "--owner-id", "rob")
    assert report["gate"]["superseded_rows"] == 1 and report["gate"]["quarantined_rows"] == 0


def test_a_hand_damaged_ledger_is_reported_not_fatal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "g", typed_records=110)
    ledger = approve(capsys, library, tmp_path)
    first = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
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
    assert next(r for r in report["rules"] if r["rule"] == "pasted_back")["spans"] == 1
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


def sample_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_review_sample_is_deterministic_bounded_and_shows_only_the_span_and_its_rule(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "g", typed_records=130)
    first, second = tmp_path / "s1.jsonl", tmp_path / "s2.jsonl"
    assert run_cli(capsys, "review-sample", "--library", library, "--out", first)[0] == 0
    assert run_cli(capsys, "review-sample", "--library", library, "--out", second)[0] == 0
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
    run_cli(capsys, "review-sample", "--library", library, "--out", few, "--per-rule", "4")
    assert max(Counter((r["rule"], r["stratum"]) for r in sample_rows(few)).values()) == 4
    also = tmp_path / "also.jsonl"
    run_cli(capsys, "review-sample", "--library", library, "--out", also, "--per-stratum", "4")
    assert also.read_bytes() == few.read_bytes()


def test_review_sample_streams_to_standard_output_and_reports_the_short_span_bias(
    library_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run_cli(capsys, "review-sample", "--library", library_path)
    assert code == 0
    assert all(json.loads(line)["schema"] for line in out.splitlines())
    summary = json.loads(err.strip().splitlines()[-1])
    assert summary["sampled"] == len(out.splitlines())
    strata = {key.split(":")[1] for key in summary["per_stratum"]}
    assert strata == {"S", "M"}
    medium = [row for row in map(json.loads, out.splitlines()) if row["stratum"] == "M"]
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
    assert run_cli(capsys, "review-sample", "--library", library, "--out", sample)[0] == 0
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
    run_cli(capsys, "review-sample", "--library", library, "--out", sample)
    answered(sample, answers, yes)
    code, out, err = record_answers(capsys, library, ledger, sample, answers, "--json")
    assert code == 0, err
    recorded = json.loads(out)
    assert recorded["appended"] == 100 and recorded["unanswered"] > 0
    assert recorded["strata"]["typed_prefix:S"]["appended"] == 100
    report = report_of(capsys, library, "--ledger", ledger, "--owner-id", "rob")
    row = next(item for item in report["rules"] if item["rule"] == "typed_prefix")
    short = row["strata"]["S"]
    assert (short["reviewed"], short["confirmed"], short["eligible"]) == (100, yes, eligible)
    assert not row["strata"]["M"]["eligible"] and not row["strata"]["L"]["eligible"]
    assert report["profile_eligible"] == (["typed_prefix:S"] if eligible else [])
    assert report["gate"]["ledger_rows"] == 100
    assert (report["gate"]["orphan_rows"], report["gate"]["non_owner_rows"]) == (0, 0)
    _, result = apply_entries(
        capsys, library, tmp_path / "out.json", "--ledger", ledger, "--owner-id", "rob"
    )
    assert (result["profile_eligible_spans"] > 0) is eligible
    again = tmp_path / "again.jsonl"
    run_cli(capsys, "review-sample", "--library", library, "--ledger", ledger, "--out", again)
    reviewed = {row["span_id"] for row in sample_rows(answers) if row["verdict"]}
    assert len(reviewed) == 100
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
    run_cli(capsys, "review-sample", "--library", library, "--out", sample)

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

    medium_answers = tmp_path / "medium.jsonl"
    answered(sample, medium_answers, 100, stratum="M")
    assert record_answers(capsys, library, ledger, sample, medium_answers)[0] == 0
    entries, result = apply_entries(
        capsys, library, tmp_path / "two.json", "--ledger", ledger, "--owner-id", "rob"
    )
    assert result["eligible_strata"] == ["typed_prefix:M", "typed_prefix:S"]
    texts = eligible_owner_texts(library, entries)
    assert any("tokenizes then builds" in text for text in texts)
    assert max(len(text) for text in texts) <= 2000


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
    run_cli(capsys, "review-sample", "--library", library, "--out", sample)
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


def test_review_record_rejects_bad_answers_before_writing_anything(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "g", typed_records=3)
    sample, answers, ledger = (tmp_path / name for name in ("s.jsonl", "a.jsonl", "l.jsonl"))
    run_cli(capsys, "review-sample", "--library", library, "--out", sample)
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
    run_cli(capsys, "review-sample", "--library", library, "--out", sample)
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
    recorded = json.loads(ledger.read_text(encoding="utf-8"))
    assert recorded["verdict"] == "yes" and recorded["reviewer"] == "rob"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", recorded["reviewed_at"])


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
    assert run_cli(capsys, "review-sample", "--library", library, "--out", sample)[0] == 0
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
    assert run_cli(capsys, "review-sample", "--library", library, "--out", sample)[0] == 0
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
            ("review-sample", "--library", library, "--out", target),
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
        ("review-sample", "--library", library, "--out", repo / "visible" / "s.jsonl"),
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
        assert run_cli(capsys, "review-sample", "--library", library, "--out", sample)[0] == 0
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
        capsys, "review-sample", "--library", library, "--out", cut, "--json"
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
        capsys, "review-sample", "--library", library, "--out", sample, "--json"
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
        code, _, err = run_cli(capsys, command, "--library", library, "--out", out)
        assert code == 2 and "leftover" in err and ".partial" in err
        assert len(err.strip().splitlines()) == 1
        assert out.with_name(name + ".partial").read_text(encoding="utf-8") == "old"


def test_an_existing_output_is_refused_for_every_writer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = build_library(tmp_path / "store" / "gen")
    out = tmp_path / "taken.out"
    out.write_text("keep", encoding="utf-8")
    for command in ("apply", "review-sample"):
        code, _, err = run_cli(capsys, command, "--library", library, "--out", out)
        assert code == 2 and "already exists" in err
    link = tmp_path / "link.out"
    link.symlink_to(tmp_path / "target.out")
    assert run_cli(capsys, "apply", "--library", library, "--out", link)[0] == 2
    assert not (tmp_path / "target.out").exists()
    assert out.read_text(encoding="utf-8") == "keep"


# --- registration ----------------------------------------------------------------------------


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
