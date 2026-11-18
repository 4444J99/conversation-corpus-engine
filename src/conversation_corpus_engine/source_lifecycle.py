#!/usr/bin/env python3
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .answering import load_json
from .paths import resolve_workspace_path
from .provider_exports import collect_supported_export_files

SUPPORTED_SOURCE_ADAPTERS = {
    "markdown-document",
    "markdown-transcript",
    "claude-export",
    "claude-local-session",
    "chatgpt-export",
    "chatgpt-local-session",
    "copilot-export",
    "gemini-export",
    "gemini-local-session",
    "grok-export",
    "perplexity-export",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def source_root_for_input(source_input: Path) -> Path:
    return source_input.parent if source_input.is_file() else source_input


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def transcript_attachment_roots(source_input: Path) -> list[Path]:
    if not source_input.exists():
        return []
    roots: list[Path] = []
    if source_input.is_file():
        candidate = source_input.parent / "Attachments"
        if candidate.is_dir():
            roots.append(candidate)
        return roots

    direct = source_input / "Attachments"
    if direct.is_dir():
        roots.append(direct)
    for candidate in sorted(source_input.rglob("Attachments")):
        if candidate.is_dir() and candidate not in roots:
            roots.append(candidate)
    return roots


def collect_source_files(
    source_input: Path, adapter_type: str, collection_scope: str | None
) -> list[Path]:
    source_input = resolve_workspace_path(source_input)
    if not source_input.exists():
        return []

    files: list[Path] = []
    if adapter_type == "markdown-document":
        if source_input.is_file():
            files = [source_input]
        elif collection_scope == "top-level":
            files = sorted(path for path in source_input.glob("*.md") if path.is_file())
        else:
            files = sorted(path for path in source_input.rglob("*.md") if path.is_file())
    elif adapter_type == "markdown-transcript":
        if source_input.is_file():
            files = [source_input]
        else:
            files = sorted(path for path in source_input.rglob("*.md") if path.is_file())
        for attachment_root in transcript_attachment_roots(source_input):
            files.extend(path for path in attachment_root.rglob("*") if path.is_file())
    elif adapter_type == "claude-export":
        if source_input.is_file():
            files = [source_input]
        else:
            files = sorted(path for path in source_input.rglob("*.json") if path.is_file())
    elif adapter_type == "claude-local-session":
        if source_input.is_file():
            files = [source_input]
        else:
            tracked_paths = [
                source_input / "Cookies",
                source_input / "config.json",
                source_input / "Preferences",
                source_input / "Local Storage" / "leveldb",
                source_input / "IndexedDB" / "https_claude.ai_0.indexeddb.leveldb",
                source_input / "Session Storage",
                source_input / "WebStorage" / "QuotaManager",
                source_input / "WebStorage" / "QuotaManager-journal",
            ]
            files = []
            for tracked in tracked_paths:
                if tracked.is_file():
                    files.append(tracked)
                elif tracked.is_dir():
                    files.extend(path for path in tracked.rglob("*") if path.is_file())
    elif adapter_type == "chatgpt-local-session":
        if source_input.is_file():
            files = [source_input]
        else:
            tracked_paths = [
                source_input / "com.openai.chat.binarycookies",
            ]
            files = [p for p in tracked_paths if p.is_file()]
    elif adapter_type == "gemini-local-session":
        if source_input.is_file():
            files = [source_input]
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(source_input) + suffix)
                if sidecar.is_file():
                    files.append(sidecar)
        else:
            tracked_paths = [
                source_input / "ChatInfo2.store",
                source_input / "ChatInfo2.store-wal",
                source_input / "ChatInfo2.store-shm",
                source_input / "Gemini" / "user1" / "ChatInfo2.store",
                source_input / "Gemini" / "user1" / "ChatInfo2.store-wal",
                source_input / "Gemini" / "user1" / "ChatInfo2.store-shm",
            ]
            files = [p for p in tracked_paths if p.is_file()]
    elif adapter_type == "chatgpt-export":
        if source_input.is_file():
            files = [source_input]
        else:
            files = sorted(path for path in source_input.rglob("*.json") if path.is_file())
    elif adapter_type in {"copilot-export", "gemini-export", "grok-export", "perplexity-export"}:
        files = collect_supported_export_files(source_input)
    else:
        return []

    deduped: dict[str, Path] = {}
    for path in files:
        deduped[str(path.resolve())] = path.resolve()
    return [deduped[key] for key in sorted(deduped)]


def build_source_signature(
    source_input: Path, adapter_type: str, collection_scope: str | None
) -> dict[str, Any]:
    source_input = resolve_workspace_path(source_input)
    if adapter_type not in SUPPORTED_SOURCE_ADAPTERS:
        return {
            "captured_at": now_iso(),
            "source_input": str(source_input),
            "adapter_type": adapter_type,
            "collection_scope": collection_scope,
            "supported": False,
            "exists": source_input.exists(),
            "root_base": str(source_root_for_input(source_input)),
            "file_count": 0,
            "total_bytes": 0,
            "latest_mtime_ns": None,
            "signature_fingerprint": "",
            "files": [],
        }
    if not source_input.exists():
        return {
            "captured_at": now_iso(),
            "source_input": str(source_input),
            "adapter_type": adapter_type,
            "collection_scope": collection_scope,
            "supported": True,
            "exists": False,
            "root_base": str(source_root_for_input(source_input)),
            "file_count": 0,
            "total_bytes": 0,
            "latest_mtime_ns": None,
            "signature_fingerprint": "",
            "files": [],
        }

    root_base = source_root_for_input(source_input)
    file_entries: list[dict[str, Any]] = []
    total_bytes = 0
    latest_mtime_ns = 0
    for path in collect_source_files(source_input, adapter_type, collection_scope):
        stat = path.stat()
        relative_path = str(path.relative_to(root_base))
        entry = {
            "relative_path": relative_path,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
        total_bytes += stat.st_size
        latest_mtime_ns = max(latest_mtime_ns, stat.st_mtime_ns)
        file_entries.append(entry)

    signature_input = "||".join(
        f"{entry['relative_path']}:{entry['size']}:{entry['mtime_ns']}" for entry in file_entries
    )
    signature_fingerprint = (
        hashlib.sha256(signature_input.encode("utf-8")).hexdigest()[:24] if file_entries else ""
    )
    return {
        "captured_at": now_iso(),
        "source_input": str(source_input),
        "adapter_type": adapter_type,
        "collection_scope": collection_scope,
        "supported": True,
        "exists": True,
        "root_base": str(root_base),
        "file_count": len(file_entries),
        "total_bytes": total_bytes,
        "latest_mtime_ns": latest_mtime_ns or None,
        "signature_fingerprint": signature_fingerprint,
        "files": file_entries,
    }


def build_source_snapshot(
    source_input: Path, adapter_type: str, collection_scope: str | None
) -> dict[str, Any]:
    signature = build_source_signature(source_input, adapter_type, collection_scope)
    snapshot = dict(signature)
    files = snapshot.get("files") or []
    if not snapshot.get("exists") or not snapshot.get("supported"):
        snapshot["content_fingerprint"] = ""
        snapshot["fingerprint"] = ""
        return snapshot

    root_base = Path(snapshot["root_base"])
    content_entries: list[dict[str, Any]] = []
    for entry in files:
        path = root_base / entry["relative_path"]
        content_entries.append({**entry, "sha256": file_sha256(path)})
    fingerprint_input = "||".join(
        f"{entry['relative_path']}:{entry['size']}:{entry['mtime_ns']}:{entry['sha256']}"
        for entry in content_entries
    )
    content_fingerprint = (
        hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest()[:24]
        if content_entries
        else ""
    )
    snapshot["files"] = content_entries
    snapshot["content_fingerprint"] = content_fingerprint
    snapshot["fingerprint"] = content_fingerprint
    return snapshot


def load_source_snapshot(corpus_root: Path) -> dict[str, Any]:
    return load_json(corpus_root / "corpus" / "source-snapshot.json", default={}) or {}


def compute_source_freshness(corpus_root: Path) -> dict[str, Any]:
    contract = load_json(corpus_root / "corpus" / "contract.json", default={}) or {}
    adapter_type = contract.get("adapter_type")
    source_input_value = contract.get("source_input")
    collection_scope = contract.get("collection_scope")
    if adapter_type not in SUPPORTED_SOURCE_ADAPTERS or not source_input_value:
        return {
            "state": "not_applicable",
            "needs_refresh": False,
            "can_refresh": False,
            "source_input": source_input_value,
            "adapter_type": adapter_type,
            "collection_scope": collection_scope,
            "note": "Corpus is not importer-managed or does not expose a refreshable source input.",
        }

    source_input = resolve_workspace_path(Path(source_input_value))
    stored_snapshot = load_source_snapshot(corpus_root)
    current_signature = build_source_signature(source_input, adapter_type, collection_scope)
    if not current_signature.get("exists"):
        return {
            "state": "missing_source",
            "needs_refresh": False,
            "can_refresh": False,
            "source_input": str(source_input),
            "adapter_type": adapter_type,
            "collection_scope": collection_scope,
            "stored_signature_fingerprint": stored_snapshot.get("signature_fingerprint")
            or contract.get("source_signature_fingerprint"),
            "current_signature_fingerprint": "",
            "note": "The recorded source input does not currently exist.",
        }

    if not stored_snapshot:
        return {
            "state": "missing_snapshot",
            "needs_refresh": True,
            "can_refresh": True,
            "source_input": str(source_input),
            "adapter_type": adapter_type,
            "collection_scope": collection_scope,
            "stored_signature_fingerprint": "",
            "current_signature_fingerprint": current_signature.get("signature_fingerprint"),
            "note": "The corpus has no recorded source snapshot and should be refreshed once.",
        }

    stored_signature = (
        stored_snapshot.get("signature_fingerprint")
        or contract.get("source_signature_fingerprint")
        or ""
    )
    current_fingerprint = current_signature.get("signature_fingerprint") or ""
    if stored_signature == current_fingerprint:
        return {
            "state": "fresh",
            "needs_refresh": False,
            "can_refresh": True,
            "source_input": str(source_input),
            "adapter_type": adapter_type,
            "collection_scope": collection_scope,
            "stored_signature_fingerprint": stored_signature,
            "current_signature_fingerprint": current_fingerprint,
            "note": "Stored source snapshot matches the current source input.",
        }

    return {
        "state": "stale",
        "needs_refresh": True,
        "can_refresh": True,
        "source_input": str(source_input),
        "adapter_type": adapter_type,
        "collection_scope": collection_scope,
        "stored_signature_fingerprint": stored_signature,
        "current_signature_fingerprint": current_fingerprint,
        "note": "Source input has changed since the last recorded snapshot.",
    }
