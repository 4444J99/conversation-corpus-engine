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
from .archive_model import Conversation, Message, write_json_atomic, write_text_atomic
from .archive_naming import NameLedger
from .archive_text import FenceTracker
from .archive_topics import load_enrichment
from .corpus_store import CorpusWriteAuthorization

# Where earlier builds kept the list of generated files, inside the vault. It is only read and
# deleted now; the live list is `ArchivePaths.vault_manifest`, beside the archive manifest.
RETIRED_VAULT_MANIFEST = ".cce-vault-manifest.json"
MESSAGE_CHARS = 60_000
NOTE_CHARS = 400_000
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
INLINE_CODE_RE = re.compile(r"(?<!`)(`+)(?!`)[^\n]*?(?<!`)\1(?!`)")
TAG_RE = re.compile(r"(?<![\w&\\/])#(?=[\w/-])")
READING_NOTE = (
    "*Conversation record — statements here are speech acts that were typed, not verified events.*"
)


def yaml_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def plain_title(title: str) -> str:
    """A note title with no bracket characters, so a pasted `[[Template]]` cannot mint a link."""
    return " ".join(title.split()).replace("[", "(").replace("]", ")") or "Untitled"


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
        pieces: list[str] = []
        end = 0
        for match in INLINE_CODE_RE.finditer(raw):
            pieces.extend((raw[end : match.start()], match.group()))
            end = match.end()
        pieces.append(raw[end:])
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


def _attachment_name(name: str) -> str:
    """Display metadata as one inert code span, even when the filename contains backticks."""
    name = " ".join(name.splitlines())
    ticks = "`" * (max((len(run[0]) for run in re.finditer(r"`+", name)), default=0) + 1)
    if name.startswith(("`", " ")) or name.endswith(("`", " ")):
        name = f" {name} "
    return f"{ticks}{name}{ticks}"


def render_message(message: Message, limit: int) -> tuple[str, bool]:
    text = message.text
    cut = len(text) > limit
    if cut:
        text = text[:limit] + f"\n\n…[{len(message.text) - limit} characters omitted in the vault]"
    body = neutralize(text)
    label = ROLE_LABELS.get(message.role, message.role.title())
    kind = str(message.meta.get("kind") or "text")
    if kind not in {"text", "code"}:
        label = f"{label} · {kind}"
    if message.attachments:
        names = ", ".join(_attachment_name(a.name) for a in message.attachments if a.name)
        if names:
            body = f"{body}\n\n*Attachments (names only):* {names}".strip()
    if kind in COLLAPSED_KINDS:
        return f"> [!quote]- {label} · {_stamp(message.ts)}\n{_quote(body)}\n", cut
    return f"### {label} · {_stamp(message.ts)}\n\n{body}\n", cut


def render_conversation(
    conversation: Conversation,
    *,
    related: list[dict[str, Any]],
    topics: list[str],
    titles: dict[str, str],
    collection_link: str,
    message_limit: int = MESSAGE_CHARS,
    note_limit: int = NOTE_CHARS,
) -> tuple[str, bool]:
    created = conversation.created or conversation.last_activity
    year = created[:4] if created else "undated"
    tags = [f"provider/{conversation.provider}", f"kind/{conversation.kind}", f"year/{year}"]
    tags += [f"topic/{term}" for term in topics if layout.topic_note(term)]
    front = [
        "---",
        f"uid: {yaml_text(conversation.uid)}",
        f"provider: {conversation.provider}",
        f"kind: {conversation.kind}",
        f"title: {yaml_text(plain_title(conversation.title))}",
        f"aliases: [{yaml_text(plain_title(conversation.title))}]",
        f"created: {conversation.created or 'null'}",
        f"last_activity: {conversation.last_activity or 'null'}",
        f"messages: {conversation.message_count}",
        f"words: {conversation.word_count}",
        f"model: {yaml_text(conversation.model)}",
        f"archived: {str(conversation.archived).lower()}",
        f"collection: {yaml_text(conversation.collection)}",
        f"source_file: {yaml_text(conversation.provenance.get('source_file', ''))}",
        f"source_sha256: {yaml_text(conversation.provenance.get('source_sha256', ''))}",
        f"tags: [{', '.join(tags)}]",
        "---",
    ]
    month = layout.month_note(created)
    facts = [f"**Provider** {wikilink(layout.provider_note(conversation.provider))}"]
    if month:
        facts.append(f"**Month** {wikilink(month)}")
    if collection_link:
        facts.append(f"**Collection** {collection_link}")
    header = [
        f"# {plain_title(conversation.title)}",
        "",
        READING_NOTE,
        "",
        " · ".join(facts),
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
    parts = ["\n".join(front), "\n".join(header), ""]
    truncated = False
    used = sum(len(part) for part in parts)
    for index, message in enumerate(conversation.messages):
        block, cut = render_message(message, message_limit)
        truncated = truncated or cut
        used += len(block)
        if used > note_limit:
            omitted = conversation.message_count - index
            parts.append(f"\n*…{omitted} further message(s) omitted from this note (size cap).*\n")
            truncated = True
            break
        parts.append(block)
    return "\n".join(parts).rstrip() + "\n", truncated


def _topic_label(term: str) -> str:
    """A topic as a link to its hub, or as plain text when the term has no valid hub name."""
    note = layout.topic_note(term)
    return wikilink(note, term) if note else term


def _hub(title: str, intro: str, rows: list[dict[str, Any]]) -> str:
    lines = [f"# {title}", "", intro, ""]
    for row in sorted(
        rows, key=lambda r: (r.get("created") or r.get("last_activity") or "", r["uid"])
    ):
        lines.append(
            f"- {conversation_link(row['uid'], row['title'])} — {row['provider']} · "
            f"{row['message_count']} msg · {(row.get('created') or '')[:10] or 'undated'}"
        )
    return "\n".join(lines) + "\n"


def _collection_names(rows: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    names: dict[tuple[str, str], str] = {}
    for row in sorted(rows, key=lambda r: r["uid"]):
        if row["collection"] and row["kind"] == "notebook":
            names.setdefault((row["provider"], row["collection"]), row["title"])
    return names


def _home(manifest: dict[str, Any], rows: list[dict[str, Any]], hub_count: int) -> str:
    lines = [
        "# AI Archive",
        "",
        READING_NOTE,
        "",
        f"Source fingerprint `{manifest['source_fingerprint'][:16]}…` · "
        f"{manifest['totals']['conversations']} conversations · "
        f"{manifest['totals']['messages']} messages · {manifest['totals']['words']} words.",
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


def _manifest_files(path: Path, root: Path) -> list[str]:
    """Validate every recorded path before using a manifest to read or remove vault files."""
    error = f"invalid vault manifest {path.name}"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{error}: cannot read JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
        raise ValueError(f"{error}: files must be a list")
    files = payload["files"]
    vault = root.resolve()
    for index, rel in enumerate(files):
        if not isinstance(rel, str) or not rel or "\0" in rel:
            raise ValueError(f"{error}: file entry {index} must be a nonempty path string")
        entry = Path(rel)
        if entry.is_absolute() or ".." in entry.parts:
            raise ValueError(f"{error}: file entry {index} must be relative without '..'")
        try:
            target = (root / entry).resolve()
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"{error}: file entry {index} cannot be resolved") from exc
        if target == vault or not target.is_relative_to(vault):
            raise ValueError(f"{error}: file entry {index} resolves outside the vault")
    return files


def _vault_target(root: Path, target: Path) -> Path:
    """Keep generated files inside the resolved vault, including existing symlink parents."""
    try:
        resolved = target.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError("generated vault path cannot be resolved") from exc
    if resolved == root or not resolved.is_relative_to(root):
        raise ValueError("generated path resolves outside the vault")
    return target


def _write_vault_text(root: Path, rel: str, content: str) -> None:
    target = _vault_target(root, root / rel)
    _vault_target(root, target.with_name(f".{target.name}.tmp"))
    write_text_atomic(target, content)


def _sync_manifest(paths: ArchivePaths, root: Path, written: set[str]) -> int:
    """Record the generated files beside the archive manifest and prune what the last build wrote
    and this one did not. A manifest an earlier build left inside the vault is read once for its
    file list, then deleted."""
    path = paths.vault_manifest
    previous: list[str] = []
    if path.is_file():
        previous = _manifest_files(path, root)
    retired = root / RETIRED_VAULT_MANIFEST
    if retired.is_file():
        previous += _manifest_files(retired, root)
    # Neither a stale note nor the legacy manifest is removed until both lists are safe.
    if retired.is_file():
        retired.unlink()
    removed = 0
    for rel in previous:
        if rel not in written:
            target = root / rel
            if target.is_file():
                target.unlink()
                removed += 1
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
    try:
        files = _manifest_files(manifest_path, root)
    except ValueError as exc:
        return [str(exc)]
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


def build_vault(
    paths: ArchivePaths,
    authorization: CorpusWriteAuthorization,
    *,
    message_chars: int = MESSAGE_CHARS,
    note_chars: int = NOTE_CHARS,
) -> dict[str, Any]:
    root = require_inside(authorization, paths.vault)
    require_inside(authorization, paths.vault_manifest)
    require_inside(
        authorization, paths.vault_manifest.with_name(f".{paths.vault_manifest.name}.tmp")
    )
    # Refuse corrupt records before replacing any note. Synchronization validates again before
    # cleanup, so it never silently trusts a manifest changed during rendering.
    for previous_manifest in (paths.vault_manifest, root / RETIRED_VAULT_MANIFEST):
        if previous_manifest.is_file():
            _manifest_files(previous_manifest, root)
    for folder in (
        layout.VAULT_CONVERSATIONS_DIRNAME,
        layout.VAULT_HUBS_DIRNAME,
        layout.VAULT_META_DIRNAME,
        ".obsidian",
    ):
        _vault_target(root, root / folder)
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
        _write_vault_text(root, rel, content)
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

    truncated = notes = 0
    for conversation in iter_store(paths):
        extra = enrichment.get(conversation.uid, {})
        text, cut = render_conversation(
            conversation,
            related=extra.get("similar", []),
            topics=extra.get("topics", []),
            titles=titles,
            collection_link=collection_link(conversation.provider, conversation.collection),
            message_limit=message_chars,
            note_limit=note_chars,
        )
        emit(
            layout.VAULT_CONVERSATIONS_DIRNAME,
            layout.conversation_note(conversation.provider, conversation.uid),
            f"conversation:{conversation.uid}",
            text,
        )
        truncated += int(cut)
        notes += 1

    hubs = layout.VAULT_HUBS_DIRNAME
    for name, items in by_provider.items():
        emit(
            hubs,
            layout.provider_note(name),
            f"provider:{name}",
            _hub(PROVIDER_SPECS[name].display, f"{len(items)} conversations.", items),
        )
    for month, items in by_month.items():
        digits = month.rsplit("-", 1)[-1]
        emit(
            hubs,
            month,
            f"month:{digits}",
            _hub(f"{digits[:4]}-{digits[4:]}", f"{len(items)} conversations started.", items),
        )
    for term, items in by_topic.items():
        note = layout.topic_note(term)
        if note is None:  # by_topic only holds named terms; this keeps the type honest
            continue
        emit(
            hubs,
            note,
            f"topic:{term}",
            _hub(
                f"Topic: {term}",
                f"{len(items)} conversations across "
                f"{len({r['provider'] for r in items})} provider(s).",
                items,
            ),
        )
    for (provider, collection), items in by_collection.items():
        name = collection_names.get((provider, collection), collection)
        emit(
            hubs,
            layout.collection_note(provider, collection),
            f"collection:{provider}\0{collection}",
            _hub(f"{PROVIDER_SPECS[provider].display}: {name}", f"{len(items)} item(s).", items),
        )
    emit("", layout.HOME_NOTE, "home", _home(manifest, rows, len(by_topic)))
    emit(layout.VAULT_META_DIRNAME, layout.COVERAGE_NOTE, "coverage", _coverage(manifest))
    emit(layout.VAULT_META_DIRNAME, layout.GUIDE_NOTE, "guide", READING_GUIDE)
    for rel, content in _obsidian_config().items():
        target = _vault_target(root, root / rel)
        if not target.is_file():
            _write_vault_text(root, rel, content)
        written.add(rel)
    removed = _sync_manifest(paths, root, written)
    hub_notes = sum(1 for rel in written if rel.startswith(f"{hubs}/"))
    return {
        "notes": notes,
        "hubs": hub_notes,
        "truncated_notes": truncated,
        "removed_stale": removed,
        "files": len(written),
        "topics_without_hub": len(unnamed_topics),
    }
