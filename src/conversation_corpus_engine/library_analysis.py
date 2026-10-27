"""Bounded native analysis projections of canonical library revisions.

The targets receive structural text atoms, not inferred concepts. Original
records remain authoritative; every occurrence retains its revision and spans.
No optional analysis package is imported by the producer.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
import zlib
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .corpus_store import authorize_corpus_write
from .library_blocks import iter_blocks
from .library_store import Library

SCHEMA = "cce.library-analysis.v1"
TARGETS = {
    "lingframe": {
        "repository": "4444J99/linguistic-atomization-framework",
        "commit": "9580334ec32a6cb175fc6c398975780e5b80e00f",
        "loader": "framework.core.ontology.Corpus.from_dict",
    },
    "uaks": {
        "repository": "4444J99/atomic-substrata",
        "commit": "3e44b865bd9b144f6227dcb134030e1aa4c0af8e",
        "loader": "scripts.graph_storage.GraphStorage",
    },
}


@dataclass(frozen=True)
class AnalysisLimits:
    max_source_bytes: int = 8 * 1024 * 1024
    max_output_bytes: int = 32 * 1024 * 1024
    max_atoms: int = 10000
    max_seconds: float = 30

    def validate(self) -> None:
        for name in ("max_source_bytes", "max_output_bytes", "max_atoms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if (
            isinstance(self.max_seconds, bool)
            or not isinstance(self.max_seconds, (int, float))
            or not math.isfinite(self.max_seconds)
            or self.max_seconds <= 0
        ):
            raise ValueError("max_seconds must be a finite positive number")


class _Budget:
    def __init__(self, limits: AnalysisLimits) -> None:
        limits.validate()
        self.limits = limits
        self.deadline = time.monotonic() + limits.max_seconds
        self.source_bytes = 0
        self.atoms = 0

    def check(self, *, atoms: int = 0) -> None:
        if time.monotonic() >= self.deadline:
            raise TimeoutError("analysis time budget exceeded")
        self.atoms += atoms
        if self.atoms > self.limits.max_atoms:
            raise ValueError("analysis atom budget exceeded")

    def encode(self, value: Any, remaining: int) -> bytes:
        output = bytearray()
        encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for chunk in encoder.iterencode(value):
            self.check()
            output.extend(chunk.encode("utf-8"))
            if len(output) + 1 > remaining:
                raise ValueError("analysis output byte budget exceeded")
        output.extend(b"\n")
        return bytes(output)


def _read_record(library: Library, uid: str, budget: _Budget) -> tuple[dict, dict]:
    budget.check()
    selected = library.connection.execute(
        "SELECT r.*,o.raw_bytes,length(o.payload) AS compressed_bytes "
        "FROM records r JOIN objects o ON o.digest=r.current_digest WHERE r.uid=?",
        (uid,),
    ).fetchone()
    if selected is None:
        raise KeyError(f"library record not found: {uid}")
    row = dict(selected)
    remaining = budget.limits.max_source_bytes - budget.source_bytes
    if row["raw_bytes"] > remaining or row["compressed_bytes"] > remaining:
        raise ValueError("analysis source byte budget exceeded; choose fewer or smaller records")
    blob = library.connection.execute(
        "SELECT payload FROM objects WHERE digest=?", (row["current_digest"],)
    ).fetchone()[0]
    decoder = zlib.decompressobj()
    raw = decoder.decompress(blob, remaining + 1)
    if len(raw) > remaining or decoder.unconsumed_tail or not decoder.eof:
        raise ValueError("analysis decoded source byte budget exceeded")
    if decoder.unused_data or len(raw) != row["raw_bytes"]:
        raise ValueError("library source size or compression integrity mismatch")
    if hashlib.sha256(raw).hexdigest() != row["current_digest"]:
        raise ValueError("library source digest mismatch")
    budget.source_bytes += len(raw)
    budget.check()
    record = json.loads(raw)
    if record.get("uid") != uid:
        raise ValueError("library source identity mismatch")
    return row, record


def _node(identity: str, kind: str, content: str, source: str, metadata: dict) -> dict:
    return {
        "id": identity,
        "type": kind,
        "hash": hashlib.sha256(content.encode()).hexdigest(),
        "content": content,
        "source": source,
        "created_at": "",
        "metadata": metadata,
    }


def _edge(identity: str, source: str, target: str, kind: str, metadata: dict) -> dict:
    return {
        "id": identity,
        "source_id": source,
        "target_id": target,
        "relationship": kind,
        "weight": 1.0,
        "created_at": "",
        "metadata": metadata,
    }


def _project_record(row: dict, record: dict, graph: dict, budget: _Budget) -> tuple[dict, dict]:
    uid, digest = row["uid"], row["current_digest"]
    prefix = f"cce:{uid}@{digest}"
    cce = {
        "uid": uid,
        "version": row["current_version"],
        "digest": digest,
        "content_digest": row["content_digest"],
        "source_ref": row["source_ref"],
        "provider": row["provider"],
        "kind": row["kind"],
        "wing": row["wing"],
        "lane": row["lane"],
        "body_status": row["body_status"],
        "provenance": record.get("provenance", {}),
        "extra": {
            key: value
            for key, value in record.get("extra", {}).items()
            if key != "alternate_messages"
        },
    }
    document = {
        "id": uid,
        "source_path": row["source_ref"] or prefix,
        "format": "cce-normalized",
        "title": row["title"],
        "author": None,
        "themes": [],
        "metadata": {"cce": cce},
    }
    graph["nodes"][prefix] = _node(prefix, "record", "", row["source_ref"], {"cce": cce})
    counts = {"messages": 0, "blocks": 0}
    budget.check(atoms=1)
    for sequence, messages in (
        ("main", record.get("messages", [])),
        ("alternate", record.get("extra", {}).get("alternate_messages", [])),
    ):
        for message_index, message in enumerate(messages):
            budget.check(atoms=1)
            message_id = f"{prefix}/{sequence}/{message_index}"
            text = message.get("text", "")
            metadata = {
                "uid": uid,
                "version": row["current_version"],
                "digest": digest,
                "sequence": sequence,
                "message_index": message_index,
                "message_id": message.get("id", ""),
                "role": message.get("role", ""),
                "channel": message.get("channel", ""),
                "timestamp": message.get("ts", ""),
                "source_ref": row["source_ref"],
                "structural_role": "message",
                "attachments": message.get("attachments", []),
                "message_meta": message.get("meta", {}),
            }
            theme = {
                "id": message_id,
                "level": "theme",
                "text": "",
                "metadata": metadata,
                "paragraphs": [],
            }
            document["themes"].append(theme)
            graph["nodes"][message_id] = _node(message_id, "message", "", prefix, metadata)
            graph["edges"][message_id + "/part_of"] = _edge(
                message_id + "/part_of", message_id, prefix, "part_of", {}
            )
            counts["messages"] += 1
            for block_index, block in enumerate(iter_blocks(text, check_callback=budget.check)):
                budget.check(atoms=1)
                occurrence_id = f"{message_id}/block/{block_index}"
                occurrence = {
                    "uid": uid,
                    "version": row["current_version"],
                    "digest": digest,
                    "sequence": sequence,
                    "message_index": message_index,
                    "message_id": message.get("id", ""),
                    "block_index": block_index,
                    "start": block["start"],
                    "end": block["end"],
                    "offset_unit": "python-codepoint",
                    "kind": block["kind"],
                    "raw_digest": block["raw_digest"],
                    "canonical_digest": block["digest"],
                    "normalization": block["normalization"],
                }
                theme["paragraphs"].append(
                    {
                        "id": occurrence_id,
                        "level": "paragraph",
                        "text": block["text"],
                        "parent_id": message_id,
                        "theme_id": message_id,
                        "metadata": occurrence,
                    }
                )
                block_id = "cce:block:" + block["raw_digest"]
                if block_id not in graph["nodes"]:
                    graph["nodes"][block_id] = _node(
                        block_id,
                        "code" if block["kind"] == "code" else "text",
                        block["text"],
                        block_id,
                        {"raw_digest": block["raw_digest"], "content_addressed": True},
                    )
                graph["edges"][occurrence_id] = _edge(
                    occurrence_id, message_id, block_id, "occurrence", occurrence
                )
                counts["blocks"] += 1
    return document, counts


def _payloads(library: Library, selected: list[str], budget: _Budget) -> tuple[dict, dict]:
    graph: dict[str, Any] = {
        "nodes": {},
        "edges": {},
        "last_updated": "",
        "metadata": {"schema": SCHEMA, "authority": "cce"},
    }
    corpus: dict[str, Any] = {
        "corpus": {
            "name": "CCE selected library revisions",
            "schema": "cce-message-blocks",
            "analysis": "structural-only",
            "identity_authority": "cce",
        },
        "documents": [],
    }
    counts = {"records": 0, "messages": 0, "blocks": 0}
    coverage: Counter[str] = Counter()
    revisions = []
    for uid in selected:
        row, record = _read_record(library, uid, budget)
        document, added = _project_record(row, record, graph, budget)
        corpus["documents"].append(document)
        counts["records"] += 1
        for name, value in added.items():
            counts[name] += value
        coverage[row["body_status"]] += 1
        revisions.append(
            {"uid": uid, "version": row["current_version"], "digest": row["current_digest"]}
        )
    return {"lingframe": corpus, "uaks": graph}, {
        "counts": counts,
        "coverage": dict(coverage),
        "revisions": revisions,
    }


def export_analysis(
    library: Library,
    destination: Path,
    *,
    project_root: Path,
    store_root: Path,
    query: str | None = None,
    uids: Sequence[str] = (),
    limit: int = 5,
    target: str = "both",
    limits: AnalysisLimits | None = None,
) -> dict[str, Any]:
    """Write a new private native projection and return only its bounded receipt.

    Exactly one selection mode is required. Query selection reports remaining
    matches; any source/atom/output/time budget excess fails without publishing
    partial data. Interrupted filesystem writes may leave an unaccepted folder,
    which is preserved and cannot be overwritten by a retry.
    """
    if library.writable:
        raise ValueError("analysis requires a read-only Library")
    if target not in {*TARGETS, "both"}:
        raise ValueError("target must be lingframe, uaks or both")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer from 1 to 100")
    if query is not None and (not isinstance(query, str) or not query.strip()):
        raise ValueError("query must contain non-whitespace text")
    if isinstance(uids, str) or (query is not None) == bool(uids):
        raise ValueError("choose one nonempty query or explicit UID selection")
    if len(uids) > limit or len(set(uids)) != len(uids):
        raise ValueError("explicit UIDs must be unique and fit the record limit")
    budget = _Budget(limits or AnalysisLimits())
    authorized = authorize_corpus_write(
        project_root=project_root,
        destination=destination,
        corpus_store_root=store_root,
        source_roots=(
            library.path if library.path.parent == store_root.resolve() else library.path.parent,
        ),
    )
    destination = authorized.destination
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    connection = library.connection
    connection.execute("SAVEPOINT analysis_read")
    try:
        with library.sql_progress(budget.check, instructions=1000):
            if query is not None:
                result = library.search(query, limit=limit)
                selected = [hit["uid"] for hit in result["hits"]]
                total = result["total"]
            else:
                selected, total = list(uids), len(uids)
            payloads, measurements = _payloads(library, selected, budget)
    finally:
        connection.execute("ROLLBACK TO analysis_read")
        connection.execute("RELEASE analysis_read")
    names = list(TARGETS) if target == "both" else [target]
    files = {}
    for name in names:
        remaining = budget.limits.max_output_bytes - sum(len(value) for value in files.values())
        files[name + ".json"] = budget.encode(payloads[name], remaining)
    receipt = {
        "schema": SCHEMA,
        "status": "exported",
        "output": str(destination),
        "source_library": str(library.path),
        "targets": {name: TARGETS[name] for name in names},
        "selection": {
            "mode": "query" if query is not None else "uids",
            "matched_records": total,
            "selected_records": len(selected),
            "remaining_records": total - len(selected),
            "limit": limit,
        },
        **measurements,
        "source_bytes": budget.source_bytes,
        "structural_atoms": budget.atoms,
        "limits": asdict(budget.limits),
        "source_mutated": False,
        "semantic_analysis_performed": False,
        "files": {
            name: {"sha256": hashlib.sha256(value).hexdigest(), "bytes": len(value)}
            for name, value in files.items()
        },
    }
    remaining = budget.limits.max_output_bytes - sum(len(value) for value in files.values())
    files["receipt.json"] = budget.encode(receipt, remaining)
    budget.check()
    for path in reversed(destination.parents):
        if path == authorized.store_root or not path.is_relative_to(authorized.store_root):
            continue
        if not path.exists():
            path.mkdir(mode=0o700)
    destination.mkdir(mode=0o700)
    for name, value in files.items():
        budget.check()
        descriptor = os.open(
            destination / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
    return receipt
