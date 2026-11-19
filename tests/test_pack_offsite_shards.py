"""The standalone packing helper `scripts/pack_offsite_shards.py`, on synthetic trees.

Every source tree is built in a temp directory. No real archive paths, names or
sizes appear here.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "pack_offsite_shards.py"


def load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("pack_offsite_shards", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["pack_offsite_shards"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def tool() -> ModuleType:
    return load_tool()


@pytest.fixture()
def source_tree(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "one.bin").write_bytes(b"1" * 10)
    (root / "a" / "b" / "two.bin").write_bytes(b"2" * 20)
    (root / "three.bin").write_bytes(b"3" * 30)
    return root


def test_scan_source_walks_regular_files_sorted(tool: ModuleType, source_tree: Path) -> None:
    rels = tool.scan_source(source_tree)
    assert rels == ["a/b/two.bin", "a/one.bin", "three.bin"]


def test_scan_source_skips_symlinks(tool: ModuleType, source_tree: Path, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.bin"
    target.write_bytes(b"secret")
    (source_tree / "link.bin").symlink_to(target)
    rels = tool.scan_source(source_tree)
    assert "link.bin" not in rels


def test_load_selection_requires_json_string_array(tool: ModuleType, tmp_path: Path) -> None:
    good = tmp_path / "sel.json"
    good.write_text(json.dumps(["a/one.bin"]), encoding="utf-8")
    assert tool.load_selection(good) == ["a/one.bin"]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    with pytest.raises(ValueError):
        tool.load_selection(bad)
    bad2 = tmp_path / "bad2.json"
    bad2.write_text(json.dumps([1, 2]), encoding="utf-8")
    with pytest.raises(ValueError):
        tool.load_selection(bad2)


def test_measure_rejects_path_escaping_source(tool: ModuleType, source_tree: Path) -> None:
    with pytest.raises(ValueError, match="escapes source"):
        tool.measure(source_tree, ["../outside.bin"])


def test_measure_rejects_missing_file(tool: ModuleType, source_tree: Path) -> None:
    with pytest.raises(ValueError, match="not a regular file"):
        tool.measure(source_tree, ["does-not-exist.bin"])


def test_measure_embeds_sha256_when_requested(tool: ModuleType, source_tree: Path) -> None:
    plain = tool.measure(source_tree, ["a/one.bin"])
    assert plain[0].sha256 is None
    hashed = tool.measure(source_tree, ["a/one.bin"], hash_files=True)
    expected = hashlib.sha256((source_tree / "a" / "one.bin").read_bytes()).hexdigest()
    assert hashed[0].sha256 == expected


def test_pack_respects_cap_and_numbers_shards(tool: ModuleType) -> None:
    entries = [
        tool.Entry(rel=f"f{i}.bin", size=size) for i, size in enumerate([40, 30, 25, 25, 10, 10, 5])
    ]
    shards, oversize = tool.pack(entries, max_bytes=64)
    assert oversize == []
    assert all(shard["bytes"] <= 64 for shard in shards)
    assert [shard["shard"] for shard in shards] == list(range(1, len(shards) + 1))
    assert [shard["name"] for shard in shards] == [
        f"shard-{i:04d}" for i in range(1, len(shards) + 1)
    ]
    assert all(shard["file_count"] == len(shard["files"]) for shard in shards)
    total = sum(shard["bytes"] for shard in shards)
    assert total == sum(e.size for e in entries)


def test_pack_is_deterministic_regardless_of_input_order(tool: ModuleType) -> None:
    forward = [
        tool.Entry(rel="b.bin", size=10),
        tool.Entry(rel="a.bin", size=10),
        tool.Entry(rel="c.bin", size=10),
    ]
    reverse = list(reversed(forward))
    shards_a, _ = tool.pack(forward, max_bytes=100)
    shards_b, _ = tool.pack(reverse, max_bytes=100)
    assert [s["files"] for s in shards_a] == [s["files"] for s in shards_b]


def test_pack_flags_oversize_single_file(tool: ModuleType) -> None:
    entries = [tool.Entry(rel="big.bin", size=1000), tool.Entry(rel="small.bin", size=10)]
    shards, oversize = tool.pack(entries, max_bytes=64)
    assert [s["name"] for s in oversize] == ["shard-0001"]
    assert shards[0]["files"][0]["path"] == "big.bin"


def test_pack_propagates_sha256_into_plan_items(tool: ModuleType) -> None:
    entries = [tool.Entry(rel="x.bin", size=5, sha256="ab" * 32)]
    shards, _ = tool.pack(entries, max_bytes=64)
    assert shards[0]["files"][0]["sha256"] == "ab" * 32


def test_main_writes_plan_and_reports_aggregates(
    tool: ModuleType, source_tree: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan_out = tmp_path / "plan.json"
    rc = tool.main(
        [
            "--source",
            str(source_tree),
            "--max-bytes",
            "48",
            "--hash",
            "--plan-out",
            str(plan_out),
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["all_shards_within_limit"] is True
    assert out["file_count"] == 3
    assert out["total_bytes"] == 60
    assert "shards" not in out  # shard detail goes to the plan file only
    plan = json.loads(plan_out.read_text(encoding="utf-8"))
    assert plan["schema"] == "cce.offsite-shard-pack.v1"
    hashed = [f for shard in plan["shards"] for f in shard["files"] if "sha256" in f]
    assert len(hashed) == 3


def test_main_returns_2_on_oversize_without_allow_flag(
    tool: ModuleType, source_tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = tool.main(["--source", str(source_tree), "--max-bytes", "5"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "oversize" in err


def test_materialize_copies_tree_and_refuses_existing_output(
    tool: ModuleType, source_tree: Path, tmp_path: Path
) -> None:
    out_dir = tmp_path / "out"
    rc = tool.main(
        ["--source", str(source_tree), "--max-bytes", "48", "--materialize", str(out_dir)]
    )
    assert rc == 0
    copied = sorted(p.relative_to(out_dir).as_posix() for p in out_dir.rglob("*.bin"))
    assert any(path.endswith("a/one.bin") for path in copied)
    rc = tool.main(
        ["--source", str(source_tree), "--max-bytes", "48", "--materialize", str(out_dir)]
    )
    assert rc == 2  # output exists; never overwritten


def test_materialize_checks_free_disk_space(
    tool: ModuleType, source_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeUsage:
        free = 1

    monkeypatch.setattr(tool.shutil, "disk_usage", lambda _p: FakeUsage())
    out_dir = tmp_path / "out"
    rc = tool.main(
        ["--source", str(source_tree), "--max-bytes", "48", "--materialize", str(out_dir)]
    )
    assert rc == 2
    assert not out_dir.exists()


def test_materialize_does_not_create_output_on_space_failure(
    tool: ModuleType, source_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeUsage:
        free = 0

    monkeypatch.setattr(tool.shutil, "disk_usage", lambda _p: FakeUsage())
    out_dir = tmp_path / "fresh" / "out"
    rc = tool.main(
        ["--source", str(source_tree), "--max-bytes", "48", "--materialize", str(out_dir)]
    )
    assert rc == 2
    assert not out_dir.exists()
