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

import functools
import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from .archive_provenance import INJECTED_TAGS, classify_user_turn
from .archive_read_library import FAMILIES
from .attribution import text_digest

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
    return RecordInfo(
        provider=provider,
        kind=kind,
        wing=wing,
        session_kind=str(extra.get("session_kind") or ""),
        authored_family=authored,
        agent_origin=origin,
    )


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
    for window in (text[:4000], text[-2000:]):
        for name, pattern in _TEXT_MARKERS:
            if pattern.search(window):
                return name
    return _frontmatter_marker(text)


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
_TAG_OPEN = re.compile(r"<([A-Za-z_][\w:.-]{0,40})((?:\s[^<>]*)?)(/?)>")


@functools.lru_cache(maxsize=256)
def _closing_tag(name: str) -> re.Pattern[str]:
    return re.compile(rf"</{re.escape(name)}\s*>", re.IGNORECASE)


def _is_wrapper(name: str) -> bool:
    lowered = name.lower()
    return lowered in WRAPPER_TAGS or lowered.startswith("ide_")


def _tag_blocks(text: str, wanted: Callable[[str], bool]) -> list[Claim]:
    """Blocks `<name ...>...</name>` for wanted names; an unclosed block runs to the end."""
    claims: list[Claim] = []
    position = 0
    while (match := _TAG_OPEN.search(text, position)) is not None:
        name = match.group(1)
        if not wanted(name):
            position = match.end()
            continue
        if match.group(0).endswith("/>"):
            end = match.end()
        else:
            close = _closing_tag(name).search(text, match.end())
            end = close.end() if close else len(text)
        claims.append((match.start(), end, f"tag={name.lower()}"))
        position = end
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
    Rule("typed_prefix", OWNER, "the characters the person typed", rule_typed_prefix),
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
    )
    digest = text_digest(text)
    free: list[Range] = [(0, length)]
    taken: list[tuple[int, int, str, str]] = []
    for rule in RULES:
        pieces = _intersect(free, _normalize(rule.claims(context), length))
        if rule.author == OWNER:
            pieces = [piece for piece in pieces if text[piece[0] : piece[1]].strip()]
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
