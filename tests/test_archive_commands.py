from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from conversation_corpus_engine import archive_commands, corpus_store
from conversation_corpus_engine.archive_commands import verify_archive
from conversation_corpus_engine.archive_ingest import ArchivePaths
from conversation_corpus_engine.archive_turns import build_turn_store
from conversation_corpus_engine.cli import main
from conversation_corpus_engine.corpus_store import CorpusReadAuthorization, CorpusStoreError


def _cce(monkeypatch, capsys, *argv: str) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "argv", ["cce", *argv])
    code = 0
    try:
        main()
    except SystemExit as exit_:
        code = int(exit_.code or 0)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def cli_env(archive_env, monkeypatch, capsys):
    # archive_env already registered the store; the CLI uses the same project/store pair.
    common = [
        "--project-root",
        str(archive_env.project),
        "--corpus-store-root",
        str(archive_env.store),
    ]
    code, out, err = _cce(
        monkeypatch, capsys, "archive", "build", "--export-root", str(archive_env.export), *common
    )
    return archive_env, common, code, out, err


def test_build_runs_every_stage_and_verifies(cli_env) -> None:
    env, _, code, out, err = cli_env
    payload = json.loads(out)
    assert code == 0 and payload["ok"] is True and payload["problems"] == []
    assert set(payload["stages"]) == {"ingest", "analyze", "vault", "database", "graph", "site"}
    assert payload["stages"]["ingest"]["totals"]["conversations"] == 24
    assert payload["stages"]["site"]["pages"] == 24
    for stage in ("ingest", "analyze", "vault", "database", "graph", "site"):
        assert f"[archive] {stage} done" in err
    assert verify_archive(env.paths) == []


def test_verify_search_and_titles_commands(cli_env, monkeypatch, capsys) -> None:
    _, common, _, _, _ = cli_env
    code, out, _ = _cce(monkeypatch, capsys, "archive", "verify", *common)
    assert code == 0 and json.loads(out) == {"ok": True, "problems": []}
    code, out, _ = _cce(monkeypatch, capsys, "archive", "search", "kubernetes", "--json", *common)
    hits = json.loads(out)
    assert code == 0 and {"chatgpt", "claude"} <= {h["provider"] for h in hits}
    code, out, _ = _cce(
        monkeypatch, capsys, "archive", "search", "sourdough", "--provider", "claude", *common
    )
    assert code == 0 and "claude" in out and out.strip().endswith("conversation(s)")
    code, out, _ = _cce(
        monkeypatch, capsys, "archive", "search", "rollout", "--titles", "--json", *common
    )
    assert [h["uid"] for h in json.loads(out)] == ["chatgpt-aaaa1111"]


def test_verify_detects_tampering_across_surfaces(cli_env, monkeypatch, capsys) -> None:
    env, common, _, _, _ = cli_env
    next(env.paths.vault.rglob("conversation-chatgpt-aaaa1111.md")).unlink()
    next(env.paths.site_pages.rglob("conversation-grok-*.html")).unlink()
    code, out, _ = _cce(monkeypatch, capsys, "archive", "verify", *common)
    problems = json.loads(out)["problems"]
    assert code == 1
    assert any("missing generated file" in p for p in problems)
    assert any("conversation page" in p for p in problems)


def test_verify_before_any_build_reports_missing_manifest(archive_env) -> None:
    assert "No archive manifest" in verify_archive(ArchivePaths(archive_env.paths.root))[0]


def test_single_stage_commands_and_provider_filter(archive_env, monkeypatch, capsys) -> None:
    common = [
        "--project-root",
        str(archive_env.project),
        "--corpus-store-root",
        str(archive_env.store),
    ]
    code, out, _ = _cce(
        monkeypatch,
        capsys,
        "archive",
        "ingest",
        "--export-root",
        str(archive_env.export),
        "--provider",
        "grok",
        "--provider",
        "perplexity",
        *common,
    )
    stages = json.loads(out)["stages"]["ingest"]
    assert code == 0 and set(stages["providers"]) == {"grok", "perplexity"}
    assert stages["totals"]["conversations"] == 8
    for stage in ("analyze", "vault", "database", "graph", "site"):
        code, out, _ = _cce(monkeypatch, capsys, "archive", stage, *common)
        assert code == 0, stage
    code, out, _ = _cce(monkeypatch, capsys, "archive", "verify", *common)
    assert code == 0, out


def test_failed_provider_makes_build_exit_nonzero(archive_env, monkeypatch, capsys) -> None:
    (archive_env.export / "perplexity" / "conversations-20250101.json").write_text(
        "{", encoding="utf-8"
    )
    code, out, _ = _cce(
        monkeypatch,
        capsys,
        "archive",
        "build",
        "--export-root",
        str(archive_env.export),
        "--project-root",
        str(archive_env.project),
        "--corpus-store-root",
        str(archive_env.store),
    )
    payload = json.loads(out)
    assert code == 1 and payload["ok"] is False
    assert any("perplexity failed" in p for p in payload["problems"])
    assert payload["stages"]["ingest"]["providers"]["chatgpt"]["status"] == "ok"


def test_unregistered_store_is_refused_with_guidance(tmp_path: Path, monkeypatch, capsys) -> None:
    store = tmp_path / "store"
    store.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    code, _, err = _cce(
        monkeypatch,
        capsys,
        "archive",
        "verify",
        "--project-root",
        str(project),
        "--corpus-store-root",
        str(store),
    )
    assert code == 2 and "corpus-store register" in err


# --- read-only custody -------------------------------------------------------------------------


def _watch_custody(monkeypatch) -> dict[str, list]:
    """Record how each archive command attached and which whole-store walks it paid for."""
    seen: dict[str, list] = {"read_only": [], "walks": []}
    real_open_context = archive_commands.open_context

    def open_context(**kwargs):
        seen["read_only"].append(kwargs.get("read_only", False))
        return real_open_context(**kwargs)

    monkeypatch.setattr(archive_commands, "open_context", open_context)
    for name in ("_find_nested_git_metadata", "_find_symlink"):
        original = getattr(corpus_store, name)

        def spy(*args, _name=name, _original=original, **kwargs):
            seen["walks"].append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(corpus_store, name, spy)
    return seen


@pytest.mark.parametrize("action", ["search", "turns", "serve"])
def test_read_only_actions_attach_without_the_whole_store_walks(
    cli_env, monkeypatch, capsys, action: str
) -> None:
    _, common, _, _, _ = cli_env
    served: list[ArchivePaths] = []
    monkeypatch.setattr(
        archive_commands, "serve", lambda paths, port: served.append(paths)
    )  # serving would block
    seen = _watch_custody(monkeypatch)
    argv = {"search": ["search", "kubernetes", "--json"], "turns": ["turns"], "serve": ["serve"]}

    code, _, _ = _cce(monkeypatch, capsys, "archive", *argv[action], *common)

    assert code == 0
    assert seen["read_only"] == [True]
    assert seen["walks"] == []
    assert bool(served) == (action == "serve")


@pytest.mark.parametrize(
    "action", ["ingest", "build", "analyze", "vault", "database", "graph", "site", "verify"]
)
def test_every_other_action_keeps_the_write_custody_walks(
    cli_env, monkeypatch, capsys, action: str
) -> None:
    env, common, _, _, _ = cli_env
    seen = _watch_custody(monkeypatch)
    extra = ["--export-root", str(env.export)] if action in {"ingest", "build"} else []

    code, _, _ = _cce(monkeypatch, capsys, "archive", action, *extra, *common)

    assert code == 0
    assert seen["read_only"] == [False]
    assert sorted(set(seen["walks"])) == ["_find_nested_git_metadata", "_find_symlink"]


def test_a_read_only_context_cannot_run_a_stage(cli_env) -> None:
    env, _, _, _, _ = cli_env
    context = archive_commands.open_context(
        project_root=env.project,
        corpus_store_root=env.store,
        archive_root=None,
        read_only=True,
    )

    assert isinstance(context.authorization, CorpusReadAuthorization)
    with pytest.raises(CorpusStoreError, match="write custody"):
        context.write_authorization()
    with pytest.raises(CorpusStoreError, match="write custody"):
        archive_commands.run_stages(context, ("analyze",))
    with pytest.raises(CorpusStoreError, match="write custody"):
        build_turn_store(context.paths, context.authorization)


def test_a_read_only_context_takes_no_source_roots(cli_env) -> None:
    env, _, _, _, _ = cli_env
    with pytest.raises(ValueError, match="no source roots"):
        archive_commands.open_context(
            project_root=env.project,
            corpus_store_root=env.store,
            archive_root=None,
            export_root=env.export,
            read_only=True,
        )


def test_a_missing_store_says_so_instead_of_asking_to_register(
    tmp_path: Path, archive_env, monkeypatch, capsys
) -> None:
    gone = tmp_path / "unmounted-volume"
    for action in ("search", "verify"):
        argv = ["archive", action, *(["kubernetes"] if action == "search" else [])]
        code, _, err = _cce(
            monkeypatch,
            capsys,
            *argv,
            "--project-root",
            str(archive_env.project),
            "--corpus-store-root",
            str(gone),
        )
        assert code == 2, action
        assert "archive custody refused" in err and "does not exist" in err, action
        assert "unmounted" in err and "volume is mounted" in err, action
        assert "corpus-store register" not in err, action


def test_a_store_with_no_archive_yet_is_named_on_a_read(archive_env, monkeypatch, capsys) -> None:
    code, _, err = _cce(
        monkeypatch,
        capsys,
        "archive",
        "search",
        "kubernetes",
        "--project-root",
        str(archive_env.project),
        "--corpus-store-root",
        str(archive_env.store),
    )
    assert code == 2
    assert "archive custody refused" in err and "Archive root does not exist" in err
    assert "corpus-store register" not in err


def test_an_unregistered_store_is_refused_on_a_read_with_guidance(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    store = tmp_path / "store"
    (store / "archive-all").mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    code, _, err = _cce(
        monkeypatch,
        capsys,
        "archive",
        "search",
        "kubernetes",
        "--project-root",
        str(project),
        "--corpus-store-root",
        str(store),
    )
    assert code == 2 and "archive custody refused" in err and "corpus-store register" in err


def test_bad_source_override_is_rejected(archive_env, monkeypatch, capsys) -> None:
    with pytest.raises(ValueError, match="PROVIDER=PATH"):
        _cce(
            monkeypatch,
            capsys,
            "archive",
            "ingest",
            "--export-root",
            str(archive_env.export),
            "--source",
            "nonsense",
            "--project-root",
            str(archive_env.project),
            "--corpus-store-root",
            str(archive_env.store),
        )
