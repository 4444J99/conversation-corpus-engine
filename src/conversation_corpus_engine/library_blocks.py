"""Lossless text spans and explicitly verified repetition references.

Offsets are Python string indices, not byte offsets. Joining ``text`` from the
returned blocks reproduces the input exactly. A matching digest is an index,
not permission to discard either occurrence or its provenance.

Only line-ending spelling, terminal paragraph newlines and a common explicit
Markdown blockquote depth are normalized for matching. Case, spaces, spelling,
inline code and paragraph order are never folded. The caller retains each raw
occurrence and chooses a canonical target; these helpers have no global state.
"""

from __future__ import annotations

import hashlib
import html
import re
from collections.abc import Mapping, Sequence
from typing import Literal, TypedDict
from urllib.parse import quote, unquote, urlsplit

from .archive_text import FenceTracker
from .archive_vault import INLINE_CODE_RE, neutralize

MIN_PROSE_CHARACTERS = 120
_QUOTE_PREFIX = re.compile(r"^ {0,3}>[ \t]?")
_INDENTED_CODE = re.compile(r"^(?: {4}|\t)")
_TERMINAL_NEWLINES = re.compile(r"\n+$")


class TextBlock(TypedDict):
    start: int
    end: int
    text: str
    digest: str
    raw_digest: str
    canonical_text: str
    normalization: list[str]
    eligible: bool
    kind: Literal["prose", "code", "separator"]


class BlockReference(TypedDict):
    label: str
    href: str


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _unquote_line(line: str) -> tuple[str, int]:
    depth = 0
    while match := _QUOTE_PREFIX.match(line):
        line = line[match.end() :]
        depth += 1
    return line, depth


def _canonical(text: str, *, prose: bool) -> tuple[str, list[str]]:
    normalized = _lf(text)
    transforms: list[str] = []
    if normalized != text:
        transforms.append("line-endings-lf")
    if prose:
        lines = normalized.splitlines(keepends=True)
        depths = [_unquote_line(line)[1] for line in lines if line.strip()]
        common_depth = min(depths, default=0)
        if common_depth:
            for index, original_line in enumerate(lines):
                line = original_line
                for _ in range(common_depth):
                    match = _QUOTE_PREFIX.match(line)
                    if match is None:
                        break
                    line = line[match.end() :]
                lines[index] = line
            normalized = "".join(lines)
            transforms.append(f"common-quote-depth:{common_depth}")
    if normalized.endswith("\n"):
        normalized = normalized.rstrip("\n")
        transforms.append("terminal-newline")
    return normalized, transforms


def _block(
    text: str, start: int, end: int, kind: Literal["prose", "code", "separator"]
) -> TextBlock:
    raw = text[start:end]
    canonical, transforms = _canonical(raw, prose=kind == "prose")
    return {
        "start": start,
        "end": end,
        "text": raw,
        "digest": _digest(canonical),
        "raw_digest": _digest(raw),
        "canonical_text": canonical,
        "normalization": transforms,
        "eligible": (
            kind == "prose"
            and len(canonical.strip()) >= MIN_PROSE_CHARACTERS
            and "`" not in canonical
        ),
        "kind": kind,
    }


def split_blocks(text: str) -> list[TextBlock]:
    """Partition text into paragraphs, separators and protected code spans.

    Fenced code includes its delimiters and blank lines. The CommonMark fence
    tracker recognizes longer wrappers around shorter inner fences. Quoted
    fences and indented lines are also protected. An unclosed fence protects
    the remainder rather than guessing where prose resumes.
    """
    blocks: list[TextBlock] = []
    tracker = FenceTracker()
    start = position = 0
    previous: Literal["prose", "code", "separator"] | None = None
    for line in text.splitlines(keepends=True):
        probe, _ = _unquote_line(line)
        fence_state = tracker.feed(probe)
        kind: Literal["prose", "code", "separator"]
        if fence_state or _INDENTED_CODE.match(probe):
            kind = "code"
        elif not probe.strip():
            kind = "separator"
        else:
            kind = "prose"
        if previous is not None and kind != previous:
            blocks.append(_block(text, start, position, previous))
            start = position
        previous = kind
        position += len(line)
    if previous is not None:
        blocks.append(_block(text, start, position, previous))
    return blocks


def _reference_href(href: str) -> str:
    """Validate portable relative links; the exporter validates vault containment."""
    if not isinstance(href, str) or not href:
        raise ValueError("reference href must be a non-empty relative link")
    decoded = href
    # Reject encoded schemes, slash prefixes and controls as well as their
    # literal forms. Recursive quoting is not a way around the same checks.
    for _ in range(4):
        expanded = unquote(decoded)
        if expanded == decoded:
            break
        decoded = expanded
    if any(ord(char) < 32 or ord(char) == 127 for char in decoded) or "\\" in decoded:
        raise ValueError("reference href contains a control character or backslash")
    parsed = urlsplit(decoded)
    if parsed.scheme or parsed.netloc or decoded.startswith("/"):
        raise ValueError("reference href must be relative, never an external or absolute target")
    if not parsed.path and not parsed.fragment:
        raise ValueError("reference href must name a record or fragment")
    return quote(href, safe="/#?=&%+~.-_")


def _reference_label(label: str) -> str:
    value = " ".join(str(label).split()) or "source"
    value = html.escape(value, quote=False)
    for original, replacement in (("[", "("), ("]", ")"), ("\\", "&#92;"), ("`", "&#96;")):
        value = value.replace(original, replacement)
    return value


def _terminal_newlines(text: str) -> str:
    match = _TERMINAL_NEWLINES.search(_lf(text))
    return match.group() if match else ""


def _literal_markdown(text: str) -> str:
    """Keep code literal and prevent source text from creating active links/HTML.

    ``neutralize`` supplies the archive's heading, tag and open-fence handling.
    Entities additionally make ordinary Markdown links/images and raw HTML
    inert in both Obsidian and portable Markdown consumers.
    """
    tracker = FenceTracker()
    lines: list[str] = []
    for raw in text.splitlines():
        if tracker.feed(raw):
            lines.append(raw)
            continue
        pieces = INLINE_CODE_RE.split(raw)
        for index in range(0, len(pieces), 2):
            pieces[index] = (
                html.escape(pieces[index], quote=False).replace("[", "&#91;").replace("]", "&#93;")
            )
        lines.append("".join(pieces))
    result = neutralize("\n".join(lines))
    return result.rstrip("\n") + _terminal_newlines(text)


def render_blocks(blocks: Sequence[TextBlock], references: Mapping[str, BlockReference]) -> str:
    """Render a safe derivative, replacing only eligible supplied references.

    A reference maps a full block digest to a human label and a relative
    Markdown target. It does not decide first occurrence or overwrite the
    source. Callers must verify target containment/existence and must not make
    the chosen source point back to itself.
    """
    output: list[str] = []
    expected_start = 0
    for block in blocks:
        raw = block["text"]
        if block["start"] != expected_start or block["end"] != expected_start + len(raw):
            raise ValueError("blocks must be contiguous exact source spans beginning at zero")
        if block["raw_digest"] != _digest(raw) or block["digest"] != _digest(
            block["canonical_text"]
        ):
            raise ValueError("block digest no longer matches its source or canonical text")
        canonical, transforms = _canonical(raw, prose=block["kind"] == "prose")
        if canonical != block["canonical_text"] or transforms != block["normalization"]:
            raise ValueError("block canonical text or normalization no longer matches its source")
        expected_start = block["end"]
        reference = references.get(block["digest"]) if block["eligible"] else None
        if reference is None:
            output.append(_literal_markdown(raw))
        else:
            href = _reference_href(reference["href"])
            label = _reference_label(reference["label"])
            output.append(f"[Repeated passage: {label}]({href})" + _terminal_newlines(raw))
    return "".join(output)
