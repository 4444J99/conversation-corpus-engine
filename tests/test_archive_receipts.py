"""Receipts for how an archive was built (issue #104): merge protection, failed rebuilds and the
inputs the manifest names. Every fixture is synthetic."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
from archive_support import copilot_small_page, gpt_conversation

from conversation_corpus_engine.archive_ingest import ingest, iter_store, load_index
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


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(path): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _kwargs(env, **extra):
    return {
        "export_root": env.export,
        "paths": env.paths,
        "authorization": env.auth,
        **extra,
    }


# --- A2: a merge against a missing or wrong source ----------------------------------------------


def test_merge_against_a_missing_source_is_refused_without_allow_input_loss(
    archive_env,
) -> None:
    ingest(**_kwargs(archive_env, providers=["grok"]))
    before = _snapshot(archive_env.paths.root)
    (archive_env.export / "grok").rename(archive_env.export / "saved-grok")
    with pytest.raises(ValueError, match="grok.*absent.*allow-input-loss"):
        ingest(**_kwargs(archive_env, providers=["grok"], merge=True))
    assert _snapshot(archive_env.paths.root) == before


def test_merge_against_a_missing_source_proceeds_when_loss_is_allowed(archive_env) -> None:
    ingest(**_kwargs(archive_env, providers=["grok"]))
    (archive_env.export / "grok").rename(archive_env.export / "saved-grok")
    manifest = ingest(**_kwargs(archive_env, providers=["grok"], merge=True, allow_input_loss=True))
    assert manifest["providers"]["grok"]["status"] == "absent"
    assert manifest["build_options"]["allow_input_loss"] is True


# --- A1: live-capture protection for every live-capture provider ---------------------------------


def _copilot_captures(folder: Path) -> list[Path]:
    return [
        write_copilot_page_file(folder / "one.html", "id-one", "First", "First capture words."),
        write_copilot_page_file(folder / "two.html", "id-two", "Second", "Second capture words."),
    ]


def write_copilot_page_file(path: Path, identifier: str, title: str, paragraph: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(copilot_small_page(identifier, title, paragraph), encoding="utf-8")
    return path


def _text_captures(suffix: str):
    def build(folder: Path) -> list[Path]:
        folder.mkdir(parents=True, exist_ok=True)
        files = []
        for stem in ("one", "two"):
            path = folder / f"{stem}{suffix}"
            path.write_text(f"Synthetic capture {stem}.", encoding="utf-8")
            files.append(path)
        return files

    return build


@pytest.mark.parametrize(
    ("provider", "build"),
    [
        ("copilot", _copilot_captures),
        ("copilotreports", _text_captures(".txt")),
        ("localmemory", _text_captures(".md")),
    ],
)
def test_merge_protects_the_live_captures_of_every_live_capture_provider(
    archive_env, tmp_path: Path, provider: str, build
) -> None:
    folder = tmp_path / "captures"
    _, second = build(folder)
    kwargs = _kwargs(archive_env, providers=[provider], overrides={provider: folder})
    first = ingest(**kwargs)["providers"][provider]
    assert len(first["live_capture_uids"]) == 2
    before = _snapshot(archive_env.paths.root)
    second.unlink()
    with pytest.raises(ValueError, match="live capture.*allow-input-loss"):
        ingest(**kwargs, merge=True)
    assert _snapshot(archive_env.paths.root) == before
    manifest = ingest(**kwargs, merge=True, allow_input_loss=True)
    assert manifest["providers"][provider]["conversations"] == 1


# --- A3: shards kept after a failed rebuild stay visible and flagged -----------------------------


def _corrupt_grok(env) -> None:
    (next((env.export / "grok").rglob("prod-grok-backend.json"))).write_text("{", encoding="utf-8")


def test_shards_retained_after_a_failed_merge_stay_counted_and_flagged(archive_env) -> None:
    first = ingest(**_kwargs(archive_env, providers=["grok"]))
    grok_total = first["providers"]["grok"]["conversations"]
    assert grok_total > 0
    _corrupt_grok(archive_env)
    manifest = ingest(**_kwargs(archive_env, providers=["grok"], merge=True))
    assert manifest["providers"]["grok"]["status"] == "failed"
    assert manifest["totals"]["conversations"] == grok_total
    # The totals count the retained shards, so every reader of the store must see them too.
    assert "grok" in archive_env.paths.providers_present()
    assert sum(1 for _ in iter_store(archive_env.paths)) == grok_total
    assert len(load_index(archive_env.paths)) == grok_total


def test_a_failed_merge_build_flags_the_retained_provider_and_keeps_surfaces_consistent(
    archive_env, monkeypatch, capsys
) -> None:
    common = [
        "--project-root",
        str(archive_env.project),
        "--corpus-store-root",
        str(archive_env.store),
    ]
    export = str(archive_env.export)
    code, _, _ = _cce(monkeypatch, capsys, "archive", "build", "--export-root", export, *common)
    assert code == 0
    _corrupt_grok(archive_env)
    code, out, _ = _cce(
        monkeypatch,
        capsys,
        "archive",
        "build",
        "--export-root",
        export,
        "--provider",
        "grok",
        "--merge",
        *common,
    )
    payload = json.loads(out)
    assert code == 1 and payload["ok"] is False
    assert payload["problems"], "the failed provider is reported"
    assert all("provider grok failed" in problem for problem in payload["problems"]), payload[
        "problems"
    ]
    assert "retained from the previous build" in payload["problems"][0]
    assert payload["stages"]["ingest"]["providers"]["grok"]["retained"] is True


# --- A4: a failed rebuild without --merge keeps the previous receipt -----------------------------


def test_failed_rebuild_without_merge_keeps_the_previous_receipt(archive_env) -> None:
    original = ingest(**_kwargs(archive_env, providers=["grok"]))["providers"]["grok"]
    _corrupt_grok(archive_env)
    manifest = ingest(**_kwargs(archive_env, providers=["grok"]))
    failed = manifest["providers"]["grok"]
    assert failed["status"] == "failed"
    assert failed["layout"] == original["layout"]
    assert failed["conversations"] == original["conversations"]
    assert failed["source_files"] == original["source_files"]
    assert failed["failed_attempt"]["status"] == "failed"
    assert failed["failed_attempt"]["error"] == failed["error"]
    assert manifest["totals"]["conversations"] == original["conversations"]
    assert sum(1 for _ in iter_store(archive_env.paths)) == original["conversations"]


def test_failed_rebuild_keeps_the_receipt_of_a_provider_that_was_never_ok_plain(
    archive_env,
) -> None:
    # No previous receipt to keep: the failed result stands alone and nothing is invented.
    _corrupt_grok(archive_env)
    failed = ingest(**_kwargs(archive_env, providers=["grok"]))["providers"]["grok"]
    assert failed["status"] == "failed" and "failed_attempt" not in failed
    assert "layout" not in failed


def test_a_rebuild_ignores_an_unreadable_previous_manifest(archive_env) -> None:
    ingest(**_kwargs(archive_env, providers=["grok"]))
    archive_env.paths.manifest.write_text("{not json", encoding="utf-8")
    manifest = ingest(**_kwargs(archive_env, providers=["grok"]))
    assert manifest["providers"]["grok"]["status"] == "ok"


# --- A5: inputs that share a basename keep separate manifest entries -----------------------------


def test_inputs_that_share_a_basename_keep_separate_source_files(
    archive_env, tmp_path: Path
) -> None:
    first, second = tmp_path / "capture-a" / "live.json", tmp_path / "capture-b" / "live.json"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text(json.dumps([gpt_conversation("delta-a", "A", [("user", "first capture")])]))
    second.write_text(json.dumps([gpt_conversation("delta-b", "B", [("user", "second capture")])]))
    info = ingest(
        **_kwargs(
            archive_env, providers=["chatgpt"], options={"chatgpt_extra_files": [first, second]}
        )
    )["providers"]["chatgpt"]
    digests = {hashlib.sha256(path.read_bytes()).hexdigest() for path in (first, second)}
    assert len(digests) == 2
    assert digests <= set(info["source_files"].values())
    assert digests == {item["sha256"] for item in info["inputs"] if "live" in item["source_file"]}
    live_keys = [key for key in info["source_files"] if key.endswith("live.json")]
    assert len(live_keys) == 2 and len(set(live_keys)) == 2
    live_inputs = [item["source_file"] for item in info["inputs"] if "live" in item["source_file"]]
    assert len(set(live_inputs)) == 2
    # No key leaks a host path, and a file nobody collides with keeps its plain name.
    for key in [*info["source_files"], *(item["source_file"] for item in info["inputs"])]:
        assert not key.startswith("/") and str(tmp_path) not in key
    assert info["source_files"]["conversations-000.json"]
