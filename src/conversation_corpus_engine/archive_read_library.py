"""Bridge from a knowledge-library generation to the archive: one atom, every family.

`cce library build` is the capture pipeline. It reads the coding CLIs' native session stores,
the notes and Drive captures, the document roots and the recovery folders, merges what several
captures say about one identity (a fuller body never loses to a thinner one), and publishes a
verified generation. `cce archive` is the presentation pipeline: bounded shards, two author
containers, a vault, a site and a turn store. This module joins them without re-reading a single
original: it streams the records of a published generation and hands each one to the archive
ingest as the same `Conversation` the web-export readers produce, so a coding session, a note, a
Drive document and a chat thread all land as turns with an author, a provider (family), a source
id, an ordinal and a timestamp.

What a family is: a provider word of the archive plus a selector over the library's records.
Most families are a library provider as it stands (`claudecode`, `codex`, `opencode`,
`antigravity`, `geminicli`, `copilotcli`, `applenotes`, `googledrive`). The library keeps every
document under one provider, so its records are split by provenance: authored files stay
`documents` (the person wrote them), agent memory trees become `agentmemory` and agent plans,
Codex outputs and library plans become `agentplans` (an agent wrote them). The ChatGPT pages the
library holds beyond the account export are the one supplement to a web provider.

Authorship is decided here, per turn, from provenance:

* a coding CLI's user turns go through `archive_provenance`: a subagent prompt or text the
  harness injected is machine, what the person typed is human;
* an authored document is the person's own text (channel `written`);
* notes and Drive documents already arrive as typed user turns;
* agent memory and agent plans keep the channels the library gave them and are machine.

The counts the library publishes for each family are read alongside the records and stored in
the reader counters, so `verify` can hold the ingest to the library's own manifest.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .archive_author import HUMAN_CHANNELS
from .archive_model import Conversation, SourceTracker
from .archive_provenance import BASIS_KEY, reclassify_typed_turns

READER = "archive_read_library@1"
POINTER = "current.json"
DATABASE = "library.sqlite"
WRITTEN = "written"


@dataclass(frozen=True)
class Family:
    """One archive provider word and the library records it is made of."""

    name: str
    library_provider: str
    display: str
    wing: str | None = None
    kinds: frozenset[str] | None = None
    # The conversation kind written to the archive; empty keeps the library's kind.
    kind: str = ""
    # A coding CLI: its typed user turns are reclassified by provenance.
    cli: bool = False
    # The text is the person's own writing (channel `written`).
    authored: bool = False

    def selects(self, row: dict[str, Any]) -> bool:
        if row["provider"] != self.library_provider:
            return False
        if self.wing is not None and row["wing"] != self.wing:
            return False
        return self.kinds is None or row["kind"] in self.kinds


FAMILIES: tuple[Family, ...] = (
    Family("claudecode", "claudecode", "Claude Code", kind="session", cli=True),
    Family("codex", "codex", "Codex", kind="session", cli=True),
    Family("opencode", "opencode", "OpenCode", kind="session", cli=True),
    Family("antigravity", "antigravity", "Antigravity", kind="session", cli=True),
    Family("geminicli", "geminicli", "Gemini CLI", kind="session", cli=True),
    Family("copilotcli", "copilotcli", "Copilot CLI", kind="session", cli=True),
    # A note's body arrives as typed turns and its table cells as `project` turns; both are the
    # person's own writing, so every user turn of the family is human.
    Family("applenotes", "applenotes", "Apple Notes", kind="note", authored=True),
    Family("googledrive", "googledrive", "Google Drive", kind="document", authored=True),
    Family(
        "documents",
        "documents",
        "Documents",
        wing="authored",
        kind="document",
        authored=True,
    ),
    Family(
        "agentmemory",
        "documents",
        "Agent memory",
        wing="ai",
        kinds=frozenset({"memory"}),
        kind="memory",
    ),
    Family(
        "agentplans",
        "documents",
        "Agent plans",
        wing="ai",
        kinds=frozenset({"plan", "artifact"}),
    ),
)
# The ChatGPT pages the library holds beyond the account export: not a provider of their own.
CHATGPT_PAGES = Family("chatgpt", "chatgpt", "ChatGPT", kinds=frozenset({"page"}), kind="page")
FAMILY_BY_NAME: dict[str, Family] = {family.name: family for family in FAMILIES}


class LibraryNotFound(FileNotFoundError):
    """No published generation at the given root."""


def locate(root: Path) -> tuple[Path, Path | None]:
    """(database, pointer) of a library root: a store folder, a `library-all` folder or a generation.

    A folder holding `current.json` resolves through it to the published generation; a folder
    holding a store's `library-all` does the same one level down; a generation folder is its own
    database. The pointer is None for a bare generation.
    """
    for pointer in (root / POINTER, root / "library-all" / POINTER):
        if pointer.is_file():
            current = json.loads(pointer.read_text(encoding="utf-8"))
            database = pointer.parent / str(current["generation"]) / DATABASE
            if database.is_file():
                return database, pointer
            raise LibraryNotFound(f"published generation is missing: {database}")
    if (root / DATABASE).is_file():
        return root / DATABASE, None
    raise LibraryNotFound(f"no knowledge library generation under {root}")


def detect(root: Path) -> bool:
    try:
        locate(root)
    except (LibraryNotFound, OSError, ValueError, KeyError):
        return False
    return True


def _open(database: Path) -> Any:
    # Imported here: the library store reaches the archive ingest, which reaches this module.
    from .library_store import Library  # noqa: PLC0415

    return Library(database)


def _rows(library: Any, family: Family) -> list[dict[str, Any]]:
    return [row for row in library.rows() if family.selects(row)]


# Library providers whose records the web exports already cover (and the archive reads from the
# exports, not the library): a record of these is never an orphan.
WEB_EXPORT_PROVIDERS: frozenset[str] = frozenset(
    {"chatgpt", "claude", "gemini", "grok", "notebooklm", "perplexity"}
)


def _claimed(row: dict[str, Any]) -> bool:
    return (
        row["provider"] in WEB_EXPORT_PROVIDERS
        or CHATGPT_PAGES.selects(row)
        or any(family.selects(row) for family in FAMILIES)
    )


def unclaimed(root: Path) -> int:
    """Library records that no family reads and no web export covers.

    A library can hold a record the family table has no selector for (a document source with a
    wing or kind of its own, a provider this table never learned). Counting them is what keeps
    such a record from being dropped without a trace: `verify` fails while the count is not 0.
    """
    database, _ = locate(root)
    with _open(database) as library:
        return sum(1 for row in library.rows() if not _claimed(row))


def expected(root: Path, family: Family) -> dict[str, int]:
    """What the library itself says the family holds: records, messages and empty records."""
    database, _ = locate(root)
    with _open(database) as library:
        rows = _rows(library, family)
    return {
        "records": len(rows),
        "messages": sum(int(row["message_count"]) for row in rows),
        "empty_records": sum(1 for row in rows if not row["message_count"]),
    }


def describe(root: Path, family: Family) -> dict[str, list[str]]:
    """The manifest's account of what a family read and what it left alone."""
    database, pointer = locate(root)
    counts = expected(root, family)
    read = [
        f"{counts['records']} library record(s), {counts['messages']} message(s)"
        f" of provider {family.library_provider}"
        + (f", wing {family.wing}" if family.wing else "")
        + (f", kind {'/'.join(sorted(family.kinds))}" if family.kinds else "")
    ]
    if pointer is not None:
        current = json.loads(pointer.read_text(encoding="utf-8"))
        for key in ("generation", "record_fingerprint", "evidence_fingerprint", "report_sha256"):
            read.append(f"library {key}: {current.get(key)}")
    else:
        read.append(f"library database: {database.parent.name}")
    orphans = unclaimed(root)
    not_read = (
        [f"{orphans} library record(s) belong to no family and are not read by any provider"]
        if orphans
        else []
    )
    return {"read": read, "not_read": not_read}


def _convert(row: dict[str, Any], payload: dict[str, Any], family: Family) -> Conversation:
    conversation = Conversation.from_dict(payload)
    conversation.provider = family.name
    if family.kind:
        conversation.kind = family.kind
    captured = dict(conversation.provenance)
    conversation.provenance = {
        **captured,
        "bridge_reader": READER,
        "library_uid": row["uid"],
        "library_version": row["current_version"],
        "library_digest": row["current_digest"],
    }
    conversation.extra["library"] = {
        "wing": row["wing"],
        "lane": row["lane"],
        "kind": row["kind"],
        "body_status": row["body_status"],
    }
    if family.authored:
        for message in conversation.messages:
            # A turn already on a human channel stays as the library wrote it.
            if message.role == "user" and message.channel not in HUMAN_CHANNELS:
                message.channel = WRITTEN
                message.typed_chars = len(message.text)
                message.meta[BASIS_KEY] = f"authored:{family.kind}"
    return conversation


def iter_family(root: Path, tracker: SourceTracker, family: Family) -> Iterator[Conversation]:
    """Stream a family's records, one conversation at a time, in uid order.

    Reader counters carry the library's own counts for the family (`library_*`) beside what was
    read (`bridge_*`), and the basis of every coding-CLI user turn (`basis:<author>:<basis>`).
    """
    database, pointer = locate(root)
    if pointer is not None:
        tracker.provenance(pointer)
    counts = expected(root, family)
    tracker.note("library_records", counts["records"])
    tracker.note("library_messages", counts["messages"])
    tracker.note("library_empty_records", counts["empty_records"])
    tracker.note("library_unclaimed", unclaimed(root))
    with _open(database) as library:
        for row in _rows(library, family):
            payload = library.record(row["uid"])
            conversation = _convert(row, payload, family)
            del payload
            if family.cli:
                for key, amount in reclassify_typed_turns(conversation).items():
                    tracker.note(f"basis:{key}", amount)
            tracker.note("bridge_records")
            tracker.note("bridge_messages", len(conversation.messages))
            yield conversation


@dataclass(frozen=True)
class FamilyReader:
    """The reader contract the archive ingest expects of a provider module."""

    family: Family

    @property
    def READER(self) -> str:  # noqa: N802 - the contract spells the constant in capitals
        return READER

    def detect(self, root: Path) -> bool:
        return detect(root)

    def channels(self, root: Path) -> dict[str, list[str]]:
        return describe(root, self.family)

    def iter_conversations(self, root: Path, tracker: SourceTracker) -> Iterator[Conversation]:
        return iter_family(root, tracker, self.family)


def reader_for(name: str) -> FamilyReader:
    return FamilyReader(FAMILY_BY_NAME[name])


def counter_report(counters: dict[str, int]) -> dict[str, dict[str, int]]:
    """The basis counters of a provider manifest grouped by author: {human: {...}, machine: {...}}."""
    grouped: dict[str, Counter[str]] = {"human": Counter(), "machine": Counter()}
    for key, value in counters.items():
        if key.startswith("basis:"):
            author, _, basis = key[len("basis:") :].partition(":")
            grouped.setdefault(author, Counter())[basis] += value
    return {author: dict(sorted(items.items())) for author, items in grouped.items()}
