from __future__ import annotations

import hashlib
import json
import shutil
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import archive_layout as layout
from . import (
    archive_read_chatgpt,
    archive_read_claude,
    archive_read_copilot,
    archive_read_gemini,
    archive_read_grok,
    archive_read_notebooklm,
    archive_read_perplexity,
)
from .archive_author import AUTHORS, author_of, container_of
from .archive_limits import SHARD_BYTES
from .archive_model import (
    SCHEMA,
    Conversation,
    SourceTracker,
    finalize_conversation,
    iter_conversations,
    iter_jsonl,
    sha256_file,
    write_json_atomic,
)
from .archive_naming import NAMING_VERSION, day_of
from .archive_shard import ShardWriter, month_key, parse_shard_subject, shard_sort_key
from .corpus_store import CorpusStoreError, CorpusWriteAuthorization, authorize_corpus_write

MANIFEST_SCHEMA = "archive-manifest.v2"
LEGACY_MANIFEST_SCHEMA = "cce.archive.manifest.v1"
ACCEPTED_MANIFEST_SCHEMAS = frozenset({MANIFEST_SCHEMA, LEGACY_MANIFEST_SCHEMA})
SCHEMA_VERSION = 2
# The layout of the folders and files, not of the rows: 3 is the bounded layout (provider-month
# shards, an author count on every provider, parts and fan-out limits in the vault and site). A
# manifest with no `layout_version` is a first-generation layout and still reads.
LAYOUT_VERSION = 3
# Where a provider's shards are written before they replace the previous ones.
BUILD_DIRNAME = ".build"
DEFAULT_ARCHIVE_DIRNAME = layout.ARCHIVE_DIRNAME
# Per-provider manifest keys that hold absolute paths or host-specific error text; they are
# dropped from the copy the source fingerprint hashes.
FINGERPRINT_EXCLUDED_KEYS = frozenset({"source_root", "error"})


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    display: str
    default_rel: str
    module: Any
    open_reader: Callable[[Path, SourceTracker, dict[str, Any]], Iterator[Conversation]]


def _chatgpt(root: Path, tracker: SourceTracker, options: dict[str, Any]) -> Iterator[Conversation]:
    return archive_read_chatgpt.iter_conversations(
        root, tracker, extra_files=options.get("chatgpt_extra_files", ())
    )


def _claude(root: Path, tracker: SourceTracker, options: dict[str, Any]) -> Iterator[Conversation]:
    return archive_read_claude.iter_conversations(
        root, tracker, include_attachment_text=bool(options.get("include_attachment_text"))
    )


def _plain(module: Any) -> Callable[[Path, SourceTracker, dict[str, Any]], Iterator[Conversation]]:
    def opener(
        root: Path, tracker: SourceTracker, options: dict[str, Any]
    ) -> Iterator[Conversation]:
        return module.iter_conversations(root, tracker)

    return opener


PROVIDER_SPECS: dict[str, ProviderSpec] = {
    spec.name: spec
    for spec in (
        ProviderSpec("chatgpt", "ChatGPT", "chatgpt", archive_read_chatgpt, _chatgpt),
        ProviderSpec("claude", "Claude", "claude", archive_read_claude, _claude),
        ProviderSpec(
            "gemini", "Gemini", "gemini", archive_read_gemini, _plain(archive_read_gemini)
        ),
        ProviderSpec(
            "notebooklm",
            "NotebookLM",
            "gemini/NotebookLM",
            archive_read_notebooklm,
            _plain(archive_read_notebooklm),
        ),
        ProviderSpec("grok", "Grok", "grok", archive_read_grok, _plain(archive_read_grok)),
        ProviderSpec(
            "perplexity",
            "Perplexity",
            "perplexity",
            archive_read_perplexity,
            _plain(archive_read_perplexity),
        ),
        ProviderSpec(
            "copilot", "Copilot", "copilot", archive_read_copilot, _plain(archive_read_copilot)
        ),
    )
}


@dataclass(frozen=True)
class ArchivePaths:
    """Every path under the archive folder. Each name comes from `archive_layout`, which mints
    it with `derive_name`; nothing here spells a file or folder name by hand."""

    root: Path

    # --- directly in the archive folder ---

    @property
    def manifest(self) -> Path:
        return self.root / layout.MANIFEST_FILENAME

    @property
    def database(self) -> Path:
        return self.root / layout.DATABASE_FILENAME

    @property
    def enrichment(self) -> Path:
        return self.root / layout.ENRICHMENT_FILENAME

    @property
    def topics(self) -> Path:
        return self.root / layout.TOPICS_FILENAME

    @property
    def vault(self) -> Path:
        return self.root / layout.VAULT_DIRNAME

    @property
    def vault_manifest(self) -> Path:
        return self.root / layout.VAULT_MANIFEST_FILENAME

    @property
    def site(self) -> Path:
        return self.root / layout.SITE_DIRNAME

    @property
    def graph(self) -> Path:
        return self.root / layout.GRAPH_DIRNAME

    # --- per provider ---

    def provider_dir(self, provider: str) -> Path:
        return self.root / layout.provider_dirname(provider)

    def conversations(self, provider: str) -> Path:
        """The first-generation single file of a provider's conversations (read-only legacy)."""
        return self.provider_dir(provider) / layout.conversations_filename(provider)

    def index(self, provider: str) -> Path:
        """The first-generation single index file of a provider (read-only legacy)."""
        return self.provider_dir(provider) / layout.index_filename(provider)

    def _shards(self, provider: str, prefix: str) -> list[Path]:
        directory = self.provider_dir(provider)
        if not directory.is_dir():
            return []
        stem = f"{prefix}-{provider}-"
        found: list[tuple[tuple[int, str, int], Path]] = []
        for path in directory.glob(f"{stem}*.jsonl"):
            try:
                found.append((shard_sort_key(path.name[len(stem) : -len(".jsonl")]), path))
            except ValueError:
                continue
        return [path for _, path in sorted(found)]

    def shard_files(self, provider: str) -> list[Path]:
        """The provider's conversation shards in reading order (months, then `undated`)."""
        return self._shards(provider, "shard")

    def index_shard_files(self, provider: str) -> list[Path]:
        """The index rows of the same shards, in the same order."""
        return self._shards(provider, "index")

    def conversation_files(self, provider: str) -> list[Path]:
        """Where a provider's conversations are: its shards, else the first-generation file."""
        shards = self.shard_files(provider)
        if shards:
            return shards
        legacy = self.conversations(provider)
        return [legacy] if legacy.is_file() else []

    def index_files(self, provider: str) -> list[Path]:
        shards = self.index_shard_files(provider)
        if shards:
            return shards
        legacy = self.index(provider)
        return [legacy] if legacy.is_file() else []

    def provider_present(self, provider: str) -> bool:
        return bool(self.conversation_files(provider))

    def feed(self, provider: str) -> Path:
        return self.root / layout.feed_filename(provider)

    def receipt(self, provider: str, fingerprint: str, day: str) -> Path:
        return self.root / layout.receipt_filename(provider, fingerprint, day)

    def providers_present(self) -> list[str]:
        return [name for name in PROVIDER_SPECS if self.provider_present(name)]

    # --- the turn store: one container per author ---

    @property
    def turns(self) -> Path:
        return self.root / layout.TURNS_DIRNAME

    @property
    def turns_manifest(self) -> Path:
        return self.turns / layout.TURNS_MANIFEST_FILENAME

    def container(self, author: str) -> Path:
        return self.turns / layout.container_dirname(container_of(author))

    def turn_shard(self, author: str, provider: str, subject: str) -> Path:
        return (
            self.container(author)
            / layout.provider_dirname(provider)
            / layout.turn_shard_filename(provider, subject)
        )

    # --- the vault ---

    @property
    def vault_conversations(self) -> Path:
        return self.vault / layout.VAULT_CONVERSATIONS_DIRNAME

    @property
    def vault_hubs(self) -> Path:
        return self.vault / layout.VAULT_HUBS_DIRNAME

    @property
    def vault_meta(self) -> Path:
        return self.vault / layout.VAULT_META_DIRNAME

    # --- the graph ---

    @property
    def graph_nodes(self) -> Path:
        return self.graph / layout.GRAPH_NODES_FILENAME

    @property
    def graph_edges(self) -> Path:
        return self.graph / layout.GRAPH_EDGES_FILENAME

    @property
    def graph_graphml(self) -> Path:
        return self.graph / layout.GRAPH_GRAPHML_FILENAME

    @property
    def graph_summary(self) -> Path:
        return self.graph / layout.GRAPH_SUMMARY_FILENAME

    # --- the site ---

    @property
    def site_pages(self) -> Path:
        return self.site / layout.SITE_PAGES_DIRNAME

    @property
    def site_home(self) -> Path:
        return self.site / layout.SITE_HOME_FILENAME

    def site_page(self, provider: str, uid: str) -> Path:
        """The first page of a conversation. Pages sit in month folders (and deeper when a month
        outgrew the folder limit), so the page is found by its name; a conversation not yet built
        answers with the flat path a first-generation site used."""
        name = f"{layout.conversation_note(provider, uid)}.html"
        if self.site_pages.is_dir():
            found = next(self.site_pages.rglob(name), None)
            if found is not None:
                return found
        return self.site_pages / name

    def vault_note(self, provider: str, uid: str) -> Path:
        """The conversation note, found by name for the same reason as `site_page`."""
        name = f"{layout.conversation_note(provider, uid)}.md"
        if self.vault_conversations.is_dir():
            found = next(self.vault_conversations.rglob(name), None)
            if found is not None:
                return found
        return self.vault_conversations / name


def latest_activity_day(manifest: dict[str, Any]) -> str:
    """The day (eight digits) of the latest activity any provider reports, from the data only.

    Receipts carry this day. There is no clock fallback: a manifest with no activity stamp at
    all has no day, and asking for one is an error.
    """
    stamps = [
        info.get("last_activity_max", "")
        for info in manifest["providers"].values()
        if info.get("status") == "ok" and info.get("last_activity_max")
    ]
    return day_of(max(stamps)) if stamps else day_of("")


def authorize_archive(
    *,
    project_root: Path,
    archive_root: Path,
    corpus_store_root: Path | None,
    source_roots: tuple[Path, ...] = (),
) -> CorpusWriteAuthorization:
    """Gate every archive write through the engine's private-custody contract."""
    return authorize_corpus_write(
        project_root=project_root,
        destination=archive_root,
        corpus_store_root=corpus_store_root,
        source_roots=source_roots,
    )


def require_inside(authorization: CorpusWriteAuthorization, path: Path) -> Path:
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(authorization.destination)
    except ValueError as exc:
        raise CorpusStoreError(f"Archive path escapes the authorized root: {resolved}") from exc
    return resolved


def resolve_sources(
    export_root: Path, providers: list[str] | None, overrides: dict[str, Path] | None = None
) -> dict[str, Path]:
    chosen = providers or list(PROVIDER_SPECS)
    unknown = [name for name in chosen if name not in PROVIDER_SPECS]
    if unknown:
        raise KeyError(f"Unknown archive provider(s): {', '.join(unknown)}")
    sources: dict[str, Path] = {}
    for name in chosen:
        override = (overrides or {}).get(name)
        sources[name] = (override or export_root / PROVIDER_SPECS[name].default_rel).resolve()
    return sources


def _bump(counter: Counter[str], key: str, amount: int = 1) -> None:
    counter[key] += amount


def content_digest(conversation: Conversation) -> str:
    """SHA-256 of a normalized conversation's content: everything but where it was read from, so
    the same conversation found in two files gives one digest."""
    payload = conversation.to_dict()
    payload.pop("provenance", None)
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def conversation_stamp(conversation: Conversation) -> str:
    """The timestamp that decides a conversation's month: when it began, else its last activity."""
    return conversation.created or conversation.last_activity


def _publish_shards(paths: ArchivePaths, provider: str, build: Path) -> None:
    """Replace a provider's previous conversation files with the freshly built shards.

    Everything the previous build (or the first-generation layout) left is removed only after
    the new shards are complete, so a failed ingest leaves the previous store readable.
    """
    directory = paths.provider_dir(provider)
    for stale in [
        *paths.shard_files(provider),
        *paths.index_shard_files(provider),
        paths.conversations(provider),
        paths.index(provider),
    ]:
        stale.unlink(missing_ok=True)
    for built in sorted(build.iterdir()):
        built.replace(directory / built.name)
    build.rmdir()


def _ingest_provider(
    spec: ProviderSpec, root: Path, paths: ArchivePaths, options: dict[str, Any]
) -> dict[str, Any]:
    if not root.exists() or not spec.module.detect(root):
        return {
            "status": "absent",
            "source_root": str(root),
            "channels": {"read": [], "not_read": []},
        }
    tracker = SourceTracker(root, spec.module.READER)
    seen: dict[str, str] = {}
    kinds: Counter[str] = Counter()
    roles: Counter[str] = Counter()
    authors: Counter[str] = Counter()
    totals: Counter[str] = Counter()
    created: list[str] = []
    activity: list[str] = []
    shard_bytes = int(options.get("shard_bytes") or SHARD_BYTES)
    directory = paths.provider_dir(spec.name)
    directory.mkdir(parents=True, exist_ok=True)
    build = directory / BUILD_DIRNAME
    shutil.rmtree(build, ignore_errors=True)
    writer = ShardWriter(build, spec.name, shard_bytes=shard_bytes)
    error = ""
    shards: list[dict[str, Any]] = []
    try:
        for conversation in spec.open_reader(root, tracker, options):
            derived = finalize_conversation(conversation)
            uid = conversation.uid
            content = content_digest(conversation)
            if uid in seen:
                _bump(totals, "duplicates_folded")
                kept = "identical" if seen[uid] == content else "differing"
                _bump(totals, f"duplicates_folded_{kept}")
                continue
            seen[uid] = content
            if derived:
                _bump(totals, "derived_ids")
                if derived == "unnameable":
                    _bump(totals, "unnameable_ids")
            writer.add(
                month_key(conversation_stamp(conversation)),
                (
                    json.dumps(conversation.to_dict(), sort_keys=True, ensure_ascii=False) + "\n"
                ).encode("utf-8"),
                (
                    json.dumps(conversation.index_row(), sort_keys=True, ensure_ascii=False)
                    + "\n"
                ).encode("utf-8"),
            )
            _bump(totals, "conversations")
            _bump(kinds, conversation.kind)
            if not conversation.messages:
                _bump(totals, "empty_conversations")
            _bump(totals, "messages", conversation.message_count)
            _bump(totals, "words", conversation.word_count)
            _bump(totals, "attachments", conversation.attachment_count)
            for message in conversation.messages:
                _bump(roles, message.role)
                _bump(authors, author_of(message.role, message.channel))
            if conversation.created:
                created.append(conversation.created)
            if conversation.last_activity:
                activity.append(conversation.last_activity)
        shards = writer.close()
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
        error = f"{type(exc).__name__}: {exc}"
    if error:
        writer.close()
        shutil.rmtree(build, ignore_errors=True)
        return {
            "status": "failed",
            "error": error,
            "source_root": str(root),
            "channels": spec.module.channels(root),
        }
    _publish_shards(paths, spec.name, build)
    for shard in shards:
        shard["sha256"] = sha256_file(
            directory / layout.shard_filename(spec.name, shard["subject"])
        )
    return {
        "status": "ok",
        "source_root": str(root),
        "conversations": totals["conversations"],
        "empty_conversations": totals["empty_conversations"],
        "derived_ids": totals["derived_ids"],
        "unnameable_ids": totals["unnameable_ids"],
        "duplicates_folded": totals["duplicates_folded"],
        "duplicates_folded_identical": totals["duplicates_folded_identical"],
        "duplicates_folded_differing": totals["duplicates_folded_differing"],
        "messages": totals["messages"],
        "words": totals["words"],
        "attachments": totals["attachments"],
        "kinds": dict(sorted(kinds.items())),
        "roles": dict(sorted(roles.items())),
        "authors": {author: authors[author] for author in AUTHORS},
        "created_min": min(created) if created else "",
        "last_activity_max": max(activity) if activity else "",
        "reader": spec.module.READER,
        "reader_counters": dict(sorted(tracker.counters.items())),
        "channels": spec.module.channels(root),
        "source_files": tracker.hashes(),
        "layout": {"version": LAYOUT_VERSION, "shard_bytes": shard_bytes, "shards": shards},
    }


def ingest(
    *,
    export_root: Path,
    paths: ArchivePaths,
    authorization: CorpusWriteAuthorization,
    providers: list[str] | None = None,
    overrides: dict[str, Path] | None = None,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    require_inside(authorization, paths.root)
    options = dict(options or {})
    sources = resolve_sources(export_root, providers, overrides)
    report: dict[str, Any] = {}
    for name, root in sources.items():
        report[name] = _ingest_provider(PROVIDER_SPECS[name], root, paths, options)
    reader_versions = {name: PROVIDER_SPECS[name].module.READER for name in report}
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "conversation_schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "naming_version": NAMING_VERSION,
        "reader_versions": reader_versions,
        "source_fingerprint": source_fingerprint(
            report, reader_versions=reader_versions, naming_version=NAMING_VERSION
        ),
        "layout_version": LAYOUT_VERSION,
        "providers": report,
        "totals": {
            "conversations": sum(info.get("conversations", 0) for info in report.values()),
            "messages": sum(info.get("messages", 0) for info in report.values()),
            "words": sum(info.get("words", 0) for info in report.values()),
            "authors": {
                author: sum(info.get("authors", {}).get(author, 0) for info in report.values())
                for author in AUTHORS
            },
        },
    }
    write_json_atomic(paths.manifest, manifest)
    return manifest


def source_fingerprint(
    providers: dict[str, dict[str, Any]],
    *,
    reader_versions: dict[str, str],
    naming_version: int,
) -> str:
    """SHA-256 over a canonical copy of the ingest result, with host-specific data removed.

    Absolute source paths (`source_root`) and exception text (`error`) never enter the hash, so
    the same export read on two hosts, from two different folders, gives the same fingerprint.
    Everything else counts: source file hashes, per-provider counts and counters, the reader
    versions, the schema version and the naming version.
    """
    canonical = {
        "naming_version": naming_version,
        "schema_version": SCHEMA_VERSION,
        "reader_versions": dict(sorted(reader_versions.items())),
        "providers": {
            name: {k: v for k, v in sorted(info.items()) if k not in FINGERPRINT_EXCLUDED_KEYS}
            for name, info in sorted(providers.items())
        },
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def iter_store(paths: ArchivePaths, providers: list[str] | None = None) -> Iterator[Conversation]:
    """Stream every normalized conversation in provider-registry then file order."""
    for name in providers or paths.providers_present():
        for path in paths.conversation_files(name):
            yield from iter_conversations(path)


def load_index(paths: ArchivePaths, providers: list[str] | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name in providers or paths.providers_present():
        for path in paths.index_files(name):
            rows.extend(iter_jsonl(path))
    return rows


def has_bounded_layout(manifest: dict[str, Any]) -> bool:
    """True for a manifest written by the bounded layout (shards, author counts, limits)."""
    return int(manifest.get("layout_version") or 0) >= LAYOUT_VERSION


def load_manifest(paths: ArchivePaths) -> dict[str, Any]:
    if not paths.manifest.is_file():
        raise FileNotFoundError(
            f"No archive manifest at {paths.manifest}; run `cce archive ingest`"
        )
    manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
    schema = manifest.get("schema")
    if schema is not None and schema not in ACCEPTED_MANIFEST_SCHEMAS:
        raise ValueError(f"unsupported archive manifest schema: {schema!r}")
    return manifest


class LegacyArchiveStore(ValueError):
    """The store was written by the first-generation normalized layer; it must be rebuilt."""


def require_current_store(manifest: dict[str, Any]) -> None:
    """Refuse a store that is not written in the current manifest and conversation schemas.

    A first-generation store is still readable (`load_manifest` and `verify` accept it), but
    loading it upgrades its rows on the fly: message ids are minted differently and every message
    counts as fully typed. A projection built from that would carry different message references
    than one built from a current store while its receipt bound to the same fingerprint, so the
    projections refuse it and ask for a rebuild.
    """
    found = (manifest.get("schema"), manifest.get("conversation_schema"))
    if found != (MANIFEST_SCHEMA, SCHEMA):
        raise LegacyArchiveStore(
            f"the archive was written in an older layout (manifest schema {found[0]!r}, "
            f"conversation schema {found[1]!r}; this engine writes {MANIFEST_SCHEMA!r} and "
            f"{SCHEMA!r}): rebuild it with `cce archive build`"
        )


def failed_providers(manifest: dict[str, Any]) -> list[str]:
    return [name for name, info in manifest["providers"].items() if info.get("status") == "failed"]
