from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from conversation_corpus_engine.archive_commands import verify_archive
from conversation_corpus_engine.archive_ingest import ArchivePaths
from conversation_corpus_engine.cli import main


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
    assert set(payload["stages"]) == {"ingest", "analyze", "vault", "kb", "graph", "site"}
    assert payload["stages"]["ingest"]["totals"]["conversations"] == 24
    assert payload["stages"]["site"]["pages"] == 24
    for stage in ("ingest", "analyze", "vault", "kb", "graph", "site"):
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
    next(env.paths.vault.rglob("chatgpt-aaaa1111.md")).unlink()
    next((env.paths.site / "c" / "grok").glob("*.html")).unlink()
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
    for stage in ("analyze", "vault", "kb", "graph", "site"):
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
