"""Versioned compressed storage over the existing sealed cryptographic primitives.

V2 shards contain length-framed logical objects. Only the encrypted manifest
maps object IDs to offsets. Compression happens before encryption; addresses
remain keyed HMACs, never public plaintext hashes. Pending ciphertext is staged
privately on disk, so memory depends on a slab and active codec buffers, not on
the size of the complete publication.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import zlib
from collections import defaultdict
from contextlib import nullcontext
from functools import partial
from pathlib import Path
from typing import Any

from . import archive_sealed as sealed
from .archive_limits import DATA_CEILING
from .archive_sealed_crypto import Identity, SealedError
from .archive_sealed_units import project_units

FORMAT = "cce-sealed.v2"
CODEC = "zlib"
PLAIN_LIMIT = 32 * 1024 * 1024
PROJECTION_LINE_LIMIT = DATA_CEILING
SHARD_LIMIT = 48 * 1024 * 1024
FRAME_BYTES = 8


def shard_path(repo: Path, sid: str) -> Path:
    if not isinstance(sid, str) or not sealed.HEX.fullmatch(sid):
        raise SealedError("invalid-packed-manifest")
    return sealed.safe_path(repo / "shards" / sid[:2] / sid)


def _integer(value: Any, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def validate_manifest(manifest: dict) -> dict[str, list[str]]:
    """Validate all offsets before decrypting or allocating a slab."""
    fields = {"format", "files", "atoms", "objects", "shards", "recipients"}
    if (
        not isinstance(manifest, dict)
        or set(manifest) != fields
        or manifest["format"] != FORMAT
        or any(not isinstance(manifest[k], dict) for k in ("files", "atoms", "objects", "shards"))
        or not isinstance(manifest["recipients"], list)
    ):
        raise SealedError("invalid-packed-manifest")
    shards, objects = manifest["shards"], manifest["objects"]
    for sid, info in shards.items():
        if (
            not isinstance(sid, str)
            or not sealed.HEX.fullmatch(sid)
            or not isinstance(info, dict)
            or set(info) != {"cipher_sha256", "codec", "compressed_bytes", "plain_bytes"}
            or info["codec"] != CODEC
            or not isinstance(info["cipher_sha256"], str)
            or not sealed.HEX.fullmatch(info["cipher_sha256"])
            or not _integer(info["compressed_bytes"], 1, SHARD_LIMIT)
            or not _integer(info["plain_bytes"], FRAME_BYTES + 1, PLAIN_LIMIT)
        ):
            raise SealedError("invalid-packed-manifest")
    groups: dict[str, list[str]] = defaultdict(list)
    for oid, info in objects.items():
        if (
            not isinstance(oid, str)
            or not sealed.HEX.fullmatch(oid)
            or not isinstance(info, dict)
            or set(info) != {"shard", "offset", "length"}
            or not isinstance(info["shard"], str)
            or info["shard"] not in shards
            or not _integer(info["offset"], FRAME_BYTES, PLAIN_LIMIT)
            or not _integer(info["length"], 1, sealed.MAX_UNIT)
            or info["offset"] + info["length"] > shards[info["shard"]]["plain_bytes"]
        ):
            raise SealedError("invalid-packed-manifest")
        groups[info["shard"]].append(oid)
    if set(groups) != set(shards):
        raise SealedError("invalid-packed-manifest")
    for sid, ids in groups.items():
        ids.sort(key=lambda oid: objects[oid]["offset"])
        end = 0
        for oid in ids:
            info = objects[oid]
            if info["offset"] - FRAME_BYTES != end:
                raise SealedError("invalid-packed-manifest")
            end = info["offset"] + info["length"]
        if end != shards[sid]["plain_bytes"]:
            raise SealedError("invalid-packed-manifest")
    for name, recipe in manifest["files"].items():
        sealed._relative(name)
        if (
            not isinstance(recipe, dict)
            or set(recipe) != {"chunks", "sha256"}
            or not isinstance(recipe["chunks"], list)
            or any(not isinstance(oid, str) or oid not in objects for oid in recipe["chunks"])
            or not isinstance(recipe["sha256"], str)
            or not sealed.HEX.fullmatch(recipe["sha256"])
        ):
            raise SealedError("invalid-packed-manifest")
    for unit_id, oid in manifest["atoms"].items():
        if (
            not isinstance(unit_id, str)
            or not unit_id
            or not isinstance(oid, str)
            or oid not in objects
        ):
            raise SealedError("invalid-packed-manifest")
    return dict(groups)


def read_cipher(path: Path) -> bytes:
    try:
        descriptor = os.open(
            sealed.safe_path(path),
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise SealedError("packed-shard-not-regular")
            data = handle.read(SHARD_LIMIT + 1)
        if len(data) > SHARD_LIMIT:
            raise SealedError("packed-shard-too-large")
        return data
    except OSError as exc:
        raise SealedError("missing-packed-shard") from exc


def _inflate(compressed: bytes, expected: int) -> bytes:
    if not _integer(expected, FRAME_BYTES + 1, PLAIN_LIMIT):
        raise SealedError("packed-shard-invalid")
    try:
        decoder = zlib.decompressobj()
        plain = decoder.decompress(compressed, expected + 1)
        if (
            len(plain) != expected
            or not decoder.eof
            or decoder.unused_data
            or decoder.unconsumed_tail
        ):
            raise SealedError("packed-shard-invalid")
        return plain
    except zlib.error as exc:
        raise SealedError("packed-shard-invalid") from exc


def read_shard(repo: Path, sid: str, info: dict, secret: bytes) -> bytes:
    data = read_cipher(shard_path(repo, sid))
    if sealed.digest(data) != info["cipher_sha256"]:
        raise SealedError("corrupt-packed-shard")
    compressed = sealed.decrypt(secret, data[64:], FORMAT + ":shard:" + sid)
    if len(compressed) != info["compressed_bytes"]:
        raise SealedError("packed-shard-invalid")
    plain = _inflate(compressed, info["plain_bytes"])
    if sealed.address(secret, plain) != sid:
        raise SealedError("packed-shard-invalid")
    return plain


class Reader:
    """One decoded slab cache. Bulk validation visits each slab as a group."""

    def __init__(self, repo: Path, manifest: dict, secret: bytes):
        self.repo, self.manifest, self.secret = repo, manifest, secret
        self.groups = validate_manifest(manifest)
        self.cached_id: str | None = None
        self.cached = b""

    def shard(self, sid: str) -> bytes:
        if self.cached_id != sid:
            # Drop the previous decoded slab before allocating another.
            self.cached = b""
            self.cached_id = None
            self.cached = read_shard(self.repo, sid, self.manifest["shards"][sid], self.secret)
            self.cached_id = sid
        return self.cached

    def object(self, oid: str) -> bytes:
        info = self.manifest["objects"][oid]
        plain = self.shard(info["shard"])
        offset, length = info["offset"], info["length"]
        if int.from_bytes(plain[offset - FRAME_BYTES : offset], "big") != length:
            raise SealedError("packed-shard-invalid")
        data = plain[offset : offset + length]
        if sealed.address(self.secret, data) != oid:
            raise SealedError("invalid-address")
        return data


def check(repo: Path, manifest: dict, secret: bytes) -> None:
    reader = Reader(repo, manifest, secret)
    atoms: dict[str, list[str]] = defaultdict(list)
    for unit_id, oid in manifest["atoms"].items():
        atoms[oid].append(unit_id)
    # Manifest keys are canonically sorted by hash. Iterating them directly
    # would repeatedly decode large slabs for adjacent tiny objects.
    for ids in reader.groups.values():
        for oid in ids:
            data = reader.object(oid)
            if oid in atoms:
                try:
                    unit = json.loads(data)
                    if not isinstance(unit, dict) or any(
                        unit.get("id") != name for name in atoms[oid]
                    ):
                        raise SealedError("unit-id-mismatch")
                except (ValueError, UnicodeError) as exc:
                    if isinstance(exc, SealedError):
                        raise
                    raise SealedError("invalid-atom") from exc
    for recipe in manifest["files"].values():
        checksum = hashlib.sha256()
        for oid in recipe["chunks"]:
            checksum.update(reader.object(oid))
        if checksum.hexdigest() != recipe["sha256"]:
            raise SealedError("reconstruction-failed")


class _Packer:
    def __init__(
        self,
        repo: Path,
        secret: bytes,
        identity: Identity,
        trust: dict,
        stage: Path | None,
        budget: int,
        plan_only: bool,
    ) -> None:
        self.repo, self.secret, self.identity, self.trust = repo, secret, identity, trust
        self.stage, self.budget, self.plan_only = stage, budget, plan_only
        self.slab = bytearray()
        self.slab_ids: list[str] = []
        self.objects: dict[str, dict] = {}
        self.shards: dict[str, dict] = {}
        self.pending: dict[str, Path] = {}
        self.pending_bytes = 0
        self.new_shards = 0
        self.maximum_shard_bytes = 0
        self.metadata_bytes = 0

    def add(self, data: bytes) -> str:
        if not data or len(data) > sealed.MAX_UNIT or len(data) + FRAME_BYTES > PLAIN_LIMIT:
            raise SealedError("unit-too-large")
        oid = sealed.address(self.secret, data)
        if oid in self.objects:
            return oid
        if len(self.slab) + FRAME_BYTES + len(data) > PLAIN_LIMIT:
            self.flush()
        offset = len(self.slab) + FRAME_BYTES
        self.slab.extend(len(data).to_bytes(FRAME_BYTES, "big"))
        self.slab.extend(data)
        self.slab_ids.append(oid)
        self.objects[oid] = {"shard": "", "offset": offset, "length": len(data)}
        # Bound metadata growth before building the final canonical manifest.
        self.metadata_bytes += len(oid) + 64 + len(sealed.canonical(self.objects[oid])) + 8
        if self.metadata_bytes > sealed.MAX_MANIFEST:
            raise SealedError("manifest-too-large")
        return oid

    def flush(self) -> None:
        if not self.slab:
            return
        plain = bytes(self.slab)
        self.slab.clear()
        sid = sealed.address(self.secret, plain)
        path = shard_path(self.repo, sid)
        if path.exists():
            cipher = read_cipher(path)
            sealed.authenticate_object(cipher, sid, self.trust["vault"], self.trust["owner"])
            compressed = sealed.decrypt(self.secret, cipher[64:], FORMAT + ":shard:" + sid)
            if _inflate(compressed, len(plain)) != plain:
                raise SealedError("object-collision")
        else:
            compressed = zlib.compress(plain, level=9)
            cipher = sealed.sign_object(
                sealed.encrypt(self.secret, compressed, FORMAT + ":shard:" + sid),
                sid,
                self.trust["vault"],
                self.identity,
            )
            if len(cipher) > SHARD_LIMIT:
                raise SealedError("packed-shard-too-large")
            self.pending_bytes += len(cipher)
            self.new_shards += 1
            if self.pending_bytes > self.budget and not self.plan_only:
                raise SealedError("storage-budget-exceeded")
            if not self.plan_only:
                assert self.stage is not None
                staged = self.stage / sid
                sealed._write(staged, cipher, exclusive=True)
                self.pending[sid] = staged
        self.maximum_shard_bytes = max(self.maximum_shard_bytes, len(cipher))
        self.shards[sid] = {
            "cipher_sha256": sealed.digest(cipher),
            "codec": CODEC,
            "compressed_bytes": len(compressed),
            "plain_bytes": len(plain),
        }
        self.metadata_bytes += len(sid) + len(sealed.canonical(self.shards[sid])) + 8
        if self.metadata_bytes > sealed.MAX_MANIFEST:
            raise SealedError("manifest-too-large")
        for oid in self.slab_ids:
            self.objects[oid]["shard"] = sid
        self.slab_ids.clear()

    def measurements(self) -> dict:
        return {
            "storage_format": FORMAT,
            "shards": len(self.shards),
            "maximum_shard_bytes": self.maximum_shard_bytes,
            "packed_plaintext_bytes": sum(info["plain_bytes"] for info in self.shards.values()),
            "packed_compressed_bytes": sum(
                info["compressed_bytes"] for info in self.shards.values()
            ),
        }


def publish_generation(
    *,
    source: Path,
    selection: list[str],
    repo: Path,
    identity: Identity,
    trust_path: Path,
    trust: dict,
    recipients: list[dict],
    previous: dict,
    secret: bytes,
    generation: int,
    parent: str,
    budget: int,
    plan_only: bool,
    projection_line_bytes: int,
) -> dict[str, Any]:
    context_manager = (
        nullcontext(None) if plan_only else tempfile.TemporaryDirectory(prefix=".cce-pack-")
    )
    with context_manager as temporary:
        stage = Path(temporary) / "stage" if temporary is not None else None
        packer = _Packer(repo, secret, identity, trust, stage, budget, plan_only)
        files: dict[str, dict] = {}
        atoms: dict[str, str] = {}
        selected = 0
        metadata_bytes = 0
        for name in selection:
            path = sealed.safe_path(source / sealed._relative(name))
            if not path.is_file():
                raise SealedError("source-not-regular-file")
            before = path.stat()
            selected += before.st_size
            checksum = hashlib.sha256()
            chunks = []
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(sealed.CHUNK), b""):
                    checksum.update(block)
                    chunks.append(packer.add(block))
                    metadata_bytes += 70
                    if metadata_bytes > sealed.MAX_MANIFEST:
                        raise SealedError("manifest-too-large")
            files[name] = {"chunks": chunks, "sha256": checksum.hexdigest()}
            metadata_bytes += len(name.encode()) + 128
            if metadata_bytes > sealed.MAX_MANIFEST:
                raise SealedError("manifest-too-large")
            for unit in project_units(path, name, max_line_bytes=projection_line_bytes):
                unit_id = unit["id"]
                if unit_id in atoms:
                    raise SealedError("duplicate-unit")
                atoms[unit_id] = packer.add(sealed.canonical(unit))
                metadata_bytes += len(unit_id.encode()) + 80
                if metadata_bytes > sealed.MAX_MANIFEST:
                    raise SealedError("manifest-too-large")
            after = path.stat()
            if any(
                getattr(before, field) != getattr(after, field)
                for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            ):
                raise SealedError("source-changed-during-publication")
        packer.flush()
        manifest = {
            "format": FORMAT,
            "files": files,
            "atoms": atoms,
            "objects": packer.objects,
            "shards": packer.shards,
            "recipients": recipients,
        }
        validate_manifest(manifest)
        measurements = packer.measurements()
        measurements["projection_line_limit"] = projection_line_bytes
        if manifest == previous:
            head = json.loads((repo / "HEAD.cce").read_bytes())
            capacity = sealed._capacity(
                repo,
                selected,
                0,
                (repo / "HEAD.cce").stat().st_size,
                budget,
                changed=False,
            )
            if plan_only:
                return {**capacity, **measurements}
            if not capacity["fits"]:
                raise SealedError("storage-budget-exceeded")
            sealed._accept(trust_path, trust, head)
            return {"changed": False, "files": len(files), "units": len(atoms), **measurements}
        plain_manifest = sealed.canonical(manifest)
        if len(plain_manifest) > sealed.MAX_MANIFEST:
            raise SealedError("manifest-too-large")
        context = f"{FORMAT}:{trust['vault']}:{generation}"
        head = sealed.sign(
            {
                "format": FORMAT,
                "vault": trust["vault"],
                "generation": generation,
                "previous": parent,
                "envelopes": [sealed.wrap(secret, r, context) for r in recipients],
                "manifest": sealed.encrypt(secret, plain_manifest, context + ":manifest").hex(),
            },
            identity,
        )
        encoded = sealed.canonical(head)
        capacity = sealed._capacity(
            repo,
            selected,
            packer.pending_bytes,
            len(encoded),
            budget,
            changed=True,
            pending_objects=packer.new_shards,
        )
        if plan_only:
            return {**capacity, **measurements}
        if not capacity["fits"]:
            raise SealedError("storage-budget-exceeded")
        created: list[Path] = []
        landed = {"head": False}
        try:
            for sid, staged in packer.pending.items():
                data = read_cipher(staged)
                if sealed.digest(data) != packer.shards[sid]["cipher_sha256"]:
                    raise SealedError("corrupt-staged-shard")
                path = shard_path(repo, sid)
                if path.exists():
                    if read_cipher(path) != data:
                        raise SealedError("object-collision")
                else:
                    # Track visibility, not the return of _write: a directory
                    # sync can fail after the exclusive publication landed.
                    sealed._write(
                        path,
                        data,
                        exclusive=True,
                        on_published=partial(created.append, path),
                    )
            sealed._write(repo / "HEAD.cce", encoded, on_published=lambda: landed.update(head=True))
        except BaseException as error:
            skipped = sealed._discard_unreferenced(
                repo, created, encoded, head_replaced=landed["head"]
            )
            if skipped:
                error.add_note(skipped)
            raise
        sealed._accept(trust_path, trust, head)
        return {
            "changed": True,
            "files": len(files),
            "units": len(atoms),
            "live_bytes": capacity["planned_tree_bytes"],
            "generation": generation,
            **measurements,
        }


def repair(
    repo: Path,
    mirror: Path,
    manifest: dict,
    secret: bytes,
    head: dict,
    trust_path: Path,
    trust: dict,
    observed: bytes | None,
    repaired_head: bool,
) -> dict[str, Any]:
    reader = Reader(repo, manifest, secret)
    replacement_reader = Reader(mirror, manifest, secret)
    with tempfile.TemporaryDirectory(prefix=".cce-repair-") as temporary:
        replacements: dict[str, Path] = {}
        for sid, ids in reader.groups.items():
            try:
                for oid in ids:
                    reader.object(oid)
            except SealedError:
                for oid in ids:
                    replacement_reader.object(oid)
                staged = Path(temporary) / sid
                sealed._write(staged, read_cipher(shard_path(mirror, sid)), exclusive=True)
                replacements[sid] = staged
        with sealed._lock(repo):
            current = (repo / "HEAD.cce").read_bytes() if (repo / "HEAD.cce").exists() else None
            if current != observed or sealed._trust(trust_path) != trust:
                raise SealedError("head-changed")
            for sid, staged in replacements.items():
                data = read_cipher(staged)
                if sealed.digest(data) != manifest["shards"][sid]["cipher_sha256"]:
                    raise SealedError("corrupt-staged-shard")
                sealed._write(shard_path(repo, sid), data)
            if repaired_head:
                sealed._write(repo / "HEAD.cce", sealed.canonical(head))
        check(repo, manifest, secret)
        sealed._accept(trust_path, trust, head)
        return {"repaired": len(replacements), "repaired_head": repaired_head}
