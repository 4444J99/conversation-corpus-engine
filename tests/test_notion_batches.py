import hashlib
import importlib.util
import json
import os
import stat
import sys
import warnings
import zipfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/package-notion-batches.py"
spec = importlib.util.spec_from_file_location("notion_batches", SCRIPT)
batches = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = batches
spec.loader.exec_module(batches)


def source_zip(path, members):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in members:
            archive.writestr(name, value)
    return path


def unpack(output, manifest):
    seen = {}
    for batch in manifest["archives"]:
        path = output / batch["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == batch["sha256"]
        with zipfile.ZipFile(path) as archive:
            assert archive.namelist()[0] == "library/"
            assert len(archive.infolist()) == batch["entries_including_wrapper"]
            for name in archive.namelist()[1:]:
                original = name.removeprefix("library/")
                assert original not in seen
                seen[original] = archive.read(name)
    return seen


def test_batches_are_exact_once_bounded_and_deterministic(tmp_path):
    members = [
        ("vault/z.md", b"same"),
        ("vault/a.md", b"same"),
        ("agent-entry.md", b"entry"),
        ("vault/café.md", "Unicode cafe: \u00e9".encode()),
        ("records.csv", b"uid,title\na,A\n"),
    ]
    source = source_zip(tmp_path / "full.zip", members)
    original = source.read_bytes()
    first = batches.package(source, tmp_path / "first", max_entries=3, max_batch_bytes=2048)
    second = batches.package(source, tmp_path / "second", max_entries=3, max_batch_bytes=2048)
    assert first == second
    assert len(first["archives"]) == 3
    assert first["original_members"] == 5
    assert first["oversized_members"] == 0
    assert unpack(tmp_path / "first", first) == dict(members)
    assert source.read_bytes() == original
    assert all(
        item["entries_including_wrapper"] <= 3 and item["bytes"] <= 2048
        for item in first["archives"]
    )
    ledger = [
        json.loads(line) for line in (tmp_path / "first/members.jsonl").read_text().splitlines()
    ]
    assert len(ledger) == 5 and {row["original_path"] for row in ledger} == set(dict(members))
    assert (tmp_path / "first").stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in (tmp_path / "first").iterdir())


def test_final_zip_byte_cap_and_explicit_oversize_preservation(tmp_path):
    members = [("small.md", b"hello"), ("large.md", os.urandom(4096))]
    source = source_zip(tmp_path / "full.zip", members)
    manifest = batches.package(source, tmp_path / "derived", max_batch_bytes=512)
    assert manifest["oversized_members"] == 1
    assert unpack(tmp_path / "derived", manifest) == dict(members)
    oversized = [
        item for item in manifest["archives"] if item["disposition"] == "oversized_preservation"
    ]
    assert len(oversized) == 1 and oversized[0]["bytes"] > 512
    assert all(
        item["bytes"] <= 512
        for item in manifest["archives"]
        if item["disposition"] == "notion_batch"
    )
    assert manifest["all_archives_within_batch_limits"] is False


@pytest.mark.parametrize(
    "name", ["../escape.md", "/absolute.md", "a/../b.md", "a\\b.md", "a//b.md", "C:drive.md"]
)
def test_unsafe_source_member_paths_are_rejected_without_output(tmp_path, name):
    source = source_zip(tmp_path / "full.zip", [(name, b"evidence")])
    with pytest.raises(ValueError, match="member path"):
        batches.package(source, tmp_path / "derived")
    assert not (tmp_path / "derived").exists()


def test_duplicates_symlinks_and_existing_destination_are_rejected(tmp_path):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        source = source_zip(
            tmp_path / "duplicate.zip", [("same.md", b"first"), ("same.md", b"second")]
        )
    with pytest.raises(ValueError, match="duplicate"):
        batches.package(source, tmp_path / "duplicate-output")
    link = zipfile.ZipInfo("link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(tmp_path / "link.zip", "w") as archive:
        archive.writestr(link, "elsewhere")
    with pytest.raises(ValueError, match="symlink"):
        batches.package(tmp_path / "link.zip", tmp_path / "link-output")
    output = tmp_path / "existing"
    output.mkdir()
    (output / "keep").write_text("preserved")
    good = source_zip(tmp_path / "good.zip", [("a.md", b"a")])
    with pytest.raises(FileExistsError):
        batches.package(good, output)
    assert (output / "keep").read_text() == "preserved"


def test_budget_failure_preserves_source_and_partial_receipt(tmp_path):
    source = source_zip(tmp_path / "full.zip", [("large.md", os.urandom(200000))])
    original = source.read_bytes()
    with pytest.raises(ValueError, match="output byte budget"):
        batches.package(source, tmp_path / "derived", max_output_bytes=65536)
    assert source.read_bytes() == original
    assert not (tmp_path / "derived/manifest.json").exists()
    failure = json.loads((tmp_path / "derived/failure.json").read_text())
    assert failure["complete"] is False
    assert not list((tmp_path / "derived").glob("*.zip"))


def test_notional_file_plan_thresholds_use_decimal_megabytes():
    assert batches.compatibility(5000000) == {"free_plan": True, "paid_plan": True}
    assert batches.compatibility(5000001) == {"free_plan": False, "paid_plan": True}
    assert batches.compatibility(50000000) == {"free_plan": False, "paid_plan": True}
    assert batches.compatibility(50000001) == {"free_plan": False, "paid_plan": False}


def test_storage_floor_prevents_batch_writes(tmp_path, monkeypatch):
    source = source_zip(tmp_path / "full.zip", [("a.md", b"evidence")])
    monkeypatch.setattr(batches.shutil, "disk_usage", lambda _: SimpleNamespace(free=1024**3 + 1))
    with pytest.raises(ValueError, match="free-space floor|planned output"):
        batches.package(source, tmp_path / "derived")
    assert not list((tmp_path / "derived").glob("*.zip"))


def test_default_entry_limit_includes_wrapper(tmp_path):
    members = [(f"vault/{index:05}.md", b"evidence") for index in range(5001)]
    source = source_zip(tmp_path / "full.zip", members)
    manifest = batches.package(source, tmp_path / "derived")
    assert [item["entries_including_wrapper"] for item in manifest["archives"]] == [5000, 3]
    assert unpack(tmp_path / "derived", manifest) == dict(members)


def test_measured_final_size_splits_normal_batches_before_cap(tmp_path):
    members = [(f"{index}.md", os.urandom(100)) for index in range(6)]
    source = source_zip(tmp_path / "full.zip", members)
    manifest = batches.package(source, tmp_path / "derived", max_batch_bytes=600)
    assert len(manifest["archives"]) > 1 and manifest["oversized_members"] == 0
    assert all(item["bytes"] <= 600 for item in manifest["archives"])
    assert unpack(tmp_path / "derived", manifest) == dict(members)


def test_source_change_and_corrupt_crc_cannot_leave_completed_manifest(tmp_path, monkeypatch):
    source = source_zip(tmp_path / "full.zip", [("a.md", b"original source")])
    measure = batches._measure_member
    changed = False

    def mutate_source(*args, **kwargs):
        nonlocal changed
        result = measure(*args, **kwargs)
        if not changed:
            with source.open("ab") as stream:
                stream.write(b"source changed")
            changed = True
        return result

    with monkeypatch.context() as patch:
        patch.setattr(batches, "_measure_member", mutate_source)
        with pytest.raises(ValueError, match="source ZIP changed"):
            batches.package(source, tmp_path / "changed-output")
    assert not (tmp_path / "changed-output/manifest.json").exists()
    assert (tmp_path / "changed-output/failure.json").is_file()
    corrupt = tmp_path / "corrupt.zip"
    with zipfile.ZipFile(corrupt, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("a.md", b"corrupt-me")
    data = bytearray(corrupt.read_bytes())
    data[data.index(b"corrupt-me")] ^= 1
    corrupt.write_bytes(data)
    with pytest.raises(zipfile.BadZipFile, match="CRC"):
        batches.package(corrupt, tmp_path / "corrupt-output")
    assert not (tmp_path / "corrupt-output/manifest.json").exists()
    assert (tmp_path / "corrupt-output/failure.json").is_file()


def test_derived_output_cannot_change_canonical_generation_inventory(tmp_path):
    generation = tmp_path / "library-all/generations/generation-fixture"
    generation.mkdir(parents=True)
    source = source_zip(generation / "notion-import.zip", [("a.md", b"evidence")])
    with pytest.raises(ValueError, match="outside the canonical generation"):
        batches.package(source, generation / "batches")
    assert not (generation / "batches").exists()


def test_other_generation_is_never_an_output_destination(tmp_path):
    generations = tmp_path / "library-all/generations"
    original = generations / "generation-original"
    protected = generations / "generation-protected"
    original.mkdir(parents=True)
    protected.mkdir()
    source = source_zip(original / "notion-import.zip", [("a.md", b"evidence")])
    sentinel = protected / "existing.md"
    sentinel.write_bytes(b"canonical evidence remains unchanged")
    with pytest.raises(ValueError, match="outside the canonical generation"):
        batches.package(source, protected / "batches")
    assert not (protected / "batches").exists()
    assert sentinel.read_bytes() == b"canonical evidence remains unchanged"


def test_late_metadata_budget_refusal_leaves_no_completion_files(tmp_path, monkeypatch):
    source = source_zip(tmp_path / "full.zip", [("a.md", b"original evidence")])
    original = source.read_bytes()
    output = tmp_path / "derived"
    charge = batches.Budget.charge

    def exhaust_at_completion(budget, amount):
        readme = output / "README.md"
        if readme.exists() and readme.stat().st_size:
            budget.max_bytes = budget.written + amount + batches.FAILURE_RESERVE - 1
        return charge(budget, amount)

    monkeypatch.setattr(batches.Budget, "charge", exhaust_at_completion)
    with pytest.raises(ValueError, match="output byte budget"):
        batches.package(source, output)
    assert source.read_bytes() == original
    assert (output / "batch-000001.zip").is_file()
    assert not (output / "run-receipt.json").exists()
    assert not (output / "manifest.json").exists()
    assert json.loads((output / "failure.json").read_bytes())["complete"] is False


@pytest.mark.parametrize("name", ["run-receipt.json", "manifest.json"])
def test_interrupted_completion_write_never_publishes_partial_marker(tmp_path, monkeypatch, name):
    source = source_zip(tmp_path / "full.zip", [("a.md", b"original evidence")])
    original = source.read_bytes()
    output = tmp_path / "derived"
    private_binary = batches._private_binary

    class InterruptedWrite:
        def __init__(self, stream):
            self.stream = stream

        def write(self, data):
            self.stream.write(data[:7])
            self.stream.flush()
            raise OSError("simulated interrupted completion write")

    @contextmanager
    def interrupt(path):
        with private_binary(path) as stream:
            yield (
                InterruptedWrite(stream) if path.name in {name, "." + name + ".partial"} else stream
            )

    monkeypatch.setattr(batches, "_private_binary", interrupt)
    with pytest.raises(OSError, match="interrupted completion write"):
        batches.package(source, output)
    assert source.read_bytes() == original
    assert (output / "batch-000001.zip").is_file()
    assert not (output / "run-receipt.json").exists()
    assert not (output / "manifest.json").exists()
    assert json.loads((output / "failure.json").read_bytes())["complete"] is False


def test_completion_fsync_failure_prevents_published_manifest(tmp_path, monkeypatch):
    source = source_zip(tmp_path / "full.zip", [("a.md", b"original evidence")])
    output = tmp_path / "derived"
    fsync = batches.os.fsync

    def refuse_manifest_sync(descriptor):
        temporary = output / ".manifest.json.partial"
        if temporary.exists() and os.fstat(descriptor).st_ino == temporary.stat().st_ino:
            raise OSError("simulated completion fsync failure")
        return fsync(descriptor)

    monkeypatch.setattr(batches.os, "fsync", refuse_manifest_sync)
    with pytest.raises(OSError, match="completion fsync failure"):
        batches.package(source, output)
    assert not (output / "run-receipt.json").exists()
    assert not (output / "manifest.json").exists()
    assert json.loads((output / "failure.json").read_bytes())["complete"] is False
