from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from conversation_corpus_engine.schema_validation import validate_payload
from conversation_corpus_engine.sharded_collection import (
    SHARDED_COLLECTION_CONTRACT,
    ShardedCollectionError,
    collection_current_path,
    collection_exists,
    collection_generations_path,
    collection_storage_path,
    iter_collection,
    load_collection,
    validate_collection,
    write_collection,
)


def record_key(record: dict[str, object]) -> str:
    return str(record["id"])


def generation_root(logical_path: Path, generation_id: str) -> Path:
    return collection_generations_path(logical_path) / generation_id


def keys_with_shared_prefix(count: int) -> list[str]:
    by_prefix: dict[str, list[str]] = {}
    candidate = 0
    while True:
        key = f"key-{candidate}"
        prefix = hashlib.sha256(key.encode()).hexdigest()[:2]
        matches = by_prefix.setdefault(prefix, [])
        matches.append(key)
        if len(matches) == count:
            return matches
        candidate += 1


def test_ordered_round_trip_and_deterministic_rerun(tmp_path: Path) -> None:
    logical_path = tmp_path / "threads-index.json"
    records = [{"id": f"record-{index:03d}", "value": index} for index in range(50)]

    first = write_collection(logical_path, records, key=record_key)
    first_current = collection_current_path(logical_path).read_bytes()
    first_generation_dirs = sorted(collection_generations_path(logical_path).iterdir())
    second = write_collection(logical_path, records, key=record_key)

    assert first["contract_name"] == SHARDED_COLLECTION_CONTRACT
    assert second == first
    assert list(iter_collection(logical_path)) == records
    assert load_collection(logical_path) == records
    assert collection_current_path(logical_path).read_bytes() == first_current
    assert sorted(collection_generations_path(logical_path).iterdir()) == first_generation_dirs
    assert not logical_path.exists()
    assert validate_collection(logical_path) == first
    assert validate_payload("sharded-collection", first)["valid"] is True


def test_recursive_prefix_splitting_honors_limits(tmp_path: Path) -> None:
    logical_path = tmp_path / "pairs-index.json"
    keys = keys_with_shared_prefix(3)
    records = [{"id": key, "payload": "small"} for key in keys]

    manifest = write_collection(
        logical_path,
        records,
        key=record_key,
        max_shard_bytes=1_024,
        max_shard_records=1,
    )

    assert load_collection(logical_path) == records
    assert all(item["record_count"] == 1 for item in manifest["shards"])
    assert all(item["byte_count"] <= 1_024 for item in manifest["shards"])
    assert any(len(item["prefix"]) > 2 for item in manifest["shards"])


def test_duplicate_or_oversized_record_never_changes_current(tmp_path: Path) -> None:
    logical_path = tmp_path / "threads-index.json"
    baseline = [{"id": "baseline", "payload": "safe"}]
    write_collection(logical_path, baseline, key=record_key, max_shard_bytes=200)
    before = collection_current_path(logical_path).read_bytes()

    with pytest.raises(ShardedCollectionError, match="Duplicate"):
        write_collection(
            logical_path,
            [{"id": "duplicate"}, {"id": "duplicate"}],
            key=record_key,
            max_shard_bytes=200,
        )
    with pytest.raises(ShardedCollectionError, match="exceeds"):
        write_collection(
            logical_path,
            [{"id": "oversized", "payload": "x" * 500}],
            key=record_key,
            max_shard_bytes=200,
        )

    assert collection_current_path(logical_path).read_bytes() == before
    assert load_collection(logical_path) == baseline


def test_limits_cannot_exceed_the_published_contract(tmp_path: Path) -> None:
    logical_path = tmp_path / "threads-index.json"

    with pytest.raises(ShardedCollectionError, match="may not exceed"):
        write_collection(
            logical_path,
            [{"id": "record"}],
            key=record_key,
            max_shard_bytes=(8 * 1024 * 1024) + 1,
        )
    with pytest.raises(ShardedCollectionError, match="may not exceed"):
        write_collection(
            logical_path,
            [{"id": "record"}],
            key=record_key,
            max_shard_records=1_001,
        )

    assert not collection_storage_path(logical_path).exists()


@pytest.mark.parametrize(
    "failure",
    ["missing", "corrupt", "duplicate-catalog", "unlisted", "path-traversal"],
)
def test_missing_corrupt_or_duplicate_shard_state_fails_closed(
    tmp_path: Path,
    failure: str,
) -> None:
    logical_path = tmp_path / f"{failure}.json"
    manifest = write_collection(
        logical_path,
        [{"id": "alpha"}, {"id": "beta"}],
        key=record_key,
    )
    root = generation_root(logical_path, manifest["generation_id"])
    manifest_path = root / "manifest.json"
    first_shard = root / manifest["shards"][0]["path"]

    if failure == "missing":
        first_shard.unlink()
    elif failure == "corrupt":
        first_shard.write_bytes(first_shard.read_bytes() + b"{}\n")
    elif failure == "duplicate-catalog":
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["shards"].append(dict(payload["shards"][0]))
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    elif failure == "unlisted":
        (root / "shards" / "extra.jsonl").write_text("{}\n", encoding="utf-8")
    else:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["shards"][0]["path"] = "shards/../../outside.jsonl"
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ShardedCollectionError):
        load_collection(logical_path)


def test_incomplete_v2_state_never_falls_back_to_v1(tmp_path: Path) -> None:
    logical_path = tmp_path / "threads-index.json"
    logical_path.write_text('[{"id":"legacy"}]\n', encoding="utf-8")
    collection_storage_path(logical_path).mkdir()

    with pytest.raises(ShardedCollectionError, match="CURRENT"):
        load_collection(logical_path)


def test_dangling_storage_symlink_with_legacy_v1_file_fails_closed(tmp_path: Path) -> None:
    logical_path = tmp_path / "threads-index.json"
    logical_path.write_text('[{"id":"stale"}]\n', encoding="utf-8")
    collection_storage_path(logical_path).symlink_to(tmp_path / "nonexistent_dir")

    with pytest.raises(ShardedCollectionError):
        collection_exists(logical_path)
    with pytest.raises(ShardedCollectionError):
        list(iter_collection(logical_path))
    with pytest.raises(ShardedCollectionError):
        load_collection(logical_path)


def test_dangling_storage_symlink_without_legacy_file_fails_closed(tmp_path: Path) -> None:
    logical_path = tmp_path / "threads-index.json"
    collection_storage_path(logical_path).symlink_to(tmp_path / "nonexistent_dir")

    with pytest.raises(ShardedCollectionError):
        collection_exists(logical_path)
    with pytest.raises(ShardedCollectionError):
        list(iter_collection(logical_path))
    with pytest.raises(ShardedCollectionError):
        load_collection(logical_path, default=[])


def test_interrupted_publication_preserves_previous_current(tmp_path: Path) -> None:
    logical_path = tmp_path / "threads-index.json"
    baseline = [{"id": "alpha", "value": 1}]
    write_collection(logical_path, baseline, key=record_key)
    before = collection_current_path(logical_path).read_bytes()
    original_replace = Path.replace

    def interrupt_current(source: Path, destination: Path) -> None:
        if destination == collection_current_path(logical_path):
            raise OSError("simulated publication interruption")
        original_replace(source, destination)

    with (
        patch(
            "conversation_corpus_engine.sharded_collection._atomic_replace",
            side_effect=interrupt_current,
        ),
        pytest.raises(OSError, match="interruption"),
    ):
        write_collection(
            logical_path,
            [{"id": "alpha", "value": 2}],
            key=record_key,
        )

    assert collection_current_path(logical_path).read_bytes() == before
    assert load_collection(logical_path) == baseline


def test_v1_array_and_wrapped_collection_compatibility(tmp_path: Path) -> None:
    array_path = tmp_path / "threads-index.json"
    wrapped_path = tmp_path / "semantic-v3-index.json"
    records = [{"id": "legacy"}]
    array_path.write_text(json.dumps(records), encoding="utf-8")
    wrapped_path.write_text(json.dumps({"threads": records}), encoding="utf-8")

    assert load_collection(array_path) == records
    assert load_collection(wrapped_path, legacy_wrapper="threads") == records


def test_one_record_change_reuses_every_unchanged_shard(tmp_path: Path) -> None:
    logical_path = tmp_path / "threads-index.json"
    keys = []
    prefixes: set[str] = set()
    candidate = 0
    while len(keys) < 2:
        key = f"locality-{candidate}"
        prefix = hashlib.sha256(key.encode()).hexdigest()[:2]
        if prefix not in prefixes:
            prefixes.add(prefix)
            keys.append(key)
        candidate += 1
    baseline = [{"id": keys[0], "value": 1}, {"id": keys[1], "value": 2}]
    first = write_collection(logical_path, baseline, key=record_key)
    changed = [{"id": keys[0], "value": 99}, {"id": keys[1], "value": 2}]
    second = write_collection(logical_path, changed, key=record_key)
    first_root = generation_root(logical_path, first["generation_id"])
    second_root = generation_root(logical_path, second["generation_id"])
    first_by_prefix = {item["prefix"]: item for item in first["shards"]}
    second_by_prefix = {item["prefix"]: item for item in second["shards"]}
    unchanged_prefixes = {
        prefix
        for prefix in first_by_prefix
        if first_by_prefix[prefix]["sha256"] == second_by_prefix[prefix]["sha256"]
    }
    changed_prefixes = set(first_by_prefix) - unchanged_prefixes

    assert len(changed_prefixes) == 1
    assert len(unchanged_prefixes) == 1
    for prefix in unchanged_prefixes:
        first_path = first_root / first_by_prefix[prefix]["path"]
        second_path = second_root / second_by_prefix[prefix]["path"]
        assert first_path.stat().st_ino == second_path.stat().st_ino
    assert load_collection(logical_path) == changed
