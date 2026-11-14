"""Reviewed idea projections over exact immutable Library revision spans.

These functions only read the existing canonical library; they never replace sources,
rewrite historical author containers, or promote structural blocks into reviewed ideas.
"""

from __future__ import annotations

import copy
from typing import Any

from .attribution import (
    ASSURANCE,
    attributed_spans,
    owner_profile_eligible,
    require_text,
    text_digest,
    validate_range,
)
from .library_store import Library

ZETTEL_SCHEMA = "cce.reviewed-zettels.v1"


def _message(library: Library, reference: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(reference, dict):
        raise ValueError("source reference must be an object")
    uid = require_text(reference.get("uid"), "source uid")
    version = require_text(reference.get("version"), "source version")
    revision_digest = require_text(reference.get("revision_digest"), "revision_digest")
    matches = [item for item in library.history(uid) if item["version"] == version]
    if len(matches) != 1 or matches[0]["digest"] != revision_digest:
        raise ValueError("missing, ambiguous or stale source revision digest")
    payload = library.record(uid, version=version)
    sequence = reference.get("sequence")
    if sequence not in ("main", "alternate"):
        raise ValueError("source sequence must explicitly select main or alternate")
    messages = (
        payload["messages"]
        if sequence == "main"
        else payload.get("extra", {}).get("alternate_messages", [])
    )
    index = reference.get("message")
    if (
        not isinstance(messages, list)
        or any(not isinstance(item, dict) for item in messages)
        or isinstance(index, bool)
        or not isinstance(index, int)
        or not 0 <= index < len(messages)
    ):
        raise ValueError("source message index is outside the selected sequence")
    message = messages[index]
    message_id = require_text(reference.get("message_id"), "source message_id")
    if (
        message.get("id") != message_id
        or sum(item.get("id") == message_id for item in messages) != 1
    ):
        raise ValueError("source message_id is missing, ambiguous or does not match its index")
    text = message.get("text")
    if not isinstance(text, str) or reference.get("text_digest") != text_digest(text):
        raise ValueError("source text digest is stale or missing")
    return payload, message


def resolve_source_span(library: Library, reference: dict[str, Any]) -> dict[str, Any]:
    """Resolve exact half-open offsets; reject guesses, stale bindings and clipping."""
    payload, message = _message(library, reference)
    start, end = reference.get("start"), reference.get("end")
    validate_range(start, end, len(message["text"]))
    return {
        "source": copy.deepcopy(reference),
        "text": message["text"][start:end],
        "source_created": payload.get("created", ""),
        "source_updated": payload.get("updated", ""),
        "message_timestamp": message.get("ts", ""),
        "role": message.get("role", "unknown"),
        "channel": message.get("channel", ""),
    }


def collect_owner_segments(
    library: Library, annotations: list[dict[str, Any]], owner_id: str
) -> tuple[list[str], list[str]]:
    """Project a chosen set of canonical message annotations into owner/other text.

    Each annotation references a whole message. Duplicate references are rejected to
    prevent inflated profile frequencies. Uncovered portions remain unknown.

    A message that is unchanged across several accepted revisions of one source is
    counted once: each span is keyed by ``(uid, sequence, message_id, span text digest)``
    plus its occurrence ordinal among identical spans of that message and whether it is
    owner-eligible, so repeated identical spans inside one message still count separately
    and a span first seen unattributed can still count once when a later revision asserts it.
    """
    require_text(owner_id, "owner_id")
    if not isinstance(annotations, list):
        raise ValueError("annotations must be a list")
    owner: list[str] = []
    other: list[str] = []
    seen: set[tuple[str, str, str, int]] = set()
    counted: set[tuple[str, str, str, str, int, bool]] = set()
    for entry in annotations:
        if not isinstance(entry, dict):
            raise ValueError("annotation entry must be an object")
        ref = entry.get("source")
        if not isinstance(ref, dict):
            raise ValueError("source reference must be an object")
        _, message = _message(library, ref)
        identity = (ref["uid"], ref["version"], ref["sequence"], ref["message"])
        if identity in seen:
            raise ValueError("duplicate annotation for canonical source message")
        seen.add(identity)
        ordinals: dict[str, int] = {}
        for span in attributed_spans(message["text"], entry.get("attribution")):
            span_digest = text_digest(span["text"])
            ordinal = ordinals.get(span_digest, 0)
            ordinals[span_digest] = ordinal + 1
            eligible = owner_profile_eligible(span, owner_id)
            span_identity = (
                ref["uid"],
                ref["sequence"],
                ref["message_id"],
                span_digest,
                ordinal,
                eligible,
            )
            if span_identity in counted:
                continue
            counted.add(span_identity)
            (owner if eligible else other).append(span["text"])
    return owner, other


def _source_evidence(library: Library, entry: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ValueError("Zettel source entry must be an object")
    ref = entry.get("source")
    if not isinstance(ref, dict):
        raise ValueError("source reference must be an object")
    resolved = resolve_source_span(library, ref)
    _, message = _message(library, ref)
    attribution = []
    for span in attributed_spans(message["text"], entry.get("attribution")):
        start, end = max(span["start"], ref["start"]), min(span["end"], ref["end"])
        if start < end:
            attribution.append(
                {**span, "start": start, "end": end, "text": message["text"][start:end]}
            )
    return {
        **resolved,
        "attribution": copy.deepcopy(entry.get("attribution")),
        "attributed_spans": attribution,
    }


def export_zettels(
    library: Library, zettels: list[dict[str, Any]], *, allow_agent_drafts: bool = False
) -> dict[str, Any]:
    """Validate and export reviewed semantic units without minting or changing IDs.

    Review and authorship are supplied assertions, not automatic independent proof.
    Relationship targets remain caller IDs and may refer to units in another export.
    The Obsidian note emitter may explicitly accept digest-bound agent drafts;
    reviewed JSON/atoms and other existing callers retain their reviewed-only contract.
    """
    if not isinstance(zettels, list):
        raise ValueError("zettels must be a list")
    result, seen = [], set()
    for zettel in zettels:
        if not isinstance(zettel, dict):
            raise ValueError("Zettel must be an object")
        identity = require_text(zettel.get("id"), "Zettel id")
        if identity in seen:
            raise ValueError("duplicate Zettel id")
        seen.add(identity)
        title = require_text(zettel.get("title"), "Zettel title")
        body = require_text(zettel.get("body"), "Zettel body")
        review = zettel.get("review")
        explicit_draft = (isinstance(review, dict) and review.get("status") == "draft") or (
            review is None
            and zettel.get("authorship") == "agent"
            and zettel.get("status") == "draft"
        )
        draft = allow_agent_drafts and explicit_draft
        if not draft and (not isinstance(review, dict) or review.get("status") != "reviewed"):
            raise ValueError("only explicitly reviewed semantic Zettels can be exported")
        review = copy.deepcopy(review) if isinstance(review, dict) else {"status": "draft"}
        if not draft or review.get("reviewer") is not None:
            require_text(review.get("reviewer"), "Zettel reviewer")
        evidence = review.get("evidence", [] if draft else None)
        if not isinstance(evidence, list) or (not evidence and not draft):
            raise ValueError("Zettel review needs evidence")
        for value in evidence:
            require_text(value, "Zettel review evidence")
        relationships = zettel.get("relationships", [])
        if not isinstance(relationships, list):
            raise ValueError("relationships must be a list")
        for relationship in relationships:
            if not isinstance(relationship, dict):
                raise ValueError("relationship must be an object")
            require_text(relationship.get("type"), "relationship type")
            require_text(relationship.get("target"), "relationship target")
        sources = zettel.get("sources")
        if not isinstance(sources, list) or not sources:
            raise ValueError("Zettel needs at least one exact source span")
        if not isinstance(zettel.get("attribution"), dict):
            raise ValueError("Zettel body needs explicit digest-bound attribution")
        body_attribution = attributed_spans(body, zettel["attribution"])
        if draft and not all(
            span["author"] == "agent" and span["assertion_status"] == "asserted"
            for span in body_attribution
        ):
            raise ValueError("a draft needs complete digest-bound asserted agent attribution")
        review.setdefault("evidence", evidence)
        result.append(
            {
                **copy.deepcopy(zettel),
                "id": identity,
                "title": title,
                "body": body,
                "review": review,
                "relationships": copy.deepcopy(relationships),
                "body_attribution": body_attribution,
                "title_spans": attributed_spans(title, zettel.get("title_attribution")),
                "sources": [_source_evidence(library, entry) for entry in sources],
            }
        )
    return {"schema": ZETTEL_SCHEMA, "assurance": ASSURANCE, "zettels": result}


def zettel_atoms(library: Library, zettels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Wrap fully validated Zettels for explicit sealed-archive .atoms.jsonl intake.

    New-unit timestamps remain caller metadata; source timestamps remain separately
    preserved inside the complete Zettel. Nothing infers the new unit's creation time
    from its quotations. The sealed importer enforces its independent size budget.
    """
    exported = export_zettels(library, zettels)
    return [
        {
            "schema": "cce-knowledge-atom.v1",
            "id": zettel["id"],
            "text": zettel["body"],
            "authorship": {
                "assurance": ASSURANCE,
                "body": copy.deepcopy(zettel["body_attribution"]),
                "title": copy.deepcopy(zettel["title_spans"]),
            },
            "source": {
                "kind": "reviewed_zettel",
                "id": zettel["id"],
                "review": copy.deepcopy(zettel["review"]),
            },
            "timestamps": {
                "created": zettel.get("created", ""),
                "updated": zettel.get("updated", ""),
            },
            "derived_from": [copy.deepcopy(source["source"]) for source in zettel["sources"]],
            "zettel": zettel,
        }
        for zettel in exported["zettels"]
    ]
