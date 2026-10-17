from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import (
    archive_read_chatgpt,
    archive_read_claude,
    archive_read_gemini,
    archive_read_grok,
    archive_read_notebooklm,
    archive_read_perplexity,
)
from .archive_model import (
    Conversation,
    SourceTracker,
    iter_conversations,
    iter_jsonl,
    write_json_atomic,
)
from .corpus_store import CorpusStoreError, CorpusWriteAuthorization, authorize_corpus_write

MANIFEST_SCHEMA = "cce.archive.manifest.v1"
DEFAULT_ARCHIVE_DIRNAME = "_archive"


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
    root: Path

    @property
    def normalized(self) -> Path:
        return self.root / "normalized"

    @property
    def analysis(self) -> Path:
        return self.root / "analysis"

    @property
    def vault(self) -> Path:
        return self.root / "vault"

    @property
    def site(self) -> Path:
        return self.root / "site"

    @property
    def kb(self) -> Path:
        return self.root / "kb"

    @property
    def graph(self) -> Path:
        return self.root / "graph"

    @property
    def manifest(self) -> Path:
        return self.root / "manifest.json"

    def conversations(self, provider: str) -> Path:
        return self.normalized / provider / "conversations.jsonl"

    def index(self, provider: str) -> Path:
        return self.normalized / provider / "index.jsonl"

    def providers_present(self) -> list[str]:
        return [name for name in PROVIDER_SPECS if self.conversations(name).is_file()]


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
    seen: set[str] = set()
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
                uid = conversation.uid
                if uid in seen:
                    _bump(totals, "duplicates_skipped")
                    continue
                seen.add(uid)
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
        "duplicates_skipped": totals["duplicates_skipped"],
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
    fingerprint = hashlib.sha256(
        json.dumps(
            {name: info.get("source_files", {}) for name, info in sorted(report.items())},
            sort_keys=True,
        ).encode()
    ).hexdigest()
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "conversation_schema": "cce.archive.conversation.v1",
        "source_fingerprint": fingerprint,
        "providers": report,
        "totals": {
            "conversations": sum(info.get("conversations", 0) for info in report.values()),
            "messages": sum(info.get("messages", 0) for info in report.values()),
            "words": sum(info.get("words", 0) for info in report.values()),
        },
    }
    write_json_atomic(paths.manifest, manifest)
    write_json_atomic(
        paths.root / "run.json",
        {"finished_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "stage": "ingest"},
    )
    return manifest


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
    return json.loads(paths.manifest.read_text(encoding="utf-8"))


def failed_providers(manifest: dict[str, Any]) -> list[str]:
    return [name for name, info in manifest["providers"].items() if info.get("status") == "failed"]
