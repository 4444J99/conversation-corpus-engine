"""The packaging scripts keep working on the new layouts and name --vault-profile when input is absent."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import pytest
from vault_support import synthetic_library

from conversation_corpus_engine.library_export import export_library, verify_export

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name: str, script: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / script)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def notion() -> ModuleType:
    return load("notion_batches_consumer", "package-notion-batches.py")


@pytest.fixture(scope="module")
def obsidian() -> ModuleType:
    return load("obsidian_packer_consumer", "package-obsidian-vault.py")


def test_the_notion_packager_names_the_profile_when_a_generation_has_no_zip(
    notion: ModuleType, tmp_path: Path
) -> None:
    with synthetic_library(tmp_path, 3) as library:
        export_library(library, tmp_path / "export", vault_profile="none")
    with pytest.raises(ValueError, match="--vault-profile") as raised:
        notion.package(tmp_path / "export" / "notion-import.zip", tmp_path / "batches")
    assert "cce library export --vault-profile cards" in str(raised.value)
    assert "rebuild" not in str(raised.value).lower()
    assert not (tmp_path / "batches").exists()


def test_the_notion_packager_exits_with_one_clean_line_and_code_two(
    notion: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with synthetic_library(tmp_path, 3) as library:
        export_library(library, tmp_path / "export", vault_profile="none")
    monkeypatch.setattr(os, "nice", lambda increment: 0)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "package-notion-batches.py",
            str(tmp_path / "export" / "notion-import.zip"),
            "--output",
            str(tmp_path / "batches"),
        ],
    )
    assert notion.main() == 2
    error = capsys.readouterr().err
    assert len(error.strip().splitlines()) == 1 and "Traceback" not in error
    assert "cce library export --vault-profile" in error


def test_the_obsidian_packager_names_the_profile_when_a_generation_has_no_vault(
    obsidian: ModuleType, tmp_path: Path
) -> None:
    with synthetic_library(tmp_path, 3) as library:
        export_library(library, tmp_path / "export", vault_profile="none")
    with pytest.raises(ValueError, match="--vault-profile") as raised:
        obsidian.package(tmp_path / "export" / "vault", tmp_path / "derived")
    assert "cce library export --vault-profile cards" in str(raised.value)
    assert "export-cards" in str(raised.value) and "rebuild" not in str(raised.value).lower()
    assert not (tmp_path / "derived").exists()


def test_the_obsidian_packager_exits_with_one_clean_line_and_code_two(
    obsidian: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with synthetic_library(tmp_path, 3) as library:
        export_library(library, tmp_path / "export", vault_profile="none")
    monkeypatch.setattr(os, "nice", lambda increment: 0)
    monkeypatch.setattr(os, "umask", lambda mask: mask)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "package-obsidian-vault.py",
            str(tmp_path / "export" / "vault"),
            "--output",
            str(tmp_path / "derived"),
        ],
    )
    assert obsidian.main() == 2
    error = capsys.readouterr().err
    assert len(error.strip().splitlines()) == 1 and "Traceback" not in error
    assert "cce library export --vault-profile" in error


@pytest.mark.parametrize("profile", ["cards", "full"])
def test_the_notion_packager_batches_the_zip_of_a_vault_profile(
    notion: ModuleType, tmp_path: Path, profile: str
) -> None:
    with synthetic_library(tmp_path, 40) as library:
        export_library(library, tmp_path / "export", vault_profile=profile)
    manifest = json.loads((tmp_path / "export" / "export-manifest.json").read_text())
    with zipfile.ZipFile(tmp_path / "export" / "notion-import.zip") as archive:
        members = len(archive.namelist())
    assert members == len(manifest["notion_members"])
    result = notion.package(
        tmp_path / "export" / "notion-import.zip",
        tmp_path / "batches",
        max_seconds=60,
    )
    assert result["complete"] is True and result["original_members"] == members


@pytest.mark.parametrize("profile", ["cards", "full"])
def test_the_obsidian_packager_accepts_the_vault_of_a_profile(
    obsidian: ModuleType, tmp_path: Path, profile: str
) -> None:
    with synthetic_library(tmp_path, 40) as library:
        export_library(library, tmp_path / "export", vault_profile=profile)
    assert verify_export(tmp_path / "export") == []
    result = obsidian.package(
        tmp_path / "export" / "vault",
        tmp_path / "derived",
        agent_entry=tmp_path / "export" / "agent-entry.md",
        max_seconds=60,
    )
    assert result["source_unchanged"] is True
    assert result["split_notes"] == 0  # no note is near the packager's own 256 KiB limit
    assert (tmp_path / "derived" / "Home.md").is_file()
    assert (tmp_path / "derived" / "agent-entry.md").is_file()
