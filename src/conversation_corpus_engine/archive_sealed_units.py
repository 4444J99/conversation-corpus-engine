"""Lossless message-sized atoms, plus explicit imported authorship annotations.

No inferred human identity, semantic assertion, or invented original-writing attribution.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .archive_sealed_crypto import SealedError


def project_units(
    path: Path,
    relative: str,
    *,
    max_line_bytes: int | None = None,
) -> Iterator[dict[str, Any]]:
    # Only named normalized collections and explicit atomic exports are interpreted as JSONL.
    normalized = path.name.startswith("collection-") and path.name.endswith("-conversations.jsonl")
    normalized = normalized or bool(
        re.fullmatch(r"shard-[a-z0-9-]+-(?:[0-9]{6}|undated)(?:-[0-9]{3,})?\.jsonl", path.name)
    )
    explicit = path.name.endswith(".atoms.jsonl")
    if not normalized and not explicit:
        return
    if max_line_bytes is not None and (type(max_line_bytes) is not int or max_line_bytes < 1):
        raise SealedError("invalid-projection-bound")
    mode = "r" if max_line_bytes is None else "rb"
    with path.open(mode, encoding="utf-8" if max_line_bytes is None else None) as handle:
        read_limit = 0 if max_line_bytes is None else max_line_bytes + 1
        lines = handle if max_line_bytes is None else iter(lambda: handle.readline(read_limit), b"")
        for line in lines:
            if max_line_bytes is not None and len(line) > max_line_bytes:
                raise SealedError("projection-line-too-large")
            if not line.strip():
                continue
            row = json.loads(line)
            if explicit:
                if row.get("schema") != "cce-knowledge-atom.v1" or not all(
                    k in row
                    for k in ("id", "text", "authorship", "source", "timestamps", "derived_from")
                ):
                    raise SealedError("invalid-atom")
                yield row
                continue
            if row.get("schema") != "archive-conversation.v2":
                raise SealedError("normalized-v2-required")
            for branch, messages in (
                ("current", row["messages"]),
                ("alternate", row.get("extra", {}).get("alternate_messages", [])),
            ):
                for index, message in enumerate(messages):
                    meta = message.get("meta", {})
                    model = (
                        meta.get("model") or meta.get("model_slug") or ""
                        if message["role"] == "assistant"
                        else ""
                    )
                    yield {
                        "schema": "cce-knowledge-atom.v1",
                        "id": f"{row['uid']}#{branch}#{message['id']}",
                        "text": message["text"],
                        "message": message,
                        "authorship": {
                            "role": message["role"],
                            "provider": row["provider"],
                            "model": model,
                            "conversation_model": row.get("model", ""),
                            "model_evidence": "message-metadata" if model else "unverified",
                            "evidence": "export-role-only",
                            "original_writing": "unverified",
                            "prompt_owner": "unverified",
                            "typed_chars": message.get("typed_chars"),
                        },
                        "source": {
                            "path": relative,
                            "uid": row["uid"],
                            "branch": branch,
                            "index": index,
                            "provenance": row.get("provenance", {}),
                        },
                        "timestamps": {
                            "message": message.get("ts", ""),
                            "created": row.get("created", ""),
                            "updated": row.get("updated", ""),
                        },
                        "derived_from": [relative, row["uid"], message["id"]],
                    }
