"""Provider-month shards and the deterministic folder plan."""

from __future__ import annotations

import random
from collections import Counter
from pathlib import Path

import pytest

from conversation_corpus_engine import archive_layout as layout
from conversation_corpus_engine.archive_naming import validate_name
from conversation_corpus_engine.archive_shard import (
    UNDATED,
    ShardWriter,
    day_key,
    month_key,
    parse_shard_subject,
    part_folder,
    plan_conversation_folders,
    plan_flat_folders,
    shard_sort_key,
    shard_subject,
)


def test_month_and_day_keys_come_from_real_calendar_stamps_only() -> None:
    assert month_key("2025-06-14T09:00:00Z") == "202506"
    assert month_key("2025-06") == "202506"
    for bad in ("", "undated", "2025-13-01T00:00:00Z", "2025-00-10", "June 2025"):
        assert month_key(bad) == UNDATED
    assert day_key("2025-06-14T09:00:00Z") == "20250614"
    assert day_key("2025-06") == "" and day_key("") == ""


def test_shard_subjects_round_trip_and_sort_in_reading_order() -> None:
    subjects = ["202506", "202506-002", "202506-003", "202507", UNDATED, f"{UNDATED}-002"]
    assert [shard_subject(*parse_shard_subject(s)) for s in subjects] == subjects
    assert sorted(reversed(subjects), key=shard_sort_key) == subjects
    with pytest.raises(ValueError, match="not a shard subject"):
        parse_shard_subject("conversations")


def test_shard_file_names_are_valid_contract_names() -> None:
    for subject in ("202506", "202506-002", UNDATED):
        for name in (
            layout.shard_filename("chatgpt", subject),
            layout.index_shard_filename("chatgpt", subject),
            layout.turn_shard_filename("chatgpt", subject),
        ):
            assert validate_name(name.rsplit(".", 1)[0]) == [], name


def test_the_writer_rolls_a_month_to_a_numbered_shard_and_never_cuts_a_row(tmp_path: Path) -> None:
    writer = ShardWriter(tmp_path / "build", "grok", shard_bytes=1000)
    row = b'{"uid":"x"}' + b" " * 388 + b"\n"  # 400 bytes
    subjects = [writer.add("202506", row, b"{}\n") for _ in range(5)]
    assert subjects == ["202506", "202506", "202506-002", "202506-002", "202506-003"]
    assert writer.add(UNDATED, row, b"{}\n") == UNDATED
    shards = writer.close()
    assert [shard["subject"] for shard in shards] == [
        "202506",
        "202506-002",
        "202506-003",
        UNDATED,
    ]
    assert [shard["conversations"] for shard in shards] == [2, 2, 1, 1]
    for shard in shards:
        path = tmp_path / "build" / layout.shard_filename("grok", shard["subject"])
        assert path.stat().st_size == shard["bytes"]
        assert path.read_bytes().count(b"\n") == shard["conversations"]


def test_an_oversize_row_gets_a_shard_of_its_own(tmp_path: Path) -> None:
    writer = ShardWriter(tmp_path / "build", "grok", shard_bytes=1000)
    big = b"x" * 5000 + b"\n"
    assert writer.add("202506", b"a\n", b"{}\n") == "202506"
    assert writer.add("202506", big, b"{}\n") == "202506-002"
    assert writer.add("202506", b"b\n", b"{}\n") == "202506-003"


def test_many_months_do_not_exhaust_open_files(tmp_path: Path) -> None:
    writer = ShardWriter(tmp_path / "build", "grok", shard_bytes=10_000_000)
    months = [f"2020{month:02d}" for month in range(1, 13)] + [f"2021{m:02d}" for m in range(1, 13)]
    for round_ in range(3):
        for month in months:
            writer.add(month, f"{round_}\n".encode(), b"{}\n")
    shards = writer.close()
    assert len(shards) == 24 and all(shard["conversations"] == 3 for shard in shards)
    assert len(writer._handles) == 0


# --- the folder plan --------------------------------------------------------------------------


def _items(counts: dict[str, int]) -> list[tuple[str, str, int]]:
    """(uid, stamp, weight) with `counts[stamp-prefix]` conversations at each given day."""
    items = []
    for stamp, number in counts.items():
        items += [(f"u-{stamp}-{index:05d}", stamp, 1) for index in range(number)]
    return items


def test_a_month_that_fits_is_one_folder() -> None:
    plan = plan_conversation_folders(_items({"2025-06-14T00:00:00Z": 30}), 100)
    assert set(plan.values()) == {"month-all-202506"}


def test_a_full_month_spills_into_day_folders_and_a_full_day_into_buckets() -> None:
    items = _items(
        {
            "2025-06-01T00:00:00Z": 40,
            "2025-06-02T00:00:00Z": 40,
            "2025-06-03T00:00:00Z": 900,
        }
    )
    plan = plan_conversation_folders(items, 100)
    sizes = Counter(plan.values())
    assert max(sizes.values()) <= 100
    assert "month-all-202506/day-all-20250601" in sizes
    assert any("/bucket-all-b" in folder for folder in sizes)
    assert all(validate_name(part) == [] for folder in sizes for part in folder.split("/"))


def test_undated_conversations_get_the_structural_folder_and_spill_to_buckets() -> None:
    plan = plan_conversation_folders(_items({"": 30}), 100)
    assert set(plan.values()) == {"undated"}
    plan = plan_conversation_folders(_items({"": 500}), 100)
    assert all(folder.startswith("undated/bucket-all-b") for folder in plan.values())
    assert max(Counter(plan.values()).values()) <= 100


def test_the_weight_of_a_split_conversation_counts_against_the_folder() -> None:
    heavy = [(f"u{index}", "2025-06-01T00:00:00Z", 2) for index in range(60)]
    plan = plan_conversation_folders(heavy, 100)
    assert len(set(plan.values())) > 1
    loads: Counter[str] = Counter()
    for uid, _stamp, weight in heavy:
        loads[plan[uid]] += weight
    assert max(loads.values()) <= 100


def test_the_plan_is_deterministic_and_independent_of_input_order() -> None:
    items = _items({"2025-06-03T00:00:00Z": 700, "2025-07-01T00:00:00Z": 20, "": 15})
    shuffled = list(items)
    random.Random(1).shuffle(shuffled)
    assert plan_conversation_folders(items, 100) == plan_conversation_folders(shuffled, 100)


def test_a_flat_set_spills_into_buckets_only_when_it_must() -> None:
    names = [f"topic-all-t{number:04d}" for number in range(50)]
    assert set(plan_flat_folders(names, 100).values()) == {""}
    spilled = plan_flat_folders([f"topic-all-t{number:05d}" for number in range(900)], 100)
    assert max(Counter(spilled.values()).values()) <= 100
    assert all(folder.startswith("bucket-all-b") for folder in spilled.values())


def test_parts_stay_in_the_conversation_folder_until_they_outgrow_it() -> None:
    assert part_folder(0, 50, 100) == "" and part_folder(49, 50, 100) == ""
    assert part_folder(0, 250, 100) == "bucket-all-b000"
    assert part_folder(99, 250, 100) == "bucket-all-b000"
    assert part_folder(100, 250, 100) == "bucket-all-b001"
    assert part_folder(249, 250, 100) == "bucket-all-b002"
