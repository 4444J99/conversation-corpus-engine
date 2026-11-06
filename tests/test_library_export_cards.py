"""cce library export-cards: an on-demand card vault that never touches the library store."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
from test_library_build import setup_sources
from vault_support import synthetic_library

from conversation_corpus_engine import library_commands
from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.attributed_knowledge import ZETTEL_SCHEMA
from conversation_corpus_engine.cli import build_parser
from conversation_corpus_engine.library_cards import (
    CARDS_MANIFEST,
    CARDS_MARKER,
    CARDS_SCHEMA,
    export_cards,
)
from conversation_corpus_engine.library_store import Library
from conversation_corpus_engine.library_vault_gate import VaultCaps, measure_vault

LINK = re.compile(r"(?<!!)\[[^\]\n]*\]\(([^)\n]*)\)")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def files_under(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


def cards_export(library_path: Path, out: Path, **options):
    with Library(library_path) as library:
        return export_cards(library, out, **options)


def test_export_cards_writes_a_marked_vault_with_an_exact_manifest(tmp_path: Path) -> None:
    with synthetic_library(tmp_path, 12) as source:
        before = sha(source.path)
        out = tmp_path / "cards"
        result = cards_export(source.path, out, store_root=tmp_path / "store")
        assert sha(source.path) == before
    manifest = json.loads((out / CARDS_MANIFEST).read_text(encoding="utf-8"))
    assert manifest["schema"] == CARDS_SCHEMA == "cce.library-cards.v1"
    assert (out / CARDS_MARKER).is_file()
    listed = {entry["path"] for entry in manifest["files"]}
    assert files_under(out) == listed | {CARDS_MARKER, CARDS_MANIFEST}
    for entry in manifest["files"]:
        assert sha(out / entry["path"]) == entry["sha256"]
        assert (out / entry["path"]).stat().st_size == entry["bytes"]
    assert result["cards"] == manifest["cards"] == 12 and result["records"] == manifest["records"]
    assert result["truncated"] is False and result["omitted"] == 0
    assert {"Home.md"} <= listed and any(path.startswith("cards/") for path in listed)
    assert measure_vault(out)["clean"]
    assert str(tmp_path) not in (out / CARDS_MANIFEST).read_text(encoding="utf-8")
    for note in out.rglob("*.md"):
        for target in LINK.findall(note.read_text(encoding="utf-8")):
            assert (note.parent / target).resolve().is_file(), (note, target)


def test_export_cards_states_its_truncation(tmp_path: Path) -> None:
    with synthetic_library(tmp_path, 30) as source:
        out = tmp_path / "cards"
        result = cards_export(source.path, out, limit_notes=12)
    assert result["truncated"] is True and 0 < result["cards"] < 30
    home = (out / "Home.md").read_text(encoding="utf-8")
    assert f"{result['cards']} of 30 records" in home and "cce library search" in home
    assert measure_vault(out)["notes"] <= 12


@pytest.mark.parametrize("where", ["store", "inside-generation", "ancestor-of-store"])
def test_export_cards_refuses_to_write_in_the_library_store(tmp_path: Path, where: str) -> None:
    work = tmp_path / "work"
    work.mkdir()
    store = tmp_path / "store"
    generation = store / "library-all" / "generations" / "generation-x"
    generation.mkdir(parents=True)
    out = {
        "store": store,
        "inside-generation": generation / "cards",
        "ancestor-of-store": tmp_path,
    }[where]
    with synthetic_library(work, 3) as source, pytest.raises(ValueError, match="library store"):
        cards_export(source.path, out, store_root=store)
    assert not (out / CARDS_MARKER).exists()


def test_export_cards_refuses_a_generation_even_without_a_store_argument(tmp_path: Path) -> None:
    out = tmp_path / "library-all" / "generations" / "generation-x" / "cards"
    with (
        synthetic_library(tmp_path, 3) as source,
        pytest.raises(ValueError, match="library store"),
    ):
        cards_export(source.path, out)
    assert not out.exists()


def test_export_cards_refuses_a_non_empty_directory_without_the_marker(tmp_path: Path) -> None:
    out = tmp_path / "notes"
    out.mkdir()
    (out / "mine.md").write_text("my own note")
    with (
        synthetic_library(tmp_path, 3) as source,
        pytest.raises(FileExistsError, match="marker"),
    ):
        cards_export(source.path, out)
    assert [path.name for path in out.iterdir()] == ["mine.md"]
    assert (out / "mine.md").read_text() == "my own note"


def test_export_cards_accepts_an_empty_existing_directory_and_refuses_a_file(
    tmp_path: Path,
) -> None:
    (tmp_path / "empty").mkdir()
    (tmp_path / "file").write_text("x")
    with synthetic_library(tmp_path, 3) as source:
        assert cards_export(source.path, tmp_path / "empty")["cards"] == 3
        with pytest.raises(FileExistsError):
            cards_export(source.path, tmp_path / "file")


def test_export_cards_refuses_a_symlinked_destination(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    with synthetic_library(tmp_path, 3) as source, pytest.raises(ValueError, match="symlink"):
        cards_export(source.path, tmp_path / "link")
    assert list(real.iterdir()) == []


def test_a_rerun_replaces_only_what_the_previous_manifest_lists(tmp_path: Path) -> None:
    with synthetic_library(tmp_path, 450) as source:
        out = tmp_path / "cards"
        first = cards_export(source.path, out)
        first_manifest = json.loads((out / CARDS_MANIFEST).read_text(encoding="utf-8"))
        first_listed = {entry["path"] for entry in first_manifest["files"]}
        (out / "My notes.md").write_text("annotations")
        (out / ".obsidian").mkdir()
        (out / ".obsidian" / "app.json").write_text("{}")
        second = cards_export(source.path, out, limit_notes=120)
    assert first["cards"] == 450 and second["cards"] < 450
    after = files_under(out)
    manifest = json.loads((out / CARDS_MANIFEST).read_text(encoding="utf-8"))
    listed = {entry["path"] for entry in manifest["files"]}
    assert after == listed | {CARDS_MARKER, CARDS_MANIFEST, "My notes.md", ".obsidian/app.json"}
    assert (out / "My notes.md").read_text() == "annotations"
    assert (out / ".obsidian" / "app.json").read_text() == "{}"
    stale = first_listed - listed
    assert stale and second["removed"] == len(stale)
    for folder in out.rglob("*"):
        if folder.is_dir():
            assert any(folder.iterdir()), f"empty folder left behind: {folder}"
    assert measure_vault(out)["ok"]


def test_a_rerun_refuses_to_overwrite_or_delete_an_edited_generated_file(tmp_path: Path) -> None:
    with synthetic_library(tmp_path, 8) as source:
        out = tmp_path / "cards"
        cards_export(source.path, out)
        card = next(iter(sorted((out / "cards").rglob("*.md"))))
        card.write_text(card.read_text(encoding="utf-8") + "\nMy annotation.\n", encoding="utf-8")
        with pytest.raises(ValueError, match="edited"):
            cards_export(source.path, out)
        with pytest.raises(ValueError, match="edited"):
            cards_export(source.path, out, limit_notes=8)
    assert "My annotation." in card.read_text(encoding="utf-8")
    assert len(list((out / "cards").rglob("*.md"))) == 8


def test_a_user_file_at_a_new_card_path_is_never_overwritten(tmp_path: Path) -> None:
    with synthetic_library(tmp_path, 30) as source:
        out = tmp_path / "cards"
        cards_export(source.path, out, limit_notes=12)
        first = files_under(out)
        # Exporting with no limit would add cards; the user already has a note at one of them.
        cards_export(source.path, tmp_path / "probe")
        added = sorted(
            path
            for path in files_under(tmp_path / "probe")
            if path.startswith("cards/") and path not in first
        )
        assert added
        target = out / added[0]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("a note the user wrote here")
        with pytest.raises(FileExistsError, match="not written by this tool"):
            cards_export(source.path, out)
    assert target.read_text() == "a note the user wrote here"


def test_a_crashed_first_run_is_adopted_when_the_files_are_identical(tmp_path: Path) -> None:
    with synthetic_library(tmp_path, 10) as source:
        out = tmp_path / "cards"
        cards_export(source.path, out)
        (out / CARDS_MANIFEST).unlink()
        again = cards_export(source.path, out)
    assert again["adopted"] == again["notes"] and again["written"] == 0
    assert (out / CARDS_MANIFEST).is_file()


def test_a_manifest_that_lists_an_unsafe_path_deletes_nothing(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("keep me")
    with synthetic_library(tmp_path, 5) as source:
        out = tmp_path / "cards"
        cards_export(source.path, out)
        manifest = json.loads((out / CARDS_MANIFEST).read_text(encoding="utf-8"))
        manifest["files"].append({"path": "../outside.md", "bytes": 7, "sha256": sha(outside)})
        (out / CARDS_MANIFEST).write_text(json.dumps(manifest))
        with pytest.raises(ValueError, match="unsafe"):
            cards_export(source.path, out, limit_notes=8)
    assert outside.read_text() == "keep me"


def test_a_rerun_with_the_same_library_changes_nothing(tmp_path: Path) -> None:
    with synthetic_library(tmp_path, 10) as source:
        out = tmp_path / "cards"
        cards_export(source.path, out)
        before = {path: sha(out / path) for path in files_under(out)}
        again = cards_export(source.path, out)
        after = {path: sha(out / path) for path in files_under(out)}
    assert before == after and again["written"] == 0 and again["removed"] == 0


def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, library: Library, *argv: str) -> int:
    monkeypatch.setattr(
        library_commands, "open_library", lambda store: (tmp_path, Library(library.path))
    )
    return library_commands.handle_library(build_parser().parse_args(["library", *argv]))


def test_the_export_cards_command_prints_the_result_and_honors_limit_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = tmp_path / "store"
    store.mkdir()
    with synthetic_library(tmp_path, 30) as library:
        out = tmp_path / "cards"
        code = cli(
            tmp_path,
            monkeypatch,
            library,
            "export-cards",
            "--store-root",
            str(store),
            "--out",
            str(out),
            "--limit-notes",
            "12",
        )
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["truncated"] is True and result["limit_notes"] == 12
    assert (out / CARDS_MARKER).is_file()


def test_the_export_cards_command_refuses_the_store_with_a_clear_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = tmp_path / "store"
    store.mkdir()
    with synthetic_library(tmp_path, 3) as library:
        code = cli(
            tmp_path,
            monkeypatch,
            library,
            "export-cards",
            "--store-root",
            str(store),
            "--out",
            str(store / "cards"),
        )
    assert code == 2
    assert "library store" in capsys.readouterr().err
    assert not (store / "cards").exists()


def test_build_takes_a_vault_profile_and_defaults_to_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project, store, manifest = setup_sources(tmp_path)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    base = ["library", "build", "--manifest", str(path), "--store-root", str(store)]
    base += ["--project-root", str(project)]
    assert library_commands.handle_library(build_parser().parse_args(base)) == 0
    default = json.loads(capsys.readouterr().out)
    assert default["vault_profile"] == "none"
    assert not (Path(default["generation"]) / "vault").exists()
    args = build_parser().parse_args([*base, "--vault-profile", "cards"])
    assert library_commands.handle_library(args) == 0
    cards = json.loads(capsys.readouterr().out)
    assert cards["vault_profile"] == "cards"
    assert (Path(cards["generation"]) / "vault" / "cards").is_dir()
    with pytest.raises(SystemExit):
        build_parser().parse_args([*base, "--vault-profile", "bulk"])


def test_export_keeps_the_legacy_layout_unless_a_profile_is_chosen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with synthetic_library(tmp_path, 4) as library:
        store = str(tmp_path)
        assert (
            cli(
                tmp_path,
                monkeypatch,
                library,
                "export",
                "--store-root",
                store,
                "--out",
                str(tmp_path / "legacy"),
            )
            == 0
        )
        legacy = json.loads(capsys.readouterr().out)
        assert "profile" not in legacy and (tmp_path / "legacy" / "vault" / "blocks").is_dir()
        args = ["export", "--store-root", store, "--vault-profile", "full"]
        assert cli(tmp_path, monkeypatch, library, *args, "--out", str(tmp_path / "full")) == 0
        full = json.loads(capsys.readouterr().out)
        assert full["profile"] == "full" and not (tmp_path / "full" / "vault" / "blocks").exists()


def test_export_warns_when_the_legacy_layout_breaks_the_vault_caps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with synthetic_library(tmp_path, 260) as library:
        args = ["export", "--store-root", str(tmp_path), "--out", str(tmp_path / "wide")]
        assert cli(tmp_path, monkeypatch, library, *args) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["records"] == 260
    assert "warning" in captured.err and "--vault-profile" in captured.err
    assert "folder_entries" in captured.err


def test_export_refuses_reviewed_zettels_with_a_profile_and_names_the_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    reviewed = tmp_path / "reviewed.json"
    reviewed.write_text(json.dumps({"schema": ZETTEL_SCHEMA, "zettels": []}))
    with synthetic_library(tmp_path, 3) as library:
        args = ["export", "--store-root", str(tmp_path), "--vault-profile", "cards"]
        code = cli(
            tmp_path,
            monkeypatch,
            library,
            *args,
            "--zettels",
            str(reviewed),
            "--out",
            str(tmp_path / "x"),
        )
    assert code == 2 and "legacy layout" in capsys.readouterr().err


def snapshot(root: Path) -> dict[str, str]:
    return {path: sha(root / path) for path in files_under(root)}


def test_a_crowded_folder_of_the_persons_own_files_warns_and_the_vault_stays_consistent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with synthetic_library(tmp_path, 700, index=False) as source:
        out = tmp_path / "cards"
        cards_export(source.path, out)
        cards_export(source.path, out, limit_notes=500)
        bucket = next(path for path in sorted((out / "cards").iterdir()) if path.is_dir())
        for number in range(1100):
            (bucket / f"mine-{number:04d}.md").write_text("my note")
        capsys.readouterr()
        result = cards_export(source.path, out, limit_notes=400)
    # The cards the tool controls fit the caps, so the run completes; the crowding is the
    # person's, so it is reported, on stderr and in the result, and nothing is half-replaced.
    assert result["cards"] < 400 and result["generated_caps"]["ok"] is True
    assert result["vault_caps"]["ok"] is False
    assert result["warnings"] and "folder_entries" in result["warnings"][0]
    assert "warning" in capsys.readouterr().err
    manifest = json.loads((out / CARDS_MANIFEST).read_text(encoding="utf-8"))
    listed = {entry["path"] for entry in manifest["files"]}
    assert manifest["cards"] == result["cards"]
    on_disk = files_under(out)
    mine = {f"{bucket.relative_to(out).as_posix()}/mine-{n:04d}.md" for n in range(1100)}
    assert on_disk == listed | {CARDS_MARKER, CARDS_MANIFEST} | mine
    for entry in manifest["files"]:
        assert sha(out / entry["path"]) == entry["sha256"]
    assert all((out / path).read_text() == "my note" for path in mine)


def test_a_vault_that_breaks_a_hard_cap_is_refused_with_nothing_changed(tmp_path: Path) -> None:
    caps = VaultCaps(note_target=1024, note_warn=2048, note_fail=2048)
    with synthetic_library(tmp_path, 12) as source:
        out = tmp_path / "cards"
        cards_export(source.path, out)
        (out / "My notes.md").write_text("annotations")
        before = snapshot(out)
    with Library.create(tmp_path / "heavy.sqlite") as heavy:
        heavy.put(
            Conversation(
                provider="chatgpt",
                source_id="brackets",
                title="Escapes grow",
                messages=[Message(role="user", text="[" * 1000)],
                extra={"wing": "ai", "lane": "conversations"},
            )
        )
        with pytest.raises(ValueError, match="note_size"):
            export_cards(heavy, out, caps=caps)
    assert snapshot(out) == before
