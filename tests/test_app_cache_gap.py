from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conversation_corpus_engine import app_cache_commands, app_cache_gap  # noqa: E402
from conversation_corpus_engine.archive_ingest import ArchivePaths  # noqa: E402
from conversation_corpus_engine.archive_model import stable_uid  # noqa: E402


def _archive(root: Path) -> Path:
    paths = ArchivePaths(root)
    rows = {
        "chatgpt": [
            {
                "uid": "chatgpt-aaa",
                "updated": "2026-10-01T00:00:00Z",
                "last_activity": "2026-10-17T00:00:00Z",
            },
            {
                "uid": "chatgpt-bbb",
                "updated": "2026-05-01T00:00:00Z",
                "last_activity": "2026-05-01T00:00:00Z",
            },
        ],
        "gemini": [
            {
                "uid": stable_uid("gemini", "c_known"),
                "updated": None,
                "last_activity": "2026-10-16T00:00:00Z",
            }
        ],
        "perplexity": [
            {"uid": "perplexity-x", "updated": None, "last_activity": "2026-10-16T16:36:05Z"}
        ],
    }
    for provider, items in rows.items():
        target = paths.index(provider)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("".join(json.dumps(r) + "\n" for r in items), encoding="utf-8")
    return root


def _chatgpt(path: Path) -> Path:
    path.mkdir()
    entries = [
        {
            "id": "aaa",
            "title": "covered",
            "update_time": "2026-10-10T00:00:00.5Z",
            "body_cached": True,
        },
        {
            "id": "bbb",
            "title": "newer with body",
            "update_time": "2026-10-20T00:00:00Z",
            "body_cached": True,
        },
        {
            "id": "ccc",
            "title": "new meta",
            "update_time": "2026-11-01T00:00:00Z",
            "body_cached": False,
        },
        {
            "id": "ddd",
            "title": "new with body",
            "update_time": "2026-11-02T00:00:00Z",
            "body_cached": True,
        },
        {
            "id": "ee",
            "title": "newer meta",
            "update_time": "2026-06-01T00:00:00Z",
            "body_cached": False,
        },
    ]
    (path / "cache-index.json").write_text(json.dumps({"schema": "x", "conversations": entries}))
    bodies = [
        {"conversation_id": "aaa", "update_time": "2026-10-10T00:00:00Z", "mapping": {}},
        {"conversation_id": "bbb", "update_time": "2026-10-20T00:00:00Z", "mapping": {}},
        {"conversation_id": "ddd", "update_time": "2026-11-02T00:00:00Z", "mapping": {}},
    ]
    (path / "conversations-000.json").write_text(json.dumps(bodies))
    return path


def test_chatgpt_gap_buckets(tmp_path: Path) -> None:
    archive = _archive(tmp_path / "archive")
    report = app_cache_gap.build_report(archive, {"chatgpt": _chatgpt(tmp_path / "gpt")})
    info = report["providers"]["chatgpt"]
    assert info["already_covered"] == 1
    assert info["counts"] == {
        "new_with_body": 1,
        "newer_with_body": 1,
        "new_metadata_only": 2,
        "newer_metadata_only": 0,
    }
    assert info["ingestible_now"] == 2 and info["needs_owner_export"] == 2
    assert info["archive_last_activity"] == "2026-10-17"
    assert info["gap_updated_first"] == "2026-06-01" and info["gap_updated_last"] == "2026-11-02"
    assert info["gap_updated_by_month"] == {"2026-06": 1, "2026-10": 1, "2026-11": 2}
    assert "Export data" in info["owner_export"]


def test_epoch_and_iso_times_compare_alike() -> None:
    assert app_cache_gap._parse(1789242171.76) == app_cache_gap._parse("1789242171.76")
    assert app_cache_gap._parse("2026-11-01T00:00:00Z") == app_cache_gap._parse(
        "2026-11-01T00:00:00+00:00"
    )
    assert app_cache_gap._parse("not a time") is None and app_cache_gap._parse(None) is None


def test_gemini_gap_is_by_date_because_app_ids_do_not_map_to_archive_uids(tmp_path: Path) -> None:
    archive = _archive(tmp_path / "archive")
    staged = tmp_path / "gem"
    staged.mkdir()
    inventory = [
        {"chat_uuid": "c_known", "last_updated": "2026-11-05T00:00:00+02:00"},
        {"chat_uuid": "c_new", "last_updated": "2026-10-20T00:00:00Z", "cached_messages": 3},
        {"chat_uuid": "c_old", "last_updated": "2026-09-01T00:00:00Z"},
        {"chat_uuid": "c_end", "last_updated": "2026-10-16T00:00:00Z"},
    ]
    (staged / "gemini-store-inventory.json").write_text(json.dumps({"inventory": inventory}))
    info = app_cache_gap.build_report(archive, {"gemini": staged})["providers"]["gemini"]
    assert info["counts"] == {"newer_metadata_only": 2}
    assert info["already_covered"] == 2 and info["needs_owner_export"] == 2
    assert "Takeout" in info["owner_export"]


def test_perplexity_gap_compares_dates_only(tmp_path: Path) -> None:
    archive = _archive(tmp_path / "archive")
    staged = tmp_path / "px"
    staged.mkdir()
    records = [
        {"context_uuid": "space-1", "context_title": "n", "updated_at": "2026-11-09T20:01:00Z"},
        {"context_uuid": "followups-2", "context_title": "f", "created_at": "2026-10-01T00:00:00Z"},
    ]
    (staged / "conversations-000.json").write_text(json.dumps({"conversations": records}))
    info = app_cache_gap.build_report(archive, {"perplexity": staged})["providers"]["perplexity"]
    assert info["counts"] == {"new_with_body": 1}
    assert info["needs_owner_export"] is None
    assert "account export" in info["owner_typed_prompts_after_archive_end"]


def test_errors_are_named(tmp_path: Path) -> None:
    archive = _archive(tmp_path / "archive")
    with pytest.raises(app_cache_gap.GapError, match="at least one"):
        app_cache_gap.build_report(archive, {})
    with pytest.raises(app_cache_gap.GapError, match="does not exist"):
        app_cache_gap.build_report(archive, {"chatgpt": tmp_path / "none"})
    empty = tmp_path / "e"
    empty.mkdir()
    with pytest.raises(app_cache_gap.GapError, match="cache-index"):
        app_cache_gap.build_report(archive, {"chatgpt": empty})
    with pytest.raises(app_cache_gap.GapError, match="holds no chatgpt"):
        app_cache_gap.build_report(tmp_path / "blank", {"chatgpt": _chatgpt(tmp_path / "g2")})


def test_report_keeps_titles_out_of_coverage_and_is_owner_only(tmp_path: Path) -> None:
    archive = _archive(tmp_path / "archive")
    report = app_cache_gap.build_report(archive, {"chatgpt": _chatgpt(tmp_path / "gpt")})
    out = tmp_path / "out"
    coverage = app_cache_gap.write_report(report, out)
    assert "rows" not in coverage["providers"]["chatgpt"]
    assert "new meta" in (out / "gap-report.json").read_text()
    assert "new meta" not in (out / "coverage.json").read_text()
    assert (out.stat().st_mode & 0o777) == 0o700
    assert all((p.stat().st_mode & 0o077) == 0 for p in out.iterdir())
    with pytest.raises(FileExistsError):
        app_cache_gap.write_report(report, out)


def test_cli(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    archive = _archive(tmp_path / "archive")
    parser = argparse.ArgumentParser()
    app_cache_commands.add_app_cache_parser(parser.add_subparsers(dest="group"))
    gpt = _chatgpt(tmp_path / "gpt")
    args = parser.parse_args(
        [
            "app-cache",
            "gap",
            "--archive",
            str(archive),
            "--chatgpt",
            str(gpt),
            "--out",
            str(tmp_path / "o"),
        ]
    )
    assert app_cache_commands.handle_app_cache(args) == 0
    assert "chatgpt: archive ends 2026-10-17; ingestible now 2" in capsys.readouterr().out
    assert app_cache_commands.handle_app_cache(args) == 2
    none = parser.parse_args(
        ["app-cache", "gap", "--archive", str(archive), "--out", str(tmp_path / "p")]
    )
    assert app_cache_commands.handle_app_cache(none) == 2


def test_a_held_conversation_restamped_before_the_archive_end_is_drift_not_a_gap(
    tmp_path: Path,
) -> None:
    archive = _archive(tmp_path / "archive")
    staged = tmp_path / "gpt"
    staged.mkdir()
    entries = [
        {"id": "bbb", "title": "t", "update_time": "2026-06-01T00:00:00Z", "body_cached": False}
    ]
    (staged / "cache-index.json").write_text(json.dumps({"conversations": entries}))
    info = app_cache_gap.build_report(archive, {"chatgpt": staged})["providers"]["chatgpt"]
    assert info["already_covered"] == 1 and info["needs_owner_export"] == 0


def test_a_stale_body_under_newer_metadata_is_metadata_only(tmp_path: Path) -> None:
    archive = _archive(tmp_path / "archive")
    staged = tmp_path / "gpt"
    staged.mkdir()
    entries = [
        {"id": "bbb", "title": "t", "update_time": "2026-11-01T00:00:00Z", "body_cached": True}
    ]
    (staged / "cache-index.json").write_text(json.dumps({"conversations": entries}))
    stale = [{"conversation_id": "bbb", "update_time": "2026-10-01T00:00:00Z", "mapping": {}}]
    (staged / "conversations-000.json").write_text(json.dumps(stale))
    info = app_cache_gap.build_report(archive, {"chatgpt": staged})["providers"]["chatgpt"]
    assert info["counts"]["newer_metadata_only"] == 1 and info["ingestible_now"] == 0


def test_ids_the_archive_hashes_are_matched_through_stable_uid(tmp_path: Path) -> None:
    archive = _archive(tmp_path / "archive")
    uid = stable_uid("chatgpt", "ABC_1")
    assert uid != "chatgpt-ABC_1"
    shard = ArchivePaths(archive).index("chatgpt")
    shard.write_text(
        shard.read_text() + json.dumps({"uid": uid, "last_activity": "2026-05-01T00:00:00Z"}) + "\n"
    )
    staged = tmp_path / "gpt"
    staged.mkdir()
    entries = [{"id": "ABC_1", "title": "t", "update_time": "2026-05-02T00:00:00Z"}]
    (staged / "cache-index.json").write_text(json.dumps({"conversations": entries}))
    info = app_cache_gap.build_report(archive, {"chatgpt": staged})["providers"]["chatgpt"]
    assert info["already_covered"] == 1


def test_offset_times_normalise_to_utc_and_the_archive_path_is_not_reported(tmp_path: Path) -> None:
    assert app_cache_gap._day(app_cache_gap._parse("2026-11-01T00:30:00+02:00")) == "2026-10-31"
    archive = _archive(tmp_path / "archive")
    report = app_cache_gap.build_report(archive, {"chatgpt": _chatgpt(tmp_path / "gpt")})
    assert str(archive) not in json.dumps(app_cache_gap.write_report(report, tmp_path / "o"))


def test_malformed_inputs_are_named_errors(tmp_path: Path) -> None:
    archive = _archive(tmp_path / "archive")
    staged = tmp_path / "gem"
    staged.mkdir()
    (staged / "gemini-store-inventory.json").write_text("[]")
    with pytest.raises(app_cache_gap.GapError, match="inventory"):
        app_cache_gap.build_report(archive, {"gemini": staged})
    ArchivePaths(archive).index("gemini").write_text("{not json\n")
    with pytest.raises(app_cache_gap.GapError, match="cannot read the gemini index"):
        app_cache_gap.build_report(archive, {"gemini": staged})
