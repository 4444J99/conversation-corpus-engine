"""Validate browser revisions before materializing source-owned CCE inputs.

This adapter never modifies a library generation, original source, agent memory
or generated wiki. It owns only a marked authoring directory with immutable
revision receipts and small current Markdown notes. Its source declaration is
consumed by the ordinary CCE candidate-build/promotion path.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .archive_sealed import safe_path
from .archive_sealed_crypto import authenticate, canonical, decrypt, digest
from .knovv_projection import _inflate

HEX = re.compile(r"[0-9a-f]{64}\Z")
PART_CHARS = 12_000


def _atomic(path: Path, raw: bytes) -> None:
    safe_path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    descriptor = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _notes(document: dict[str, Any], revision: dict, home: Path) -> None:
    identity, body = document["id"], document["body"]
    metadata = {
        key: document.get(key, "")
        for key in (
            "id",
            "kind",
            "title",
            "author",
            "authorship_basis",
            "status",
            "created",
            "updated",
            "sources",
            "links",
            "attachments",
        )
    }
    metadata.update(knovv_revision=revision["id"], knovv_device=revision["device"])
    header = (
        "---\n"
        + "\n".join(
            key + ": " + json.dumps(value, ensure_ascii=False) for key, value in metadata.items()
        )
        + "\n---\n\n"
    )
    if len(body) <= PART_CHARS:
        _atomic(home / "notes" / (identity + ".md"), (header + body).encode())
    else:
        names = []
        for number, start in enumerate(range(0, len(body), PART_CHARS), 1):
            name = f"{identity}/part-{number:05}.md"
            _atomic(home / "notes" / name, (header + body[start : start + PART_CHARS]).encode())
            names.append(name)
        hub = header + "\n".join(f"- [Part {i + 1}]({name})" for i, name in enumerate(names))
        _atomic(home / "notes" / (identity + ".md"), hub.encode())


def import_journal(
    journal: dict,
    read_object: Callable[[str], bytes],
    secret: bytes,  # allow-secret: runtime-only
    owner_public: str,
    home: Path,
    *,
    epoch_keys: dict[str, str] | None = None,
) -> dict:
    if (
        journal.get("schema") != "knovv-journal.v1"
        or not HEX.fullmatch(journal.get("vault", ""))
        or type(journal.get("epoch")) is not int
        or journal["epoch"] < 1
    ):
        raise ValueError("invalid-journal")

    def key_for(epoch):
        if epoch == journal["epoch"]:
            return secret
        value = (epoch_keys or {}).get(str(epoch), "")
        if (
            type(epoch) is not int
            or epoch < 1
            or epoch >= journal["epoch"]
            or not HEX.fullmatch(value)
        ):
            raise ValueError("historical-epoch-not-enrolled")
        return bytes.fromhex(value)

    home = safe_path(home)
    marker = home / ".knovv-authoring.json"
    if home.exists() and not marker.is_file():
        raise ValueError("unowned-authoring-directory")
    state = (
        json.loads(marker.read_bytes())
        if marker.is_file()
        else {"schema": "knovv-authoring.v1", "vault": journal["vault"], "cursor": 0, "heads": {}}
    )
    if state.get("schema") != "knovv-authoring.v1" or state.get("vault") != journal["vault"]:
        raise ValueError("wrong-authoring-vault")
    grants = {}
    for grant in journal.get("grants", []):
        authenticate(grant, owner_public)
        if (
            grant.get("schema") != "knovv-device.v1"
            or digest(grant["signing_public"].encode()) != grant["id"]
        ):
            raise ValueError("invalid-device-grant")
        if grant["id"] in grants:
            raise ValueError("duplicate-device-grant")
        grants[grant["id"]] = grant
    if not isinstance(journal.get("revisions"), list) or len(journal["revisions"]) > 10000:
        raise ValueError("journal-bound")
    planned = []
    attachments: dict[str, bytes] = {}
    planned_bytes = 0
    heads = dict(state["heads"])
    cursor = state["cursor"]
    seen = set()
    for entry in journal.get("revisions", []):
        revision = entry.get("envelope", {})
        sequence = entry.get("seq")
        if type(sequence) is not int or sequence < 1 or sequence in seen:
            raise ValueError("invalid-journal-sequence")
        seen.add(sequence)
        if revision.get("schema") != "knovv-revision.v1" or any(
            not HEX.fullmatch(revision.get(key, "")) for key in ("id", "doc", "object", "device")
        ):
            raise ValueError("invalid-revision")
        grant = grants.get(revision["device"])
        if grant is None:
            raise ValueError("unknown-device")
        authenticate(revision, grant["signing_public"])
        if sequence <= state["cursor"]:
            receipt = home / "history" / revision["doc"] / (revision["id"] + ".json")
            if not receipt.is_file() or json.loads(receipt.read_bytes())["envelope"] != revision:
                raise ValueError("journal-equivocation")
            continue
        if grant["revoked"]:
            raise ValueError("revoked-device-review-required")
        if sequence <= cursor or revision["parent"] != heads.get(revision["doc"]):
            raise ValueError("journal-chain-conflict")
        raw = read_object(revision["object"])
        if len(raw) > 48 * 1024 * 1024 or digest(raw) != revision["object"]:
            raise ValueError("journal-object-digest")
        body = _inflate(
            decrypt(
                key_for(revision["epoch"]),
                raw,
                f"knovv-revision-object:{journal['vault']}:{revision['epoch']}",
            )
        )
        document = json.loads(body)
        if (
            document.get("id") != revision["doc"]
            or document.get("kind") not in {"wiki", "project", "zettel", "capture"}
            or not isinstance(document.get("body"), str)
            or not isinstance(document.get("title"), str)
        ):
            raise ValueError("invalid-authoring-document")
        refs = document.get("attachments", [])
        if not isinstance(refs, list) or len(refs) > 8:
            raise ValueError("attachment-bound")
        planned_bytes += len(body)
        for ref in refs:
            oid = ref.get("object", "")
            if not HEX.fullmatch(oid):
                raise ValueError("invalid-attachment")
            if oid in attachments:
                continue
            cipher = read_object(oid)
            if len(cipher) > 5 * 1024 * 1024 or digest(cipher) != oid:
                raise ValueError("attachment-digest")
            plain = _inflate(
                decrypt(
                    key_for(ref.get("epoch")),
                    cipher,
                    f"knovv-attachment:{journal['vault']}:{ref['epoch']}",
                ),
                4 * 1024 * 1024,
            )
            if len(plain) != ref.get("size"):
                raise ValueError("attachment-size")
            attachments[oid] = plain
            planned_bytes += len(plain)
        if planned_bytes > 64 * 1024 * 1024:
            raise ValueError("journal-import-batch-bound")
        planned.append((entry, document))
        heads[revision["doc"]] = revision["id"]
        cursor = sequence
    # Validation completes before creating or changing anything in the authoring home.
    if not planned and not home.exists():
        return {"imported": 0, "cursor": cursor}
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not marker.exists():
        _atomic(marker, canonical(state))
    for oid, plain in attachments.items():
        _atomic(home / "attachments" / oid, plain)
    for entry, document in planned:
        revision = entry["envelope"]
        receipt = home / "history" / document["id"] / (revision["id"] + ".json")
        value = canonical({"envelope": revision, "document": document, "seq": entry["seq"]})
        if receipt.exists() and receipt.read_bytes() != value:
            raise ValueError("immutable-receipt-conflict")
        if not receipt.exists():
            _atomic(receipt, value)
        _notes(document, revision, home)
        state["heads"][document["id"]] = revision["id"]
        state["cursor"] = entry["seq"]
        _atomic(marker, canonical(state))
    # Old part files remain preserved; only parts in current heads are ingested.
    active_paths = []
    for identity, revision_id in state["heads"].items():
        current = json.loads((home / "history" / identity / (revision_id + ".json")).read_bytes())[
            "document"
        ]
        active_paths.append(identity + ".md")
        if len(current["body"]) > PART_CHARS:
            active_paths.extend(
                f"{identity}/part-{number:05}.md"
                for number in range(1, (len(current["body"]) + PART_CHARS - 1) // PART_CHARS + 1)
            )
    declaration = {
        "schema": "cce.library-sources.v1",
        "documents": [
            {
                "id": "knovv-" + journal["vault"][:16],
                "root": str(home / "notes"),
                "include": sorted(active_paths),
                "wing": "authored",
                "lane": "research",
                "kind": "document",
                "max_bytes": 65536,
            }
        ],
    }
    _atomic(home / "source.json", canonical(declaration))
    return {
        "imported": len(planned),
        "cursor": state["cursor"],
        "source_manifest": str(home / "source.json"),
    }
