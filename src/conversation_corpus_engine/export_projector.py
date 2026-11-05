"""Route a legacy provider export bundle through the archive corpus projector.

The legacy export importers used to shape corpora by hand (slug ids, their own
branch/channel rules), duplicating the shaping the archive projector already does
from the normalized layer. This module is the single rewire point (see
conversation-corpus-engine#78): a bundle is merged, ingested into a temporary
normalized store, projected with :func:`archive_corpus.project_corpus`, and the
projected corpus is adopted verbatim as the importer's ``corpus/`` output, so
the importer and the projector can no longer disagree on contracts or member
ids. The temporary store and projection scratch live under the output root and
are removed once the corpus is adopted.

The importer's own envelope (copied sources, import manifest, README, result
dict) is the caller's job; this module only owns the corpus shaping.
"""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .answering import load_json, write_json
from .archive_corpus import (
    ADAPTER_TYPE,
    ALL_COLLECTIONS,
    COLLECTION_SCOPE,
    CONTRACT_NAME,
    CONTRACT_VERSION,
    EVALUATION_NOTE,
    CorpusProjectionError,
    project_corpus,
)
from .archive_ingest import (
    DEFAULT_ARCHIVE_DIRNAME,
    ArchivePaths,
    authorize_archive,
    ingest,
)
from .archive_layout import corpus_dirname
from .archive_naming import NAMING_VERSION
from .corpus_store import (
    authorize_additional_corpus_path,
    register_corpus_store,
)
from .sharded_collection import (
    collection_storage_path,
    load_collection,
    load_corpus_collection,
    write_corpus_collection,
)

if TYPE_CHECKING:
    from .corpus_store import CorpusWriteAuthorization

_CONVERSATION_KEY = {"chatgpt": ("conversation_id", "id"), "claude": ("uuid",)}


def _conversation_key(provider: str, conversation: dict[str, Any]) -> str | None:
    for field in _CONVERSATION_KEY[provider]:
        value = conversation.get(field)
        if value:
            return str(value)
    return None


def _is_conversations_storage(part_root: Path, path: Path) -> bool:
    storage = collection_storage_path(part_root / "conversations.json")
    return path == part_root / "conversations.json" or path == storage or storage in path.parents


def _merge_zip(destination: Path, source: Path) -> None:
    """Union the members of ``source`` into ``destination`` (first member wins)."""
    members: dict[str, bytes] = {}
    if destination.exists():
        with zipfile.ZipFile(destination) as archive:
            for info in archive.infolist():
                members[info.filename] = archive.read(info.filename)
    with zipfile.ZipFile(source) as archive:
        for info in archive.infolist():
            members.setdefault(info.filename, archive.read(info.filename))
    tmp = destination.with_suffix(".merging")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in sorted(members):
            archive.writestr(name, members[name])
    tmp.replace(destination)


def merge_bundle_roots(bundle_roots: list[Path], provider: str, workdir: Path) -> tuple[Path, int]:
    """Merge one or more bundle roots into one ingestible bundle directory.

    Conversations are concatenated in part order with the importers' old rule:
    a later conversation whose id was already seen is skipped and counted.
    Every other file comes from the first part that has it; zip members are
    unioned across parts. Returns the merged root and the duplicate count.
    """
    merged = workdir / "bundle"
    merged.mkdir(parents=True, exist_ok=True)
    conversations: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicates = 0
    for part_root in bundle_roots:
        for conversation in load_collection(part_root / "conversations.json", default=[]) or []:
            key = (
                _conversation_key(provider, conversation)
                if isinstance(conversation, dict)
                else None
            )
            if key and key in seen:
                duplicates += 1
                continue
            if key:
                seen.add(key)
            conversations.append(conversation)
    (merged / "conversations.json").write_text(
        json.dumps(conversations, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    for part_root in bundle_roots:
        for path in sorted(part_root.rglob("*")):
            if not path.is_file() or _is_conversations_storage(part_root, path):
                continue
            relative = path.relative_to(part_root)
            destination = merged / relative
            if relative.suffix == ".zip" and destination.exists():
                _merge_zip(destination, path)
            elif not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
    return merged, duplicates


def _empty_corpus(corpus_dir: Path, provider: str, manifest: dict[str, Any]) -> dict[str, Any]:
    """Write the projector-shaped surface for a provider with no eligible rows."""
    corpus_dir.mkdir(parents=True, exist_ok=True)
    for filename in ALL_COLLECTIONS:
        write_corpus_collection(corpus_dir / filename, [])
    info = manifest["providers"][provider]
    generated_at = info.get("last_activity_max") or ""
    contract = {
        "contract_name": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "adapter_type": ADAPTER_TYPE,
        "corpus_id": corpus_dirname(provider),
        "name": f"{provider} archive",
        "generated_at": generated_at,
        "required_files": [
            f"corpus/{name.removesuffix('.json')}.collection"
            for name in (
                "threads-index.json",
                "semantic-v3-index.json",
                "pairs-index.json",
                "doctrine-briefs.json",
                "family-dossiers.json",
            )
        ],
        "counts": {
            "threads": 0,
            "families": 0,
            "pairs": 0,
            "excluded_conversations": 0,
            "actions": 0,
            "unresolved": 0,
            "entities": 0,
        },
        "source_input": None,
        "collection_scope": COLLECTION_SCOPE,
        "source_snapshot_path": None,
        "source_signature_fingerprint": None,
        "source_content_fingerprint": manifest.get("source_fingerprint"),
        "source_file_count": None,
        "source_total_bytes": None,
        "source_latest_mtime_ns": None,
        "naming_version": NAMING_VERSION,
        "archive_schema_version": manifest.get("schema_version"),
        "reader_version": manifest.get("reader_versions", {}).get(provider),
    }
    write_json(corpus_dir / "contract.json", contract)
    write_json(
        corpus_dir / "evaluation-summary.json",
        {
            "generated_at": generated_at,
            "fixture_sources": {"manual": {"source": "unavailable", "count": 0}},
            "regression_gates": {"overall_state": "warn", "source_reliability_state": "warn"},
            "notes": [EVALUATION_NOTE],
        },
    )
    write_json(
        corpus_dir / "regression-gates.json",
        {
            "generated_at": generated_at,
            "overall_state": "warn",
            "source_reliability_state": "warn",
            "source_notes": [EVALUATION_NOTE],
            "gates": [],
        },
    )
    return contract


def project_export_bundle(
    provider: str,
    bundle_roots: list[Path],
    output_root: Path,
    *,
    authorization: CorpusWriteAuthorization | None = None,
) -> dict[str, Any]:
    """Project ``bundle_roots`` and adopt the result as ``output_root/corpus``.

    Returns the projector summary for the provider, its contract, and the
    duplicate count from merging the bundle parts.
    """
    output_root.mkdir(parents=True, exist_ok=True)
    work = output_root / ".export-projector"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        merged, duplicates = merge_bundle_roots(bundle_roots, provider, work)
        if authorization is not None:
            store_dir = work / "store"
            archive_auth = authorize_additional_corpus_path(authorization, store_dir)
            paths = ArchivePaths(archive_auth.destination)
        else:
            project_dir = work / "project"
            project_dir.mkdir()
            store_root = work / "store-root"
            store_root.mkdir()
            register_corpus_store(project_dir, store_root)
            archive_auth = authorize_archive(
                project_root=project_dir,
                archive_root=store_root / DEFAULT_ARCHIVE_DIRNAME,
                corpus_store_root=store_root,
                source_roots=(merged,),
            )
            paths = ArchivePaths(archive_auth.destination)
        manifest = ingest(
            export_root=output_root,
            paths=paths,
            authorization=archive_auth,
            providers=[provider],
            overrides={provider: merged},
        )
        info = manifest["providers"][provider]
        if info.get("status") != "ok":
            raise CorpusProjectionError(
                f"the {provider} export did not ingest: {info.get('error') or info.get('status')}"
            )
        projected_out = work / "projected"
        summary = project_corpus(paths.root, projected_out, providers=[provider])
        provider_summary = summary[provider]
        corpus_dir = output_root / "corpus"
        if provider_summary.get("status") == "empty":
            shutil.rmtree(corpus_dir, ignore_errors=True)
            contract = _empty_corpus(corpus_dir, provider, manifest)
            counts = dict(contract["counts"])
            counts["excluded_conversations"] = sum(provider_summary.get("excluded", {}).values())
        else:
            projected_corpus = Path(provider_summary["root"]) / "corpus"
            shutil.rmtree(corpus_dir, ignore_errors=True)
            shutil.copytree(projected_corpus, corpus_dir)
            contract = load_json(corpus_dir / "contract.json", default={}) or {}
            counts = dict(contract.get("counts", {}))
        return {
            "provider": provider,
            "summary": provider_summary,
            "contract": contract,
            "counts": counts,
            "corpus_dir": corpus_dir,
            "duplicate_conversations_skipped": duplicates,
            "conversations_seen": provider_summary.get("conversations_seen", 0),
            "excluded": provider_summary.get("excluded", {}),
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


def projected_rows(corpus_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """Load every projected collection by filename (empty list when absent)."""
    return {
        name: load_corpus_collection(corpus_dir / name, default=[]) or []
        for name in ALL_COLLECTIONS
    }
