"""Deterministic, versioned authorship rules over the text of one message.

The contract this extends is `attribution.py` (`cce.span-attribution.v1`): digest-bound spans
whose author is `owner`, `agent`, `third_party` or `unknown`. A provider's `user` role is not proof
that the owner wrote a span (injected system text, command output, pasted logs and quotes and
AI-written instruction files all sit in user turns), so this engine is conservative: it partitions
every character of a message into contiguous, non-overlapping spans, and a span is `owner` only
when an owner-granting rule claims it and no other rule does.

How a partition is decided:

* every rule is a pure function from a `MessageContext` to claimed character ranges;
* the rules in `RULES` run in the listed order and the first rule to claim a character keeps it
  (a later rule only receives what is still unclaimed);
* the rules that can grant `owner` run after every rule that grants anything else, so a higher
  rule can never lose a character to an owner claim;
* what no rule claims is `unknown` under the rule id `unattributed` (whitespace included), never
  `owner`; an owner-granting rule declines a piece that is only whitespace.

Every span this engine produces has `profile_eligible=False`. A rule becomes eligible only through
the precision gate (`review_statistics`), which reads an owner-review ledger; nothing in this
module reads the clock or the filesystem except the explicit ledger helpers.
"""

from __future__ import annotations

import fcntl
import functools
import hashlib
import heapq
import itertools
import json
import os
import re
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .archive_provenance import INJECTED_TAGS, classify_user_turn
from .archive_read_library import FAMILIES
from .attribution import ATTRIBUTION_SCHEMA, text_digest

RULES_VERSION = "cce.attribution-rules@1"

OWNER = "owner"
AGENT = "agent"
THIRD_PARTY = "third_party"
UNKNOWN = "unknown"
UNATTRIBUTED = "unattributed"

Range = tuple[int, int]
# A claim is (start, end, detail): half-open codepoint offsets plus a short evidence detail.
Claim = tuple[int, int, str]

DOCUMENT_KINDS = frozenset({"document", "note", "plan", "artifact", "memory", "page"})
AGENT_ROLES = frozenset(
    {"assistant", "model", "tool", "system", "function", "developer", "ai", "bot"}
)
MACHINE_CHANNELS = frozenset({"reply", "thinking", "tool", "system", "memory"})
# The coding CLIs whose user turns the archive reclassifies by provenance, plus Cursor.
CLI_PROVIDERS = frozenset(
    {family.library_provider for family in FAMILIES if family.cli} | {"cursor"}
)
# Instruction and skill files agents write or maintain, and the folders that hold them.
AGENT_FILE_NAMES = frozenset(
    {
        "skill.md",
        "claude.md",
        "agents.md",
        "gemini.md",
        "codex.md",
        "copilot-instructions.md",
        ".cursorrules",
    }
)
AGENT_DIRECTORIES = frozenset(
    {".claude", ".codex", ".gemini", ".cursor", ".opencode", ".copilot", ".agents", ".antigravity"}
)
AGENT_NAME = re.compile(
    r"\b(?:claude|anthropic|codex|openai|chatgpt|gpt(?:-?[\w.]+)?|gemini|copilot|cursor|opencode"
    r"|antigravity|assistant|llm)\b",
    re.IGNORECASE,
)
AGENT_KINDS = frozenset({"agent", "machine", "ai", "model", "assistant", "llm", "bot", "generated"})
SKILL_FRONTMATTER_KEYS = frozenset(
    {"allowed-tools", "argument-hint", "disable-model-invocation", "user-invocable"}
)


def _norm(value: Any) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def marks_agent_origin(mapping: Any) -> str:
    """The key of a provider/record field that states an agent produced the text, else ''."""
    if not isinstance(mapping, Mapping):
        return ""
    flag = mapping.get("agent_authored")
    if flag is True or _norm(flag) in {"true", "yes", "1"}:
        return "agent_authored"
    for key in ("created_by_agent", "producer_agent"):
        if mapping.get(key) not in (None, False, "", 0):
            return key
    if _norm(mapping.get("author_kind")) in AGENT_KINDS:
        return "author_kind"
    for key in ("generated_by", "authored_by"):
        value = mapping.get(key)
        if isinstance(value, str) and AGENT_NAME.search(value):
            return key
    return ""


def _path_reason(provenance: Mapping[str, Any]) -> str:
    for key in ("source_file", "source_path"):
        value = provenance.get(key)
        if not isinstance(value, str) or not value:
            continue
        parts = PurePosixPath(value.replace("\\", "/")).parts
        if not parts:
            continue
        base = parts[-1].lower()
        if base in AGENT_FILE_NAMES:
            return f"path:{base}"
        if base.endswith(".mdc"):
            return "path:rules-file"
        for part in parts[:-1]:
            if part.lower() in AGENT_DIRECTORIES:
                return f"path:{part.lower()}"
    return ""


@dataclass(frozen=True)
class RecordInfo:
    """What the rules need to know about the record a message belongs to."""

    provider: str = ""
    kind: str = ""
    wing: str = ""
    session_kind: str = ""
    # The record belongs to a family of the person's own writing (notes, Drive, authored files).
    authored_family: bool = False
    # Why the record as a whole is machine-made ('' when nothing says so).
    agent_origin: str = ""
    # An AI-authorship marker found anywhere in a note or document of the person's own library:
    # section boundaries cannot be proven, so none of the record can be attributed to the owner.
    ai_marker: str = ""


def record_info(payload: Mapping[str, Any]) -> RecordInfo:
    """Derive the rule inputs from a library record payload (a `Conversation.to_dict()`)."""
    extra = payload.get("extra") if isinstance(payload.get("extra"), Mapping) else {}
    provenance = payload.get("provenance") if isinstance(payload.get("provenance"), Mapping) else {}
    provider = str(payload.get("provider") or "")
    kind = str(payload.get("kind") or "")
    wing = str(extra.get("wing", "ai"))
    row = {"provider": provider, "wing": wing, "kind": kind}
    authored = any(family.authored and family.selects(row) for family in FAMILIES)
    origin = marks_agent_origin(provenance) or marks_agent_origin(extra)
    if not origin:
        origin = next(
            (
                f"family:{family.name}"
                for family in FAMILIES
                if family.library_provider == "documents"
                and not family.authored
                and family.selects(row)
            ),
            "",
        )
    if not origin and kind in DOCUMENT_KINDS:
        origin = _path_reason(provenance)
    ai_marker = ""
    if kind in DOCUMENT_KINDS:
        for body in _record_texts(payload, extra):
            if not origin:
                marker = _text_marker(body)
                origin = f"text:{marker}" if marker else ""
            if authored and kind in NOTE_KINDS and not ai_marker:
                ai_marker = _ai_marker(body)
    return RecordInfo(
        provider=provider,
        kind=kind,
        wing=wing,
        session_kind=str(extra.get("session_kind") or ""),
        authored_family=authored,
        agent_origin=origin,
        ai_marker=ai_marker,
    )


def _record_texts(payload: Mapping[str, Any], extra: Mapping[str, Any]) -> Iterator[str]:
    """The text of every message of a record, in both sequences."""
    for messages in (payload.get("messages"), extra.get("alternate_messages")):
        if isinstance(messages, list):
            for item in messages:
                body = item.get("text") if isinstance(item, Mapping) else None
                if isinstance(body, str):
                    yield body


@dataclass(frozen=True)
class MessageContext:
    """The exact message the rules decide on: its text plus the record's stated evidence."""

    text: str
    role: str = ""
    channel: str = ""
    typed_chars: Any = None
    meta: Mapping[str, Any] = field(default_factory=dict)
    record: RecordInfo = field(default_factory=RecordInfo)
    # Character ranges the repeated-span census flagged as later occurrences (rule f).
    boilerplate: tuple[Range, ...] = ()
    # Paragraphs that repeat text of an earlier assistant turn of the same conversation.
    pasted_back: tuple[Range, ...] = ()


@dataclass(frozen=True)
class AttributedSpan:
    """One contiguous span of one message, decided by exactly one rule."""

    start: int
    end: int
    author: str
    rule: str
    detail: str
    text_digest: str
    # Always False from this engine; only the precision gate may raise it (see `to_annotation`).
    profile_eligible: bool = False

    @property
    def span_id(self) -> str:
        return span_id(self.rule, self.text_digest, self.start, self.end)

    @property
    def stratum(self) -> str:
        return stratum_of(self.end - self.start)


# Length strata. The precision gate judges each (rule, stratum) on its own, so reviews of short
# spans never vouch for long ones.
STRATA = ("S", "M", "L")
STRATUM_S_MAX = 240
STRATUM_M_MAX = 2000


def stratum_of(length: int) -> str:
    """S for up to 240 characters, M for 241 to 2000, L for more."""
    if length <= STRATUM_S_MAX:
        return "S"
    return "M" if length <= STRATUM_M_MAX else "L"


def span_id(rule: str, digest: str, start: int, end: int) -> str:
    """Stable identity of a span: bound to the rules version, the rule, the text and the range."""
    key = f"{RULES_VERSION}\n{rule}\n{digest}\n{start}\n{end}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


# --- i: agent families -----------------------------------------------------------------------


def rule_agent_family(context: MessageContext) -> list[Claim]:
    """Assistant, model and tool turns of every provider are machine, and so is text a harness
    put in a user turn: a machine channel, provider metadata naming an agent, a coding CLI's
    subagent prompt, or a CLI turn `archive_provenance` classifies as not typed by the person."""
    length = len(context.text)
    role, channel = _norm(context.role), _norm(context.channel)
    if role in AGENT_ROLES:
        return [(0, length, f"role={role}")]
    if role != "user":
        return []
    if channel in MACHINE_CHANNELS:
        return [(0, length, f"channel={channel}")]
    marker = marks_agent_origin(context.meta)
    if marker:
        return [(0, length, f"meta={marker}")]
    record = context.record
    if record.provider in CLI_PROVIDERS:
        if record.session_kind == "subagent":
            return [(0, length, "basis=subagent-prompt")]
        if channel == "typed":
            human, basis = classify_user_turn(context.text, record.session_kind)
            if not human:
                return [(0, length, f"basis={basis}")]
    return []


# --- h: document origin ----------------------------------------------------------------------

_TEXT_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "co-authored-by",
        re.compile(
            r"co-authored-by:\s*(?:claude|codex|chatgpt|gpt|gemini|copilot|openai|anthropic|cursor)",
            re.IGNORECASE,
        ),
    ),
    (
        "generated-with",
        re.compile(
            r"generated\s+(?:with|by)\s+\[?(?:claude|codex|chatgpt|gpt|gemini|copilot|cursor"
            r"|openai|anthropic|an?\s+(?:ai|llm)\b)",
            re.IGNORECASE,
        ),
    ),
    ("robot-generated", re.compile(r"\N{ROBOT FACE}\s*generated", re.IGNORECASE)),
    (
        "auto-generated",
        re.compile(
            r"(?:auto-?generated|generated\s+automatically)\s+by\s+(?:an?\s+)?"
            r"(?:ai|llm|agent|assistant)\b",
            re.IGNORECASE,
        ),
    ),
)
_FRONTMATTER_LINE = re.compile(r"^([A-Za-z0-9_-]+):\s*(.*)$")


def _frontmatter_marker(text: str) -> str:
    lines = text[:4000].split("\n")
    if not lines or lines[0].strip() != "---":
        return ""
    keys: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        found = _FRONTMATTER_LINE.match(line)
        if found:
            keys[found.group(1).lower()] = found.group(2)
    else:
        return ""
    if SKILL_FRONTMATTER_KEYS & keys.keys():
        return "skill-frontmatter"
    marker = marks_agent_origin(keys)
    return f"frontmatter-{marker}" if marker else ""


def _text_marker(text: str) -> str:
    """A generated-by footer, co-author line or skill frontmatter anywhere in the text."""
    for name, pattern in _TEXT_MARKERS:
        if pattern.search(text):
            return name
    return _frontmatter_marker(text)


_AGENT_NOUN = (
    r"(?:an?\s+)?(?:ai|a\.i\.|llm|large\s+language\s+model|language\s+model|chat\s?bot"
    r"|chatgpt|gpt[-\w.]*|claude|gemini|copilot|codex|openai|anthropic|assistant|bard|grok"
    r"|perplexity|mistral|deepseek)"
)
_AI_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ai-authorship",
        re.compile(
            r"\b(?:generated|written|drafted|composed|authored|created|produced|summari[sz]ed"
            r"|translated|rewritten|edited|suggested|answered)\s+(?:by|using|with|via)\s+"
            rf"(?:the\s+help\s+of\s+)?{_AGENT_NOUN}\b",
            re.IGNORECASE,
        ),
    ),
    (
        "with-help-of-ai",
        re.compile(
            rf"\bwith\s+(?:the\s+)?(?:help|assistance|aid)\s+(?:of|from)\s+{_AGENT_NOUN}\b",
            re.IGNORECASE,
        ),
    ),
    ("as-an-ai", re.compile(r"\bas\s+an\s+ai\b", re.IGNORECASE)),
    (
        "ai-generated",
        re.compile(
            r"\b(?:ai|a\.i\.|llm|machine)[-\s]+(?:generated|written|authored|produced|assisted)\b",
            re.IGNORECASE,
        ),
    ),
)


def _ai_marker(text: str) -> str:
    """The name of an AI-authorship statement anywhere in the text, else ''."""
    for name, pattern in _AI_MARKERS:
        if pattern.search(text):
            return name
    return _text_marker(text)


def rule_document_origin(context: MessageContext) -> list[Claim]:
    """A record whose provenance says an agent produced it (an agent-authorship field, an agent
    memory or plan family, an agent instruction or skill file) is machine whatever the role. For
    documents, notes and plans a generated-by footer or skill frontmatter says the same."""
    record = context.record
    length = len(context.text)
    if record.agent_origin:
        return [(0, length, f"origin={record.agent_origin}")]
    if record.kind in DOCUMENT_KINDS:
        marker = _text_marker(context.text)
        if marker:
            return [(0, length, f"origin=text:{marker}")]
    return []


# --- b: inline injection ---------------------------------------------------------------------

# Wrapper blocks a harness or tool injects: the archive's injected tags, the slash-command and
# shell-command wrappers (rendered by the harness even though the person typed the command), and
# tool-result and attachment blocks.
WRAPPER_TAGS = frozenset(tag.lower() for tag in INJECTED_TAGS) | frozenset(
    {
        "command-name",
        "command-message",
        "command-args",
        "bash-input",
        "user_shell_command",
        "tool_result",
        "tool-result",
        "tool_use",
        "tool_response",
        "function_results",
        "function_calls",
        "attachment",
        "attachments",
    }
)
# The name of an opening tag, and the rest of it up to the closing angle bracket. Quoted attribute
# values may hold angle brackets; the alternatives start with different characters, so matching
# never backtracks.
_TAG_NAME = re.compile(r"<([A-Za-z_][\w:.-]{0,40})(?=[\s/>])")
_TAG_REST = re.compile(r"""(?:\s(?:[^<>"'/]|"[^"]*"|'[^']*'|/(?!>))*)?(/?)>""")


@functools.lru_cache(maxsize=256)
def _tag_pair(name: str) -> re.Pattern[str]:
    return re.compile(rf"<(/?){re.escape(name)}(?=[\s/>])", re.IGNORECASE)


def _is_wrapper(name: str) -> bool:
    lowered = name.lower()
    return lowered in WRAPPER_TAGS or lowered.startswith("ide_")


def _opening_end(text: str, name_end: int) -> tuple[int, bool]:
    """Where an opening tag ends and whether it is self-closing. A malformed one (an attribute
    quote never closed) is still an opening tag: it ends at the next angle bracket, or at the end
    of the text."""
    found = _TAG_REST.match(text, name_end)
    if found:
        return found.end(), bool(found.group(1))
    bracket = text.find(">", name_end)
    return (bracket + 1 if bracket != -1 else len(text)), False


def _matching_close(text: str, name: str, start: int) -> int:
    """The end of the block opened just before `start`: its matching close tag, counting nested
    tags of the same name. A block that is never closed runs to the end of the text."""
    pattern = _tag_pair(name)
    depth, position = 1, start
    while (found := pattern.search(text, position)) is not None:
        if found.group(1):
            bracket = text.find(">", found.end())
            position = bracket + 1 if bracket != -1 else len(text)
            depth -= 1
            if depth == 0:
                return position
        else:
            position, self_closing = _opening_end(text, found.end())
            depth += not self_closing
    return len(text)


def _tag_blocks(text: str, wanted: Callable[[str], bool]) -> list[Claim]:
    """Blocks `<name ...>...</name>` for wanted names. Same-name tags nest; an unclosed block runs
    to the end of the text."""
    claims: list[Claim] = []
    position = 0
    while (match := _TAG_NAME.search(text, position)) is not None:
        name = match.group(1)
        if not wanted(name):
            position = match.end()
            continue
        opening_end, self_closing = _opening_end(text, match.end())
        end = opening_end if self_closing else _matching_close(text, name, opening_end)
        claims.append((match.start(), end, f"tag={name.lower()}"))
        position = max(end, match.end())
    return claims


def rule_inline_injection(context: MessageContext) -> list[Claim]:
    """Wrapper blocks are machine even in the middle of owner text; the text around them stays
    available to later rules."""
    return _tag_blocks(context.text, _is_wrapper)


# --- a: the typed prefix of a user turn ------------------------------------------------------


def _typed_limit(context: MessageContext) -> int | None:
    """How many leading characters the record says the person typed (None: no such evidence).

    Only a user turn on the `typed` channel of a record that is not an authored family qualifies:
    an authored family's reader sets `typed_chars` to the whole body, which is not evidence of
    typing.
    """
    value = context.typed_chars
    if (
        _norm(context.role) != "user"
        or _norm(context.channel) != "typed"
        or context.record.authored_family
        or isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
    ):
        return None
    return min(value, len(context.text))


def rule_typed_remainder(context: MessageContext) -> list[Claim]:
    """What follows the typed prefix of a user turn was not typed: machine or injected."""
    limit = _typed_limit(context)
    length = len(context.text)
    if limit is None or limit >= length:
        return []
    return [(limit, length, f"typed_chars={limit}/{length}")]


def rule_typed_prefix(context: MessageContext) -> list[Claim]:
    """Only the first `typed_chars` characters of a user turn can be the owner's own typing."""
    limit = _typed_limit(context)
    if not limit:
        return []
    return [(0, limit, f"typed_chars={limit}/{len(context.text)}")]


def _lines(text: str) -> list[tuple[int, int, str]]:
    """(start, end, content) of every line; `end` excludes the line break."""
    lines: list[tuple[int, int, str]] = []
    position = 0
    for content in text.split("\n"):
        end = position + len(content)
        lines.append((position, end, content))
        position = end + 1
    return lines


def _paragraphs(lines: list[tuple[int, int, str]]) -> list[Range]:
    """Maximal runs of lines that hold a non-whitespace character."""
    result: list[Range] = []
    first = None
    last = 0
    for start, end, content in lines:
        if content.strip():
            if first is None:
                first = start
            last = end
        elif first is not None:
            result.append((first, last))
            first = None
    if first is not None:
        result.append((first, last))
    return result


# --- c: quotes -------------------------------------------------------------------------------

# A passage inside quotation marks is only treated as pasted when it is long enough to be one.
MIN_QUOTED_CHARS = 40
PASTE_TAGS = frozenset({"pasted_content", "pasted-content", "paste"})
_QUOTE_LINE = re.compile(r"^ {0,3}>")
_MARKED_QUOTES = tuple(
    re.compile(f"{opening}([^{opening}{closing}]{{{MIN_QUOTED_CHARS},}}){closing}")
    for opening, closing in (("“", "”"), ("«", "»"), ("„", "“"))
)
_BLANK_LINE = re.compile(r"\n[ \t]*\n")
# A line that starts a new block and so cannot continue a quoted paragraph lazily.
_BLOCK_START = re.compile(
    r"^ {0,3}(?:`{3,}|~{3,}|#{1,6}(?:\s|$)|(?:[-*+]|\d{1,9}[.)])\s|([-*_])[ \t]*(?:\1[ \t]*){2,}$)"
)


def rule_quote(context: MessageContext) -> list[Claim]:
    """Markdown blockquote lines, long passages inside matching quotation marks and pasted-content
    wrappers are text a turn quotes from elsewhere: third party, not the owner's own words."""
    text = context.text
    lines = _lines(text)
    claims: list[Claim] = []
    first: int | None = None
    for index, (_, _, content) in enumerate([*lines, (0, 0, "")]):
        inside = index < len(lines)
        if inside and _QUOTE_LINE.match(content):
            first = index if first is None else first
        elif first is not None and inside and content.strip() and not _BLOCK_START.match(content):
            continue  # a lazy continuation line is still part of the quote
        elif first is not None:
            claims.append((lines[first][0], lines[index - 1][1], "blockquote"))
            first = None
    for pattern in _MARKED_QUOTES:
        claims.extend(
            (match.start(), match.end(), "quotation-marks")
            for match in pattern.finditer(text)
            if not _BLANK_LINE.search(match.group(1))
        )
    for start, end in _paragraphs(lines):
        marks = [start + offset for offset, char in enumerate(text[start:end]) if char == '"']
        if len(marks) % 2:
            continue  # unpaired quotes: the paragraph is ambiguous (see `rule_ambiguous`)
        claims.extend(
            (opening, closing + 1, "quotation-marks")
            for opening, closing in zip(marks[0::2], marks[1::2], strict=True)
            if closing - opening - 1 >= MIN_QUOTED_CHARS
        )
    claims.extend(_tag_blocks(text, lambda name: name.lower() in PASTE_TAGS))
    return claims


# --- d: code ---------------------------------------------------------------------------------

_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def _fenced_blocks(lines: list[tuple[int, int, str]]) -> list[Claim]:
    claims: list[Claim] = []
    index = 0
    while index < len(lines):
        start, _, content = lines[index]
        opened = _FENCE_OPEN.match(content)
        if not opened or (opened.group(1)[0] == "`" and "`" in opened.group(2)):
            index += 1
            continue
        marker = opened.group(1)
        closer = re.compile(rf"^ {{0,3}}{re.escape(marker[0])}{{{len(marker)},}}[ \t]*$")
        close = index + 1
        while close < len(lines) and not closer.match(lines[close][2]):
            close += 1
        claims.append((start, lines[min(close, len(lines) - 1)][1], "fenced"))
        index = close + 1
    return claims


def _is_indented(content: str) -> bool:
    return bool(content.strip()) and content.startswith(("\t", "    "))


def _indented_blocks(lines: list[tuple[int, int, str]]) -> list[Claim]:
    """Runs of at least two indented lines that start after a blank line (or the text start)."""
    claims: list[Claim] = []
    index = 0
    while index < len(lines):
        if not _is_indented(lines[index][2]) or (index and lines[index - 1][2].strip()):
            index += 1
            continue
        probe, last, count = index, index, 0
        while probe < len(lines):
            content = lines[probe][2]
            if _is_indented(content):
                last, count = probe, count + 1
            elif content.strip():
                break
            probe += 1
        if count >= 2:
            claims.append((lines[index][0], lines[last][1], "indented"))
        index = max(probe, index + 1)
    return claims


def rule_code(context: MessageContext) -> list[Claim]:
    """Fenced code blocks and indented code are pasted content, not owner prose. The class is
    third party because who wrote the code cannot be told from the text."""
    lines = _lines(context.text)
    return [*_fenced_blocks(lines), *_indented_blocks(lines)]


# --- e: logs and traces ----------------------------------------------------------------------

# A line that is a log line on its own (a timestamp with seconds).
_STRONG_LOG = (
    re.compile(r"^\s*[\[(]?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"),
    re.compile(r"^\s*\[\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\]"),
    re.compile(r"^[A-Z][a-z]{2}\s+\d{1,2}\s\d{2}:\d{2}:\d{2}\s"),
)
# A line shaped like tool output, which counts only inside a run of three or more such lines.
_WEAK_LOG = (
    re.compile(r"^\s*\[?(?:TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL|CRITICAL)\]?(?:[:\s|-]|$)"),
    re.compile(r"^[\w./\\-]*\w\.\w{1,6}:\d+(?::\d+)?:\s?"),
    re.compile(r"^\$ \S"),
    re.compile(r"^(?:@@ -\d|diff --git |index [0-9a-f]{7,}\.\.[0-9a-f]{7,}|\+\+\+ \S|--- \S)"),
    re.compile(r"^[d\-l][rwx\-]{9}[@+.]?\s+\d+\s+\S"),
    re.compile(r"^(?:npm|yarn) (?:ERR!|WARN|error|warn)"),
)
# A stack frame and an exception header: weak on their own, but a header with its frame, or two
# frames, is a stack trace even when it is only two lines long.
_STACK_FRAME = re.compile(r"^\s+at\s+\S.*(?:\([^()]*\)|:\d+(?::\d+)?)\s*$")
_STACK_HEADER = re.compile(
    r"^(?:Exception in thread\b|Caused by:|(?:[\w$]+\.)*[\w$]*(?:Exception|Error)\b[:\s])"
)
_TRACE_HEADER = re.compile(r"^\s*Traceback \(most recent call last\):\s*$")
_TRACE_LAST = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception|Interrupt|Exit|Warning)\b(?::.*)?$")
MIN_WEAK_RUN = 3


def _traceback_blocks(lines: list[tuple[int, int, str]]) -> list[Claim]:
    claims: list[Claim] = []
    for index, (start, _, content) in enumerate(lines):
        if not _TRACE_HEADER.match(content):
            continue
        stop = index + 1
        while stop < len(lines) and lines[stop][2].startswith((" ", "\t")):
            stop += 1
        if stop < len(lines) and _TRACE_LAST.match(lines[stop][2]):
            stop += 1
        claims.append((start, lines[stop - 1][1], "traceback"))
    return claims


def rule_log_trace(context: MessageContext) -> list[Claim]:
    """Timestamped log lines, Python tracebacks and runs of three or more tool-output shaped
    lines (log levels, grep hits, diffs, shell prompts, stack frames) are machine output."""
    lines = _lines(context.text)
    claims = _traceback_blocks(lines)
    kinds: list[str] = []
    for _, _, content in lines:
        if any(pattern.match(content) for pattern in _STRONG_LOG):
            kinds.append("strong")
        elif _STACK_FRAME.match(content):
            kinds.append("frame")
        elif _STACK_HEADER.match(content):
            kinds.append("header")
        elif any(pattern.match(content) for pattern in _WEAK_LOG):
            kinds.append("weak")
        else:
            kinds.append("")
    index = 0
    while index < len(lines):
        if not kinds[index]:
            index += 1
            continue
        stop = index
        while stop < len(lines) and kinds[stop]:
            stop += 1
        run_kinds = set(kinds[index:stop])
        if stop - index >= MIN_WEAK_RUN:
            claims.append((lines[index][0], lines[stop - 1][1], "tool-output-run"))
        elif stop - index == 2 and "frame" in run_kinds and run_kinds <= {"frame", "header"}:
            claims.append((lines[index][0], lines[stop - 1][1], "stack-trace"))
        else:
            strong = [i for i in range(index, stop) if kinds[i] == "strong"]
            for _, group in itertools.groupby(
                enumerate(strong), key=lambda pair: pair[1] - pair[0]
            ):
                members = [item[1] for item in group]
                claims.append((lines[members[0]][0], lines[members[-1]][1], "timestamped-log"))
        index = stop
    return claims


# --- f: boilerplate (a repeated-span census over the slice being scanned) -----------------------

DEFAULT_REPEAT_MIN_CHARS = 200
DEFAULT_REPEAT_MIN_OCCURRENCES = 3


def _repeat_key(text: str) -> int:
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")


def _repeat_units(text: str, min_chars: int) -> list[tuple[int, int, int]]:
    """(start, end, key) of each paragraph, and of the whole message when it has several, that is
    at least `min_chars` long after whitespace normalization. Only digests are ever kept."""
    paragraphs = _paragraphs(_lines(text))
    spans = list(paragraphs)
    if len(paragraphs) >= 2:
        spans.append((paragraphs[0][0], paragraphs[-1][1]))
    units: list[tuple[int, int, int]] = []
    for start, end in spans:
        piece = text[start:end]
        stripped = piece.strip()
        if len(stripped) < min_chars:
            continue
        normalized = " ".join(stripped.split())
        if len(normalized) >= min_chars:
            lead = len(piece) - len(piece.lstrip())
            units.append((start + lead, start + lead + len(stripped), _repeat_key(normalized)))
    return units


class RepeatCensus:
    """Finds long text that repeats verbatim (after whitespace normalization) across a slice.

    Feed every message of the slice to `observe`, then call `later_occurrences` for every message
    in the order the slice is scanned. A unit that occurs `min_occurrences` or more times in the
    slice is boilerplate; its first occurrence in scan order is left to the other rules and every
    later occurrence, in the same message, the same conversation or another one, is returned as a
    range. The answer depends on the slice and its scan order, so a different slice can decide a
    span differently. Memory holds one 64-bit digest per distinct long unit, never the text.
    """

    def __init__(
        self,
        *,
        min_chars: int = DEFAULT_REPEAT_MIN_CHARS,
        min_occurrences: int = DEFAULT_REPEAT_MIN_OCCURRENCES,
    ) -> None:
        if isinstance(min_chars, bool) or not isinstance(min_chars, int) or min_chars < 1:
            raise ValueError("min_chars must be a positive integer")
        if (
            isinstance(min_occurrences, bool)
            or not isinstance(min_occurrences, int)
            or min_occurrences < 2
        ):
            raise ValueError("min_occurrences must be at least 2")
        self.min_chars = min_chars
        self.min_occurrences = min_occurrences
        self._counts: dict[int, int] = {}
        self._seen: set[int] = set()
        self._claiming = False

    def rewind(self) -> None:
        """Start the claim pass over: the next scan sees every first occurrence as a first."""
        self._seen.clear()

    def observe(self, text: str) -> None:
        if self._claiming:
            raise RuntimeError("observe every message before asking for later occurrences")
        for _, _, key in _repeat_units(text, self.min_chars):
            self._counts[key] = self._counts.get(key, 0) + 1

    def later_occurrences(self, text: str) -> tuple[Range, ...]:
        self._claiming = True
        claimed: list[Range] = []
        for start, end, key in _repeat_units(text, self.min_chars):
            if self._counts.get(key, 0) < self.min_occurrences:
                continue
            if key in self._seen:
                claimed.append((start, end))
            else:
                self._seen.add(key)
        return tuple(sorted(claimed))


def boilerplate_ranges(
    texts: Sequence[str],
    *,
    min_chars: int = DEFAULT_REPEAT_MIN_CHARS,
    min_occurrences: int = DEFAULT_REPEAT_MIN_OCCURRENCES,
) -> list[tuple[Range, ...]]:
    """The later-occurrence ranges of every text of a slice, aligned with the input."""
    census = RepeatCensus(min_chars=min_chars, min_occurrences=min_occurrences)
    for text in texts:
        census.observe(text)
    return [census.later_occurrences(text) for text in texts]


def rule_boilerplate(context: MessageContext) -> list[Claim]:
    """Later occurrences of long verbatim-repeated text were copied, not freshly written."""
    return [(start, end, "verbatim-repeat") for start, end in context.boilerplate]


# --- pasted-back assistant text ---------------------------------------------------------------

DEFAULT_ECHO_MIN_PARAGRAPH = 600
DEFAULT_ECHO_MIN_SHARED = 100
DEFAULT_ECHO_HISTORY = 400_000


class AssistantEcho:
    """Finds text a user turn pastes back from an earlier assistant turn of its conversation.

    Feed the conversation's turns in order: `add_assistant` for each assistant reply and
    `pasted_back` for each user turn. A paragraph of the user turn that is at least
    `min_paragraph` characters long and shares at least `min_shared` characters (after
    whitespace normalization) with an earlier assistant turn is returned as a range. Only the
    most recent `history_limit` normalized characters of assistant text are remembered, and the
    index is built only when a user turn has a paragraph long enough to need it.
    """

    def __init__(
        self,
        *,
        min_paragraph: int = DEFAULT_ECHO_MIN_PARAGRAPH,
        min_shared: int = DEFAULT_ECHO_MIN_SHARED,
        history_limit: int = DEFAULT_ECHO_HISTORY,
    ) -> None:
        for name, value in (
            ("min_paragraph", min_paragraph),
            ("min_shared", min_shared),
            ("history_limit", history_limit),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self.min_paragraph = min_paragraph
        self.min_shared = min_shared
        self.history_limit = history_limit
        self._turns: list[str] = []
        self._size = 0
        self._index: set[int] | None = None

    def add_assistant(self, text: str) -> None:
        normalized = " ".join(text.split())
        if len(normalized) < self.min_shared:
            return
        self._turns.append(normalized)
        self._size += len(normalized)
        while self._size > self.history_limit and len(self._turns) > 1:
            self._size -= len(self._turns.pop(0))
        self._index = None

    def _windows(self) -> set[int]:
        if self._index is None:
            width = self.min_shared
            self._index = {
                hash(turn[i : i + width])
                for turn in self._turns
                for i in range(len(turn) - width + 1)
            }
        return self._index

    def _shares(self, normalized: str) -> bool:
        width = self.min_shared
        if not self._turns or len(normalized) < width:
            return False
        index = self._windows()
        for i in range(len(normalized) - width + 1):
            window = normalized[i : i + width]
            if hash(window) in index and any(window in turn for turn in self._turns):
                return True
        return False

    def pasted_back(self, text: str) -> tuple[Range, ...]:
        claimed: list[Range] = []
        if not self._turns:
            return ()
        for start, end in _paragraphs(_lines(text)):
            piece = text[start:end]
            stripped = piece.strip()
            if len(stripped) < self.min_paragraph:
                continue
            if self._shares(" ".join(stripped.split())):
                lead = len(piece) - len(piece.lstrip())
                claimed.append((start + lead, start + lead + len(stripped)))
        return tuple(claimed)


def rule_pasted_back(context: MessageContext) -> list[Claim]:
    """A long paragraph a user turn pastes back from an earlier assistant turn was written by the
    assistant, not by the person."""
    return [(start, end, "pasted-back") for start, end in context.pasted_back]


# --- ambiguous text: unknown, never owner -----------------------------------------------------


def _owns_notes(record: RecordInfo) -> bool:
    return record.authored_family and record.kind in NOTE_KINDS


def rule_ambiguous(context: MessageContext) -> list[Claim]:
    """Text whose authorship cannot be told from its shape or its record. A paragraph with an odd
    number of ASCII double quotes cannot be paired into quotations, and a note or document of the
    person's own library that states an AI had a hand in it anywhere cannot be split into the
    person's sections and the machine's: none of it is attributed. Every stronger rule has
    already taken its own characters by the time this one runs."""
    text = context.text
    marker = ""
    if _owns_notes(context.record):
        marker = context.record.ai_marker or _ai_marker(text)
    if marker:
        return [(0, len(text), f"ai-marker:{marker}")]
    claims: list[Claim] = []
    for start, end in _paragraphs(_lines(text)):
        if text.count('"', start, end) % 2:
            claims.append((start, end, "odd-ascii-quotes"))
    return claims


# --- pasted blocks: emails, forwards and tables ------------------------------------------------

_MAIL_HEADER = re.compile(r"^(?:From|To|Cc|Bcc|Subject|Date|Sent|Reply-To):[ \t]*\S", re.IGNORECASE)
_MAIL_MARKERS = (
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^-{2,}\s*Forwarded message\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^Begin forwarded message:?\s*$", re.IGNORECASE),
    re.compile(r"^On .{5,200} wrote:\s*$", re.IGNORECASE),
)
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)+\|?\s*$")


def rule_pasted_block(context: MessageContext) -> list[Claim]:
    """A pasted email or forward and a markdown table are third-party text. Where an email starts
    its extent cannot be proven, so it runs to the end of the message."""
    lines = _lines(context.text)
    claims: list[Claim] = []
    for index, (start, _, content) in enumerate(lines):
        marker = any(pattern.match(content) for pattern in _MAIL_MARKERS)
        header_run = (
            _MAIL_HEADER.match(content)
            and index + 1 < len(lines)
            and _MAIL_HEADER.match(lines[index + 1][2])
            and (index == 0 or not _MAIL_HEADER.match(lines[index - 1][2]))
        )
        if marker or header_run:
            claims.append((start, len(context.text), "email" if header_run else "forward"))
            break
    index = 0
    while index < len(lines):
        if "|" not in lines[index][2]:
            index += 1
            continue
        stop = index
        while stop < len(lines) and "|" in lines[stop][2]:
            stop += 1
        if stop - index >= 2 and any(
            _TABLE_SEPARATOR.match(lines[i][2]) for i in range(index, stop)
        ):
            claims.append((lines[index][0], lines[stop - 1][1], "markdown-table"))
        index = stop
    return claims


# --- g: notes default ------------------------------------------------------------------------

NOTE_KINDS = frozenset({"note", "document"})


def rule_note_owner(context: MessageContext) -> list[Claim]:
    """The body of a note or document in a family of the person's own writing is owner by
    default. Every higher rule still takes its spans first (quotes, code, logs, injected blocks,
    boilerplate, an agent-produced document), and a message whose provider metadata marks an
    agent origin is not claimed."""
    record = context.record
    if (
        _norm(context.role) != "user"
        or not record.authored_family
        or record.kind not in NOTE_KINDS
        or marks_agent_origin(context.meta)
        or record.ai_marker
        or _ai_marker(context.text)
    ):
        return []
    return [(0, len(context.text), f"family={record.provider}")]


# --- the ordered rule table ------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    id: str
    author: str
    description: str
    claims: Callable[[MessageContext], list[Claim]]


RULES: tuple[Rule, ...] = (
    Rule("agent_family", AGENT, "assistant, model, tool and harness turns", rule_agent_family),
    Rule(
        "document_origin",
        AGENT,
        "a document an agent produced (skill, instruction, agent memory or plan)",
        rule_document_origin,
    ),
    Rule(
        "inline_injection",
        AGENT,
        "a wrapper block injected into the turn (system reminder, command, tool result)",
        rule_inline_injection,
    ),
    Rule(
        "typed_remainder",
        AGENT,
        "text after the characters the person typed",
        rule_typed_remainder,
    ),
    Rule("code", THIRD_PARTY, "fenced or indented code pasted into the turn", rule_code),
    Rule(
        "quote",
        THIRD_PARTY,
        "blockquotes, long quoted passages and pasted-content wrappers",
        rule_quote,
    ),
    Rule(
        "log_trace",
        AGENT,
        "timestamped log lines, stack traces and tool-output shaped runs",
        rule_log_trace,
    ),
    Rule(
        "pasted_block",
        THIRD_PARTY,
        "a pasted email or forward, or a markdown table",
        rule_pasted_block,
    ),
    Rule(
        "boilerplate",
        THIRD_PARTY,
        "later occurrences of long text that repeats verbatim across the slice",
        rule_boilerplate,
    ),
    Rule(
        "pasted_back",
        THIRD_PARTY,
        "a long paragraph repeated from an earlier assistant turn of the same conversation",
        rule_pasted_back,
    ),
    Rule(
        "ambiguous",
        UNKNOWN,
        "text whose authorship cannot be told (unpaired quotes, an AI-marked note); never owner",
        rule_ambiguous,
    ),
    Rule("typed_prefix", OWNER, "the characters the person typed", rule_typed_prefix),
    Rule(
        "note_owner",
        OWNER,
        "the body of a note or document in the person's own authored library",
        rule_note_owner,
    ),
)
RULE_IDS: tuple[str, ...] = tuple(rule.id for rule in RULES)
RULE_BY_ID: dict[str, Rule] = {rule.id: rule for rule in RULES}
AUTHOR_BY_RULE: dict[str, str] = {**{rule.id: rule.author for rule in RULES}, UNATTRIBUTED: UNKNOWN}


# --- the engine ------------------------------------------------------------------------------


def _normalize(claims: Sequence[Claim], length: int) -> list[Claim]:
    """Clamp, sort and make a rule's claims disjoint (an earlier claim keeps an overlap)."""
    ordered = sorted(
        ((max(0, start), min(length, end), detail) for start, end, detail in claims),
        key=lambda claim: (claim[0], claim[1]),
    )
    result: list[Claim] = []
    cursor = 0
    for start, end, detail in ordered:
        start = max(start, cursor)  # noqa: PLW2901 - trimming an overlap is the intent
        if start < end:
            result.append((start, end, detail))
            cursor = end
    return result


def _intersect(free: list[Range], claims: list[Claim]) -> list[Claim]:
    pieces: list[Claim] = []
    first = 0
    for free_start, free_end in free:
        while first < len(claims) and claims[first][1] <= free_start:
            first += 1
        index = first
        while index < len(claims) and claims[index][0] < free_end:
            start = max(claims[index][0], free_start)
            end = min(claims[index][1], free_end)
            if start < end:
                pieces.append((start, end, claims[index][2]))
            index += 1
    return pieces


def _subtract(free: list[Range], pieces: list[Claim]) -> list[Range]:
    result: list[Range] = []
    index = 0
    for free_start, free_end in free:
        cursor = free_start
        while index < len(pieces) and pieces[index][0] < free_end:
            start, end, _ = pieces[index]
            if start > cursor:
                result.append((cursor, start))
            cursor = end
            index += 1
        if cursor < free_end:
            result.append((cursor, free_end))
    return result


def _trimmed(text: str, pieces: list[Claim]) -> list[Claim]:
    """An owner span never starts or ends with whitespace, and is never only whitespace."""
    result: list[Claim] = []
    for start, end, detail in pieces:
        segment = text[start:end]
        lead = len(segment) - len(segment.lstrip())
        trail = len(segment) - len(segment.rstrip())
        if lead + trail < len(segment):
            result.append((start + lead, end - trail, detail))
    return result


def partition_problems(
    text: str, spans: Sequence[AttributedSpan], digest: str | None = None
) -> dict[str, int]:
    """Independent check of a partition: every character covered exactly once, bound to the text."""
    expected = digest or text_digest(text)
    problems = {"gaps": 0, "overlaps": 0, "uncovered": 0, "out_of_range": 0, "digest_mismatch": 0}
    cursor = 0
    for span in sorted(spans, key=lambda item: (item.start, item.end)):
        if span.text_digest != expected:
            problems["digest_mismatch"] += 1
        if span.start < 0 or span.end > len(text) or span.start >= span.end:
            problems["out_of_range"] += 1
        if span.start > cursor:
            problems["gaps"] += 1
            problems["uncovered"] += span.start - cursor
        elif span.start < cursor:
            problems["overlaps"] += 1
        cursor = max(cursor, span.end)
    if cursor < len(text):
        problems["gaps"] += 1
        problems["uncovered"] += len(text) - cursor
    return problems


def attribute_message(
    text: str,
    *,
    role: Any = "",
    channel: Any = "",
    typed_chars: Any = None,
    meta: Mapping[str, Any] | None = None,
    record: RecordInfo | None = None,
    boilerplate: Sequence[Range] = (),
    pasted_back: Sequence[Range] = (),
) -> list[AttributedSpan]:
    """Partition one message into spans, first rule wins; leftovers are `unknown`."""
    if not isinstance(text, str):
        raise ValueError("attribution text must be a string")
    length = len(text)
    if length == 0:
        return []
    context = MessageContext(
        text=text,
        role=role if isinstance(role, str) else "",
        channel=channel if isinstance(channel, str) else "",
        typed_chars=typed_chars,
        meta=meta if isinstance(meta, Mapping) else {},
        record=record or RecordInfo(),
        boilerplate=tuple(boilerplate),
        pasted_back=tuple(pasted_back),
    )
    digest = text_digest(text)
    free: list[Range] = [(0, length)]
    taken: list[tuple[int, int, str, str]] = []
    for rule in RULES:
        pieces = _intersect(free, _normalize(rule.claims(context), length))
        if rule.author == OWNER:
            pieces = _trimmed(text, pieces)
        if not pieces:
            continue
        free = _subtract(free, pieces)
        taken.extend((start, end, rule.id, detail) for start, end, detail in pieces)
    taken.extend((start, end, UNATTRIBUTED, "") for start, end in free)
    taken.sort()
    merged: list[list[Any]] = []
    for start, end, rule_id, detail in taken:
        if merged and merged[-1][1] == start and merged[-1][2:] == [rule_id, detail]:
            merged[-1][1] = end
        else:
            merged.append([start, end, rule_id, detail])
    spans = [
        AttributedSpan(start, end, AUTHOR_BY_RULE[rule_id], rule_id, detail, digest)
        for start, end, rule_id, detail in merged
    ]
    problems = partition_problems(text, spans, digest)
    if any(problems.values()):
        raise RuntimeError(f"attribution partition invariant violated: {problems}")
    return spans


# --- annotations in the existing cce.span-attribution.v1 form ---------------------------------


def to_annotation(
    text: str,
    spans: Sequence[AttributedSpan],
    *,
    owner_id: str | None = None,
    eligible: Collection[tuple[str, str]] = frozenset(),
) -> dict[str, Any]:
    """The `cce.span-attribution.v1` annotation of one message (see `attribution.py`).

    Unknown spans are left out (the contract fills uncovered text as unknown). Evidence names the
    rules version, the deciding rule, the length stratum and the span id. `profile_eligible` is
    False unless the span is the owner's, its (rule, stratum) passed the precision gate
    (`eligible`) and an `owner_id` binds it.
    """
    digest = text_digest(text)
    problems = partition_problems(text, spans, digest)
    if any(problems.values()):
        raise ValueError(f"attribution partition does not match the exact text: {problems}")
    entries: list[dict[str, Any]] = []
    for span in spans:
        if span.author == UNKNOWN:
            continue
        is_owner = span.author == OWNER
        evidence = [
            f"{RULES_VERSION}:rule:{span.rule}",
            f"stratum:{span.stratum}",
            f"span:{span.span_id}",
        ]
        if span.detail:
            evidence.append(f"detail:{span.detail}")
        entries.append(
            {
                "start": span.start,
                "end": span.end,
                "author": span.author,
                "author_id": owner_id if is_owner else None,
                "evidence": evidence,
                "assertion_status": "asserted",
                "profile_eligible": bool(
                    is_owner and owner_id and (span.rule, span.stratum) in eligible
                ),
            }
        )
    return {"schema": ATTRIBUTION_SCHEMA, "text_digest": digest, "spans": entries}


# --- the owner-review ledger and the precision gate ------------------------------------------

LEDGER_SCHEMA = "cce.attribution-review-ledger.v2"
# Rows of an earlier ledger version carry no stratum; they are read but never counted.
LEGACY_LEDGER_SCHEMAS = frozenset({"cce.attribution-review-ledger.v1"})
SAMPLE_SCHEMA = "cce.attribution-review-sample.v2"
VERDICTS = ("yes", "no", "unsure")
# A (rule, stratum) may be profile-eligible only when at least this many of its spans were
# reviewed by the owner and at least this share of them (counting unsure and no as not confirmed)
# was confirmed.
GATE_MIN_REVIEWED = 100
GATE_MIN_PRECISION_PERCENT = 95
SAMPLE_PER_STRATUM = 100
SAMPLE_SEED = "cce-attribution-review-v2"
# A long span is shown to the owner as a bounded excerpt plus its exact length.
EXCERPT_HEAD = 240
EXCERPT_TAIL = 120

_LEDGER_KEYS = frozenset(
    {"schema", "rules_version", "span_id", "rule", "stratum", "verdict", "reviewer", "reviewed_at"}
)
_LEGACY_LEDGER_KEYS = _LEDGER_KEYS - {"stratum"}
_SPAN_ID = re.compile(r"[0-9a-f]{32}")
UTC_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def utc_stamp(moment: datetime) -> str:
    """The ledger's UTC stamp for a time the caller supplies (this module never reads the clock)."""
    if moment.tzinfo is None:
        raise ValueError("a review time must be timezone-aware")
    return moment.astimezone(UTC).strftime(UTC_STAMP_FORMAT)


def validate_ledger_row(row: Any) -> dict[str, Any]:
    """A copy of a ledger row, or ValueError: nothing malformed is ever skipped silently."""
    if not isinstance(row, dict):
        raise ValueError("ledger row must be an object")
    legacy = row.get("schema") in LEGACY_LEDGER_SCHEMAS
    expected = _LEGACY_LEDGER_KEYS if legacy else _LEDGER_KEYS
    if set(row) != expected:
        raise ValueError(f"ledger row keys must be exactly {sorted(expected)}")
    if row["schema"] != LEDGER_SCHEMA and not legacy:
        raise ValueError(f"ledger row schema must be {LEDGER_SCHEMA}")
    version = row["rules_version"]
    if not isinstance(version, str) or not version.strip():
        raise ValueError("ledger row needs a rules_version")
    if not isinstance(row["span_id"], str) or not _SPAN_ID.fullmatch(row["span_id"]):
        raise ValueError("ledger row span_id must be 32 lowercase hexadecimal digits")
    rule = row["rule"]
    if (
        not isinstance(rule, str)
        or not rule
        or (version == RULES_VERSION and rule not in RULE_BY_ID)
    ):
        raise ValueError(f"ledger row names an unknown rule: {rule!r}")
    if not legacy and row["stratum"] not in STRATA:
        raise ValueError(f"ledger row stratum must be one of {list(STRATA)}")
    if row["verdict"] not in VERDICTS:
        raise ValueError(f"ledger row verdict must be one of {list(VERDICTS)}")
    reviewer = row["reviewer"]
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("ledger row needs a reviewer")
    stamp = row["reviewed_at"]
    try:
        if not isinstance(stamp, str):
            raise ValueError
        datetime.strptime(stamp, UTC_STAMP_FORMAT)  # noqa: DTZ007 - validates a stamp that states Z
    except ValueError:
        raise ValueError(
            "ledger row reviewed_at must be a UTC time like 2026-01-02T03:04:05Z"
        ) from None
    return dict(row)


def ledger_row(
    span_id: str,
    rule: str,
    verdict: str,
    reviewer: str,
    reviewed_at: str,
    *,
    stratum: str,
    rules_version: str = RULES_VERSION,
) -> dict[str, Any]:
    """One owner verdict on one span. The time is a parameter: no clock is read here."""
    return validate_ledger_row(
        {
            "schema": LEDGER_SCHEMA,
            "rules_version": rules_version,
            "span_id": span_id,
            "rule": rule,
            "stratum": stratum,
            "verdict": verdict,
            "reviewer": reviewer,
            "reviewed_at": reviewed_at,
        }
    )


@dataclass(frozen=True)
class LedgerContents:
    """What a ledger file holds: its valid rows, and the lines that could not be read."""

    rows: list[dict[str, Any]]
    quarantined: list[tuple[int, str]]


def parse_ledger(lines: Iterable[str]) -> LedgerContents:
    """Rows of a JSONL ledger. Blank lines are ignored; a malformed line is quarantined with its
    line number and reason, never counted for the gate, and never allowed to break a read."""
    rows: list[dict[str, Any]] = []
    quarantined: list[tuple[int, str]] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            rows.append(validate_ledger_row(json.loads(line)))
        except (ValueError, RecursionError) as error:
            quarantined.append((number, str(error).removeprefix("ledger row: ")))
    return LedgerContents(rows=rows, quarantined=quarantined)


def _decode(data: bytes) -> list[str]:
    return data.decode("utf-8", errors="replace").split("\n")


def read_ledger(path: Path) -> LedgerContents:
    if path.is_symlink():
        raise ValueError("ledger path must not be a symlink")
    return parse_ledger(_decode(path.read_bytes()))


def _kind_conflict(
    existing: Iterable[dict[str, Any]], incoming: Sequence[dict[str, Any]]
) -> str | None:
    """A message naming a span, among those the incoming rows touch, that one reviewer reviewed
    as a different rule or stratum."""

    def key_of(row: dict[str, Any]) -> tuple[str, str, str]:
        return (row["rules_version"], row["reviewer"], row["span_id"])

    touched = {key_of(row) for row in incoming if row["schema"] == LEDGER_SCHEMA}
    kinds: dict[tuple[str, str, str], tuple[str, str]] = {}
    for row in [*existing, *incoming]:
        if row["schema"] != LEDGER_SCHEMA or key_of(row) not in touched:
            continue
        kind = (row["rule"], row["stratum"])
        if kinds.setdefault(key_of(row), kind) != kind:
            return (
                f"span {row['span_id']} is already reviewed by {row['reviewer']} as "
                f"{kinds[key_of(row)]} and cannot also be reviewed as {kind}"
            )
    return None


def _read_all(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while chunk := os.read(descriptor, 1 << 20):
        chunks.append(chunk)
    return b"".join(chunks)


def append_ledger(path: Path, rows: Sequence[dict[str, Any]]) -> int:
    """Append validated rows to an append-only JSONL ledger.

    Nothing is written when any row is malformed or names a span one reviewer already reviewed
    as a different rule or stratum (re-answering a span under the same rule and stratum is fine:
    the last answer wins). Reading, checking and writing happen under one exclusive lock, in one
    write, and a ledger created here is private (mode 0600).
    """
    valid = [validate_ledger_row(row) for row in rows]
    if path.is_symlink():
        raise ValueError("ledger path must not be a symlink")
    conflict = _kind_conflict([], valid)
    if conflict:
        raise ValueError(f"ledger conflict: {conflict}; nothing was written")
    payload = "".join(json.dumps(row, sort_keys=True, ensure_ascii=True) + "\n" for row in valid)
    flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        existing = _read_all(descriptor)
        if existing and not existing.endswith(b"\n"):
            raise ValueError("ledger does not end with a newline; an earlier write was cut short")
        if not existing:
            os.fchmod(descriptor, 0o600)
        conflict = _kind_conflict(parse_ledger(_decode(existing)).rows, valid)
        if conflict:
            raise ValueError(f"ledger conflict: {conflict}; nothing was written")
        data = payload.encode("utf-8")
        while data:
            data = data[os.write(descriptor, data) :]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return len(valid)


@dataclass(frozen=True)
class StratumReview:
    """What the ledger says about one (rule, stratum) at the current rules version."""

    rule: str
    stratum: str
    reviewed: int
    confirmed: int
    rejected: int
    unsure: int
    precision: float | None
    eligible: bool


@dataclass(frozen=True)
class ReviewSummary:
    strata: dict[tuple[str, str], StratumReview]
    rows: int
    # Rows of another rules version or an older ledger schema: read, never counted.
    stale_rows: int = 0
    # Rows whose reviewer is not the owner identity: never counted.
    non_owner_rows: int = 0
    non_owner_reviewers: dict[str, int] = field(default_factory=dict)
    # Rows whose span id does not exist in the scan under the same rule and stratum.
    orphan_rows: int = 0
    # Rows of one reviewer that name the same span as different rules or strata: not counted.
    quarantined_rows: int = 0
    # Earlier answers to a span that a later row answered again (the last answer counts).
    superseded_rows: int = 0

    def for_rule(self, rule: str) -> list[StratumReview]:
        return [self.strata[(rule, stratum)] for stratum in STRATA]


def review_statistics(
    rows: Iterable[dict[str, Any]],
    *,
    matched: Mapping[str, tuple[str, str]],
    owner: str | None,
    rules_version: str = RULES_VERSION,
) -> ReviewSummary:
    """Per-(rule, stratum) review counts and eligibility from ledger rows.

    A row counts only when all of these hold: it is current (this `rules_version`, the current
    schema); its reviewer equals `owner` (None identifies nobody, so nothing counts); and its span
    id exists in the scan (`matched` maps span id to (rule, stratum)) under the same rule and
    stratum. A span counts once, by its last verdict in file order. `unsure` and `no` both count
    as reviewed and not confirmed. The precision test is integer arithmetic.
    """
    candidates: list[dict[str, Any]] = []
    total = stale = non_owner = orphan = quarantined = superseded = 0
    reviewers: dict[str, int] = {}
    for raw in rows:
        row = validate_ledger_row(raw)
        total += 1
        if row["rules_version"] != rules_version or row["schema"] != LEDGER_SCHEMA:
            stale += 1
        elif owner is None or row["reviewer"] != owner:
            non_owner += 1
            reviewers[row["reviewer"]] = reviewers.get(row["reviewer"], 0) + 1
        else:
            candidates.append(row)
    kinds: dict[str, set[tuple[str, str]]] = {}
    for row in candidates:
        kinds.setdefault(row["span_id"], set()).add((row["rule"], row["stratum"]))
    latest: dict[str, dict[str, Any]] = {}
    for row in candidates:
        if len(kinds[row["span_id"]]) > 1:
            quarantined += 1
        elif matched.get(row["span_id"]) != (row["rule"], row["stratum"]):
            orphan += 1
        else:
            superseded += row["span_id"] in latest
            latest[row["span_id"]] = row
    counts = {
        (rule.id, stratum): {verdict: 0 for verdict in VERDICTS}
        for rule in RULES
        for stratum in STRATA
    }
    for row in latest.values():
        counts[(row["rule"], row["stratum"])][row["verdict"]] += 1
    reviews: dict[tuple[str, str], StratumReview] = {}
    for (rule_id, stratum), tally in counts.items():
        reviewed = sum(tally.values())
        confirmed = tally["yes"]
        reviews[(rule_id, stratum)] = StratumReview(
            rule=rule_id,
            stratum=stratum,
            reviewed=reviewed,
            confirmed=confirmed,
            rejected=tally["no"],
            unsure=tally["unsure"],
            precision=(confirmed / reviewed) if reviewed else None,
            eligible=reviewed >= GATE_MIN_REVIEWED
            and confirmed * 100 >= GATE_MIN_PRECISION_PERCENT * reviewed,
        )
    return ReviewSummary(
        strata=reviews,
        rows=total,
        stale_rows=stale,
        non_owner_rows=non_owner,
        non_owner_reviewers=reviewers,
        orphan_rows=orphan,
        quarantined_rows=quarantined,
        superseded_rows=superseded,
    )


def eligible_strata(summary: ReviewSummary) -> frozenset[tuple[str, str]]:
    return frozenset(key for key, review in summary.strata.items() if review.eligible)


# --- the review sample -----------------------------------------------------------------------


def review_excerpt(text: str) -> tuple[str, bool]:
    """What the owner is shown for a span: the whole text up to 360 characters, else its first
    240 characters, a statement of how many characters are omitted, and its last 120."""
    if len(text) <= EXCERPT_HEAD + EXCERPT_TAIL:
        return text, False
    omitted = len(text) - EXCERPT_HEAD - EXCERPT_TAIL
    return f"{text[:EXCERPT_HEAD]} [...{omitted} chars omitted...] {text[-EXCERPT_TAIL:]}", True


class ReviewSampler:
    """A deterministic stratified sample of spans for the owner to confirm or reject.

    One stratum per (rule, length stratum). Within it the sample is the `per_stratum` distinct
    span texts with the smallest seeded hash, so it does not depend on the order spans are
    offered. A span of up to 360 characters is shown whole; a longer one as a bounded excerpt with
    its exact length, so the owner can judge the whole span. Rows show nothing about the record a
    span came from.
    """

    def __init__(
        self,
        *,
        per_stratum: int = SAMPLE_PER_STRATUM,
        seed: str = SAMPLE_SEED,
        exclude: Collection[str] = (),
    ) -> None:
        if isinstance(per_stratum, bool) or not isinstance(per_stratum, int) or per_stratum < 1:
            raise ValueError("per_stratum must be a positive integer")
        self.per_stratum = per_stratum
        self.seed = seed
        self.exclude = frozenset(exclude)
        self._heaps: dict[tuple[str, str], list[int]] = {}
        self._members: dict[tuple[str, str], dict[int, tuple[str, int, str, bool]]] = {}

    def offer(self, span: AttributedSpan, text: str) -> None:
        """Consider one span; `text` is exactly the span's own characters."""
        if span.rule not in RULE_BY_ID or span.author == UNKNOWN or not text.strip():
            return
        identifier = span.span_id
        if identifier in self.exclude:
            return
        stratum = span.stratum
        normalized = " ".join(text.split())
        digest = hashlib.sha256(
            f"{self.seed}\n{span.rule}\n{stratum}\n{normalized}".encode()
        ).digest()
        key = int.from_bytes(digest[:8], "big")
        slot = (span.rule, stratum)
        heap = self._heaps.setdefault(slot, [])
        members = self._members.setdefault(slot, {})
        if key in members:
            if identifier < members[key][0]:
                members[key] = (identifier, len(text), *review_excerpt(text))
        elif len(heap) < self.per_stratum:
            heapq.heappush(heap, -key)
            members[key] = (identifier, len(text), *review_excerpt(text))
        elif key < -heap[0]:
            evicted = -heapq.heapreplace(heap, -key)
            del members[evicted]
            members[key] = (identifier, len(text), *review_excerpt(text))

    def rows(self, *, partial: bool = False) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for rule in RULES:
            for stratum in STRATA:
                members = self._members.get((rule.id, stratum), {})
                for key in sorted(members):
                    identifier, length, text, excerpted = members[key]
                    result.append(
                        {
                            "schema": SAMPLE_SCHEMA,
                            "rules_version": RULES_VERSION,
                            "span_id": identifier,
                            "rule": rule.id,
                            "stratum": stratum,
                            "claim": rule.author,
                            "rule_description": rule.description,
                            "length": length,
                            "excerpted": excerpted,
                            "text": text,
                            "verdict": None,
                            "partial": partial,
                        }
                    )
        return result
