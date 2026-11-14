"""Prepare source-bound project drafts without modifying a vault or library.

These drafts preserve historical narrative and uncertainty. Their stable logical
IDs identify source files, not canonical projects or an adopted project version.
The submitting agent, rather than this adapter, declares its import authorship.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .archive_model import Message
from .archive_sealed import safe_path
from .archive_sealed_crypto import address
from .archive_text import FenceTracker
from .archive_vault import render_message
from .attribution import text_digest
from .knovv_projection import PAGE_CHARS
from .library_store import Library, digest_json

MAX_PROJECT_BYTES = 64 * 1024
MAX_TARGET_BYTES = 2 * 1024 * 1024
MAX_PROJECTS = 128
MAX_LINKS = 256
MAX_REVISIONS = 128
MAX_RECORD_CHARS = 16 * 1024 * 1024
UID = re.compile(r"[a-z0-9][a-z0-9-]{0,199}\Z")
HEX = re.compile(r"[0-9a-f]{64}\Z")
LINK = re.compile(r"\[\[([^\[\]\n]+)\]\]")
HEADING = re.compile(r"(User|Assistant|System|Tool) · (\d{4}-\d{2}-\d{2} \d{2}:\d{2})\Z")
FOOTER = re.compile(
    r"The cited conversation's CCE identity is\s*"
    r"`(?P<uid>[a-z0-9][a-z0-9-]{0,199})`, revision `(?P<version>\d+\.\d+\.\d+)`, "
    r"normalized\s+digest `(?P<digest>[0-9a-f]{64})`\."
)


def _read(path: Path, limit: int) -> tuple[bytes, os.stat_result]:
    safe_path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("project-input-not-regular-file")
        if before.st_size > limit:
            raise ValueError("project-input-byte-bound")
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if len(raw) > limit:
        raise ValueError("project-input-byte-bound")
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise ValueError("project-input-changed-during-read")
    return raw, before


def _metadata(text: str) -> dict[str, str]:
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ValueError("project-target-frontmatter-required")
    metadata = {}
    for line in text[4:].split("\n---\n", 1)[0].splitlines():
        key, separator, value = line.partition(":")
        if not separator or key not in {"uid", "source_sha256"}:
            continue
        if key in metadata:
            raise ValueError("project-target-duplicate-metadata")
        value = value.strip()
        if value.startswith('"'):
            value = json.loads(value)
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        metadata[key] = value
    if (
        not isinstance(metadata.get("uid"), str)
        or not UID.fullmatch(metadata["uid"])
        or not isinstance(metadata.get("source_sha256"), str)
        or not HEX.fullmatch(metadata["source_sha256"])
    ):
        raise ValueError("project-target-source-metadata-required")
    return metadata


def _declarations(text: str) -> dict[str, dict[str, str]]:
    declarations = {}
    matches = list(FOOTER.finditer(text))
    if not matches or len(matches) != text.count("The cited conversation's CCE identity is"):
        raise ValueError("project-declared-revision-required")
    for match in matches:
        value = match.groupdict()
        if value["uid"] in declarations:
            raise ValueError("project-duplicate-declared-revision")
        declarations[value["uid"]] = value
    return declarations


def _target(vault_root: Path, name: str) -> Path:
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in name.split("/"))
        or any(character in name for character in ("\\", "%", ":"))
        or any(ord(character) < 32 for character in name)
    ):
        raise ValueError("project-target-path-refused")
    target = vault_root / (name if name.endswith(".md") else name + ".md")
    safe_path(target)
    if not target.is_relative_to(vault_root):
        raise ValueError("project-target-path-refused")
    return target


def _revision(
    library: Library,
    metadata: dict[str, str],
    declared: dict[str, str] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    history = library.history(metadata["uid"])
    if len(history) > MAX_REVISIONS:
        raise ValueError("project-source-revision-bound")
    if declared:
        history = [row for row in history if row["version"] == declared["version"]]
        if len(history) != 1 or history[0]["digest"] != declared["digest"]:
            raise ValueError("project-declared-revision-mismatch")
    candidates = []
    for row in history:
        record = library.record(metadata["uid"], version=row["version"])
        if len(json.dumps(record)) > MAX_RECORD_CHARS:
            raise ValueError("project-source-record-bound")
        if digest_json(record) != row["digest"]:
            raise ValueError("project-source-normalized-digest-mismatch")
        if record.get("provenance", {}).get("source_sha256") == metadata["source_sha256"]:
            candidates.append((row, record))
    if not candidates:
        raise ValueError("project-source-revision-mismatch")
    if len(candidates) != 1:
        raise ValueError("project-ambiguous-revision")
    return candidates[0]


def _minute(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError):
        return ""
    if parsed.tzinfo is None:
        return ""
    return parsed.astimezone(UTC).strftime("%Y-%m-%d %H:%M")


def _section(target_text: str, heading: str) -> str:
    """Select one generated heading outside code, retaining its entire block."""
    lines = target_text.splitlines(keepends=True)
    boundaries = []
    matching = []
    tracker = FenceTracker()
    for index, line in enumerate(lines):
        if tracker.feed(line):
            continue
        if line.startswith(("### ", "> [!quote]- ")):
            boundaries.append(index)
        if line.rstrip("\r\n") == "### " + heading:
            matching.append(index)
    if len(matching) != 1:
        raise ValueError("project-target-heading-not-unique")
    start = matching[0]
    end = next((index for index in boundaries if index > start), len(lines))
    return "".join(lines[start:end]).rstrip("\n")


def _message(
    record: dict[str, Any],
    target_text: str,
    heading: str,
) -> tuple[int, str, dict[str, str]]:
    match = HEADING.fullmatch(heading)
    if not match:
        raise ValueError("project-wikilink-heading-invalid")
    section = _section(target_text, heading)
    role, minute = match.groups()
    matches = [
        (index, message)
        for index, message in enumerate(record.get("messages", []))
        if message.get("role", "").casefold() == role.casefold()
        and _minute(message.get("ts", "")) == minute
        and isinstance(message.get("text"), str)
    ]
    binding = {"method": "unique-role-utc-minute"}
    if len(matches) > 1:
        exact = []
        for index, message in matches:
            rendered = render_message(Message.from_dict(message), MAX_TARGET_BYTES)
            if len(rendered) == 1 and rendered[0].rstrip("\n") == section:
                exact.append((index, message))
        matches = exact
        binding = {
            "method": "unique-rendered-section",
            "renderer": "archive_vault.render_message",
            "rendered_section_sha256": hashlib.sha256(section.encode()).hexdigest(),
        }
    if len(matches) != 1:
        raise ValueError("project-ambiguous-message")
    index, message = matches[0]
    return index, message["text"], binding


def _draft(library: Library, vault: Path, path: Path, id_key: bytes) -> dict[str, Any]:
    raw, observed = _read(path, MAX_PROJECT_BYTES)
    text = raw.decode("utf-8")
    declarations = _declarations(text)
    matches = list(LINK.finditer(text))
    if len(matches) > MAX_LINKS:
        raise ValueError("project-wikilink-bound")
    if not matches or len(matches) != text.count("[[") or len(matches) != text.count("]]"):
        raise ValueError("project-wikilink-invalid")
    sources: dict[str, dict[str, Any]] = {}
    revisions: dict[tuple[str, str, str], dict[str, Any]] = {}
    bindings: dict[str, dict[str, Any]] = {}
    targets = {}
    resolved_uids = set()

    def replace(match: re.Match[str]) -> str:
        reference, _, label = match.group(1).partition("|")
        name, separator, heading = reference.partition("#")
        if not separator or "#" in heading or "|" in label:
            raise ValueError("project-wikilink-invalid")
        target = _target(vault, name)
        if target not in targets:
            target_raw, _ = _read(target, MAX_TARGET_BYTES)
            target_text = target_raw.decode("utf-8")
            metadata = _metadata(target_text)
            row, record = _revision(library, metadata, declarations.get(metadata["uid"]))
            targets[target] = (target_raw, target_text, metadata, row, record)
        target_raw, target_text, metadata, row, record = targets[target]
        index, message, binding = _message(record, target_text, heading)
        uid, version = metadata["uid"], row["version"]
        resolved_uids.add(uid)
        identity = address(id_key, f"cce:{uid}@{version}#main:{index}:0".encode())
        sources[identity] = {
            "uid": uid,
            "version": version,
            "sequence": "main",
            "message": index,
            "digest": text_digest(message),
            "start": 0,
            "end": min(PAGE_CHARS, len(message)),
        }
        relative = target.relative_to(vault).as_posix()
        bindings[identity] = {
            **binding,
            "uid": uid,
            "version": version,
            "message": index,
            "whole_message_digest": sources[identity]["digest"],
            "target_note": relative,
            "target_heading": heading,
        }
        revisions[(uid, version, relative)] = {
            "uid": uid,
            "version": version,
            "normalized_digest": row["digest"],
            "source_sha256": metadata["source_sha256"],
            "target_note": relative,
            "target_note_sha256": hashlib.sha256(target_raw).hexdigest(),
            "revision_basis": "declared-footer" if uid in declarations else "unique-source-hash",
        }
        return f"[{label or heading}](#/document/{identity})"

    body = LINK.sub(replace, text)
    if set(declarations) - resolved_uids:
        raise ValueError("project-declared-revision-not-cited")
    relative = path.relative_to(vault).as_posix()
    source_hash = hashlib.sha256(raw).hexdigest()
    modified = datetime.fromtimestamp(observed.st_mtime, UTC).isoformat().replace("+00:00", "Z")
    provenance = {
        "schema": "knovv-project-draft-source.v1",
        "source_file": relative,
        "source_sha256": source_hash,
        "source_modified_at": modified,
        "prepared_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source_authorship": {"author": "unknown", "basis": "unverified"},
        "revisions": list(revisions.values()),
        "citation_bindings": list(bindings.values()),
        "project_identity_basis": "source-file-only",
        "project_semver_adoption": "not-established",
    }
    body += (
        "\n\n---\n\n## Import provenance\n\n"
        f"Source snapshot: `{relative}`.\n\nSHA-256: `{source_hash}`.\n\n"
        f"Source file modification time: `{modified}` (filesystem observation, not authorship time).\n\n"
        f"Draft prepared at: `{provenance['prepared_at']}`.\n\n"
        "Source authorship: unknown; attribution is unverified. The submitting agent declares "
        "its own import authorship. This draft retains the source snapshot's uncertainty; "
        "its file identity does not establish canonical project identity or project SemVer adoption.\n"
    )
    title = next(
        (line[2:].strip() for line in text.splitlines() if line.startswith("# ")), path.stem
    )
    return {
        "kind": "project",
        "status": "draft",
        "title": title,
        "logical_id": "project-snapshot:" + relative,
        "body": body,
        "sources": list(sources.values()),
        "links": list(sources),
        "provenance": provenance,
    }


def prepare_project_drafts(
    library: Library,
    vault_root: Path,
    projects_root: Path,
    id_key: bytes,
    *,
    project_paths: list[Path] | None = None,
) -> list[dict[str, Any]]:
    """Return agent-submittable drafts for the directory's direct Markdown snapshots.

    All target paths are exact vault-relative paths. A footer pins its declared
    revision; unpinned conversations require exactly one revision with the raw
    source hash. A citation requires a unique role and UTC minute in that revision,
    or one exact rendered-content match to a unique target heading. Optional
    explicit paths select direct Markdown children of the project directory.
    Nothing is written, and an invalid snapshot prevents returning a partial batch.
    """
    vault = safe_path(vault_root)
    projects = safe_path(projects_root)
    if not vault.is_dir() or not projects.is_dir() or not projects.is_relative_to(vault):
        raise ValueError("project-root-path-refused")
    if not isinstance(id_key, bytes) or len(id_key) != 32:
        raise ValueError("project-address-key-invalid")
    if project_paths is not None and len(project_paths) > MAX_PROJECTS:
        raise ValueError("project-count-bound")
    files = []
    for candidate in projects.iterdir() if project_paths is None else project_paths:
        path = safe_path(candidate)
        if path.parent != projects or (project_paths is not None and path.suffix != ".md"):
            raise ValueError("project-selection-path-refused")
        if path.suffix == ".md":
            if path in files:
                raise ValueError("project-selection-duplicate")
            files.append(path)
            if len(files) > MAX_PROJECTS:
                raise ValueError("project-count-bound")
    return [_draft(library, vault, path, id_key) for path in sorted(files)]
