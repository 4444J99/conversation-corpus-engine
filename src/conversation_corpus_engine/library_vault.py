"""Capped reading vaults for the library: the ``cards`` and ``full`` profiles.

The legacy export writes one note per record and one per repeated passage into flat folders. On a
large library that is hundreds of thousands of notes in one folder and notes of many megabytes,
which an editor that indexes a vault at start-up cannot open. These profiles keep every promise of
the Wiki Standard caps (see ``library_vault_gate``) by construction:

* ``cards`` is a small curated vault: one short card per record (title, facets, a bounded excerpt
  and the exact ``cce library read`` command; the body is never copied), most recently updated
  first, limited to a note budget. What does not fit is stated on the Home note, never dropped
  silently.
* ``full`` keeps every record's whole text, split at message boundaries (and inside a message at
  line boundaries, fence-aware, nothing cut) into linked parts of about 12 KiB. There are no
  per-block notes: repeated passages are shown inline and stay in the library database. It refuses,
  before writing anything, when the result would pass a hard cap.

Folders are planned with the archive's deterministic hash-bucket plan so none passes the folder
line, and every hub is paginated at the hub entry cap. Nothing here writes a file: a plan renders
``(path, text)`` pairs and the caller decides where they go.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .archive_shard import plan_flat_folders
from .archive_split import pack, paginate_rows, split_text, utf8_size
from .library_blocks import render_blocks, split_blocks
from .library_note_text import (
    EVIDENCE,
    FACETS,
    WINGS,
    check,
    clip,
    facet_stem,
    inert_lines,
    plain,
    safe_uid,
)
from .library_store import Library, canonical_json
from .library_vault_gate import (
    DEFAULT_CAPS,
    VaultCaps,
    hard_cap_problems,
    measure_planned,
)

PROFILE_NONE = "none"
PROFILE_CARDS = "cards"
PROFILE_FULL = "full"
VAULT_PROFILES = (PROFILE_NONE, PROFILE_CARDS, PROFILE_FULL)
INDEXED_FACETS = ("lane", "kind", "provider")
# Home, the two wing hubs and the three facet index hubs exist even for an empty library.
HUB_FLOOR = 1 + len(WINGS) + len(INDEXED_FACETS)
# Room kept in a note for its header, its navigation and the escaping of metadata.
HEADER_RESERVE = 4096
HUB_RESERVE = 2048
CONTINUATION_RESERVE = 128
CONTINUATION = "\n(continued from the previous part)\n\n"
TITLE_CHARS = 200
LABEL_CHARS = 120
FACET_CHARS = 80
EXCERPT_CHARS = 400
SOURCE_CHARS = 300
URI_CHARS = 1024
# A record's header lists this many parents; the rest are counted, not listed.
MAX_PARENTS = 20
# Page paths are measured as if two bucket folders deep, the deepest the plan can need here.
NOMINAL_FOLDERS = "bucket-all-b000/bucket-all-b000"


FULL_ADVICE = (
    "Use --vault-profile cards for a capped card vault (for a published generation, "
    "`cce library export --vault-profile cards` or `cce library export-cards` writes one), "
    "or --vault-profile none"
)
CARDS_ADVICE = "Lower --limit-notes, or use --vault-profile none"


class VaultRefused(ValueError):
    """The vault would pass a hard cap or a limit that cannot hold it; nothing is written."""


def _refusal(profile: str, what: str, caps: VaultCaps) -> VaultRefused:
    return VaultRefused(
        f"the {profile} vault would pass the vault hard cap ({what}; the caps are "
        f"{caps.vault_notes_fail} notes and {caps.vault_bytes_fail} bytes). Nothing was written. "
        f"{FULL_ADVICE}"
    )


def _join(*parts: str) -> str:
    return "/".join(part for part in parts if part)


def _href(source: str, target: str) -> str:
    """Relative link from the note at ``source`` to the file at ``target`` (both from one root).

    Purely lexical: both paths are relative to the vault's output root and contain no ``..``.
    """
    start = source.split("/")[:-1]
    parts = target.split("/")
    shared = 0
    while shared < min(len(start), len(parts) - 1) and start[shared] == parts[shared]:
        shared += 1
    return "/".join([".."] * (len(start) - shared) + parts[shared:])


@lru_cache(maxsize=65536)
def _row_label(title: str) -> str:
    """The inert, clipped link text for a record title (repeated for every facet it is in)."""
    return plain(clip(" ".join(title.split()), LABEL_CHARS)[0]) or "Untitled"


@dataclass(frozen=True)
class _Item:
    label: str
    target: str  # a path from the output root, or "hub:<page stem>"
    tail: str


@dataclass
class _Hub:
    stem: str
    title: str
    intro: str
    items: list[_Item]
    pages: list[list[_Item]]

    def page_stem(self, number: int) -> str:
        return self.stem if number == 1 else f"{self.stem}-p{number:03d}"


def _title(row: dict[str, Any]) -> str:
    return plain(clip(" ".join(str(row["title"]).split()), TITLE_CHARS)[0]) or "Untitled"


def _facet_groups(rows: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        for facet in FACETS:
            groups[facet, row[facet]].append(row)
    for wing in WINGS:
        groups.setdefault(("wing", wing), [])
    return groups


def _item_line(item: _Item, href: str) -> str:
    return f"- [{item.label}]({href}) - {item.tail}\n"


def _hubs(
    rows: list[dict[str, Any]], note_path: Callable[[dict[str, Any]], str], caps: VaultCaps
) -> list[_Hub]:
    """Every facet hub (one per value) and one index hub per open facet, each paginated."""
    groups = _facet_groups(rows)
    hubs: list[_Hub] = []
    for (facet, value), members in sorted(groups.items()):
        label = WINGS[value] if facet == "wing" else value
        items = [
            _Item(
                _row_label(str(row["title"])),
                note_path(row),
                f"{row['uid']} - {plain(row['body_status'])}",
            )
            for row in members
        ]
        hubs.append(
            _Hub(
                facet_stem(facet, value),
                plain(f"{facet.title()}: {clip(label, FACET_CHARS)[0]}"),
                f"{len(members)} records. Titles may repeat; stable identities distinguish them.",
                items,
                [],
            )
        )
    for facet in INDEXED_FACETS:
        values = sorted(value for member_facet, value in groups if member_facet == facet)
        items = [
            _Item(
                plain(clip(value, FACET_CHARS)[0]) or "Unspecified",
                "hub:" + facet_stem(facet, value),
                f"{len(groups[facet, value])} records",
            )
            for value in values
        ]
        hubs.append(
            _Hub(f"{facet}-index", f"{facet.title()} index", f"{len(values)} values.", items, [])
        )
    budget = max(512, caps.note_warn - HUB_RESERVE)
    for hub in hubs:
        hub.pages = _paginate(hub, budget, caps.hub_entries)
    return hubs


def _paginate(hub: _Hub, budget: int, entries: int) -> list[list[_Item]]:
    """Cut a hub into pages of at most ``entries`` items and ``budget`` bytes."""
    lines = [_item_line(item, "x/" * 4 + item.target) for item in hub.items]
    pages = paginate_rows([], lines, budget, budget, entries)
    out: list[list[_Item]] = []
    start = 0
    for page in pages:
        out.append(hub.items[start : start + len(page)])
        start += len(page)
    return out


def _page_count(hubs: list[_Hub]) -> int:
    return sum(len(hub.pages) for hub in hubs)


def _hub_paths(hubs: list[_Hub], prefix: str, caps: VaultCaps) -> dict[str, str]:
    stems = [hub.page_stem(number) for hub in hubs for number in range(1, len(hub.pages) + 1)]
    folders = plan_flat_folders(stems, caps.folder_warn)
    return {stem: _join(prefix + "hubs", folders[stem], stem + ".md") for stem in stems}


def _hub_notes(
    hubs: list[_Hub], hub_path: dict[str, str], home_path: str
) -> Iterator[tuple[str, str]]:
    for hub in hubs:
        total = len(hub.pages)
        for number, items in enumerate(hub.pages, 1):
            here = hub_path[hub.page_stem(number)]
            heading = f"# {hub.title}" + (f", page {number} of {total}" if total > 1 else "")
            out = [f"{heading}\n\n[Library home]({_href(here, home_path)})\n\n{hub.intro}\n\n"]
            if number > 1:
                out.append(
                    f"[Previous page]({_href(here, hub_path[hub.page_stem(number - 1)])})\n\n"
                )
            if number < total:
                out.append(f"[Next page]({_href(here, hub_path[hub.page_stem(number + 1)])})\n\n")
            for item in items:
                target = (
                    hub_path[item.target[4:]] if item.target.startswith("hub:") else item.target
                )
                out.append(_item_line(item, _href(here, target)))
            yield here, "".join(out)


def _meta_lines(row: dict[str, Any], here: str, hub_path: dict[str, str]) -> list[str]:
    lines = [
        f"- UID: {row['uid']}\n",
        f"- Version: {plain(row['current_version'])}\n",
        f"- Body coverage: {plain(row['body_status'])}\n",
    ]
    for facet in FACETS:
        label = WINGS.get(row[facet], row[facet]) if facet == "wing" else row[facet]
        href = _href(here, hub_path[facet_stem(facet, row[facet])])
        lines.append(f"- {facet.title()}: [{plain(clip(label, FACET_CHARS)[0])}]({href})\n")
    lines.append(
        f"- Created: {plain(row['created']) or 'Not recorded'}\n"
        f"- Updated: {plain(row['updated']) or 'Not recorded'}\n"
    )
    lines.append(f"- Normalized revision SHA-256: {plain(row['current_digest'])}\n")
    source = str(row["source_ref"])
    if source:
        if Path(source).is_absolute() and len(source) <= URI_CHARS:
            lines.append(f"- [Original source]({Path(source).as_uri()})\n")
        else:
            lines.append(f"- Source reference: {plain(clip(source, SOURCE_CHARS)[0])}\n")
    return lines


# --- full: whole records, split into linked parts ------------------------------------------


def _message_units(payload: dict[str, Any], check_callback: Callable[..., Any] | None) -> list[str]:
    """One unit per message (and per alternate), rendered as inert Markdown with inline passages."""
    units: list[str] = []
    messages = payload["messages"]
    if not messages:
        units.append(
            "\nNo source body was available. Metadata is retained and this record remains "
            "visible in coverage reports.\n"
        )
    for index, message in enumerate(messages):
        check(check_callback)
        unit = (
            f"\n## Message {index + 1}\n\nRole: {plain(message.get('role', ''))}; "
            f"channel: {plain(message.get('channel', '')) or 'Not recorded'}\n\n"
            + render_blocks(split_blocks(message["text"]), {})
            + "\n"
        )
        attachments = message.get("attachments", [])
        if attachments:
            unit += "\nAttachment metadata (original binaries remain at source):\n\n"
            unit += "".join("- " + plain(canonical_json(item)) + "\n" for item in attachments)
        units.append(unit)
    alternates = payload.get("extra", {}).get("alternate_messages", [])
    if isinstance(alternates, list) and alternates:
        units.append(
            "\n## Alternate source messages\n\nThese retained source branches were not part of "
            "the main message sequence.\n"
        )
        for index, message in enumerate(alternates):
            check(check_callback)
            if isinstance(message, dict):
                units.append(
                    f"\n### Alternate {index + 1}\n\n"
                    + render_blocks(split_blocks(str(message.get("text", ""))), {})
                    + "\n"
                )
    return units


def _fit_units(units: list[str], limit: int) -> list[str]:
    """Units that each fit ``limit`` bytes: a message longer than that is split, losslessly."""
    fitted: list[str] = []
    for unit in units:
        if utf8_size(unit) <= limit:
            fitted.append(unit)
            continue
        # The notes are CommonMark, where a fence indented one to three spaces, inside a list item
        # or not, must close and reopen at that indentation (a column-0 closer would end the item
        # and open a fence of its own). Other openers keep the column-0 lines; see archive_split.
        for index, chunk in enumerate(
            split_text(unit, limit - CONTINUATION_RESERVE, container_fences=True)
        ):
            fitted.append(chunk.text() if index == 0 else CONTINUATION + chunk.text())
    return fitted


def _record_bodies(
    payload: dict[str, Any], budget: int, check_callback: Callable[..., Any] | None
) -> list[str]:
    units = _fit_units(_message_units(payload, check_callback), budget - 1)
    return ["".join(group) for group in pack(units, budget, budget)]


def _nav(paths: list[str], index: int) -> str:
    here = paths[index]
    pieces = [f"Part {index + 1} of {len(paths)}"]
    if index > 0:
        pieces.append(f"[Previous part]({_href(here, paths[index - 1])})")
    if index + 1 < len(paths):
        pieces.append(f"[Next part]({_href(here, paths[index + 1])})")
    return " - ".join(pieces)


def _parent_links(
    library: Library, uid: str, record_path: dict[str, str]
) -> tuple[list[tuple[str, str | None]], int]:
    """The first MAX_PARENTS parents of a record (with their note, if in the vault) and the rest."""
    connection = library.connection
    total = connection.execute(
        "SELECT COUNT(*) FROM relations WHERE src=? AND kind='parent'", (uid,)
    ).fetchone()[0]
    shown = [
        (safe_uid(relation["dst"]), record_path.get(relation["dst"]))
        for relation in connection.execute(
            "SELECT dst FROM relations WHERE src=? AND kind='parent' ORDER BY dst LIMIT ?",
            (uid, MAX_PARENTS),
        )
    ]
    return shown, total - len(shown)


def _record_texts(
    row: dict[str, Any],
    bodies: list[str],
    paths: list[str],
    *,
    parents: list[tuple[str, str | None]],
    more_parents: int,
    hub_path: dict[str, str],
    home_path: str,
    entry_doc: str | None,
) -> list[str]:
    title = _title(row)
    total = len(bodies)
    texts: list[str] = []
    for index, body in enumerate(bodies):
        here = paths[index]
        nav = _nav(paths, index) if total > 1 else ""
        home = f"[Library home]({_href(here, home_path)})"
        if index == 0:
            head = [f"# {title}\n\n{EVIDENCE}\n\n{home}\n\n", *_meta_lines(row, here, hub_path)]
            for parent, parent_path in parents:
                if parent_path is None:
                    head.append(f"- Parent record: {parent} (not present in this vault)\n")
                else:
                    head.append(f"- [Parent record: {parent}]({_href(here, parent_path)})\n")
            if more_parents:
                head.append(
                    f"- {more_parents} more parent records are not listed here; "
                    "the library database and graph.jsonl hold every relation\n"
                )
            reader = (
                f"[agent entry]({_href(here, entry_doc)})"
                if entry_doc
                else "`cce library history` and `cce library read`"
            )
            head.append(
                "\nAll accepted normalized revisions remain in the library database. Use the "
                f"bounded history and read commands in {reader}.\n"
            )
            if nav:
                head.append(f"\n{nav}\n")
        else:
            first = _href(here, paths[0])
            head = [
                f"# {title} - part {index + 1} of {total}\n\n{EVIDENCE}\n\n{home} - "
                f"[Start of this record]({first})\n\n{nav}\n"
            ]
        texts.append("".join(head) + body + (f"\n{nav}\n" if nav else ""))
    return texts


# --- cards: one bounded note per record ----------------------------------------------------


def _card_text(
    row: dict[str, Any],
    payload: dict[str, Any],
    here: str,
    *,
    hub_path: dict[str, str],
    home_path: str,
    entry_doc: str | None,
) -> str:
    uid = row["uid"]
    out = [
        f"# {_title(row)}\n\n{EVIDENCE}\n\n[Library home]({_href(here, home_path)})\n\n",
        *_meta_lines(row, here, hub_path),
        f"- Messages: {row['message_count']}\n",
    ]
    messages = payload["messages"]
    if messages:
        first = messages[0]
        text, cut = clip(str(first.get("text", "")), EXCERPT_CHARS)
        shown = f"{len(text)} of {len(str(first.get('text', '')))} characters" if cut else "whole"
        quote = "\n".join("> " + line for line in (inert_lines(text).split("\n") or [""]))
        out.append(
            f"\n## Excerpt\n\nFirst message, role {plain(first.get('role', ''))} ({shown}):\n\n"
            f"{quote}\n"
        )
    else:
        out.append(
            "\n## Excerpt\n\nNo source body was available. Metadata is retained and this record "
            "remains visible in coverage reports.\n"
        )
    reader = (
        f"[Bounded agent entry]({_href(here, entry_doc)}) explains the commands.\n"
        if entry_doc
        else ""
    )
    out.append(
        "\n## Read the full record\n\nThis card does not copy the record. Read it with the "
        "bounded reader (STORE is the registered CCE store root):\n\n```sh\n"
        f'cce library read {uid} --store-root "$STORE" --message 0 --offset 0 --limit 12000\n'
        f'cce library history {uid} --store-root "$STORE" --offset 0 --limit 50\n```\n'
        f"{reader}"
    )
    return "".join(out)


def _recency(row: dict[str, Any]) -> str:
    return str(row["updated"] or row["created"] or "")


# --- the plan ------------------------------------------------------------------------------


@dataclass
class VaultPlan:
    """A vault fully rendered in memory and already measured; nothing is written.

    Every note exists as text, so the planning pass measures the real notes, folders and hub pages
    against all of the hard caps and refuses before a single file is written.
    """

    profile: str
    paths: dict[str, str | None]
    records: int
    cards: int | None
    truncated: bool
    omitted: int
    notes: int
    limit_notes: int | None
    caps: VaultCaps
    files: list[tuple[str, str]]
    gate: dict[str, Any]

    def write(self, emit: Callable[[str, str], None]) -> dict[str, int]:
        """Hand every note to ``emit``; the caps are re-checked on what is actually produced."""
        notes = size = 0
        for path, text in self.files:
            notes += 1
            size += utf8_size(text)
            if notes > self.caps.vault_notes_fail or size > self.caps.vault_bytes_fail:
                raise _refusal(self.profile, f"{notes} notes and {size} bytes so far", self.caps)
            emit(path, text)
        return {"notes": notes, "bytes": size}


def _gate_planned(
    profile: str, files: list[tuple[str, str]], prefix: str, caps: VaultCaps
) -> dict[str, Any]:
    """Measure the rendered notes against every hard cap; raise before anything is written."""
    report = measure_planned(dict(files), caps, root=prefix)
    if not report["ok"]:
        advice = FULL_ADVICE if profile == PROFILE_FULL else CARDS_ADVICE
        raise VaultRefused(
            f"the {profile} vault would break its hard caps ({'; '.join(hard_cap_problems(report))}). "
            f"Nothing was written. {advice}"
        )
    return report


def _home(
    lines: list[str],
    hub_path: dict[str, str],
    home_path: str,
    groups: dict[tuple[str, str], int],
    entry_doc: str | None,
    reading: str,
) -> str:
    out = ["# Knowledge library\n\n", EVIDENCE + "\n\n", *lines, "## Two wings\n\n"]
    for wing, label in WINGS.items():
        href = _href(home_path, hub_path[facet_stem("wing", wing)])
        out.append(f"- [{label}]({href}) - {groups.get(('wing', wing), 0)} records\n")
    out.append("\n## Browse\n\n")
    for facet in INDEXED_FACETS:
        href = _href(home_path, hub_path[f"{facet}-index"])
        count = sum(1 for member_facet, _ in groups if member_facet == facet)
        out.append(f"- [{facet.title()}s]({href}) - {count} values\n")
    entry = f"[Bounded agent retrieval]({_href(home_path, entry_doc)}). " if entry_doc else ""
    out.append(f"\n## Reading and reuse\n\n{entry}{reading}\n")
    return "".join(out)


def _placed(names: list[str], prefix: str, folder: str, caps: VaultCaps) -> dict[str, str]:
    folders = plan_flat_folders(names, caps.folder_warn)
    return {name: _join(prefix + folder, folders[name], name + ".md") for name in names}


def _plan_full(
    library: Library,
    rows: list[dict[str, Any]],
    prefix: str,
    entry_doc: str | None,
    caps: VaultCaps,
    check_callback: Callable[..., Any] | None,
) -> VaultPlan:
    floor = len(rows) + HUB_FLOOR
    if floor > caps.vault_notes_fail:
        raise _refusal(PROFILE_FULL, f"{len(rows)} records need at least {floor} notes", caps)
    budget = max(1024, caps.note_warn - HEADER_RESERVE)
    bodies: dict[str, list[str]] = {}
    extra = size = 0
    for row in rows:
        check(check_callback)
        parts = _record_bodies(library.record(row["uid"]), budget, check_callback)
        bodies[row["uid"]] = parts
        extra += len(parts) - 1
        size += sum(utf8_size(part) for part in parts)
        if floor + extra > caps.vault_notes_fail or size > caps.vault_bytes_fail:
            raise _refusal(
                PROFILE_FULL, f"at least {floor + extra} notes and {size} bytes of text", caps
            )
    record_path = _placed([row["uid"] for row in rows], prefix, "records", caps)
    part_names = [
        f"{uid}-p{number:03d}"
        for uid, parts in bodies.items()
        for number in range(2, len(parts) + 1)
    ]
    part_path = _placed(part_names, prefix, "parts", caps)
    hubs = _hubs(rows, lambda row: record_path[row["uid"]], caps)
    hub_path = _hub_paths(hubs, prefix, caps)
    notes = len(rows) + extra + _page_count(hubs) + 1
    if notes > caps.vault_notes_fail:
        raise _refusal(PROFILE_FULL, f"{notes} notes", caps)
    home_path = prefix + "Home.md"
    groups = {key: len(members) for key, members in _facet_groups(rows).items()}
    files: list[tuple[str, str]] = []
    for row in rows:
        uid = row["uid"]
        check(check_callback)
        parts = bodies.pop(uid)
        paths = [
            record_path[uid],
            *(part_path[f"{uid}-p{n:03d}"] for n in range(2, len(parts) + 1)),
        ]
        parents, more_parents = _parent_links(library, uid, record_path)
        texts = _record_texts(
            row,
            parts,
            paths,
            parents=parents,
            more_parents=more_parents,
            hub_path=hub_path,
            home_path=home_path,
            entry_doc=entry_doc,
        )
        files.extend(zip(paths, texts, strict=True))
    files.extend(_hub_notes(hubs, hub_path, home_path))
    files.append(
        (
            home_path,
            _home(
                [
                    f"{len(rows)} records in the full reading vault. Repeated passages are shown "
                    "inline in each record; the shared-passage index stays in the library "
                    "database. Original normalized revisions remain in the database and original "
                    "source files remain untouched.\n\n"
                ],
                hub_path,
                home_path,
                groups,
                entry_doc,
                "Long records are split into linked parts of about 12 KiB. Source bodies are "
                "inert Markdown evidence; generated navigation links are separate.",
            ),
        )
    )
    gate = _gate_planned(PROFILE_FULL, files, prefix, caps)
    return VaultPlan(
        PROFILE_FULL,
        {row["uid"]: record_path[row["uid"]] for row in rows},
        len(rows),
        None,
        False,
        0,
        len(files),
        None,
        caps,
        files,
        gate,
    )


def _plan_cards(
    library: Library,
    rows: list[dict[str, Any]],
    prefix: str,
    entry_doc: str | None,
    caps: VaultCaps,
    limit_notes: int | None,
    check_callback: Callable[..., Any] | None,
) -> VaultPlan:
    # By default a card vault stays under the standard's warning line, so it is also clean.
    limit = caps.vault_notes_warn if limit_notes is None else limit_notes
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise VaultRefused("limit_notes must be an integer number of notes")
    if limit > caps.vault_notes_fail:
        raise VaultRefused(
            f"limit_notes {limit} passes the vault hard cap of {caps.vault_notes_fail} notes"
        )
    if limit < HUB_FLOOR:
        raise VaultRefused(
            f"limit_notes {limit} cannot hold the Home note and the hubs "
            f"(at least {HUB_FLOOR} notes are needed)"
        )
    ranked = sorted(rows, key=lambda row: row["uid"])
    ranked.sort(key=_recency, reverse=True)
    nominal = _join(prefix + "cards", NOMINAL_FOLDERS)

    def total(count: int) -> int:
        chosen = ranked[:count]
        hubs = _hubs(chosen, lambda row: _join(nominal, row["uid"] + ".md"), caps)
        return count + _page_count(hubs) + 1

    # Every card is a note, so more records than notes cannot all fit: skip laying out the hubs
    # of a library of any size just to learn that, and search the count that does fit.
    if len(ranked) + HUB_FLOOR <= limit and total(len(ranked)) <= limit:
        count = len(ranked)
    else:
        low, high = 0, min(len(ranked), limit)
        while low < high:
            middle = (low + high + 1) // 2
            if total(middle) <= limit:
                low = middle
            else:
                high = middle - 1
        count = low
    chosen = sorted(ranked[:count], key=lambda row: row["uid"])
    card_path = _placed([row["uid"] for row in chosen], prefix, "cards", caps)
    hubs = _hubs(chosen, lambda row: card_path[row["uid"]], caps)
    hub_path = _hub_paths(hubs, prefix, caps)
    omitted = len(rows) - count
    home_path = prefix + "Home.md"
    groups = {key: len(members) for key, members in _facet_groups(chosen).items()}
    if omitted:
        status = (
            f"This card vault shows {count} of {len(rows)} records, the most recently updated "
            f"first, because it is limited to {limit} notes (cards, hub pages and this page). "
            f"{omitted} records have no card here; find them with `cce library search` and read "
            "them with `cce library read`. `cce library export-cards --limit-notes N` writes a "
            f"vault with another limit; the vault hard cap is {caps.vault_notes_fail} notes.\n\n"
        )
    else:
        status = (
            f"This card vault has one card for each of its {len(rows)} records. Every record has "
            "a card. Cards do not copy record bodies.\n\n"
        )
    commands = (
        "Search and read the full text with the bounded commands (STORE is the registered CCE "
        "store root):\n\n```sh\n"
        'cce library search TERM --store-root "$STORE" --limit 20 --offset 0\n'
        'cce library read UID --store-root "$STORE" --message 0 --offset 0 --limit 12000\n```\n\n'
    )

    files: list[tuple[str, str]] = []
    for row in chosen:
        check(check_callback)
        uid = row["uid"]
        files.append(
            (
                card_path[uid],
                _card_text(
                    row,
                    library.record(uid),
                    card_path[uid],
                    hub_path=hub_path,
                    home_path=home_path,
                    entry_doc=entry_doc,
                ),
            )
        )
    files.extend(_hub_notes(hubs, hub_path, home_path))
    files.append(
        (
            home_path,
            _home(
                [status, commands],
                hub_path,
                home_path,
                groups,
                entry_doc,
                "Source bodies are inert Markdown evidence; generated navigation links are "
                "separate.",
            ),
        )
    )
    gate = _gate_planned(PROFILE_CARDS, files, prefix, caps)
    return VaultPlan(
        PROFILE_CARDS,
        {row["uid"]: card_path.get(row["uid"]) for row in rows},
        len(rows),
        count,
        bool(omitted),
        omitted,
        len(files),
        limit,
        caps,
        files,
        gate,
    )


def plan_vault(
    profile: str,
    library: Library,
    rows: list[dict[str, Any]],
    *,
    prefix: str = "vault/",
    entry_doc: str | None = None,
    caps: VaultCaps | None = None,
    limit_notes: int | None = None,
    check_callback: Callable[..., Any] | None = None,
) -> VaultPlan:
    """Plan a vault for ``rows``; raises ``VaultRefused`` before anything exists if it cannot fit.

    ``prefix`` is the vault folder below the output root ("vault/" in a generation, "" for a
    stand-alone card vault); ``entry_doc`` is the retrieval note below the same root, if any.
    """
    caps = caps or DEFAULT_CAPS
    if profile == PROFILE_NONE:
        return VaultPlan(
            profile,
            dict.fromkeys((row["uid"] for row in rows)),
            len(rows),
            None,
            False,
            0,
            0,
            None,
            caps,
            [],
            measure_planned({}, caps, root=prefix),
        )
    if profile == PROFILE_FULL:
        if limit_notes is not None:
            raise VaultRefused("limit_notes applies to the cards profile only")
        return _plan_full(library, rows, prefix, entry_doc, caps, check_callback)
    if profile == PROFILE_CARDS:
        return _plan_cards(library, rows, prefix, entry_doc, caps, limit_notes, check_callback)
    raise ValueError(
        f"unknown vault profile {profile!r}; choose one of {', '.join(VAULT_PROFILES)}"
    )
