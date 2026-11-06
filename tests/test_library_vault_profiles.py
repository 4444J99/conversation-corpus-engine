"""Vault profiles of the library export: none, cards and full (synthetic data only)."""

from __future__ import annotations

import csv
import json
import re
import zipfile
from pathlib import Path

import pytest
from vault_support import REPEATED, sentinel_lines, synthetic_library

from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.attributed_knowledge import ZETTEL_SCHEMA
from conversation_corpus_engine.library_export import export_library, verify_export
from conversation_corpus_engine.library_store import Library
from conversation_corpus_engine.library_vault import _href as vault_href
from conversation_corpus_engine.library_vault_gate import VaultCaps, measure_vault

KIB = 1024
NON_VAULT = {
    "agent-entry.md",
    "export-manifest.json",
    "graph.jsonl",
    "library-index.jsonl",
    "records.csv",
}


def manifest_of(destination: Path) -> dict:
    return json.loads((destination / "export-manifest.json").read_text(encoding="utf-8"))


def index_of(destination: Path) -> list[dict]:
    lines = (destination / "library-index.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def graph_of(destination: Path) -> list[dict]:
    lines = (destination / "graph.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def notes_under(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.md") if path.is_file())


def test_none_writes_no_vault_and_keeps_every_other_artifact(tmp_path: Path) -> None:
    with synthetic_library(tmp_path, 5) as library:
        destination = tmp_path / "export"
        result = export_library(library, destination, vault_profile="none")
        assert {path.name for path in destination.iterdir()} == NON_VAULT
        assert not (destination / "vault").exists()
        assert not (destination / "notion-import.zip").exists()
        assert result["profile"] == "none" and result["vault"] is None and result["notion"] is None
        manifest = manifest_of(destination)
        assert manifest["schema"] == "cce.library-export.v2" and manifest["profile"] == "none"
        assert manifest["records"] == 5 and "notion_members" not in manifest
        assert sorted(manifest["files"]) == sorted([*NON_VAULT])
        rows = index_of(destination)
        assert {row["uid"] for row in rows} == {row["uid"] for row in library.rows()}
        assert all(row["schema"] == "cce.library-index.v2" and row["path"] is None for row in rows)
        with (destination / "records.csv").open(newline="", encoding="utf-8") as stream:
            assert {row["path"] for row in csv.DictReader(stream)} == {""}
        nodes = [row for row in graph_of(destination) if row["type"] == "node"]
        assert {row["kind"] for row in nodes} >= {"record", "block"}
        assert all("path" not in row for row in nodes)
        entry = (destination / "agent-entry.md").read_text(encoding="utf-8")
        assert "--vault-profile none" in entry and "export-cards" in entry
        assert "no reading vault and no Notion ZIP" in entry and "vault/Home.md" not in entry
        assert len(entry.encode()) < 4096
        assert verify_export(destination) == []


def test_full_drops_block_notes_shards_folders_and_caps_hubs(tmp_path: Path) -> None:
    with synthetic_library(tmp_path, 450) as library:
        destination = tmp_path / "export"
        result = export_library(library, destination, vault_profile="full")
        vault = destination / "vault"
        assert not (vault / "blocks").exists()
        assert result["profile"] == "full" and result["vault"] == "vault/Home.md"
        gate = measure_vault(vault)
        assert gate["ok"] and gate["clean"], gate["findings"]
        assert gate["widest_folder"]["entries"] <= 200
        assert gate["max_hub_entries"] <= 100 and gate["hub_pages"] >= 10
        assert result["vault_caps"]["notes"] == gate["notes"]
        for note in notes_under(destination):
            text = note.read_text(encoding="utf-8")
            assert "/blocks/" not in text and "[Repeated passage:" not in text, note
        rows = index_of(destination)
        assert len(rows) == 450 and all(row["path"] for row in rows)
        first = destination / rows[0]["path"]
        assert first.is_file() and rows[0]["path"].startswith("vault/records/")
        assert rows[0]["path"] != f"vault/records/{rows[0]['uid']}.md"  # sharded, not flat
        assert REPEATED in first.read_text(encoding="utf-8")
        blocks = [row for row in graph_of(destination) if row.get("kind") == "block"]
        assert blocks and all("path" not in row for row in blocks)
        manifest = manifest_of(destination)
        assert manifest["profile"] == "full" and manifest["blocks"] == len(blocks)
        with zipfile.ZipFile(destination / "notion-import.zip") as archive:
            assert set(archive.namelist()) == set(manifest["notion_members"])
            assert "vault/Home.md" in archive.namelist()
        assert verify_export(destination) == []


@pytest.mark.parametrize(
    ("source", "target", "expected"),
    [
        ("vault/Home.md", "vault/hubs/a.md", "hubs/a.md"),
        ("vault/hubs/a.md", "vault/Home.md", "../Home.md"),
        ("vault/records/b1/x.md", "vault/hubs/b2/y.md", "../../hubs/b2/y.md"),
        ("vault/records/x.md", "agent-entry.md", "../../agent-entry.md"),
        ("Home.md", "cards/a.md", "cards/a.md"),
        ("cards/a.md", "Home.md", "../Home.md"),
        ("vault/a.md", "vault/a.md", "a.md"),
    ],
)
def test_note_links_are_relative_paths_between_two_places_below_one_root(
    source: str, target: str, expected: str
) -> None:
    assert vault_href(source, target) == expected


def test_a_small_full_vault_keeps_flat_record_paths(tmp_path: Path) -> None:
    with synthetic_library(tmp_path, 3) as library:
        destination = tmp_path / "export"
        export_library(library, destination, vault_profile="full")
        uids = sorted(row["uid"] for row in library.rows())
        assert [row["path"] for row in index_of(destination)] == [
            f"vault/records/{uid}.md" for uid in uids
        ]
        assert verify_export(destination) == []


def test_full_splits_a_long_record_into_linked_parts_without_losing_text(tmp_path: Path) -> None:
    with Library.create(tmp_path / "library.sqlite") as library:
        marker = "\N{SECTION SIGN}"
        library.put(
            Conversation(
                provider="chatgpt",
                source_id="long",
                title="A very long record",
                messages=[
                    Message(role="user", text=sentinel_lines("A", 3000)),
                    Message(role="assistant", text=sentinel_lines("B", 3000)),
                    Message(role="user", text=marker * 200_000),
                    Message(role="assistant", text="The last short message."),
                ],
                extra={"wing": "ai", "lane": "conversations"},
            )
        )
        library.put(
            Conversation(
                provider="chatgpt",
                source_id="short",
                title="A short record",
                messages=[Message(role="user", text="Short.")],
                extra={"wing": "ai", "lane": "conversations"},
            )
        )
        library.index_blocks()
        destination = tmp_path / "export"
        export_library(library, destination, vault_profile="full")
        paths = {row["uid"]: row["path"] for row in index_of(destination)}
        first = destination / paths["chatgpt-long"]
        siblings = sorted((destination / "vault" / "parts").rglob("chatgpt-long-p*.md"))
        parts = [first, *siblings]
        assert len(parts) >= 20
        sizes = [part.stat().st_size for part in parts]
        assert max(sizes) <= 16 * KIB, max(sizes)
        gate = measure_vault(destination / "vault")
        assert gate["ok"] and gate["clean"], gate["findings"]
        text = [part.read_text(encoding="utf-8") for part in parts]
        assert f"Part 1 of {len(parts)}" in text[0] and f"Part 2 of {len(parts)}" in text[1]
        assert re.search(r"\]\([^)]*chatgpt-long-p002\.md\)", text[0])
        assert re.search(r"\]\([^)]*chatgpt-long\.md\)", text[1])
        for tag, lines in (("A", 3000), ("B", 3000)):
            seen = [
                line
                for chunk in text
                for line in chunk.splitlines()
                if line.startswith(f"{tag} line ")
            ]
            assert seen == [f"{tag} line {n:05d} lorem ipsum dolor sit amet" for n in range(lines)]
        assert sum(chunk.count(marker) for chunk in text) == 200_000
        assert "The last short message." in text[-1]
        assert verify_export(destination) == []


def test_full_refuses_before_writing_when_the_note_count_would_pass_the_hard_cap(
    tmp_path: Path,
) -> None:
    caps = VaultCaps(vault_notes_warn=5, vault_notes_fail=10)
    with synthetic_library(tmp_path, 12) as library:
        destination = tmp_path / "export"
        with pytest.raises(ValueError, match=r"--vault-profile cards") as raised:
            export_library(library, destination, vault_profile="full", caps=caps)
        assert "full" in str(raised.value) and "hard cap" in str(raised.value)
        assert not destination.exists()


def test_full_refuses_before_writing_when_the_bytes_would_pass_the_hard_cap(
    tmp_path: Path,
) -> None:
    caps = VaultCaps(vault_bytes_warn=2000, vault_bytes_fail=4000)
    with synthetic_library(tmp_path, 4, lines=lambda number: 200) as library:
        destination = tmp_path / "export"
        with pytest.raises(ValueError, match=r"--vault-profile cards"):
            export_library(library, destination, vault_profile="full", caps=caps)
        assert not destination.exists()


def test_cards_write_one_small_card_per_record_and_never_copy_the_body(tmp_path: Path) -> None:
    with synthetic_library(tmp_path, 30, lines=lambda number: 800) as library:
        destination = tmp_path / "export"
        result = export_library(library, destination, vault_profile="cards")
        vault = destination / "vault"
        cards = notes_under(vault / "cards")
        assert len(cards) == 30 and result["cards"] == 30 and result["truncated"] is False
        assert not (vault / "records").exists() and not (vault / "blocks").exists()
        uid = next(iter(library.rows()))["uid"]
        card = (vault / "cards" / f"{uid}.md").read_text(encoding="utf-8")
        assert f"cce library read {uid}" in card and "line 00500" not in card
        assert max(path.stat().st_size for path in cards) <= 4 * KIB
        gate = measure_vault(vault)
        assert gate["ok"] and gate["clean"], gate["findings"]
        home = (vault / "Home.md").read_text(encoding="utf-8")
        assert "Every record has a card" in home
        manifest = manifest_of(destination)
        assert manifest["profile"] == "cards"
        assert manifest["cards"] == {
            "included": 30,
            "records": 30,
            "truncated": False,
            "limit_notes": 10000,
        }
        rows = {row["uid"]: row["path"] for row in index_of(destination)}
        assert all((destination / path).is_file() for path in rows.values())
        assert verify_export(destination) == []


def test_cards_shard_folders_and_paginate_hubs_for_a_large_set(tmp_path: Path) -> None:
    with synthetic_library(tmp_path, 450) as library:
        destination = tmp_path / "export"
        export_library(library, destination, vault_profile="cards")
        gate = measure_vault(destination / "vault")
        assert gate["ok"] and gate["clean"], gate["findings"]
        assert gate["widest_folder"]["entries"] <= 200 and gate["max_hub_entries"] <= 100
        assert verify_export(destination) == []


def test_cards_truncate_visibly_to_the_note_limit_keeping_the_newest(tmp_path: Path) -> None:
    with synthetic_library(tmp_path, 30) as library:
        destination = tmp_path / "export"
        result = export_library(library, destination, vault_profile="cards", limit_notes=14)
        vault = destination / "vault"
        included = result["cards"]
        assert 0 < included < 30 and result["truncated"] is True
        assert result["omitted"] == 30 - included
        gate = measure_vault(vault)
        assert gate["notes"] <= 14 and gate["ok"]
        rows = index_of(destination)
        carded = sorted(row["uid"] for row in rows if row["path"])
        newest = sorted(
            f"{row['uid']}" for row in sorted(rows, key=lambda r: r["updated"])[-included:]
        )
        assert carded == newest
        assert sum(1 for row in rows if row["path"] is None) == 30 - included
        home = (vault / "Home.md").read_text(encoding="utf-8")
        assert f"{included} of 30 records" in home and "cce library search" in home
        assert f"{30 - included} records have no card" in home
        manifest = manifest_of(destination)
        assert manifest["cards"]["truncated"] is True and manifest["cards"]["limit_notes"] == 14
        assert verify_export(destination) == []


def test_cards_refuse_a_limit_the_standard_forbids_or_cannot_hold(tmp_path: Path) -> None:
    with synthetic_library(tmp_path, 3) as library:
        with pytest.raises(ValueError, match="hard cap"):
            export_library(library, tmp_path / "a", vault_profile="cards", limit_notes=20001)
        with pytest.raises(ValueError, match="limit_notes"):
            export_library(library, tmp_path / "b", vault_profile="cards", limit_notes=1)
        with pytest.raises(ValueError, match="limit_notes"):
            export_library(library, tmp_path / "c", vault_profile="cards", limit_notes=True)
        assert not (tmp_path / "a").exists() and not (tmp_path / "b").exists()


def test_reviewed_zettels_need_the_legacy_layout(tmp_path: Path) -> None:
    envelope = {"schema": ZETTEL_SCHEMA, "zettels": []}
    with synthetic_library(tmp_path, 2) as library:
        for profile in ("none", "cards", "full"):
            with pytest.raises(ValueError, match="legacy layout") as raised:
                export_library(library, tmp_path / profile, zettels=envelope, vault_profile=profile)
            assert "--vault-profile" in str(raised.value)
            assert not (tmp_path / profile).exists()


def test_an_unknown_profile_is_refused_naming_the_choices(tmp_path: Path) -> None:
    with synthetic_library(tmp_path, 2) as library:
        with pytest.raises(ValueError, match="none, cards, full") as raised:
            export_library(library, tmp_path / "export", vault_profile="bulk")
        assert "legacy" in str(raised.value)
        assert not (tmp_path / "export").exists()


@pytest.mark.parametrize("profile", ["none", "cards", "full"])
def test_every_profile_charges_its_writes_to_the_budget_callback(
    tmp_path: Path, profile: str
) -> None:
    charged = []

    def callback(**kwargs: int) -> None:
        charged.append(kwargs.get("bytes_written", 0))

    with synthetic_library(tmp_path, 6) as library:
        destination = tmp_path / "export"
        export_library(library, destination, check_callback=callback, vault_profile=profile)
        on_disk = sum(path.stat().st_size for path in destination.rglob("*") if path.is_file())
        assert sum(charged) >= on_disk > 0


def test_a_cards_export_that_is_tampered_with_fails_verification(tmp_path: Path) -> None:
    with synthetic_library(tmp_path, 8) as library:
        destination = tmp_path / "export"
        export_library(library, destination, vault_profile="cards")
        card = next(iter(notes_under(destination / "vault" / "cards")))
        with card.open("a", encoding="utf-8") as stream:
            stream.write("x" * (70 * KIB))
        problems = verify_export(destination)
        assert any("hard cap" in problem for problem in problems), problems


def test_a_missing_hub_page_fails_verification_as_a_broken_link(tmp_path: Path) -> None:
    with synthetic_library(tmp_path, 8) as library:
        destination = tmp_path / "export"
        export_library(library, destination, vault_profile="full")
        hub = next(iter(notes_under(destination / "vault" / "hubs")))
        hub.unlink()
        problems = verify_export(destination)
        assert any("missing" in problem for problem in problems), problems


def test_an_index_path_that_names_no_file_fails_verification(tmp_path: Path) -> None:
    with synthetic_library(tmp_path, 4) as library:
        destination = tmp_path / "export"
        export_library(library, destination, vault_profile="none")
        index = destination / "library-index.jsonl"
        rows = index_of(destination)
        rows[0]["path"] = "vault/records/nothing-here.md"
        index.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
        problems = verify_export(destination)
        assert problems
