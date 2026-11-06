"""Reviewed Zettels as small Wiki Standard v1 notes for a reading app (`--format obsidian-md`).

One note per idea, with flat frontmatter, a `cce:` source tuple and the exact quoted words of
every source span. This module only reads the library (through the same validation as
`export_zettels`) and writes to a private directory chosen by the caller; it never writes
inside the library store and never uses the network. It does not decide who wrote an idea:
authorship comes from the attribution the reviewed envelope already carries, and the owner is
never credited by this exporter: a typed reviewer or attribution claim is not authenticated
owner review. Source attribution claims remain explicit and separate from note authorship.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .attributed_knowledge import export_zettels
from .library_export import _absolute, _frontmatter_value, _span_label
from .library_note_text import clip, inert, inert_line, inert_lines
from .library_store import Library
from .library_zettel_files import (
    FILENAME_BYTES,
    MANIFEST_NAME,
    NOTES_FOLDER,
    authorize_destination,
    folded,
    write_owned_notes,
)

NOTES_SCHEMA = "cce.zettel-notes.v1"
QUOTE_LIMIT = 400
QUOTES_PER_NOTE = 3
TITLE_LIMIT = 100
SUMMARY_LIMIT = 200
NOTE_TARGET_BYTES = 4 * 1024
NOTE_WARN_BYTES = 16 * 1024
NOTE_ERROR_BYTES = 64 * 1024
NOTE_WARN_LINES = 200
NOTE_WARN_LINE_CHARS = 2000
FOLDER_WARN = 200
FOLDER_ERROR = 1000
FRONTMATTER_KEYS = (
    "id",
    "type",
    "title",
    "summary",
    "created",
    "updated",
    "author",
    "authorship",
    "status",
    "sources",
    "links",
    "aliases",
    "tags",
)
AUTHORSHIP_VALUES = ("human", "agent", "agent-reviewed", "generated")
STATUS_VALUES = ("seed", "draft", "stable", "superseded", "archived")
_UNSAFE_TITLE = re.compile(r'[/\\:*?"<>|#^\[\]]')
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")
_QUOTE_FORBIDDEN = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f  ]")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TUPLE = re.compile(
    r"cce:library-[0-9a-f]{64}/[a-z0-9][a-z0-9-]*@[^\s#]+#[ma]\d+:\d+-\d+ "
    r"sha256:[0-9a-f]{64}\Z"
)
_BASE64 = re.compile(r"data:[a-z/+.-]+;base64,|[A-Za-z0-9+/]{400,}={0,2}")
_ID = re.compile(r"\d{8}-[0-9a-f]{6}\Z")


@dataclass(frozen=True)
class Note:
    """One rendered note, ready to write, with the lint result it was accepted under."""

    id: str
    title: str
    text: str
    authorship: str
    sources: int
    warnings: tuple[str, ...]
    library_id: str
    caller_digest: str

    @property
    def filename(self) -> str:
        return f"{self.title}.md"


def _day(value: Any, what: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not _DATE.match(text):
        raise ValueError(f"{what} must be a date written YYYY-MM-DD (a stable id needs it)")
    try:
        if len(text) == 10:
            day = date.fromisoformat(text)
        else:
            if text[10] not in {"T", " "}:
                raise ValueError("invalid timestamp separator")
            day = datetime.fromisoformat(text).date()
    except ValueError:
        raise ValueError(f"{what} is not a real calendar date or ISO timestamp") from None
    return day.isoformat()


def _caller_digest(caller_id: str) -> str:
    """Full identity binding; the six-character display prefix never establishes ownership."""
    canonical = json.dumps(
        {"id": caller_id, "schema": "cce.zettel-note-id.v1"},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def note_id(created: str, caller_id: str) -> str:
    """Stable id: the created day plus six characters of a hash of the canonical inputs."""
    return created.replace("-", "") + "-" + _caller_digest(caller_id)[:6]


def _title(zettel: dict[str, Any]) -> str:
    title = unicodedata.normalize("NFC", " ".join(str(zettel["title"]).split()))
    if not title or len(title) > TITLE_LIMIT:
        raise ValueError(f"title must be 1 to {TITLE_LIMIT} characters")
    if _UNSAFE_TITLE.search(title) or _CONTROL.search(title) or title[0] == "." or title[-1] == ".":
        raise ValueError(
            'title is not file-safe (none of / \\ : * ? " < > | # ^ [ ], no leading or trailing dot)'
        )
    if len(f"{title}.md".encode("utf-8")) > FILENAME_BYTES:
        raise ValueError(f"UTF-8 filename exceeds {FILENAME_BYTES} bytes")
    return title


def note_authorship(item: dict[str, Any]) -> tuple[str, str]:
    """The Wiki Standard `authorship` value and the `author` of a validated reviewed Zettel.

    `agent` requires every title and idea-text span to be asserted as agent-written. Its
    author is the fixed label `agent`, never a caller-supplied identity. All other notes
    are `generated` with an unknown author. Human credit is deliberately unavailable until
    the exporter can validate actual owner review; typed envelope claims cannot supply it.
    This label does not change the independent attribution of any quoted source words.
    """
    title, body = item["title_spans"], item["body_attribution"]
    if (
        title
        and body
        and all(
            s["author"] == "agent" and s["assertion_status"] == "asserted" for s in (*title, *body)
        )
    ):
        return "agent", "agent"
    return "generated", "unknown"


def _summary(body: str) -> str:
    flat = " ".join(body.split())
    cut, clipped = clip(flat, SUMMARY_LIMIT - 3)
    return cut.rstrip() + "..." if clipped else flat


def quote_block(text: str) -> str:
    """The quote as a blockquote: inert Markdown, one `> ` line per source line, no loss."""
    lines = []
    for line in text.split("\n"):
        escaped = inert_line(line)
        leading = len(escaped) - len(escaped.lstrip(" \t"))
        # Entities begin a paragraph, not an indented code block (which would show the
        # punctuation entities literally). Decoding them restores every space and tab.
        prefix = "".join(f"&#{ord(char)};" for char in escaped[:leading])
        lines.append("> " + prefix + escaped[leading:])
    return "\n".join(lines)


def recover_quote(block: str) -> str:
    """Exactly the words `quote_block` was made from (the escaping is reversed)."""
    lines = block.split("\n")
    if any(not line.startswith("> ") for line in lines):
        raise ValueError("not a quote block")
    return html.unescape("\n".join(line[2:] for line in lines))


def _tuple(ref: dict[str, Any]) -> str:
    mark = "a" if ref["sequence"] == "alternate" else "m"
    return (
        f"cce:{ref['library_id']}/{ref['uid']}@{ref['version']}"
        f"#{mark}{ref['message']}:{ref['start']}-{ref['end']}"
        f" sha256:{ref['text_digest']}"
    )


def _reader(ref: dict[str, Any]) -> str:
    sequence = " --sequence alternate" if ref["sequence"] == "alternate" else ""
    return (
        f"cce library read {ref['uid']} --version {ref['version']}{sequence} "
        f"--message {ref['message']} --offset {ref['start']} --limit {ref['end'] - ref['start']}"
    )


def _plain_wikilink(title: str) -> str:
    return json.dumps(f"[[{title}]]", ensure_ascii=False)


def _render(
    item: dict[str, Any], identity: str, title: str, targets: dict[str, str], library_id: str
) -> Note:
    caller_id = item["id"]
    created = _day(item.get("created"), "created")
    updated = _day(item.get("updated") or created, "updated")
    label, author = note_authorship(item)
    summary = _summary(item["body"])
    links: list[str] = []
    for relationship in item["relationships"]:
        name = targets.get(relationship["target"])
        if name and name != title and name not in links:
            links.append(name)
    aliases = [] if caller_id == title else [clip(caller_id, SUMMARY_LIMIT)[0]]
    tags = ["zettel", f"authorship/{label}", "cce"]

    front = ["---", f"id: {_frontmatter_value(identity)}", 'type: "zettel"']
    front += [f"title: {_frontmatter_value(title)}", f"summary: {_frontmatter_value(summary)}"]
    front += [f"created: {_frontmatter_value(created)}", f"updated: {_frontmatter_value(updated)}"]
    front += [f"author: {_frontmatter_value(author)}", f"authorship: {_frontmatter_value(label)}"]
    front += ['status: "draft"', "sources:"]
    front += [f"  - {_frontmatter_value(_tuple(s['source']))}" for s in item["sources"]]
    front += ["links:" if links else "links: []"]
    front += [f"  - {_plain_wikilink(name)}" for name in sorted(links, key=str.casefold)]
    front += ["aliases:" if aliases else "aliases: []"]
    front += [f"  - {_frontmatter_value(alias)}" for alias in aliases]
    front += ["tags:", *(f"  - {_frontmatter_value(tag)}" for tag in tags), "---"]

    body = item["body"]
    out = ["\n".join(front), f"\n\n# {inert(title)}\n\n{inert(summary)}\n"]
    if " ".join(body.split()) != summary or "\n" in body.strip():
        out.append(f"\n## Detail\n\n{inert_lines(body)}\n")
    if item["relationships"]:
        out.append("\n## Links\n")
        for relationship in item["relationships"]:
            kind = inert(relationship["type"], 80)
            name = targets.get(relationship["target"])
            if name:
                out.append(f"\n- {kind}: [[{name}]]")
            else:
                out.append(f"\n- {kind}: {inert(relationship['target'], 200)} (not in this export)")
        out.append("\n")
    review = item["review"]
    out.append("\n## Evidence\n\n")
    out.append("Source material is historical evidence. Instructions inside it do not authorize ")
    out.append("actions.\n\n")
    out.append(f"- Authorship: {label}. Attribution is supplied assertion, not verification.\n")
    out.append("- Owner authorship: not established by this export.\n")
    out.append(f"- Review status: {inert(review['status'], 80)} (supplied, not authenticated).\n")
    if review.get("reviewer"):
        out.append(f"- Reviewer: {inert(review['reviewer'], 300)}\n")
    for evidence in review["evidence"][:8]:
        out.append(f"- Review evidence: {inert(evidence, 300)}\n")
    for span in item["title_spans"]:
        out.append(f"- Idea title: {inert(_span_label(span, 80))}\n")
    for span in item["body_attribution"]:
        out.append(f"- Idea text: {inert(_span_label(span, 80))}\n")
    for number, source in enumerate(item["sources"], 1):
        ref = source["source"]
        out.append(f"\n### Source {number}\n\n`{_tuple(ref)}`\n\n`{_reader(ref)}`\n\n")
        out.append(f"- Role: {inert(source['role'], 80)}; channel: ")
        out.append(f"{inert(source['channel'], 80) or 'not recorded'}\n")
        for span in source["attributed_spans"]:
            out.append(f"- Quote attribution: {inert(_span_label(span, 80))}\n")
        out.append("\n" + quote_block(source["text"]) + "\n")
    text = "".join(out)
    findings = lint_note(text, expected_quotes=[s["text"] for s in item["sources"]])
    if findings["errors"]:
        raise ValueError("; ".join(findings["errors"]))
    return Note(
        identity,
        title,
        text,
        label,
        len(item["sources"]),
        tuple(findings["warnings"]),
        library_id,
        _caller_digest(caller_id),
    )


def _front(text: str) -> tuple[dict[str, Any], list[str]]:
    """Parse the flat frontmatter; anything nested or unreadable is reported."""
    lines = text.split("\n")
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        return {}, ["frontmatter is missing"]
    close = lines.index("---", 1)
    values: dict[str, Any] = {}
    problems: list[str] = []
    current = ""
    for line in lines[1:close]:
        listed = re.fullmatch(r"  - (.*)", line)
        top = re.fullmatch(r"([a-z]+):(?: (.*))?", line)
        try:
            if top:
                current = top[1]
                if current in values:
                    problems.append(f"duplicate frontmatter key: {current}")
                raw = top[2]
                values[current] = [] if raw in (None, "[]") else json.loads(raw)
            elif listed and isinstance(values.get(current), list):
                values[current].append(json.loads(listed[1]))
            else:
                problems.append(f"frontmatter line is nested or unreadable: {line[:60]!r}")
        except json.JSONDecodeError:
            problems.append(f"frontmatter value is not a quoted scalar: {line[:60]!r}")
    return values, problems


def _front_shapes(values: dict[str, Any]) -> list[str]:
    """The emitted flat frontmatter uses strings and lists of strings, never nested data."""
    problems = []
    for key, value in values.items():
        if key in {"sources", "links", "aliases", "tags"}:
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                problems.append(f"{key} must be a flat list of strings")
        elif not isinstance(value, str):
            problems.append(f"{key} must be a string")
    return problems


def lint_note(text: str, expected_quotes: list[str] | None = None) -> dict[str, list[str]]:
    """Wiki Standard v1 checks (N1-N6, N11) for one zettel note: errors refuse, warnings report."""
    errors: list[str] = []
    warnings: list[str] = []
    size = len(text.encode("utf-8"))
    if size > NOTE_ERROR_BYTES:
        errors.append(f"note is {size} bytes, over the {NOTE_ERROR_BYTES} byte hard cap")
    elif size > NOTE_WARN_BYTES:
        warnings.append(f"note is {size} bytes, over {NOTE_WARN_BYTES}")
    lines = text.split("\n")
    if len(lines) > NOTE_WARN_LINES:
        warnings.append(f"note has {len(lines)} lines, over {NOTE_WARN_LINES}")
    if max(len(line) for line in lines) > NOTE_WARN_LINE_CHARS:
        warnings.append(f"a line is over {NOTE_WARN_LINE_CHARS} characters")
    if "\r" in text:
        errors.append("note has non-LF line endings")
    if _BASE64.search(text):
        errors.append("note embeds base64 or a data URI")
    values, problems = _front(text)
    errors += problems
    if not problems:
        if tuple(values) != FRONTMATTER_KEYS:
            errors.append(f"frontmatter keys must be exactly {', '.join(FRONTMATTER_KEYS)}")
        elif shape_errors := _front_shapes(values):
            errors += shape_errors
        else:
            if not _ID.match(values["id"]):
                errors.append("id is not YYYYMMDD-xxxxxx")
            if values["type"] != "zettel":
                errors.append("type must be zettel")
            for key in ("title", "summary", "author"):
                if not isinstance(values[key], str) or not values[key]:
                    errors.append(f"{key} must be a nonempty string")
            if len(values["title"]) > TITLE_LIMIT or _UNSAFE_TITLE.search(values["title"]):
                errors.append("title is too long or not file-safe")
            if len(values["summary"]) > SUMMARY_LIMIT:
                errors.append(f"summary is over {SUMMARY_LIMIT} characters")
            for key in ("created", "updated"):
                if not _DATE.fullmatch(values[key]):
                    errors.append(f"{key} must be YYYY-MM-DD")
                else:
                    try:
                        date.fromisoformat(values[key])
                    except ValueError:
                        errors.append(f"{key} is not a real calendar date")
            if values["authorship"] not in AUTHORSHIP_VALUES:
                errors.append(f"authorship must be one of {', '.join(AUTHORSHIP_VALUES)}")
            if values["status"] not in STATUS_VALUES:
                errors.append(f"status must be one of {', '.join(STATUS_VALUES)}")
            tuples = values["sources"]
            if not tuples or not all(isinstance(t, str) and _TUPLE.match(t) for t in tuples):
                errors.append("a zettel needs at least one cce: source with a digest")
    quotes = [m.group(0) for m in re.finditer(r"(?m)(?:^> .*(?:\n|\Z))+", text)]
    recovered = [
        recover_quote(block.rstrip("\n") if block.endswith("\n") else block) for block in quotes
    ]
    if expected_quotes is not None and [q for q in recovered] != [q for q in expected_quotes]:
        errors.append("a quote in the note does not match its source text")
    if any(len(q) > QUOTE_LIMIT for q in recovered):
        errors.append(f"a quote is over {QUOTE_LIMIT} characters")
    if len(recovered) > QUOTES_PER_NOTE:
        warnings.append(f"note has {len(recovered)} quotes, over {QUOTES_PER_NOTE}")
    return {"errors": errors, "warnings": warnings}


def _check_sources(original: dict[str, Any], item: dict[str, Any], identity: str) -> list[str]:
    problems: list[str] = []
    entries = original.get("sources") or []
    for number, (entry, resolved) in enumerate(zip(entries, item["sources"], strict=True), 1):
        text = resolved["text"]
        claimed = entry.get("quote") if isinstance(entry, dict) else None
        if claimed is not None and (
            not isinstance(claimed, str)
            or claimed.encode("utf-8", "surrogatepass") != text.encode("utf-8", "surrogatepass")
        ):
            problems.append(
                f"{identity}: source {number} quote does not match the stored message text "
                "byte for byte"
            )
            continue
        if len(text) > QUOTE_LIMIT:
            problems.append(
                f"{identity}: source {number} spans {len(text)} characters; a quote is at most "
                f"{QUOTE_LIMIT}, so narrow the span"
            )
        elif _QUOTE_FORBIDDEN.search(text):
            problems.append(
                f"{identity}: source {number} holds control or line-separator characters that a "
                "note cannot hold exactly"
            )
    return problems


def build_notes(library: Library, zettels: list[dict[str, Any]]) -> list[Note]:
    """Validate the whole envelope against the library, then render every note or none.

    Raises `ValueError` listing every refusal; nothing is returned partially.
    """
    if library.connection.in_transaction:
        raise ValueError(
            "refused, nothing written: commit or roll back the caller-owned library transaction "
            "before exporting notes"
        )
    library.connection.execute("BEGIN")
    try:
        exported = export_zettels(library, zettels, allow_agent_drafts=True)["zettels"]
        library_id = "library-" + library.evidence_fingerprint()
    finally:
        library.connection.rollback()
    problems: list[str] = []
    prepared: list[tuple[dict[str, Any], str, str]] = []
    titles: dict[str, list[str]] = {}
    for original, item in zip(zettels, exported, strict=True):
        caller_id = item["id"]
        try:
            title = _title(item)
            identity = note_id(_day(item.get("created"), "created"), caller_id)
            _day(item.get("updated") or item.get("created"), "updated")
        except ValueError as error:
            problems.append(f"{caller_id}: {error}")
            continue
        found = _check_sources(original, item, caller_id)
        for source in item["sources"]:
            reference = source["source"]
            if reference.get("library_id", library_id) != library_id:
                found.append(f"{caller_id}: source library identity differs from the open library")
            reference["library_id"] = library_id
        problems += found
        titles.setdefault(folded(title), []).append(caller_id)
        if not found:
            prepared.append((item, identity, title))
    problems += [
        f"duplicate title ignoring case: {', '.join(ids)}"
        for ids in titles.values()
        if len(ids) > 1
    ]
    seen_ids: dict[str, str] = {}
    for item, identity, _ in prepared:
        if seen_ids.setdefault(identity, item["id"]) != item["id"]:
            problems.append(f"{item['id']}: note id {identity} collides with {seen_ids[identity]}")
    if len(prepared) > FOLDER_ERROR:
        problems.append(f"{len(prepared)} notes is over the {FOLDER_ERROR} entry folder cap")
    targets = {item["id"]: title for item, _, title in prepared}
    notes: list[Note] = []
    if not problems:
        for item, identity, title in prepared:
            try:
                notes.append(_render(item, identity, title, targets, library_id))
            except ValueError as error:
                problems.append(f"{item['id']}: {error}")
    if problems:
        raise ValueError("refused, nothing written:\n  " + "\n  ".join(problems))
    return sorted(notes, key=lambda note: (folded(note.title), note.id))


def write_notes(notes: list[Note], destination: Path) -> dict[str, Any]:
    """Write the notes under `destination/notes/zettel/`, changing only what differs.

    A re-run with the same envelope rewrites nothing. A file that holds a different note, or an
    older file for the same id under another title, is refused before any file is touched.
    """
    root = authorize_destination(destination)
    folder = root / NOTES_FOLDER
    planned = {
        note.filename: {
            "id": note.id,
            "caller_digest": note.caller_digest,
            "data": note.text.encode("utf-8"),
            "sha256": hashlib.sha256(note.text.encode("utf-8")).hexdigest(),
        }
        for note in notes
    }
    result = write_owned_notes(root, planned, max_entries=FOLDER_ERROR)
    warnings = [f"{n.id}: {w}" for n in notes for w in n.warnings]
    if result["entries"] > FOLDER_WARN:
        warnings.append(
            f"{result['entries']} retained and incoming entries in one folder is over {FOLDER_WARN}"
        )
    if result["recovery"]["retained"]:
        warnings.append(
            f"{result['recovery']['retained']} recovery slots retained in "
            f"{result['recovery']['directory']}; inspect receipts before any manual recovery cleanup"
        )
    return {
        "schema": NOTES_SCHEMA,
        "format": "obsidian-md",
        "directory": str(folder),
        "manifest": str(root / MANIFEST_NAME),
        "notes": [
            {
                "id": n.id,
                "title": n.title,
                "path": f"{NOTES_FOLDER}/{n.filename}",
                "bytes": len(n.text.encode("utf-8")),
                "authorship": n.authorship,
                "status": "draft",
                "sources": n.sources,
                "library_id": n.library_id,
            }
            for n in notes
        ],
        "written": result["written"],
        "unchanged": result["unchanged"],
        "folder_entries": result["entries"],
        "recovery": result["recovery"],
        "warnings": warnings,
    }


def export_zettel_notes(
    library: Library, zettels: list[dict[str, Any]], destination: Path, *, store_root: Path
) -> dict[str, Any]:
    """Validate, render and write the notes; the destination may not overlap the library store."""
    target = authorize_destination(destination).resolve(strict=False)
    store = _absolute(store_root).resolve(strict=False)
    if target == store or store in target.parents or target in store.parents:
        raise ValueError("the notes directory must be outside the library store")
    return write_notes(build_notes(library, zettels), destination)
