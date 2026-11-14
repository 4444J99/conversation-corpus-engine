"""Portable, private reading surfaces for the canonical CCE library.

These are disposable derivatives of the revision store, never instructions or
new ingestion authorities. Exporting preserves the source store and refuses to
replace an existing reading surface, including a user's annotations.
"""

from __future__ import annotations

import csv
import hashlib
import html
import io
import itertools
import json
import math
import os
import re
import shlex
import sys
import zipfile
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import unquote, urlsplit

from .archive_text import FenceTracker
from .archive_vault import INLINE_CODE_RE
from .attributed_knowledge import ZETTEL_SCHEMA, export_zettels
from .attribution import ASSURANCE
from .corpus_store import require_git_private
from .library_blocks import render_blocks, split_blocks
from .library_note_text import EVIDENCE as _EVIDENCE
from .library_note_text import FACETS as _FACETS
from .library_note_text import WINGS as _WINGS
from .library_note_text import check as _check
from .library_note_text import clip as _clip
from .library_note_text import facet_stem
from .library_note_text import inert as _inert
from .library_note_text import inert_lines as _inert_lines
from .library_note_text import plain as _plain
from .library_note_text import safe_uid as _uid
from .library_store import Library, canonical_json
from .library_vault import PROFILE_CARDS, PROFILE_FULL, PROFILE_NONE, VAULT_PROFILES, plan_vault
from .library_vault_gate import VaultCaps, hard_cap_problems, measure_vault

EXPORT_SCHEMA = "cce.library-export.v1"
INDEX_SCHEMA = "cce.library-index.v1"
# The vault profiles (none, cards, full) write the v2 manifest and index: a note path may be
# absent (null) and there are no block notes, so a v1 reader refuses them instead of misreading.
EXPORT_SCHEMA_V2 = "cce.library-export.v2"
INDEX_SCHEMA_V2 = "cce.library-index.v2"
LEGACY_PROFILE = "legacy"
EXPORT_PROFILES = (LEGACY_PROFILE, *VAULT_PROFILES)
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_RECEIPTS = {"library.sqlite", "sources.json", "build-state.json"}
_LATER_RECEIPTS = _RECEIPTS | {"report.json"}
_LINK = re.compile(r"(?<!!)\[[^\]\n]*\]\(([^)\n]*)\)")
_CHUNK = 1024 * 1024
ZETTEL_PAGE_LIMIT = 16 * 1024
ZETTEL_HUB_SIZE = 100
_ZETTEL_ID_LIMIT = 512
_QUOTE_LIMIT = 400
_ZETTEL_PAGE = re.compile(r"vault/zettels/[0-9a-f]{16}\.md\Z")
_ZETTEL_HUB = re.compile(r"vault/hubs/zettels-[1-9][0-9]*\.md\Z")
_FRONTMATTER_ESCAPES = str.maketrans(
    {
        "[": "\\u005b",
        "]": "\\u005d",
        "<": "\\u003c",
        ">": "\\u003e",
        "&": "\\u0026",
        "`": "\\u0060",
        "\x85": "\\u0085",
        " ": "\\u2028",
        " ": "\\u2029",
    }
)


def _facet_path(facet: str, value: str) -> str:
    return f"vault/hubs/{facet_stem(facet, value)}.md"


def _safe_relative(value: str) -> Path:
    path = Path(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError(f"unsafe export path: {value!r}")
    return path


def _no_symlinks(path: Path) -> None:
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError(f"export path contains a symlink: {component}")


def _absolute(path: Path) -> Path:
    """Normalize lexically; resolving would follow the symlinks we must reject."""
    return Path(os.path.abspath(path))  # noqa: PTH100 - no filesystem walks or symlink following


class _CheckedBinary:
    """Charge actual writes, including ZIP header rewrites, before touching disk."""

    def __init__(self, stream: BinaryIO, check_callback: Callable[..., Any] | None) -> None:
        self.stream = stream
        self.check_callback = check_callback

    def write(self, value: bytes) -> int:
        _check(self.check_callback, bytes_written=len(value))
        return self.stream.write(value)

    def tell(self) -> int:
        return self.stream.tell()

    def seek(self, *args: Any) -> int:
        return self.stream.seek(*args)

    def flush(self) -> None:
        self.stream.flush()


class _Writer:
    def __init__(self, destination: Path, callback: Callable[..., Any] | None) -> None:
        self.destination = destination
        self.callback = callback
        self.files: list[str] = []

    @contextmanager
    def binary(self, relative: str) -> Iterator[_CheckedBinary]:
        path = self.destination / _safe_relative(relative)
        _check(self.callback)
        _no_symlinks(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        for parent in path.parents:
            if parent == self.destination:
                break
            parent.chmod(0o700)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        self.files.append(relative)
        with os.fdopen(descriptor, "wb") as stream:
            yield _CheckedBinary(stream, self.callback)

    def text(self, relative: str, chunks: str | Iterable[str]) -> None:
        if isinstance(chunks, str):
            chunks = (chunks,)
        with self.binary(relative) as stream:
            for chunk in chunks:
                _check(self.callback)
                raw = chunk.encode("utf-8")
                for start in range(0, len(raw), _CHUNK):
                    stream.write(raw[start : start + _CHUNK])


def _index_row(
    row: dict[str, Any], paths: Mapping[str, str | None] | None = None
) -> dict[str, Any]:
    """One index row; ``paths`` (a vault profile) names each record's note, or null for none."""
    return {
        "schema": INDEX_SCHEMA if paths is None else INDEX_SCHEMA_V2,
        **{
            key: row[key]
            for key in (
                "uid",
                "title",
                "provider",
                "kind",
                "wing",
                "lane",
                "created",
                "updated",
                "body_status",
                "message_count",
                "source_ref",
                "content_digest",
            )
        },
        "version": row["current_version"],
        "digest": row["current_digest"],
        "path": f"vault/records/{row['uid']}.md" if paths is None else paths[row["uid"]],
    }


def _record_note(
    library: Library,
    row: dict[str, Any],
    repeated: set[str],
    known: set[str],
    callback: Callable[..., Any] | None,
    citations: list[dict[str, Any]] | None = None,
) -> Iterator[str]:
    uid = row["uid"]
    yield f"# {_plain(row['title']) or 'Untitled'}\n\n{_EVIDENCE}\n\n[Library home](../Home.md)\n\n"
    yield f"- UID: {uid}\n- Version: {_plain(row['current_version'])}\n- Body coverage: {_plain(row['body_status'])}\n"
    for facet in _FACETS:
        href = "../hubs/" + Path(_facet_path(facet, row[facet])).name
        label = _WINGS.get(row[facet], row[facet]) if facet == "wing" else row[facet]
        yield f"- {facet.title()}: [{_plain(label)}]({href})\n"
    yield f"- Created: {_plain(row['created']) or 'Not recorded'}\n- Updated: {_plain(row['updated']) or 'Not recorded'}\n"
    yield f"- Normalized revision SHA-256: {_plain(row['current_digest'])}\n"
    source = row["source_ref"]
    if source:
        if Path(source).is_absolute():
            yield f"- [Original source]({Path(source).as_uri()})\n"
        else:
            yield f"- Source reference: {_plain(source)}\n"
    for relation in library.connection.execute(
        "SELECT dst FROM relations WHERE src=? AND kind='parent' ORDER BY dst", (uid,)
    ):
        _check(callback)
        parent = _uid(relation["dst"])
        if parent in known:
            yield f"- [Parent record: {parent}]({parent}.md)\n"
        else:
            yield f"- Parent record: {parent} (not present in this generation)\n"
    yield "\nAll accepted normalized revisions remain in the library database. Use the bounded history and read commands in [agent entry](../../agent-entry.md).\n"
    payload = library.record(uid)
    if not payload["messages"]:
        yield "\nNo source body was available. Metadata is retained and this record remains visible in coverage reports.\n"
    for index, message in enumerate(payload["messages"]):
        _check(callback)
        yield f"\n## Message {index + 1}\n\nRole: {_plain(message.get('role', ''))}; channel: {_plain(message.get('channel', '')) or 'Not recorded'}\n\n"
        blocks = split_blocks(message["text"])
        references = {}
        for block in blocks:
            _check(callback)
            if block["eligible"] and block["digest"] in repeated:
                references[block["digest"]] = {
                    "label": block["digest"][:12],
                    "href": f"../blocks/{block['digest']}.md",
                }
        yield render_blocks(blocks, references) + "\n"
        attachments = message.get("attachments", [])
        if attachments:
            yield "\nAttachment metadata (original binaries remain at source):\n\n"
            for attachment in attachments:
                _check(callback)
                yield "- " + _plain(canonical_json(attachment)) + "\n"
    alternates = payload.get("extra", {}).get("alternate_messages", [])
    if isinstance(alternates, list) and alternates:
        yield "\n## Alternate source messages\n\nThese retained source branches were not part of the main message sequence.\n"
        for index, message in enumerate(alternates):
            _check(callback)
            if isinstance(message, dict):
                yield (
                    f"\n### Alternate {index + 1}\n\n"
                    + render_blocks(split_blocks(str(message.get("text", ""))), {})
                    + "\n"
                )
    if citations:
        yield "\n## Zettels citing this record\n\n"
        for citation in citations:
            _check(callback)
            yield _citation_line(citation)


def _block_note(
    library: Library, digest: str, callback: Callable[..., Any] | None
) -> Iterator[str]:
    text = library.block_text(digest)
    yield f"# Repeated passage {digest[:12]}\n\n{_EVIDENCE}\n\n[Library home](../Home.md)\n\n"
    yield "One canonical reading copy follows. Original message text and exact occurrence offsets remain in the revision store. Matching only normalizes line endings, terminal newlines and common Markdown quote depth.\n\n"
    yield render_blocks(split_blocks(text), {}) + "\n\n## Source occurrences\n\n"
    for occurrence in library.connection.execute(
        "SELECT o.*,r.title FROM occurrences o JOIN records r ON r.uid=o.uid WHERE o.digest=? ORDER BY o.uid,o.message_index,o.block_index",
        (digest,),
    ):
        _check(callback)
        uid = _uid(occurrence["uid"])
        number = occurrence["message_index"] + 1
        normalization = ", ".join(json.loads(occurrence["normalization"])) or "none"
        yield f"- [{_plain(occurrence['title']) or uid} / {uid} / message {number}](../records/{uid}.md#message-{number}); characters {occurrence['start']}:{occurrence['end']}; normalization: {_plain(normalization)}\n"


def _frontmatter_value(value: Any) -> str:
    """A YAML double-quoted scalar that no Markdown link scanner can read as a link."""
    return json.dumps(str(value), ensure_ascii=False).translate(_FRONTMATTER_ESCAPES)


def _zettel_hash(identity: str) -> str:
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _zettel_path(identity: str) -> str:
    return f"vault/zettels/{_zettel_hash(identity)}.md"


def _zettel_plan(
    library: Library, envelope: dict[str, Any], rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """Validate the reviewed envelope against the library and derive every cross reference."""
    if not isinstance(envelope, dict) or envelope.get("schema") != ZETTEL_SCHEMA:
        raise ValueError("unsupported reviewed Zettel envelope schema")
    by_uid = {row["uid"]: row for row in rows}
    items = sorted(
        export_zettels(library, envelope.get("zettels"))["zettels"], key=lambda z: z["id"]
    )
    hashes: dict[str, str] = {}
    seen_hashes: set[str] = set()
    for item in items:
        if len(item["id"]) > _ZETTEL_ID_LIMIT:
            raise ValueError(f"Zettel id too long for a page ({_ZETTEL_ID_LIMIT} characters max)")
        digest = _zettel_hash(item["id"])
        if digest in seen_hashes:
            raise ValueError("Zettel page hash collision")
        seen_hashes.add(digest)
        hashes[item["id"]] = digest
        for entry in item["sources"]:
            if _uid(entry["source"]["uid"]) not in by_uid:
                raise ValueError("Zettel source record is absent from this export")
    citations: dict[str, list[dict[str, Any]]] = defaultdict(list)
    inbound: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for item in items:
        for entry in item["sources"]:
            ref = entry["source"]
            row = by_uid[ref["uid"]]
            citations[ref["uid"]].append(
                {
                    "hash": hashes[item["id"]],
                    "title": _inert(item["title"], 120) or "Untitled",
                    "version": ref["version"],
                    "sequence": ref["sequence"],
                    "message": ref["message"],
                    "start": ref["start"],
                    "end": ref["end"],
                    "historical": not _is_current(ref, row),
                }
            )
        for relationship in item["relationships"]:
            if relationship["target"] in hashes:
                inbound[relationship["target"]].append((relationship["type"], item["id"]))
    for values in citations.values():
        values.sort(key=lambda c: (c["hash"], c["sequence"], c["message"], c["start"], c["end"]))
    return {
        "items": items,
        "hashes": hashes,
        "titles": {item["id"]: item["title"] for item in items},
        "position": {item["id"]: index for index, item in enumerate(items)},
        "rows": by_uid,
        "citations": dict(citations),
        "inbound": {key: sorted(set(value)) for key, value in inbound.items()},
    }


def _is_current(ref: dict[str, Any], row: dict[str, Any]) -> bool:
    return (
        ref["version"] == row["current_version"] and ref["revision_digest"] == row["current_digest"]
    )


def _citation_line(citation: dict[str, Any]) -> str:
    what = "alternate message" if citation["sequence"] == "alternate" else "message"
    line = (
        f"- [{citation['title']}](../zettels/{citation['hash']}.md) - {what} "
        f"{citation['message'] + 1}, characters {citation['start']}:{citation['end']}"
    )
    if citation["historical"]:
        line += f"; revision {_inert(citation['version'])} is historical and this note shows a later revision"
    return line + "\n"


def _span_label(span: dict[str, Any], limit: int = 80) -> str:
    author_id = str(span["author_id"])[:limit] if span.get("author_id") else ""
    who = span["author"] + (f":{author_id}" if author_id else "")
    return f"{who} {span['start']}-{span['end']} {span['assertion_status']}"


def _render_zettel(item: dict[str, Any], plan: dict[str, Any], scale: int) -> str:
    def cap(base: int) -> int:
        return max(1, base // scale)

    hashes, rows = plan["hashes"], plan["rows"]
    identity = item["id"]
    hub = plan["position"][identity] // ZETTEL_HUB_SIZE + 1
    title, title_cut = _clip(" ".join(item["title"].split()), cap(300))
    title += "..." if title_cut else ""
    body, body_cut = _clip(item["body"], cap(3000))
    authors = sorted({span["author"] for span in item["body_attribution"]})
    author = authors[0] if len(authors) == 1 else ("mixed" if authors else "unknown")
    spans = [("body", s) for s in item["body_attribution"]] + [
        ("title", s) for s in item["title_spans"]
    ]
    shown_spans = spans[: cap(12)]
    sources = item["sources"][: cap(32)]
    relationships = item["relationships"][: cap(24)]

    def tuple_of(entry: dict[str, Any]) -> str:
        ref = entry["source"]
        mark = "a" if ref["sequence"] == "alternate" else "m"
        return (
            f"cce:{ref['uid']}@{ref['version']}#{mark}{ref['message']}:{ref['start']}-{ref['end']}"
            f" sha256:{ref['text_digest']}"
        )

    links: list[str] = []
    for link in (
        *(f"records/{entry['source']['uid']}.md" for entry in sources),
        *(
            f"zettels/{hashes[relationship['target']]}.md"
            for relationship in relationships
            if relationship["target"] in hashes
        ),
    ):
        if link not in links:
            links.append(link)
    summary = _clip(" ".join(item["body"].split()), 200)[0]
    front = ["---", f"id: {_frontmatter_value(identity)}", 'type: "zettel"']
    front += [f"title: {_frontmatter_value(title)}", f"summary: {_frontmatter_value(summary)}"]
    front += [
        f"created: {_frontmatter_value(_clip(str(item.get('created') or ''), cap(64))[0])}",
        f"updated: {_frontmatter_value(_clip(str(item.get('updated') or ''), cap(64))[0])}",
        f"author: {_frontmatter_value(author)}",
        "authorship:",
    ]
    front += [
        f"  - {_frontmatter_value(f'{part} {_span_label(s, cap(80))}')}" for part, s in shown_spans
    ]
    # Omissions are stated in the visible sections below, never as pseudo entries here.
    front += ['status: "reviewed"', "sources:"]
    front += [f"  - {_frontmatter_value(tuple_of(entry))}" for entry in sources]
    front += ["links:", *(f"  - {_frontmatter_value(link)}" for link in links)]
    front += ["aliases:", f"  - {_frontmatter_value(identity)}", "tags:"]
    front += [
        f"  - {_frontmatter_value(tag)}" for tag in ("zettel", f"author-{author}", "reviewed")
    ]
    front.append("---")
    out = ["\n".join(front), f"\n# {_inert(title) or 'Untitled'}\n\n{_EVIDENCE}\n\n"]
    out.append(f"[Library home](../Home.md) - [All Zettels](../hubs/zettels-{hub}.md)\n\n")
    out.append(f"Zettel ID: {_inert(identity)}\n\n{_inert_lines(body)}\n")
    if body_cut:
        out.append(
            f"\nBody truncated: {len(body)} of {len(item['body'])} characters shown; the full text is in the reviewed envelope.\n"
        )
    out.append("\n## Authorship\n\n")
    out.append(f"- Assurance: {ASSURANCE}\n")
    for part, span in shown_spans:
        out.append(f"- {part.title()}: {_inert(_span_label(span, cap(80)))}\n")
    if len(spans) > len(shown_spans):
        out.append(f"- Authorship spans truncated: {len(spans) - len(shown_spans)} not shown\n")
    review = item["review"]
    out.append(f"\n## Review\n\n- Reviewer: {_inert(review['reviewer'], cap(300))}\n")
    for evidence in review["evidence"][: cap(8)]:
        out.append(f"- Evidence: {_inert(evidence, cap(300))}\n")
    if relationships:
        out.append("\n## Relationships\n\n")
        for relationship in relationships:
            kind = _inert(relationship["type"], cap(80))
            target = relationship["target"]
            if target in hashes:
                label = _inert(plan["titles"][target], 120) or "Untitled"
                out.append(f"- {kind}: [{label}]({hashes[target]}.md)\n")
            else:
                out.append(f"- {kind}: {_inert(target, cap(200))} (not present in this export)\n")
        if len(item["relationships"]) > len(relationships):
            out.append(
                f"- Relationships truncated: {len(item['relationships']) - len(relationships)} not shown\n"
            )
    out.append("\n## Sources\n")
    for number, entry in enumerate(sources, 1):
        ref = entry["source"]
        row = rows[ref["uid"]]
        label = _inert(row["title"], 120) or ref["uid"]
        anchor = (
            f"#{'alternate' if ref['sequence'] == 'alternate' else 'message'}-{ref['message'] + 1}"
        )
        out.append(f"\n### Source {number}\n\n`{tuple_of(entry)}`\n\n")
        if _is_current(ref, row):
            out.append(f"- Record: [{label}](../records/{ref['uid']}.md{anchor})\n")
        else:
            out.append(
                f"- Record: [{label}](../records/{ref['uid']}.md) - revision {_inert(ref['version'])} is a historical revision; the current note shows a later one\n"
            )
        out.append(
            f"- Role: {_inert(entry['role'], cap(80))}; channel: {_inert(entry['channel'], cap(80)) or 'Not recorded'}; message time: {_inert(entry['message_timestamp'], cap(80)) or 'Not recorded'}\n"
        )
        for span in entry["attributed_spans"][: cap(6)]:
            out.append(f"- Quoted text attribution: {_inert(_span_label(span, cap(80)))}\n")
        quote, cut = _clip(entry["text"], max(1, _QUOTE_LIMIT // scale))
        out.append(
            "\n"
            + "\n".join("> " + line for line in (_inert_lines(quote).split("\n") or [""]))
            + "\n"
        )
        if cut:
            out.append(
                f"\nQuotation truncated: {len(quote)} of {len(entry['text'])} characters shown; the exact span is {ref['start']}-{ref['end']}.\n"
            )
    if len(item["sources"]) > len(sources):
        out.append(f"\nSources truncated: {len(item['sources']) - len(sources)} not shown.\n")
    linked = plan["inbound"].get(identity, [])
    if linked:
        out.append("\n## Linked from\n\n")
        for kind, other_id in linked[: cap(24)]:
            label = _inert(plan["titles"][other_id], 120) or "Untitled"
            out.append(f"- [{label}]({hashes[other_id]}.md) ({_inert(kind, cap(80))})\n")
    return "".join(out)


def _zettel_page(item: dict[str, Any], plan: dict[str, Any]) -> str:
    for scale in (1, 2, 4, 8, 16, 32):
        text = _render_zettel(item, plan, scale)
        if len(text.encode("utf-8")) <= ZETTEL_PAGE_LIMIT:
            return text
    raise ValueError(f"Zettel page cannot fit {ZETTEL_PAGE_LIMIT} bytes: {item['id'][:80]!r}")


def _zettel_hub(plan: dict[str, Any], number: int, pages: int) -> Iterator[str]:
    items = plan["items"][(number - 1) * ZETTEL_HUB_SIZE : number * ZETTEL_HUB_SIZE]
    yield f"# Zettels, page {number} of {pages}\n\n[Library home](../Home.md)\n\n"
    yield f"{len(plan['items'])} reviewed Zettels; this page lists {len(items)}.\n\n"
    if number > 1:
        yield f"[Previous page](zettels-{number - 1}.md)\n\n"
    if number < pages:
        yield f"[Next page](zettels-{number + 1}.md)\n\n"
    for item in items:
        yield (
            f"- [{_inert(item['title'], 200) or 'Untitled'}](../zettels/{plan['hashes'][item['id']]}.md)"
            f" - {_inert(item['id'])} - sources: {len(item['sources'])}\n"
        )


def _zettel_graph_rows(plan: dict[str, Any]) -> Iterator[str]:
    hashes = plan["hashes"]
    for item in plan["items"]:
        yield (
            canonical_json(
                {
                    "type": "node",
                    "id": "zettel:" + hashes[item["id"]],
                    "kind": "zettel",
                    "path": _zettel_path(item["id"]),
                    "zettel_id": item["id"],
                    "reviewer": item["review"]["reviewer"],
                }
            )
            + "\n"
        )
    absent: set[str] = set()
    for item in plan["items"]:
        for relationship in item["relationships"]:
            target = relationship["target"]
            if target not in hashes and target not in absent:
                absent.add(target)
                yield (
                    canonical_json(
                        {
                            "type": "node",
                            "id": "zettel:" + _zettel_hash(target),
                            "kind": "unresolved_zettel",
                            "present": False,
                            "zettel_id": target,
                        }
                    )
                    + "\n"
                )
    for item in plan["items"]:
        source = "zettel:" + hashes[item["id"]]
        for entry in item["sources"]:
            ref = entry["source"]
            yield (
                canonical_json(
                    {
                        "type": "edge",
                        "kind": "derived_from",
                        "source": source,
                        "target": ref["uid"],
                        "version": ref["version"],
                        "sequence": ref["sequence"],
                        "message_index": ref["message"],
                        "start": ref["start"],
                        "end": ref["end"],
                        "text_digest": ref["text_digest"],
                    }
                )
                + "\n"
            )
        for relationship in item["relationships"]:
            target = relationship["target"]
            yield (
                canonical_json(
                    {
                        "type": "edge",
                        "kind": "relation:" + relationship["type"],
                        "source": source,
                        "target": "zettel:" + (hashes.get(target) or _zettel_hash(target)),
                    }
                )
                + "\n"
            )


def _graph_rows(
    library: Library,
    rows: list[dict[str, Any]],
    repeated: set[str],
    callback: Callable[..., Any] | None,
    paths: Mapping[str, str | None] | None = None,
) -> Iterator[str]:
    """Graph rows. With ``paths`` (a vault profile) a record node names its note only when it
    has one and a shared-passage node has no note at all: its text stays in the database."""
    known = {row["uid"] for row in rows}
    for row in rows:
        _check(callback)
        node = {
            "type": "node",
            "id": row["uid"],
            "kind": "record",
            "path": f"vault/records/{row['uid']}.md" if paths is None else paths[row["uid"]],
            "wing": row["wing"],
            "lane": row["lane"],
            "version": row["current_version"],
        }
        if node["path"] is None:
            del node["path"]
        yield canonical_json(node) + "\n"
    for digest in sorted(repeated):
        _check(callback)
        node = {"type": "node", "id": "block:" + digest, "kind": "block"}
        if paths is None:
            node["path"] = f"vault/blocks/{digest}.md"
        yield canonical_json(node) + "\n"
    absent: set[str] = set()
    for relation in library.connection.execute("SELECT * FROM relations ORDER BY src,dst,kind"):
        _check(callback)
        for endpoint in (relation["src"], relation["dst"]):
            _uid(endpoint)
            if endpoint not in known and endpoint not in absent:
                absent.add(endpoint)
                yield (
                    canonical_json(
                        {
                            "type": "node",
                            "id": endpoint,
                            "kind": "unresolved_record",
                            "present": False,
                        }
                    )
                    + "\n"
                )
        yield (
            canonical_json(
                {
                    "type": "edge",
                    "kind": relation["kind"],
                    "source": relation["src"],
                    "target": relation["dst"],
                    "evidence": json.loads(relation["evidence"]),
                }
            )
            + "\n"
        )
    for occurrence in library.connection.execute(
        "SELECT * FROM occurrences ORDER BY uid,message_index,block_index"
    ):
        _check(callback)
        if occurrence["digest"] in repeated:
            yield (
                canonical_json(
                    {
                        "type": "edge",
                        "kind": "reference",
                        "source": occurrence["uid"],
                        "target": "block:" + occurrence["digest"],
                        "message_index": occurrence["message_index"],
                        "block_index": occurrence["block_index"],
                        "start": occurrence["start"],
                        "end": occurrence["end"],
                        "normalization": json.loads(occurrence["normalization"]),
                    }
                )
                + "\n"
            )


def _csv_cell(value: Any) -> str:
    text = str(value)
    if text.startswith(("\t", "\r", "\n")) or text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def _csv_rows(
    rows: list[dict[str, Any]],
    callback: Callable[..., Any] | None,
    paths: Mapping[str, str | None] | None = None,
) -> Iterator[str]:
    columns = (
        "uid",
        "title",
        "provider",
        "wing",
        "lane",
        "kind",
        "version",
        "body_status",
        "message_count",
        "path",
        "source_ref",
    )
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(columns)
    yield buffer.getvalue()
    for row in rows:
        _check(callback)
        buffer.seek(0)
        buffer.truncate()
        metadata = _index_row(row, paths)
        writer.writerow(
            ["" if metadata[column] is None else _csv_cell(metadata[column]) for column in columns]
        )
        yield buffer.getvalue()


_PROVIDER_NOTE = "Raw provider exports and original documents remain at source."
# The three paragraphs after the commands, per layout. The legacy text is the original, byte for byte.
_ENTRY_TAIL = {
    LEGACY_PROFILE: (
        "[Human library](vault/Home.md) has two wings: AI conversations and coding; authored research and creative writing. `library.sqlite` retains compressed normalized revisions and source observations. Raw provider exports and original documents remain at source.\n\n",
        "`library-index.jsonl` is the compact metadata and canonical-note contract for my-knowledge-base and LingFrame; it is not a second database. `graph.jsonl` records parent relationships and repeated-passage references.\n\n",
        "`notion-import.zip` contains this Markdown tree and `records.csv`. Notion supports Markdown ZIP and CSV imports; imported internal links and heading anchors may vary, so inspect them after import. This export does not publish or upload private material.\n",
    ),
    PROFILE_NONE: (
        "This generation was built with `--vault-profile none`: it holds no reading vault and no Notion ZIP. "
        "`library.sqlite` retains compressed normalized revisions and source observations. "
        + _PROVIDER_NOTE
        + "\n\n",
        "`library-index.jsonl` is the compact metadata contract for my-knowledge-base and LingFrame; its `path` is null because no notes exist. `graph.jsonl` records parent relationships and repeated-passage references. `records.csv` lists every record.\n\n",
        "For a small reading vault run `cce library export-cards --out DIR` (one capped card per record) or `cce library export --vault-profile cards --out DIR`. This export does not publish or upload private material.\n",
    ),
    PROFILE_CARDS: (
        "[Human library](vault/Home.md) is a capped card vault (`--vault-profile cards`): one short card per record, most recently updated first, never the full body; its Home note states how many records have no card. `library.sqlite` retains compressed normalized revisions and source observations. "
        + _PROVIDER_NOTE
        + "\n\n",
        "`library-index.jsonl` is the compact metadata contract for my-knowledge-base and LingFrame; its `path` names a record's card and is null for a record without one. `graph.jsonl` records parent relationships and repeated-passage references.\n\n",
        "`notion-import.zip` contains the card tree and `records.csv`. Notion supports Markdown ZIP and CSV imports; imported internal links and heading anchors may vary, so inspect them after import. This export does not publish or upload private material.\n",
    ),
    PROFILE_FULL: (
        "[Human library](vault/Home.md) holds every record whole, split into linked parts (`--vault-profile full`); repeated passages appear inline and have no notes of their own. `library.sqlite` retains compressed normalized revisions and source observations. "
        + _PROVIDER_NOTE
        + "\n\n",
        "`library-index.jsonl` is the compact metadata and canonical-note contract for my-knowledge-base and LingFrame (`path` names the first part of a record); it is not a second database. `graph.jsonl` records parent relationships and repeated-passage references.\n\n",
        "`notion-import.zip` contains this Markdown tree and `records.csv`. Notion supports Markdown ZIP and CSV imports; imported internal links and heading anchors may vary, so inspect them after import. This export does not publish or upload private material.\n",
    ),
}


def _agent_entry(destination: Path, profile: str = LEGACY_PROFILE) -> str:
    registered = (
        destination.parent.name == "generations" and destination.parent.parent.name == "library-all"
    )
    assignment = (
        f"STORE={shlex.quote(str(destination.parents[2]))}\n"
        if registered
        else "# Set STORE to the registered CCE store root before running these commands.\n"
    )
    executable = Path(sys.executable).with_name("cce")
    command = str(executable) if executable.is_file() else "cce"
    commands = (
        assignment
        + f"CCE={shlex.quote(command)}\n"
        + '"$CCE" library search TERM --store-root "$STORE" --limit 20 --offset 0\n"$CCE" library read UID --store-root "$STORE" --message 0 --offset 0 --limit 12000\n"$CCE" library history UID --store-root "$STORE" --offset 0 --limit 50\n'
    )
    fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", commands)), default=0))
    text = (
        "# Bounded library entry\n\n" + _EVIDENCE + "\n\n"
        "Search first, then read one record or revision page. Do not load the whole vault or index into an agent prompt. TERM is a search expression; UID is a returned stable identity. Advance the returned next_offset for another page.\n\n"
        + fence
        + "sh\n"
        + commands
        + fence
        + "\n\n"
        + "".join(_ENTRY_TAIL[profile])
    )
    if len(text.encode()) >= 4096:
        raise ValueError("agent entry exceeds its 4096-byte budget")
    return text


def _write_notion_zip(
    writer: _Writer,
    destination: Path,
    members: list[str],
    check_callback: Callable[..., Any] | None,
) -> None:
    with (
        writer.binary("notion-import.zip") as output,
        zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive,
    ):
        for relative in members:
            _check(check_callback)
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100600 << 16
            with (
                (destination / relative).open("rb") as source,
                archive.open(info, "w", force_zip64=True) as target,
            ):
                while chunk := source.read(_CHUNK):
                    _check(check_callback)
                    target.write(chunk)


def _load_rows(
    library: Library, check_callback: Callable[..., Any] | None
) -> tuple[list[dict[str, Any]], set[str]]:
    """Every record row (validated) and the digests of the repeated passages."""
    rows = []
    for row in library.rows():
        _check(check_callback)
        _uid(row["uid"])
        if row["wing"] not in _WINGS:
            raise ValueError("unknown library wing")
        rows.append(row)
    repeated: set[str] = set()
    for row in library.connection.execute(
        "SELECT b.digest FROM blocks b JOIN occurrences o ON o.digest=b.digest WHERE b.eligible=1 GROUP BY b.digest HAVING COUNT(*)>1"
    ):
        _check(check_callback)
        if not _DIGEST.fullmatch(row["digest"]):
            raise ValueError("unsafe block digest")
        repeated.add(row["digest"])
    return rows, repeated


def _require_empty_destination(destination: Path) -> None:
    _no_symlinks(destination)
    if destination.exists():
        if not destination.is_dir():
            raise FileExistsError(destination)
        for entry in destination.iterdir():
            if entry.name not in _RECEIPTS or entry.is_symlink() or not entry.is_file():
                raise FileExistsError(f"refusing to replace existing export content: {entry}")


def _empty_gate(caps: VaultCaps) -> dict[str, Any]:
    return {
        "notes": 0,
        "bytes": 0,
        "max_note_bytes": 0,
        "max_note_path": "",
        "folders": 0,
        "widest_folder": {"path": ".", "entries": 0},
        "hub_pages": 0,
        "max_hub_entries": 0,
        "findings": [],
        "complete": True,
        "ok": True,
        "clean": True,
        "caps": caps.as_dict(),
    }


def _export_profile(
    library: Library,
    destination: Path,
    *,
    check_callback: Callable[..., Any] | None,
    profile: str,
    limit_notes: int | None,
    caps: VaultCaps | None,
) -> dict[str, Any]:
    """The none, cards and full layouts: a capped (or absent) vault beside the same indexes."""
    caps = caps or VaultCaps()
    destination = _absolute(destination)
    _require_empty_destination(destination)
    rows, repeated = _load_rows(library, check_callback)
    # Planning reads every record it must lay out and refuses, before any directory exists, a
    # vault that would pass a hard cap.
    plan = plan_vault(
        profile,
        library,
        rows,
        prefix="vault/",
        entry_doc="agent-entry.md",
        caps=caps,
        limit_notes=limit_notes,
        check_callback=check_callback,
    )
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.chmod(0o700)
    for entry in destination.iterdir():
        entry.chmod(0o600)
    writer = _Writer(destination, check_callback)
    plan.write(writer.text)
    gate = measure_vault(destination / "vault", caps) if profile != PROFILE_NONE else None
    if gate is not None and not gate["ok"]:
        raise ValueError("vault breaks its hard caps: " + "; ".join(hard_cap_problems(gate)))
    writer.text("agent-entry.md", _agent_entry(destination, profile))
    writer.text(
        "library-index.jsonl", (canonical_json(_index_row(row, plan.paths)) + "\n" for row in rows)
    )
    writer.text("graph.jsonl", _graph_rows(library, rows, repeated, check_callback, plan.paths))
    writer.text("records.csv", _csv_rows(rows, check_callback, plan.paths))
    inventory: dict[str, Any] = {
        "schema": EXPORT_SCHEMA_V2,
        "profile": profile,
        "records": len(rows),
        "blocks": len(repeated),
    }
    if profile != PROFILE_NONE:
        members = sorted(
            path for path in writer.files if path.endswith(".md") or path == "records.csv"
        )
        _write_notion_zip(writer, destination, members, check_callback)
        inventory["notion_members"] = members
    if profile == PROFILE_CARDS:
        inventory["cards"] = {
            "included": plan.cards,
            "records": len(rows),
            "truncated": plan.truncated,
            "limit_notes": plan.limit_notes,
        }
    inventory["files"] = sorted([*writer.files, "export-manifest.json"])
    writer.text("export-manifest.json", canonical_json(inventory) + "\n")
    result: dict[str, Any] = {
        "records": len(rows),
        "blocks": len(repeated),
        "files": len(writer.files),
        "vault": "vault/Home.md" if profile != PROFILE_NONE else None,
        "index": "library-index.jsonl",
        "notion": "notion-import.zip" if profile != PROFILE_NONE else None,
        "profile": profile,
        "vault_caps": gate or _empty_gate(caps),
    }
    if profile == PROFILE_CARDS:
        result.update(
            cards=plan.cards,
            omitted=plan.omitted,
            truncated=plan.truncated,
            limit_notes=plan.limit_notes,
        )
    return result


def export_library(
    library: Library,
    destination: Path,
    *,
    check_callback: Callable[..., Any] | None = None,
    zettels: dict[str, Any] | None = None,
    vault_profile: str = LEGACY_PROFILE,
    limit_notes: int | None = None,
    caps: VaultCaps | None = None,
) -> dict[str, Any]:
    """Create all reading derivatives, refusing any existing owned output.

    ``check_callback`` receives impending write deltas as ``bytes_written`` and
    receives no arguments at cooperative deadline checkpoints. Exceptions leave
    the candidate intact for diagnosis; publication belongs to the builder.

    ``zettels`` is an optional ``cce.reviewed-zettels.v1`` envelope. It is validated
    against this library before anything is written; every reviewed Zettel then gets
    a page, a hub entry, graph rows and backlinks on the records it cites. Without it
    the output is byte-for-byte what it was before this option existed.

    ``vault_profile`` defaults to the original ``legacy`` layout, unchanged. ``none`` writes no
    vault and no Notion ZIP, ``cards`` a capped card vault (``limit_notes`` bounds its notes) and
    ``full`` whole records split into linked parts; the last two never write per-block notes and
    keep every folder, note and hub inside the vault caps, or refuse before writing (see
    ``library_vault``). Those layouts carry the v2 manifest and index and no reviewed Zettels.
    """
    if vault_profile not in EXPORT_PROFILES:
        raise ValueError(
            f"unknown vault profile {vault_profile!r}; choose one of "
            f"{', '.join(VAULT_PROFILES)} (or {LEGACY_PROFILE} for the original layout)"
        )
    if vault_profile != LEGACY_PROFILE:
        if zettels is not None:
            raise ValueError(
                "reviewed Zettel pages need the legacy layout; omit --vault-profile (vault_profile)"
            )
        return _export_profile(
            library,
            destination,
            check_callback=check_callback,
            profile=vault_profile,
            limit_notes=limit_notes,
            caps=caps,
        )
    if limit_notes is not None or caps is not None:
        raise ValueError("limit_notes and caps apply to the none, cards and full profiles only")
    destination = _absolute(destination)
    _no_symlinks(destination)
    # The exports are the whole library as plaintext, so a directory Git can see is refused here
    # too: the builder authorised its generation the same way, and this covers every other caller.
    require_git_private(destination)
    if destination.exists():
        if not destination.is_dir():
            raise FileExistsError(destination)
        for entry in destination.iterdir():
            if entry.name not in _RECEIPTS or entry.is_symlink() or not entry.is_file():
                raise FileExistsError(f"refusing to replace existing export content: {entry}")
    rows = []
    facets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in library.rows():
        _check(check_callback)
        _uid(row["uid"])
        if row["wing"] not in _WINGS:
            raise ValueError("unknown library wing")
        rows.append(row)
        for facet in _FACETS:
            facets[facet, row[facet]].append(row)
    repeated: set[str] = set()
    for row in library.connection.execute(
        "SELECT b.digest FROM blocks b JOIN occurrences o ON o.digest=b.digest WHERE b.eligible=1 GROUP BY b.digest HAVING COUNT(*)>1"
    ):
        _check(check_callback)
        if not _DIGEST.fullmatch(row["digest"]):
            raise ValueError("unsafe block digest")
        repeated.add(row["digest"])
    plan = _zettel_plan(library, zettels, rows) if zettels is not None else None
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.chmod(0o700)
    for entry in destination.iterdir():
        entry.chmod(0o600)
    writer = _Writer(destination, check_callback)
    known = {row["uid"] for row in rows}
    for row in rows:
        _check(check_callback)
        writer.text(
            f"vault/records/{row['uid']}.md",
            _record_note(
                library,
                row,
                repeated,
                known,
                check_callback,
                plan["citations"].get(row["uid"]) if plan else None,
            ),
        )
    for digest in sorted(repeated):
        _check(check_callback)
        writer.text(f"vault/blocks/{digest}.md", _block_note(library, digest, check_callback))
    zettel_pages = 0
    if plan:
        for item in plan["items"]:
            _check(check_callback)
            writer.text(_zettel_path(item["id"]), _zettel_page(item, plan))
        zettel_pages = max(1, math.ceil(len(plan["items"]) / ZETTEL_HUB_SIZE))
        for number in range(1, zettel_pages + 1):
            _check(check_callback)
            writer.text(f"vault/hubs/zettels-{number}.md", _zettel_hub(plan, number, zettel_pages))
    for wing in _WINGS:
        facets.setdefault(("wing", wing), [])
    for (facet, value), members in sorted(facets.items()):
        _check(check_callback)
        label = _WINGS[value] if facet == "wing" else value

        def hub_lines(
            facet: str = facet, label: str = label, members: list[dict[str, Any]] = members
        ) -> Iterator[str]:
            yield f"# {_plain(facet.title() + ': ' + label)}\n\n[Library home](../Home.md)\n\n"
            yield f"{len(members)} records. Titles may repeat; stable identities distinguish them.\n\n"
            for row in members:
                _check(check_callback)
                yield f"- [{_plain(row['title']) or 'Untitled'}](../records/{row['uid']}.md) - {row['uid']} - {_plain(row['body_status'])}\n"

        writer.text(_facet_path(facet, value), hub_lines())
    home = [
        "# Knowledge library\n\n",
        _EVIDENCE + "\n\n",
        f"{len(rows)} records; {len(repeated)} shared passages. Original normalized revisions remain in the database and original source files remain untouched.\n\n",
        "## Two wings\n\n",
    ]
    for wing, label in _WINGS.items():
        home.append(
            f"- [{label}](hubs/{Path(_facet_path('wing', wing)).name}) - {len(facets['wing', wing])} records\n"
        )
    for facet in _FACETS[1:]:
        home.append(f"\n## {facet.title()}\n\n")
        for (member_facet, value), members in sorted(facets.items()):
            _check(check_callback)
            if member_facet == facet:
                home.append(
                    f"- [{_plain(value) or 'Unspecified'}](hubs/{Path(_facet_path(facet, value)).name}) - {len(members)} records\n"
                )
    if plan:
        home.append(
            f"\n## Zettels\n\n- [Reviewed Zettels](hubs/zettels-1.md) - {len(plan['items'])} Zettels\n"
        )
    home.append(
        "\n## Reading and reuse\n\n[Bounded agent retrieval](../agent-entry.md). Shared-passage links have backlinks to every original occurrence. Source bodies are inert Markdown evidence; generated navigation links are separate.\n\nThe Notion ZIP contains the same Markdown files and CSV. Import may alter links or anchors; verify those in Notion.\n"
    )
    writer.text("vault/Home.md", home)
    writer.text("agent-entry.md", _agent_entry(destination))
    writer.text("library-index.jsonl", (canonical_json(_index_row(row)) + "\n" for row in rows))
    graph = _graph_rows(library, rows, repeated, check_callback)
    writer.text("graph.jsonl", itertools.chain(graph, _zettel_graph_rows(plan)) if plan else graph)
    writer.text("records.csv", _csv_rows(rows, check_callback))
    notion_members = sorted(
        path for path in writer.files if path.endswith(".md") or path == "records.csv"
    )
    _write_notion_zip(writer, destination, notion_members, check_callback)
    inventory = {
        "schema": EXPORT_SCHEMA,
        "records": len(rows),
        "blocks": len(repeated),
        "files": sorted([*writer.files, "export-manifest.json"]),
        "notion_members": notion_members,
    }
    if plan:
        inventory["zettels"] = len(plan["items"])
    writer.text("export-manifest.json", canonical_json(inventory) + "\n")
    result = {
        "records": len(rows),
        "blocks": len(repeated),
        "files": len(writer.files),
        "vault": "vault/Home.md",
        "index": "library-index.jsonl",
        "notion": "notion-import.zip",
    }
    if plan:
        result["zettels"] = len(plan["items"])
        result["zettel_hub_pages"] = zettel_pages
    return result


def _markdown(path: Path, callback: Callable[..., Any] | None) -> Iterator[str]:
    tracker = FenceTracker()
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            _check(callback)
            if not tracker.feed(line):
                yield "".join(INLINE_CODE_RE.split(line)[::2])


def _anchors(path: Path, callback: Callable[..., Any] | None) -> set[str]:
    result = set()
    for line in _markdown(path, callback):
        if match := re.match(r"^#{1,6} +(.+?)\s*#*\s*$", line):
            result.add(re.sub(r"[^\w -]", "", html.unescape(match[1]).lower()).replace(" ", "-"))
    return result


def _verify_zettel_pages(
    destination: Path,
    existing: set[str],
    declared: int | None,
    expected: int | None,
    callback: Callable[..., Any] | None,
) -> list[str]:
    """Count Zettel pages on disk against the manifest and envelope; bind id to file name."""
    problems: list[str] = []
    pages = sorted(path for path in existing if path.startswith("vault/zettels/"))
    for relative in pages:
        _check(callback)
        if not _ZETTEL_PAGE.fullmatch(relative):
            problems.append(f"unexpected Zettel page name: {relative}")
            continue
        try:
            with (destination / relative).open(encoding="utf-8") as stream:
                head = [stream.readline().rstrip("\n") for _ in range(3)]
            identity = json.loads(head[1].removeprefix("id: "))
            if head[0] != "---" or not head[1].startswith("id: ") or head[2] != 'type: "zettel"':
                raise ValueError("frontmatter missing")
            if relative != _zettel_path(identity):
                problems.append(f"Zettel page name does not match its id: {relative}")
        except (OSError, UnicodeError, ValueError, TypeError) as error:
            problems.append(f"unreadable Zettel frontmatter {relative}: {error}")
    if declared is None and pages:
        problems.append("Zettel pages present but the manifest declares none")
    if declared is not None and len(pages) != declared:
        problems.append(
            f"zettel page count differs from manifest: pages={len(pages)} manifest={declared}"
        )
    if expected is not None and len(pages) != expected:
        problems.append(
            f"zettel page count differs from reviewed envelope: pages={len(pages)} reviewed={expected}"
        )
    if declared is not None:
        hubs = sorted(path for path in existing if _ZETTEL_HUB.fullmatch(path))
        wanted = {
            f"vault/hubs/zettels-{n}.md"
            for n in range(1, max(1, -(-declared // ZETTEL_HUB_SIZE)) + 1)
        }
        if set(hubs) != wanted:
            problems.append("Zettel hub pages differ from the declared pagination")
        entries = 0
        for relative in hubs:
            try:
                count = sum(
                    "](../zettels/" in line
                    for line in (destination / relative).read_text(encoding="utf-8").splitlines()
                )
            except (OSError, UnicodeError) as error:
                problems.append(f"unreadable Zettel hub {relative}: {error}")
                continue
            if count > ZETTEL_HUB_SIZE:
                problems.append(f"Zettel hub exceeds {ZETTEL_HUB_SIZE} entries: {relative}")
            entries += count
        if entries != declared:
            problems.append(f"zettel hub entries differ from manifest: {entries} of {declared}")
    for relative in pages:
        if (destination / relative).stat().st_size > ZETTEL_PAGE_LIMIT:
            problems.append(f"Zettel page exceeds {ZETTEL_PAGE_LIMIT} bytes: {relative}")
    return problems


def _verify_profile_index(
    profile: str,
    manifest: dict[str, Any],
    noted: dict[str, str | None],
    expected: set[str],
) -> list[str]:
    """The v2 index must name exactly the notes the profile wrote, each at most once."""
    paths = [path for path in noted.values() if path]
    indexed = set(paths)
    if profile == PROFILE_NONE:
        return ["library index names notes in an export built without a vault"] if paths else []
    tree = "vault/records/" if profile == PROFILE_FULL else "vault/cards/"
    problems = []
    if indexed != {path for path in expected if path.startswith(tree) and path.endswith(".md")}:
        problems.append("record inventory differs from library index")
    if len(indexed) != len(paths):
        problems.append("library index names one note for two records")
    if profile == PROFILE_FULL and len(paths) != len(noted):
        problems.append("a full vault must name a note for every record")
    if profile == PROFILE_CARDS:
        cards = manifest.get("cards")
        if not isinstance(cards, dict) or cards.get("included") != len(paths):
            problems.append("card count differs from manifest")
    return problems


def verify_export(
    destination: Path,
    *,
    check_callback: Callable[..., Any] | None = None,
    expected_zettels: int | None = None,
    caps: VaultCaps | None = None,
) -> list[str]:
    """Check inventory, generated links/anchors, identity graph and ZIP parity.

    Source file links are provenance, not a request to read or fetch those files.
    No network access is performed. Build-level hashes bind these verified files.
    ``expected_zettels`` is the reviewed-Zettel count of the envelope the export was
    built from; the Zettel pages found on disk must match it exactly. A Zettel source
    link whose ``#message-N`` or ``#alternate-N`` anchor is absent is reported as an
    unresolved source anchor.

    A v2 manifest (the none, cards and full profiles) is verified for its profile: ``none`` must
    hold no vault and no Notion ZIP, ``cards`` and ``full`` must keep every note, folder and hub
    inside the hard caps of ``caps`` (the standard's defaults) and index exactly their notes.
    """
    destination = _absolute(destination)
    problems: list[str] = []
    profile: str | None = None  # None is the legacy layout
    try:
        _no_symlinks(destination)
        _no_symlinks(destination / "export-manifest.json")
        manifest = json.loads((destination / "export-manifest.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("schema") not in {
            EXPORT_SCHEMA,
            EXPORT_SCHEMA_V2,
        }:
            raise ValueError("unknown export manifest schema")
        expected = set(manifest["files"])
        declared = manifest.get("zettels")
        if manifest["schema"] == EXPORT_SCHEMA_V2:
            profile = manifest.get("profile")
            if profile not in VAULT_PROFILES:
                raise ValueError("unknown vault profile")
            if declared is not None:
                raise ValueError("a vault profile carries no reviewed Zettels")
            required = {
                "export-manifest.json",
                "agent-entry.md",
                "library-index.jsonl",
                "graph.jsonl",
                "records.csv",
            }
            if profile != PROFILE_NONE:
                required |= {"vault/Home.md", "notion-import.zip"}
        else:
            required = {
                "export-manifest.json",
                "vault/Home.md",
                "agent-entry.md",
                "library-index.jsonl",
                "graph.jsonl",
                "records.csv",
                "notion-import.zip",
                "vault/hubs/wing-ai.md",
                "vault/hubs/wing-authored.md",
            }
        if declared is not None:
            if isinstance(declared, bool) or not isinstance(declared, int) or declared < 0:
                raise ValueError("invalid Zettel count")
            required.add("vault/hubs/zettels-1.md")
        if len(expected) != len(manifest["files"]) or not required <= expected:
            raise ValueError("invalid export inventory")
        for relative in expected:
            _safe_relative(relative)
    except (OSError, ValueError, KeyError, TypeError) as error:
        return [f"export manifest missing or invalid: {error}"]
    existing: set[str] = set()
    for path in destination.rglob("*"):
        _check(check_callback)
        relative = path.relative_to(destination).as_posix()
        if path.is_symlink():
            problems.append(f"symlink in export: {relative}")
        elif path.is_file():
            existing.add(relative)
    for relative in sorted(expected - existing):
        problems.append(f"missing export file: {relative}")
    for relative in sorted(existing - expected - _LATER_RECEIPTS):
        problems.append(f"unexpected export file: {relative}")
    if any(problem.startswith("symlink in export:") for problem in problems):
        return problems
    problems.extend(
        _verify_zettel_pages(destination, existing, declared, expected_zettels, check_callback)
    )
    if profile in {PROFILE_CARDS, PROFILE_FULL}:
        try:
            problems.extend(
                f"vault {line}"
                for line in hard_cap_problems(measure_vault(destination / "vault", caps))
            )
        except OSError as error:
            problems.append(f"vault unreadable: {error}")

    anchors: dict[Path, set[str]] = {}
    for relative in sorted(expected & existing):
        _check(check_callback)
        if not relative.endswith(".md"):
            continue
        path = destination / relative
        try:
            _no_symlinks(path)
            for line in _markdown(path, check_callback):
                for match in _LINK.finditer(line):
                    target = urlsplit(unquote(match[1]))
                    if target.scheme == "file" and not target.netloc:
                        continue
                    if target.scheme or target.netloc or "\\" in target.path:
                        problems.append(f"unsafe link in {relative}: {match[1]}")
                        continue
                    # The inventory above rejects every symlink first. Normalize
                    # lexically here rather than walking the filesystem for each
                    # repeated occurrence link (there may be millions).
                    linked = _absolute(path.parent / target.path) if target.path else path
                    if not linked.is_relative_to(destination):
                        problems.append(f"escaping link in {relative}: {match[1]}")
                    elif linked.relative_to(destination).as_posix() not in existing:
                        problems.append(f"missing link target in {relative}: {match[1]}")
                    elif target.fragment:
                        if linked not in anchors:
                            anchors[linked] = _anchors(linked, check_callback)
                        if target.fragment not in anchors[linked]:
                            kind = (
                                "unresolved source anchor"
                                if _ZETTEL_PAGE.fullmatch(relative)
                                else "missing anchor"
                            )
                            problems.append(f"{kind} in {relative}: {match[1]}")
        except (OSError, UnicodeError) as error:
            problems.append(f"unreadable Markdown {relative}: {error}")

    identities: set[str] = set()
    noted: dict[str, str | None] = {}
    try:
        with (destination / "library-index.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                _check(check_callback)
                row = json.loads(line)
                uid = _uid(row["uid"])
                path = row.get("path")
                if profile is None:
                    consistent = row.get("schema") == INDEX_SCHEMA and path == (
                        f"vault/records/{uid}.md"
                    )
                else:
                    consistent = row.get("schema") == INDEX_SCHEMA_V2 and (
                        path is None or (isinstance(path, str) and path in expected)
                    )
                if uid in identities or not consistent:
                    raise ValueError("inconsistent library index identity")
                identities.add(uid)
                noted[uid] = path
        if len(identities) != manifest["records"]:
            problems.append("library index record count differs from manifest")
        if profile is not None:
            problems.extend(_verify_profile_index(profile, manifest, noted, expected))
        elif {f"vault/records/{uid}.md" for uid in identities} != {
            path for path in expected if path.startswith("vault/records/")
        }:
            problems.append("record inventory differs from library index")
        nodes: dict[str, str] = {}
        missing_endpoint = False
        with (destination / "graph.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                _check(check_callback)
                row = json.loads(line)
                if row["type"] == "node":
                    if row["id"] in nodes:
                        raise ValueError("duplicate graph node")
                    nodes[row["id"]] = row["kind"]
                    if row.get("path") and row["path"] not in expected:
                        raise ValueError("graph node path absent from inventory")
                elif row["type"] == "edge":
                    # The writer declares endpoints before their first edge.
                    # Avoid retaining an O(occurrences) edge list in memory.
                    missing_endpoint |= row["source"] not in nodes or row["target"] not in nodes
                else:
                    raise ValueError("unknown graph row type")
        if {node for node, kind in nodes.items() if kind == "record"} != identities:
            problems.append("graph records differ from library index")
        if sum(kind == "block" for kind in nodes.values()) != manifest["blocks"]:
            problems.append("graph block count differs from manifest")
        if sum(kind == "zettel" for kind in nodes.values()) != (declared or 0):
            problems.append("graph Zettel count differs from manifest")
        if missing_endpoint:
            problems.append("graph has missing endpoint")
    except (OSError, ValueError, KeyError, TypeError) as error:
        problems.append(f"index or graph missing or invalid: {error}")

    if profile == PROFILE_NONE:
        return problems
    try:
        members = set(manifest["notion_members"])
        if members != {path for path in expected if path.endswith(".md") or path == "records.csv"}:
            raise ValueError("Notion member inventory differs from Markdown and CSV tree")
        with zipfile.ZipFile(destination / "notion-import.zip") as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or set(names) != members:
                raise ValueError("ZIP members differ from declared Markdown and CSV tree")
            for relative in names:
                _check(check_callback)
                _safe_relative(relative)
                path = destination / relative
                _no_symlinks(path)
                if not path.is_file():
                    problems.append(f"missing ZIP counterpart: {relative}")
                    continue
                if archive.getinfo(relative).file_size != path.stat().st_size:
                    problems.append(f"ZIP bytes differ: {relative}")
                    continue
                with archive.open(relative) as member, path.open("rb") as local:
                    while True:
                        _check(check_callback)
                        zipped = member.read(_CHUNK)
                        original = local.read(_CHUNK)
                        if zipped != original:
                            problems.append(f"ZIP bytes differ: {relative}")
                            break
                        if not zipped:
                            break
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        problems.append(f"Notion ZIP missing or invalid: {error}")
    return problems
