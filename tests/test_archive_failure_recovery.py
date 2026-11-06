"""Archive builds that fail or are interrupted, then rebuilt.

The lineage of generations must stay chained and complete across a crash, the site must show each
generation once, and what a failed provider left behind must be listed by verify and cleared by the
next successful build. Every string here is invented.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from test_archive_site_wiki import change_rows, edit_export, history_of, lineage_of, rebuild

from conversation_corpus_engine import archive_commands, archive_ingest, archive_lineage
from conversation_corpus_engine.archive_commands import verify_archive
from conversation_corpus_engine.archive_ingest import (
    BUILD_DIRNAME,
    ArchivePaths,
    ingest,
    iter_store,
    load_index,
    retained_after_failure,
)
from conversation_corpus_engine.archive_wiki import verify_site_wiki
from conversation_corpus_engine.cli import main


def _cce(monkeypatch, capsys, *argv: str) -> tuple[int, str]:
    monkeypatch.setattr(sys, "argv", ["cce", *argv])
    code = 0
    try:
        main()
    except SystemExit as exit_:
        code = int(exit_.code or 0)
    return code, capsys.readouterr().out


def build(env, monkeypatch, capsys) -> tuple[int, dict[str, Any]]:
    code, out = _cce(
        monkeypatch,
        capsys,
        "archive",
        "build",
        "--export-root",
        str(env.export),
        "--project-root",
        str(env.project),
        "--corpus-store-root",
        str(env.store),
    )
    return code, json.loads(out)


def digests_of(paths: ArchivePaths) -> dict[str, dict[str, str]]:
    return archive_lineage.load_digests(paths)


def every_uid(paths: ArchivePaths) -> list[str]:
    return sorted(row["uid"] for row in load_index(paths))


def crash_on_lineage_write(monkeypatch, nth: int) -> list[str]:
    """Fail the nth write the lineage module makes (a ledger row, then the digest baseline)."""
    real = archive_lineage.write_jsonl_atomic
    written: list[str] = []

    def flaky(path, rows):
        written.append(path.name)
        if len(written) == nth:
            raise OSError("simulated crash while recording the generation")
        return real(path, rows)

    monkeypatch.setattr(archive_lineage, "write_jsonl_atomic", flaky)
    return written


def assert_chained(ledger: list[dict[str, Any]]) -> None:
    assert [row["generation"] for row in ledger] == list(range(1, len(ledger) + 1))
    assert [row["parent"] for row in ledger] == ["", *[row["fingerprint"] for row in ledger[:-1]]]


def assert_no_uid_added_twice(ledger: list[dict[str, Any]]) -> None:
    added = [uid for row in ledger for uid in row["added"]]
    assert len(added) == len(set(added)), "a conversation is added by one generation only"


# --- (b) a build that fails after the lineage was recorded, then a rebuild ----------------------


def test_a_build_that_fails_after_the_lineage_was_recorded_rebuilds_one_chained_generation(
    archive_env, monkeypatch, capsys
) -> None:
    env = archive_env
    code, payload = build(env, monkeypatch, capsys)
    assert code == 0 and payload["ok"] is True
    uids = edit_export(env)

    def die(*args, **kwargs):
        raise RuntimeError("simulated failure part-way through the build")

    with monkeypatch.context() as patched:
        patched.setattr(archive_commands, "build_vault", die)
        with pytest.raises(RuntimeError, match="simulated"):
            build(env, monkeypatch, capsys)
    assert len(lineage_of(env.paths)) == 2, "the ingest stage had already recorded generation 2"
    code, payload = build(env, monkeypatch, capsys)
    assert code == 0 and payload["ok"] is True and payload["problems"] == []
    ledger = lineage_of(env.paths)
    assert_chained(ledger)
    assert_no_uid_added_twice(ledger)
    second = ledger[1]
    assert (second["added"], second["changed"], second["removed"]) == (
        [uids["added"]],
        [uids["changed"]],
        [uids["removed"]],
    )
    assert sorted(change_rows(env)) == sorted(
        [(uids["added"], "added"), (uids["changed"], "changed"), (uids["removed"], "removed")]
    )
    assert history_of(env, uids["changed"]).entries == [(2, "changed"), (1, "added")]
    assert (
        payload["wiki"]["generations"] == 2 and payload["wiki"]["lineage"]["status"] == "recorded"
    )


def test_a_rebuild_of_a_build_that_died_after_recording_adds_no_second_generation(
    archive_env, monkeypatch, capsys
) -> None:
    env = archive_env
    build(env, monkeypatch, capsys)
    edit_export(env)
    with monkeypatch.context() as patched:
        patched.setattr(archive_commands, "build_site", lambda *a, **k: 1 / 0)
        with pytest.raises(ZeroDivisionError):
            build(env, monkeypatch, capsys)
    before = lineage_of(env.paths)
    for _ in range(2):
        build(env, monkeypatch, capsys)
        assert lineage_of(env.paths) == before
    assert verify_archive(env.paths) == []


@pytest.mark.parametrize("nth", [1, 2], ids=["first-write", "second-write"])
def test_a_crash_while_recording_the_first_generation_is_rerun_to_a_complete_ledger(
    archive_env, monkeypatch, nth
) -> None:
    env = archive_env
    crash_on_lineage_write(monkeypatch, nth)
    with pytest.raises(OSError, match="simulated crash"):
        ingest(export_root=env.export, paths=env.paths, authorization=env.auth)
    monkeypatch.undo()
    rebuild(env)
    (first,) = lineage_of(env.paths)
    assert first["generation"] == 1 and first["parent"] == ""
    assert first["added"] == every_uid(env.paths), "the generation's changes were not lost"
    assert first["changed"] == [] and first["removed"] == []
    assert sorted(uid for uid, kind in change_rows(env) if kind == "added") == every_uid(env.paths)
    assert sum(len(rows) for rows in digests_of(env.paths).values()) == len(every_uid(env.paths))
    assert verify_site_wiki(env.paths)["problems"] == []


@pytest.mark.parametrize("nth", [1, 2], ids=["first-write", "second-write"])
def test_a_crash_while_recording_a_later_generation_is_rerun_to_its_exact_changes(
    archive_env, monkeypatch, nth
) -> None:
    env = archive_env
    rebuild(env)
    uids = edit_export(env)
    crash_on_lineage_write(monkeypatch, nth)
    with pytest.raises(OSError, match="simulated crash"):
        ingest(export_root=env.export, paths=env.paths, authorization=env.auth)
    monkeypatch.undo()
    rebuild(env)
    ledger = lineage_of(env.paths)
    assert_chained(ledger)
    assert len(ledger) == 2, "one generation for one change of the sources"
    assert_no_uid_added_twice(ledger)
    second = ledger[1]
    assert (second["added"], second["changed"], second["removed"]) == (
        [uids["added"]],
        [uids["changed"]],
        [uids["removed"]],
    )
    assert history_of(env, uids["changed"]).entries == [(2, "changed"), (1, "added")]
    assert verify_site_wiki(env.paths)["problems"] == []
    assert sum(len(rows) for rows in digests_of(env.paths).values()) == len(every_uid(env.paths))


def test_a_crash_between_the_manifest_and_the_lineage_is_rerun_to_the_exact_changes(
    archive_env, monkeypatch
) -> None:
    env = archive_env
    rebuild(env)
    uids = edit_export(env)

    def die(*args, **kwargs):
        raise OSError("simulated crash after the manifest was written")

    monkeypatch.setattr(archive_ingest, "record_generation", die)
    with pytest.raises(OSError, match="simulated crash"):
        ingest(export_root=env.export, paths=env.paths, authorization=env.auth)
    monkeypatch.undo()
    rebuild(env)
    ledger = lineage_of(env.paths)
    assert_chained(ledger)
    assert len(ledger) == 2 and ledger[1]["added"] == [uids["added"]]
    assert verify_site_wiki(env.paths)["problems"] == []


def test_a_lost_digest_baseline_is_rebuilt_without_inventing_a_generation(
    archive_env, monkeypatch, capsys
) -> None:
    """verify says 'run `cce archive ingest`' for a ledger whose digest file is gone; doing that
    on unchanged sources must restore the baseline, not list every conversation as added again."""
    env = archive_env
    build(env, monkeypatch, capsys)
    edit_export(env)
    build(env, monkeypatch, capsys)
    ledger, baseline = lineage_of(env.paths), digests_of(env.paths)
    assert len(ledger) == 2
    env.paths.digests.unlink()
    assert any("digest baseline missing" in p for p in verify_site_wiki(env.paths)["problems"])
    ingest(export_root=env.export, paths=env.paths, authorization=env.auth)
    assert lineage_of(env.paths) == ledger and digests_of(env.paths) == baseline
    build(env, monkeypatch, capsys)
    assert verify_archive(env.paths) == []


def test_a_crash_rewriting_an_unchanged_baseline_changes_nothing_that_was_recorded(
    archive_env, monkeypatch
) -> None:
    env = archive_env
    rebuild(env)
    ledger, baseline = lineage_of(env.paths), digests_of(env.paths)
    crash_on_lineage_write(
        monkeypatch, 1
    )  # a rerun with nothing new to say writes the baseline only
    with pytest.raises(OSError, match="simulated crash"):
        ingest(export_root=env.export, paths=env.paths, authorization=env.auth)
    monkeypatch.undo()
    assert lineage_of(env.paths) == ledger and digests_of(env.paths) == baseline


# --- (c) retained shards of a failed provider ---------------------------------------------------


def break_perplexity(env) -> tuple[Path, str]:
    path = env.export / "perplexity" / "conversations-20250101.json"
    saved = path.read_text("utf-8")
    path.write_text("{", encoding="utf-8")
    return path, saved


def test_a_failed_rebuild_keeps_serving_the_retained_shards_and_verify_lists_them(
    archive_env, monkeypatch, capsys
) -> None:
    env = archive_env
    code, good = build(env, monkeypatch, capsys)
    assert code == 0
    total = good["stages"]["ingest"]["totals"]["conversations"]
    path, _ = break_perplexity(env)
    code, failed = build(env, monkeypatch, capsys)
    assert code == 1 and failed["ok"] is False
    ingest_report = failed["stages"]["ingest"]["providers"]["perplexity"]
    assert ingest_report["status"] == "failed" and ingest_report["retained"] is True
    assert failed["stages"]["ingest"]["totals"]["conversations"] == total
    (listed,) = [p for p in failed["problems"] if "perplexity failed" in p]
    assert "retained from the previous build" in listed
    # What verify does not complain about: the retained shards, receipts and pages are whole.
    assert [p for p in failed["problems"] if p != listed] == []
    manifest = json.loads(env.paths.manifest.read_text("utf-8"))
    assert retained_after_failure(manifest["providers"]["perplexity"])
    assert len(list(iter_store(env.paths, ["perplexity"]))) > 0
    assert failed["wiki"]["lineage"]["status"] == "recorded"
    assert path.read_text("utf-8") == "{"


def test_the_next_successful_rebuild_clears_what_the_failed_one_retained(
    archive_env, monkeypatch, capsys
) -> None:
    env = archive_env
    build(env, monkeypatch, capsys)
    path, saved = break_perplexity(env)
    build(env, monkeypatch, capsys)
    build(env, monkeypatch, capsys)  # a second failure must not pile anything up
    manifest = json.loads(env.paths.manifest.read_text("utf-8"))
    assert manifest["providers"]["perplexity"]["status"] == "failed"
    path.write_text(saved, encoding="utf-8")
    code, fixed = build(env, monkeypatch, capsys)
    assert code == 0 and fixed["ok"] is True and fixed["problems"] == []
    manifest = json.loads(env.paths.manifest.read_text("utf-8"))
    receipt = manifest["providers"]["perplexity"]
    assert receipt["status"] == "ok" and "failed_attempt" not in receipt and "error" not in receipt
    assert not list(env.paths.root.rglob(BUILD_DIRNAME)), "no half-built shard directory remains"
    ledger = lineage_of(env.paths)
    assert_chained(ledger)
    assert_no_uid_added_twice(ledger)
    assert verify_archive(env.paths) == []


def test_a_failed_rebuild_that_repeats_adds_no_generation(archive_env, monkeypatch, capsys) -> None:
    env = archive_env
    build(env, monkeypatch, capsys)
    break_perplexity(env)
    build(env, monkeypatch, capsys)
    recorded = lineage_of(env.paths)
    build(env, monkeypatch, capsys)
    assert lineage_of(env.paths) == recorded, "the same failure is the same generation"
    assert_chained(recorded)


def test_a_half_built_shard_directory_from_a_killed_ingest_does_not_survive_the_next_one(
    archive_env,
) -> None:
    env = archive_env
    rebuild(env)
    leftover = env.paths.provider_dir("chatgpt") / BUILD_DIRNAME
    leftover.mkdir()
    (leftover / "shard-chatgpt-202401.jsonl").write_text('{"half": "built"}\n', encoding="utf-8")
    before = lineage_of(env.paths)
    ingest(export_root=env.export, paths=env.paths, authorization=env.auth)
    assert not leftover.exists()
    assert lineage_of(env.paths) == before
    assert len(list(iter_store(env.paths, ["chatgpt"]))) == len(
        [uid for uid in every_uid(env.paths) if uid.startswith("chatgpt-")]
    )
