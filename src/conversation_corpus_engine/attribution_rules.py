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

import bisect
import fcntl
import functools
import hashlib
import heapq
import itertools
import json
import os
import re
import time
import types
import unicodedata
from array import array
from collections import Counter, deque
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .archive_provenance import INJECTED_TAGS, classify_user_turn
from .archive_read_library import FAMILIES
from .attribution import ATTRIBUTION_SCHEMA, text_digest

RULES_VERSION = "cce.attribution-rules@4"

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


# Names of AI assistants (case-insensitive; "GPT" with or without a version, "ChatGPT").
_AI_NAME = (
    r"(?:chat[-\s]?gpt|gpt(?:[-\s]?\d[\w.]*|s)?|claude|gemini|copilot|codex|bard|llama(?:[-\s]?\d[\w.]*)?"
    r"|mistral|grok|deepseek|perplexity|openai|anthropic)"
)
# "an LLM", "an AI", "an A.I.", "a chatbot", "a language model", "an assistant", "an AI assistant".
_AI_KIND = (
    r"an?\s+(?:llm|ai|a\.i\.|chat\s?bot|(?:large\s+)?language\s+model"
    r"|(?:ai\s+|a\.i\.\s+)?assistant)"
)
_AGENT_NOUN = (
    rf"(?:{_AI_NAME}|(?:an?\s+)?(?:ai|a\.i\.|llm|large\s+language\s+model|language\s+model"
    r"|chat\s?bot|assistant|ai\s+assistant))"
)
_AI_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ai-authorship",
        re.compile(
            r"\b(?:generated|written|drafted|composed|authored|created|produced|summari[sz]ed"
            r"|translated|rewritten|edited|suggested|answered|polished|proofread|rephrased)\s+"
            rf"(?:by|using|with|via|through)\s+(?:the\s+help\s+of\s+)?{_AGENT_NOUN}(?!\w)",
            re.IGNORECASE,
        ),
    ),
    (
        "used-an-ai",
        re.compile(
            r"\b(?:asked|used|had|let|got|prompted|using|with|via|through|from|by|per"
            rf"|according\s+to)\s+(?:{_AI_NAME}|{_AI_KIND})(?!\w)",
            re.IGNORECASE,
        ),
    ),
    (
        "with-help-of-ai",
        re.compile(
            rf"\bwith\s+(?:the\s+)?(?:help|assistance|aid)\s+(?:of|from)\s+{_AGENT_NOUN}(?!\w)",
            re.IGNORECASE,
        ),
    ),
    ("as-an-ai", re.compile(r"\bas\s+an\s+ai\b", re.IGNORECASE)),
    (
        "ai-generated",
        re.compile(
            r"\b(?:ai|a\.i\.|llm|machine)[-\s]+(?:generated|written|authored|produced|assisted"
            r"|drafted|composed)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "ai-said",
        re.compile(
            rf"\b{_AI_NAME}\s+(?:said|says|wrote|writes|suggested|replied|answered|responded"
            r"|recommended|generated|gave\s+me|told\s+me)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "prompt-block",
        re.compile(r"\bprompt\s*:[ \t]*(?:\n\s*)*(?:```|~~~|>|[\"\u201c'])", re.IGNORECASE),
    ),
    ("co-authored-by", re.compile(r"\bco-authored-by\s*:", re.IGNORECASE)),
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
        length, key = _collapsed_key(stripped)
        if length >= min_chars:
            lead = len(piece) - len(piece.lstrip())
            units.append((start + lead, start + lead + len(stripped), key))
    return units


def _collapsed_key(stripped: str) -> tuple[int, int]:
    """Length and census key of a text with every whitespace run collapsed to one space. A huge
    text is hashed piece by piece (the pieces end at whitespace, so the bytes hashed are exactly
    those of the one-shot form) instead of being split into every one of its words at once."""
    if len(stripped) <= 2 * ECHO_CHUNK:
        collapsed = " ".join(stripped.split())
        return len(collapsed), _repeat_key(collapsed)
    digest = hashlib.sha256()
    length = 0
    for chunk in _chunks(stripped):
        piece = " ".join(chunk.split())
        if piece:
            data = f" {piece}" if length else piece
            digest.update(data.encode("utf-8"))
            length += len(data)
    return length, int.from_bytes(digest.digest()[:8], "big")


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

DEFAULT_ECHO_MIN_PARAGRAPH = 60
DEFAULT_ECHO_MIN_RUN = 300
DEFAULT_ECHO_MIN_SHARED = 60
DEFAULT_ECHO_MIN_RUN_SHARED = 100
DEFAULT_ECHO_HISTORY = 400_000
# Only the head of an assistant turn is indexed: a longer turn costs no more memory than this.
DEFAULT_ECHO_TURN = 400_000
ECHO_CHUNK = 1 << 16
_LIST_MARKER = re.compile(
    r"^[ \t]*(?:[-*+•·▪‣◦]|\(?\d{1,3}[.)]|\(?[a-z][.)])[ \t]+",
    re.MULTILINE,
)
_NOT_TEXT = re.compile(r"[^\w\s]|_")
_ANY_SPACE = re.compile(r"\s")
_SPACE_RUN = re.compile(r"\s+")


def match_form(text: str) -> str:
    """Text as the echo compares it, so that rewrapping, case, markdown, list numbering,
    punctuation, smart quotes and dashes, compatibility characters and spacing cannot hide a paste:
    NFKC, case-folded, list markers and numbering dropped, every punctuation or symbol character
    read as a space, whitespace collapsed."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    folded = _LIST_MARKER.sub(" ", folded)
    return " ".join(_NOT_TEXT.sub(" ", folded).split())


def _chunks(text: str) -> Iterator[str]:
    """Pieces of about `ECHO_CHUNK` characters that end at a line break (else at whitespace)."""
    position, length = 0, len(text)
    while position < length:
        end = position + ECHO_CHUNK
        if end >= length:
            yield text[position:]
            return
        newline = text.find("\n", end, end + ECHO_CHUNK)
        if newline != -1:
            stop = newline + 1
        else:
            blank = _ANY_SPACE.search(text, end)
            stop = blank.end() if blank else length
        yield text[position:stop]
        position = stop


def _match_form_of(text: str) -> str:
    """`match_form` of any size without a list of every word of a huge text."""
    if len(text) <= 2 * ECHO_CHUNK:
        return match_form(text)
    return " ".join(piece for piece in map(match_form, _chunks(text)) if piece)


def _match_head(text: str, limit: int) -> str:
    """The first `limit` characters of `_match_form_of(text)`, normalizing no more than needed."""
    if len(text) <= 2 * ECHO_CHUNK:
        return match_form(text)[:limit]
    pieces: list[str] = []
    total = 0
    for chunk in _chunks(text):
        piece = match_form(chunk)
        if piece:
            pieces.append(piece)
            total += len(piece) + 1
            if total > limit:
                break
    return " ".join(pieces)[:limit]


class AssistantEcho:
    """Finds text a user turn pastes back from an earlier assistant turn of its conversation.

    Feed the conversation's turns in order: `add_assistant` for each assistant reply and
    `pasted_back` (or `pasted_back_turns`, for consecutive user turns) for the user turns. Both
    sides are compared in their `match_form`, and a stretch of `min_shared` characters that also
    occurs in an earlier assistant turn is a match (the comparison is by 64-bit hash; a collision
    would only make a paste look more pasted, never less). Two kinds of candidate are returned as
    ranges:

    * a paragraph of at least `min_paragraph` characters that holds a match;
    * a run of adjacent paragraphs, each touched by a match, whose joined text is at least
      `min_run` characters and that holds a stretch of at least `min_run_shared` shared
      characters (a pasted answer is often several short paragraphs). Fenced code blocks between
      paragraphs do not break a run, and the consecutive user turns given to `pasted_back_turns`
      (no assistant turn between them) are matched as one run.

    A paragraph that only quotes a short phrase (fewer than `min_shared` shared characters) is
    never a candidate, and neither is a short paragraph on its own. Memory is bounded: only the
    first `turn_limit` normalized characters of an assistant turn are indexed (a paste of text
    beyond that head is not recognised), the total indexed text never exceeds `history_limit`
    (the oldest turns go first, the newest turn included in the count), and each indexed turn is
    kept as an array of 64-bit window hashes, never as text. Matching across conversations is not
    attempted.
    """

    def __init__(
        self,
        *,
        min_paragraph: int = DEFAULT_ECHO_MIN_PARAGRAPH,
        min_run: int = DEFAULT_ECHO_MIN_RUN,
        min_shared: int = DEFAULT_ECHO_MIN_SHARED,
        min_run_shared: int = DEFAULT_ECHO_MIN_RUN_SHARED,
        history_limit: int = DEFAULT_ECHO_HISTORY,
        turn_limit: int = DEFAULT_ECHO_TURN,
    ) -> None:
        for name, value in (
            ("min_paragraph", min_paragraph),
            ("min_run", min_run),
            ("min_shared", min_shared),
            ("min_run_shared", min_run_shared),
            ("history_limit", history_limit),
            ("turn_limit", turn_limit),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if min_run_shared < min_shared:
            raise ValueError("min_run_shared must not be smaller than min_shared")
        self.min_paragraph = min_paragraph
        self.min_run = min_run
        self.min_shared = min_shared
        self.min_run_shared = min_run_shared
        self.history_limit = history_limit
        self.turn_limit = turn_limit
        self._turns: deque[tuple[int, array]] = deque()
        self._size = 0
        self._index: set[int] | None = None

    @property
    def indexed_characters(self) -> int:
        """How many normalized characters of assistant text are indexed right now."""
        return self._size

    def _hashes(self, form: str) -> Iterator[int]:
        width = self.min_shared
        return (hash(form[i : i + width]) for i in range(len(form) - width + 1))

    def add_assistant(self, text: str) -> None:
        head = _match_head(text, min(self.turn_limit, self.history_limit))
        if len(head) < self.min_shared:
            return
        windows = array("q", self._hashes(head))
        self._turns.append((len(head), windows))
        self._size += len(head)
        evicted = False
        while self._size > self.history_limit and self._turns:
            self._size -= self._turns.popleft()[0]
            evicted = True
        if evicted:
            self._index = None
        elif self._index is not None:
            self._index.update(windows)

    def _windows(self) -> set[int]:
        if self._index is None:
            self._index = set().union(*(windows for _, windows in self._turns))
        return self._index

    def pasted_back(self, text: str) -> tuple[Range, ...]:
        return self.pasted_back_turns([text])[0]

    def pasted_back_turns(self, texts: Sequence[str]) -> list[tuple[Range, ...]]:
        """The pasted-back ranges of consecutive user turns, matched as one conversation stretch;
        one tuple of ranges (offsets into that turn) per turn."""
        if not self._turns:
            return [() for _ in texts]
        width = self.min_shared
        # Every paragraph of every turn: (turn, start, end, in a fenced block, normalized range,
        # length with whitespace collapsed).
        paragraphs: list[tuple[int, int, int, bool, Range | None, int]] = []
        pieces: list[str] = []
        position = 0
        for turn, text in enumerate(texts):
            lines = _lines(text)
            fenced = [(start, end) for start, end, _ in _fenced_blocks(lines)]
            fence_starts = [start for start, _ in fenced]
            for start, end in _paragraphs(lines):
                found = bisect.bisect_right(fence_starts, start) - 1
                inside = found >= 0 and end <= fenced[found][1]
                body = text[start:end]
                form = _match_form_of(body)
                if form:
                    span: Range | None = (position, position + len(form))
                    pieces.append(form)
                    position += len(form) + 1
                else:
                    span = None
                collapsed = len(_SPACE_RUN.sub(" ", body))
                paragraphs.append((turn, start, end, inside, span, collapsed))
        results: list[list[Range]] = [[] for _ in texts]
        combined = " ".join(pieces)
        if len(combined) < width:
            return [() for _ in texts]
        index = self._windows()
        hits = [i for i, h in enumerate(self._hashes(combined)) if h in index]
        if not hits:
            return [() for _ in texts]
        # Maximal stretches of consecutive matching windows, as normalized ranges.
        stretches: list[Range] = []
        first = last = hits[0]
        for hit in hits[1:]:
            if hit == last + 1:
                last = hit
            else:
                stretches.append((first, last + width))
                first = last = hit
        stretches.append((first, last + width))
        stretch_starts = [start for start, _ in stretches]

        def touched(span: Range) -> bool:
            low = bisect.bisect_right(hits, span[0] - width)
            return low < len(hits) and hits[low] < span[1]

        def holds_match(span: Range) -> bool:
            low = bisect.bisect_left(hits, span[0])
            return low < len(hits) and hits[low] + width <= span[1]

        def longest_shared(low: int, high: int) -> int:
            best = 0
            at = max(0, bisect.bisect_right(stretch_starts, low) - 1)
            while at < len(stretches) and stretches[at][0] < high:
                if stretches[at][1] > low:
                    best = max(best, stretches[at][1] - stretches[at][0])
                at += 1
            return best

        def original(turn: int, begin: int, finish: int) -> Range:
            body = texts[turn][begin:finish]
            lead = len(body) - len(body.lstrip())
            return begin + lead, begin + len(body.rstrip())

        at = 0
        while at < len(paragraphs):
            _, _, _, inside, span, _ = paragraphs[at]
            if inside or span is None or not touched(span):
                at += 1
                continue
            # Extend the run over touched paragraphs; code blocks and marker-only paragraphs
            # between them neither join nor break it.
            members = [at]
            probe = at + 1
            while probe < len(paragraphs):
                _, _, _, inside_probe, span_probe, _ = paragraphs[probe]
                if inside_probe or span_probe is None:
                    probe += 1
                elif touched(span_probe):
                    members.append(probe)
                    probe += 1
                else:
                    break
            spans = [paragraphs[member][4] for member in members]
            # The joined length counts the paragraphs as written as well as normalized, so that
            # stripping markup cannot shrink a run below the threshold.
            joined = (
                max(
                    sum(item[1] - item[0] for item in spans),
                    sum(paragraphs[member][5] for member in members),
                )
                + len(members)
                - 1
            )
            if (
                len(members) >= 2
                and joined >= self.min_run
                and longest_shared(spans[0][0], spans[-1][1]) >= self.min_run_shared
            ):
                by_turn: dict[int, list[int]] = {}
                for member in members:
                    by_turn.setdefault(paragraphs[member][0], []).append(member)
                for turn, group in by_turn.items():
                    results[turn].append(
                        original(turn, paragraphs[group[0]][1], paragraphs[group[-1]][2])
                    )
            else:
                for member in members:
                    turn, begin, finish, _, span, _ = paragraphs[member]
                    if span[1] - span[0] >= self.min_paragraph and holds_match(span):
                        results[turn].append(original(turn, begin, finish))
            at = members[-1] + 1
        return [tuple(sorted(found)) for found in results]


def rule_pasted_back(context: MessageContext) -> list[Claim]:
    """Text a user turn pastes back from an earlier assistant turn was written by the assistant,
    not by the person."""
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
# One of these four, followed by anything (a blank line or the body), starts a pasted email.
_FIRST_MAIL_HEADER = re.compile(r"^(?:From|To|Subject|Date):[ \t]*\S", re.IGNORECASE)
_MAIL_MARKERS = (
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^-{2,}\s*Forwarded message\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^Begin forwarded message:?\s*$", re.IGNORECASE),
    re.compile(r"^On .{5,200} wrote:\s*$", re.IGNORECASE),
)
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)+\|?\s*$")


def rule_pasted_block(context: MessageContext) -> list[Claim]:
    """A pasted email or forward and a markdown table are third-party text. A From, To, Subject
    or Date header line that something follows, or two header lines in a row, or a forward marker
    starts an email; where it starts its extent cannot be proven, so it runs to the end of the
    message. A table is two or more lines in a row that each hold two or more pipes, or lines
    with pipes that include a separator row."""
    lines = _lines(context.text)
    claims: list[Claim] = []
    for index, (start, _, content) in enumerate(lines):
        marker = any(pattern.match(content) for pattern in _MAIL_MARKERS)
        header_run = (
            _MAIL_HEADER.match(content)
            and index + 1 < len(lines)
            and _MAIL_HEADER.match(lines[index + 1][2])
            and (index == 0 or not _MAIL_HEADER.match(lines[index - 1][2]))
        ) or (_FIRST_MAIL_HEADER.match(content) and index + 1 < len(lines))
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
    index = 0
    while index < len(lines):
        if lines[index][2].count("|") < 2:
            index += 1
            continue
        stop = index
        while stop < len(lines) and lines[stop][2].count("|") >= 2:
            stop += 1
        table = (lines[index][0], lines[stop - 1][1], "markdown-table")
        if stop - index >= 2 and table not in claims:
            claims.append(table)
        index = stop
    # Rows of a table without outer pipes ("Name | Cost"): two or more consecutive lines that
    # each hold the same number of pipes, at least one.
    index = 0
    while index < len(lines):
        pipes = lines[index][2].count("|")
        if not pipes:
            index += 1
            continue
        stop = index
        while stop < len(lines) and lines[stop][2].count("|") == pipes:
            stop += 1
        table = (lines[index][0], lines[stop - 1][1], "markdown-table")
        if stop - index >= 2 and table not in claims:
            claims.append(table)
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


# Characters that show nothing by themselves: spaces, line breaks, control characters, the
# zero-width and other format characters (U+200B, U+FEFF, U+2060, ...) and combining and
# enclosing marks (a mark alone has nothing to sit on). Text made only of these is nobody's.
_BLANK_CATEGORIES = frozenset({"Cf", "Zs", "Zl", "Zp", "Cc", "Mn", "Me"})
_MARK_CATEGORIES = frozenset({"Mn", "Me"})


def is_blank(char: str) -> bool:
    return char.isspace() or unicodedata.category(char) in _BLANK_CATEGORIES


def blank_text(text: str) -> bool:
    return all(is_blank(char) for char in text)


def _trimmed(text: str, pieces: list[Claim]) -> list[Claim]:
    """An owner span never starts or ends with a blank character (see `is_blank`) and is never
    only blank characters. A combining mark that follows a visible character belongs to it (an
    accent in decomposed text) and is not trimmed from the end."""
    result: list[Claim] = []
    for start, end, detail in pieces:
        first, last = start, end
        while first < last and is_blank(text[first]):
            first += 1
        if first == last:
            continue
        while is_blank(text[last - 1]) and not _rides_on_letter(text, first, last - 1):
            last -= 1
        result.append((first, last, detail))
    return result


def _rides_on_letter(text: str, first: int, at: int) -> bool:
    """True if `text[at]` is a combining mark whose base, before it and not before `first`, is a
    visible character."""
    if unicodedata.category(text[at]) not in _MARK_CATEGORIES:
        return False
    before = at - 1
    while before >= first and unicodedata.category(text[before]) in _MARK_CATEGORIES:
        before -= 1
    return before >= first and not is_blank(text[before])


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
    if not text.isascii():
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            # Text with a lone surrogate has no digest the contract could bind a span to: all of
            # it is unknown (its digest here is of the surrogate-passing bytes, for the span id).
            digest = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
            return [AttributedSpan(0, length, UNKNOWN, UNATTRIBUTED, "", digest)]
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
    verdicts: Mapping[str, str] = types.MappingProxyType({}),
) -> dict[str, Any]:
    """The `cce.span-attribution.v1` annotation of one message (see `attribution.py`).

    Unknown spans are left out (the contract fills uncovered text as unknown). Evidence names the
    rules version, the deciding rule, the length stratum and the span id. `profile_eligible` is
    decided per span: it is False unless the span is the owner's, its (rule, stratum) passed the
    precision gate (`eligible`), an `owner_id` binds it and the owner did not answer `no` or
    `unsure` for this very span. `verdicts` maps span ids to the owner's counted verdict; a span
    with one carries it as `owner_verdict` (and as `owner_verdict:<verdict>` evidence, which
    survives the contract's key filtering). A confirmed span is still not eligible while its
    stratum is closed.
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
        verdict = verdicts.get(span.span_id)
        if verdict is not None:
            evidence.append(f"owner_verdict:{verdict}")
        entry = {
            "start": span.start,
            "end": span.end,
            "author": span.author,
            "author_id": owner_id if is_owner else None,
            "evidence": evidence,
            "assertion_status": "asserted",
            "profile_eligible": bool(
                is_owner
                and owner_id
                and (span.rule, span.stratum) in eligible
                and verdict in (None, "yes")
            ),
        }
        if verdict is not None:
            entry["owner_verdict"] = verdict
        entries.append(entry)
    return {"schema": ATTRIBUTION_SCHEMA, "text_digest": digest, "spans": entries}


# --- the owner-review ledger and the precision gate ------------------------------------------

LEDGER_SCHEMA = "cce.attribution-review-ledger.v4"
# Rows of an earlier ledger schema were written under other gate rules; they are read but never
# counted.
LEGACY_LEDGER_SCHEMAS = frozenset(
    {
        "cce.attribution-review-ledger.v1",
        "cce.attribution-review-ledger.v2",
        "cce.attribution-review-ledger.v3",
    }
)
SAMPLE_SCHEMA = "cce.attribution-review-sample.v4"
SAMPLE_HEADER_SCHEMA = "cce.attribution-review-sample-header.v4"
VERDICTS = ("yes", "no", "unsure")
# A (rule, stratum) may be profile-eligible only when at least this many of its spans were
# reviewed by the owner and at least this share of them (counting unsure and no as not confirmed)
# was confirmed.
GATE_MIN_REVIEWED = 100
GATE_MIN_PRECISION_PERCENT = 95
SAMPLE_PER_STRATUM = 100
SAMPLE_SEED = "cce-attribution-review-v4"
# A long span is shown to the owner as a bounded excerpt plus its exact length.
EXCERPT_HEAD = 240
EXCERPT_TAIL = 120

_LEGACY_V1_KEYS = frozenset(
    {"schema", "rules_version", "span_id", "rule", "verdict", "reviewer", "reviewed_at"}
)
_LEGACY_V2_KEYS = _LEGACY_V1_KEYS | {"stratum"}
# What a row binds itself to: the sample it answers, the full text it saw (as a digest) and how
# many rows that sample drew for its (rule, stratum).
_LEGACY_V3_KEYS = _LEGACY_V2_KEYS | {"sample_digest", "text_sha256", "drawn"}
_LEDGER_KEYS = _LEGACY_V3_KEYS
_LEGACY_KEYS_BY_SCHEMA = {
    "cce.attribution-review-ledger.v1": _LEGACY_V1_KEYS,
    "cce.attribution-review-ledger.v2": _LEGACY_V2_KEYS,
    "cce.attribution-review-ledger.v3": _LEGACY_V3_KEYS,
}
_SPAN_ID = re.compile(r"[0-9a-f]{32}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
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
    expected = _LEGACY_KEYS_BY_SCHEMA[row["schema"]] if legacy else _LEDGER_KEYS
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
    if "stratum" in expected and row["stratum"] not in STRATA:
        raise ValueError(f"ledger row stratum must be one of {list(STRATA)}")
    if not legacy:
        for key in ("sample_digest", "text_sha256"):
            if not isinstance(row[key], str) or not _SHA256.fullmatch(row[key]):
                raise ValueError(f"ledger row {key} must be 64 lowercase hexadecimal digits")
        drawn = row["drawn"]
        if isinstance(drawn, bool) or not isinstance(drawn, int) or drawn < 1:
            raise ValueError("ledger row drawn must be a positive integer")
    if row["verdict"] not in VERDICTS:
        raise ValueError(f"ledger row verdict must be one of {list(VERDICTS)}")
    reviewer = row["reviewer"]
    if not isinstance(reviewer, str) or blank_text(reviewer):
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
    sample_digest: str,
    text_sha256: str,
    drawn: int,
    rules_version: str = RULES_VERSION,
) -> dict[str, Any]:
    """One owner verdict on one span of one sample. The time is a parameter: no clock is read
    here. The row names the sample (`sample_digest`), the full text the owner saw
    (`text_sha256`) and how many rows that sample drew for the span's rule and stratum."""
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
            "sample_digest": sample_digest,
            "text_sha256": text_sha256,
            "drawn": drawn,
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


LEDGER_LOCK_SECONDS = 30.0
_LOCK_POLL_SECONDS = 0.05


@dataclass(frozen=True)
class LedgerAppend:
    """What one append did."""

    appended: int
    # The ledger ended in a line cut short; a newline was written first, so that fragment is now
    # one malformed line, which every reader quarantines.
    repaired_torn_line: bool = False


def _lock_exclusively(
    descriptor: int,
    timeout: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> None:
    """Take the exclusive lock, polling for up to `timeout` seconds: a ledger some other process
    holds is waited for, never waited on forever."""
    deadline = clock() + timeout
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if clock() >= deadline:
                raise ValueError(
                    f"ledger is locked by another process (waited {timeout:g} seconds); "
                    "nothing was written"
                ) from None
            sleep(_LOCK_POLL_SECONDS)


def append_rows(
    path: Path,
    rows: Sequence[dict[str, Any]],
    *,
    lock_timeout: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    precondition: Callable[[list[dict[str, Any]]], None] | None = None,
) -> LedgerAppend:
    """Append validated rows to an append-only JSONL ledger.

    Nothing is written when any row is malformed or names a span one reviewer already reviewed
    as a different rule or stratum (re-answering a span under the same rule and stratum is fine:
    the last answer wins). Reading, checking and writing happen under one exclusive lock (waited
    for up to `LEDGER_LOCK_SECONDS`, else ValueError), in one write, and a ledger created here is
    private (mode 0600). If the file does not end in a newline an earlier write was cut short:
    a newline is written first, the fragment stays as one quarantined line, and the result says
    so. `precondition`, if given, is called with the valid rows already in the ledger while the
    lock is held (and with no rows, before anything is created, for a ledger that does not exist
    yet); if it raises, nothing is written.
    """
    valid = [validate_ledger_row(row) for row in rows]
    if path.is_symlink():
        raise ValueError("ledger path must not be a symlink")
    conflict = _kind_conflict([], valid)
    if conflict:
        raise ValueError(f"ledger conflict: {conflict}; nothing was written")
    if precondition is not None and not path.exists():
        precondition([])
    payload = "".join(json.dumps(row, sort_keys=True, ensure_ascii=True) + "\n" for row in valid)
    flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        _lock_exclusively(
            descriptor, LEDGER_LOCK_SECONDS if lock_timeout is None else lock_timeout, clock, sleep
        )
        existing = _read_all(descriptor)
        torn = bool(existing) and not existing.endswith(b"\n")
        if not existing:
            os.fchmod(descriptor, 0o600)
        existing_rows = parse_ledger(_decode(existing)).rows
        if precondition is not None:
            precondition(existing_rows)
        conflict = _kind_conflict(existing_rows, valid)
        if conflict:
            raise ValueError(f"ledger conflict: {conflict}; nothing was written")
        data = (b"\n" if torn else b"") + payload.encode("utf-8")
        while data:
            data = data[os.write(descriptor, data) :]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return LedgerAppend(appended=len(valid), repaired_torn_line=torn)


def append_ledger(path: Path, rows: Sequence[dict[str, Any]]) -> int:
    """`append_rows`, returning only how many rows were appended."""
    return append_rows(path, rows).appended


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
    # Rows a recorded sample drew for this (rule, stratum) that the ledger has no row for; they
    # are counted as reviewed and unsure (they are inside `unsure` and `reviewed`).
    missing: int = 0


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
    # Rows whose span exists in the scan but whose text is not the text the row answered.
    text_mismatch_rows: int = 0
    # Rows that recorded samples drew but the ledger lacks, counted as unsure in their stratum.
    missing_rows: int = 0
    # Earlier answers to a span that a later row answered again (the last answer counts).
    superseded_rows: int = 0
    # The owner's counted verdict on each span (the last answer). A span the ledger names but
    # cannot count cleanly (conflicting or edited rows) is held back as `unsure`. Eligibility is
    # decided per span from this map: `no` and `unsure` spans are never eligible.
    verdicts: dict[str, str] = field(default_factory=dict)

    def for_rule(self, rule: str) -> list[StratumReview]:
        return [self.strata[(rule, stratum)] for stratum in STRATA]


def review_statistics(
    rows: Iterable[dict[str, Any]],
    *,
    matched: Mapping[str, tuple[str, str, str]],
    owner: str | None,
    rules_version: str = RULES_VERSION,
) -> ReviewSummary:
    """Per-(rule, stratum) review counts and eligibility from ledger rows.

    A row counts only when all of these hold: it is current (this `rules_version`, the current
    schema); its reviewer equals `owner` (None identifies nobody, so nothing counts); and its span
    exists in the scan (`matched` maps span id to (rule, stratum, text_sha256)) under the same rule
    and stratum with the very text the row answered. A span counts once, by its last verdict in
    file order. `unsure` and `no` both count as reviewed and not confirmed.

    Precision is over the rows a sample drew, not over the rows that happen to be in the ledger:
    every row carries the digest of its sample and how many rows that sample drew for its rule and
    stratum, and a drawn row the ledger lacks counts as `unsure`. The test is integer arithmetic.
    """
    candidates: list[dict[str, Any]] = []
    total = stale = non_owner = orphan = quarantined = superseded = mismatched = 0
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
    held_back: set[str] = set()
    for row in candidates:
        fact = matched.get(row["span_id"])
        if len(kinds[row["span_id"]]) > 1:
            quarantined += 1
            held_back.add(row["span_id"])
        elif fact is None or fact[:2] != (row["rule"], row["stratum"]):
            orphan += 1
            if fact is not None:
                held_back.add(row["span_id"])  # the span exists, but this row misdescribes it
        elif fact[2] != row["text_sha256"]:
            mismatched += 1
            held_back.add(row["span_id"])  # the span exists, but its text is not what was seen
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
    # A drawn row the ledger holds no row for is a row somebody did not answer: unsure.
    drawn_rows: dict[tuple[str, str, str], tuple[int, set[str]]] = {}
    for row in candidates:
        key = (row["sample_digest"], row["rule"], row["stratum"])
        drawn, present = drawn_rows.get(key, (0, set()))
        present.add(row["span_id"])
        drawn_rows[key] = (max(drawn, row["drawn"]), present)
    missing: dict[tuple[str, str], int] = {}
    for (_, rule_id, stratum), (drawn, present) in drawn_rows.items():
        short = max(0, drawn - len(present))
        missing[(rule_id, stratum)] = missing.get((rule_id, stratum), 0) + short
    reviews: dict[tuple[str, str], StratumReview] = {}
    for (rule_id, stratum), tally in counts.items():
        absent = missing.get((rule_id, stratum), 0)
        reviewed = sum(tally.values()) + absent
        confirmed = tally["yes"]
        reviews[(rule_id, stratum)] = StratumReview(
            rule=rule_id,
            stratum=stratum,
            reviewed=reviewed,
            confirmed=confirmed,
            rejected=tally["no"],
            unsure=tally["unsure"] + absent,
            precision=(confirmed / reviewed) if reviewed else None,
            eligible=reviewed >= GATE_MIN_REVIEWED
            and confirmed * 100 >= GATE_MIN_PRECISION_PERCENT * reviewed,
            missing=absent,
        )
    verdicts = {span: row["verdict"] for span, row in latest.items()}
    for span in held_back - verdicts.keys():
        verdicts[span] = "unsure"
    return ReviewSummary(
        strata=reviews,
        rows=total,
        stale_rows=stale,
        non_owner_rows=non_owner,
        non_owner_reviewers=reviewers,
        orphan_rows=orphan,
        quarantined_rows=quarantined,
        text_mismatch_rows=mismatched,
        missing_rows=sum(missing.values()),
        superseded_rows=superseded,
        verdicts=verdicts,
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
    its exact length, so the owner can judge the whole span; every row also carries the SHA-256 of
    the span's full text. Rows show nothing about the record a span came from.

    `exclude` keeps spans out that were already reviewed. A mapping from span id to
    (rule, stratum, text_sha256) excludes a span only when it is exactly that reviewed span; a
    plain collection of ids excludes those ids whatever they are.
    """

    def __init__(
        self,
        *,
        per_stratum: int = SAMPLE_PER_STRATUM,
        seed: str = SAMPLE_SEED,
        exclude: Collection[str] | Mapping[str, tuple[str, str, str]] = (),
    ) -> None:
        if isinstance(per_stratum, bool) or not isinstance(per_stratum, int) or per_stratum < 1:
            raise ValueError("per_stratum must be a positive integer")
        self.per_stratum = per_stratum
        self.seed = seed
        self.exclude: Mapping[str, tuple[str, str, str] | None] = (
            dict(exclude) if isinstance(exclude, Mapping) else dict.fromkeys(exclude)
        )
        self._heaps: dict[tuple[str, str], list[int]] = {}
        self._members: dict[tuple[str, str], dict[int, tuple[str, int, str, bool, str]]] = {}

    def _already_reviewed(self, span: AttributedSpan, text: str) -> bool:
        if span.span_id not in self.exclude:
            return False
        reviewed = self.exclude[span.span_id]
        return reviewed is None or reviewed == (span.rule, span.stratum, text_digest(text))

    def offer(self, span: AttributedSpan, text: str) -> None:
        """Consider one span; `text` is exactly the span's own characters."""
        if span.rule not in RULE_BY_ID or span.author == UNKNOWN or blank_text(text):
            return
        identifier = span.span_id
        if self._already_reviewed(span, text):
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
                members[key] = (identifier, len(text), *review_excerpt(text), text_digest(text))
        elif len(heap) < self.per_stratum:
            heapq.heappush(heap, -key)
            members[key] = (identifier, len(text), *review_excerpt(text), text_digest(text))
        elif key < -heap[0]:
            evicted = -heapq.heapreplace(heap, -key)
            del members[evicted]
            members[key] = (identifier, len(text), *review_excerpt(text), text_digest(text))

    def rows(self, *, partial: bool = False) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for rule in RULES:
            for stratum in STRATA:
                members = self._members.get((rule.id, stratum), {})
                for key in sorted(members):
                    identifier, length, text, excerpted, full_digest = members[key]
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
                            "text_sha256": full_digest,
                            "verdict": None,
                            "partial": partial,
                        }
                    )
        return result


# --- the sample document: a header that binds the rows --------------------------------------

_SAMPLE_ROW_KEYS = frozenset(
    {
        "schema",
        "rules_version",
        "span_id",
        "rule",
        "stratum",
        "claim",
        "rule_description",
        "length",
        "excerpted",
        "text",
        "text_sha256",
        "verdict",
        "partial",
    }
)
_SAMPLE_HEADER_KEYS = frozenset(
    {
        "schema",
        "rules_version",
        "rows",
        "sample_digest",
        "library",
        "owner_id",
        "seed",
        "per_stratum",
        "partial",
        "repeat_min_chars",
        "repeat_min_occurrences",
        "exclusion_digest",
        "drawn",
    }
)


def load_json_line(line: str, label: str, number: int) -> Any:
    """One JSON value of a JSONL file; text nested too deeply to parse is refused like any other
    unreadable line instead of escaping as a crash."""
    try:
        return json.loads(line)
    except RecursionError:
        raise ValueError(f"{label} line {number}: unreadable (nested too deeply)") from None
    except ValueError as error:
        raise ValueError(f"{label} line {number}: not JSON") from error


def sample_digest(header: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> str:
    """SHA-256 over the canonical JSON of the whole header (every field except `sample_digest`
    itself) and of the rows, answers left out (`verdict`). Editing any header field, such as the
    seed, the owner, the library, the repeat-census settings or the ledger state, changes it."""
    document = {
        "header": {key: value for key, value in header.items() if key != "sample_digest"},
        "rows": [{key: value for key, value in row.items() if key != "verdict"} for row in rows],
    }
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def drawn_counts(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """How many rows a sample drew for each `rule:stratum`."""
    counts = Counter(f"{row['rule']}:{row['stratum']}" for row in rows)
    return dict(sorted(counts.items()))


def exclusion_digest(rows: Iterable[Mapping[str, Any]], owner: str) -> str:
    """Names the ledger state a draw excludes against: SHA-256 over the sorted
    (span id, verdict) pairs of the current rows of `owner`. Rows of other reviewers, of other
    rules versions and of older schemas hide nothing from a draw, so they are not part of it.
    A repeated answer is part of the state, so every recording moves the digest on."""
    pairs = sorted(
        [row["span_id"], row["verdict"]]
        for row in rows
        if row["schema"] == LEDGER_SCHEMA
        and row["rules_version"] == RULES_VERSION
        and row["reviewer"] == owner
    )
    canonical = json.dumps(pairs, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


EMPTY_EXCLUSION_DIGEST = exclusion_digest([], "")


def sample_header(
    rows: Sequence[Mapping[str, Any]],
    *,
    library: str,
    owner_id: str,
    seed: str,
    per_stratum: int,
    partial: bool,
    repeat_min_chars: int = DEFAULT_REPEAT_MIN_CHARS,
    repeat_min_occurrences: int = DEFAULT_REPEAT_MIN_OCCURRENCES,
    exclusion_digest: str = EMPTY_EXCLUSION_DIGEST,
) -> dict[str, Any]:
    """The first line of a sample file: it counts the rows, digests itself and the rows, and names
    the library (its fingerprint), the owner the sample was drawn for, the settings that decide
    which spans exist and in what order they are drawn (seed, repeat census) and the ledger state
    the draw excluded against (`exclusion_digest`)."""
    header = {
        "schema": SAMPLE_HEADER_SCHEMA,
        "rules_version": RULES_VERSION,
        "rows": len(rows),
        "library": library,
        "owner_id": owner_id,
        "seed": seed,
        "per_stratum": per_stratum,
        "partial": partial,
        "repeat_min_chars": repeat_min_chars,
        "repeat_min_occurrences": repeat_min_occurrences,
        "exclusion_digest": exclusion_digest,
        "drawn": drawn_counts(rows),
    }
    return {**header, "sample_digest": sample_digest(header, rows)}


def preview_only_reasons(header: Mapping[str, Any]) -> list[str]:
    """Why a sample may be looked at but not recorded, else an empty list. Only the default draw
    over the whole library can be recorded: its rows are fixed by the library, the ledger and the
    owner, so skipping a recording only brings the same rows back. A sample drawn with another
    seed, over part of the library or with other repeat-census settings is a different draw, and
    choosing among such draws would be choosing the rows to answer."""
    reasons = []
    if header["partial"]:
        reasons.append(
            "it covers only part of the library (a record limit or the deadline cut the scan)"
        )
    if header["seed"] != SAMPLE_SEED:
        reasons.append(f"it was drawn with another seed ({header['seed']!r})")
    if (header["repeat_min_chars"], header["repeat_min_occurrences"]) != (
        DEFAULT_REPEAT_MIN_CHARS,
        DEFAULT_REPEAT_MIN_OCCURRENCES,
    ):
        reasons.append("it was drawn with other repeat-census settings")
    return reasons


def sample_text(header: Mapping[str, Any], rows: Iterable[Mapping[str, Any]]) -> str:
    """The sample as JSONL: the header, then one row per line."""
    return "".join(
        json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in (header, *rows)
    )


@dataclass(frozen=True)
class ReviewSample:
    """A review-sample file that passed every integrity check."""

    header: dict[str, Any]
    rows: list[dict[str, Any]]


def _stale_sample(what: str) -> ValueError:
    return ValueError(
        f"sample {what}: drawn under another rules version or schema than the current "
        f"{RULES_VERSION}; draw a new sample"
    )


def _check_sample_row(row: Any, number: int) -> dict[str, Any]:
    if not isinstance(row, dict) or row.get("schema") != SAMPLE_SCHEMA:
        raise _stale_sample(f"line {number}")
    if set(row) != _SAMPLE_ROW_KEYS:
        raise ValueError(
            f"sample line {number}: row keys must be exactly {sorted(_SAMPLE_ROW_KEYS)}"
        )
    if row["rules_version"] != RULES_VERSION:
        raise _stale_sample(f"line {number}")
    problems = []
    if not isinstance(row["span_id"], str) or not _SPAN_ID.fullmatch(row["span_id"]):
        problems.append("span_id")
    if row["rule"] not in RULE_BY_ID or row["claim"] != RULE_BY_ID[row["rule"]].author:
        problems.append("rule")
    if row["stratum"] not in STRATA:
        problems.append("stratum")
    if not isinstance(row["text_sha256"], str) or not _SHA256.fullmatch(row["text_sha256"]):
        problems.append("text_sha256")
    if not isinstance(row["text"], str) or not row["text"]:
        problems.append("text")
    length = row["length"]
    if isinstance(length, bool) or not isinstance(length, int) or length < 1:
        problems.append("length")
    if not isinstance(row["excerpted"], bool) or not isinstance(row["partial"], bool):
        problems.append("excerpted/partial")
    if problems:
        raise ValueError(f"sample line {number}: bad {', '.join(problems)}")
    if not row["excerpted"] and (
        row["length"] != len(row["text"]) or text_digest(row["text"]) != row["text_sha256"]
    ):
        raise ValueError(f"sample line {number}: the text does not match its length and hash")
    if row["excerpted"] and row["length"] <= EXCERPT_HEAD + EXCERPT_TAIL:
        raise ValueError(f"sample line {number}: an excerpt of a span that short")
    return row


def parse_sample(lines: Iterable[str]) -> ReviewSample:
    """Read a review-sample file and refuse it unless it is exactly what `ReviewSampler` drew:
    a header whose digest, row count and per-stratum counts match the rows, rows of the current
    schema and rules version with the hash of the full text each shows, and no repeated span.
    A tampered or hand-written sample (which cannot have a valid header) is refused."""
    numbered = [(number, line) for number, line in enumerate(lines, start=1) if line.strip()]
    if not numbered:
        raise ValueError("sample is empty: it has no header")
    first_number, first_line = numbered[0]
    header = load_json_line(first_line, "sample", first_number)
    if not isinstance(header, dict) or header.get("schema") != SAMPLE_HEADER_SCHEMA:
        raise _stale_sample("has no header")
    if set(header) != _SAMPLE_HEADER_KEYS:
        raise ValueError(f"sample header keys must be exactly {sorted(_SAMPLE_HEADER_KEYS)}")
    if header["rules_version"] != RULES_VERSION:
        raise _stale_sample("header")
    rows = [
        _check_sample_row(load_json_line(line, "sample", number), number)
        for number, line in numbered[1:]
    ]
    problems = []
    if header["rows"] != len(rows):
        problems.append(f"the header counts {header['rows']} rows but the file holds {len(rows)}")
    if len({row["span_id"] for row in rows}) != len(rows):
        problems.append("a span appears twice")
    if header["sample_digest"] != sample_digest(header, rows):
        problems.append("the header and rows do not match the header's digest")
    if header["drawn"] != drawn_counts(rows):
        problems.append("the per-stratum counts do not match the rows")
    if header["partial"] is not all(row["partial"] for row in rows) and rows:
        problems.append("the partial flags disagree")
    for key in ("library", "owner_id"):
        if not isinstance(header[key], str) or not header[key].strip():
            problems.append(f"the header needs a {key}")
    for key in ("repeat_min_chars", "repeat_min_occurrences"):
        if isinstance(header[key], bool) or not isinstance(header[key], int) or header[key] < 1:
            problems.append(f"the header needs a positive {key}")
    if not isinstance(header["exclusion_digest"], str) or not _SHA256.fullmatch(
        header["exclusion_digest"]
    ):
        problems.append("the header needs an exclusion_digest (64 lowercase hexadecimal digits)")
    if problems:
        raise ValueError(f"sample was altered or is not a sample: {'; '.join(problems)}")
    return ReviewSample(header=dict(header), rows=rows)
