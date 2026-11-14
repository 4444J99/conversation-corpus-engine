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
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterator
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from .archive_model import Conversation, Message, to_iso

READER_VERSION = "library-documents@1.3.0"
DEFAULT_MAX_BYTES = 20 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 4096
# Directory packages recorded as one document and never descended into when selected.
PACKAGE_DIRECTORY_SUFFIXES = frozenset({".textbundle", ".numbers", ".key"})
# Formats with no text reader: they receive a receipt naming the format, not silence.
KNOWN_UNSUPPORTED_SUFFIXES = {".numbers": "numbers", ".key": "key"}
TEXTBUNDLE_TEXT_NAMES = frozenset({"text.md", "text.markdown", "text.txt"})
# Reasons for which the cause is an unavailable tool or placeholder, not a bad document.
UNAVAILABLE_REASONS = frozenset(
    {
        "pdf_extractor_unavailable",
        "pages_preview_unavailable",
        "pages_native_extractor_unavailable",
        "legacy_converter_unavailable",
    }
)
TEXT_SUFFIXES = frozenset(
    {
        ".md",
        ".markdown",
        ".txt",
        ".rst",
        ".json",
        ".jsonl",
        ".tex",
        ".yaml",
        ".yml",
        ".org",
        ".csv",
        ".fountain",
    }
)
WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _utf8_text(data: bytes) -> str:
    text = data.decode("utf-8-sig")
    if "\x00" in text:
        raise ValueError("text contains binary NUL")
    return text


def _safe_xml(data: bytes) -> ET.Element:
    # Null removal also detects declarations encoded as UTF-16/32. No DTD or
    # entity declaration is needed for these document formats.
    if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", data.replace(b"\x00", b""), re.IGNORECASE):
        raise ValueError("document contains unsupported XML declarations")
    return ET.fromstring(data)


class _StaticHTML(HTMLParser):
    """Extract static text only; never render, execute or fetch references."""

    hidden_tags = {"head", "script", "style", "template", "noscript", "iframe", "object"}
    void_tags = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
    breaks = {
        "address",
        "article",
        "blockquote",
        "br",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "li",
        "p",
        "pre",
        "section",
        "table",
        "tr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool]] = []
        self.hidden_depth = 0
        self.pieces: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        style = re.sub(r"\s+", "", attributes.get("style") or "").lower()
        hidden = (
            tag in self.hidden_tags
            or "hidden" in attributes
            or (attributes.get("aria-hidden") or "").lower() == "true"
            or "display:none" in style
            or "visibility:hidden" in style
        )
        if tag not in self.void_tags:
            self.stack.append((tag, hidden))
            self.hidden_depth += hidden
        if not hidden and not self.hidden_depth:
            if tag in self.breaks:
                self.pieces.append("\n")
            elif tag in {"td", "th"}:
                self.pieces.append("\t")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in self.void_tags:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.hidden_depth and tag in self.breaks:
            self.pieces.append("\n")
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                self.hidden_depth -= sum(value for _, value in self.stack[index:])
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.pieces.append(data)


def _html_text(data: bytes) -> tuple[str, dict[str, Any]]:
    reader = _StaticHTML()
    reader.feed(_utf8_text(data))
    reader.close()
    lines = [
        re.sub(r"[ \t\f\v]+", " ", line).strip() for line in "".join(reader.pieces).splitlines()
    ]
    return "\n".join(line for line in lines if line), {
        "extractor": "html-static-text@1",
        "extraction_scope": "static_text_no_scripts_styles_or_fetch",
    }


def _fdx_text(data: bytes) -> tuple[str, dict[str, Any]]:
    document = _safe_xml(data)
    if document.tag.rsplit("}", 1)[-1] != "FinalDraft":
        raise ValueError("FDX root is not FinalDraft")
    paragraphs = []
    for paragraph in document.iter():
        if paragraph.tag.rsplit("}", 1)[-1] == "Paragraph":
            text = "".join(
                "".join(element.itertext())
                for element in paragraph.iter()
                if element.tag.rsplit("}", 1)[-1] == "Text"
            )
            if text:
                paragraphs.append(text)
    return "\n\n".join(paragraphs), {"extractor": "fdx-xml@1", "extraction_scope": "paragraph_text"}


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
            document = _safe_xml(xml)
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
            [
                sys.executable,
                "-c",
                "import os,resource,sys; resource.setrlimit(resource.RLIMIT_FSIZE,(int(sys.argv[1]),int(sys.argv[1]))); os.execv(sys.argv[2],sys.argv[2:])",
                str(maximum + 1),
                executable,
                "-enc",
                "UTF-8",
                "-nopgbrk",
                str(input_path),
                str(output_path),
            ],
            check=True,
            timeout=30,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if output_path.stat().st_size > maximum:
            raise ValueError("PDF extracted text exceeds byte limit")
        text = output_path.read_text(encoding="utf-8")
    return text, {"extractor": "pdftotext", "extraction_scope": "embedded_text_no_ocr"}


def _pages_native_text(data: bytes, maximum: int) -> tuple[str, dict[str, Any]]:
    executable = shutil.which("pages2text")
    if executable is None:
        raise RuntimeError("pages_native_extractor_unavailable")
    with tempfile.TemporaryDirectory(prefix="cce-pages-") as folder:
        source_path, output_path = Path(folder) / "input.pages", Path(folder) / "output.txt"
        source_path.write_bytes(data)
        source_path.chmod(0o600)
        with output_path.open("xb") as output:
            output_path.chmod(0o600)
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import os,resource,sys; resource.setrlimit(resource.RLIMIT_FSIZE,(int(sys.argv[1]),int(sys.argv[1]))); resource.setrlimit(resource.RLIMIT_CPU,(25,25)); os.execv(sys.argv[2],sys.argv[2:])",
                    str(maximum + 1),
                    executable,
                    str(source_path),
                ],
                check=True,
                timeout=30,
                stdout=output,
                stderr=subprocess.DEVNULL,
            )
        if output_path.stat().st_size > maximum:
            raise ValueError("Pages native text exceeds byte limit")
        text = _utf8_text(output_path.read_bytes())
    return text, {
        "extractor": "libetonyek-pages2text",
        "extraction_scope": "native_document_text_no_layout_or_media",
        "extraction_complete": False,
        "partial_reason": "native_text_conversion_not_visually_verified",
    }


def _pages_text(data: bytes, maximum: int) -> tuple[str, dict[str, Any]]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if len(archive.infolist()) > 4096:
            raise ValueError("Pages archive has too many entries")
        candidates = (
            "quicklook/preview.txt",
            "preview.txt",
            "quicklook/preview.pdf",
            "preview.pdf",
        )
        members = {name.lower(): name for name in archive.namelist()}
        name = next((members[key] for key in candidates if key in members), None)
        if name is None:
            return _pages_native_text(data, maximum)
        if (
            sum(member.lower() == name.lower() for member in archive.namelist()) != 1
            or archive.getinfo(name).file_size > maximum
        ):
            raise ValueError("Pages preview is ambiguous or exceeds byte limit")
        with archive.open(name) as member:
            preview = member.read(maximum + 1)
        if len(preview) > maximum:
            raise ValueError("Pages expanded preview exceeds byte limit")
    if name.lower().endswith(".pdf"):
        text, extraction = _pdf_text(preview, maximum)
    else:
        text, extraction = _utf8_text(preview), {"extractor": "utf8@1"}
    return text, {
        "extractor": "pages-quicklook@1",
        "preview_extractor": extraction["extractor"],
        "extraction_parts": [name],
        "extraction_scope": "quicklook_preview_not_native_document",
        "extraction_complete": False,
    }


def _textutil_convert(data: bytes, suffix: str, maximum: int) -> str:
    """Convert legacy Word or RTF bytes to UTF-8 text with macOS ``textutil``."""
    executable = shutil.which("textutil")
    if executable is None:
        raise RuntimeError("legacy_converter_unavailable")
    with tempfile.TemporaryDirectory(prefix="cce-legacy-") as folder:
        # textutil infers the input format from the extension.
        source_path, output_path = Path(folder) / f"input{suffix}", Path(folder) / "output.txt"
        source_path.write_bytes(data)
        source_path.chmod(0o600)
        output_path.touch(mode=0o600)
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import os,resource,sys; resource.setrlimit(resource.RLIMIT_FSIZE,(int(sys.argv[1]),int(sys.argv[1]))); resource.setrlimit(resource.RLIMIT_CPU,(25,25)); os.execv(sys.argv[2],sys.argv[2:])",
                str(maximum + 1),
                executable,
                "-convert",
                "txt",
                "-encoding",
                "UTF-8",
                "-output",
                str(output_path),
                str(source_path),
            ],
            check=True,
            timeout=30,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if output_path.stat().st_size > maximum:
            raise ValueError("converted text exceeds byte limit")
        return _utf8_text(output_path.read_bytes())


# Injectable seam: ``(data, suffix, maximum) -> text``. Tests replace it so no host tool is needed.
_LEGACY_CONVERTER = _textutil_convert


def _legacy_text(data: bytes, suffix: str, maximum: int) -> tuple[str, dict[str, Any]]:
    return _LEGACY_CONVERTER(data, suffix, maximum), {
        "extractor": "textutil@1",
        "extraction_scope": "converted_text_no_layout_or_media",
    }


def _zip_index(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > MAX_ARCHIVE_ENTRIES:
        raise ValueError("archive has too many entries")
    names = [info.filename for info in infos]
    if len(set(names)) != len(names):
        raise ValueError("archive has duplicate member names")
    return {info.filename: info for info in infos}


def _zip_read(archive: zipfile.ZipFile, name: str, budget: list[int]) -> bytes:
    """Read one member, charging its real expanded size against a shared byte budget."""
    with archive.open(name) as member:
        data = member.read(max(budget[0], 0) + 1)
    budget[0] -= len(data)
    if budget[0] < 0:
        raise ValueError("archive expanded content exceeds byte limit")
    return data


def _epub_text(data: bytes, maximum: int) -> tuple[str, dict[str, Any]]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = _zip_index(archive)
        budget = [maximum]
        container = _safe_xml(_zip_read(archive, "META-INF/container.xml", budget))
        opf_path = next(
            (
                element.get("full-path", "")
                for element in container.iter()
                if element.tag.rsplit("}", 1)[-1] == "rootfile"
            ),
            "",
        )
        if not opf_path or opf_path not in members:
            raise ValueError("EPUB package document is missing")
        package = _safe_xml(_zip_read(archive, opf_path, budget))
        manifest: dict[str, str] = {}
        spine: list[str] = []
        for element in package.iter():
            tag = element.tag.rsplit("}", 1)[-1]
            if tag == "item" and element.get("id") and element.get("href"):
                manifest[element.get("id", "")] = element.get("href", "")
            elif tag == "itemref" and element.get("idref"):
                spine.append(element.get("idref", ""))
        if not spine:
            raise ValueError("EPUB has no reading order")
        base = posixpath.dirname(opf_path)
        parts: list[str] = []
        chapters: list[str] = []
        for idref in spine:
            href = unquote(manifest.get(idref, "").split("#", 1)[0])
            member = posixpath.normpath(posixpath.join(base, href))
            if not href or member.startswith(("..", "/")) or member not in members:
                raise ValueError("EPUB reading order references a missing member")
            if posixpath.splitext(member)[1].lower() not in {".xhtml", ".html", ".htm", ".xml"}:
                continue
            text, _ = _html_text(_zip_read(archive, member, budget))
            chapters.append(member)
            if text:
                parts.append(text)
    return "\n\n".join(parts), {
        "extractor": "epub-xhtml@1",
        "extraction_scope": "spine_text_no_images_or_styles",
        "extraction_parts": chapters,
    }


def _textbundle_member(names: list[str]) -> str:
    """Pick the single shallowest text member of a bundle: root or one top-level folder."""
    found = [
        name
        for name in names
        if not name.startswith("__MACOSX/")
        and name.count("/") <= 1
        and posixpath.basename(name).lower() in TEXTBUNDLE_TEXT_NAMES
    ]
    if not found:
        raise ValueError("TextBundle has no text member")
    shallowest = min(name.count("/") for name in found)
    chosen = [name for name in found if name.count("/") == shallowest]
    if len(chosen) != 1:
        raise ValueError("TextBundle text member is ambiguous")
    return chosen[0]


def _textbundle_zip_text(data: bytes, maximum: int) -> tuple[str, dict[str, Any]]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = _zip_index(archive)
        name = _textbundle_member(list(members))
        text = _utf8_text(_zip_read(archive, name, [maximum]))
    return text, {"extractor": "textbundle@1", "extraction_parts": [name]}


def _textbundle_member_of_folder(path: Path) -> tuple[Path, os.stat_result]:
    names = [item.name for item in path.iterdir() if item.name.lower() in TEXTBUNDLE_TEXT_NAMES]
    if len(names) != 1:
        raise ValueError("TextBundle text member is missing or ambiguous")
    member = path / names[0]
    info = member.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("TextBundle text member is not a regular file")
    return member, info


def _package_stats(path: Path) -> tuple[int, bool]:
    """Total regular-file bytes and whether any child is cloud-only; uses lstat only."""
    size, dataless = 0, False
    for directory, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                info = (Path(directory) / name).lstat()
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                size += info.st_size
                dataless = dataless or _is_dataless(info)
    return size, dataless


def _is_dataless(info: Any) -> bool:
    return bool(getattr(info, "st_flags", 0) & getattr(stat, "SF_DATALESS", 0x40000000))


def _unsupported(suffix: str, provenance: dict[str, Any]) -> str:
    if suffix in KNOWN_UNSUPPORTED_SUFFIXES:
        provenance["known_unsupported_format"] = KNOWN_UNSUPPORTED_SUFFIXES[suffix]
    return "unsupported_format"


def _extract_body(
    suffix: str, data: bytes, maximum: int, provenance: dict[str, Any]
) -> tuple[str, str]:
    """Return ``(body, reason)`` for one regular file; a nonempty reason means no body."""
    if suffix in TEXT_SUFFIXES:
        provenance["extractor"] = "utf8@1"
        return _utf8_text(data), ""
    readers = {
        ".html": lambda: _html_text(data),
        ".htm": lambda: _html_text(data),
        ".fdx": lambda: _fdx_text(data),
        ".pages": lambda: _pages_text(data, maximum),
        ".docx": lambda: _docx_text(data, maximum),
        ".pdf": lambda: _pdf_text(data, maximum),
        ".doc": lambda: _legacy_text(data, suffix, maximum),
        ".rtf": lambda: _legacy_text(data, suffix, maximum),
        ".epub": lambda: _epub_text(data, maximum),
        ".textbundle": lambda: _textbundle_zip_text(data, maximum),
        ".textpack": lambda: _textbundle_zip_text(data, maximum),
    }
    if suffix not in readers:
        return "", _unsupported(suffix, provenance)
    body, extraction = readers[suffix]()
    provenance.update(extraction)
    return body, ""


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
    suffix = path.suffix.lower()
    package = stat.S_ISDIR(info.st_mode) and suffix in PACKAGE_DIRECTORY_SUFFIXES
    if not package and not stat.S_ISREG(info.st_mode):
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
        "size_bytes": _package_stats(path)[0] if package else info.st_size,
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
    if _is_dataless(info):
        reason = "cloud_placeholder_not_hydrated"
    elif not package and info.st_size > maximum:
        reason = "size_limit"
    else:
        try:
            if package:
                if suffix == ".textbundle":
                    member, member_info = _textbundle_member_of_folder(path)
                    if _is_dataless(member_info):
                        reason = "cloud_placeholder_not_hydrated"
                    else:
                        data = _safe_bytes(member, member_info, maximum)
                        provenance["source_sha256"] = hashlib.sha256(data).hexdigest()
                        provenance.update(extractor="textbundle@1", extraction_parts=[member.name])
                        body = _utf8_text(data)
                else:
                    body, reason = "", _unsupported(suffix, provenance)
            else:
                data = _safe_bytes(path, info, maximum)
                provenance["source_sha256"] = hashlib.sha256(data).hexdigest()
                body, reason = _extract_body(suffix, data, maximum, provenance)
            if len(body.encode("utf-8")) > maximum:
                raise ValueError("extracted text exceeds byte limit")
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
            reason = str(error) if str(error) in UNAVAILABLE_REASONS else "extraction_error"
            _finding(report, source["id"], relative, reason, type(error).__name__)
            report["counts"]["errors"] += 1
    status = (
        "metadata_only"
        if reason
        else ("partial" if provenance.get("extraction_complete") is False else "full")
    )
    if reason and reason != "extraction_error" and reason not in UNAVAILABLE_REASONS:
        _finding(
            report,
            source["id"],
            relative,
            reason,
            str(provenance.get("known_unsupported_format", "")),
        )
    coverage_reason = (
        provenance.get("partial_reason", "preview_only") if status == "partial" else reason
    )
    if status == "partial":
        report["counts"]["partial"] += 1
        _finding(report, source["id"], relative, coverage_reason)
    file_report.update(status=status, reason=coverage_reason)
    report["counts"]["metadata_only" if reason else "ingested"] += 1
    extra = {
        "wing": source.get("wing", "authored"),
        "lane": source.get("lane", "writing"),
        "source_id": source["id"],
        "body_status": status,
        "body_reason": coverage_reason,
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
    for key in ("matched", "ingested", "partial", "metadata_only", "excluded", "errors"):
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
                elif path.suffix.lower() in PACKAGE_DIRECTORY_SUFFIXES and any(
                    _matches(relative, pattern) for pattern in include
                ):
                    # One document per package; its members are never separate records.
                    row = _record(path, root, source, report)
                    if row is not None:
                        yield row
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


DOCUMENT_SUFFIXES = (
    TEXT_SUFFIXES
    | PACKAGE_DIRECTORY_SUFFIXES
    | frozenset(
        {".html", ".htm", ".fdx", ".pages", ".docx", ".pdf", ".doc", ".rtf", ".epub", ".textpack"}
    )
)


def iter_document_files(root: Path) -> Iterator[dict[str, Any]]:
    """List document-type files under ``root`` with cloud-only (dataless) detection.

    Only ``lstat`` is used, so listing never hydrates a placeholder or reads bytes.
    Symlinks are skipped, and each package directory counts as one document. ``state`` is
    ``dataless`` when the entry (or any package member) is cloud-only, else ``local``.
    """
    root = Path(root).expanduser().absolute()
    if root.is_symlink() or not root.is_dir():
        raise ValueError("enumeration root must be an existing directory and not a symlink")
    for directory, dirs, files in os.walk(root, followlinks=False):
        folder = Path(directory)
        retained: list[str] = []
        for name in sorted(dirs):
            path = folder / name
            if path.is_symlink():
                continue
            if path.suffix.lower() in PACKAGE_DIRECTORY_SUFFIXES:
                size, dataless = _package_stats(path)
                yield _document_entry(path, root, size, dataless, package=True)
            else:
                retained.append(name)
        dirs[:] = retained
        for name in sorted(files):
            path = folder / name
            if path.suffix.lower() not in DOCUMENT_SUFFIXES:
                continue
            try:
                info = path.lstat()
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                yield _document_entry(path, root, info.st_size, _is_dataless(info), package=False)


def _document_entry(
    path: Path, root: Path, size: int, dataless: bool, *, package: bool
) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "extension": path.suffix.lower(),
        "size_bytes": size,
        "state": "dataless" if dataless else "local",
        "package": package,
    }


def summarize_document_files(entries: Iterator[dict[str, Any]] | list[dict[str, Any]]) -> dict:
    """Counts by extension and state for entries from :func:`iter_document_files`."""
    by_extension: dict[str, dict[str, int]] = {}
    by_state = {"local": 0, "dataless": 0}
    total_bytes = dataless_bytes = 0
    for entry in entries:
        counts = by_extension.setdefault(entry["extension"], {"local": 0, "dataless": 0})
        counts[entry["state"]] += 1
        by_state[entry["state"]] += 1
        total_bytes += entry["size_bytes"]
        if entry["state"] == "dataless":
            dataless_bytes += entry["size_bytes"]
    return {
        "files_total": sum(by_state.values()),
        "by_extension": {key: by_extension[key] for key in sorted(by_extension)},
        "by_state": by_state,
        "total_bytes": total_bytes,
        "dataless_bytes": dataless_bytes,
    }


def enumerate_documents(root: Path) -> dict[str, Any]:
    """Summarize document-type files under ``root`` without reading or hydrating any."""
    return summarize_document_files(iter_document_files(root))
