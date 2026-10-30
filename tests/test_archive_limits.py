"""The size and fan-out limits, and the census that checks a tree against them."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conversation_corpus_engine.archive_limits import (
    DATA,
    KIB,
    MIB,
    NOTE,
    SQLITE,
    Limits,
    census_problems,
    file_class,
    take_census,
    verify_limits,
)
from conversation_corpus_engine.cli import main


def test_the_defaults_are_the_agreed_ceilings() -> None:
    limits = Limits()
    assert limits.note_bytes == 100 * KIB and limits.note_ceiling == 128 * KIB
    assert limits.folder_entries == 1800 and limits.folder_ceiling == 2000
    assert limits.data_ceiling == 64 * MIB and limits.shard_bytes == 48 * MIB
    assert limits.shard_bytes < limits.data_ceiling, "a shard must roll over before the ceiling"


def test_scaled_limits_keep_the_ceilings_in_the_default_proportion() -> None:
    default = Limits.scaled()
    assert default == Limits()
    small = Limits.scaled(note_bytes=6144, folder_entries=40, shard_bytes=65536)
    assert small.note_ceiling == 7864 and small.folder_ceiling == 44
    assert small.data_ceiling == Limits().data_ceiling


@pytest.mark.parametrize(
    "kwargs",
    [
        {"shard_bytes": 100 * MIB},
        {"note_bytes": 100},
        {"note_bytes": 4096, "note_ceiling": 2048},
        {"folder_entries": 1},
        {"folder_entries": 50, "folder_ceiling": 40},
        {"shard_bytes": 10},
    ],
)
def test_limits_that_contradict_themselves_are_refused(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        Limits(**kwargs)


def test_file_class_is_decided_by_suffix_and_sqlite_is_its_own_class() -> None:
    assert file_class("a.md") == NOTE and file_class("A.HTML") == NOTE
    assert file_class("shard-chatgpt-202506.sqlite") == SQLITE and file_class("x.db") == SQLITE
    for name in ("x.jsonl", "x.json", "x.js", "x.graphml", "x.css", "noext"):
        assert file_class(name) == DATA


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "ok.md").write_bytes(b"x" * 1000)
    (root / "data.jsonl").write_bytes(b"y" * 5000)
    (root / "shard.sqlite").write_bytes(b"z" * 300_000)
    return root


def test_a_tree_within_its_limits_is_ok_and_sqlite_is_exempt_from_the_size_ceiling(
    tmp_path: Path,
) -> None:
    limits = Limits(note_bytes=2048, note_ceiling=4096, data_ceiling=100_000, shard_bytes=65536)
    report = take_census(_tree(tmp_path), limits)
    assert report["ok"] and report["complete"]
    assert report["classes"][SQLITE]["max_bytes"] == 300_000 > limits.data_ceiling
    assert report["files"] == 3 and report["directories"] == 2
    assert verify_limits(_tree(tmp_path / "again"), limits) == []


def test_a_big_note_a_big_data_file_and_a_wide_folder_are_each_reported(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    (root / "notes" / "huge.html").write_bytes(b"h" * 9000)
    (root / "huge.json").write_bytes(b"j" * 200_000)
    wide = root / "wide"
    wide.mkdir()
    for number in range(30):
        (wide / f"f{number:03d}.txt").write_bytes(b"")
    limits = Limits(
        note_bytes=2048,
        note_ceiling=4096,
        data_ceiling=100_000,
        folder_entries=10,
        folder_ceiling=20,
        shard_bytes=65536,
    )
    report = take_census(root, limits)
    assert not report["ok"]
    assert report["violations"]["counts"] == {"notes": 1, "data": 1, "folders": 1}
    assert report["violations"]["notes"][0] == {"path": "notes/huge.html", "bytes": 9000}
    assert report["violations"]["folders"][0] == {"path": "wide", "entries": 30}
    assert report["widest_folder"] == {"path": "wide", "entries": 30}
    assert report["largest"][0]["bytes"] == 300_000
    problems = census_problems(report)
    assert len(problems) == 3 and "note/page" in problems[0]


def test_a_census_that_ran_out_of_time_never_claims_the_tree_is_fine(tmp_path: Path) -> None:
    report = take_census(_tree(tmp_path), Limits(), deadline_seconds=-1)
    assert report["complete"] is False and report["ok"] is False
    assert "incomplete" in census_problems(report)[0]


def test_the_census_reads_metadata_only_and_refuses_a_missing_root(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        take_census(tmp_path / "missing")


def test_the_census_command_exits_one_on_a_violation_and_zero_when_clean(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = _tree(tmp_path)

    def run(*argv: str) -> tuple[int, dict]:
        monkeypatch.setattr("sys.argv", ["cce", "archive", "census", "--root", str(root), *argv])
        code = 0
        try:
            main()
        except SystemExit as exit_:
            code = int(exit_.code or 0)
        out = capsys.readouterr().out
        return code, json.loads(out) if out.strip().startswith("{") else {}

    code, report = run()
    assert code == 0 and report["ok"] and report["problems"] == []
    (root / "notes" / "big.md").write_bytes(b"b" * 200_000)
    code, report = run()
    assert code == 1 and report["violations"]["counts"]["notes"] == 1
    assert report["problems"]
    code, _ = run("--note-bytes", "150000")
    assert code == 1, "--note-bytes moves the cut size, not the ceiling"
    monkeypatch.setattr("sys.argv", ["cce", "archive", "census", "--root", str(root / "nope")])
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 2
