from __future__ import annotations

import hashlib
import heapq
import json
import re
import shutil
import uuid
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .corpus_store import CorpusWriteAuthorization

SHARDED_COLLECTION_CONTRACT = "cce.sharded_collection.v1"
SHARDED_COLLECTION_VERSION = 1
MAX_SHARD_BYTES = 8 * 1024 * 1024
MAX_SHARD_RECORDS = 1_000
INITIAL_PREFIX_LENGTH = 2
CURRENT_GENERATION_PATTERN = re.compile(r"^[a-f0-9]{32}$")

CORPUS_COLLECTION_KEY_FIELDS: dict[str, tuple[str, ...]] = {
    "action-ledger.json": ("action_key",),
    "actions-index.json": ("federated_action_id",),
    "canonical-actions.json": ("federated_action_id",),
    "canonical-entities.json": (
        "canonical_entity_id",
        "federated_entity_id",
        "entity_id",
        "canonical_label",
    ),
    "canonical-families.json": (
        "canonical_family_id",
        "federated_family_id",
        "family_id",
    ),
    "canonical-unresolved.json": ("federated_question_id",),
    "corpora-summary.json": ("corpus_id",),
    "doctrine-briefs.json": ("family_id", "canonical_family_id", "federated_family_id"),
    "doctrine-timeline.json": ("canonical_family_id", "family_id"),
    "entity-aliases.json": ("canonical_entity_id", "canonical_label"),
    "entity-dossiers.json": ("federated_entity_id",),
    "entities-index.json": ("federated_entity_id",),
    "family-dossiers.json": ("family_id", "canonical_family_id"),
    "families-index.json": ("federated_family_id",),
    "import-audit.json": ("thread_uid", "conversation_id", "id"),
    "import-manifest.json": (
        "conversation_uuid",
        "thread_uid",
        "source_export",
        "source_markdown",
        "relative_path",
    ),
    "near-duplicates.json": ("duplicate_id", "pair_id", "thread_uid", "id"),
    "pairs-index.json": ("pair_id",),
    "project-dossiers.json": ("federated_family_id", "project_id"),
    "lineage-map.json": ("federated_family_id",),
    "semantic-v3-index.json": ("thread_uid",),
    "threads-index.json": ("thread_uid",),
    "unresolved-index.json": ("federated_question_id",),
    "unresolved-ledger.json": ("question_key",),
}


class ShardedCollectionError(ValueError):
    """Raised when sharded collection state is invalid or cannot be published safely."""


def canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def content_hash_key(payload: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def collection_storage_path(logical_path: Path) -> Path:
    if logical_path.suffix:
        return logical_path.with_suffix(".collection")
    return logical_path.with_name(f"{logical_path.name}.collection")


def collection_current_path(logical_path: Path) -> Path:
    return collection_storage_path(logical_path) / "CURRENT"


def collection_generations_path(logical_path: Path) -> Path:
    return collection_storage_path(logical_path) / "generations"


def _collection_storage_exists(logical_path: Path) -> bool:
    storage_root = collection_storage_path(logical_path)
    return storage_root.is_symlink() or storage_root.exists()


def collection_exists(logical_path: Path) -> bool:
    logical_path = logical_path.resolve(strict=False)
    if _collection_storage_exists(logical_path):
        validate_collection(logical_path)
        return True
    return logical_path.is_file()


def _record_key_from_fields(record: Any, fields: Sequence[str]) -> str:
    if not isinstance(record, dict):
        raise ShardedCollectionError("Collection records must be JSON objects.")
    for field in fields:
        value = record.get(field)
        if isinstance(value, (str, int)) and str(value):
            return str(value)
    raise ShardedCollectionError(
        f"Collection record is missing a stable key field from: {', '.join(fields)}"
    )


def corpus_collection_key(logical_path: Path, record: Any) -> str:
    if logical_path.name == "near-duplicates.json":
        if not isinstance(record, dict):
            raise ShardedCollectionError("Collection records must be JSON objects.")
        thread_uids = record.get("thread_uids")
        if isinstance(thread_uids, list) and len(thread_uids) >= 2:
            values = [str(value) for value in thread_uids if value]
            if len(values) == len(thread_uids):
                return "\0".join(sorted(values))
    fields = CORPUS_COLLECTION_KEY_FIELDS.get(logical_path.name)
    if fields is None:
        raise ShardedCollectionError(
            f"No stable-key contract is registered for collection: {logical_path.name}"
        )
    return _record_key_from_fields(record, fields)


def merge_ledger_records(
    records: Iterable[dict[str, Any]],
    *,
    key_field: str,
    text_field: str,
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for record in records:
        key = str(record.get(key_field) or "")
        if not key:
            raise ShardedCollectionError(f"Ledger record is missing {key_field}.")
        existing = merged.get(key)
        if existing is None:
            merged[key] = {
                **record,
                "family_ids": list(record.get("family_ids") or []),
                "thread_uids": list(record.get("thread_uids") or []),
            }
            continue
        if existing.get(text_field) != record.get(text_field):
            raise ShardedCollectionError(
                f"Ledger key collision has conflicting {text_field}: {key}"
            )
        existing["family_ids"] = list(
            dict.fromkeys([*existing["family_ids"], *(record.get("family_ids") or [])])
        )
        existing["thread_uids"] = list(
            dict.fromkeys([*existing["thread_uids"], *(record.get("thread_uids") or [])])
        )
        existing["occurrence_count"] = int(existing.get("occurrence_count") or 0) + int(
            record.get("occurrence_count") or 0
        )
    return list(merged.values())


def merge_entity_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for record in records:
        key = str(record.get("canonical_entity_id") or "")
        if not key:
            raise ShardedCollectionError("Entity record is missing canonical_entity_id.")
        existing = merged.get(key)
        if existing is None:
            merged[key] = {
                **record,
                "aliases": list(record.get("aliases") or []),
            }
            continue
        if existing.get("canonical_label") != record.get("canonical_label") or existing.get(
            "entity_type"
        ) != record.get("entity_type"):
            raise ShardedCollectionError(f"Entity key collision has conflicting identity: {key}")
        existing["aliases"] = list(
            dict.fromkeys([*existing["aliases"], *(record.get("aliases") or [])])
        )
    return list(merged.values())


def _encode_envelope(key: str, ordinal: int, record: Any) -> bytes:
    return (
        canonical_json_bytes(
            {
                "key": key,
                "ordinal": ordinal,
                "record": record,
            }
        )
        + b"\n"
    )


def _prepare_records(
    records: Iterable[Any],
    key: Callable[[Any], str],
    *,
    max_shard_bytes: int,
) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for ordinal, record in enumerate(records):
        stable_key = key(record)
        if not isinstance(stable_key, str) or not stable_key:
            raise ShardedCollectionError("Collection stable keys must be non-empty strings.")
        if stable_key in seen_keys:
            raise ShardedCollectionError(f"Duplicate collection key: {stable_key}")
        seen_keys.add(stable_key)
        encoded = _encode_envelope(stable_key, ordinal, record)
        if len(encoded) > max_shard_bytes:
            raise ShardedCollectionError(
                f"Collection record {stable_key!r} exceeds the shard byte limit."
            )
        prepared.append(
            {
                "key": stable_key,
                "key_hash": hashlib.sha256(stable_key.encode("utf-8")).hexdigest(),
                "ordinal": ordinal,
                "record": record,
                "encoded": encoded,
            }
        )
    return prepared


def _bucket_within_limits(
    items: Sequence[dict[str, Any]],
    *,
    max_shard_bytes: int,
    max_shard_records: int,
) -> bool:
    return len(items) <= max_shard_records and sum(len(item["encoded"]) for item in items) <= (
        max_shard_bytes
    )


def _split_bucket(
    prefix: str,
    items: Sequence[dict[str, Any]],
    *,
    max_shard_bytes: int,
    max_shard_records: int,
) -> dict[str, list[dict[str, Any]]]:
    if _bucket_within_limits(
        items,
        max_shard_bytes=max_shard_bytes,
        max_shard_records=max_shard_records,
    ):
        return {prefix: list(items)}
    if len(prefix) >= 64:
        raise ShardedCollectionError(
            f"Hash-prefix bucket {prefix!r} cannot be split within shard limits."
        )
    children: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        child_prefix = item["key_hash"][: len(prefix) + 1]
        children.setdefault(child_prefix, []).append(item)
    if len(children) == 1:
        child_prefix, child_items = next(iter(children.items()))
        return _split_bucket(
            child_prefix,
            child_items,
            max_shard_bytes=max_shard_bytes,
            max_shard_records=max_shard_records,
        )
    result: dict[str, list[dict[str, Any]]] = {}
    for child_prefix in sorted(children):
        result.update(
            _split_bucket(
                child_prefix,
                children[child_prefix],
                max_shard_bytes=max_shard_bytes,
                max_shard_records=max_shard_records,
            )
        )
    return result


def _partition_records(
    records: Sequence[dict[str, Any]],
    *,
    max_shard_bytes: int,
    max_shard_records: int,
) -> dict[str, list[dict[str, Any]]]:
    initial: dict[str, list[dict[str, Any]]] = {}
    for item in records:
        prefix = item["key_hash"][:INITIAL_PREFIX_LENGTH]
        initial.setdefault(prefix, []).append(item)
    shards: dict[str, list[dict[str, Any]]] = {}
    for prefix in sorted(initial):
        shards.update(
            _split_bucket(
                prefix,
                initial[prefix],
                max_shard_bytes=max_shard_bytes,
                max_shard_records=max_shard_records,
            )
        )
    return shards


def _manifest_core(
    logical_path: Path,
    *,
    record_count: int,
    shards: list[dict[str, Any]],
    max_shard_bytes: int,
    max_shard_records: int,
) -> dict[str, Any]:
    return {
        "contract_name": SHARDED_COLLECTION_CONTRACT,
        "contract_version": SHARDED_COLLECTION_VERSION,
        "logical_name": logical_path.name,
        "record_count": record_count,
        "limits": {
            "max_shard_bytes": max_shard_bytes,
            "max_shard_records": max_shard_records,
        },
        "shards": shards,
    }


def _generation_id(manifest_core: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(manifest_core)).hexdigest()[:32]


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ShardedCollectionError(
            f"Collection manifest is missing or malformed: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise ShardedCollectionError(f"Collection manifest must be a JSON object: {path}")
    return payload


def _resolve_current_generation(logical_path: Path) -> tuple[Path, dict[str, Any]]:
    storage_root = collection_storage_path(logical_path)
    if storage_root.is_symlink() or not storage_root.is_dir():
        raise ShardedCollectionError(f"Collection storage root is invalid: {storage_root}")
    current_path = collection_current_path(logical_path)
    if current_path.is_symlink():
        raise ShardedCollectionError(
            f"Collection CURRENT pointer may not be a symlink: {current_path}"
        )
    try:
        generation_id = current_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ShardedCollectionError(
            f"Collection v2 state exists without a readable CURRENT pointer: {storage_root}"
        ) from exc
    if not CURRENT_GENERATION_PATTERN.fullmatch(generation_id):
        raise ShardedCollectionError(f"Collection CURRENT pointer is malformed: {current_path}")
    generation_root = collection_generations_path(logical_path) / generation_id
    if generation_root.is_symlink() or not generation_root.is_dir():
        raise ShardedCollectionError(f"Collection generation is missing: {generation_root}")
    manifest = _load_manifest(generation_root / "manifest.json")
    if manifest.get("generation_id") != generation_id:
        raise ShardedCollectionError(
            f"Collection manifest generation does not match CURRENT: {generation_root}"
        )
    return generation_root, manifest


def _validate_manifest_shape(
    logical_path: Path,
    generation_root: Path,
    manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    if set(manifest) != {
        "contract_name",
        "contract_version",
        "logical_name",
        "record_count",
        "limits",
        "shards",
        "generation_id",
    }:
        raise ShardedCollectionError(f"Collection manifest fields are invalid: {generation_root}")
    if manifest.get("contract_name") != SHARDED_COLLECTION_CONTRACT:
        raise ShardedCollectionError(f"Unsupported collection contract: {generation_root}")
    if manifest.get("contract_version") != SHARDED_COLLECTION_VERSION:
        raise ShardedCollectionError(f"Unsupported collection contract version: {generation_root}")
    if manifest.get("logical_name") != logical_path.name:
        raise ShardedCollectionError(f"Collection logical name mismatch: {generation_root}")
    record_count = manifest.get("record_count")
    if not isinstance(record_count, int) or record_count < 0:
        raise ShardedCollectionError(f"Collection record count is invalid: {generation_root}")
    limits = manifest.get("limits")
    if not isinstance(limits, dict) or set(limits) != {
        "max_shard_bytes",
        "max_shard_records",
    }:
        raise ShardedCollectionError(f"Collection limits are missing: {generation_root}")
    for field in ("max_shard_bytes", "max_shard_records"):
        if not isinstance(limits.get(field), int) or limits[field] <= 0:
            raise ShardedCollectionError(f"Collection limit {field} is invalid: {generation_root}")
    if (
        limits["max_shard_bytes"] > MAX_SHARD_BYTES
        or limits["max_shard_records"] > MAX_SHARD_RECORDS
    ):
        raise ShardedCollectionError(f"Collection limits exceed the contract: {generation_root}")
    shards = manifest.get("shards")
    if not isinstance(shards, list):
        raise ShardedCollectionError(f"Collection shard catalog is invalid: {generation_root}")
    return shards


def _validate_generation(
    logical_path: Path,
    generation_root: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    shards = _validate_manifest_shape(logical_path, generation_root, manifest)
    limits = manifest["limits"]
    seen_paths: set[str] = set()
    seen_prefixes: set[str] = set()
    seen_keys: set[str] = set()
    seen_ordinals: set[int] = set()
    total_records = 0

    for shard in shards:
        if not isinstance(shard, dict) or set(shard) != {
            "prefix",
            "path",
            "record_count",
            "byte_count",
            "sha256",
            "min_ordinal",
            "max_ordinal",
        }:
            raise ShardedCollectionError(f"Collection shard entry is invalid: {generation_root}")
        relative_path = shard.get("path")
        prefix = shard.get("prefix")
        if (
            not isinstance(relative_path, str)
            or not relative_path.startswith("shards/")
            or Path(relative_path).is_absolute()
            or ".." in Path(relative_path).parts
        ):
            raise ShardedCollectionError(f"Collection shard path is invalid: {generation_root}")
        if relative_path in seen_paths:
            raise ShardedCollectionError(f"Duplicate collection shard path: {relative_path}")
        seen_paths.add(relative_path)
        if not isinstance(prefix, str) or not re.fullmatch(r"[a-f0-9]{2,64}", prefix):
            raise ShardedCollectionError(f"Collection shard prefix is invalid: {relative_path}")
        if relative_path != f"shards/{prefix}.jsonl":
            raise ShardedCollectionError(f"Collection shard path is invalid: {relative_path}")
        if prefix in seen_prefixes:
            raise ShardedCollectionError(f"Duplicate collection shard prefix: {prefix}")
        if any(
            prefix.startswith(existing_prefix) or existing_prefix.startswith(prefix)
            for existing_prefix in seen_prefixes
        ):
            raise ShardedCollectionError(f"Overlapping collection shard prefix: {prefix}")
        seen_prefixes.add(prefix)

        shard_path = generation_root / relative_path
        if shard_path.is_symlink() or not shard_path.is_file():
            raise ShardedCollectionError(f"Collection shard is missing: {shard_path}")
        raw = shard_path.read_bytes()
        if len(raw) != shard.get("byte_count") or len(raw) > limits["max_shard_bytes"]:
            raise ShardedCollectionError(f"Collection shard byte count is invalid: {shard_path}")
        if hashlib.sha256(raw).hexdigest() != shard.get("sha256"):
            raise ShardedCollectionError(f"Collection shard digest mismatch: {shard_path}")
        lines = raw.splitlines(keepends=True)
        if len(lines) != shard.get("record_count") or len(lines) > limits["max_shard_records"]:
            raise ShardedCollectionError(f"Collection shard record count is invalid: {shard_path}")
        previous_ordinal = -1
        for line in lines:
            if not line.endswith(b"\n"):
                raise ShardedCollectionError(f"Collection shard line lacks newline: {shard_path}")
            try:
                envelope = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ShardedCollectionError(
                    f"Collection shard contains malformed JSON: {shard_path}"
                ) from exc
            if not isinstance(envelope, dict) or set(envelope) != {"key", "ordinal", "record"}:
                raise ShardedCollectionError(
                    f"Collection shard envelope is malformed: {shard_path}"
                )
            key = envelope["key"]
            ordinal = envelope["ordinal"]
            record = envelope["record"]
            if not isinstance(key, str) or not key:
                raise ShardedCollectionError(f"Collection shard key is invalid: {shard_path}")
            if hashlib.sha256(key.encode("utf-8")).hexdigest()[: len(prefix)] != prefix:
                raise ShardedCollectionError(
                    f"Collection shard key is mispartitioned: {shard_path}"
                )
            if not isinstance(ordinal, int) or ordinal < 0 or ordinal <= previous_ordinal:
                raise ShardedCollectionError(f"Collection shard ordinals are invalid: {shard_path}")
            if not isinstance(record, dict):
                raise ShardedCollectionError(f"Collection record is not an object: {shard_path}")
            if key in seen_keys:
                raise ShardedCollectionError(f"Duplicate collection key: {key}")
            if ordinal in seen_ordinals:
                raise ShardedCollectionError(f"Duplicate collection ordinal: {ordinal}")
            if line != _encode_envelope(key, ordinal, record):
                raise ShardedCollectionError(
                    f"Collection shard envelope is not canonical: {shard_path}"
                )
            seen_keys.add(key)
            seen_ordinals.add(ordinal)
            previous_ordinal = ordinal
        if lines:
            if shard.get("min_ordinal") != json.loads(lines[0])["ordinal"]:
                raise ShardedCollectionError(
                    f"Collection shard minimum ordinal mismatch: {shard_path}"
                )
            if shard.get("max_ordinal") != json.loads(lines[-1])["ordinal"]:
                raise ShardedCollectionError(
                    f"Collection shard maximum ordinal mismatch: {shard_path}"
                )
        total_records += len(lines)

    shards_root = generation_root / "shards"
    if shards_root.is_symlink():
        raise ShardedCollectionError(f"Collection shards root may not be a symlink: {shards_root}")
    actual_shards = (
        {
            path.relative_to(generation_root).as_posix()
            for path in shards_root.rglob("*")
            if path.is_file()
        }
        if shards_root.exists()
        else set()
    )
    if actual_shards != seen_paths:
        raise ShardedCollectionError(
            f"Collection shard files do not match the manifest: {generation_root}"
        )

    if total_records != manifest["record_count"]:
        raise ShardedCollectionError(
            f"Collection manifest record total mismatch: {generation_root}"
        )
    if seen_ordinals != set(range(total_records)):
        raise ShardedCollectionError(
            f"Collection logical ordinals are incomplete: {generation_root}"
        )
    manifest_core = _manifest_core(
        logical_path,
        record_count=manifest["record_count"],
        shards=shards,
        max_shard_bytes=limits["max_shard_bytes"],
        max_shard_records=limits["max_shard_records"],
    )
    if _generation_id(manifest_core) != manifest.get("generation_id"):
        raise ShardedCollectionError(f"Collection generation digest mismatch: {generation_root}")
    return manifest


def validate_collection(logical_path: Path) -> dict[str, Any]:
    logical_path = logical_path.resolve(strict=False)
    generation_root, manifest = _resolve_current_generation(logical_path)
    return _validate_generation(logical_path, generation_root, manifest)


def _iter_generation_envelopes(
    generation_root: Path,
    manifest: dict[str, Any],
) -> Iterator[dict[str, Any]]:
    iterators: list[Iterator[dict[str, Any]]] = []
    heap: list[tuple[int, int, dict[str, Any]]] = []
    for shard in manifest["shards"]:
        path = generation_root / shard["path"]

        def iter_shard(shard_path: Path = path) -> Iterator[dict[str, Any]]:
            with shard_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    yield json.loads(line)

        iterator = iter_shard()
        iterator_index = len(iterators)
        iterators.append(iterator)
        first = next(iterator, None)
        if first is not None:
            heapq.heappush(heap, (first["ordinal"], iterator_index, first))

    while heap:
        _ordinal, iterator_index, envelope = heapq.heappop(heap)
        yield envelope
        following = next(iterators[iterator_index], None)
        if following is not None:
            heapq.heappush(
                heap,
                (following["ordinal"], iterator_index, following),
            )


def iter_collection(
    logical_path: Path,
    *,
    legacy_wrapper: str | None = None,
) -> Iterator[Any]:
    logical_path = logical_path.resolve(strict=False)
    if _collection_storage_exists(logical_path):
        manifest = validate_collection(logical_path)
        generation_root, _loaded_manifest = _resolve_current_generation(logical_path)
        for envelope in _iter_generation_envelopes(generation_root, manifest):
            yield envelope["record"]
        return
    if not logical_path.exists():
        return
    try:
        payload = json.loads(logical_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ShardedCollectionError(f"Legacy collection is malformed: {logical_path}") from exc
    if legacy_wrapper is not None:
        if not isinstance(payload, dict) or not isinstance(payload.get(legacy_wrapper), list):
            raise ShardedCollectionError(
                f"Legacy collection wrapper {legacy_wrapper!r} is malformed: {logical_path}"
            )
        payload = payload[legacy_wrapper]
    if not isinstance(payload, list):
        raise ShardedCollectionError(f"Legacy collection must be a JSON array: {logical_path}")
    yield from payload


def load_collection(
    logical_path: Path,
    *,
    legacy_wrapper: str | None = None,
    default: Any = None,
) -> Any:
    logical_path = logical_path.resolve(strict=False)
    if not _collection_storage_exists(logical_path) and not logical_path.exists():
        return default
    return list(iter_collection(logical_path, legacy_wrapper=legacy_wrapper))


def _write_stage_file(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _atomic_replace(source: Path, destination: Path) -> None:
    source.replace(destination)


def _existing_shard_by_prefix(
    logical_path: Path,
) -> tuple[Path | None, dict[str, dict[str, Any]]]:
    if not _collection_storage_exists(logical_path):
        return None, {}
    manifest = validate_collection(logical_path)
    generation_root, _loaded_manifest = _resolve_current_generation(logical_path)
    return generation_root, {item["prefix"]: item for item in manifest["shards"]}


def write_collection(
    logical_path: Path,
    records: Iterable[Any],
    *,
    key: Callable[[Any], str],
    authorization: CorpusWriteAuthorization | None = None,
    max_shard_bytes: int = MAX_SHARD_BYTES,
    max_shard_records: int = MAX_SHARD_RECORDS,
) -> dict[str, Any]:
    if max_shard_bytes <= 0 or max_shard_records <= 0:
        raise ShardedCollectionError("Shard limits must be positive integers.")
    if max_shard_bytes > MAX_SHARD_BYTES or max_shard_records > MAX_SHARD_RECORDS:
        raise ShardedCollectionError("Shard limits may not exceed the collection contract.")
    logical_path = logical_path.resolve(strict=False)
    storage_root = collection_storage_path(logical_path)
    if authorization is not None:
        from .corpus_store import authorize_additional_corpus_path  # noqa: PLC0415

        authorize_additional_corpus_path(authorization, storage_root)

    prepared = _prepare_records(records, key, max_shard_bytes=max_shard_bytes)
    buckets = _partition_records(
        prepared,
        max_shard_bytes=max_shard_bytes,
        max_shard_records=max_shard_records,
    )
    shard_payloads: dict[str, bytes] = {}
    shard_entries: list[dict[str, Any]] = []
    for prefix in sorted(buckets):
        items = sorted(buckets[prefix], key=lambda item: item["ordinal"])
        payload = b"".join(item["encoded"] for item in items)
        shard_payloads[prefix] = payload
        shard_entries.append(
            {
                "prefix": prefix,
                "path": f"shards/{prefix}.jsonl",
                "record_count": len(items),
                "byte_count": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "min_ordinal": items[0]["ordinal"],
                "max_ordinal": items[-1]["ordinal"],
            }
        )

    manifest_core = _manifest_core(
        logical_path,
        record_count=len(prepared),
        shards=shard_entries,
        max_shard_bytes=max_shard_bytes,
        max_shard_records=max_shard_records,
    )
    generation_id = _generation_id(manifest_core)
    manifest = {
        **manifest_core,
        "generation_id": generation_id,
    }

    previous_root, previous_shards = _existing_shard_by_prefix(logical_path)
    generations_root = collection_generations_path(logical_path)
    generation_root = generations_root / generation_id
    storage_root.mkdir(parents=True, exist_ok=True)
    generations_root.mkdir(parents=True, exist_ok=True)
    if generation_root.exists():
        existing_manifest = validate_collection_generation(logical_path, generation_root)
        if existing_manifest != manifest:
            raise ShardedCollectionError(
                f"Existing immutable generation does not match content: {generation_root}"
            )
    else:
        stage_root = generations_root / f".stage-{uuid.uuid4().hex}"
        try:
            for entry in shard_entries:
                prefix = entry["prefix"]
                stage_path = stage_root / entry["path"]
                previous = previous_shards.get(prefix)
                previous_path = (
                    previous_root / previous["path"]
                    if previous_root is not None and previous is not None
                    else None
                )
                if (
                    previous_path is not None
                    and previous.get("sha256") == entry["sha256"]
                    and previous_path.is_file()
                ):
                    stage_path.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        stage_path.hardlink_to(previous_path)
                    except OSError:
                        shutil.copyfile(previous_path, stage_path)
                else:
                    _write_stage_file(stage_path, shard_payloads[prefix])
            _write_stage_file(
                stage_root / "manifest.json",
                canonical_json_bytes(manifest) + b"\n",
            )
            validate_collection_generation(logical_path, stage_root)
            _atomic_replace(stage_root, generation_root)
        except Exception:
            if stage_root.exists():
                shutil.rmtree(stage_root)
            raise

    current_path = collection_current_path(logical_path)
    if current_path.exists() and current_path.read_text(encoding="utf-8").strip() == generation_id:
        validate_collection(logical_path)
        return manifest

    current_temp = storage_root / f".CURRENT-{uuid.uuid4().hex}"
    try:
        current_temp.write_text(f"{generation_id}\n", encoding="utf-8")
        _atomic_replace(current_temp, current_path)
    finally:
        if current_temp.exists():
            current_temp.unlink()
    validate_collection(logical_path)
    return manifest


def validate_collection_generation(
    logical_path: Path,
    generation_root: Path,
) -> dict[str, Any]:
    logical_path = logical_path.resolve(strict=False)
    manifest = _load_manifest(generation_root / "manifest.json")
    return _validate_generation(logical_path, generation_root, manifest)


def write_corpus_collection(
    logical_path: Path,
    records: Iterable[Any],
    *,
    authorization: CorpusWriteAuthorization | None = None,
) -> dict[str, Any]:
    return write_collection(
        logical_path,
        records,
        key=lambda record: corpus_collection_key(logical_path, record),
        authorization=authorization,
    )


def iter_corpus_collection(logical_path: Path) -> Iterator[Any]:
    legacy_wrapper = "threads" if logical_path.name == "semantic-v3-index.json" else None
    return iter_collection(logical_path, legacy_wrapper=legacy_wrapper)


def load_corpus_collection(logical_path: Path, *, default: Any = None) -> Any:
    legacy_wrapper = "threads" if logical_path.name == "semantic-v3-index.json" else None
    return load_collection(
        logical_path,
        legacy_wrapper=legacy_wrapper,
        default=default,
    )
