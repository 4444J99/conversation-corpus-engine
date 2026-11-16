"""Bounded, sequential verification and restoration of a packed KNOVV graph.

Private scratch tables contain identifiers and offsets, never document bodies.
Sorted covering indexes avoid random slab/locator reads for a multi-million-message
restore. Every signed locator and document binding is checked before output exists.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import tempfile
import time
from pathlib import Path

from .archive_sealed import safe_path
from .archive_sealed_crypto import canonical, derive
from .knovv_projection import METADATA_PAGE, PLAIN_LIMIT, _Reader

ID = re.compile(r"[0-9a-f]{64}\Z")


class Output:
    def __init__(self, destination: Path, layout: str):
        self.destination = safe_path(destination)
        self.destination.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.layout = layout
        self.stream = None
        self.files = []
        self.count = self.size = self.part_count = 0
        self.hash = hashlib.sha256()

    def _close(self):
        if self.stream is not None:
            self.stream.flush()
            os.fsync(self.stream.fileno())
            self.stream.close()
            self.files.append(
                {
                    "file": self.name,
                    "bytes": self.size,
                    "documents": self.part_count,
                    "sha256": self.hash.hexdigest(),
                }
            )
            self.stream = None

    def write(self, identity: str, raw: bytes):
        if self.layout == "directory":
            fd = os.open(
                self.destination / (identity + ".json"), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
        else:
            if self.stream is not None and self.size + len(raw) + 1 > 32 * 1024 * 1024:
                self._close()
            if self.stream is None:
                self.name = f"documents-{len(self.files):06}.jsonl"
                fd = os.open(
                    self.destination / self.name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                )
                self.stream = os.fdopen(fd, "wb")
                self.size = self.part_count = 0
                self.hash = hashlib.sha256()
            line = raw + b"\n"
            self.stream.write(line)
            self.hash.update(line)
            self.size += len(line)
            self.part_count += 1
        self.count += 1

    def finish(self):
        self._close()
        result = {"documents": self.count, "layout": self.layout}
        if self.layout == "jsonl":
            receipt = {"schema": "knovv-restore.v1", **result, "files": self.files}
            fd = os.open(
                self.destination / "restore.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
            with os.fdopen(fd, "wb") as stream:
                stream.write(canonical(receipt))
                stream.flush()
                os.fsync(stream.fileno())
        return result


def restore_packed(
    root: Path,
    destination: Path,
    manifest: dict,
    secret: bytes,  # allow-secret: runtime-only
    *,
    layout: str,
    deadline: float,
):
    reader = _Reader(root, manifest, secret)
    address_key = derive(secret, b"address")
    metadata_keys = manifest.get("metadata")
    if not isinstance(metadata_keys, list) or len(set(metadata_keys)) != len(metadata_keys):
        raise ValueError("invalid-metadata-pages")
    metadata_order = {key: number for number, key in enumerate(metadata_keys)}
    pending_pages = set(metadata_keys)
    auxiliary = set(manifest.get("search", {}).values()) | set(manifest.get("lookup", {}).values())

    def check():
        if time.monotonic() > deadline:
            raise ValueError("restore-deadline")

    destination = safe_path(destination)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="knovv-restore-", dir=destination.parent) as temporary:
        Path(temporary).chmod(0o700)
        db = sqlite3.connect(str(Path(temporary) / "verification.sqlite"))
        db.execute("PRAGMA cache_size=-8192")
        db.execute("PRAGMA temp_store=FILE")
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        db.execute("CREATE TABLE refs(key BLOB, object BLOB, offset INTEGER, length INTEGER)")
        db.execute("CREATE TABLE observed(key BLOB, object BLOB, offset INTEGER, length INTEGER)")
        db.execute("CREATE TABLE metadata(key BLOB, id BLOB, ordinal INTEGER)")
        db.execute(
            "CREATE TABLE documents(key BLOB, object BLOB, offset INTEGER, length INTEGER, id BLOB)"
        )
        data_oids = set()
        try:
            for prefix, oid in manifest["resolve"].items():
                check()
                mapping = json.loads(reader.direct(oid))
                if not isinstance(mapping, dict):
                    raise ValueError("invalid-object-locator")
                rows = []
                for key, ref in mapping.items():
                    if (
                        not ID.fullmatch(key)
                        or key[:2] != prefix
                        or not isinstance(ref, list)
                        or len(ref) != 3
                        or not isinstance(ref[0], str)
                        or not ID.fullmatch(ref[0])
                        or type(ref[1]) is not int
                        or type(ref[2]) is not int
                        or ref[1] < 8
                        or ref[2] < 1
                        or ref[1] + ref[2] > PLAIN_LIMIT
                    ):
                        raise ValueError("invalid-packed-offset")
                    rows.append((bytes.fromhex(key), bytes.fromhex(ref[0]), ref[1], ref[2]))
                    data_oids.add(ref[0])
                db.executemany("INSERT INTO refs VALUES (?,?,?,?)", rows)
            for oid in sorted(data_oids):
                check()
                slab = reader.direct(oid)
                offset = 0
                observed = []
                documents = []
                while offset < len(slab):
                    if offset + 8 > len(slab):
                        raise ValueError("invalid-packed-frame")
                    length = int.from_bytes(slab[offset : offset + 8], "big")
                    offset += 8
                    if not 0 < length <= PLAIN_LIMIT or offset + length > len(slab):
                        raise ValueError("invalid-packed-frame")
                    raw = slab[offset : offset + length]
                    key = hmac.new(address_key, raw, hashlib.sha256).digest()
                    key_hex = key.hex()
                    value = json.loads(raw)
                    observed.append((key, bytes.fromhex(oid), offset, length))
                    if key_hex in metadata_order:
                        page = metadata_order[key_hex]
                        if not isinstance(value, list) or len(value) > METADATA_PAGE:
                            raise ValueError("invalid-metadata-page")
                        if key_hex in pending_pages:
                            for number, meta in enumerate(value):
                                if (
                                    not isinstance(meta, dict)
                                    or not ID.fullmatch(meta.get("id", ""))
                                    or not ID.fullmatch(meta.get("object", ""))
                                    or meta.get("ordinal") != page * METADATA_PAGE + number
                                ):
                                    raise ValueError("projection-document-mismatch")
                                db.execute(
                                    "INSERT INTO metadata VALUES (?,?,?)",
                                    (
                                        bytes.fromhex(meta["object"]),
                                        bytes.fromhex(meta["id"]),
                                        meta["ordinal"],
                                    ),
                                )
                            pending_pages.remove(key_hex)
                    if key_hex in auxiliary:
                        auxiliary.remove(key_hex)
                    if (
                        isinstance(value, dict)
                        and isinstance(value.get("id"), str)
                        and ID.fullmatch(value["id"])
                        and isinstance(value.get("body"), str)
                        and isinstance(value.get("title"), str)
                        and value.get("kind") in {"wiki", "project", "zettel", "capture", "source"}
                    ):
                        documents.append(
                            (key, bytes.fromhex(oid), offset, length, bytes.fromhex(value["id"]))
                        )
                    offset += length
                db.executemany("INSERT INTO observed VALUES (?,?,?,?)", observed)
                db.executemany("INSERT INTO documents VALUES (?,?,?,?,?)", documents)
            if pending_pages or auxiliary:
                raise ValueError("projection-missing-object")
            if db.execute("SELECT COUNT(*) FROM metadata").fetchone()[0] != manifest["count"]:
                raise ValueError("projection-count-mismatch")
            db.execute("CREATE INDEX refs_order ON refs(key,object,offset,length)")
            db.execute("CREATE INDEX observed_order ON observed(key,object,offset,length)")
            db.execute("CREATE INDEX metadata_order ON metadata(key,id,ordinal)")
            db.execute("CREATE UNIQUE INDEX metadata_id ON metadata(id)")
            db.execute("CREATE INDEX documents_order ON documents(key,object,offset,length,id)")
            if db.execute(
                "SELECT r.key FROM refs r INDEXED BY refs_order LEFT JOIN observed o INDEXED BY observed_order ON r.key=o.key AND r.object=o.object AND r.offset=o.offset AND r.length=o.length WHERE o.key IS NULL LIMIT 1"
            ).fetchone():
                raise ValueError("invalid-packed-address")
            joins = "FROM metadata m INDEXED BY metadata_order LEFT JOIN refs r INDEXED BY refs_order ON m.key=r.key LEFT JOIN documents d INDEXED BY documents_order ON d.key=r.key AND d.object=r.object AND d.offset=r.offset AND d.length=r.length"
            if db.execute(
                "SELECT m.id " + joins + " WHERE d.id IS NULL OR d.id<>m.id LIMIT 1"
            ).fetchone():
                raise ValueError("projection-document-mismatch")
            check()
            output = Output(destination, layout)
            previous = None
            slab = b""
            try:
                for identity, oid, offset, length in db.execute(
                    "SELECT m.id,r.object,r.offset,r.length "
                    + joins
                    + " ORDER BY r.object,r.offset"
                ):
                    if oid != previous:
                        check()
                        slab = reader.direct(oid.hex())
                        previous = oid
                    output.write(identity.hex(), slab[offset : offset + length])
                return output.finish()
            finally:
                output._close()
        except sqlite3.OperationalError as error:
            if time.monotonic() > deadline:
                raise ValueError("restore-deadline") from error
            raise
        finally:
            db.close()
