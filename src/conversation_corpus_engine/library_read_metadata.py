"""Bounded attachment context for library reads; normalized bodies stay byte-for-byte intact."""

from __future__ import annotations

import re
from typing import Any

MAX_ATTACHMENTS = 20
FIELD_LIMITS = {"name": 512, "kind": 128, "ref": 512}
# Retain inert, simple identifiers only. URLs (including signed URLs), filesystem paths,
# encoded URLs, query strings and fragments must not become retrieval capabilities.
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,511}\Z")


def _attachments(message: dict[str, Any]) -> list[dict[str, Any]]:
    value = message.get("attachments", [])
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError("attachment metadata is malformed")
    return value


def sequence_attachment_count(messages: list[dict[str, Any]]) -> int:
    """Count observations in the selected sequence, including messages beyond the read page."""
    return sum(len(_attachments(message)) for message in messages)


def _typed_boundary(
    message: dict[str, Any], attachments: list[dict[str, Any]]
) -> tuple[int | None, str]:
    text = str(message.get("text", ""))
    typed = message.get("typed_chars")
    if typed is None:
        return None, "unknown"
    if isinstance(typed, bool) or not isinstance(typed, int) or not 0 <= typed <= len(text):
        return None, "invalid"
    # Older ChatGPT normalization inserted [image] inline, then defaulted the boundary to
    # len(text). A single prefix cannot describe typed text interleaved with those markers.
    # Keep the archived text and decline to report a boundary containing a known marker.
    image_at = text.find("[image]")
    if (
        image_at >= 0
        and typed > image_at
        and any(str(item.get("kind", "")).lower().startswith("image") for item in attachments)
    ):
        return None, "synthetic-marker"
    return typed, "normalized"


def message_metadata(
    message: dict[str, Any], *, max_attachments: int = MAX_ATTACHMENTS
) -> dict[str, Any]:
    attachments = _attachments(message)
    typed, status = _typed_boundary(message, attachments)
    visible = []
    truncated = len(attachments) > max_attachments
    redacted = False
    for item in attachments[:max_attachments]:
        name, kind, ref = (
            str(item.get("name", "")),
            str(item.get("kind", "file")),
            str(item.get("ref", "")),
        )
        truncated |= any(
            len(value) > FIELD_LIMITS[key]
            for key, value in (("name", name), ("kind", kind), ("ref", ref))
        )
        safe_ref = ref if IDENTIFIER.fullmatch(ref) else ""
        redacted |= bool(ref and not safe_ref)
        size = item.get("size")
        visible.append(
            {
                "name": name[: FIELD_LIMITS["name"]],
                "kind": kind[: FIELD_LIMITS["kind"]],
                "size": size
                if isinstance(size, int) and not isinstance(size, bool) and size >= 0
                else None,
                "ref": safe_ref,
            }
        )
    return {
        "typed_chars": typed,
        "typed_chars_status": status,
        "attachment_count_in_message": len(attachments),
        "attachments": visible,
        "attachments_truncated": truncated,
        "attachment_refs_redacted": redacted,
    }
