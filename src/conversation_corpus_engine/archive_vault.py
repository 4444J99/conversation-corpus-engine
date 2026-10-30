from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from . import archive_layout as layout
from .archive_ingest import (
    PROVIDER_SPECS,
    ArchivePaths,
    iter_store,
    load_index,
    load_manifest,
    require_inside,
)
from .archive_layout import wikilink
from .archive_limits import Limits
from .archive_model import Conversation, Message, write_json_atomic, write_text_atomic
from .archive_naming import NameLedger
from .archive_shard import part_folder, plan_conversation_folders, plan_flat_folders
from .archive_split import fit_blocks, pack, paginate_rows, utf8_size
from .archive_text import FenceTracker
from .archive_topics import load_enrichment
from .corpus_store import CorpusWriteAuthorization

# Where earlier builds kept the list of generated files, inside the vault. It is only read and
# deleted now; the live list is `ArchivePaths.vault_manifest`, beside the archive manifest.
RETIRED_VAULT_MANIFEST = ".cce-vault-manifest.json"
ROLE_LABELS = {"user": "User", "assistant": "Assistant", "system": "System", "tool": "Tool"}
COLLAPSED_KINDS = {"reasoning", "tool", "source"}
PALETTE = {
    "chatgpt": 0x10A37F,
    "claude": 0xD97757,
    "gemini": 0x4285F4,
    "notebooklm": 0x8E6BD6,
    "grok": 0x7A7A7A,
    "perplexity": 0x20B8CD,
    "copilot": 0xD6409F,
}
INLINE_CODE_RE = re.compile(r"(`[^`\n]+`)")
TAG_RE = re.compile(r"(?<![\w&\\/])#(?=[\w/-])")
READING_NOTE = (
    "*Conversation record — statements here are speech acts that were typed, not verified events.*"
)
# Room kept in every part for the navigation line, and the longest title a header repeats. The full
# title stays in the search store; a header only needs enough of it to be read.
NAV_RESERVE = 1024
TITLE_CHARS = 300
# Text more than this many times its byte size cannot grow past a part even after escaping, so a
# conversation under it is known to be one note without rendering it twice (neutralizing at worst
# doubles a line, and the header and navigation fit well inside the margin).
ESCAPE_GROWTH = 2
SINGLE_PART_MARGIN = 8192


def yaml_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def plain_title(title: str) -> str:
    """A note title with no bracket characters, so a pasted `[[Template]]` cannot mint a link."""
    cleaned = " ".join(title.split()).replace("[", "(").replace("]", ")") or "Untitled"
    if len(cleaned) > TITLE_CHARS:
        cleaned = cleaned[: TITLE_CHARS - 1].rstrip() + "…"
    return cleaned


def link_label(text: str) -> str:
    cleaned = " ".join(str(text).split()).replace("|", "/").replace("[", "(").replace("]", ")")
    return cleaned[:90] or "Untitled"


def conversation_link(uid: str, label: str, *, in_table: bool = False) -> str:
    """The link to a conversation's note, minted from the same name its file gets."""
    return wikilink(layout.conversation_note_for_uid(uid), link_label(label), in_table=in_table)


def neutralize(text: str) -> str:
    """Stop message text from creating wikilinks, embeds, tags or headings in the vault."""
    lines: list[str] = []
    tracker = FenceTracker()
    for raw in text.splitlines():
        if tracker.feed(raw):
            lines.append(raw)
            continue
        pieces = INLINE_CODE_RE.split(raw)
        for index in range(0, len(pieces), 2):
            pieces[index] = TAG_RE.sub(
                "\\#", pieces[index].replace("[[", "\\[\\[").replace("]]", "\\]\\]")
            )
        safe = "".join(pieces)
        if safe.lstrip().startswith("#"):
            safe = "\\" + safe.lstrip()
        lines.append(safe)
    if tracker.opener:
        lines.append(tracker.opener)
    return "\n".join(lines)


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())


def _stamp(ts: str) -> str:
    return ts[:16].replace("T", " ") if ts else "undated"


def message_blocks(message: Message, max_block: int) -> list[str]:
    """One message as blocks that each fit `max_block` bytes: whole, or split into numbered pieces.

    Nothing is dropped. A message too large for a block is cut at line boundaries (a fence cut
    this way is closed and reopened), and each piece says which piece of how many it is.
    """
    kind = str(message.meta.get("kind") or "text")
    label = ROLE_LABELS.get(message.role, message.role.title())
    if kind not in {"text", "code"}:
        label = f"{label} · {kind}"
    names = ", ".join(f"`{a.name}`" for a in message.attachments if a.name)

    def render(piece: str, index: int, total: int) -> str:
        body = neutralize(piece)
        if index == 0 and names:
            body = f"{body}\n\n*Attachments (names only):* {names}".strip()
        tag = f"{label} · piece {index + 1}/{total}" if total > 1 else label
        if kind in COLLAPSED_KINDS:
            return f"> [!quote]- {tag} · {_stamp(message.ts)}\n{_quote(body)}\n"
        return f"### {tag} · {_stamp(message.ts)}\n\n{body}\n"

    return fit_blocks(message.text, render, max_block)


def render_message(message: Message, limit: int) -> list[str]:
    """The blocks of one message under a block budget of `limit` bytes."""
    return message_blocks(message, limit)


def _facts(conversation: Conversation, collection_link: str) -> str:
    created = conversation.created or conversation.last_activity
    facts = [f"**Provider** {wikilink(layout.provider_note(conversation.provider))}"]
    month = layout.month_note(created)
    if month:
        facts.append(f"**Month** {wikilink(month)}")
    if collection_link:
        facts.append(f"**Collection** {collection_link}")
    return " · ".join(facts)


def _front(
    conversation: Conversation, topics: list[str], *, part: int, parts: int, title: str
) -> list[str]:
    created = conversation.created or conversation.last_activity
    year = created[:4] if created else "undated"
    tags = [f"provider/{conversation.provider}", f"kind/{conversation.kind}", f"year/{year}"]
    tags += [f"topic/{term}" for term in topics if layout.topic_note(term)]
    if parts > 1:
        tags.append("split")
    authors = Counter(message.author for message in conversation.messages)
    front = [
        "---",
        f"uid: {yaml_text(conversation.uid)}",
        f"provider: {conversation.provider}",
        f"kind: {conversation.kind}",
        f"title: {yaml_text(title)}",
        f"aliases: [{yaml_text(plain_title(conversation.title))}]",
        f"created: {conversation.created or 'null'}",
        f"last_activity: {conversation.last_activity or 'null'}",
        f"messages: {conversation.message_count}",
        f"human_turns: {authors['human']}",
        f"machine_turns: {authors['machine']}",
        f"words: {conversation.word_count}",
        f"model: {yaml_text(conversation.model)}",
        f"archived: {str(conversation.archived).lower()}",
        f"collection: {yaml_text(conversation.collection)}",
        f"source_file: {yaml_text(conversation.provenance.get('source_file', ''))}",
        f"source_sha256: {yaml_text(conversation.provenance.get('source_sha256', ''))}",
        f"part: {part}",
        f"parts: {parts}",
        f"tags: [{', '.join(tags)}]",
        "---",
    ]
    return front


def _first_head(
    conversation: Conversation,
    *,
    related: list[dict[str, Any]],
    topics: list[str],
    titles: dict[str, str],
    collection_link: str,
    parts: int,
) -> str:
    title = plain_title(conversation.title)
    header = [
        f"# {title}",
        "",
        READING_NOTE,
        "",
        _facts(conversation, collection_link),
    ]
    if topics:
        header.append("**Topics** " + " · ".join(_topic_label(term) for term in topics))
    if related:
        header.append(
            "**Related** "
            + " · ".join(
                f"{conversation_link(row['uid'], titles.get(row['uid'], row['uid']))} "
                f"({row['score']:.2f})"
                for row in related
            )
        )
    front = _front(conversation, topics, part=1, parts=parts, title=title)
    return "\n".join(["\n".join(front), "\n".join(header), ""])


def _part_head(conversation: Conversation, topics: list[str], *, part: int, parts: int) -> str:
    title = plain_title(conversation.title)
    front = _front(
        conversation, topics, part=part, parts=parts, title=f"{title} (part {part} of {parts})"
    )
    return "\n".join(["\n".join(front), f"# {title} — part {part} of {parts}", "", READING_NOTE, ""])


def _nav(names: list[str], index: int, anchor: str, labels: tuple[str, str, str]) -> str:
    """The line that walks a split note: previous, the first part, next."""
    back, start, forward = labels
    pieces = []
    if index > 0:
        pieces.append(wikilink(names[index - 1], back))
    if index != 0:
        pieces.append(wikilink(anchor, start))
    if index + 1 < len(names):
        pieces.append(wikilink(names[index + 1], forward))
    return " · ".join(pieces)


def _single_part_certain(conversation: Conversation, cap: int) -> bool:
    raw = sum(utf8_size(message.text) for message in conversation.messages)
    return raw * ESCAPE_GROWTH + SINGLE_PART_MARGIN + 1024 * len(conversation.messages) < cap


def conversation_part_names(conversation: Conversation, parts: int) -> list[str]:
    """The note names of a conversation's parts: the conversation note, then `part-...` names."""
    anchor = layout.conversation_note(conversation.provider, conversation.uid)
    return [anchor] + [
        layout.part_name(conversation.provider, number, anchor) for number in range(2, parts + 1)
    ]


def render_conversation(
    conversation: Conversation,
    *,
    related: list[dict[str, Any]],
    topics: list[str],
    titles: dict[str, str],
    collection_link: str,
    note_bytes: int | None = None,
) -> list[str]:
    """The conversation as notes of at most `note_bytes` bytes: the conversation note, then parts.

    Every message is present in full. A thread that does not fit one note continues in numbered
    parts linked from each other, so the first note is always a readable start and none of the
    text is cut.
    """
    cap = note_bytes or Limits().note_bytes
    # Sizes are measured on the header as it will be written, once parts are known to be 1 and
    # again with the real count (the digits are the only thing that moves).
    head = _first_head(
        conversation,
        related=related,
        topics=topics,
        titles=titles,
        collection_link=collection_link,
        parts=1,
    )
    part_head = _part_head(conversation, topics, part=2, parts=99999)
    first_budget = cap - utf8_size(head) - NAV_RESERVE - 16
    budget = cap - utf8_size(part_head) - NAV_RESERVE - 16
    if budget < 1024:
        raise ValueError(f"note_bytes {cap} leaves no room for content beside the header")
    blocks = [
        block for message in conversation.messages for block in message_blocks(message, budget)
    ]
    grouped = pack(blocks, first_budget, budget)
    total = len(grouped)
    names = conversation_part_names(conversation, total)
    labels = ("← Previous", "Start", "Next →")
    notes: list[str] = []
    for index, group in enumerate(grouped):
        if index == 0:
            text = _first_head(
                conversation,
                related=related,
                topics=topics,
                titles=titles,
                collection_link=collection_link,
                parts=total,
            )
        else:
            text = _part_head(conversation, topics, part=index + 1, parts=total)
        pieces = [text.rstrip("\n"), "", "\n".join(group).rstrip("\n")]
        if total > 1:
            pieces += ["", _nav(names, index, names[0], labels)]
        note = "\n".join(pieces).rstrip() + "\n"
        if utf8_size(note) > cap:
            raise ValueError(
                f"{conversation.uid}: note {index + 1} is {utf8_size(note)} bytes, cap is {cap}"
            )
        notes.append(note)
    return notes


def _topic_label(term: str) -> str:
    """A topic as a link to its hub, or as plain text when the term has no valid hub name."""
    note = layout.topic_note(term)
    return wikilink(note, term) if note else term


def _hub_rows(rows: list[dict[str, Any]]) -> list[str]:
    return [
        f"- {conversation_link(row['uid'], row['title'])} — {row['provider']} · "
        f"{row['message_count']} msg · {(row.get('created') or '')[:10] or 'undated'}"
        for row in sorted(
            rows, key=lambda r: (r.get("created") or r.get("last_activity") or "", r["uid"])
        )
    ]


def hub_pages(
    name: str, title: str, intro: str, rows: list[dict[str, Any]], note_bytes: int
) -> list[tuple[str, str]]:
    """A hub as (note name, text) pages of at most `note_bytes`: the hub, then `part-...` pages.

    Every row lands on some page whole; the hub's own name stays the first page, so every
    existing link to it still resolves.
    """
    lines = _hub_rows(rows)
    head = [f"# {title}", "", intro, ""]
    later_head = [f"# {title} — continued", ""]
    first_budget = note_bytes - sum(utf8_size(line) + 1 for line in head) - NAV_RESERVE
    budget = note_bytes - sum(utf8_size(line) + 1 for line in later_head) - NAV_RESERVE
    pages = paginate_rows([], lines, first_budget, budget)
    names = [name] + [layout.part_name("all", number, name) for number in range(2, len(pages) + 1)]
    out: list[tuple[str, str]] = []
    for index, group in enumerate(pages):
        header = head if index == 0 else later_head
        nav = _nav(names, index, name, ("← Previous", "Start", "Next →")) if len(pages) > 1 else ""
        text = "\n".join([*header, *group] + ([""] + [nav] if nav else [])) + "\n"
        out.append((names[index], text))
    return out


def _collection_names(rows: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    names: dict[tuple[str, str], str] = {}
    for row in sorted(rows, key=lambda r: r["uid"]):
        if row["collection"] and row["kind"] == "notebook":
            names.setdefault((row["provider"], row["collection"]), row["title"])
    return names


def _home(manifest: dict[str, Any], rows: list[dict[str, Any]], hub_count: int) -> str:
    totals = manifest["totals"]
    authors = totals.get("authors")
    lines = [
        "# AI Archive",
        "",
        READING_NOTE,
        "",
        f"Source fingerprint `{manifest['source_fingerprint'][:16]}…` · "
        f"{totals['conversations']} conversations · "
        f"{totals['messages']} messages · {totals['words']} words.",
    ]
    if authors:
        lines += [
            "",
            f"Turns by author: {authors.get('human', 0)} typed by the person (the authored "
            f"container) · {authors.get('machine', 0)} by the machine (the machine container).",
        ]
    lines += [
        "",
        "| Provider | Conversations | Empty | Messages | Words | First | Last activity |",
        "|---|---:|---:|---:|---:|---|---|",
    ]
    for name, info in manifest["providers"].items():
        display = PROVIDER_SPECS[name].display
        if info.get("status") != "ok":
            lines.append(f"| {display} | *{info.get('status')}* | | | | | |")
            continue
        lines.append(
            f"| {wikilink(layout.provider_note(name), display, in_table=True)} | "
            f"{info['conversations']} | {info['empty_conversations']} | {info['messages']} | "
            f"{info['words']} | {info['created_min'][:10]} | {info['last_activity_max'][:10]} |"
        )
    lines += [
        "",
        f"See {wikilink(layout.COVERAGE_NOTE)} for the channels read and not read, and "
        f"{wikilink(layout.GUIDE_NOTE)} for how to use this vault.",
        f"Topic hubs: {hub_count}. Browse by month via the `month-all-YYYYMM` notes.",
        "",
    ]
    return "\n".join(lines)


COVERAGE_INTRO = (
    "Denominators and scope for this vault, derived from the ingest manifest. A provider "
    "listed under *not read* was present in the export but deliberately outside this archive."
)


def _coverage(manifest: dict[str, Any]) -> str:
    lines = ["# Coverage", "", COVERAGE_INTRO, ""]
    for name, info in manifest["providers"].items():
        lines += [f"## {PROVIDER_SPECS[name].display}", "", f"Status: **{info.get('status')}**"]
        if info.get("status") == "ok":
            lines += [
                "",
                f"- conversations: {info['conversations']} ({info['empty_conversations']} empty, "
                f"{info.get('duplicates_folded', info.get('duplicates_skipped', 0))} duplicate(s) "
                f"folded: {info.get('duplicates_folded_identical', 0)} identical, "
                f"{info.get('duplicates_folded_differing', 0)} differing; "
                f"{info.get('derived_ids', 0)} derived id(s))",
                f"- messages: {info['messages']}; roles: {info['roles']}",
                f"- kinds: {info['kinds']}",
                f"- reader: `{info['reader']}`; counters: {info['reader_counters'] or 'none'}",
            ]
            if info.get("authors"):
                lines.append(f"- authors: {info['authors']}")
        if info.get("error"):
            lines.append(f"- error: `{info['error']}`")
        channels = info.get("channels") or {"read": [], "not_read": []}
        lines += ["", "Channels read:", *[f"- {item}" for item in channels["read"] or ["none"]]]
        lines += [
            "",
            "Channels not read:",
            *[f"- {item}" for item in channels["not_read"] or ["none"]],
        ]
        lines.append("")
    return "\n".join(lines)


READING_GUIDE = """# Reading guide

- **Conversation text is speech acts, not events.** A plan typed in a chat is evidence that the
  plan was typed. Corroborate with commits, transactions or files before treating it as done.
- Notes are *derived*: regenerate them with `cce archive vault`; edits inside generated notes
  are overwritten. Add your own notes anywhere outside the generated paths — they are never
  touched.
- **Nothing is cut.** A thread too long for one note continues in numbered parts (`part-...`
  notes in a folder beside the conversation note); each part links to the previous and next.
  Conversations sit in a folder per month, and a month that outgrew the folder limit is spread
  over folders per day.
- Each note's frontmatter counts its turns by author: `human_turns` were typed by the person,
  `machine_turns` are everything else. `cce archive search --author human` searches only the
  first.
- Colour in the graph view is by provider. Topic hubs (`topic-all-*`) bridge providers; the
  *Related* line on each note lists its nearest neighbours by TF-IDF cosine similarity.
- Attachments are referenced by file name only; their bytes are not part of the vault.
"""


def _obsidian_config() -> dict[str, str]:
    groups = [
        {"query": f"tag:#provider/{name}", "color": {"a": 1, "rgb": rgb}}
        for name, rgb in PALETTE.items()
    ]
    graph = {
        "collapse-filter": True,
        "search": "",
        "showTags": False,
        "showAttachments": False,
        "hideUnresolved": True,
        "showOrphans": True,
        "colorGroups": groups,
        "nodeSizeMultiplier": 1,
        "linkDistance": 250,
    }
    app = {"alwaysUpdateLinks": False, "newFileLocation": "root", "showFrontmatter": True}
    return {
        ".obsidian/graph.json": json.dumps(graph, indent=2) + "\n",
        ".obsidian/app.json": json.dumps(app, indent=2) + "\n",
    }


def _prune_empty_directories(root: Path) -> None:
    for directory in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
        if not any(directory.iterdir()):
            directory.rmdir()


def _sync_manifest(paths: ArchivePaths, root: Path, written: set[str]) -> int:
    """Record the generated files beside the archive manifest and prune what the last build wrote
    and this one did not. A manifest an earlier build left inside the vault is read once for its
    file list, then deleted."""
    path = paths.vault_manifest
    previous: list[str] = []
    if path.is_file():
        previous = json.loads(path.read_text(encoding="utf-8")).get("files", [])
    retired = root / RETIRED_VAULT_MANIFEST
    if retired.is_file():
        previous += json.loads(retired.read_text(encoding="utf-8")).get("files", [])
        retired.unlink()
    removed = 0
    for rel in previous:
        if rel not in written:
            target = root / rel
            if target.is_file():
                target.unlink()
                removed += 1
    _prune_empty_directories(root)
    write_json_atomic(path, {"files": sorted(written)})
    return removed


WIKILINK_RE = re.compile(r"(?<!\\)\[\[([^\]|\\]+)(?:\\?\|[^\]]*)?\]\]")


def link_targets(markdown: str) -> list[str]:
    """Wikilink targets Obsidian would resolve: code fences (also inside callouts) and inline
    code are not parsed for links, so they are excluded."""
    tracker = FenceTracker()
    kept = [raw for raw in markdown.splitlines() if not tracker.feed(raw.lstrip("> "))]
    return WIKILINK_RE.findall(INLINE_CODE_RE.sub(" ", "\n".join(kept)))


def verify_vault(paths: ArchivePaths) -> list[str]:
    """Check that every generated file exists and every wikilink resolves to a note."""
    root = paths.vault
    manifest_path = paths.vault_manifest
    if not manifest_path.is_file():
        return [f"vault manifest missing: {manifest_path}"]
    files = json.loads(manifest_path.read_text(encoding="utf-8")).get("files", [])
    problems = [f"missing generated file: {rel}" for rel in files if not (root / rel).is_file()]
    names = {Path(rel).stem for rel in files if rel.endswith(".md")}
    dangling: Counter[str] = Counter()
    for rel in files:
        target_file = root / rel
        if not rel.endswith(".md") or not target_file.is_file():
            continue
        for target in link_targets(target_file.read_text(encoding="utf-8")):
            if target not in names:
                dangling[target] += 1
    problems += [f"dangling wikilink [[{t}]] x{n}" for t, n in sorted(dangling.items())[:20]]
    return problems


def count_conversation_notes(paths: ArchivePaths) -> int:
    """Conversation notes in the vault (a split conversation counts once, not per part)."""
    return sum(1 for _ in paths.vault_conversations.rglob("conversation-*.md"))


def build_vault(
    paths: ArchivePaths,
    authorization: CorpusWriteAuthorization,
    *,
    limits: Limits | None = None,
) -> dict[str, Any]:
    limits = limits or Limits()
    root = require_inside(authorization, paths.vault)
    manifest = load_manifest(paths)
    rows = load_index(paths)
    titles = {row["uid"]: row["title"] for row in rows}
    enrichment = load_enrichment(paths)
    collection_names = _collection_names(rows)
    written: set[str] = set()
    ledger = NameLedger()

    def emit(folder: str, name: str, identity: str, content: str) -> None:
        """Write one note. The name is claimed first, so two different notes can never share a
        title (not even one that differs only by letter case)."""
        ledger.register(name, identity)
        rel = f"{folder}/{name}.md" if folder else f"{name}.md"
        write_text_atomic(root / rel, content)
        written.add(rel)

    by_month: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_provider: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_topic: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_collection: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    unnamed_topics: set[str] = set()
    for row in rows:
        month = layout.month_note(row["created"] or row["last_activity"])
        if month:
            by_month[month].append(row)
        by_provider[row["provider"]].append(row)
        if row["collection"]:
            by_collection[(row["provider"], row["collection"])].append(row)
        for term in enrichment.get(row["uid"], {}).get("topics", []):
            if layout.topic_note(term):
                by_topic[term].append(row)
            else:
                unnamed_topics.add(term)

    def collection_link(provider: str, collection: str) -> str:
        if not collection:
            return ""
        name = collection_names.get((provider, collection), collection)
        return wikilink(layout.collection_note(provider, collection), link_label(name))

    def render(conversation: Conversation) -> list[str]:
        extra = enrichment.get(conversation.uid, {})
        return render_conversation(
            conversation,
            related=extra.get("similar", []),
            topics=extra.get("topics", []),
            titles=titles,
            collection_link=collection_link(conversation.provider, conversation.collection),
            note_bytes=limits.note_bytes,
        )

    # Pass one: how many files each conversation takes, so the folder plan is exact. A
    # conversation that cannot reach the cap is known to be one note and is not rendered twice.
    stamps = {row["uid"]: row["created"] or row["last_activity"] for row in rows}
    weights: dict[str, int] = {}
    for conversation in iter_store(paths):
        if _single_part_certain(conversation, limits.note_bytes):
            weights[conversation.uid] = 1
        else:
            weights[conversation.uid] = 1 if len(render(conversation)) == 1 else 2
    plan = plan_conversation_folders(
        [(uid, stamps.get(uid, ""), weight) for uid, weight in weights.items()],
        limits.folder_entries,
    )

    # Pass two: write.
    notes = split_notes = parts_written = 0
    for conversation in iter_store(paths):
        rendered = render(conversation)
        names = conversation_part_names(conversation, len(rendered))
        folder = f"{layout.VAULT_CONVERSATIONS_DIRNAME}/{plan[conversation.uid]}"
        emit(folder, names[0], f"conversation:{conversation.uid}", rendered[0])
        notes += 1
        if len(rendered) > 1:
            split_notes += 1
            parts_folder = f"{folder}/{names[0]}"
            for position, (name, text) in enumerate(zip(names[1:], rendered[1:], strict=True)):
                sub = part_folder(position, len(rendered) - 1, limits.folder_entries)
                emit(
                    f"{parts_folder}/{sub}" if sub else parts_folder,
                    name,
                    f"conversation:{conversation.uid}:{name}",
                    text,
                )
                parts_written += 1

    hubs = layout.VAULT_HUBS_DIRNAME
    hub_notes: list[tuple[str, str, str]] = []  # (name, identity, text)

    def add_hub(name: str, identity: str, title: str, intro: str, hub_rows: list[dict]) -> None:
        for page_name, text in hub_pages(name, title, intro, hub_rows, limits.note_bytes):
            page_identity = identity if page_name == name else f"{identity}:{page_name}"
            hub_notes.append((page_name, page_identity, text))

    for name, items in by_provider.items():
        add_hub(
            layout.provider_note(name),
            f"provider:{name}",
            PROVIDER_SPECS[name].display,
            f"{len(items)} conversations.",
            items,
        )
    for month, items in by_month.items():
        digits = month.rsplit("-", 1)[-1]
        add_hub(
            month,
            f"month:{digits}",
            f"{digits[:4]}-{digits[4:]}",
            f"{len(items)} conversations started.",
            items,
        )
    for term, items in by_topic.items():
        note = layout.topic_note(term)
        if note is None:  # by_topic only holds named terms; this keeps the type honest
            continue
        add_hub(
            note,
            f"topic:{term}",
            f"Topic: {term}",
            f"{len(items)} conversations across {len({r['provider'] for r in items})} provider(s).",
            items,
        )
    for (provider, collection), items in by_collection.items():
        name = collection_names.get((provider, collection), collection)
        add_hub(
            layout.collection_note(provider, collection),
            f"collection:{provider}\0{collection}",
            f"{PROVIDER_SPECS[provider].display}: {name}",
            f"{len(items)} item(s).",
            items,
        )
    hub_folders = plan_flat_folders([name for name, _, _ in hub_notes], limits.folder_entries)
    for name, identity, text in hub_notes:
        sub = hub_folders[name]
        emit(f"{hubs}/{sub}" if sub else hubs, name, identity, text)

    emit("", layout.HOME_NOTE, "home", _home(manifest, rows, len(by_topic)))
    emit(layout.VAULT_META_DIRNAME, layout.COVERAGE_NOTE, "coverage", _coverage(manifest))
    emit(layout.VAULT_META_DIRNAME, layout.GUIDE_NOTE, "guide", READING_GUIDE)
    for rel, content in _obsidian_config().items():
        if not (root / rel).is_file():
            write_text_atomic(root / rel, content)
        written.add(rel)
    require_inside(authorization, paths.vault_manifest)
    removed = _sync_manifest(paths, root, written)
    return {
        "notes": notes,
        "split_notes": split_notes,
        "parts": parts_written,
        "hubs": len(hub_notes),
        "removed_stale": removed,
        "files": len(written),
        "topics_without_hub": len(unnamed_topics),
    }
