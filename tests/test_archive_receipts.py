"""Receipts for how an archive was built (issue #104): merge protection, failed rebuilds and the
inputs the manifest names. Every fixture is synthetic."""

from __future__ import annotations

from pathlib import Path

import pytest
from archive_support import copilot_small_page

from conversation_corpus_engine.archive_ingest import ingest


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
