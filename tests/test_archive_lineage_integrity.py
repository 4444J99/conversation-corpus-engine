"""The lineage when its files were changed behind its back, or cut short.

A crash leaves a state the next ingest settles (see test_archive_failure_recovery). These are the
states a crash cannot leave: the pending baseline deleted or edited after the ledger row that
describes it was written, a ledger whose last line is partial, a stray second pending file. They
must not be repaired silently into a second record of the same generation. Every string here is
invented.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from test_archive_failure_recovery import crash_at, edit_export_again
from test_archive_site_wiki import edit_export, lineage_of, rebuild

from conversation_corpus_engine import archive_layout as layout
from conversation_corpus_engine import archive_lineage
from conversation_corpus_engine.archive_ingest import ArchivePaths, ingest
from conversation_corpus_engine.archive_naming import verify_names
from conversation_corpus_engine.archive_wiki import verify_site_wiki
from conversation_corpus_engine.cli import main

DIVERGED = "lineage baseline diverged from the ledger"
REMEDY = "--rebuild-lineage"


def cce(monkeypatch, capsys, *argv: str) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "argv", ["cce", *argv])
    code = 0
    try:
        main()
    except SystemExit as exit_:
        code = int(exit_.code or 0)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def archive_ingest_command(env, *extra: str) -> list[str]:
    return [
        "archive",
        "ingest",
        "--export-root",
        str(env.export),
        "--project-root",
        str(env.project),
        "--corpus-store-root",
        str(env.store),
        *extra,
    ]


def crashed_after_the_row(env, monkeypatch) -> None:
    """Generation 2's ledger row is written and its pending baseline is intact; only the rename
    is missing (the state `finish_interrupted_commit` completes)."""
    rebuild(env)
    edit_export(env)
    crash_at(monkeypatch, "rename")
    with pytest.raises(OSError, match="simulated crash"):
        ingest(export_root=env.export, paths=env.paths, authorization=env.auth)
    monkeypatch.undo()
    assert len(lineage_of(env.paths)) == 2 and env.paths.digests_pending.is_file()


def problems_of(env) -> list[str]:
    return verify_site_wiki(env.paths)["problems"]


# --- the pending baseline deleted or edited after its row was written -----------------------------


@pytest.mark.parametrize("tamper", ["delete", "modify"])
def test_a_pending_baseline_deleted_or_edited_after_its_row_is_not_recorded_again(
    archive_env, monkeypatch, tamper
) -> None:
    env = archive_env
    crashed_after_the_row(env, monkeypatch)
    pending = env.paths.digests_pending
    if tamper == "delete":
        pending.unlink()
    else:
        pending.write_bytes(pending.read_bytes() + b"\n")
    ledger, baseline = lineage_of(env.paths), env.paths.digests.read_bytes()
    with pytest.raises(archive_lineage.LineageError, match=DIVERGED) as raised:
        ingest(export_root=env.export, paths=env.paths, authorization=env.auth)
    assert REMEDY in str(raised.value), "the refusal names the remedy"
    assert lineage_of(env.paths) == ledger, "no generation was recorded"
    assert env.paths.digests.read_bytes() == baseline, "the baseline was not replaced"
    assert pending.exists() == (tamper == "modify"), "an edited pending file is kept as evidence"
    report = verify_site_wiki(env.paths)
    assert report["lineage"]["status"] == "baseline-diverged"
    assert any(DIVERGED in problem and REMEDY in problem for problem in report["problems"])


def test_the_command_exits_2_with_the_remedy_and_changes_nothing(
    archive_env, monkeypatch, capsys
) -> None:
    env = archive_env
    crashed_after_the_row(env, monkeypatch)
    env.paths.digests_pending.unlink()
    ledger = lineage_of(env.paths)
    code, out, err = cce(monkeypatch, capsys, *archive_ingest_command(env))
    assert code == 2 and out == ""
    assert DIVERGED in err and REMEDY in err
    assert lineage_of(env.paths) == ledger


def test_rebuild_lineage_records_one_flagged_generation_and_deletes_no_row(
    archive_env, monkeypatch, capsys
) -> None:
    env = archive_env
    crashed_after_the_row(env, monkeypatch)
    env.paths.digests_pending.unlink()
    before = lineage_of(env.paths)
    code, _, err = cce(monkeypatch, capsys, *archive_ingest_command(env, "--rebuild-lineage"))
    assert code == 0, err
    ledger = lineage_of(env.paths)
    assert ledger[:2] == before, "every earlier row is untouched"
    (rebuilt,) = ledger[2:]
    assert rebuilt["rebuilt"] is True and rebuilt["generation"] == 3
    assert rebuilt["parent"] == before[-1]["fingerprint"]
    assert (rebuilt["added"], rebuilt["changed"], rebuilt["removed"]) == ([], [], []), (
        "what changed since the last good row cannot be known, so none is claimed"
    )
    digests = archive_lineage.load_digests(env.paths)
    assert rebuilt["conversations"] == sum(len(rows) for rows in digests.values())
    assert archive_lineage.baseline_state(env.paths, ledger) == "agree"
    assert not env.paths.digests_pending.exists()
    # the next plain ingest trusts the rebuilt baseline and adds nothing
    ingest(export_root=env.export, paths=env.paths, authorization=env.auth)
    assert lineage_of(env.paths) == ledger
    rebuild(env)
    assert problems_of(env) == []


def test_a_healthy_lineage_is_not_flagged_and_is_not_rebuilt_by_a_plain_ingest(archive_env) -> None:
    env = archive_env
    rebuild(env)
    edit_export(env)
    rebuild(env)
    ledger = lineage_of(env.paths)
    assert archive_lineage.baseline_state(env.paths, ledger) == "agree"
    assert problems_of(env) == []
    ingest(export_root=env.export, paths=env.paths, authorization=env.auth)
    assert lineage_of(env.paths) == ledger and not any("rebuilt" in row for row in ledger)


def test_a_row_that_carries_no_baseline_hash_has_nothing_to_compare(archive_env) -> None:
    """Ledgers written before rows carried the hash keep working: no hash, no verdict."""
    env = archive_env
    rebuild(env)
    (row,) = lineage_of(env.paths)
    del row["baseline"]
    env.paths.lineage.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")
    env.paths.digests.write_text("", encoding="utf-8")  # not the baseline any row described
    assert archive_lineage.baseline_state(env.paths, [row]) == "none"
    assert not any(DIVERGED in problem for problem in problems_of(env))


# --- a ledger cut short ---------------------------------------------------------------------------


def truncated_ledger(env) -> tuple[list[dict[str, Any]], bytes]:
    rebuild(env)
    edit_export(env)
    rebuild(env)
    ledger = lineage_of(env.paths)
    assert len(ledger) == 2
    data = env.paths.lineage.read_bytes()
    env.paths.lineage.write_bytes(data[:-15])  # the last row loses its end, newline included
    return ledger, data[:-15]


def test_a_ledger_with_a_partial_last_line_names_the_file_and_the_remedy(archive_env) -> None:
    env = archive_env
    truncated_ledger(env)
    with pytest.raises(archive_lineage.LineageError) as raised:
        archive_lineage.load_lineage(env.paths)
    message = str(raised.value)
    assert layout.LINEAGE_FILENAME in message and REMEDY in message
    assert "line 2" in message and "1 readable" in message
    with pytest.raises(archive_lineage.LineageError):
        ingest(export_root=env.export, paths=env.paths, authorization=env.auth)


def test_verify_reports_a_damaged_ledger_instead_of_failing_on_it(archive_env) -> None:
    env = archive_env
    truncated_ledger(env)
    report = verify_site_wiki(env.paths)
    assert report["lineage"]["status"] == "damaged"
    (problem,) = report["problems"]
    assert layout.LINEAGE_FILENAME in problem and REMEDY in problem


def test_the_command_refuses_a_damaged_ledger_with_exit_2(archive_env, monkeypatch, capsys) -> None:
    env = archive_env
    truncated_ledger(env)
    code, _, err = cce(monkeypatch, capsys, *archive_ingest_command(env))
    assert code == 2 and layout.LINEAGE_FILENAME in err and REMEDY in err
    assert "JSONDecodeError" not in err and "Traceback" not in err


def test_rebuild_lineage_keeps_the_readable_rows_and_a_copy_of_the_damaged_file(
    archive_env, monkeypatch, capsys
) -> None:
    env = archive_env
    ledger, damaged = truncated_ledger(env)
    code, _, err = cce(monkeypatch, capsys, *archive_ingest_command(env, "--rebuild-lineage"))
    assert code == 0, err
    rows = lineage_of(env.paths)
    assert rows[0] == ledger[0], "the readable row is untouched"
    assert [row["generation"] for row in rows] == [1, 2]
    assert rows[1].get("rebuilt") is True and rows[1]["parent"] == ledger[0]["fingerprint"]
    copy = env.paths.lineage.with_name(env.paths.lineage.name + ".damaged")
    assert copy.read_bytes() == damaged, "the unreadable ledger is kept, not discarded"
    assert verify_names(env.paths.root)["ok"], "the saved copy keeps a name the contract accepts"


def test_a_ledger_damaged_in_the_middle_is_never_rebuilt_over_the_rows_after_it(
    archive_env, monkeypatch, capsys
) -> None:
    """Rows that still parse after the bad line would be dropped from the live ledger by a
    rebuild, which is "deletes no row" only in a copy. So the rebuild refuses, before it reads
    anything, and the file is left as it is."""
    env = archive_env
    rebuild(env)
    edit_export(env)
    rebuild(env)
    edit_export_again(env)
    rebuild(env)
    assert len(lineage_of(env.paths)) == 3
    lines = env.paths.lineage.read_text(encoding="utf-8").splitlines(keepends=True)
    lines[1] = '{"generation": 2, "fingerprint": \n'
    env.paths.lineage.write_text("".join(lines), encoding="utf-8")
    damaged = env.paths.lineage.read_bytes()
    with pytest.raises(archive_lineage.LineageError, match="damaged in the middle") as raised:
        archive_lineage.load_lineage(env.paths)
    assert "1 more row" in str(raised.value) and REMEDY not in str(raised.value)
    for extra in ([], ["--rebuild-lineage"]):
        code, _, err = cce(monkeypatch, capsys, *archive_ingest_command(env, *extra))
        assert code == 2 and "damaged in the middle" in err and "Traceback" not in err
    assert env.paths.lineage.read_bytes() == damaged
    assert not env.paths.lineage.with_name(env.paths.lineage.name + ".damaged").exists()
    (problem,) = verify_site_wiki(env.paths)["problems"]
    assert "damaged in the middle" in problem


def test_a_ledger_that_is_not_made_of_rows_is_damaged_too(archive_env) -> None:
    env = archive_env
    rebuild(env)
    env.paths.lineage.write_text("[1, 2, 3]\n", encoding="utf-8")
    with pytest.raises(archive_lineage.LineageError, match="not a generation row"):
        archive_lineage.load_lineage(env.paths)


# --- two pending files ----------------------------------------------------------------------------


def test_a_stray_second_pending_file_is_never_adopted_and_verify_names_it(
    archive_env, monkeypatch
) -> None:
    env = archive_env
    crashed_after_the_row(env, monkeypatch)
    genuine = env.paths.digests_pending.read_bytes()
    stray = env.paths.digests.with_name(env.paths.digests.name + ".pending.old")
    stray.write_text('{"provider": "chatgpt", "uid": "x", "digest": "0"}\n', encoding="utf-8")
    assert any("stray pending" in problem and stray.name in problem for problem in problems_of(env))
    ledger = lineage_of(env.paths)
    ingest(export_root=env.export, paths=env.paths, authorization=env.auth)
    assert lineage_of(env.paths) == ledger, "the genuine pending file finished generation 2"
    assert env.paths.digests.read_bytes() == genuine
    assert stray.exists(), "a file the ingest does not own is left alone"
    assert any("stray pending" in problem for problem in problems_of(env))


# --- the states, one by one -----------------------------------------------------------------------

SOURCES = {
    "g1": {"u1": "d1", "u2": "d2"},
    "g2": {"u1": "d1x", "u2": "d2", "u3": "d3"},
}


def record(paths: ArchivePaths, tag: str, **kwargs: Any) -> dict[str, Any] | None:
    manifest = {"providers": {"chatgpt": {"status": "ok"}}, "source_fingerprint": f"fp-{tag}"}
    return archive_lineage.record_generation(
        paths, manifest, {"chatgpt": SOURCES[tag]}, read=["chatgpt"], **kwargs
    )


def state_of(paths: ArchivePaths) -> str:
    return archive_lineage.baseline_state(paths, archive_lineage.load_lineage(paths))


def test_baseline_state_tells_the_five_situations_apart(tmp_path, monkeypatch) -> None:
    paths = ArchivePaths(tmp_path / "archive")
    assert state_of(paths) == "none", "no ledger, nothing to compare"
    record(paths, "g1")
    assert state_of(paths) == "agree"

    def boom(pending: Path, baseline: Path) -> None:
        raise OSError("simulated crash before the rename")

    with monkeypatch.context() as patch:
        patch.setattr(archive_lineage, "_commit_baseline", boom)
        with pytest.raises(OSError, match="simulated crash"):
            record(paths, "g2")
    assert state_of(paths) == "finish", "the row is written and the pending file is its baseline"
    paths.digests_pending.unlink()
    assert state_of(paths) == "diverged", "the row is written and nothing describes it any more"
    paths.digests.unlink()
    assert state_of(paths) == "missing"
    paths.digests.write_text("{}\n", encoding="utf-8")
    assert state_of(paths) == "diverged"


def test_record_generation_refuses_a_diverged_baseline_and_rebuild_accepts_it(tmp_path) -> None:
    paths = ArchivePaths(tmp_path / "archive")
    record(paths, "g1")
    paths.digests.write_text("{}\n", encoding="utf-8")
    ledger = archive_lineage.load_lineage(paths)
    with pytest.raises(archive_lineage.LineageError, match=DIVERGED):
        record(paths, "g2")
    assert archive_lineage.load_lineage(paths) == ledger
    row = record(paths, "g2", rebuild=True)
    assert row is not None and row["rebuilt"] is True and row["generation"] == 2
    assert (row["added"], row["changed"], row["removed"]) == ([], [], [])
    assert row["conversations"] == len(SOURCES["g2"])
    assert archive_lineage.load_lineage(paths)[:1] == ledger
    assert state_of(paths) == "agree"
