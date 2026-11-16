"""The shared text heuristics were moved out of the two export importers without changing them.

The golden values below were produced by the importers' own code before the move (the literals by
running the old `split_sentences` and friends from the previous commit). The digests were
recomputed after conversation-corpus-engine#78 rewired the importers onto the archive corpus
projector, so they pin the projected collections, not the retired hand-built ones. Every string
here is invented.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from conversation_corpus_engine import (
    corpus_text,
    import_chatgpt_export_corpus,
    import_claude_export_corpus,
)
from conversation_corpus_engine.sharded_collection import load_corpus_collection

CHATGPT_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "chatgpt-export"
# Digests of every collection the importers write, over the fixtures, from the code before the move.
GOLDEN_CHATGPT_DIGEST = "85a0e10c2df9126514bc01ad316cb613d6e7bf045e88422dbb23dda69624f73f"
GOLDEN_CLAUDE_DIGEST = "10ca6dec4ff79ba2dfa37a4c8e28f65ab36d5d0a0ecb3cbe9ce3053441454d37"
# The projected collections only: the rewire retired the importers' extra
# files (entity-aliases, import-audit, near-duplicates) with the hand-built shaping.
DIGESTED = (
    "threads-index.json",
    "semantic-v3-index.json",
    "pairs-index.json",
    "doctrine-briefs.json",
    "family-dossiers.json",
    "canonical-families.json",
    "action-ledger.json",
    "unresolved-ledger.json",
    "canonical-entities.json",
)
MOVED = (
    "STOP_WORDS",
    "ACTION_MARKERS",
    "UNRESOLVED_MARKERS",
    "normalize_whitespace",
    "shorten",
    "split_sentences",
    "top_keywords",
    "vector_terms",
    "dedupe_preserve",
    "extract_actions",
    "extract_unresolved",
    "build_entities",
    "detect_near_duplicates",
)
REPEATED_ASK = "Please build a deterministic federation adapter with audit visibility."
SAMPLE = (
    "We should build a lantern checklist for the weekend trip. "
    "Maybe the wick needs trimming first? I will implement the checklist next.\n"
    "What if the flame stays unsteady? Lantern wicks need attention before every trip."
)


def _digest(output_root: Path) -> str:
    payload: dict[str, Any] = {}
    for name in DIGESTED:
        records = load_corpus_collection(output_root / "corpus" / name, default=None)
        if records is None:
            continue
        for record in records:
            record.pop("thread_path", None)  # holds the temporary output path
        payload[name] = records
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _claude_message(uuid: str, sender: str, when: str, text: str) -> dict[str, Any]:
    return {
        "uuid": uuid,
        "sender": sender,
        "created_at": when,
        "updated_at": when,
        "text": text,
        "content": [{"type": "text", "text": text}],
        "attachments": [],
        "files": [],
    }


def _claude_bundle(root: Path) -> Path:
    bundle = root / "claude-bundle"
    bundle.mkdir(parents=True)
    for name in ("users.json", "projects.json", "memories.json"):
        (bundle / name).write_text("[]", encoding="utf-8")
    conversations = [
        {
            "uuid": "conv-1",
            "name": "Audit thread A",
            "summary": "",
            "created_at": "2026-03-14T10:00:00Z",
            "updated_at": "2026-03-14T10:05:00Z",
            "chat_messages": [
                _claude_message("m1", "human", "2026-03-14T10:00:00Z", REPEATED_ASK),
                _claude_message(
                    "m2",
                    "assistant",
                    "2026-03-14T10:01:00Z",
                    "We should add a replay step. Maybe the audit needs a second pass? "
                    "I will implement the adapter next.",
                ),
                _claude_message(
                    "m3", "human", "2026-03-14T10:02:00Z", "What if the gate is unclear?"
                ),
            ],
        },
        {
            "uuid": "conv-2",
            "name": "Audit thread B",
            "summary": "",
            "created_at": "2026-03-14T11:00:00Z",
            "updated_at": "2026-03-14T11:05:00Z",
            "chat_messages": [
                _claude_message("m4", "human", "2026-03-14T11:00:00Z", REPEATED_ASK),
                _claude_message(
                    "m5",
                    "assistant",
                    "2026-03-14T11:01:00Z",
                    "I will keep parity with the richer adapter path.",
                ),
            ],
        },
    ]
    (bundle / "conversations.json").write_text(json.dumps(conversations), encoding="utf-8")
    return bundle


@pytest.mark.parametrize("module", [import_chatgpt_export_corpus, import_claude_export_corpus])
def test_the_importers_use_the_one_shared_definition(module) -> None:
    # An importer keeps no copy of a moved heuristic: whatever it still names is the shared object.
    defined_here = {
        name
        for name, value in vars(module).items()
        if getattr(value, "__module__", None) == module.__name__
    }
    assert not defined_here & set(MOVED)
    for name in MOVED:
        if hasattr(module, name):
            assert getattr(module, name) is getattr(corpus_text, name), name
    # The importers no longer re-export detect_near_duplicates: the rewire
    # retired near-duplicate screening with the hand-built shaping. The screen
    # itself still lives in corpus_text and is covered by its own tests.
    for name in MOVED:
        owner = getattr(corpus_text, name)
        if callable(owner):
            assert owner.__module__ == "conversation_corpus_engine.corpus_text", name


def test_the_heuristics_give_the_output_the_importers_gave_before_the_move() -> None:
    sentences = corpus_text.split_sentences(SAMPLE)
    assert sentences == [
        "We should build a lantern checklist for the weekend trip.",
        "Maybe the wick needs trimming first?",
        "I will implement the checklist next.",
        "What if the flame stays unsteady?",
        "Lantern wicks need attention before every trip.",
    ]
    keywords = corpus_text.top_keywords(SAMPLE)
    assert keywords == [
        "lantern",
        "checklist",
        "trip",
        "build",
        "weekend",
        "wick",
        "needs",
        "trimming",
        "first",
        "implement",
        "next",
        "flame",
    ]
    vectors = corpus_text.vector_terms(SAMPLE)
    assert vectors["lantern"] == vectors["checklist"] == vectors["trip"] == 1.0
    assert len(vectors) == 18 and vectors["wick"] == 0.5
    assert corpus_text.extract_actions(sentences) == [
        "We should build a lantern checklist for the weekend trip.",
        "I will implement the checklist next.",
    ]
    assert corpus_text.extract_unresolved(sentences) == [
        "Maybe the wick needs trimming first?",
        "What if the flame stays unsteady?",
    ]
    assert corpus_text.build_entities("Lantern plan", keywords) == [
        {"canonical_label": "Lantern plan", "entity_type": "conversation"},
        {"canonical_label": "Lantern", "entity_type": "concept"},
        {"canonical_label": "Checklist", "entity_type": "concept"},
        {"canonical_label": "Trip", "entity_type": "concept"},
    ]
    assert corpus_text.shorten(SAMPLE, 60) == (
        "We should build a lantern checklist for the weekend trip...."
    )
    assert corpus_text.dedupe_preserve(["A  b", "a b", "C", ""]) == ["A b", "C"]


def test_the_chatgpt_importer_writes_the_projector_collections(tmp_path: Path) -> None:
    import_chatgpt_export_corpus.import_chatgpt_export_corpus(
        CHATGPT_FIXTURE, tmp_path / "out", corpus_id="golden-chatgpt"
    )
    assert _digest(tmp_path / "out") == GOLDEN_CHATGPT_DIGEST


def test_the_claude_importer_writes_the_projector_collections(tmp_path: Path) -> None:
    import_claude_export_corpus.import_claude_export_corpus(
        _claude_bundle(tmp_path), tmp_path / "out", corpus_id="golden-claude"
    )
    assert _digest(tmp_path / "out") == GOLDEN_CLAUDE_DIGEST
    duplicates = load_corpus_collection(
        tmp_path / "out" / "corpus" / "near-duplicates.json", default=[]
    )
    assert duplicates == []  # near-duplicate screening left the importer path with the rewire


def test_the_pair_builder_numbers_pairs_and_leads_with_the_assistant_when_it_must() -> None:
    turns = [
        (corpus_text.RESPONSE, "A reply that came first.", "ref-a"),
        (corpus_text.PROMPT, "A first question?", "ref-b"),
        (corpus_text.RESPONSE, "An answer.", "ref-c"),
        (corpus_text.RESPONSE, "More of the answer.", "ref-d"),
        (corpus_text.PROMPT, "A second question?", "ref-e"),
    ]
    minted: list[tuple[int, str]] = []

    def make_pair_id(index: int, key: str) -> str:
        minted.append((index, key))
        return f"pair-{index}"

    pairs = corpus_text.build_pairs_from_turns(
        turns, "thread-1", "family-1", label="Test", make_pair_id=make_pair_id
    )
    assert minted == [(1, "ref-a"), (2, "ref-b"), (3, "ref-e")]
    assert [pair["pair_id"] for pair in pairs] == ["pair-1", "pair-2", "pair-3"]
    assert [pair["title"] for pair in pairs] == [
        "Assistant-Led Turn",
        "A first question?",
        "A second question?",
    ]
    assert (
        pairs[1]["summary"] == "User: A first question? Assistant: An answer. More of the answer."
    )
    assert all(
        pair["thread_uid"] == "thread-1" and pair["family_ids"] == ["family-1"] for pair in pairs
    )
    assert (
        corpus_text.build_pairs_from_turns([], "t", "f", label="Test", make_pair_id=make_pair_id)
        == []
    )
