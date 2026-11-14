"""The vault size gate: one metadata walk that checks a vault against the standard's caps."""

from __future__ import annotations

from pathlib import Path

import pytest

from conversation_corpus_engine.archive_limits import Limits, take_census
from conversation_corpus_engine.library_vault_gate import (
    DEFAULT_CAPS,
    VaultCaps,
    describe_findings,
    hard_cap_problems,
    measure_planned,
    measure_vault,
)

KIB = 1024
MIB = 1024 * 1024


def note(root: Path, relative: str, size: int = 100) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def codes(report: dict, severity: str) -> set[str]:
    return {item["code"] for item in report["findings"] if item["severity"] == severity}


def test_the_default_caps_are_the_standards_numbers() -> None:
    assert VaultCaps() == DEFAULT_CAPS
    assert (DEFAULT_CAPS.note_target, DEFAULT_CAPS.note_warn, DEFAULT_CAPS.note_fail) == (
        4 * KIB,
        16 * KIB,
        64 * KIB,
    )
    assert (DEFAULT_CAPS.folder_warn, DEFAULT_CAPS.folder_fail) == (200, 1000)
    assert (DEFAULT_CAPS.vault_notes_warn, DEFAULT_CAPS.vault_notes_fail) == (10_000, 20_000)
    assert (DEFAULT_CAPS.vault_bytes_warn, DEFAULT_CAPS.vault_bytes_fail) == (
        50 * MIB,
        100 * MIB,
    )
    assert DEFAULT_CAPS.hub_entries == 100


def test_caps_that_contradict_each_other_are_refused() -> None:
    with pytest.raises(ValueError, match="note_warn"):
        VaultCaps(note_warn=8 * KIB, note_fail=4 * KIB)
    with pytest.raises(ValueError, match="folder"):
        VaultCaps(folder_warn=500, folder_fail=100)
    with pytest.raises(ValueError, match="vault_notes"):
        VaultCaps(vault_notes_warn=10, vault_notes_fail=5)
    with pytest.raises(ValueError, match="vault_bytes"):
        VaultCaps(vault_bytes_warn=10, vault_bytes_fail=5)
    with pytest.raises(ValueError, match="hub_entries"):
        VaultCaps(hub_entries=0)


def test_a_small_vault_reports_its_numbers_and_is_clean(tmp_path: Path) -> None:
    note(tmp_path, "Home.md", 300)
    note(tmp_path, "records/a.md", 1200)
    note(tmp_path, "records/b.md", 800)
    note(tmp_path, "records/c.md", 100)
    report = measure_vault(tmp_path)
    assert report["notes"] == 4
    assert report["bytes"] == 2400
    assert report["max_note_bytes"] == 1200
    assert report["max_note_path"] == "records/a.md"
    assert report["widest_folder"] == {"path": "records", "entries": 3}
    assert report["findings"] == []
    assert report["ok"] and report["clean"] and report["complete"]
    assert report["caps"]["hub_entries"] == 100
    assert hard_cap_problems(report) == []


def test_an_empty_vault_is_valid(tmp_path: Path) -> None:
    report = measure_vault(tmp_path)
    assert report["notes"] == 0 and report["bytes"] == 0 and report["max_note_bytes"] == 0
    assert report["ok"] and report["clean"]


def test_the_warn_tier_reports_its_own_true_worst_not_the_fail_cap(tmp_path: Path) -> None:
    note(tmp_path, "a.md", 17_000)
    note(tmp_path, "b.md", 20_000)
    note(tmp_path, "huge.md", 66_368)
    for number in range(700):
        note(tmp_path, f"warned/n{number:04d}.md", 10)
    for number in range(1001):
        note(tmp_path, f"failed/n{number:04d}.md", 10)
    report = measure_vault(tmp_path)
    by_kind = {(f["code"], f["severity"]): f for f in report["findings"]}
    assert by_kind["note_size", "warn"]["worst"] == 20_000
    assert by_kind["note_size", "warn"]["count"] == 2
    assert by_kind["note_size", "fail"]["worst"] == 66_368
    assert by_kind["folder_entries", "warn"]["worst"] == 700
    assert by_kind["folder_entries", "warn"]["count"] == 1
    assert by_kind["folder_entries", "fail"]["worst"] == 1001


def test_a_note_past_the_warn_size_warns_and_past_the_fail_size_fails(tmp_path: Path) -> None:
    note(tmp_path, "warn.md", 16 * KIB + 1)
    report = measure_vault(tmp_path)
    assert codes(report, "warn") == {"note_size"} and report["ok"] and not report["clean"]
    assert hard_cap_problems(report) == []
    note(tmp_path, "big.md", 64 * KIB + 1)
    report = measure_vault(tmp_path)
    assert codes(report, "fail") == {"note_size"} and not report["ok"]
    failing = next(f for f in report["findings"] if f["severity"] == "fail")
    assert failing["count"] == 1 and failing["worst"] == 64 * KIB + 1
    assert failing["limit"] == 64 * KIB and failing["examples"] == ["big.md"]
    # The note past the hard cap is counted once, as a failure, not again as a warning.
    assert codes(report, "warn") == {"note_size"}
    warned = next(f for f in report["findings"] if f["severity"] == "warn")
    assert warned["count"] == 1
    assert any("note_size" in line for line in hard_cap_problems(report))


def test_the_sizes_at_the_caps_are_allowed(tmp_path: Path) -> None:
    note(tmp_path, "exact-warn.md", 16 * KIB)
    note(tmp_path, "sub/exact-warn.md", 16 * KIB)
    assert measure_vault(tmp_path)["clean"]
    note(tmp_path, "exact-fail.md", 64 * KIB)
    report = measure_vault(tmp_path)
    assert report["ok"] and codes(report, "warn") == {"note_size"}


def test_a_folder_past_200_entries_warns_and_past_1000_fails(tmp_path: Path) -> None:
    for number in range(200):
        note(tmp_path, f"wide/n{number:04d}.md", 10)
    assert measure_vault(tmp_path)["clean"]
    note(tmp_path, "wide/n0200.md", 10)
    report = measure_vault(tmp_path)
    assert codes(report, "warn") == {"folder_entries"} and report["ok"]
    assert report["widest_folder"] == {"path": "wide", "entries": 201}
    for number in range(201, 1001):
        note(tmp_path, f"wide/n{number:04d}.md", 10)
    report = measure_vault(tmp_path)
    assert report["widest_folder"]["entries"] == 1001
    assert codes(report, "fail") == {"folder_entries"} and not report["ok"]
    folder = next(f for f in report["findings"] if f["severity"] == "fail")
    assert folder["worst"] == 1001 and folder["limit"] == 1000 and folder["examples"] == ["wide"]


def test_subfolders_count_as_entries_of_their_parent(tmp_path: Path) -> None:
    for number in range(201):
        (tmp_path / f"d{number:03d}").mkdir()
    report = measure_vault(tmp_path)
    assert report["widest_folder"]["entries"] == 201
    assert codes(report, "warn") == {"folder_entries"}


def test_note_count_and_total_bytes_have_warn_and_fail_tiers(tmp_path: Path) -> None:
    caps = VaultCaps(
        vault_notes_warn=3, vault_notes_fail=5, vault_bytes_warn=400, vault_bytes_fail=700
    )
    for number in range(3):
        note(tmp_path, f"a/n{number}.md", 100)
    report = measure_vault(tmp_path, caps)
    assert report["clean"]
    note(tmp_path, "a/n3.md", 100)
    note(tmp_path, "a/n4.md", 100)
    report = measure_vault(tmp_path, caps)
    assert codes(report, "warn") == {"vault_notes", "vault_bytes"} and report["ok"]
    for number in range(5, 8):
        note(tmp_path, f"a/n{number}.md", 100)
    report = measure_vault(tmp_path, caps)
    assert codes(report, "fail") == {"vault_notes", "vault_bytes"} and not report["ok"]
    problems = hard_cap_problems(report)
    assert any("vault_notes: 8 exceeds the hard cap of 5" in line for line in problems)
    assert any("vault_bytes: 800 exceeds the hard cap of 700" in line for line in problems)


def test_a_hub_page_with_more_than_100_entries_fails(tmp_path: Path) -> None:
    rows = "".join(f"- [Record {n}](../records/r{n}.md) - r{n}\n" for n in range(100))
    note(tmp_path, "hubs/ok.md").write_text("# Hub\n\n[Next](two.md)\n\n" + rows)
    assert measure_vault(tmp_path)["clean"]
    (tmp_path / "hubs" / "two.md").write_text(rows + "- [Extra](../records/x.md) - x\n")
    report = measure_vault(tmp_path)
    assert codes(report, "fail") == {"hub_entries"} and not report["ok"]
    finding = next(f for f in report["findings"] if f["code"] == "hub_entries")
    assert finding["worst"] == 101 and finding["limit"] == 100
    assert finding["examples"] == ["hubs/two.md"]
    assert report["max_hub_entries"] == 101 and report["hub_pages"] == 2


def test_hub_pages_below_nested_folders_are_counted(tmp_path: Path) -> None:
    rows = "".join(f"- [Record {n}](r{n}.md)\n" for n in range(101))
    note(tmp_path, "hubs/bucket-a/deep.md").write_text(rows)
    assert codes(measure_vault(tmp_path), "fail") == {"hub_entries"}


def test_symlinks_are_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    for number in range(300):
        note(outside, f"n{number}.md", 10)
    vault = tmp_path / "vault"
    vault.mkdir()
    note(vault, "Home.md", 10)
    (vault / "linked").symlink_to(outside)
    report = measure_vault(vault)
    assert report["notes"] == 1 and report["widest_folder"]["entries"] == 2


def test_a_walk_that_stops_at_its_deadline_never_claims_the_vault_is_fine(
    tmp_path: Path,
) -> None:
    note(tmp_path, "Home.md")
    report = measure_vault(tmp_path, deadline_seconds=-1)
    assert report["complete"] is False and report["ok"] is False and report["clean"] is False


def test_a_missing_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        measure_vault(tmp_path / "missing")


def test_the_census_reports_what_exceeds_the_target_without_changing_its_verdict(
    tmp_path: Path,
) -> None:
    note(tmp_path, "mid.md", 3000)
    note(tmp_path, "small.md", 100)
    limits = Limits(note_bytes=2048, note_ceiling=4096, folder_entries=10, folder_ceiling=20)
    report = take_census(tmp_path, limits)
    assert report["above_target"] == {"notes": 1, "folders": 0}
    assert report["ok"] and report["violations"]["counts"] == {"notes": 0, "data": 0, "folders": 0}


def planned_set() -> dict[str, str]:
    """A small vault that breaks or strains every kind of cap, as path -> text."""
    rows = "".join(f"- [Record {n}](../records/r{n}.md) - r{n}\n" for n in range(101))
    files = {
        "vault/Home.md": "# Home\n",
        "vault/records/ok.md": "x" * 3000,
        "vault/records/warned-a.md": "x" * 17_000,
        "vault/records/warned-b.md": "x" * 20_000,
        "vault/records/huge.md": "x" * 66_368,
        "vault/hubs/wide.md": "# Hub\n\n" + rows,
        "vault/hubs/narrow.md": "# Hub\n\n- [One](../records/ok.md) - ok\n",
    }
    files.update({f"vault/crowded/n{n:04d}.md": "x" for n in range(1001)})
    files.update({f"vault/warm/n{n:04d}.md": "x" for n in range(700)})
    return files


def comparable(report: dict) -> dict:
    keys = ("notes", "bytes", "max_note_bytes", "hub_pages", "max_hub_entries", "ok", "clean")
    shape = {key: report[key] for key in keys}
    shape["widest"] = report["widest_folder"]["entries"]
    shape["findings"] = sorted(
        (f["code"], f["severity"], f.get("count"), f["worst"], f["limit"])
        for f in report["findings"]
    )
    return shape


def test_a_planned_set_is_measured_exactly_like_the_same_files_on_disk(tmp_path: Path) -> None:
    files = planned_set()
    for relative, text in files.items():
        note(tmp_path, relative, 0).write_text(text, encoding="utf-8")
    planned = measure_planned(files, root="vault/")
    on_disk = measure_vault(tmp_path / "vault")
    assert comparable(planned) == comparable(on_disk)
    assert not planned["ok"]
    assert {(f["code"], f["severity"]) for f in planned["findings"]} == {
        ("note_size", "warn"),
        ("note_size", "fail"),
        ("folder_entries", "warn"),
        ("folder_entries", "fail"),
        ("hub_entries", "fail"),
    }
    assert planned["max_note_path"] == "records/huge.md"
    assert hard_cap_problems(planned)


def test_a_planned_set_within_the_caps_is_clean_and_reads_no_disk() -> None:
    files = {"Home.md": "# Home\n", "cards/a.md": "a", "hubs/h.md": "- [A](../cards/a.md)\n"}
    report = measure_planned(files)
    assert report["ok"] and report["clean"] and report["notes"] == 3
    assert report["widest_folder"] == {"path": ".", "entries": 3}
    assert measure_planned({})["ok"]


def test_planned_notes_and_bytes_have_the_vault_tiers() -> None:
    caps = VaultCaps(vault_notes_warn=2, vault_notes_fail=3, vault_bytes_warn=5, vault_bytes_fail=8)
    files = {f"n{n}.md": "xx" for n in range(4)}
    kinds = {(f["code"], f["severity"]) for f in measure_planned(files, caps)["findings"]}
    assert kinds == {("vault_notes", "fail"), ("vault_bytes", "warn")}
    assert {
        f["code"] for f in measure_planned({"a.md": "xxxx", "b.md": "xxxxx"}, caps)["findings"]
    } == {"vault_bytes"}


def test_findings_are_described_on_one_line() -> None:
    line = describe_findings(measure_planned(planned_set(), root="vault/"))
    assert "\n" not in line
    assert "fail note_size (66368 past 65536)" in line
    assert "warn note_size (20000 past 16384)" in line
