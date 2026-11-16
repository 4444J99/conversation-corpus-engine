"""A library of well over a thousand long records stays inside every vault cap (synthetic data)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from vault_support import sentinel_lines, synthetic_library

from conversation_corpus_engine.library_export import export_library, verify_export
from conversation_corpus_engine.library_store import Library
from conversation_corpus_engine.library_vault_gate import VaultCaps, measure_vault

KIB = 1024
RECORDS = 1500
LONG_EVERY = 62  # every 62nd record is far past the 64 KiB hard cap on its own
LONG_LINES = 3600
SHORT_LINES = 280


def lines_for(number: int) -> int:
    return LONG_LINES if number % LONG_EVERY == 0 else SHORT_LINES


@pytest.fixture(scope="module")
def library(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Library]:
    with synthetic_library(tmp_path_factory.mktemp("scale"), RECORDS, lines=lines_for) as scaled:
        yield scaled


def test_the_fixture_is_a_big_library_with_records_past_the_hard_cap(library: Library) -> None:
    status = library.status()
    assert status["records"] == RECORDS and status["repeated_blocks"] >= 1
    longest = max(len(library.record(row["uid"])["messages"][0]["text"]) for row in library.rows())
    assert longest > 64 * KIB


def test_none_writes_no_vault_for_the_whole_library(library: Library, tmp_path: Path) -> None:
    result = export_library(library, tmp_path / "export", vault_profile="none")
    assert result["records"] == RECORDS and result["vault"] is None
    assert not (tmp_path / "export" / "vault").exists()
    assert verify_export(tmp_path / "export") == []


def test_cards_fit_every_cap_for_the_whole_library(library: Library, tmp_path: Path) -> None:
    destination = tmp_path / "export"
    result = export_library(library, destination, vault_profile="cards")
    vault = destination / "vault"
    gate = measure_vault(vault)
    assert gate["ok"] and gate["clean"], gate["findings"]
    assert result["cards"] == RECORDS and result["truncated"] is False
    assert gate["notes"] < 10_000 and gate["bytes"] < 50 * 1024 * 1024
    assert gate["widest_folder"]["entries"] <= 200 and gate["max_hub_entries"] <= 100
    cards = list((vault / "cards").rglob("*.md"))
    assert len(cards) == RECORDS and max(card.stat().st_size for card in cards) <= 4 * KIB
    assert gate["max_note_bytes"] <= 16 * KIB  # hub pages are the largest notes here
    assert not (vault / "blocks").exists() and not (vault / "records").exists()
    assert verify_export(destination) == []


def test_cards_truncate_visibly_when_the_library_passes_the_note_budget(
    library: Library, tmp_path: Path
) -> None:
    # The same arithmetic as the default 10,000-note budget, at a tenth of the size.
    caps = VaultCaps(vault_notes_warn=1000, vault_notes_fail=2000)
    destination = tmp_path / "export"
    result = export_library(library, destination, vault_profile="cards", caps=caps)
    vault = destination / "vault"
    gate = measure_vault(vault, caps)
    assert gate["ok"] and gate["clean"], gate["findings"]
    assert gate["notes"] <= 1000 and result["truncated"] is True and result["limit_notes"] == 1000
    assert result["cards"] + result["omitted"] == RECORDS and result["omitted"] > 0
    assert gate["widest_folder"]["entries"] <= 200 and gate["max_hub_entries"] <= 100
    home = (vault / "Home.md").read_text(encoding="utf-8")
    assert f"{result['cards']} of {RECORDS} records" in home
    assert f"{result['omitted']} records have no card" in home
    rows = [json.loads(line) for line in (destination / "library-index.jsonl").open()]
    carded = {row["uid"] for row in rows if row["path"]}
    newest = sorted(rows, key=lambda row: row["updated"])[-result["cards"] :]
    assert carded == {row["uid"] for row in newest}
    assert verify_export(destination, caps=caps) == []


def test_full_fits_every_cap_for_the_whole_library(library: Library, tmp_path: Path) -> None:
    destination = tmp_path / "export"
    result = export_library(library, destination, vault_profile="full")
    vault = destination / "vault"
    gate = measure_vault(vault)
    assert gate["ok"] and gate["clean"], gate["findings"]
    assert gate["notes"] < 10_000 and gate["bytes"] < 50 * 1024 * 1024
    assert gate["max_note_bytes"] <= 16 * KIB
    assert gate["widest_folder"]["entries"] <= 200 and gate["max_hub_entries"] <= 100
    assert result["vault_caps"]["notes"] == gate["notes"]
    assert not (vault / "blocks").exists()
    parts = list((vault / "parts").rglob("*.md"))
    assert len(parts) > RECORDS // 2  # records past one part continue in the parts folder
    rows = [json.loads(line) for line in (destination / "library-index.jsonl").open()]
    assert len(rows) == RECORDS and all(row["path"] for row in rows)
    # Nothing is lost: every numbered line of the longest record is in the vault, in order.
    longest = next(row for row in rows if row["uid"].endswith("rec00000"))
    names = [Path(longest["path"]).name, *(p.name for p in parts if "rec00000-p" in p.name)]
    texts = [
        next(vault.rglob(name)).read_text(encoding="utf-8")
        for name in sorted(names, key=lambda n: (n != Path(longest["path"]).name, n))
    ]
    seen = [line for text in texts for line in text.splitlines() if line.startswith("R00000 line ")]
    assert seen == sentinel_lines("R00000", LONG_LINES).splitlines()
    assert verify_export(destination) == []
