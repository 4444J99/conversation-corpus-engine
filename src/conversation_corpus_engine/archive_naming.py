"""The archive naming contract: one pure function mints every name the archive layer writes.

A name is a kind word followed by up to four more segments, joined by single hyphens:

    name = kind [-scope] [-subject] [-identity] [-date]

* kind: one word from the closed table in KIND_TABLE. The table says, per kind, which
  segments are required, optional or forbidden. Adding a kind bumps NAMING_VERSION;
  removing or renaming one is forbidden.
* scope: a provider word (see PROVIDER_SCOPES) or the word `all`.
* subject: lowercase words `[a-z0-9]+` joined by hyphens, at most 32 characters. A subject is
  validated, never transformed: a title, prompt text or free text is not a subject.
* identity: for kind `conversation`, the frozen `archive_model.stable_uid` tail exactly as
  stable_uid produces it. For every other kind, either a plain vendor identifier used as-is,
  or the first 16 hex characters of SHA-256 over the exact identity string as given (no
  normalization before hashing). Kinds listed with the `hash` form always hash.
* date: eight digits naming a real calendar day, taken from the data and never from the
  clock. It is present only on kinds that require it; living artifacts carry no date.

The result matches `^[a-z0-9]+(-[a-z0-9]+)*$` and is at most 80 characters. A name over the
limit is an error, never truncated. File extensions are added by the caller and are not part
of the name.

The grammar is unambiguous by construction: scope words are a closed list, a date is always
the last token of a kind that requires one, and a kind that allows both a subject and an
identity requires the identity to be a 16 hex hash in the last position. `derive_name` re-parses every name it mints
before returning it, and `parse_name(derive_name(...))` always gives back the segments the name
holds. Those are the resolved segments, which equal the inputs only where nothing was hashed. For
a hashed identity (the `receipt` and `pattern` kinds, and any other kind given an identity that
is not plain) `parse_name` returns the 16 hex segment, not the identity string that was hashed:
the hash is one way, and feeding the segment back to `derive_name` hashes it again.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

NAMING_VERSION = 1

MAX_NAME_LENGTH = 80
MAX_SUBJECT_LENGTH = 32
MAX_PLAIN_IDENTITY_LENGTH = 64
HASH_LENGTH = 16

REQUIRED = "required"
OPTIONAL = "optional"
FORBIDDEN = "forbidden"

SCOPE_ALL = "all"
# Every provider the engine knows: the provider catalog plus archive-only readers. A test holds
# this list to be a superset of both, so adding a provider forces a conscious edit here.
PROVIDER_SCOPES: tuple[str, ...] = (
    "chatgpt",
    "claude",
    "copilot",
    "deepseek",
    "gemini",
    "grok",
    "mistral",
    "notebooklm",
    "perplexity",
)
SCOPES: tuple[str, ...] = (*PROVIDER_SCOPES, SCOPE_ALL)

# Directory names that organize a layout without being minted names (vault folders).
STRUCTURAL_NAMES: frozenset[str] = frozenset({"conversations", "hubs", "meta"})

_NAME_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
_SUBJECT_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
_PLAIN_IDENTITY_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
_HEX_RE = re.compile(r"[0-9a-f]{16}")
_DATE_RE = re.compile(r"[0-9]{8}")
_MONTH_RE = re.compile(r"[0-9]{6}")

# How an identity is resolved into its segment.
IDENTITY_TAIL = "tail"  # used exactly as given (the stable_uid tail); never hashed
IDENTITY_ANY = "any"  # plain identifiers as-is, anything else hashed
IDENTITY_HASH = "hash"  # always hashed

# Which scope words a kind accepts.
SCOPE_ANY = "any"
SCOPE_PROVIDER = "provider"
SCOPE_ONLY_ALL = "all"


class NamingError(ValueError):
    """Base class for every naming failure."""


class InvalidName(NamingError):
    def __init__(self, subject: str, problems: list[str]) -> None:
        self.subject = subject
        self.problems = list(problems)
        super().__init__(f"{subject}: " + "; ".join(self.problems))


class NameCollision(NamingError):
    def __init__(self, name: str, existing_identity: str, identity: str) -> None:
        self.name = name
        super().__init__(
            f"name {name!r} is already taken by a different identity "
            f"(existing {existing_identity!r}, new {identity!r})"
        )


@dataclass(frozen=True)
class KindRule:
    scope: str = REQUIRED
    scope_values: str = SCOPE_ANY
    subject: str = FORBIDDEN
    subject_max_words: int | None = None
    subject_month: bool = False
    identity: str = FORBIDDEN
    identity_form: str = IDENTITY_ANY
    date: str = FORBIDDEN
    note: str = ""

    @property
    def has_subject_and_identity(self) -> bool:
        return FORBIDDEN not in (self.identity, self.subject)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "scope_values": self.scope_values,
            "subject": self.subject,
            "subject_max_words": self.subject_max_words,
            "subject_month": self.subject_month,
            "identity": self.identity,
            "identity_form": self.identity_form if self.identity != FORBIDDEN else None,
            "date": self.date,
            "note": self.note,
        }


# The closed kind table. Order is part of the contract (it is printed in this order).
KIND_TABLE: dict[str, KindRule] = {
    "archive": KindRule(scope_values=SCOPE_ONLY_ALL, note="archive directory in the private store"),
    "conversation": KindRule(
        scope_values=SCOPE_PROVIDER,
        identity=REQUIRED,
        identity_form=IDENTITY_TAIL,
        note="provider plus the frozen stable_uid tail; reproduces today's uid exactly",
    ),
    "message": KindRule(
        scope_values=SCOPE_PROVIDER,
        identity=REQUIRED,
        note="stable message reference",
    ),
    "thread": KindRule(scope_values=SCOPE_PROVIDER, identity=REQUIRED),
    "family": KindRule(scope_values=SCOPE_PROVIDER, identity=REQUIRED),
    "pair": KindRule(scope_values=SCOPE_PROVIDER, identity=REQUIRED),
    "action": KindRule(scope_values=SCOPE_PROVIDER, identity=REQUIRED),
    "question": KindRule(scope_values=SCOPE_PROVIDER, identity=REQUIRED),
    "feed": KindRule(
        scope_values=SCOPE_PROVIDER, subject=REQUIRED, note="feed file stem; extension by caller"
    ),
    "receipt": KindRule(
        scope=OPTIONAL,
        subject=REQUIRED,
        identity=REQUIRED,
        identity_form=IDENTITY_HASH,
        date=REQUIRED,
        note="bound to a source fingerprint; date is the manifest latest-activity day",
    ),
    "corpus": KindRule(
        scope_values=SCOPE_PROVIDER,
        subject=REQUIRED,
        note="projected corpus directory = registry key = contract corpus_id",
    ),
    "index": KindRule(subject=REQUIRED),
    "pattern": KindRule(
        identity=REQUIRED,
        identity_form=IDENTITY_HASH,
        note="one recurring form; the identity hashes its lineage id, so the name never moves",
    ),
    "manifest": KindRule(
        scope_values=SCOPE_ONLY_ALL,
        subject=OPTIONAL,
        note="the archive manifest; with a subject, the manifest of one surface (manifest-all-vault)",
    ),
    "report": KindRule(subject=REQUIRED),
    "vault": KindRule(scope_values=SCOPE_ONLY_ALL),
    "site": KindRule(scope_values=SCOPE_ONLY_ALL),
    "graph": KindRule(scope_values=SCOPE_ONLY_ALL),
    "database": KindRule(scope_values=SCOPE_ONLY_ALL, subject=REQUIRED),
    "check": KindRule(scope=OPTIONAL, subject=REQUIRED, note="a predicate script"),
    "fixture": KindRule(subject=OPTIONAL),
    "provider": KindRule(scope_values=SCOPE_PROVIDER, note="vault hub note for one provider"),
    "month": KindRule(
        scope_values=SCOPE_ONLY_ALL,
        subject=REQUIRED,
        subject_month=True,
        note="vault hub note; subject is six digits yyyymm; carries no date segment",
    ),
    "topic": KindRule(
        scope_values=SCOPE_ONLY_ALL,
        subject=REQUIRED,
        subject_max_words=1,
        note="vault hub note; subject is one validated word",
    ),
    "collection": KindRule(scope_values=SCOPE_PROVIDER, subject=REQUIRED),
    "home": KindRule(scope_values=SCOPE_ONLY_ALL),
    "coverage": KindRule(scope_values=SCOPE_ONLY_ALL),
    "guide": KindRule(scope_values=SCOPE_ONLY_ALL),
}
KINDS: tuple[str, ...] = tuple(KIND_TABLE)


def _check_table() -> None:
    """Refuse to import a table that would make the grammar ambiguous."""
    for kind, rule in KIND_TABLE.items():
        if not _NAME_RE.fullmatch(kind) or "-" in kind:
            raise RuntimeError(f"kind {kind!r} must be one lowercase word")
        if rule.scope == FORBIDDEN:
            raise RuntimeError(f"kind {kind}: scope may not be forbidden")
        if rule.date == OPTIONAL:
            raise RuntimeError(f"kind {kind}: an optional date makes parsing ambiguous")
        if rule.identity == OPTIONAL:
            raise RuntimeError(f"kind {kind}: an optional identity makes parsing ambiguous")
        if rule.has_subject_and_identity and rule.identity_form != IDENTITY_HASH:
            raise RuntimeError(f"kind {kind}: subject plus identity needs the hash identity form")
        if rule.subject == OPTIONAL and rule.identity != FORBIDDEN:
            raise RuntimeError(f"kind {kind}: an optional subject beside an identity is ambiguous")
    for scope in SCOPES:
        if "-" in scope or not _NAME_RE.fullmatch(scope):
            raise RuntimeError(f"scope {scope!r} must be one lowercase word")
    if set(SCOPES) & set(KINDS):
        raise RuntimeError("a scope word may not also be a kind word")


_check_table()


@dataclass(frozen=True)
class NameParts:
    """The resolved segments of one name. Every field is the text that appears in the name."""

    kind: str
    scope: str | None = None
    subject: str | None = None
    identity: str | None = None
    date: str | None = None

    def render(self) -> str:
        return "-".join(
            segment
            for segment in (self.kind, self.scope, self.subject, self.identity, self.date)
            if segment is not None
        )

    def as_dict(self) -> dict[str, str | None]:
        return {
            "kind": self.kind,
            "scope": self.scope,
            "subject": self.subject,
            "identity": self.identity,
            "date": self.date,
        }


def hash_identity(identity: str) -> str:
    """First 16 hex characters of SHA-256 over the identity string exactly as given."""
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:HASH_LENGTH]


def is_plain_identity(identity: str) -> bool:
    return (
        len(identity) <= MAX_PLAIN_IDENTITY_LENGTH
        and _PLAIN_IDENTITY_RE.fullmatch(identity) is not None
    )


def normalize_date(value: Any) -> str:
    """Return an eight digit calendar day from a date, a datetime or an eight digit string."""
    if isinstance(value, _dt.date):
        text = value.strftime("%Y%m%d")
    elif isinstance(value, str):
        text = value
    else:
        raise InvalidName("date", [f"date must be a date or an eight digit string, got {value!r}"])
    if not _DATE_RE.fullmatch(text):
        raise InvalidName("date", [f"date must be eight digits, got {text!r}"])
    try:
        _dt.date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError as exc:
        raise InvalidName("date", [f"{text!r} is not a real calendar day"]) from exc
    return text


def day_of(timestamp: str) -> str:
    """Eight digit day from a normalized `YYYY-MM-DDTHH:MM:SSZ` (or `YYYY-MM-DD`) timestamp.

    The day always comes from data. There is no clock fallback: an empty or unreadable
    timestamp is an error.
    """
    text = (timestamp or "")[:10]
    if len(text) != 10 or text[4] != "-" or text[7] != "-":
        raise InvalidName("date", [f"no calendar day in timestamp {timestamp!r}"])
    return normalize_date(text.replace("-", ""))


def _check_parts(parts: NameParts, rule: KindRule) -> list[str]:
    problems: list[str] = []
    kind = parts.kind

    if parts.scope is None:
        if rule.scope == REQUIRED:
            problems.append(f"{kind}: scope is required")
    elif parts.scope not in SCOPES:
        problems.append(f"{kind}: scope {parts.scope!r} is not a known provider word or 'all'")
    elif rule.scope_values == SCOPE_PROVIDER and parts.scope == SCOPE_ALL:
        problems.append(f"{kind}: scope must be a provider, not 'all'")
    elif rule.scope_values == SCOPE_ONLY_ALL and parts.scope != SCOPE_ALL:
        problems.append(f"{kind}: scope must be 'all'")

    if parts.subject is None:
        if rule.subject == REQUIRED:
            problems.append(f"{kind}: subject is required")
    elif rule.subject == FORBIDDEN:
        problems.append(f"{kind}: subject is not allowed")
    else:
        subject = parts.subject
        if not _SUBJECT_RE.fullmatch(subject):
            problems.append(
                f"{kind}: subject {subject!r} must be lowercase words joined by hyphens"
            )
        else:
            if len(subject) > MAX_SUBJECT_LENGTH:
                problems.append(f"{kind}: subject is longer than {MAX_SUBJECT_LENGTH} characters")
            words = subject.split("-")
            if rule.subject_max_words is not None and len(words) > rule.subject_max_words:
                problems.append(f"{kind}: subject may have at most {rule.subject_max_words} word")
            if rule.subject_month and not _is_month(subject):
                problems.append(f"{kind}: subject must be a real month as six digits yyyymm")
            if parts.scope is None and words[0] in SCOPES:
                problems.append(
                    f"{kind}: without a scope the subject may not start with the scope word "
                    f"{words[0]!r}"
                )

    if parts.identity is None:
        if rule.identity == REQUIRED:
            problems.append(f"{kind}: identity is required")
    elif rule.identity == FORBIDDEN:
        problems.append(f"{kind}: identity is not allowed")
    elif rule.identity_form == IDENTITY_HASH:
        if not _HEX_RE.fullmatch(parts.identity):
            problems.append(f"{kind}: identity must be {HASH_LENGTH} lowercase hex characters")
    elif not is_plain_identity(parts.identity):
        problems.append(
            f"{kind}: identity {parts.identity!r} must be lowercase words joined by single "
            f"hyphens, at most {MAX_PLAIN_IDENTITY_LENGTH} characters"
        )

    if parts.date is None:
        if rule.date == REQUIRED:
            problems.append(f"{kind}: date is required")
    elif rule.date == FORBIDDEN:
        problems.append(f"{kind}: date is not allowed")
    else:
        try:
            normalize_date(parts.date)
        except InvalidName as exc:
            problems.extend(f"{kind}: {item}" for item in exc.problems)
    return problems


def _is_month(text: str) -> bool:
    if not _MONTH_RE.fullmatch(text):
        return False
    return 1 <= int(text[4:]) <= 12


def _parse(name: Any) -> tuple[NameParts | None, list[str]]:
    if not isinstance(name, str) or not name:
        return None, ["name must be a non-empty string"]
    problems: list[str] = []
    if len(name) > MAX_NAME_LENGTH:
        problems.append(f"name is {len(name)} characters, the limit is {MAX_NAME_LENGTH}")
    if not _NAME_RE.fullmatch(name):
        problems.append(
            "name must be lowercase ascii letters and digits in words joined by single hyphens"
        )
        return None, problems
    tokens = name.split("-")
    kind = tokens[0]
    rule = KIND_TABLE.get(kind)
    if rule is None:
        problems.append(f"{kind!r} is not a known kind")
        return None, problems
    rest = tokens[1:]

    scope: str | None = None
    if rest and rest[0] in SCOPES:
        scope = rest.pop(0)

    date: str | None = None
    if rule.date == REQUIRED:
        if rest and _DATE_RE.fullmatch(rest[-1]):
            date = rest.pop()
        else:
            problems.append(f"{kind}: the last segment must be an eight digit date")

    subject: str | None = None
    identity: str | None = None
    if rule.has_subject_and_identity:
        if rest and _HEX_RE.fullmatch(rest[-1]):
            identity = rest.pop()
        else:
            problems.append(f"{kind}: the identity must be {HASH_LENGTH} lowercase hex characters")
        subject = "-".join(rest) or None
    elif rule.identity != FORBIDDEN:
        identity = "-".join(rest) or None
    elif rule.subject != FORBIDDEN:
        subject = "-".join(rest) or None
    elif rest:
        problems.append(f"{kind}: unexpected segments {'-'.join(rest)!r}")

    parts = NameParts(kind=kind, scope=scope, subject=subject, identity=identity, date=date)
    problems.extend(problem for problem in _check_parts(parts, rule) if problem not in problems)
    return parts, problems


def validate_name(name: str) -> list[str]:
    """Problems with a name; an empty list means it is valid."""
    return _parse(name)[1]


def parse_name(name: str) -> NameParts:
    """Split a valid name into its segments; raise InvalidName when it is not valid."""
    parts, problems = _parse(name)
    if problems or parts is None:
        raise InvalidName(repr(name), problems)
    return parts


def derive_name(
    kind: str,
    scope: str | None = None,
    subject: str | None = None,
    identity: str | None = None,
    date: Any = None,
) -> str:
    """Mint one name. Pure: the same inputs always give the same name; no clock, no I/O."""
    problems: list[str] = []
    for label, value in (("kind", kind), ("scope", scope), ("subject", subject)):
        if value is not None and not isinstance(value, str):
            problems.append(f"{label} must be a string, got {value!r}")
    if identity is not None and not isinstance(identity, str):
        problems.append(f"identity must be a string, got {identity!r}")
    rule = KIND_TABLE.get(kind) if isinstance(kind, str) else None
    if rule is None:
        problems.append(f"{kind!r} is not a known kind")
    if problems or rule is None:
        raise InvalidName(repr(kind), problems)

    resolved_identity: str | None = None
    if identity is not None and rule.identity != FORBIDDEN:
        if rule.identity_form == IDENTITY_TAIL:
            resolved_identity = identity
        elif rule.identity_form == IDENTITY_HASH or not is_plain_identity(identity):
            resolved_identity = hash_identity(identity)
        else:
            resolved_identity = identity
    elif identity is not None:
        resolved_identity = identity  # forbidden: carried through so validation names it

    resolved_date: str | None = None
    if date is not None:
        try:
            resolved_date = normalize_date(date)
        except InvalidName as exc:
            problems.extend(f"{kind}: {item}" for item in exc.problems)

    parts = NameParts(
        kind=kind, scope=scope, subject=subject, identity=resolved_identity, date=resolved_date
    )
    problems.extend(problem for problem in _check_parts(parts, rule) if problem not in problems)
    name = parts.render()
    if len(name) > MAX_NAME_LENGTH:
        problems.append(f"name is {len(name)} characters, the limit is {MAX_NAME_LENGTH}")
    if problems:
        raise InvalidName(name, problems)
    reparsed, reparse_problems = _parse(name)
    if reparse_problems or reparsed != parts:
        raise InvalidName(name, ["name does not parse back to its own segments", *reparse_problems])
    return name


def conversation_name(provider: str, uid: str) -> str:
    """Name for a conversation, from its `stable_uid`: `conversation-<uid>`.

    The uid is `<provider>-<tail>`; the tail is carried exactly, so existing uids are
    reproduced byte for byte. Raises InvalidName for a uid that cannot be expressed as a
    valid name (an edge or doubled hyphen in a plain vendor id, or a name over 80 characters).
    """
    prefix = f"{provider}-"
    if not uid.startswith(prefix):
        raise InvalidName(repr(uid), [f"uid does not start with {prefix!r}"])
    return derive_name("conversation", scope=provider, identity=uid[len(prefix) :])


def conversation_uid(name: str) -> str:
    """Inverse of `conversation_name`: the `stable_uid` a conversation name stands for."""
    parts = parse_name(name)
    if parts.kind != "conversation" or parts.scope is None or parts.identity is None:
        raise InvalidName(repr(name), ["not a conversation name"])
    return f"{parts.scope}-{parts.identity}"


class NameLedger:
    """Remember which identity owns each name; case-folded, so a clash on a
    case-insensitive filesystem is caught here.

    Registering the same name for the same identity is a harmless rerun. Registering it for a
    different identity raises NameCollision.
    """

    def __init__(self) -> None:
        self._owners: dict[str, tuple[str, str]] = {}

    def register(self, name: str, identity: str) -> bool:
        """Claim `name` for `identity`. True when newly claimed, False for a harmless rerun."""
        if not isinstance(name, str) or not name:
            raise InvalidName(repr(name), ["name must be a non-empty string"])
        if not isinstance(identity, str):
            raise InvalidName(repr(name), ["identity must be a string"])
        key = name.casefold()
        existing = self._owners.get(key)
        if existing is None:
            self._owners[key] = (name, identity)
            return True
        if existing[1] == identity:
            return False
        raise NameCollision(name, existing[1], identity)

    def identity_of(self, name: str) -> str | None:
        existing = self._owners.get(name.casefold())
        return existing[1] if existing else None

    def names(self) -> list[str]:
        return sorted(name for name, _ in self._owners.values())

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name.casefold() in self._owners

    def __len__(self) -> int:
        return len(self._owners)


def names_payload() -> dict[str, Any]:
    """The contract as data: what `cce archive names --json` prints."""
    return {
        "naming_version": NAMING_VERSION,
        "grammar": "kind [-scope] [-subject] [-identity] [-date]",
        "pattern": _NAME_RE.pattern,
        "max_name_length": MAX_NAME_LENGTH,
        "max_subject_length": MAX_SUBJECT_LENGTH,
        "max_plain_identity_length": MAX_PLAIN_IDENTITY_LENGTH,
        "hash_length": HASH_LENGTH,
        "providers": list(PROVIDER_SCOPES),
        "scopes": list(SCOPES),
        "structural_names": sorted(STRUCTURAL_NAMES),
        "kinds": [{"kind": kind, **rule.as_dict()} for kind, rule in KIND_TABLE.items()],
    }


def render_names_text() -> str:
    lines = [
        f"naming version {NAMING_VERSION}: kind [-scope] [-subject] [-identity] [-date]",
        f"scopes: {', '.join(SCOPES)}",
        "kind          scope     subject    identity  date",
    ]
    for kind, rule in KIND_TABLE.items():
        lines.append(
            f"{kind:<13} {rule.scope:<9} {rule.subject:<10} {rule.identity:<9} {rule.date}"
        )
    return "\n".join(lines)


def _name_stem(entry: str) -> str:
    """The minted part of a file name: everything before the first dot."""
    return entry.split(".", 1)[0]


def verify_names(root: Path, structural: Iterable[str] = STRUCTURAL_NAMES) -> dict[str, Any]:
    """Check every file and directory under `root` against the naming contract.

    Each entry must be lowercase, and its stem (the text before the first dot) must either be a
    structural name or parse as a valid minted name. Dot-prefixed entries (atomic-write
    temporaries) are skipped. The root itself is not checked. Symbolic links are checked by
    name and never followed.
    """
    allowed = frozenset(structural)
    problems: list[str] = []
    checked = 0

    def walk(directory: Path, prefix: str) -> None:
        nonlocal checked
        for entry in sorted(directory.iterdir(), key=lambda item: item.name):
            if entry.name.startswith("."):
                continue
            relative = f"{prefix}{entry.name}"
            checked += 1
            if entry.name != entry.name.lower():
                problems.append(f"{relative}: not lowercase")
            stem = _name_stem(entry.name).lower()
            if stem not in allowed:
                problems.extend(f"{relative}: {problem}" for problem in validate_name(stem))
            if entry.is_dir() and not entry.is_symlink():
                walk(entry, f"{relative}/")

    if not root.is_dir():
        raise NotADirectoryError(str(root))
    walk(root, "")
    return {
        "ok": not problems,
        "naming_version": NAMING_VERSION,
        "checked": checked,
        "problems": problems,
    }
