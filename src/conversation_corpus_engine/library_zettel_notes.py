"""Reviewed Zettels as small Wiki Standard v1 notes for a reading app (`--format obsidian-md`).

One note per idea, with flat frontmatter, a `cce:` source tuple and the exact quoted words of
every source span. This module only reads the library (through the same validation as
`export_zettels`) and writes to a private directory chosen by the caller; it never writes
inside the library store and never uses the network. Nobody types an owner identity and no typed
reviewer, owner id or eligibility flag authenticates owner review, so none can grant `human`.
The authorship engine derives who wrote every quoted word (`library_zettel_authorship`); the idea
text is agent-written by asserted agent draft, or made only of its quotes (`text_origin:
quoted`), and the owner is credited only when every quoted character is derived as the owner's
(see `note_authorship`). That credit is rules-derived, not owner-reviewed, and the note says so.
Source attribution claims remain explicit and separate from note authorship.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .attributed_knowledge import export_zettels
from .attribution_rules import AGENT, OWNER, RULES_VERSION
from .library_export import _absolute, _frontmatter_value, _span_label
from .library_note_text import clip, inert, inert_line, inert_lines
from .library_store import Library
from .library_zettel_authorship import (
    DEFAULT_DEADLINE_SECONDS,
    QuoteAuthorship,
    derive_quote_authors,
    weakest,
)
from .library_zettel_files import (
    FILENAME_BYTES,
    MANIFEST_NAME,
    NOTES_FOLDER,
    authorize_destination,
    folded,
    write_owned_notes,
)
from .origin_hints import OriginHints

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
    "authorship_basis",
    "authorship_rules",
    "authorship_note",
    "status",
    "sources",
    "links",
    "aliases",
    "tags",
)
AUTHORSHIP_VALUES = ("human", "agent", "agent-reviewed", "generated")
# How the label was reached: `derived` from the stored messages by the rules, `declared` by the
# reviewed envelope (an asserted agent draft), or `unverified` (nothing established who wrote it).
BASIS_VALUES = ("derived", "declared", "unverified")
# Who wrote the idea text and title, as the reviewed envelope declares in `text_origin`.
TEXT_ORIGINS = ("agent", "owner", "quoted")
STATUS_VALUES = ("seed", "draft", "stable", "superseded", "archived")
_UNSAFE_TITLE = re.compile(r'[/\\:*?"<>|#^\[\]]')
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")
_QUOTE_FORBIDDEN = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f  ]")
_QUOTE_LIST_OPEN = re.compile(r"(?:[0-9]{1,9}[.)]|[-+])(?:[ \t]|$)")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_KEY = re.compile(r"([a-z_]+):(?: (.*))?")
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


CAVEAT = (
    "rules-derived, not owner-reviewed: the attribution rules' precision gate has not been met "
    "(profile_eligible is false), and this export does not read the owner's review ledger"
)


@dataclass(frozen=True)
class Authorship:
    """The label of one note and how it was reached."""

    label: str
    author: str
    basis: str
    note: str
    origin: str


def _agent_asserted(item: dict[str, Any]) -> bool:
    """Every title and idea-text span is asserted as agent-written (main's agent rule)."""
    title, body = item["title_spans"], item["body_attribution"]
    return bool(
        title
        and body
        and all(
            s["author"] == "agent" and s["assertion_status"] == "asserted" for s in (*title, *body)
        )
    )


def text_origin(item: dict[str, Any]) -> str:
    """Who wrote the idea text and title: `agent`, `owner`, `quoted` or `unverified`.

    The envelope declares it in `text_origin`. Without the field, only the older agent draft
    rule applies (title and idea text all asserted as agent-written); every other older claim,
    an owner assertion included, is `unverified`.
    """
    declared = item.get("text_origin")
    if declared is None:
        return "agent" if _agent_asserted(item) else "unverified"
    if not isinstance(declared, str) or declared not in TEXT_ORIGINS:
        raise ValueError(f"text_origin must be one of {', '.join(TEXT_ORIGINS)}")
    return declared


def _flat(text: str) -> str:
    return unicodedata.normalize("NFC", " ".join(text.split()))


def made_of_quotes(item: dict[str, Any]) -> bool:
    """The idea text is exactly its quotes in order, and the title is a piece of that text."""
    quoted = " ".join(_flat(source["text"]) for source in item["sources"])
    return bool(quoted) and _flat(item["body"]) == quoted and _flat(item["title"]) in quoted


CENSUS_NOT_USED = "per-conversation derivation; library-wide repeat census not used"
CENSUS_USED = "library-wide repeat census used (opt-in, full-library derivation)"


def note_authorship(
    item: dict[str, Any], quotes: Sequence[QuoteAuthorship], *, repeat_census: bool = False
) -> Authorship:
    """`_note_authorship`, with the note saying whether the library-wide repeat census was used."""
    authorship = _note_authorship(item, quotes)
    census = CENSUS_USED if repeat_census else CENSUS_NOT_USED
    return replace(authorship, note=f"{authorship.note}; {census}")


def _note_authorship(item: dict[str, Any], quotes: Sequence[QuoteAuthorship]) -> Authorship:
    """The Wiki Standard `authorship` value, `author` and basis of a validated Zettel.

    `quotes` holds the class the authorship engine derived for each source span, in order. Nobody
    types an identity, the reviewer is provenance only and no envelope assertion grants the owner
    credit. The author is always a fixed label (`owner`, `agent` or `unknown`).

    - Idea text of agent origin (an asserted agent draft, with or without `text_origin: agent`):
      `agent`, whatever the quotes are; the label says who wrote the note text and the quotes
      are classed separately.
    - Idea text declared `quoted` and really made only of its quotes: the weakest derived quote
      class decides. `human` only when every visible quoted character is derived as the owner's
      (the one path to `human`), `agent` when the weakest is agent, `generated` otherwise.
    - Idea text declared `owner`, a `quoted` claim the text does not bear out, or anything
      nothing derives: `generated`, basis `unverified`.
    """
    origin = text_origin(item)
    if origin == "agent":
        if _agent_asserted(item):
            note = "agent origin asserted by the reviewed envelope, not verified; the quotes are "
            return Authorship("agent", AGENT, "declared", note + CAVEAT, origin)
        note = "agent origin declared without asserted agent attribution of title and text"
        return Authorship("generated", "unknown", "unverified", note, origin)
    if origin == "quoted":
        if not made_of_quotes(item):
            note = "text_origin quoted, but the idea text and title are not made only of the quotes"
            return Authorship("generated", "unknown", "unverified", note, origin)
        weak = weakest([quote.author for quote in quotes])
        if weak == OWNER:
            return Authorship("human", OWNER, "derived", CAVEAT, origin)
        if weak == AGENT:
            return Authorship(
                "agent", AGENT, "derived", f"weakest quote is agent; {CAVEAT}", origin
            )
        return Authorship(
            "generated", "unknown", "derived", f"weakest quote is {weak}; {CAVEAT}", origin
        )
    if origin == "owner":
        note = "owner text declared without derivation: nothing in the envelope grants owner credit"
        return Authorship("generated", "unknown", "unverified", note, origin)
    return Authorship(
        "generated", "unknown", "unverified", "no origin of the idea text is established", origin
    )


def _summary(body: str) -> str:
    flat = " ".join(body.split())
    cut, clipped = clip(flat, SUMMARY_LIMIT - 3)
    return cut.rstrip() + "..." if clipped else flat


def quote_block(text: str) -> str:
    """The quote as a blockquote: inert Markdown, one `> ` line per source line, no loss."""
    lines = []
    for line in text.split("\n"):
        escaped = inert_line(line).replace("~", "&#126;")
        leading = len(escaped) - len(escaped.lstrip(" \t"))
        # Entities begin a paragraph, not an indented code block (which would show the
        # punctuation entities literally). Decoding them restores every space and tab.
        prefix = "".join(f"&#{ord(char)};" for char in escaped[:leading])
        rest = escaped[leading:]
        # CommonMark consumes list markers and can renumber the quoted words. Escape
        # the opener; the shared helper already handles star markers and other syntax.
        if _QUOTE_LIST_OPEN.match(rest):
            rest = f"&#{ord(rest[0])};" + rest[1:]
        lines.append("> " + prefix + rest)
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
    item: dict[str, Any],
    identity: str,
    title: str,
    targets: dict[str, str],
    library_id: str,
    quotes: Sequence[QuoteAuthorship],
    repeat_census: bool = False,
) -> Note:
    caller_id = item["id"]
    created = _day(item.get("created"), "created")
    updated = _day(item.get("updated") or created, "updated")
    authorship = note_authorship(item, quotes, repeat_census=repeat_census)
    label, author = authorship.label, authorship.author
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
    front += [f"authorship_basis: {_frontmatter_value(authorship.basis)}"]
    front += [f"authorship_rules: {_frontmatter_value(RULES_VERSION)}"]
    front += [f"authorship_note: {_frontmatter_value(authorship.note)}"]
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
    out.append(f"- Authorship: {label} ({authorship.basis}), {inert(authorship.note, 600)}.\n")
    out.append("- Attribution claims in the envelope are supplied assertion, not verification.\n")
    out.append(f"- Idea text origin: {authorship.origin}\n")
    if label == "human":
        out.append("- Owner authorship: derived from the stored messages, not owner-reviewed.\n")
    else:
        out.append("- Owner authorship: not established by this export.\n")
    out.append(f"- Review status: {inert(review['status'], 80)} (supplied, not authenticated).\n")
    if review.get("reviewer"):
        reviewer = inert(review["reviewer"], 300)
        out.append(f"- Reviewer (provenance only, grants no authorship): {reviewer}\n")
    for evidence in review["evidence"][:8]:
        out.append(f"- Review evidence: {inert(evidence, 300)}\n")
    for span in item["title_spans"]:
        out.append(f"- Idea title: {inert(_span_label(span, 80))}\n")
    for span in item["body_attribution"]:
        out.append(f"- Idea text: {inert(_span_label(span, 80))}\n")
    for number, (source, quote) in enumerate(zip(item["sources"], quotes, strict=True), 1):
        ref = source["source"]
        out.append(f"\n### Source {number}\n\n`{_tuple(ref)}`\n\n`{_reader(ref)}`\n\n")
        out.append(f"- Role: {inert(source['role'], 80)}; channel: ")
        out.append(f"{inert(source['channel'], 80) or 'not recorded'}\n")
        detail = [f"classes {', '.join(quote.classes)}"]
        detail += [f"rules {', '.join(quote.rules)}"] if quote.rules else []
        detail += [quote.reason] if quote.reason else []
        out.append(f"- Quote authorship (derived, {RULES_VERSION}): {quote.author}")
        out.append(f" ({inert('; '.join(detail), 300)})\n")
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
        top = _KEY.fullmatch(line)
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
            if values["authorship_basis"] not in BASIS_VALUES:
                errors.append(f"authorship_basis must be one of {', '.join(BASIS_VALUES)}")
            for key in ("title", "summary", "author", "authorship_rules", "authorship_note"):
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


def build_notes(
    library: Library,
    zettels: list[dict[str, Any]],
    *,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    repeat_census: bool = False,
    origin_hints: OriginHints | None = None,
) -> list[Note]:
    """Validate the whole envelope against the library, then render every note or none.

    Authorship is derived (see `note_authorship`); no owner identity is taken. The derivation
    reads only each quoted conversation (cost per quoted conversation, within `deadline_seconds`
    for the whole export); a quote it could not derive in time is `unknown` and the note is never
    `human`. `repeat_census=True` opts in to the library-wide repeat census: a full pass over the
    whole library.
    Raises `ValueError` listing every refusal; nothing is returned partially.
    """
    if origin_hints is not None:
        origin_hints.refuse_findings()
    if isinstance(deadline_seconds, bool) or not (
        isinstance(deadline_seconds, int | float) and deadline_seconds > 0
    ):
        raise ValueError("deadline_seconds must be a positive number")
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
            text_origin(item)
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
        derived = iter(
            derive_quote_authors(
                library,
                [source["source"] for item, _, _ in prepared for source in item["sources"]],
                deadline_seconds=deadline_seconds,
                repeat_census=repeat_census,
                origin_hints=origin_hints,
            )
        )
        for item, identity, title in prepared:
            quotes = [next(derived) for _ in item["sources"]]
            try:
                notes.append(
                    _render(item, identity, title, targets, library_id, quotes, repeat_census)
                )
            except ValueError as error:
                problems.append(f"{item['id']}: {error}")
    if problems:
        raise ValueError("refused, nothing written:\n  " + "\n  ".join(problems))
    return sorted(notes, key=lambda note: (folded(note.title), note.id))


def write_notes(notes: list[Note], destination: Path) -> dict[str, Any]:
    """Write the notes under `destination/notes/zettel/`, changing only what differs.

    A re-run with the same rendered notes rewrites nothing. A file that holds a different note, or an
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
    library: Library,
    zettels: list[dict[str, Any]],
    destination: Path,
    *,
    store_root: Path,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    repeat_census: bool = False,
    origin_hints: OriginHints | None = None,
) -> dict[str, Any]:
    """Validate, render and write the notes; the destination may not overlap the library store.

    Authorship is derived from the library by the attribution rules; no owner identity is taken.
    """
    target = authorize_destination(destination).resolve(strict=False)
    store = _absolute(store_root).resolve(strict=False)
    if target == store or store in target.parents or target in store.parents:
        raise ValueError("the notes directory must be outside the library store")
    notes = build_notes(
        library,
        zettels,
        deadline_seconds=deadline_seconds,
        repeat_census=repeat_census,
        origin_hints=origin_hints,
    )
    return write_notes(notes, destination)
