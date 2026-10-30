"""The bounded layout end to end, on the fixture's stress mode.

One build of the synthetic exports plus one 380 KB reply and a crowd of short conversations in one
month, made with deliberately small limits, must keep every surface inside them without losing a
character, and must reconcile its author counts and shard counts to the manifest.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from conversation_corpus_engine import archive_search
from conversation_corpus_engine.archive_author import HUMAN, MACHINE, reconcile
from conversation_corpus_engine.archive_commands import verify_archive
from conversation_corpus_engine.archive_fixture import LONG_THREAD_ID, write_fixture
from conversation_corpus_engine.archive_ingest import (
    DEFAULT_ARCHIVE_DIRNAME,
    ArchivePaths,
    authorize_archive,
    iter_store,
    load_manifest,
)
from conversation_corpus_engine.archive_limits import Limits, take_census
from conversation_corpus_engine.archive_naming import verify_names
from conversation_corpus_engine.archive_turns import load_turn_manifest, verify_turn_store
from conversation_corpus_engine.archive_vault import verify_vault
from conversation_corpus_engine.cli import main
from conversation_corpus_engine.corpus_store import register_corpus_store

STRESS = 90
SMALL = ("--note-bytes", "6144", "--folder-entries", "40", "--shard-bytes", "65536")
LIMITS = Limits.scaled(note_bytes=6144, folder_entries=40, shard_bytes=65536)
LONG_UID = f"chatgpt-{LONG_THREAD_ID}"


def _cce(monkeypatch, capsys, *argv: str) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "argv", ["cce", *argv])
    code = 0
    try:
        main()
    except SystemExit as exit_:
        code = int(exit_.code or 0)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _env(tmp_path: Path, stress: int) -> SimpleNamespace:
    export = tmp_path / "export"
    write_fixture(export, stress=stress)
    project, store = tmp_path / "project", tmp_path / "store"
    project.mkdir()
    store.mkdir()
    register_corpus_store(project, store)
    authorization = authorize_archive(
        project_root=project,
        archive_root=store / DEFAULT_ARCHIVE_DIRNAME,
        corpus_store_root=store,
        source_roots=(export,),
    )
    return SimpleNamespace(
        export=export,
        project=project,
        store=store,
        paths=ArchivePaths(authorization.destination),
        common=["--project-root", str(project), "--corpus-store-root", str(store)],
    )


def _build(env, monkeypatch, capsys, *limit_args: str) -> dict:
    code, out, err = _cce(
        monkeypatch,
        capsys,
        "archive",
        "build",
        "--export-root",
        str(env.export),
        *env.common,
        *limit_args,
    )
    payload = json.loads(out)
    assert code == 0 and payload["ok"], (payload["problems"], err[-400:])
    return payload


@pytest.fixture
def small(tmp_path, monkeypatch, capsys):
    env = _env(tmp_path, STRESS)
    payload = _build(env, monkeypatch, capsys, *SMALL)
    return env, payload


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def test_a_stress_build_verifies_and_passes_its_own_census(small) -> None:
    env, payload = small
    assert payload["problems"] == []
    assert verify_archive(env.paths, LIMITS) == []
    report = take_census(env.paths.root, LIMITS)
    assert report["ok"], report["violations"]
    assert report["widest_folder"]["entries"] <= LIMITS.folder_ceiling
    assert verify_names(env.paths.root)["ok"]
    assert verify_vault(env.paths) == []


def test_the_long_thread_is_split_in_the_vault_without_losing_a_character(small) -> None:
    env, _ = small
    note = env.paths.vault_note("chatgpt", LONG_UID)
    folder = note.parent / note.stem
    parts = sorted(folder.rglob("part-*.md"))
    first = note.read_text(encoding="utf-8")
    parts_declared = int(re.search(r"^parts: (\d+)$", first, re.MULTILINE).group(1))
    assert parts_declared == len(parts) + 1 > 20
    texts = [first, *(p.read_text(encoding="utf-8") for p in parts)]
    assert all(len(t.encode("utf-8")) <= LIMITS.note_bytes for t in texts)
    joined = "\n".join(texts)
    assert all(f"Row {n:05d}:" in joined for n in range(3000)), "a row of the log was dropped"
    assert all(f"step_{n:04d} =" in joined for n in range(400)), "a code line was dropped"
    wide = sum(len(run) for t in texts for run in re.findall(r"[wick]{400,}", t))
    assert wide == 120_000, "the one very long line lost characters"
    assert all(folder in (p.parent, p.parent.parent) for p in parts)


def test_the_long_thread_is_split_on_the_site_without_losing_a_character(small) -> None:
    env, _ = small
    page = env.paths.site_page("chatgpt", LONG_UID)
    folder = page.parent / page.stem
    parts = sorted(folder.rglob("part-*.html"))
    assert len(parts) > 20
    texts = [p.read_text(encoding="utf-8") for p in (page, *parts)]
    assert all(len(t.encode("utf-8")) <= LIMITS.note_bytes for t in texts)
    joined = "\n".join(texts)
    assert all(f"Row {n:05d}:" in joined for n in range(3000))
    wide = sum(len(run) for t in texts for run in re.findall(r"[wick]{400,}", t))
    assert wide == 120_000
    assert "Next →" in texts[0] and "Start" in texts[1] and "← Previous" in texts[1]


def test_a_crowded_month_spills_into_day_folders_and_every_folder_stays_small(small) -> None:
    env, _ = small
    october = [p for p in env.paths.vault_conversations.iterdir() if p.name == "month-all-202510"]
    assert october and any(child.name.startswith("day-all-") for child in october[0].iterdir())
    for root in (env.paths.vault, env.paths.site, env.paths.root):
        for folder in [root, *(p for p in root.rglob("*") if p.is_dir())]:
            assert sum(1 for _ in folder.iterdir()) <= LIMITS.folder_ceiling, folder


def test_hubs_that_outgrow_a_note_continue_on_numbered_pages(small) -> None:
    env, _ = small
    hubs = list(env.paths.vault_hubs.rglob("*.md"))
    assert any(p.name.startswith("part-all-p002-") for p in hubs)
    provider = next(p for p in hubs if p.stem == "provider-chatgpt")
    assert "Next →" in provider.read_text(encoding="utf-8")
    assert all(len(p.read_bytes()) <= LIMITS.note_bytes for p in hubs)


def test_the_normalized_layer_is_sliced_by_provider_and_month_and_rolls_over(small) -> None:
    env, _ = small
    manifest = load_manifest(env.paths)
    info = manifest["providers"]["chatgpt"]
    subjects = [shard["subject"] for shard in info["layout"]["shards"]]
    assert "202510-002" in subjects, "the crowded month rolled to a numbered shard"
    files = env.paths.shard_files("chatgpt")
    assert [f.name for f in files] == [f"shard-chatgpt-{s}.jsonl" for s in subjects]
    assert sum(1 for _ in iter_store(env.paths, ["chatgpt"])) == info["conversations"]
    assert not env.paths.conversations("chatgpt").exists(), "no monolith beside the shards"
    for shard in info["layout"]["shards"]:
        assert shard["bytes"] <= max(
            LIMITS.shard_bytes, shard["bytes"] if shard["conversations"] == 1 else 0
        )


def test_authors_reconcile_in_the_manifest_the_shards_and_both_containers(small) -> None:
    env, _ = small
    manifest = load_manifest(env.paths)
    assert reconcile(manifest["totals"]["authors"], manifest["totals"]["messages"]) == []
    turns = load_turn_manifest(env.paths)
    assert turns["totals"]["authors"] == manifest["totals"]["authors"]
    assert sum(turns["totals"]["authors"].values()) == manifest["totals"]["messages"]
    assert set(turns["containers"]) == {"authored", "machine"}
    assert turns["containers"]["authored"]["turns"] == manifest["totals"]["authors"][HUMAN]
    assert turns["containers"]["machine"]["turns"] == manifest["totals"]["authors"][MACHINE]
    assert verify_turn_store(env.paths) == []
    assert set(env.paths.container(HUMAN).iterdir()) != set(env.paths.container(MACHINE).iterdir())


def test_a_question_about_what_the_person_wrote_never_reaches_machine_text(small) -> None:
    env, _ = small
    paths = env.paths
    ask = archive_search.search(paths, "stress ask 0007", authors=["human"], limit=5)
    assert ask and all(hit["author"] == HUMAN for hit in ask)
    assert archive_search.search(paths, "stress ask 0007", authors=["machine"], limit=5) == []
    reply = archive_search.search(paths, "snug tight enough", authors=["machine"], limit=5)
    assert reply and all(hit["author"] == MACHINE for hit in reply)
    assert archive_search.search(paths, "snug tight enough", authors=["human"], limit=5) == []
    both = archive_search.search(paths, "snug tight enough", limit=5)
    assert {hit["author"] for hit in both} == {MACHINE}


def test_month_and_provider_filters_open_only_the_shards_they_name(small) -> None:
    env, _ = small
    paths = env.paths
    hits = archive_search.search(
        paths, "stress", months=["202510"], providers=["chatgpt"], limit=50
    )
    assert hits and all(
        hit["provider"] == "chatgpt" and hit["created"].startswith("2025-10") for hit in hits
    )
    assert archive_search.search(paths, "stress", months=["202401"], limit=5) == []
    assert archive_search.search(paths, "stress", providers=["claude"], limit=5) == []
    titles = archive_search.search_titles(paths, "stress conversation 0003", limit=5)
    assert titles and titles[0]["uid"] == "chatgpt-fixture-stress-0003"


def test_a_second_build_writes_byte_identical_files(small, tmp_path, monkeypatch, capsys) -> None:
    env, _ = small
    first = _tree(env.paths.root)
    _build(env, monkeypatch, capsys, *SMALL)
    second = _tree(env.paths.root)
    assert first.keys() == second.keys()
    changed = [
        name for name in first if first[name] != second[name] and not name.endswith(".sqlite")
    ]
    assert changed == []
    assert all(first[name] == second[name] or name.endswith(".sqlite") for name in first)


def test_the_default_limits_split_the_long_thread_into_a_handful_of_parts(
    tmp_path, monkeypatch, capsys
) -> None:
    env = _env(tmp_path, 12)
    _build(env, monkeypatch, capsys)
    note = env.paths.vault_note("chatgpt", LONG_UID)
    parts = list((note.parent / note.stem).glob("part-*.md"))
    assert 3 <= len(parts) <= 12
    assert all(len(p.read_bytes()) <= 100 * 1024 for p in (note, *parts))
    assert verify_archive(env.paths) == []


def test_census_cli_agrees_with_the_build_and_search_cli_filters_by_author(
    small, monkeypatch, capsys
) -> None:
    env, _ = small
    code, out, _ = _cce(
        monkeypatch, capsys, "archive", "census", "--root", str(env.paths.root), *SMALL
    )
    assert code == 0 and json.loads(out)["problems"] == []
    code, out, _ = _cce(
        monkeypatch,
        capsys,
        "archive",
        "search",
        "stress ask 0007",
        "--author",
        "human",
        "--json",
        *env.common,
    )
    assert code == 0 and [hit["uid"] for hit in json.loads(out)] == ["chatgpt-fixture-stress-0007"]
    code, out, _ = _cce(
        monkeypatch,
        capsys,
        "archive",
        "turns",
        "--author",
        "human",
        "--provider",
        "chatgpt",
        "--month",
        "202510",
        "--limit",
        "3",
        "--json",
        *env.common,
    )
    rows = json.loads(out)
    assert code == 0 and len(rows) == 3 and {row["author"] for row in rows} == {HUMAN}
