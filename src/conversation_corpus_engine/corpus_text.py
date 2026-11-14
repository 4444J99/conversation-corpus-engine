"""Text heuristics shared by every corpus builder.

The sentence splitter, keyword and vector-term counters, action and open-question detectors,
concept entities, near-duplicate screen and the prompt/response pair builder used to be copied
into each export importer. They live here once. The two export importers import them from this
module with their behavior unchanged, and the archive corpus projector (`archive_corpus`) uses
the same functions, so a heuristic has one definition however a corpus is built.

Nothing here reads a file, a clock or a provider format: every function is a pure function of
the text it is given.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable, Iterable
from difflib import SequenceMatcher
from typing import Any

from .answering import tokenize

STOP_WORDS = {
    "a",
    "about",
    "all",
    "also",
    "an",
    "and",
    "any",
    "are",
    "as",
    "at",
    "be",
    "been",
    "being",
    "but",
    "by",
    "can",
    "could",
    "do",
    "for",
    "from",
    "get",
    "going",
    "have",
    "if",
    "in",
    "into",
    "is",
    "it",
    "just",
    "like",
    "make",
    "maybe",
    "more",
    "need",
    "not",
    "of",
    "on",
    "or",
    "our",
    "out",
    "say",
    "should",
    "so",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "they",
    "this",
    "to",
    "want",
    "we",
    "what",
    "where",
    "will",
    "with",
    "you",
    "your",
}


ACTION_MARKERS = (
    "need to",
    "needs to",
    "should",
    "will",
    "want to",
    "implement",
    "add",
    "build",
    "develop",
    "next step",
    "recommend",
)


UNRESOLVED_MARKERS = (
    "maybe",
    "perhaps",
    "unclear",
    "unknown",
    "whether",
    "what if",
    "which option",
)


def normalize_whitespace(text: str) -> str:
    return " ".join(text.split()).strip()


def shorten(text: str, limit: int = 320) -> str:
    cleaned = normalize_whitespace(text)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 3)].rstrip() + "..."


def split_sentences(text: str) -> list[str]:
    rough = []
    for line in text.splitlines():
        stripped_line = line.strip()
        if not stripped_line:
            continue
        rough.extend(
            part.strip()
            for part in stripped_line.replace("?", "?\n")
            .replace("!", "!\n")
            .replace(".", ".\n")
            .splitlines()
        )
    return [normalize_whitespace(item) for item in rough if normalize_whitespace(item)]


def top_keywords(text: str, *, limit: int = 12) -> list[str]:
    counts = Counter(
        token for token in tokenize(text) if token not in STOP_WORDS and len(token) > 2
    )
    return [token for token, _ in counts.most_common(limit)]


def vector_terms(text: str, *, limit: int = 18) -> dict[str, float]:
    counts = Counter(
        token for token in tokenize(text) if token not in STOP_WORDS and len(token) > 2
    )
    if not counts:
        return {}
    highest = max(counts.values())
    return {
        token: round(count / highest, 4)  # allow-secret
        for token, count in counts.most_common(limit)
    }


def dedupe_preserve(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        cleaned = normalize_whitespace(value)
        lowered = cleaned.lower()
        if not cleaned or lowered in seen:
            continue
        seen.add(lowered)
        ordered.append(cleaned)
    return ordered


def extract_actions(sentences: list[str]) -> list[str]:
    candidates = []
    for sentence in sentences:
        lowered = sentence.lower()
        if len(sentence) < 24 or len(sentence) > 220:
            continue
        if any(marker in lowered for marker in ACTION_MARKERS):
            candidates.append(shorten(sentence, 180))
    return dedupe_preserve(candidates)[:6]


def extract_unresolved(sentences: list[str]) -> list[str]:
    candidates = []
    for sentence in sentences:
        lowered = sentence.lower()
        if len(sentence) < 24 or len(sentence) > 220:
            continue
        if sentence.endswith("?") or any(marker in lowered for marker in UNRESOLVED_MARKERS):
            candidates.append(shorten(sentence, 180))
    return dedupe_preserve(candidates)[:5]


def build_entities(title: str, keywords: list[str]) -> list[dict[str, str]]:
    entities = [{"canonical_label": title, "entity_type": "conversation"}]
    for keyword in keywords[:3]:
        entities.append(
            {"canonical_label": keyword.replace("-", " ").title(), "entity_type": "concept"}
        )
    seen: set[str] = set()
    result: list[dict[str, str]] = []
    for entity in entities:
        label = normalize_whitespace(entity["canonical_label"])
        lowered = label.lower()
        if not label or lowered in seen:
            continue
        seen.add(lowered)
        result.append({"canonical_label": label, "entity_type": entity["entity_type"]})
    return result


def _trigram_fingerprint(text: str) -> frozenset[str]:
    """Build character trigram set from tokenized text for fast similarity screening."""
    tokens = [t for t in tokenize(text.lower()) if t not in STOP_WORDS and len(t) > 2]
    trigrams: set[str] = set()
    for token in tokens:
        for k in range(len(token) - 2):
            trigrams.add(token[k : k + 3])
    return frozenset(trigrams)


def detect_near_duplicates(
    threads: list[dict[str, Any]],
    first_prompts: dict[str, str],
    *,
    threshold: float = 0.92,
    throttle: float = 0.0,
) -> list[dict[str, Any]]:
    """Detect near-duplicate threads by comparing first user prompts.

    Uses a trigram Jaccard pre-filter to avoid expensive SequenceMatcher calls
    on clearly dissimilar pairs. The Jaccard threshold (0.35) is conservative —
    any pair with SequenceMatcher ratio >= 0.92 will pass the pre-filter.
    """
    candidates: list[dict[str, Any]] = []
    uid_to_index = {t["thread_uid"]: idx for idx, t in enumerate(threads)}
    uids = [t["thread_uid"] for t in threads]

    # Pre-compute fingerprints for eligible prompts
    fingerprints: dict[str, frozenset[str]] = {}
    for uid in uids:
        prompt = first_prompts.get(uid, "")
        if len(prompt) >= 20:
            fingerprints[uid] = _trigram_fingerprint(prompt[:500])

    eligible = [uid for uid in uids if uid in fingerprints]

    for i in range(len(eligible)):
        fp_a = fingerprints[eligible[i]]
        prompt_a = first_prompts[eligible[i]][:500]
        if throttle > 0 and i % 50 == 0:
            time.sleep(throttle)
        for j in range(i + 1, len(eligible)):
            fp_b = fingerprints[eligible[j]]
            # Fast Jaccard pre-filter (frozenset ops, microseconds per pair)
            union_size = len(fp_a | fp_b)
            if union_size == 0:
                continue
            if len(fp_a & fp_b) / union_size < 0.35:
                continue
            # Expensive comparison only for plausible candidates
            prompt_b = first_prompts[eligible[j]][:500]
            ratio = SequenceMatcher(None, prompt_a, prompt_b).ratio()
            if ratio >= threshold:
                candidates.append(
                    {
                        "thread_uids": [eligible[i], eligible[j]],
                        "similarity": round(ratio, 4),
                        "titles": [
                            threads[uid_to_index[eligible[i]]].get("title_normalized", ""),
                            threads[uid_to_index[eligible[j]]].get("title_normalized", ""),
                        ],
                    }
                )
    return candidates


PROMPT = "prompt"
RESPONSE = "response"


def build_pairs_from_turns(
    turns: Iterable[tuple[str, str, str]],
    thread_uid: str,
    family_id: str,
    *,
    label: str,
    make_pair_id: Callable[[int, str], str],
) -> list[dict[str, Any]]:
    """Group a conversation's turns into prompt/response pairs.

    `turns` holds `(kind, text, key)` in conversation order, where `kind` is PROMPT or RESPONSE
    (a caller drops turns that are neither) and `key` is whatever the caller wants to hand to
    `make_pair_id`. A pair opens at each prompt and gathers the responses that follow; responses
    before any prompt form one assistant-led pair. `make_pair_id(index, key)` mints the pair id
    from the pair's 1-based position and the key of the turn that opened it. `label` names the
    provider in the fallback title.
    """
    pairs: list[dict[str, Any]] = []
    current_prompt = ""
    current_response_parts: list[str] = []
    current_title = ""
    current_key = ""
    pair_index = 0

    def pair_record(title: str) -> dict[str, Any]:
        response_text = normalize_whitespace(" ".join(current_response_parts))
        summary = shorten(f"User: {current_prompt} Assistant: {response_text}", 240)
        pair_id = make_pair_id(pair_index, current_key)
        return {
            "pair_id": pair_id,
            "thread_uid": thread_uid,
            "title": title,
            "summary": summary,
            "search_text": f"{pair_id} {current_prompt} {response_text}",
            "themes": top_keywords(f"{current_prompt} {response_text}", limit=8),
            "entities": [],
            "family_ids": [family_id],
            "vector_terms": vector_terms(f"{current_prompt} {response_text}"),
        }

    for kind, text, key in turns:
        if kind == PROMPT:
            if current_prompt or current_response_parts:
                pair_index += 1
                pairs.append(
                    pair_record(
                        current_title
                        or shorten(current_prompt, 80)
                        or f"{label} Pair {pair_index:03d}"
                    )
                )
            current_prompt = text
            current_title = shorten(text, 80)
            current_response_parts = []
            current_key = key
        elif kind == RESPONSE:
            if not current_prompt:
                current_prompt = "[assistant-led]"
                current_title = "Assistant-Led Turn"
                current_key = key
            current_response_parts.append(text)

    if current_prompt or current_response_parts:
        pair_index += 1
        pairs.append(pair_record(current_title or f"{label} Pair {pair_index:03d}"))
    return pairs
