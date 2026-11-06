"""Validate the offline wikipedia-article.v1 producer contract and adapt its fields.

Original article metadata stays attached; this is not a network client. Refusals are
operational receipts, while missing pages have no body and remain metadata only.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .attribution import text_digest

WIKIPEDIA_SCHEMA = "wikipedia-article.v1"


class SkippedArticle(ValueError):
    """A valid operational refusal with no article body to ingest."""


_WIKI_REQUIRED = frozenset(
    {
        "schema",
        "id",
        "key",
        "status",
        "reason",
        "requested_url",
        "requested_title",
        "lang",
        "api_host",
        "resolved_title",
        "redirected",
        "redirect_chain",
        "page_id",
        "revision_id",
        "revision_timestamp",
        "fetched_at",
        "content_sha256",
        "text",
        "text_chars",
        "categories",
        "is_disambiguation",
        "disambiguation_evidence",
        "license_note",
        "worker",
    }
)


_WIKI_LICENSE = "Wikipedia text is CC BY-SA 4.0; third-party text, never owner-authored"


def _require(valid: bool, message: str) -> None:
    if not valid:
        raise ValueError("web capture " + message)


def _text(value: Any, label: str, *, empty: bool = False) -> str:
    _require(isinstance(value, str) and (empty or bool(value.strip())), f"{label} must be text")
    return value


def _timestamp(value: Any, label: str) -> str:
    value = _text(value, label)
    _require(
        re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value) is not None,
        f"{label} must be a UTC timestamp",
    )
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise ValueError(f"web capture {label} is not a valid timestamp") from None
    return value


def _url(value: Any, label: str) -> str:
    value = _text(value, label)
    try:
        parts = urlsplit(value)
        valid = (
            parts.scheme.lower() in ("http", "https")
            and bool(parts.hostname)
            and parts.username is None
            and parts.password is None
            and (parts.port is None or 0 < parts.port < 65536)
            and not any(ord(char) < 32 or ord(char) == 127 for char in value)
        )
    except ValueError:
        valid = False
    _require(bool(valid), f"{label} must be an HTTP(S) URL without credentials")
    return value


def _extractor(value: Any) -> dict[str, str]:
    _require(
        isinstance(value, dict) and set(value) == {"name", "version"},
        "extractor needs a name and version",
    )
    return {key: _text(value[key], "extractor " + key) for key in ("name", "version")}


def _identity(row: dict[str, Any]) -> None:
    key = _text(row.get("key"), "key")
    expected = "pg_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    _require(row.get("id") == expected, "id does not match key")


def _body(row: dict[str, Any], *, present: bool, digest_field: str) -> str:
    body = _text(row.get("text"), "body", empty=True)
    if present:
        _require(bool(body.strip()), "successful page needs a body")
        _require(
            row.get(digest_field) == "sha256:" + text_digest(body),
            "content digest does not match exact text",
        )
    else:
        _require(not body and row.get(digest_field) is None, "failed page must not contain a body")
    return body


def _wiki_identity(row: dict[str, Any], requested: str) -> None:
    lang = _text(row.get("lang"), "lang")
    _require(re.fullmatch(r"[a-z]+(?:-[a-z]+)*", lang) is not None, "invalid Wikipedia language")
    parts = urlsplit(requested)
    _require(
        parts.hostname in (f"{lang}.wikipedia.org", f"{lang}.m.wikipedia.org")
        and row["api_host"] == f"{lang}.wikipedia.org",
        "Wikipedia host does not match language",
    )
    title = _text(row["requested_title"], "requested_title")
    _require(row["key"] == f"wikipedia:{lang}:{title}", "Wikipedia key does not match title")
    params = {
        key.lower(): value for key, value in parse_qs(parts.query, keep_blank_values=True).items()
    }
    if parts.path.startswith("/wiki/"):
        raw_title = unquote(parts.path[len("/wiki/") :])
    elif parts.path.startswith(("/w/index.php/", "/index.php/")):
        raw_title = unquote(parts.path.split("/index.php/", 1)[1])
    elif parts.path in ("/w/index.php", "/index.php"):
        raw_title = params.get("title", [""])[0]
    else:
        raw_title = ""
    normalized = unicodedata.normalize("NFC", " ".join(raw_title.replace("_", " ").split()))
    head = normalized[:1].upper()
    normalized = (head if len(head) == 1 else normalized[:1]) + normalized[1:]
    _require(
        normalized == title and not params.keys() & {"search", "fulltext"},
        "Wikipedia requested URL does not match title",
    )


def _wikipedia(row: dict[str, Any]) -> dict[str, Any] | None:
    status = row.get("status")
    _require(status in ("stored", "missing", "refused"), "unknown Wikipedia status")
    if status == "refused":
        _require(row["key"].startswith("refused:"), "invalid refused identity")
        _text(row.get("reason"), "refusal reason")
        _text(row.get("requested_url"), "refused URL")
        _require(
            not row.get("text") and row.get("content_sha256") is None,
            "refused row must not contain a body",
        )
        return None
    _require(row.keys() >= _WIKI_REQUIRED, "missing Wikipedia provenance fields")
    requested = _url(row["requested_url"], "requested_url")
    _wiki_identity(row, requested)
    _require(row["license_note"] == _WIKI_LICENSE, "unknown Wikipedia license note")
    body = _body(row, present=status == "stored", digest_field="content_sha256")
    _require(
        type(row["text_chars"]) is int and row["text_chars"] == len(body),
        "Wikipedia text length does not match body",
    )
    _require(
        row["reason"] is None
        if status == "stored"
        else isinstance(row["reason"], str) and bool(row["reason"]),
        "Wikipedia reason does not match status",
    )
    for key in ("page_id", "revision_id"):
        _require(
            row[key] is None
            or type(row[key]) is int
            and (row[key] > 0 or key == "page_id" and status == "missing"),
            f"invalid {key}",
        )
    if row["revision_timestamp"] is not None:
        _timestamp(row["revision_timestamp"], "revision_timestamp")
    for key in ("categories", "redirect_chain", "disambiguation_evidence"):
        _require(
            isinstance(row[key], list) and all(isinstance(value, str) for value in row[key]),
            f"{key} must be a list of strings",
        )
    for key in ("redirected", "is_disambiguation"):
        _require(type(row[key]) is bool, f"{key} must be a boolean")
    resolved = row["resolved_title"]
    _require(resolved is None or isinstance(resolved, str), "invalid resolved title")
    return {
        "url": requested,
        "requested_url": requested,
        "final_url": None,
        "fetched_at": _timestamp(row["fetched_at"], "fetched_at"),
        "content_digest": row["content_sha256"],
        "extractor": _extractor(row["worker"]),
        "license_note": row["license_note"],
        "text": body,
        "title": resolved or row["requested_title"],
        "reason": row["reason"],
        "identity_prefix": "wikipedia",
    }


def normalize_article(row: dict[str, Any]) -> dict[str, Any]:
    """Map validated producer fields to the shared capture intake without losing metadata."""
    _identity(row)
    page = _wikipedia(row)
    if page is None:
        raise SkippedArticle("Wikipedia refusal: " + str(row["reason"])[:128])
    return {
        "schema": WIKIPEDIA_SCHEMA,
        "id": row["id"],
        "key": row["key"],
        "requested_url": page["requested_url"],
        "final_url": None,
        "status": "captured" if row["status"] == "stored" else "failed",
        "http_status": None,
        "content_digest": page["content_digest"],
        "captured_at": page["fetched_at"],
        "extractor": page["extractor"],
        "text": page["text"],
        "failure_class": page["reason"],
        "license_note": page["license_note"],
        "title": page["title"],
        "wikipedia": {key: value for key, value in row.items() if key != "text"},
    }
