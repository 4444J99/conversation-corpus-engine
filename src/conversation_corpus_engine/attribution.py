"""Digest-bound assertions of original authorship, separate from provider roles.

Offsets are half-open Python Unicode codepoint positions in the unchanged message.
Assertions record supplied evidence; they do not independently verify that evidence.
"""

from __future__ import annotations

import hashlib
from typing import Any

ATTRIBUTION_SCHEMA = "cce.span-attribution.v1"
ASSURANCE = "attributed_assertions_not_independent_verification"
AUTHORS = frozenset({"owner", "agent", "third_party", "unknown"})
ASSERTION_STATUSES = frozenset({"asserted", "disputed", "retracted"})


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def require_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def validate_range(start: Any, end: Any, length: int) -> None:
    if (
        isinstance(start, bool)
        or not isinstance(start, int)
        or isinstance(end, bool)
        or not isinstance(end, int)
        or not 0 <= start < end <= length
    ):
        raise ValueError("span must have integer offsets with 0 <= start < end <= text length")


def _unknown(text: str, start: int, end: int) -> dict[str, Any]:
    return {
        "start": start,
        "end": end,
        "text": text[start:end],
        "author": "unknown",
        "author_id": None,
        "evidence": [],
        "assertion_status": "unattributed",
        "profile_eligible": False,
    }


def attributed_spans(text: str, annotation: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Validate an annotation and return a complete lossless partition, including gaps.

    No annotation means unknown, including for user-role messages and authored files.
    An invalid or stale supplied annotation fails instead of becoming an eligible span.
    """
    if not isinstance(text, str):
        raise ValueError("attribution text must be a string")
    if annotation is None:
        return [_unknown(text, 0, len(text))] if text else []
    if not isinstance(annotation, dict) or annotation.get("schema") != ATTRIBUTION_SCHEMA:
        raise ValueError("unsupported span attribution schema")
    if annotation.get("text_digest") != text_digest(text):
        raise ValueError("attribution text digest does not match the exact message")
    spans = annotation.get("spans")
    if not isinstance(spans, list):
        raise ValueError("attribution spans must be a list")
    validated: list[dict[str, Any]] = []
    for span in spans:
        if not isinstance(span, dict):
            raise ValueError("attribution span must be an object")
        start, end = span.get("start"), span.get("end")
        validate_range(start, end, len(text))
        author = span.get("author")
        if not isinstance(author, str) or author not in AUTHORS:
            raise ValueError("span author must be owner, agent, third_party or unknown")
        status = span.get("assertion_status")
        if not isinstance(status, str) or status not in ASSERTION_STATUSES:
            raise ValueError(
                "span needs an explicit asserted, disputed or retracted assertion status"
            )
        evidence = span.get("evidence", [])
        if not isinstance(evidence, list) or any(
            not isinstance(item, str) or not item.strip() for item in evidence
        ):
            raise ValueError("attribution evidence must be a list of nonempty strings")
        eligible = span.get("profile_eligible", False)
        if not isinstance(eligible, bool):
            raise ValueError("profile_eligible must be an explicit boolean")
        author_id = span.get("author_id")
        if author_id is not None:
            require_text(author_id, "author_id")
        validated.append(
            {
                "start": start,
                "end": end,
                "text": text[start:end],
                "author": author,
                "author_id": author_id,
                "evidence": list(evidence),
                "assertion_status": status,
                "profile_eligible": eligible,
            }
        )
    result = []
    cursor = 0
    for span in sorted(validated, key=lambda item: item["start"]):
        if span["start"] < cursor:
            raise ValueError("attribution spans overlap")
        if span["start"] > cursor:
            result.append(_unknown(text, cursor, span["start"]))
        result.append(span)
        cursor = span["end"]
    if cursor < len(text):
        result.append(_unknown(text, cursor, len(text)))
    return result


def owner_profile_eligible(span: dict[str, Any], owner_id: str) -> bool:
    """Only explicit evidenced assertions by this owner may enter a strict profile."""
    return (
        span["author"] == "owner"
        and span["author_id"] == owner_id
        and span["assertion_status"] == "asserted"
        and bool(span["evidence"])
        and span["profile_eligible"] is True
    )
