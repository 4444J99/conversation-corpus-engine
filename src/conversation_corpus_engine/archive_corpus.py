"""The corpus projection: the normalized archive, shaped as engine corpora the federation accepts.

`cce archive project corpus` reads the normalized layer and writes, per provider, one corpus
folder named `corpus-<provider>-archive` with the five collections federation requires and the
four it uses when they are present (canonical families, the action and open-question ledgers and
the concept entities). The folder name is the registry key and the `corpus_id` in its
`contract.json`, so `upsert_corpus`, `build_federation` and the schema check all agree on it.

Rules the projector follows, each tested:

* Every id is minted by `derive_name` through `archive_layout`, from the conversation uid and
  from digests of exact text. No id holds a title or a slug of a sentence, so renaming a
  conversation changes no id.
* Only the current branch is read: a conversation's main message list. Off-branch messages stay
  in the archive and are not corpus text.
* A conversation is kept when its kind is `chat`, `search` or `activity`. Projects, memories and
  notebooks are not conversations and are counted, by kind, as excluded.
* What is read of a message is what a person typed or what the assistant replied: channels
  `typed` and `reply`. Reasoning, tool output and system text stay out. A user message is cut
  off at `typed_chars`, so attachment text a reader appended is not mistaken for typing.
* The text heuristics are the ones in `corpus_text`, the same functions the export importers use.
* A conversation is its own family. Whether two conversations belong together is a federation
  decision (a reviewed family merge), not something the projector guesses.
* Two runs over the same archive write the same bytes: records are in conversation order, every
  timestamp is read from the manifest, nothing reads a clock.
* Only a store in the current manifest and conversation schemas is projected; a first-generation
  store is refused with a rebuild message (see `archive_feed` for why).
* The folder is built beside its final place and swapped in, so a failed run leaves the previous
  corpus untouched.
* A provider with nothing eligible writes no corpus, and removes the one an earlier run left
  (`removed_stale: true` in its summary), so stale text is not federated.
"""

from __future__ import annotations

import shutil
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import archive_layout as layout
from .answering import load_json, write_json
from .archive_feed import ELIGIBLE_KINDS, ask_text
from .archive_ingest import (
    ArchivePaths,
    LegacyArchiveStore,
    load_manifest,
    require_current_store,
)
from .archive_model import Conversation, iter_conversations, message_reference
from .archive_naming import NAMING_VERSION, InvalidName, NameCollision, NameLedger
from .corpus_store import require_git_private
from .corpus_text import (
    PROMPT,
    RESPONSE,
    build_entities,
    build_pairs_from_turns,
    dedupe_preserve,
    extract_actions,
    extract_unresolved,
    normalize_whitespace,
    shorten,
    split_sentences,
    top_keywords,
    vector_terms,
)
from .schema_validation import validate_payload
from .sharded_collection import (
    merge_entity_records,
    merge_ledger_records,
    write_corpus_collection,
)

CONTRACT_NAME = "conversation-corpus-engine.v2"
CONTRACT_VERSION = 2
ADAPTER_TYPE = "archive-normalized"
COLLECTION_SCOPE = "normalized-archive"
ANSWER_STATE = "archive_projected"
ENTITY_TYPE = "concept"
CORPUS_CHANNELS = frozenset({"typed", "reply"})
CORPUS_ROLES = frozenset({"user", "assistant"})
# The five collections federation requires, then the four it reads when they exist.
REQUIRED_COLLECTIONS = (
    "threads-index.json",
    "semantic-v3-index.json",
    "pairs-index.json",
    "doctrine-briefs.json",
    "family-dossiers.json",
)
OPTIONAL_COLLECTIONS = (
    "canonical-families.json",
    "action-ledger.json",
    "unresolved-ledger.json",
    "canonical-entities.json",
)
ALL_COLLECTIONS = (*REQUIRED_COLLECTIONS, *OPTIONAL_COLLECTIONS)
WHY_UNRESOLVED = "The archived conversation still implies an open question or unresolved branch."
EVALUATION_NOTE = "Projected archive corpus has not been manually evaluated."


class CorpusProjectionError(ValueError):
    """The corpus cannot be written faithfully; the previous corpus, if any, is untouched."""


def _turns(conversation: Conversation) -> list[tuple[str, str, str]]:
    """`(kind, text, message reference)` for the messages that are corpus text, in order."""
    provider, uid = conversation.provider, conversation.uid
    turns: list[tuple[str, str, str]] = []
    for message in conversation.messages:
        if message.role not in CORPUS_ROLES or message.channel not in CORPUS_CHANNELS:
            continue
        raw = ask_text(message)[0] if message.role == "user" else message.text
        text = normalize_whitespace(raw)
        if not text:
            continue
        kind = PROMPT if message.role == "user" else RESPONSE
        turns.append((kind, text, message_reference(provider, uid, message.id)))
    return turns


def _title(conversation: Conversation, turns: list[tuple[str, str, str]]) -> str:
    """The display title: the conversation's own, else the start of its first ask. A label only;
    no id is built from it."""
    own = normalize_whitespace(conversation.title)
    if own:
        return own
    first_ask = next((text for kind, text, _ in turns if kind == PROMPT), "")
    return shorten(first_ask, 120) or f"{conversation.provider} conversation"


def _concept_entities(keywords: list[str]) -> list[dict[str, str]]:
    """Concept entities for a conversation's leading keywords, each with its minted id.

    The conversation itself is not an entity (its label would be its title). `build_entities`
    supplies the labels; the id comes from the keyword the label was made from.
    """
    term_for = {}
    for keyword in keywords[:3]:
        term_for.setdefault(
            normalize_whitespace(keyword.replace("-", " ").title()).lower(), keyword
        )
    entities: list[dict[str, str]] = []
    for entity in build_entities("", keywords):
        term = term_for[entity["canonical_label"].lower()]
        entities.append(
            {
                "canonical_entity_id": layout.entity_id(term),
                "canonical_label": entity["canonical_label"],
                "entity_type": ENTITY_TYPE,
            }
        )
    return entities


class _Collections:
    """The rows of one provider's corpus while it is being assembled."""

    def __init__(self) -> None:
        self.threads: list[dict[str, Any]] = []
        self.semantic: list[dict[str, Any]] = []
        self.pairs: list[dict[str, Any]] = []
        self.briefs: list[dict[str, Any]] = []
        self.dossiers: list[dict[str, Any]] = []
        self.families: list[dict[str, Any]] = []
        self.actions: list[dict[str, Any]] = []
        self.questions: list[dict[str, Any]] = []
        self.entities: dict[str, dict[str, Any]] = {}


def _add_conversation(
    rows: _Collections,
    names: NameLedger,
    conversation: Conversation,
    turns: list[tuple[str, str, str]],
) -> None:
    provider, uid = conversation.provider, conversation.uid
    thread_uid = layout.thread_id(provider, uid)
    family_id = layout.family_id(provider, uid)
    names.register(thread_uid, uid)
    names.register(family_id, uid)

    title = _title(conversation, turns)
    combined = "\n".join(text for _, text, _ in turns)
    sentences = split_sentences(combined)
    keywords = top_keywords(combined)
    vectors = vector_terms(combined)
    summary = shorten(" ".join(sentences[:3]) or combined or title, 320)
    key_entities = _concept_entities(keywords)
    entity_labels = [entity["canonical_label"] for entity in key_entities]

    def mint_pair_id(_index: int, message_ref: str) -> str:
        pair = layout.pair_id(provider, uid, message_ref)
        names.register(pair, f"{uid}\0{message_ref}")
        return pair

    pairs = build_pairs_from_turns(
        turns, thread_uid, family_id, label=provider, make_pair_id=mint_pair_id
    )
    for pair in pairs:
        pair["entities"] = entity_labels

    action_items = []
    for action in extract_actions(sentences):
        key = layout.action_id(provider, action)
        names.register(key, action)
        action_items.append(
            {
                "action_key": key,
                "canonical_action": action,
                "status": "open",
                "family_ids": [family_id],
                "thread_uids": [thread_uid],
                "occurrence_count": 1,
            }
        )
    question_items = []
    for question in extract_unresolved(sentences):
        key = layout.question_id(provider, question)
        names.register(key, question)
        question_items.append(
            {
                "question_key": key,
                "canonical_question": question,
                "why_unresolved": WHY_UNRESOLVED,
                "family_ids": [family_id],
                "thread_uids": [thread_uid],
                "occurrence_count": 1,
            }
        )

    rows.threads.append(
        {
            "thread_uid": thread_uid,
            "conversation_id": uid,
            "title_normalized": title,
            "title_raw": title,
            "answer_state": ANSWER_STATE,
            "tags": dedupe_preserve(["archive", f"{provider}-archive"]),
            "semantic_tags": [f"{provider}-thread"],
            "keywords": keywords,
            "semantic_themes": keywords[:6],
            "semantic_v2_themes": keywords[:8],
            "semantic_v3_themes": keywords[:8],
            "semantic_v3_entities": entity_labels,
            "semantic_summary": summary,
            "semantic_v2_summary": summary,
            "semantic_v3_summary": (
                f"{title} archived conversation centered on {', '.join(keywords[:4])}."
            ),
            "action_count": len(action_items),
            "unresolved_question_count": len(question_items),
            "audit_flags": [],
            "family_ids": [family_id],
            "update_time_iso": conversation.last_activity,
        }
    )
    rows.semantic.append(
        {
            "thread_uid": thread_uid,
            "title": title,
            "summary": summary,
            "search_text": f"{title} {combined}",
            "family_ids": [family_id],
            "vector_terms": vectors,
        }
    )
    rows.briefs.append(
        {
            "family_id": family_id,
            "canonical_title": title,
            "canonical_thread_uid": thread_uid,
            "member_count": 1,
            "stable_themes": keywords[:8],
            "brief_text": (
                f"{title} currently centers on {', '.join(keywords[:4])}. "
                f"Projected from the {provider} archive."
            ),
            "search_text": f"{title} {' '.join(keywords[:10])} {combined}",
            "vector_terms": vectors,
        }
    )
    rows.dossiers.append(
        {
            "family_id": family_id,
            "canonical_title": title,
            "canonical_thread_uid": thread_uid,
            "member_count": 1,
            "stable_themes": keywords[:8],
            "doctrine_summary": f"{title} archived conversation summary: {summary}",
            "search_text": f"{title} doctrine {' '.join(keywords[:10])} {combined}",
            "actions": [
                {"action_key": item["action_key"], "canonical_action": item["canonical_action"]}
                for item in action_items
            ],
            "unresolved": [
                {
                    "question_key": item["question_key"],
                    "canonical_question": item["canonical_question"],
                }
                for item in question_items
            ],
            "key_entities": [
                {"canonical_label": e["canonical_label"], "entity_type": e["entity_type"]}
                for e in key_entities
            ],
            "vector_terms": vectors,
        }
    )
    rows.families.append(
        {
            "canonical_family_id": family_id,
            "canonical_title": title,
            "canonical_thread_uid": thread_uid,
            "thread_uids": [thread_uid],
        }
    )
    rows.pairs.extend(pairs)
    rows.actions.extend(action_items)
    rows.questions.extend(question_items)
    for entity in key_entities:
        rows.entities.setdefault(entity["canonical_entity_id"], {**entity, "aliases": []})


def _contract(
    provider: str, manifest: dict[str, Any], generated_at: str, counts: dict[str, int]
) -> dict[str, Any]:
    corpus_id = layout.corpus_dirname(provider)
    return {
        "contract_name": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "adapter_type": ADAPTER_TYPE,
        "corpus_id": corpus_id,
        "name": f"{provider} archive",
        "generated_at": generated_at,
        "required_files": [
            f"corpus/{name.removesuffix('.json')}.collection" for name in REQUIRED_COLLECTIONS
        ],
        "counts": counts,
        "source_input": None,
        "collection_scope": COLLECTION_SCOPE,
        "source_snapshot_path": None,
        "source_signature_fingerprint": None,
        "source_content_fingerprint": manifest["source_fingerprint"],
        "source_file_count": None,
        "source_total_bytes": None,
        "source_latest_mtime_ns": None,
        "naming_version": NAMING_VERSION,
        "archive_schema_version": manifest.get("schema_version"),
        "reader_version": manifest.get("reader_versions", {}).get(provider),
    }


def _write_corpus(
    staging: Path,
    provider: str,
    rows: _Collections,
    manifest: dict[str, Any],
    generated_at: str,
    counts: dict[str, int],
) -> None:
    corpus_dir = staging / "corpus"
    corpus_dir.mkdir(parents=True)
    actions = merge_ledger_records(
        rows.actions, key_field="action_key", text_field="canonical_action"
    )
    questions = merge_ledger_records(
        rows.questions, key_field="question_key", text_field="canonical_question"
    )
    entities = merge_entity_records(rows.entities.values())
    counts.update(actions=len(actions), unresolved=len(questions), entities=len(entities))
    collections: dict[str, list[dict[str, Any]]] = {
        "threads-index.json": rows.threads,
        "semantic-v3-index.json": rows.semantic,
        "pairs-index.json": rows.pairs,
        "doctrine-briefs.json": rows.briefs,
        "family-dossiers.json": rows.dossiers,
        "canonical-families.json": rows.families,
        "action-ledger.json": actions,
        "unresolved-ledger.json": questions,
        "canonical-entities.json": entities,
    }
    for filename in ALL_COLLECTIONS:
        write_corpus_collection(corpus_dir / filename, collections[filename])
    contract = _contract(provider, manifest, generated_at, counts)
    verdict = validate_payload("corpus-contract", contract)
    if not verdict["valid"]:
        raise CorpusProjectionError(
            f"the {provider} contract fails its schema: {verdict['errors']}"
        )
    write_json(corpus_dir / "contract.json", contract)
    write_json(
        corpus_dir / "evaluation-summary.json",
        {
            "generated_at": generated_at,
            "fixture_sources": {"manual": {"source": "unavailable", "count": 0}},
            "regression_gates": {"overall_state": "warn", "source_reliability_state": "warn"},
            "notes": [EVALUATION_NOTE],
        },
    )
    write_json(
        corpus_dir / "regression-gates.json",
        {
            "generated_at": generated_at,
            "overall_state": "warn",
            "source_reliability_state": "warn",
            "source_notes": [EVALUATION_NOTE],
            "gates": [],
        },
    )


def _remove_stale(out: Path, name: str) -> bool:
    """Remove the corpus an earlier run wrote for a provider that now has nothing eligible.

    Left in place it would stay registered and federated with text the archive no longer
    projects. Federation tolerates the missing folder: the registered corpus shows as an invalid
    contract with no content. Only a folder this projector wrote (by its adapter type) is
    removed. Returns whether a corpus was removed.
    """
    for leftover in (out / f".{name}.building", out / f".{name}.previous"):
        shutil.rmtree(leftover, ignore_errors=True)
    final = out / name
    if not final.exists():
        return False
    contract = load_json(final / "corpus" / "contract.json", default={}) or {}
    if contract.get("adapter_type") != ADAPTER_TYPE:
        raise CorpusProjectionError(
            f"{final} is not a corpus this projector wrote, so it is not removed"
        )
    shutil.rmtree(final)
    return True


def _project_provider(
    paths: ArchivePaths, provider: str, manifest: dict[str, Any], out: Path
) -> dict[str, Any]:
    info = manifest["providers"][provider]
    generated_at = info.get("last_activity_max") or ""
    if not generated_at:
        raise CorpusProjectionError(f"the manifest holds no activity stamp for {provider}")
    rows = _Collections()
    names = NameLedger()
    excluded: Counter[str] = Counter()
    seen = messages = 0
    try:
        for conversation in iter_conversations(paths.conversations(provider)):
            seen += 1
            messages += len(conversation.messages)
            if conversation.kind not in ELIGIBLE_KINDS:
                excluded[f"kind-{conversation.kind}"] += 1
                continue
            turns = _turns(conversation)
            if not turns:
                excluded["no-text"] += 1
                continue
            _add_conversation(rows, names, conversation, turns)
    except (InvalidName, NameCollision) as exc:
        raise CorpusProjectionError(f"the {provider} corpus cannot be named: {exc}") from exc
    if seen != info.get("conversations") or messages != info.get("messages"):
        raise CorpusProjectionError(
            f"the {provider} store disagrees with the manifest ({seen} conversations, "
            f"{messages} messages; the manifest says {info.get('conversations')} and "
            f"{info.get('messages')}): run `cce archive ingest`"
        )
    name = layout.corpus_dirname(provider)
    summary: dict[str, Any] = {
        "corpus_id": name,
        "conversations_seen": seen,
        "excluded": dict(sorted(excluded.items())),
    }
    if not rows.threads:
        return {**summary, "status": "empty", "removed_stale": _remove_stale(out, name)}

    counts = {
        "threads": len(rows.threads),
        "families": len(rows.families),
        "pairs": len(rows.pairs),
        "excluded_conversations": sum(excluded.values()),
    }
    final = out / name
    staging = out / f".{name}.building"
    previous = out / f".{name}.previous"
    for leftover in (staging, previous):
        shutil.rmtree(leftover, ignore_errors=True)
    try:
        _write_corpus(staging, provider, rows, manifest, generated_at, counts)
        if final.exists():
            final.rename(previous)
        staging.rename(final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        if previous.exists() and not final.exists():
            previous.rename(final)
        raise
    shutil.rmtree(previous, ignore_errors=True)
    return {**summary, "status": "written", "root": str(final), **counts}


def project_corpus(
    root: Path, out: Path, providers: Iterable[str] | None = None
) -> dict[str, dict[str, Any]]:
    """Write each provider's corpus folder into `out`; return a counts-only summary."""
    paths = ArchivePaths(root)
    try:
        manifest = load_manifest(paths)
    except FileNotFoundError as exc:
        raise CorpusProjectionError(str(exc)) from exc
    try:
        require_current_store(manifest)
    except LegacyArchiveStore as exc:
        raise CorpusProjectionError(str(exc)) from exc
    if not manifest.get("source_fingerprint"):
        raise CorpusProjectionError(
            "the manifest has no source fingerprint: run `cce archive ingest` again"
        )
    ready = [
        name
        for name, info in manifest["providers"].items()
        if info.get("status") == "ok" and paths.conversations(name).is_file()
    ]
    chosen = list(providers) if providers else ready
    unready = [name for name in chosen if name not in ready]
    if unready:
        raise CorpusProjectionError(
            f"no readable normalized store for: {', '.join(sorted(unready))}"
        )
    out = require_git_private(out)
    out.mkdir(parents=True, exist_ok=True)
    return {name: _project_provider(paths, name, manifest, out) for name in chosen}
