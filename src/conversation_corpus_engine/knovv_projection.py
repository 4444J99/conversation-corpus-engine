"""Private, encrypted application projections over the canonical library.

The browser publication is a derived surface, not a second source database. Stable
source tuples and a persistent keyed document identity survive publication rebuilds.
No private key, source title, token index or document body is written to the output
in plaintext. The only plaintext index is an automatically removed private scratch DB.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import tempfile
import time
import zlib
from collections import OrderedDict
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .archive_sealed import safe_path
from .archive_sealed_crypto import (
    Identity,
    address,
    authenticate,
    canonical,
    decrypt,
    derive,
    digest,
    encrypt,
    sign,
    unwrap,
    wrap,
)
from .attribution import text_digest
from .library_store import Library

SCHEMA = "knovv-publication.v1"
CATALOG = "knovv-catalog.v1"
PLAIN_LIMIT = 32 * 1024 * 1024
PAGE_CHARS = 8000
METADATA_PAGE = 256
DEFAULT_BUDGET = 512 * 1024 * 1024
MAX_BUDGET = 8 * 1024 * 1024 * 1024
PACK_BYTES = 4 * 1024 * 1024
TOKEN = re.compile(r"[^\W_]+", re.UNICODE)  # allow-secret: runtime-only
LINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]")
MARKDOWN_LINK = re.compile(r"(?<!!)\[([^\]\n]+)\]\(([^)\n]+)\)")
SOURCE_TUPLE = re.compile(
    r"cce:(?:library-[0-9a-f]{64}/)?(?P<uid>[a-z0-9][a-z0-9-]*)@(?P<version>[^\s#]+)#(?P<sequence>[ma])(?P<message>\d+):(?P<start>\d+)-(?P<end>\d+) sha256:(?P<digest>[0-9a-f]{64})"
)


def _inflate(data: bytes, limit: int = PLAIN_LIMIT) -> bytes:
    decoder = zlib.decompressobj()
    result = decoder.decompress(data, limit + 1)
    if len(result) > limit or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError("projection-object-bound")
    return result


def _object(root: Path, oid: str) -> bytes:
    if not re.fullmatch(r"[0-9a-f]{64}", oid):
        raise ValueError("invalid-object-id")
    path = safe_path(root / "objects" / oid)
    if path.stat().st_size > PLAIN_LIMIT + 8192:
        raise ValueError("projection-object-bound")
    raw = path.read_bytes()
    if digest(raw) != oid:
        raise ValueError("projection-object-digest")
    return raw


def _postings(rows) -> dict:
    """Lossless interval varints, including very dense title-derived postings."""
    result = {}
    term = None
    previous = -1
    pending = None
    encoded = bytearray()
    count = 0

    def emit(start, end):
        nonlocal previous, count
        for component in (start - previous, end - start + 1):
            value = component
            while value >= 128:
                encoded.append((value & 127) | 128)
                value >>= 7
            encoded.append(value)
        previous = end
        count += end - start + 1

    def finish():
        if term is not None:
            if pending is not None:
                emit(*pending)
            result[term] = {"count": count, "ranges": base64.b64encode(encoded).decode("ascii")}

    prior_start = -1
    for row in rows:
        current, start = row[:2]
        end = row[2] if len(row) > 2 else start
        if current != term:
            finish()
            term, previous, pending, encoded, count, prior_start = (
                current,
                -1,
                None,
                bytearray(),
                0,
                -1,
            )
        if (
            type(start) is not int
            or type(end) is not int
            or not 0 <= start <= end <= 0xFFFFFFFF
            or start < prior_start
        ):
            raise ValueError("invalid-search-posting")
        prior_start = start
        if pending is not None and start <= pending[1] + 1:
            pending = (pending[0], max(end, pending[1]))
        else:
            if pending is not None:
                emit(*pending)
            pending = (start, end)
    finish()
    return result


def _context(vault: str, epoch: int) -> str:
    return f"knovv-object:{vault}:{epoch}"


def read_bundle(root: Path, identity: Identity, owner: str, *, checkpoint: dict | None = None):
    root = safe_path(root)
    path = root / "HEAD.knovv"
    if path.stat().st_size > 256 * 1024:
        raise ValueError("projection-head-bound")
    raw = path.read_bytes()
    head = json.loads(raw)
    if canonical(head) != raw or head.get("schema") != SCHEMA:
        raise ValueError("invalid-projection-head")
    authenticate(head, owner)
    if checkpoint and (
        checkpoint["vault"] != head["vault"]
        or head["generation"] < checkpoint["generation"]
        or (head["generation"] == checkpoint["generation"] and digest(raw) != checkpoint["head"])
    ):
        raise ValueError("rollback-or-equivocation")
    secret = unwrap(  # allow-secret: runtime-only
        head["envelopes"], identity, f"{SCHEMA}:{head['vault']}:{head['epoch']}"
    )  # allow-secret: runtime-only
    manifest = json.loads(
        _inflate(
            decrypt(secret, _object(root, head["manifest"]), _context(head["vault"], head["epoch"]))
        )
    )
    if (
        manifest.get("schema") != CATALOG
        or manifest["vault"] != head["vault"]
        or manifest["epoch"] != head["epoch"]
    ):
        raise ValueError("projection-manifest-mismatch")
    return head, manifest, secret


class _Writer:
    def __init__(
        self,
        root: Path,
        secret: bytes,  # allow-secret: runtime-only
        vault: str,
        epoch: int,
        budget: int,
        previous: Path | None,
        reused: dict,
        connection: sqlite3.Connection,
    ):
        self.root, self.secret, self.vault, self.epoch = root, secret, vault, epoch
        self.address_key = derive(secret, b"address")
        self.budget, self.previous, self.reused = budget, previous, reused
        self.bytes = 0
        self.seen: set[str] = set()
        self.connection = connection
        self.slab = bytearray()
        self.slab_refs: dict[bytes, tuple[int, int]] = {}
        self.recent_keys: OrderedDict[bytes, None] = OrderedDict()
        self.bloom = bytearray(64 * 1024)
        self.bloom_entries = 0
        connection.execute(
            "CREATE TABLE packed(logical BLOB, object BLOB, offset INTEGER, length INTEGER)"
        )
        connection.execute(
            "CREATE TABLE previous_packed(logical BLOB PRIMARY KEY, object BLOB, offset INTEGER, length INTEGER) WITHOUT ROWID"
        )
        if previous:
            for oid in reused.values():
                mapping = json.loads(
                    _inflate(decrypt(secret, _object(previous, oid), _context(vault, epoch)))
                )
                connection.executemany(
                    "INSERT INTO previous_packed VALUES (?,?,?,?)",
                    (
                        (bytes.fromhex(key), bytes.fromhex(ref[0]), *ref[1:])
                        for key, ref in mapping.items()
                    ),
                )

    def _land(self, cipher: bytes) -> str:
        oid = digest(cipher)
        if oid not in self.seen:
            if self.bytes + len(cipher) > self.budget:
                raise ValueError("projection-storage-budget")
            path = self.root / "objects" / oid
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(cipher)
            self.seen.add(oid)
            self.bytes += len(cipher)
        return oid

    def direct(self, value: Any, previous_oid: str | None = None) -> str:
        raw = canonical(value)
        if len(raw) > PLAIN_LIMIT:
            raise ValueError("projection-object-bound")
        if self.previous and previous_oid:
            cipher = _object(self.previous, previous_oid)
            if _inflate(decrypt(self.secret, cipher, _context(self.vault, self.epoch))) == raw:
                return self._land(cipher)
        return self._land(
            encrypt(self.secret, zlib.compress(raw, level=1), _context(self.vault, self.epoch))
        )

    def flush(self) -> None:
        if not self.slab:
            return
        cipher = encrypt(
            self.secret, zlib.compress(bytes(self.slab), level=1), _context(self.vault, self.epoch)
        )
        oid = self._land(cipher)
        self.connection.executemany(
            "INSERT INTO packed VALUES (?,?,?,?)",
            (
                (key, bytes.fromhex(oid), offset, length)
                for key, (offset, length) in self.slab_refs.items()
            ),
        )
        self.slab.clear()
        self.slab_refs.clear()

    def resolution(self) -> dict[str, str]:
        self.flush()
        self.bloom.clear()
        self.recent_keys.clear()
        self.connection.execute("CREATE INDEX packed_order ON packed(logical,object,offset,length)")
        result = {}
        for first in range(256):
            prefix = f"{first:02x}"
            mapping = {
                row[0].hex(): [row[1].hex(), *row[2:]]
                for row in self.connection.execute(
                    "SELECT logical,object,offset,length FROM packed WHERE logical>=? AND logical<? ORDER BY logical",
                    (bytes([first]), bytes([first + 1]) if first < 255 else b"\xff" * 33),
                )
            }
            if mapping:
                result[prefix] = self.direct(mapping, self.reused.get(prefix))
        return result

    @staticmethod
    def _bits(key: bytes, mask: int):
        return (int.from_bytes(key[start : start + 4], "big") & mask for start in range(0, 28, 4))

    def _mark(self, key: bytes):
        for bit in self._bits(key, len(self.bloom) * 8 - 1):
            self.bloom[bit >> 3] |= 1 << (bit & 7)

    def _remember(self, key: bytes):
        self.recent_keys[key] = None
        self.recent_keys.move_to_end(key)
        if len(self.recent_keys) > 4096:
            self.recent_keys.popitem(last=False)

    def _grow(self):
        if self.bloom_entries < len(self.bloom) // 4 or len(self.bloom) >= 64 * 1024 * 1024:
            return
        self.bloom = bytearray(min(64 * 1024 * 1024, len(self.bloom) * 16))
        for (key,) in self.connection.execute("SELECT logical FROM packed"):
            self._mark(key)
        for key in self.slab_refs:
            self._mark(key)

    def put(self, value: Any) -> str:
        raw = canonical(value)
        if len(raw) > PLAIN_LIMIT - 8:
            raise ValueError("projection-object-bound")
        key = hmac.new(self.address_key, raw, hashlib.sha256).hexdigest()
        binary = bytes.fromhex(key)
        if binary in self.recent_keys or binary in self.slab_refs:
            self._remember(binary)
            return key
        self._grow()
        # A bounded filter avoids random reads for new objects. Positives always
        # receive an exact SQL check; it can never authorize a false deduplication.
        if (
            all(
                self.bloom[bit >> 3] & (1 << (bit & 7))
                for bit in self._bits(binary, len(self.bloom) * 8 - 1)
            )
            and self.connection.execute(
                "SELECT 1 FROM packed WHERE logical=? LIMIT 1", (binary,)
            ).fetchone()
        ):
            self._remember(binary)
            return key
        old = (
            self.connection.execute(
                "SELECT object,offset,length FROM previous_packed WHERE logical=?",
                (binary,),
            ).fetchone()
            if self.previous
            else None
        )
        if old and self.previous:
            self._land(_object(self.previous, old[0].hex()))
            self.connection.execute("INSERT INTO packed VALUES (?,?,?,?)", (binary, *old))
        else:
            if self.slab and len(self.slab) + 8 + len(raw) > PACK_BYTES:
                self.flush()
            offset = len(self.slab) + 8
            self.slab.extend(len(raw).to_bytes(8, "big"))
            self.slab.extend(raw)
            self.slab_refs[binary] = (offset, len(raw))
            if len(self.slab) >= PACK_BYTES:
                self.flush()
        self._mark(binary)
        self.bloom_entries += 1
        self._remember(binary)
        return key


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    header = text.split("---", 2)
    if len(header) < 3:
        return {}
    return {
        key.strip(): value.strip().strip("\"'")
        for line in header[1].splitlines()
        if ":" in line
        for key, value in [line.split(":", 1)]
    }


def _wiki(root: Path, id_key: bytes, library: Library) -> Iterator[dict]:
    files = sorted(
        p
        for p in root.rglob("*.md")
        if not any(x.startswith(".") for x in p.relative_to(root).parts)
    )
    identities = {
        p: address(id_key, ("wiki:" + p.relative_to(root).as_posix()).encode()) for p in files
    }
    titles: dict[str, list[Path]] = {}
    for p in files:
        titles.setdefault(p.stem.casefold(), []).append(p)
    for path in files:
        safe_path(path)
        if path.stat().st_size > 65536:
            raise ValueError(
                "wiki-note-over-64-kib: split in its owning generator before publication"
            )
        text = path.read_text()
        meta = _frontmatter(text)
        links = []

        def link(match, links=links):
            name, label = match.group(1), match.group(2)
            exact = root / (name + ".md")
            candidates = (
                [exact] if exact in identities else titles.get(Path(name).name.casefold(), [])
            )
            if len(candidates) == 1:
                target = identities[candidates[0]]
                links.append(target)
                return f"[{label or Path(name).name}](#/document/{target})"
            return match.group(0)

        visible = (
            text.split("---", 2)[2].lstrip()
            if text.startswith("---\n") and len(text.split("---", 2)) == 3
            else text
        )

        def markdown_link(match, links=links, path=path):
            label, target = match.groups()
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or not parsed.path:
                return match.group(0)
            candidate = (path.parent / unquote(parsed.path)).resolve()
            if candidate not in identities:
                return match.group(0)
            identity = identities[candidate]
            links.append(identity)
            return f"[{label}](#/document/{identity})"

        body = MARKDOWN_LINK.sub(markdown_link, LINK.sub(link, visible))
        sources = []
        for found in SOURCE_TUPLE.finditer(text):
            ref = found.groupdict()
            for key in ("message", "start", "end"):
                ref[key] = int(ref[key])
            ref["sequence"] = "main" if ref["sequence"] == "m" else "alternate"
            try:
                record = library.record(ref["uid"], version=ref["version"])
                messages = (
                    record.get("messages", [])
                    if ref["sequence"] == "main"
                    else record.get("extra", {}).get("alternate_messages", [])
                )
                message = messages[ref["message"]]["text"]
                if (
                    not 0 <= ref["start"] <= ref["end"] <= len(message)
                    or text_digest(message) != ref["digest"]
                ):
                    raise ValueError("invalid-wiki-citation")
            except (KeyError, IndexError, TypeError) as error:
                raise ValueError("invalid-wiki-citation") from error
            sources.append(ref)
        kind = meta.get("type", "wiki")
        if kind not in {"wiki", "project", "zettel"}:
            kind = "wiki"
        yield {
            "id": identities[path],
            "logical_id": "wiki:" + path.relative_to(root).as_posix(),
            "kind": kind,
            "title": meta.get("title", path.stem),
            "summary": meta.get("summary") or re.sub(r"[#*`\[\]]", "", visible).strip()[:200],
            "body": body,
            "author": meta.get("author", "unknown"),
            "authorship_basis": meta.get(
                "authorship_basis", "declared" if meta.get("author") else "unverified"
            ),
            "status": meta.get("status", "draft"),
            "created": meta.get("created", ""),
            "updated": meta.get("updated", ""),
            "sources": sources,
            "links": sorted(set(links)),
            "tags": [],
            "source_file": path.relative_to(root).as_posix(),
        }


def _source_candidates(library: Library, id_key: bytes) -> Iterator[dict]:
    for row in library.rows():
        versions = [entry["version"] for entry in library.history(row["uid"])]
        history = [
            {
                "version": version,
                "id": address(id_key, f"cce:{row['uid']}@{version}#main:0:0".encode()),
            }
            for version in versions
        ]
        for version in versions:
            yield from _revision_sources(
                row, library.record(row["uid"], version=version), version, history, id_key
            )


def _revision_sources(
    row: dict, payload: dict, version: str, history: list, id_key: bytes
) -> Iterator[dict]:
    address_key = derive(id_key, b"address")
    title = payload.get("title", row["uid"])
    shared = (row["uid"] + "@" + version, frozenset(TOKEN.findall(title.lower())))
    for sequence, messages in (
        ("main", payload.get("messages", []) or [{"text": ""}]),
        ("alternate", payload.get("extra", {}).get("alternate_messages", [])),
    ):
        for number, message in enumerate(messages):
            text = message.get("text", "")
            source_digest = text_digest(text)
            for start in range(0, max(1, len(text)), PAGE_CHARS):
                part = text[start : start + PAGE_CHARS]
                ref = {
                    "uid": row["uid"],
                    "version": version,
                    "digest": source_digest,
                    "message": number,
                    "start": start,
                    "end": start + len(part),
                    "sequence": sequence,
                }
                logical = f"cce:{row['uid']}@{version}#{sequence}:{number}:{start}"
                spans = message.get("meta", {}).get("attribution", {})
                yield {
                    "id": hmac.new(address_key, logical.encode(), hashlib.sha256).hexdigest(),
                    "logical_id": logical,
                    "kind": "source",
                    "title": title + f" · {number + 1}",
                    "_index_shared": shared,
                    "_index_number": str(number + 1),
                    "body": part,
                    "summary": part[:200],
                    "author": "unknown",
                    "authorship_basis": "unverified" if not spans else "source-assertion",
                    "source_attribution": spans,
                    "role": message.get("role", ""),
                    "status": "source"
                    if version == row["current_version"]
                    else "historical-source",
                    "created": message.get("ts", ""),
                    "updated": payload.get("updated", ""),
                    "sources": [ref],
                    "source_history": history,
                    "links": [],
                    "tags": [payload.get("provider", "")],
                    "source_url": payload.get("provenance", {}).get("url", ""),
                }


def _sources(
    library: Library, id_key: bytes, attribution_deadline: float | None = None
) -> Iterator[dict]:
    from itertools import groupby  # noqa: PLC0415

    from .library_zettel_authorship import derive_quote_authors  # noqa: PLC0415

    for _, group in groupby(
        _source_candidates(library, id_key), key=lambda doc: doc["sources"][0]["uid"]
    ):
        if attribution_deadline is not None and time.monotonic() >= attribution_deadline:
            for document in group:
                document.update(
                    author="unknown",
                    authorship_basis="unmeasured",
                    authorship_note="global attribution deadline reached",
                )
                yield document
            continue
        documents = list(group)
        eligible = [d for d in documents if d["body"] and d["sources"][0]["sequence"] == "main"]
        refs = [{**d["sources"][0], "text_digest": d["sources"][0]["digest"]} for d in eligible]
        remaining = (
            (attribution_deadline - time.monotonic()) if attribution_deadline is not None else 900.0
        )
        if remaining <= 0:
            for document in documents:
                document.update(
                    author="unknown",
                    authorship_basis="unmeasured",
                    authorship_note="global attribution deadline reached",
                )
            yield from documents
            continue
        derived = derive_quote_authors(library, refs, deadline_seconds=remaining)
        for document, attribution in zip(eligible, derived, strict=True):
            document.update(
                author=attribution.author,
                authorship_basis="rules-derived",
                authorship_rules=list(attribution.rules),
                authorship_classes=list(attribution.classes),
                authorship_note=attribution.reason,
            )
        yield from documents


def export_bundle(
    library: Library,
    wiki_root: Path,
    destination: Path,
    owner: Identity,
    recipients: list[dict],
    *,
    budget: int = DEFAULT_BUDGET,
    previous: Path | None = None,
    rotate: bool = False,
    deadline_seconds: float = 3600.0,
    attribution_seconds: float = 900.0,
) -> dict:
    if type(budget) is not int or not 1 <= budget <= MAX_BUDGET:
        raise ValueError("invalid-publication-budget")
    if not 0 < deadline_seconds <= 86400 or not 0 < attribution_seconds <= 86400:
        raise ValueError("invalid-publication-deadline")
    started = time.monotonic()
    deadline = started + deadline_seconds
    if not recipients or owner.recipient() not in recipients or len(recipients) > 64:
        raise ValueError("publisher-and-recovery-recipient-required")
    destination = safe_path(destination)
    wiki_root = safe_path(wiki_root)
    if (
        destination == wiki_root
        or wiki_root in destination.parents
        or destination in wiki_root.parents
    ):
        raise ValueError("publication-overlaps-wiki")
    if destination == library.path or library.path in destination.parents:
        raise ValueError("publication-overlaps-library")
    prior, manifest, secret = (  # allow-secret: runtime-only
        read_bundle(previous, owner, owner.signing_public())
        if previous
        else ({}, {}, os.urandom(32))
    )
    vault, epoch = prior.get("vault", os.urandom(32).hex()), prior.get("epoch", 1)
    if prior and manifest.get("recipients") != recipients and not rotate:
        raise ValueError("recipient-change-requires-explicit-rotation")
    epoch_keys = dict(manifest.get("epoch_keys", {}))
    if rotate:
        if not prior:
            raise ValueError("rotation-requires-previous-publication")
        epoch_keys[str(epoch)] = secret.hex()
        epoch += 1
        secret = os.urandom(32)  # allow-secret: runtime-only
    id_key = bytes.fromhex(manifest["id_key"]) if prior else os.urandom(32)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    (destination / "objects").mkdir(mode=0o700)
    metadata, navigation, page = [], [], []
    count = 0
    with tempfile.TemporaryDirectory(prefix="knovv-index-", dir=destination.parent) as scratch:
        Path(scratch).chmod(0o700)
        connection = sqlite3.connect(str(Path(scratch) / "tokens.sqlite"))
        connection.execute("PRAGMA cache_size=-4096")
        connection.execute("PRAGMA temp_store=FILE")
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        packed_connection = sqlite3.connect(str(Path(scratch) / "packed.sqlite"))
        packed_connection.execute("PRAGMA cache_size=-4096")
        packed_connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        writer = _Writer(
            destination,
            secret,
            vault,
            epoch,
            budget,
            None if rotate else previous,
            {} if rotate else manifest.get("resolve", {}),
            packed_connection,
        )
        connection.execute(
            "CREATE TABLE postings(bucket TEXT, term TEXT, start INTEGER, end INTEGER)"
        )
        connection.execute("CREATE TABLE locators(id TEXT, ordinal INTEGER)")
        try:
            from itertools import chain  # noqa: PLC0415

            shared_key = None
            shared_terms = frozenset()
            shared_start = 0

            def flush_shared():
                if shared_key is not None:
                    connection.executemany(
                        "INSERT INTO postings VALUES (?,?,?,?)",
                        (
                            (hashlib.sha256(t.encode()).hexdigest()[:4], t, shared_start, count - 1)
                            for t in shared_terms
                        ),
                    )

            for doc in chain(
                _wiki(wiki_root, id_key, library),
                _sources(library, id_key, started + min(attribution_seconds, deadline_seconds)),
            ):
                if time.monotonic() > deadline:
                    raise ValueError("publication-deadline")
                shared = doc.pop("_index_shared", None)
                number = doc.pop("_index_number", None)
                key = shared[0] if shared else None
                if key != shared_key:
                    flush_shared()
                    shared_key = key
                    shared_terms = shared[1] if shared else frozenset()
                    shared_start = count
                object_id = writer.put(doc)
                meta = {k: v for k, v in doc.items() if k != "body"}
                meta.update(ordinal=count, object=object_id)
                connection.execute("INSERT INTO locators VALUES (?,?)", (doc["id"], count))
                page.append(meta)
                if doc["kind"] != "source":
                    navigation.append(meta)
                if shared is not None:
                    terms = (set(TOKEN.findall(doc["body"].lower())) | {number}) - shared_terms
                else:
                    terms = set(TOKEN.findall((doc["title"] + " " + doc["body"]).lower()))
                connection.executemany(
                    "INSERT INTO postings VALUES (?,?,?,?)",
                    [(hashlib.sha256(t.encode()).hexdigest()[:4], t, count, count) for t in terms],
                )
                count += 1
                if len(page) == METADATA_PAGE:
                    metadata.append(writer.put(page))
                    page = []
            flush_shared()
            if page:
                metadata.append(writer.put(page))
            connection.execute("CREATE INDEX postings_order ON postings(bucket,term,start,end)")
            search = {}
            for (bucket,) in connection.execute(
                "SELECT DISTINCT bucket FROM postings ORDER BY bucket"
            ):
                if time.monotonic() > deadline:
                    raise ValueError("publication-deadline")
                postings = _postings(
                    connection.execute(
                        "SELECT term,start,end FROM postings WHERE bucket=? ORDER BY term,start,end",
                        (bucket,),
                    )
                )
                search[bucket] = writer.put(postings)
            connection.execute("CREATE INDEX locators_order ON locators(id,ordinal)")
            lookup = {}
            locator_count = 0
            for first in range(256):
                if time.monotonic() > deadline:
                    raise ValueError("publication-deadline")
                prefix = f"{first:02x}"
                locators = dict(
                    connection.execute(
                        "SELECT id,ordinal FROM locators WHERE id>=? AND id<?",
                        (prefix, f"{first + 1:02x}" if first < 255 else "g"),
                    )
                )
                locator_count += len(locators)
                if locators:
                    lookup[prefix] = writer.put(locators)
            if locator_count != count:
                raise ValueError("projection-duplicate-document-id")
            resolution = writer.resolution()
            if time.monotonic() > deadline:
                raise ValueError("publication-deadline")
        finally:
            connection.close()
            packed_connection.close()
    catalog = {
        "schema": CATALOG,
        "vault": vault,
        "epoch": epoch,
        "id_key": id_key.hex(),
        "count": count,
        "metadata": metadata,
        "lookup": lookup,
        "search": search,
        "search_prefix": 4,
        "search_encoding": "ranges-u32.v1",
        "navigation": navigation,
        "receipts": [
            {
                "source": "canonical-library",
                "status": "projected",
                "records": library.connection.execute("SELECT COUNT(*) FROM records").fetchone()[0],
            },
            {"source": "wiki", "status": "projected", "pages": len(navigation)},
            {"source": "capture-adapters", "status": "not-refreshed-by-publication"},
        ],
        "recipients": recipients,
        "resolve": resolution,
        "epoch_keys": epoch_keys,
    }
    manifest_id = writer.direct(catalog)
    head = sign(
        {
            "schema": SCHEMA,
            "vault": vault,
            "generation": prior.get("generation", 0) + 1,
            "epoch": epoch,
            "parent": digest(canonical(prior)) if prior else None,
            "manifest": manifest_id,
            "envelopes": [wrap(secret, r, f"{SCHEMA}:{vault}:{epoch}") for r in recipients],
        },
        owner,
    )
    raw = canonical(head)
    if writer.bytes + len(raw) > budget:
        raise ValueError("projection-storage-budget")
    if time.monotonic() > deadline:
        raise ValueError("publication-deadline")
    fd = os.open(destination / "HEAD.knovv", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    return {
        "documents": count,
        "cipher_bytes": writer.bytes + len(raw),
        "objects": len(writer.seen),
        "generation": head["generation"],
        "head": digest(raw),
    }


class _Reader:
    def __init__(self, root: Path, manifest: dict, secret: bytes):  # allow-secret: runtime-only
        self.root, self.manifest, self.secret = root, manifest, secret  # allow-secret: runtime-only
        self.address_key = derive(secret, b"address")
        self.context = _context(manifest["vault"], manifest["epoch"])
        self.maps: OrderedDict[str, dict] = OrderedDict()
        self.slab_id = None
        self.slab = b""

    def direct(self, oid: str) -> bytes:
        return _inflate(decrypt(self.secret, _object(self.root, oid), self.context))

    def raw(self, key: str) -> bytes:
        if "resolve" not in self.manifest:
            return self.direct(key)
        prefix = key[:2]
        if prefix not in self.maps:
            oid = self.manifest["resolve"].get(prefix)
            if not oid:
                raise ValueError("missing-object-locator")
            mapping = json.loads(self.direct(oid))
            if not isinstance(mapping, dict):
                raise ValueError("invalid-object-locator")
            if len(self.maps) >= 16:
                self.maps.popitem(last=False)
            self.maps[prefix] = mapping
        self.maps.move_to_end(prefix)
        ref = self.maps[prefix].get(key)
        if not isinstance(ref, list) or len(ref) != 3:
            raise ValueError("invalid-object-locator")
        oid, offset, length = ref
        if (
            type(offset) is not int
            or type(length) is not int
            or offset < 8
            or length < 1
            or offset + length > PLAIN_LIMIT
        ):
            raise ValueError("invalid-packed-offset")
        if self.slab_id != oid:
            self.slab = b""
            self.slab_id = None
            self.slab = self.direct(oid)
            self.slab_id = oid
        if (
            offset + length > len(self.slab)
            or int.from_bytes(self.slab[offset - 8 : offset], "big") != length
        ):
            raise ValueError("invalid-packed-frame")
        raw = self.slab[offset : offset + length]
        if hmac.new(self.address_key, raw, hashlib.sha256).hexdigest() != key:
            raise ValueError("invalid-packed-address")
        return raw

    def value(self, key: str):
        return json.loads(self.raw(key))


def restore_bundle(
    root: Path,
    destination: Path,
    identity: Identity,
    owner: str,
    *,
    checkpoint: dict | None = None,
    layout: str = "directory",
    deadline_seconds: float = 3600.0,
) -> dict:
    head, manifest, secret = read_bundle(  # allow-secret: runtime-only
        root, identity, owner, checkpoint=checkpoint
    )  # allow-secret: runtime-only
    if layout not in {"directory", "jsonl"} or not 0 < deadline_seconds <= 86400:
        raise ValueError("invalid-restore-configuration")
    if "resolve" in manifest:
        from .knovv_restore import restore_packed  # noqa: PLC0415

        return restore_packed(
            root,
            destination,
            manifest,
            secret,
            layout=layout,
            deadline=time.monotonic() + deadline_seconds,
        )
    if layout != "directory":
        raise ValueError("legacy-unpacked-restore-requires-directory-layout")
    reader = _Reader(root, manifest, secret)
    documents = []
    seen = set()
    for oid in manifest["metadata"]:
        page = reader.value(oid)
        if not isinstance(page, list) or len(page) > METADATA_PAGE:
            raise ValueError("invalid-metadata-page")
        for meta in page:
            raw = reader.raw(meta["object"])
            identity = meta.get("id", "")
            if (
                not re.fullmatch(r"[0-9a-f]{64}", identity)
                or identity in seen
                or json.loads(raw).get("id") != identity
            ):
                raise ValueError("projection-document-mismatch")
            seen.add(identity)
            documents.append((identity, meta["object"]))
    if len(documents) != manifest["count"]:
        raise ValueError("projection-count-mismatch")
    for oid in list(manifest["search"].values()) + list(manifest["lookup"].values()):
        reader.value(oid)
    # Every content/count/identifier check finishes before the destination exists.
    destination = safe_path(destination)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    for identity, oid in documents:
        raw = reader.raw(oid)
        fd = os.open(
            destination / (identity + ".json"), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
    return {"documents": len(documents)}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Publish a private encrypted KNOVV projection; source inputs are read-only"
    )
    parser.add_argument("--store-root", type=Path, required=True)
    parser.add_argument("--wiki-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--owner-key", type=Path, required=True)
    parser.add_argument("--passphrase-file", type=Path, required=True)
    parser.add_argument("--recipient", type=Path, action="append", default=[])
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    args = parser.parse_args()
    from .archive_sealed_credentials import unlock_identity  # noqa: PLC0415
    from .library_commands import open_library  # noqa: PLC0415

    for path in (args.owner_key, args.passphrase_file):
        safe_path(path)
        if path.stat().st_mode & 0o077:
            parser.error("credential-permissions")
    owner = unlock_identity(args.owner_key.read_bytes(), args.passphrase_file.read_bytes())
    recipients = [json.loads(p.read_bytes()) for p in args.recipient] or [owner.recipient()]
    _, library = open_library(args.store_root)
    try:
        print(
            json.dumps(
                export_bundle(
                    library,
                    args.wiki_root,
                    args.output,
                    owner,
                    recipients,
                    budget=args.budget,
                    previous=args.previous,
                ),
                sort_keys=True,
            )
        )
    finally:
        library.close()


if __name__ == "__main__":
    main()
