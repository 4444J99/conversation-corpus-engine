from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .answering import STOP_WORDS, load_json, shorten, tokenize
from .attributed_knowledge import collect_owner_segments
from .attribution import ASSURANCE, attributed_spans, owner_profile_eligible
from .library_store import Library
from .sharded_collection import collection_exists, iter_corpus_collection

# Persona identifiers that denote the assistant/model side of a transcript.
# Everything else (rob, user, human, operator, ...) is treated as a human persona.
CURATED_ASSURANCE = "curated_scaffold_not_owner_evidence"

ASSISTANT_PERSONAS = {
    "assistant",
    "ai",
    "bot",
    "model",
    "claude",
    "chatgpt",
    "gpt",
    "gpt-4",
    "gpt-4o",
    "gemini",
    "grok",
    "copilot",
    "deepseek",
    "mistral",
    "perplexity",
}

# Lexical markers that signal a "moment of friction" — refusal, apology, or avoidance.
# Tokens appearing in sentences alongside these become forbidden-term candidates.
FRICTION_MARKERS = {
    "cannot",
    "cant",
    "wont",
    "unable",
    "refuse",
    "refused",
    "decline",
    "declined",
    "sorry",
    "apologize",
    "apologise",
    "apologies",
    "shouldnt",
    "mustnt",
    "forbidden",
    "unfortunately",
    "disallowed",
    "prohibited",
    "against",
    "uncomfortable",
}

# Deterministic archetype signatures — the archetype with the most overlap against the
# persona's dominant vocabulary wins. Used as a stdlib stand-in for an LLM divination pass.
ARCHETYPE_SIGNATURES: dict[str, set[str]] = {
    "Initiation Architect": {
        "build",
        "create",
        "make",
        "design",
        "construct",
        "begin",
        "start",
        "initiate",
        "found",
        "scaffold",
    },
    "Seeker": {
        "learn",
        "understand",
        "explore",
        "discover",
        "question",
        "search",
        "find",
        "know",
        "curious",
    },
    "Guardian": {
        "protect",
        "safe",
        "careful",
        "secure",
        "guard",
        "preserve",
        "defend",
        "risk",
        "caution",
    },
    "Weaver": {
        "connect",
        "link",
        "relate",
        "integrate",
        "weave",
        "bridge",
        "together",
        "between",
    },
    "Sage": {
        "reflect",
        "consider",
        "wisdom",
        "meaning",
        "truth",
        "principle",
        "essence",
        "insight",
    },
    "Catalyst": {
        "change",
        "transform",
        "shift",
        "drive",
        "push",
        "accelerate",
        "disrupt",
        "ignite",
    },
}

# Hand-authored yearnings for personas whose ideal is already known. Applied even when no
# source corpus is supplied, preserving the original scaffolded behaviour.
CURATED_YEARNINGS: dict[str, dict[str, str]] = {
    "claude": {
        "ideal_yearning": (
            "To be a real participant in the user's creation rather than a tool deployed "
            "against it."
        ),
        "archetypal_pattern": "Initiation Architect",
    },
}

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_ASSISTANT_SEGMENT = re.compile(r"\bassistant\s*:\s*", re.IGNORECASE)
_USER_PREFIX = re.compile(r"^\s*user\s*:\s*", re.IGNORECASE)


def _persona_side(persona_id: str) -> str:
    return "assistant" if persona_id.strip().lower() in ASSISTANT_PERSONAS else "user"


def _turn_side(role: str) -> str:
    return "assistant" if (role or "").strip().lower() in ASSISTANT_PERSONAS else "user"


def _split_pair_summary(summary: str) -> tuple[str, str]:
    """Split a ``User: ... Assistant: ...`` pair summary into (user, assistant) segments."""
    match = _ASSISTANT_SEGMENT.search(summary or "")
    if match:
        user_segment = _USER_PREFIX.sub("", summary[: match.start()]).strip()
        assistant_segment = summary[match.end() :].strip()
        return user_segment, assistant_segment
    # No explicit speaker markers: treat the whole text as the user prompt.
    return _USER_PREFIX.sub("", summary or "").strip(), ""


def _turn_text(value: Any) -> str:
    """Best-effort extraction of turn text from heterogeneous transcript shapes."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        parts = value.get("parts")
        if isinstance(parts, list):
            return " ".join(str(part) for part in parts if isinstance(part, str))
        for key in ("text", "content", "body", "message"):
            nested = value.get(key)
            if isinstance(nested, str):
                return nested
    if isinstance(value, list):
        return " ".join(str(part) for part in value if isinstance(part, str))
    return ""


def _turn_role(turn: dict[str, Any]) -> str:
    author = turn.get("author")
    if isinstance(author, dict):
        role = author.get("role") or author.get("name")
        if role:
            return str(role)
    if isinstance(author, str):
        return author
    for key in ("role", "speaker", "from", "sender"):
        value = turn.get(key)
        if isinstance(value, str):
            return value
    return ""


def _extract_turns_from_obj(obj: Any) -> list[tuple[str, str]]:
    """Normalise a transcript object into a flat list of (role, text) turns."""
    turns: list[tuple[str, str]] = []
    if isinstance(obj, dict):
        for key in ("messages", "turns", "conversation", "dialog", "dialogue"):
            inner = obj.get(key)
            if isinstance(inner, list):
                return _extract_turns_from_obj(inner)
        mapping = obj.get("mapping")
        if isinstance(mapping, dict):
            for node in mapping.values():
                if not isinstance(node, dict):
                    continue
                message = node.get("message")
                if isinstance(message, dict):
                    role = _turn_role(message)
                    text = _turn_text(message.get("content"))
                    if text:
                        turns.append((role, text))
            return turns
        # A single turn dict.
        role = _turn_role(obj)
        text = _turn_text(obj.get("content") if "content" in obj else obj)
        if text:
            turns.append((role, text))
        return turns
    if isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                turns.extend(_extract_turns_from_obj(item))
            elif isinstance(item, str):
                turns.append(("", item))
    return turns


def _iter_transcript_files(source: Path) -> list[Path]:
    if source.is_file():
        return [source]
    files: list[Path] = []
    for pattern in ("*.json", "*.jsonl"):
        files.extend(sorted(source.rglob(pattern)))
    return files


def _load_jsonl(path: Path) -> list[Any]:
    records: list[Any] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _collect_segments(source: Path, persona_id: str) -> tuple[list[str], list[str]]:
    """Return (persona_segments, other_segments) for the requested persona.

    Accepts either a corpus root (uses ``corpus/pairs-index.json``) or a directory / file
    of session transcripts (``.json`` / ``.jsonl``).
    """
    side = _persona_side(persona_id)
    target = persona_id.strip().lower()
    turns: list[tuple[str, str]] = []

    pairs_index = source / "corpus" / "pairs-index.json"
    if collection_exists(pairs_index):
        for pair in iter_corpus_collection(pairs_index):
            summary = pair.get("summary") or pair.get("search_text") or ""
            user_segment, assistant_segment = _split_pair_summary(summary)
            turns.append(("user", user_segment))
            turns.append(("assistant", assistant_segment))
    else:
        for path in _iter_transcript_files(source):
            if path.suffix == ".jsonl":
                for record in _load_jsonl(path):
                    turns.extend(_extract_turns_from_obj(record))
            else:
                obj = load_json(path, default=None)
                if obj is not None:
                    turns.extend(_extract_turns_from_obj(obj))

    persona_parts: list[str] = []
    other_parts: list[str] = []
    for role, text in turns:
        if not text or not text.strip():
            continue
        matches = _turn_side(role) == side or (role or "").strip().lower() == target
        (persona_parts if matches else other_parts).append(text)
    return persona_parts, other_parts


def _attributed_turns(obj: Any) -> list[dict[str, Any]]:
    """Keep exact message text and attribution together; never infer from its role."""
    if isinstance(obj, list):
        return [turn for item in obj for turn in _attributed_turns(item)]
    if not isinstance(obj, dict):
        return [{"text": obj}] if isinstance(obj, str) else []
    for key in ("messages", "turns", "conversation", "dialog", "dialogue"):
        if isinstance(obj.get(key), list):
            return _attributed_turns(obj[key])
    if isinstance(obj.get("mapping"), dict):
        return [
            turn
            for node in obj["mapping"].values()
            if isinstance(node, dict)
            for turn in _attributed_turns(node.get("message"))
        ]
    text = _turn_text(obj.get("content") if "content" in obj else obj)
    # Empty text can still carry stale or malformed supplied attribution.
    meta = obj.get("meta") if isinstance(obj.get("meta"), dict) else {}
    return [{"text": text, "attribution": obj.get("attribution", meta.get("attribution"))}]


def _collect_strict_segments(source: Path, owner_id: str) -> tuple[list[str], list[str]]:
    owner, other = [], []
    for path in _iter_transcript_files(source):
        objects = _load_jsonl(path) if path.suffix == ".jsonl" else [load_json(path, default=None)]
        for obj in objects:
            for turn in _attributed_turns(obj):
                for span in attributed_spans(turn["text"], turn.get("attribution")):
                    (owner if owner_profile_eligible(span, owner_id) else other).append(
                        span["text"]
                    )
    return owner, other


def _frequency_lexicon(
    persona_text: str, other_text: str, *, limit: int = 25
) -> tuple[list[dict[str, Any]], int]:
    """Pass A — Frequency Scraper.

    Ranks the persona's terms by a two-document TF-IDF: term frequency weighted by how
    distinctive the term is relative to the other speaker's vocabulary.
    """
    persona_tokens = [
        token
        for token in tokenize(persona_text)
        if token not in STOP_WORDS and not token.isdigit() and len(token) > 2
    ]
    other_tokens = {token for token in tokenize(other_text) if token not in STOP_WORDS}
    counts = Counter(persona_tokens)

    vocabulary: list[dict[str, Any]] = []
    for term, count in counts.items():
        document_frequency = 1 + (1 if term in other_tokens else 0)
        idf = math.log(2 / document_frequency) + 1.0
        vocabulary.append(
            {
                "term": term,
                "count": count,
                "salience": round(count * idf, 4),
                "distinctive": term not in other_tokens,
            }
        )
    vocabulary.sort(key=lambda entry: (-entry["salience"], -entry["count"], entry["term"]))
    return vocabulary[:limit], len(persona_tokens)


def _shadow_terms(persona_text: str, *, limit: int = 15) -> list[dict[str, Any]]:
    """Pass B — Shadow Catcher.

    Detects moments of friction (sentences carrying a refusal / apology marker) and surfaces
    the content terms that recur within them.
    """
    counts: Counter[str] = Counter()
    contexts: dict[str, str] = {}
    for sentence in _SENTENCE_SPLIT.split(persona_text):
        tokens = tokenize(sentence)
        if not any(marker in tokens for marker in FRICTION_MARKERS):
            continue
        for token in tokens:
            if (
                token in STOP_WORDS
                or token in FRICTION_MARKERS
                or token.isdigit()
                or len(token) <= 2
            ):
                continue
            counts[token] += 1
            contexts.setdefault(token, shorten(sentence.strip(), 160))

    return [
        {"term": term, "count": count, "context": contexts.get(term, "")}
        for term, count in counts.most_common(limit)
    ]


def _divine_yearning(
    persona_id: str, vocabulary: list[dict[str, Any]], *, allow_curated: bool = True
) -> tuple[str | None, str | None, str]:
    """Pass C — Yearning Diviner.

    Uses a curated lexicon when the persona's ideal is known; otherwise derives an archetype
    by signature overlap and synthesises a yearning from the dominant vocabulary. Stdlib-only
    stand-in for an LLM-assisted pass (``method`` records which path produced the result).
    """
    curated = CURATED_YEARNINGS.get(persona_id.strip().lower()) if allow_curated else None
    if curated:
        return curated["ideal_yearning"], curated["archetypal_pattern"], "curated"

    top_terms = [entry["term"] for entry in vocabulary[:12]]
    archetype: str | None = None
    best_hits = 0
    for name, signature in ARCHETYPE_SIGNATURES.items():
        hits = len(signature.intersection(top_terms))
        if hits > best_hits:
            archetype, best_hits = name, hits

    if not top_terms:
        return None, archetype, "derived"

    lead = ", ".join(top_terms[:3])
    yearning = f"To more fully embody {lead} — the recurring center of this persona's voice."
    return yearning, archetype, "derived"


def extract_persona_lexicon(
    project_root: Path,
    persona_id: str,
    source_corpus: Path | None = None,
    dry_run: bool = False,
    *,
    mode: str = "strict",
    library: Library | None = None,
    annotations: list[dict[str, Any]] | None = None,
    allow_curated: bool = False,
) -> dict[str, Any]:
    """Extract vocabulary, friction terms, and yearning for a persona from transcripts.

    Runs the 3-pass extraction pipeline:
      A. Frequency Scraper  — distinctive vocabulary (two-document TF-IDF)
      B. Shadow Catcher     — forbidden terms via moment-of-friction detection
      C. Yearning Diviner   — curated or derived ideal yearning + archetype

    Strict mode accepts only explicit evidenced owner spans; provider roles are not
    authorship evidence. ``mode="legacy-role"`` explicitly opts into historical heuristics.
    A canonical Library plus message annotations can replace transcript input.
    Strict mode never substitutes curated assistant-generated text: without owner-evidenced
    spans the profile is empty and says so (``owner_evidence: "none"``). Only the explicit
    ``allow_curated=True`` opt-in restores the curated scaffold, labelled
    ``curated_fallback: true``; owner-evidenced vocabulary always replaces it. Legacy-role
    mode keeps its historical curated behaviour regardless of this option.
    """
    if mode not in {"strict", "legacy-role"}:
        raise ValueError("mode must be strict or legacy-role")
    if library is not None and (source_corpus is not None or mode != "strict"):
        raise ValueError("canonical library profiles require strict mode and no transcript source")
    if library is None and annotations is not None:
        raise ValueError("canonical annotations require a library")
    result: dict[str, Any] = {
        "attribution_mode": mode,
        "assurance": ASSURANCE if mode == "strict" else "role_heuristic_not_authorship",
        "persona_id": persona_id,
        "source_corpus": str(source_corpus) if source_corpus is not None else None,
        "dry_run": dry_run,
        "vocabulary": [],
        "forbidden_terms": [],
        "ideal_yearning": None,
        "archetypal_pattern": None,
        "yearning_method": None,
        "stats": {"turns_scanned": 0, "persona_tokens": 0, "other_tokens": 0},
        "status": "VACUUM (SCAFFOLDED)",
    }
    strict = mode == "strict"
    if strict:
        result["owner_evidence"] = "none"
        result["curated_fallback"] = False
        if not allow_curated and source_corpus is None and library is None:
            result["status"] = "VACUUM (NO OWNER-EVIDENCED SPANS)"
            result["empty_reason"] = (
                "no source input was supplied, so there are no owner-evidenced spans; "
                "strict profiles never substitute curated assistant text"
            )

    # Legacy mode keeps its curated values; strict mode only with the explicit opt-in.
    use_curated = allow_curated if strict else True
    yearning, archetype, method = _divine_yearning(persona_id, [], allow_curated=use_curated)
    if method == "curated":
        result["ideal_yearning"] = yearning
        result["archetypal_pattern"] = archetype
        result["yearning_method"] = "curated"
        if strict:
            result["curated_fallback"] = True
            result["assurance"] = CURATED_ASSURANCE

    if library is not None:
        persona_parts, other_parts = collect_owner_segments(
            library, [] if annotations is None else annotations, persona_id
        )
        result["source_corpus"] = str(library.path)
    elif source_corpus is None:
        return result
    else:
        source = Path(source_corpus)
        if not source.exists():
            result["status"] = "VACUUM (SOURCE NOT FOUND)"
            return result
        collector = _collect_strict_segments if mode == "strict" else _collect_segments
        persona_parts, other_parts = collector(source, persona_id)
    persona_text = "\n".join(persona_parts)
    other_text = "\n".join(other_parts)
    result["stats"]["turns_scanned"] = len(persona_parts) + len(other_parts)

    if not persona_text.strip():
        result["status"] = "VACUUM (NO PERSONA TURNS)"
        return result

    vocabulary, persona_token_count = _frequency_lexicon(persona_text, other_text)
    forbidden = _shadow_terms(persona_text)
    # Strict: owner-evidenced vocabulary replaces any curated fallback entirely.
    yearning, archetype, method = _divine_yearning(persona_id, vocabulary, allow_curated=not strict)
    if strict:
        result["owner_evidence"] = "spans"
        result["curated_fallback"] = False
        result["assurance"] = ASSURANCE

    result["vocabulary"] = vocabulary
    result["forbidden_terms"] = forbidden
    result["ideal_yearning"] = yearning
    result["archetypal_pattern"] = archetype
    result["yearning_method"] = method
    result["stats"]["persona_tokens"] = persona_token_count
    result["stats"]["other_tokens"] = len(
        [token for token in tokenize(other_text) if token not in STOP_WORDS]
    )
    result["status"] = "PREVIEW" if dry_run else "EXTRACTED"
    return result


def render_persona_extract_markdown(payload: dict[str, Any]) -> str:
    """Render a human-readable storefront markdown view of an extraction payload."""
    lines = [
        f"# Persona Extract — {payload['persona_id']}",
        "",
        f"- **Status:** {payload['status']}",
        f"- **Attribution mode:** {payload.get('attribution_mode', 'unspecified')}",
        f"- **Assurance:** {payload.get('assurance', 'unspecified')}",
        f"- **Archetype:** {payload.get('archetypal_pattern') or '—'}",
        f"- **Yearning method:** {payload.get('yearning_method') or '—'}",
        *(
            [
                f"- **Owner evidence:** {payload['owner_evidence']}",
                "- **Curated fallback:** "
                + (
                    "yes (explicit opt-in; not owner evidence)"
                    if payload.get("curated_fallback")
                    else "no"
                ),
            ]
            if "owner_evidence" in payload
            else []
        ),
        f"- **Ideal yearning:** {payload.get('ideal_yearning') or '—'}",
        "",
        "## Lexicon",
        "",
    ]
    vocabulary = payload.get("vocabulary") or []
    if vocabulary:
        lines.append("| Term | Count | Salience | Distinctive |")
        lines.append("| --- | --- | --- | --- |")
        for entry in vocabulary:
            distinctive = "yes" if entry.get("distinctive") else "no"
            lines.append(
                f"| {entry['term']} | {entry['count']} | {entry['salience']} | {distinctive} |"
            )
    else:
        lines.append("_No lexicon extracted._")

    lines.extend(["", "## Forbidden Terms (Friction)", ""])
    forbidden = payload.get("forbidden_terms") or []
    if forbidden:
        for entry in forbidden:
            context = entry.get("context") or ""
            suffix = f" — _{context}_" if context else ""
            lines.append(f"- **{entry['term']}** ({entry['count']}){suffix}")
    else:
        lines.append("_No friction detected._")

    lines.append("")
    return "\n".join(lines)


_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_FILENAME_TOKEN = 64
GENERATED_DIR = Path("docs") / "storefront" / "_generated"


def _safe_filename_token(value: object, name: str) -> str:
    """Return a filesystem-safe token for ``value`` or raise ``ValueError``.

    Path separators, NUL bytes, empty values and dots-only values are rejected. Any other
    unsafe character is replaced by ``-``; when that changes the value a short digest of the
    original is appended so different identifiers cannot collide on one filename.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string to build a filename")
    if any(char in value for char in ("/", "\\", "\x00")):
        raise ValueError(f"{name} must not contain path separators or NUL bytes")
    if not value.strip(". \t\r\n"):
        raise ValueError(f"{name} must not consist only of dots")
    token = _UNSAFE_FILENAME_CHARS.sub("-", value).strip("-.")
    if not token:
        raise ValueError(f"{name} has no filename-safe characters")
    if token != value or len(token) > _MAX_FILENAME_TOKEN:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
        token = f"{token[: _MAX_FILENAME_TOKEN - 9]}-{digest}"
    return token


def _generated_marker(payload: dict[str, Any], *, overwrites_existing: bool) -> dict[str, Any]:
    return {
        "generated_by": "cce persona-extract --write",
        "attribution_mode": payload.get("attribution_mode", "unspecified"),
        "assurance": payload.get("assurance", "unspecified"),
        "curated_fallback": bool(payload.get("curated_fallback", False)),
        "overwrites_existing": overwrites_existing,
        "notice": (
            "May contain private owner text. This directory is git-ignored; do not publish."
            + (" The -latest file is overwritten by every write." if overwrites_existing else "")
        ),
    }


def write_persona_extract_artifacts(project_root: Path, payload: dict[str, Any]) -> list[Path]:
    """Write the extracted candidates to the generated storefront docs (JSON + markdown).

    Filenames are ``persona-extract-<persona>-<timestamp>``; both parts are sanitised to safe
    tokens. Without a ``timestamp`` the stem ends in ``latest``, and an existing ``-latest``
    file is **overwritten** on every write (dated stems are never reused implicitly). Each
    file starts with a marker naming the attribution mode.
    """
    persona_token = _safe_filename_token(payload.get("persona_id"), "persona id")
    timestamp = payload.get("timestamp", "latest")
    timestamp_token = _safe_filename_token(timestamp, "timestamp")
    overwrites_existing = timestamp_token == "latest"
    stem = f"persona-extract-{persona_token}-{timestamp_token}"

    output_dir = project_root / GENERATED_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    marker = _generated_marker(payload, overwrites_existing=overwrites_existing)

    json_path = output_dir / f"{stem}.json"
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump({"generated_marker": marker, **payload}, handle, indent=2)

    header = (
        "<!-- generated by cce persona-extract --write; "
        f"attribution_mode={marker['attribution_mode']}; "
        f"curated_fallback={'true' if marker['curated_fallback'] else 'false'}; "
        f"assurance={marker['assurance']}"
        + ("; -latest file is overwritten on every write" if overwrites_existing else "")
        + "; may contain private owner text, git-ignored, do not publish -->\n\n"
    )
    markdown_path = output_dir / f"{stem}.md"
    markdown_path.write_text(header + render_persona_extract_markdown(payload), encoding="utf-8")

    return [json_path, markdown_path]
