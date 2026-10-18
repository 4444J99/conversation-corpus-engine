from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import archive_layout as layout
from . import (
    archive_read_chatgpt,
    archive_read_claude,
    archive_read_gemini,
    archive_read_grok,
    archive_read_notebooklm,
    archive_read_perplexity,
)
from .archive_model import (
    SCHEMA,
    Conversation,
    SourceTracker,
    finalize_conversation,
    iter_conversations,
    iter_jsonl,
    write_json_atomic,
)
from .archive_naming import NAMING_VERSION, day_of
from .corpus_store import CorpusStoreError, CorpusWriteAuthorization, authorize_corpus_write

MANIFEST_SCHEMA = "archive-manifest.v2"
LEGACY_MANIFEST_SCHEMA = "cce.archive.manifest.v1"
ACCEPTED_MANIFEST_SCHEMAS = frozenset({MANIFEST_SCHEMA, LEGACY_MANIFEST_SCHEMA})
SCHEMA_VERSION = 2
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
        return self.provider_dir(provider) / layout.conversations_filename(provider)

    def index(self, provider: str) -> Path:
        return self.provider_dir(provider) / layout.index_filename(provider)

    def feed(self, provider: str) -> Path:
        return self.root / layout.feed_filename(provider)

    def receipt(self, provider: str, fingerprint: str, day: str) -> Path:
        return self.root / layout.receipt_filename(provider, fingerprint, day)

    def providers_present(self) -> list[str]:
        return [name for name in PROVIDER_SPECS if self.conversations(name).is_file()]

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
        return self.site_pages / f"{layout.conversation_note(provider, uid)}.html"


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
    totals: Counter[str] = Counter()
    created: list[str] = []
    activity: list[str] = []
    conv_path = paths.conversations(spec.name)
    index_path = paths.index(spec.name)
    conv_path.parent.mkdir(parents=True, exist_ok=True)
    temp_conv = conv_path.with_name(f".{conv_path.name}.tmp")
    temp_index = index_path.with_name(f".{index_path.name}.tmp")
    error = ""
    try:
        with (
            temp_conv.open("w", encoding="utf-8") as conv_out,
            temp_index.open("w", encoding="utf-8") as index_out,
        ):
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
                conv_out.write(
                    json.dumps(conversation.to_dict(), sort_keys=True, ensure_ascii=False)
                )
                conv_out.write("\n")
                index_out.write(
                    json.dumps(conversation.index_row(), sort_keys=True, ensure_ascii=False)
                )
                index_out.write("\n")
                _bump(totals, "conversations")
                _bump(kinds, conversation.kind)
                if not conversation.messages:
                    _bump(totals, "empty_conversations")
                _bump(totals, "messages", conversation.message_count)
                _bump(totals, "words", conversation.word_count)
                _bump(totals, "attachments", conversation.attachment_count)
                for message in conversation.messages:
                    _bump(roles, message.role)
                if conversation.created:
                    created.append(conversation.created)
                if conversation.last_activity:
                    activity.append(conversation.last_activity)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
        error = f"{type(exc).__name__}: {exc}"
    if error:
        temp_conv.unlink(missing_ok=True)
        temp_index.unlink(missing_ok=True)
        return {
            "status": "failed",
            "error": error,
            "source_root": str(root),
            "channels": spec.module.channels(root),
        }
    temp_conv.replace(conv_path)
    temp_index.replace(index_path)
    return {
        "status": "ok",
        "source_root": str(root),
        "conversations": totals["conversations"],
        "empty_conversations": totals["empty_conversations"],
        "derived_ids": totals["derived_ids"],
        "duplicates_folded": totals["duplicates_folded"],
        "duplicates_folded_identical": totals["duplicates_folded_identical"],
        "duplicates_folded_differing": totals["duplicates_folded_differing"],
        "messages": totals["messages"],
        "words": totals["words"],
        "attachments": totals["attachments"],
        "kinds": dict(sorted(kinds.items())),
        "roles": dict(sorted(roles.items())),
        "created_min": min(created) if created else "",
        "last_activity_max": max(activity) if activity else "",
        "reader": spec.module.READER,
        "reader_counters": dict(sorted(tracker.counters.items())),
        "channels": spec.module.channels(root),
        "source_files": tracker.hashes(),
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
        "providers": report,
        "totals": {
            "conversations": sum(info.get("conversations", 0) for info in report.values()),
            "messages": sum(info.get("messages", 0) for info in report.values()),
            "words": sum(info.get("words", 0) for info in report.values()),
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
        path = paths.conversations(name)
        if path.is_file():
            yield from iter_conversations(path)


def load_index(paths: ArchivePaths, providers: list[str] | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name in providers or paths.providers_present():
        path = paths.index(name)
        if path.is_file():
            rows.extend(iter_jsonl(path))
    return rows


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
