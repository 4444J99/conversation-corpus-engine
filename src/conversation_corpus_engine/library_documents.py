"""Explicit, read-only document sources for the unified private library.

Source ids and relative paths define identity; content hashes define observations.
Copies at different paths retain distinct provenance. Renames are new identities
until a source manifest explicitly records their relationship. No directory is
implicitly selected, and symlink targets are never followed.
"""

from __future__ import annotations

import fnmatch
import hashlib
import io
import mimetypes
import os
import shutil
import stat
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .archive_model import Conversation, Message, to_iso

READER_VERSION = "library-documents@1.0.0"
DEFAULT_MAX_BYTES = 20 * 1024 * 1024
TEXT_SUFFIXES = frozenset(
    {".md", ".markdown", ".txt", ".rst", ".json", ".jsonl", ".tex", ".yaml", ".yml", ".org"}
)
WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _matches(relative: str, pattern: str) -> bool:
    """Path-aware globs, including zero directories for **/*.md."""
    parts, tokens = relative.strip("/").split("/"), pattern.strip("/").split("/")

    def walk(path_index: int, token_index: int) -> bool:
        if token_index == len(tokens):
            return path_index == len(parts)
        if tokens[token_index] == "**":
            return walk(path_index, token_index + 1) or (
                path_index < len(parts) and walk(path_index + 1, token_index)
            )
        return (
            path_index < len(parts)
            and fnmatch.fnmatchcase(parts[path_index], tokens[token_index])
            and walk(path_index + 1, token_index + 1)
        )

    return walk(0, 0)


def _validate_sources(sources: list[dict]) -> None:
    seen: set[str] = set()
    for source in sources:
        if (
            not isinstance(source, dict)
            or not isinstance(source.get("id"), str)
            or not source["id"].strip()
        ):
            raise ValueError("document source requires a nonempty id")
        if source["id"] in seen:
            raise ValueError(f"duplicate document source id: {source['id']}")
        seen.add(source["id"])
        if not isinstance(source.get("root"), str) or not source["root"].strip():
            raise ValueError(f"source {source['id']} requires an explicit root")
        for key in ("include", "exclude"):
            values = source.get(key, ["**/*"] if key == "include" else [])
            if not isinstance(values, list) or any(
                not isinstance(item, str)
                or not item.strip()
                or item.startswith("/")
                or ".." in item.split("/")
                for item in values
            ):
                raise ValueError(f"source {source['id']} has invalid {key} globs")
        maximum = source.get("max_bytes", DEFAULT_MAX_BYTES)
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum <= 0:
            raise ValueError(f"source {source['id']} max_bytes must be a positive integer")
        for key, allowed, default in (
            ("wing", {"authored", "ai"}, "authored"),
            ("lane", {"research", "creative", "writing", "support"}, "writing"),
            ("kind", {"document", "plan", "memory", "artifact"}, "document"),
        ):
            if source.get(key, default) not in allowed:
                raise ValueError(f"source {source['id']} has invalid {key}")


def _finding(report: dict, source_id: str, path: str, code: str, detail: str = "") -> None:
    report["findings"].append(
        {"source_id": source_id, "path": path, "code": code, "detail": detail}
    )


def _safe_bytes(path: Path, expected: os.stat_result, maximum: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        current = os.fstat(handle.fileno())
        if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != (
            expected.st_dev,
            expected.st_ino,
        ):
            raise ValueError("source changed before read")
        data = handle.read(maximum + 1)
        after = os.fstat(handle.fileno())
        if len(data) > maximum:
            raise ValueError("source exceeded byte limit during read")
        if (current.st_size, current.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("source changed during read")
        return data


def _docx_text(data: bytes, maximum: int) -> tuple[str, dict[str, Any]]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = ["word/document.xml"]
        names.extend(
            sorted(
                name
                for name in archive.namelist()
                if name.startswith("word/")
                and name.endswith(".xml")
                and (
                    Path(name).name.startswith(("header", "footer"))
                    or name in {"word/footnotes.xml", "word/endnotes.xml", "word/comments.xml"}
                )
            )
        )
        if sum(archive.getinfo(name).file_size for name in names) > maximum:
            raise ValueError("DOCX expanded XML exceeds byte limit")
        paragraphs: list[str] = []
        for name in names:
            xml = archive.read(name)
            if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
                raise ValueError("DOCX contains unsupported XML declarations")
            document = ET.fromstring(xml)
            for paragraph in document.iter(f"{WORD_NS}p"):
                pieces: list[str] = []
                for element in paragraph.iter():
                    if element.tag == f"{WORD_NS}t":
                        pieces.append(element.text or "")
                    elif element.tag == f"{WORD_NS}tab":
                        pieces.append("\t")
                    elif element.tag in {f"{WORD_NS}br", f"{WORD_NS}cr"}:
                        pieces.append("\n")
                if pieces:
                    paragraphs.append("".join(pieces))
    return "\n\n".join(paragraphs), {"extractor": "docx-xml@1", "extraction_parts": names}


def _pdf_text(data: bytes, maximum: int) -> tuple[str, dict[str, Any]]:
    executable = shutil.which("pdftotext")
    if not executable:
        raise RuntimeError("pdf_extractor_unavailable")
    with tempfile.TemporaryDirectory(prefix="cce-document-") as folder:
        input_path, output_path = Path(folder) / "input.pdf", Path(folder) / "output.txt"
        input_path.write_bytes(data)
        input_path.chmod(0o600)
        output_path.touch(mode=0o600)
        subprocess.run(
            [executable, "-enc", "UTF-8", "-nopgbrk", str(input_path), str(output_path)],
            check=True,
            timeout=30,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if output_path.stat().st_size > maximum:
            raise ValueError("PDF extracted text exceeds byte limit")
        text = output_path.read_text(encoding="utf-8")
    return text, {"extractor": "pdftotext", "extraction_scope": "embedded_text_no_ocr"}


def _record(path: Path, root: Path, source: dict, report: dict) -> Conversation | None:
    relative = path.relative_to(root).as_posix()
    identity = hashlib.sha256(f"{source['id']}\0{relative}".encode()).hexdigest()[:32]
    file_report = {"source_id": source["id"], "path": relative, "status": "metadata_only"}
    report["files"].append(file_report)
    report["counts"]["matched"] += 1
    try:
        info = path.lstat()
    except OSError as error:
        _finding(report, source["id"], relative, "stat_error", type(error).__name__)
        file_report["reason"] = "stat_error"
        report["counts"]["errors"] += 1
        return None
    if stat.S_ISLNK(info.st_mode):
        _finding(report, source["id"], relative, "symlink_not_followed")
        file_report["status"] = "excluded"
        file_report["reason"] = "symlink_not_followed"
        report["counts"]["excluded"] += 1
        return None
    if not stat.S_ISREG(info.st_mode):
        _finding(report, source["id"], relative, "not_regular_file")
        file_report["status"] = "excluded"
        report["counts"]["excluded"] += 1
        return None
    maximum = source.get("max_bytes", DEFAULT_MAX_BYTES)
    provenance: dict[str, Any] = {
        "source_file": relative,
        "source_path": str(path),
        "source_root": str(root),
        "source_uri": path.as_uri(),
        "size_bytes": info.st_size,
        "modified_ns": info.st_mtime_ns,
        "modified_at": to_iso(info.st_mtime),
        "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        "reader": READER_VERSION,
        "reader_version": READER_VERSION,
    }
    created = to_iso(getattr(info, "st_birthtime", None))
    if created:
        provenance["created_at"] = created
    body, reason = "", ""
    if info.st_size > maximum:
        reason = "size_limit"
    else:
        try:
            data = _safe_bytes(path, info, maximum)
            provenance["source_sha256"] = hashlib.sha256(data).hexdigest()
            suffix = path.suffix.lower()
            if suffix in TEXT_SUFFIXES:
                body = data.decode("utf-8-sig")
                if "\x00" in body:
                    raise ValueError("text contains binary NUL")
                provenance["extractor"] = "utf8@1"
            elif suffix == ".docx":
                body, extraction = _docx_text(data, maximum)
                provenance.update(extraction)
            elif suffix == ".pdf":
                body, extraction = _pdf_text(data, maximum)
                provenance.update(extraction)
            else:
                reason = "unsupported_format"
            if not reason and not body.strip():
                reason = "empty_body"
        except (
            OSError,
            ValueError,
            RuntimeError,
            KeyError,
            ET.ParseError,
            zipfile.BadZipFile,
            subprocess.SubprocessError,
        ) as error:
            reason = (
                "pdf_extractor_unavailable"
                if str(error) == "pdf_extractor_unavailable"
                else "extraction_error"
            )
            _finding(report, source["id"], relative, reason, type(error).__name__)
            report["counts"]["errors"] += 1
    status = "metadata_only" if reason else "full"
    if reason and reason not in {"extraction_error", "pdf_extractor_unavailable"}:
        _finding(report, source["id"], relative, reason)
    file_report.update(status=status, reason=reason)
    report["counts"]["metadata_only" if reason else "ingested"] += 1
    extra = {
        "wing": source.get("wing", "authored"),
        "lane": source.get("lane", "writing"),
        "source_id": source["id"],
        "body_status": status,
        "body_reason": reason,
        "original_refs": [path.as_uri()],
        "reader_version": READER_VERSION,
    }
    return Conversation(
        provider="documents",
        source_id=f"file-{identity}",
        title=path.stem,
        created=created,
        updated=to_iso(info.st_mtime),
        kind=source.get("kind", "document"),
        collection=source["id"],
        provenance=provenance,
        extra=extra,
        messages=[]
        if reason
        else [
            Message(
                id=f"document-{identity}",
                role="user",
                text=body,
                channel="project",
                ts=to_iso(info.st_mtime),
                typed_chars=0,
                meta={"kind": source.get("kind", "document")},
            )
        ],
    )


def iter_documents(sources: list[dict], *, report: dict | None = None) -> Iterator[Conversation]:
    """Read only manifest-selected roots; emit records and explicit coverage findings.

    ``report`` is updated as the iterator is consumed. Missing roots and unreadable
    paths remain findings. Unsupported or oversized regular files remain visible
    as metadata-only records with original references, never as extracted text.
    """
    _validate_sources(sources)
    report = report if report is not None else {}
    for key in ("sources", "files", "findings"):
        report.setdefault(key, [])
    counts = report.setdefault("counts", {})
    for key in ("matched", "ingested", "metadata_only", "excluded", "errors"):
        counts.setdefault(key, 0)
    for source in sources:
        root = Path(source["root"]).expanduser().absolute()
        entry = {"id": source["id"], "root": str(root), "status": "scanned"}
        report["sources"].append(entry)
        if any(part.is_symlink() for part in [root, *root.parents]):
            entry["status"] = "symlink_not_followed"
            _finding(report, source["id"], str(root), entry["status"])
            counts["errors"] += 1
            continue
        if not root.is_dir():
            entry["status"] = "missing_root" if not root.exists() else "not_directory"
            _finding(report, source["id"], str(root), entry["status"])
            counts["errors"] += 1
            continue
        include, exclude = source.get("include", ["**/*"]), source.get("exclude", [])

        def walk_error(
            error: OSError, source_id: str = source["id"], source_root: Path = root
        ) -> None:
            _finding(
                report,
                source_id,
                str(error.filename or source_root),
                "directory_read_error",
                type(error).__name__,
            )
            counts["errors"] += 1

        for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
            folder = Path(directory)
            retained: list[str] = []
            for name in sorted(dirs):
                path = folder / name
                relative = path.relative_to(root).as_posix()
                if any(_matches(relative, pattern) for pattern in exclude):
                    report["files"].append(
                        {
                            "source_id": source["id"],
                            "path": relative,
                            "status": "excluded",
                            "reason": "exclude_pattern",
                            "scope": "subtree",
                        }
                    )
                    counts["excluded"] += 1
                elif path.is_symlink():
                    _record(path, root, source, report)
                else:
                    retained.append(name)
            dirs[:] = retained
            for name in sorted(files):
                path = folder / name
                relative = path.relative_to(root).as_posix()
                if not any(_matches(relative, pattern) for pattern in include):
                    continue
                if any(_matches(relative, pattern) for pattern in exclude):
                    report["files"].append(
                        {
                            "source_id": source["id"],
                            "path": relative,
                            "status": "excluded",
                            "reason": "exclude_pattern",
                        }
                    )
                    counts["excluded"] += 1
                    continue
                row = _record(path, root, source, report)
                if row is not None:
                    yield row
