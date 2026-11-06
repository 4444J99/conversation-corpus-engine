"""The packaging scripts keep working on the new layouts and name --vault-profile when input is absent."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import pytest
from markdown_it import MarkdownIt
from vault_support import sentinel_lines, synthetic_library

from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.library_export import export_library, verify_export
from conversation_corpus_engine.library_store import Library

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
LINK = re.compile(r"(?<!!)\[[^\]\n]*\]\(([^)\n]*)\)")


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
        obsidian.package(
            tmp_path / "export" / "vault", tmp_path / "derived", source_generation="test-generation"
        )
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
            "--source-generation",
            "test-generation",
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
        source_generation="test-generation",
        max_seconds=60,
    )
    assert result["source_unchanged"] is True
    assert result["split_notes"] == 0  # no note is near the packager's own 256 KiB limit
    assert (tmp_path / "derived" / "Home.md").is_file()
    assert (tmp_path / "derived" / "agent-entry.md").is_file()


@pytest.mark.parametrize("profile", ["cards", "full"])
def test_the_obsidian_packager_rebases_the_agent_entry_and_names_the_generation(
    obsidian: ModuleType, tmp_path: Path, profile: str
) -> None:
    with synthetic_library(tmp_path, 12) as library:
        export_library(library, tmp_path / "export", vault_profile=profile)
    assert "(vault/Home.md)" in (tmp_path / "export" / "agent-entry.md").read_text()
    obsidian.package(
        tmp_path / "export" / "vault",
        tmp_path / "derived",
        agent_entry=tmp_path / "export" / "agent-entry.md",
        source_generation="synthetic-generation",
        max_seconds=60,
    )
    entry = (tmp_path / "derived" / "agent-entry.md").read_text()
    targets = [target for target in LINK.findall(entry) if "://" not in target]
    assert "vault/Home.md" not in entry and "(Home.md)" in entry
    assert all((tmp_path / "derived" / target.split("#")[0]).is_file() for target in targets)
    manifest = json.loads((tmp_path / "derived" / ".cce-package" / "manifest.json").read_text())
    assert manifest["source_generation"] == "synthetic-generation"
    assert obsidian.verify_package(tmp_path / "derived")["status"] == "verified"


def test_the_obsidian_packager_accepts_a_fence_that_crosses_a_full_profile_part(
    obsidian: ModuleType, tmp_path: Path
) -> None:
    body = "```python\n" + sentinel_lines("F", 900) + "\n```\nafter"
    with Library.create(tmp_path / "library.sqlite") as library:
        library.put(
            Conversation(
                provider="chatgpt",
                source_id="fenced",
                title="A record with a long fenced block",
                messages=[Message(role="user", text=body, id="m0")],
                extra={"wing": "ai", "lane": "conversations"},
            )
        )
        library.index_blocks()
        export_library(library, tmp_path / "export", vault_profile="full")
    assert len(list((tmp_path / "export" / "vault" / "parts").glob("*.md"))) > 1
    result = obsidian.package(
        tmp_path / "export" / "vault",
        tmp_path / "derived",
        source_generation="synthetic-generation",
        max_seconds=60,
    )
    assert result["status"] == "verified"
    assert obsidian.verify_package(tmp_path / "derived")["status"] == "verified"


@pytest.mark.parametrize(
    ("head", "indent"),
    [("- item", "  "), ("1. item", "   ")],
    # The block renderer re-fences anything indented four columns or more as literal code, so
    # these are the list shapes whose fence reaches the splitter as a fence.
    ids=["bullet-item", "ordered-item"],
)
def test_the_obsidian_packager_accepts_a_list_item_fence_cut_by_a_full_profile_part(
    obsidian: ModuleType, tmp_path: Path, head: str, indent: str
) -> None:
    code = "\n".join(indent + line for line in sentinel_lines("F", 900).splitlines())
    body = f"{head}\n{indent}```sh\n{code}\n{indent}```\nafter\n"
    with Library.create(tmp_path / "library.sqlite") as library:
        library.put(
            Conversation(
                provider="chatgpt",
                source_id="listed",
                title="A record with a long fenced block in a list",
                messages=[Message(role="user", text=body, id="m0")],
                extra={"wing": "ai", "lane": "conversations"},
            )
        )
        library.index_blocks()
        export_library(library, tmp_path / "export", vault_profile="full")
    assert verify_export(tmp_path / "export") == []
    parser = MarkdownIt("commonmark", {"html": True})
    notes = sorted(
        [
            *(tmp_path / "export/vault/records").glob("*.md"),
            *(tmp_path / "export/vault/parts").glob("*.md"),
        ]
    )
    assert len(notes) > 2
    for note in notes:
        text = note.read_text(encoding="utf-8")
        written = LINK.findall(text)
        rendered = [
            child.attrGet("href")
            for token in parser.parse(text)
            for child in token.children or []
            if child.type == "link_open"
        ]
        assert sorted(written) == sorted(rendered), f"{note.name}: a link renders as code"
    result = obsidian.package(
        tmp_path / "export" / "vault",
        tmp_path / "derived",
        source_generation="synthetic-generation",
        max_seconds=60,
    )
    assert result["status"] == "verified"
    assert obsidian.verify_package(tmp_path / "derived")["status"] == "verified"


@pytest.mark.parametrize(
    ("opener", "lead", "closer"),
    [("> ```sh", "> ", "> ```"), ("- ```sh", "  ", "  ```")],
    ids=["blockquote", "list-marker-line"],
)
def test_the_obsidian_packager_accepts_a_fence_the_splitter_does_not_track(
    obsidian: ModuleType, tmp_path: Path, opener: str, lead: str, closer: str
) -> None:
    # The splitter does not track these shapes (see its tests); the block renderer re-fences them
    # as literal code first, so a long one still splits into notes whose links render.
    code = "\n".join(lead + line for line in sentinel_lines("F", 900).splitlines())
    body = f"intro\n{opener}\n{code}\n{closer}\nafter\n"
    with Library.create(tmp_path / "library.sqlite") as library:
        library.put(
            Conversation(
                provider="chatgpt",
                source_id="quoted",
                title="A record with a long block the splitter does not track",
                messages=[Message(role="user", text=body, id="m0")],
                extra={"wing": "ai", "lane": "conversations"},
            )
        )
        library.index_blocks()
        export_library(library, tmp_path / "export", vault_profile="full")
    assert len(list((tmp_path / "export" / "vault" / "parts").glob("*.md"))) > 1
    result = obsidian.package(
        tmp_path / "export" / "vault",
        tmp_path / "derived",
        source_generation="synthetic-generation",
        max_seconds=60,
    )
    assert result["status"] == "verified"
    assert obsidian.verify_package(tmp_path / "derived")["status"] == "verified"
