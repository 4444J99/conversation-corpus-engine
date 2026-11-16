"""Atomic Zettel view of a Wiki Standard v1 wiki (`cce wiki zettels`).

The wiki keeps one note per source file; this module cuts every note into one small Zettel per
heading section (long sections are cut again at paragraph boundaries), so the same knowledge can
be read as atomic units. Each Zettel carries a source tuple (`wiki:<note id>#<heading path>
sha256:<hash of the exact text>`) and a link back to its parent note. Authorship is only ever
inherited from the parent note, never upgraded: an agent note yields agent Zettels. Output is
deterministic and idempotent, owned files are tracked in a manifest, foreign files are never
overwritten, and nothing reads the network or writes inside the wiki.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = "cce.wiki-zettels.v1"
MANIFEST_NAME = ".wiki-zettels-manifest.json"
ZETTEL_TARGET_BYTES = 3500
FILENAME_TITLE_BYTES = 160
TITLE_LIMIT = 100
SUMMARY_LIMIT = 200
SKIP_NAMES = {"00-home.md"}
SKIP_DIRS = {"hubs"}

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_LINK_ONLY = re.compile(r"^\s*(?:[-*+]\s+)?\[\[[^\]]+\]\](?::.*)?\s*$")
_UNSAFE = re.compile(r'[/\\:*?"<>|#^\[\]\x00-\x1f]')
_SECRET = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bAKIA[0-9A-Z]{16}\b|\bgh[pousr]_[A-Za-z0-9]{30,}\b"
    r"|\bsk-[A-Za-z0-9_-]{24,}\b|\bxox[abprs]-[A-Za-z0-9-]{10,}\b"
)


@dataclass(frozen=True)
class Zettel:
    """One rendered Zettel and the parent note it was cut from."""

    id: str
    filename: str
    text: str
    parent_id: str


@dataclass(frozen=True)
class EmitResult:
    schema: str
    notes_read: int
    zettels: int
    written: int
    unchanged: int
    removed: int
    skipped_secret: int
    skipped_empty: int
    skipped_notes: int
    skipped_paths: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return str(json.loads(value))
        except ValueError:
            return value[1:-1]
    return value


def parse_note(text: str) -> tuple[dict[str, Any], str]:
    """Split a note into flat frontmatter (scalars and string lists) and its body."""
    if not text.startswith("---\n"):
        raise ValueError("note has no frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError("frontmatter is not closed")
    meta: dict[str, Any] = {}
    key: str | None = None
    for line in text[4:end].split("\n"):
        item = re.match(r"\s+-\s+(.*)", line)
        if item and key is not None:
            if isinstance(meta.get(key), list):
                meta[key].append(_unquote(item.group(1)))
            continue
        pair = re.match(r"([A-Za-z_][A-Za-z0-9_-]*):(?:\s+(.*))?$", line)
        if pair:
            key = pair.group(1)
            value = (pair.group(2) or "").strip()
            if value.startswith("[") and value.endswith("]"):
                meta[key] = [_unquote(v) for v in value[1:-1].split(",") if v.strip()]
            else:
                meta[key] = _unquote(value) if value else []
        else:
            key = None
    return meta, text[end + 5 :]


def _slug(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-") or "section"


def split_sections(body: str) -> list[tuple[list[str], str]]:
    """Return (heading path, text) per heading section; fenced code never opens a heading."""
    sections: list[tuple[list[str], list[str]]] = [([], [])]
    stack: list[tuple[int, str]] = []
    fence = ""
    for line in body.split("\n"):
        fence = _fence_state(line, fence)
        match = None if fence else _HEADING.match(line)
        if match:
            level = len(match.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, match.group(2).strip()))
            sections.append(([title for _, title in stack], []))
        else:
            sections[-1][1].append(line)
    return [(path, "\n".join(lines).strip("\n")) for path, lines in sections]


def _fence_state(line: str, fence: str) -> str:
    """Track the open code fence: only the same character, at least as long, closes it."""
    found = _FENCE.match(line)
    if not found:
        return fence
    mark = found.group(1)
    if not fence:
        return mark
    if mark[0] == fence[0] and len(mark) >= len(fence) and not line.strip(" `~"):
        return ""
    return fence


def _is_link_only(text: str) -> bool:
    lines = [line for line in text.split("\n") if line.strip()]
    return bool(lines) and all(_LINK_ONLY.match(line) for line in lines)


def _chunks(text: str) -> list[str]:
    """Cut at blank lines once half the target is reached, and hard at the target (never in a fence)."""
    if len(text.encode()) <= ZETTEL_TARGET_BYTES:
        return [text]
    out: list[str] = []
    current: list[str] = []
    size = 0
    fence = ""
    for line in text.split("\n"):
        fence = _fence_state(line, fence)
        fenced = bool(fence)
        current.append(line)
        size += len(line.encode()) + 1
        if (
            not line.strip()
            and not fenced
            and size >= ZETTEL_TARGET_BYTES // 2
            or size >= ZETTEL_TARGET_BYTES
            and not fenced
        ):
            out.append("\n".join(current).strip("\n"))
            current, size = [], 0
    if "".join(current).strip():
        out.append("\n".join(current).strip("\n"))
    return [chunk for chunk in out if chunk.strip()]


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _q(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


AUTHORSHIP_VALUES = ("human", "agent", "agent-reviewed", "generated")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _line(value: Any, default: str = "") -> str:
    """One safe line: control characters and line breaks collapse to single spaces."""
    text = " ".join(re.sub(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]", " ", str(value or "")).split())
    return text or default


def _date(value: Any) -> str:
    text = _line(value)[:10]
    return text if _DATE.fullmatch(text) else ""


def _summary(text: str) -> str:
    for line in text.split("\n"):
        stripped = re.sub(r"^[-*+>\s]+|^\d+[.)]\s+", "", line).strip()
        if stripped:
            return _clip(stripped, SUMMARY_LIMIT)
    return ""


def render_zettels(meta: dict[str, Any], body: str) -> tuple[list[Zettel], int, int]:
    """Cut one parsed note; returns (zettels, skipped_secret, skipped_empty)."""
    parent_id = str(meta.get("id") or "")
    parent_title = str(meta.get("title") or "")
    if not parent_id or not parent_title:
        raise ValueError("note needs an id and a title")
    # Inherited only: a value outside the vocabulary never becomes `human`.
    authorship = _line(meta.get("authorship"))
    authorship = authorship if authorship in AUTHORSHIP_VALUES else "agent"
    created = _date(meta.get("created"))
    updated = _date(meta.get("updated")) or created
    stem = Path(str(meta.get("_path") or parent_title)).stem
    zettels: list[Zettel] = []
    withheld = empty = 0
    for index, (path, text) in enumerate(split_sections(body)):
        if not text.strip() or _is_link_only(text):
            empty += 1 if text.strip() else 0
            continue
        for part, chunk in enumerate(_chunks(text), start=1):
            if _SECRET.search(chunk) or _SECRET.search(parent_title + " ".join(path)):
                withheld += 1
                continue
            anchor = f"{index}:" + ("/".join(_slug(p) for p in path) or "intro")
            digest = hashlib.sha256(chunk.encode()).hexdigest()
            key = hashlib.sha256(f"{parent_id}|{index}|{part}|{anchor}".encode()).hexdigest()[:12]
            day = created.replace("-", "") or "00000000"
            zid = f"{day}-{key}"
            heading = path[-1] if path else "Introduction"
            title = _clip(
                f"{_line(parent_title)} — {_line(heading)}" + (f" ({part})" if part > 1 else ""),
                100,
            )
            safe = _UNSAFE.sub("-", title).strip(" .-")[:TITLE_LIMIT] or "zettel"
            while len(safe.encode()) > FILENAME_TITLE_BYTES:
                safe = safe[:-1]
            filename = f"{safe.strip(' .-') or 'zettel'}--{key}.md"
            tags = [
                "zettel",
                *[_line(t) for t in meta.get("tags", []) if isinstance(t, str) and _line(t)],
            ]
            front = [
                "---",
                f"id: {zid}",
                "type: zettel",
                f"title: {_q(title)}",
                f"summary: {_q(_summary(chunk))}",
                f"created: {created}",
                f"updated: {updated}",
                f"author: {_q(_line(meta.get('author'), 'unknown'))}",
                f"authorship: {authorship}",
                "authorship_basis: inherited",
                f"status: {_q(_line(meta.get('status'), 'seed'))}",
                "sources:",
                f"  - {_q(f'wiki:{parent_id}#{anchor}@{part} sha256:{digest}')}",
                "links:",
                f"  - {_q('[[' + stem + ']]')}",
                "tags:",
                *[f"  - {_q(t)}" for t in tags],
                "---",
                f"# {title}",
                "",
                chunk,
                "",
            ]
            zettels.append(Zettel(zid, filename, "\n".join(front), parent_id))
    return zettels, withheld, empty


def _fold(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _safe_name(name: Any) -> bool:
    return isinstance(name, str) and name == Path(name).name and name.endswith(".md")


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def emit(wiki: Path, out: Path) -> EmitResult:
    """Write the Zettel view of `wiki` into `out` (idempotent; removes only stale owned files)."""
    wiki = wiki.expanduser().resolve()
    out = out.expanduser().resolve()
    if not wiki.is_dir():
        raise ValueError(f"wiki directory not found: {wiki}")
    if _within(out, wiki) or _within(wiki, out):
        raise ValueError("--out must be outside the wiki and must not contain it")
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / MANIFEST_NAME
    owned: set[str] = set()
    if manifest_path.exists():
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        if data.get("schema") != SCHEMA:
            raise ValueError("manifest has an unknown schema; refusing to touch the folder")
        owned = {n for n in data.get("files", {}) if _safe_name(n)}
    owned_fold = {_fold(n): n for n in owned}

    all_zettels: dict[str, Zettel] = {}
    read = withheld = empty = skipped_notes = 0
    skipped: list[str] = []
    seen_ids: dict[str, str] = {}
    seen_fold: dict[str, str] = {}
    for path in sorted(wiki.rglob("*.md")):
        rel = path.relative_to(wiki)
        if (
            rel.name in SKIP_NAMES
            or rel.parts[0] in SKIP_DIRS
            or any(p.startswith(".") for p in rel.parts)
        ):
            continue
        read += 1
        try:
            meta, body = parse_note(path.read_text(encoding="utf-8"))
            meta["_path"] = rel.as_posix()
            made, sec, emp = render_zettels(meta, body)
        except (ValueError, UnicodeDecodeError):
            skipped_notes += 1
            skipped.append(rel.as_posix())
            continue
        withheld += sec
        empty += emp
        for zettel in made:
            if _fold(zettel.filename) in seen_fold or zettel.id in seen_ids:
                raise ValueError(f"duplicate Zettel filename or id {zettel.filename}")
            seen_fold[_fold(zettel.filename)] = zettel.filename
            seen_ids[zettel.id] = zettel.filename
            all_zettels[zettel.filename] = zettel

    # Refuse before writing anything: every existing file must be owned (compared case-folded,
    # because the volume may be case-insensitive).
    for name in all_zettels:
        if (out / name).exists() and _fold(name) not in owned_fold:
            raise ValueError(f"refusing to overwrite a file this emitter does not own: {name}")

    def save_manifest(files: set[str]) -> None:
        by_name = {n: {"id": z.id, "parent": z.parent_id} for n, z in all_zettels.items()}
        body = {"schema": SCHEMA, "files": {n: by_name.get(n, {}) for n in sorted(files)}}
        temp = manifest_path.with_suffix(".tmp")
        temp.write_text(json.dumps(body, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        temp.replace(manifest_path)

    # Own the planned names before writing them, so an interrupted run is recoverable.
    save_manifest(owned | set(all_zettels))
    written = unchanged = 0
    for name, zettel in all_zettels.items():
        target = out / name
        old = owned_fold.get(_fold(name))
        if old is not None and old != name:
            (out / old).unlink(missing_ok=True)  # a case-only rename keeps one file
        elif target.exists() and target.read_text(encoding="utf-8") == zettel.text:
            unchanged += 1
            continue
        target.write_text(zettel.text, encoding="utf-8")
        written += 1
    keep = {_fold(n) for n in all_zettels}
    removed = 0
    for name in sorted(owned):
        stale = out / name
        if _fold(name) not in keep and stale.is_file():
            stale.unlink()
            removed += 1
    save_manifest(set(all_zettels))
    return EmitResult(
        SCHEMA,
        read,
        len(all_zettels),
        written,
        unchanged,
        removed,
        withheld,
        empty,
        skipped_notes,
        tuple(skipped),
    )
