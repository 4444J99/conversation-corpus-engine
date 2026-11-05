"""Regression contracts: supplied text never automatically becomes owner-authored text."""

import copy
import hashlib
import json

import pytest

from conversation_corpus_engine import library_commands
from conversation_corpus_engine.archive_model import Conversation, Message
from conversation_corpus_engine.attributed_knowledge import (
    collect_owner_segments,
    export_zettels,
    resolve_source_span,
    zettel_atoms,
)
from conversation_corpus_engine.attribution import attributed_spans
from conversation_corpus_engine.cli import build_parser
from conversation_corpus_engine.library_store import Library
from conversation_corpus_engine.persona_extract import (
    extract_persona_lexicon,
    render_persona_extract_markdown,
)


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def span(start, end, author="owner", author_id="rob", **changes):
    return {
        "start": start,
        "end": end,
        "author": author,
        "author_id": author_id,
        "evidence": ["Owner reviewed original writing"],
        "assertion_status": "asserted",
        "profile_eligible": author == "owner",
        **changes,
    }


def annotation(text, spans):
    return {"schema": "cce.span-attribution.v1", "text_digest": digest(text), "spans": spans}


def test_strict_profile_excludes_quote_saved_ai_reply_and_unannotated_import(tmp_path):
    prompt = "cathedral Quote: borrowedword"
    note = "gardening savedmodel"
    turns = [
        {
            "role": "user",
            "text": prompt,
            "attribution": annotation(
                prompt,
                [
                    span(0, 9),
                    span(16, len(prompt), "third_party", "someone"),
                ],
            ),
        },
        {
            "role": "user",
            "text": note,
            "attribution": annotation(
                note,
                [
                    span(0, 9),
                    span(10, len(note), "agent", "model"),
                ],
            ),
        },
        {"role": "assistant", "text": "assistantword"},
        {"role": "user", "text": "importedword"},
    ]
    source = tmp_path / "turns.json"
    source.write_text(json.dumps(turns))
    result = extract_persona_lexicon(tmp_path, "rob", source)
    assert {v["term"] for v in result["vocabulary"]} == {"cathedral", "gardening"}
    assert result["attribution_mode"] == "strict"
    assert result["assurance"] == "attributed_assertions_not_independent_verification"


def test_strict_profile_requires_matching_owner_evidence_status_and_opt_in(tmp_path):
    words = ["eligible", "noevidence", "disputed", "retracted", "optout", "otherowner"]
    turns = []
    changes = [
        {},
        {"evidence": []},
        {"assertion_status": "disputed"},
        {"assertion_status": "retracted"},
        {"profile_eligible": False},
        {"author_id": "sam"},
    ]
    for word, change in zip(words, changes, strict=True):
        turns.append(
            {"text": word, "attribution": annotation(word, [span(0, len(word), **change)])}
        )
    source = tmp_path / "turns.json"
    source.write_text(json.dumps(turns))
    result = extract_persona_lexicon(tmp_path, "rob", source)
    assert {v["term"] for v in result["vocabulary"]} == {"eligible"}


def test_strict_rejects_stale_annotations_and_legacy_is_explicitly_labelled(tmp_path):
    source = tmp_path / "turns.json"
    source.write_text(
        json.dumps(
            [
                {
                    "role": "user",
                    "text": "changedword",
                    "attribution": annotation("originalword", [span(0, 12)]),
                }
            ]
        )
    )
    with pytest.raises(ValueError, match="digest"):
        extract_persona_lexicon(tmp_path, "rob", source)
    legacy = extract_persona_lexicon(tmp_path, "rob", source, mode="legacy-role")
    assert legacy["attribution_mode"] == "legacy-role"
    assert legacy["assurance"] == "role_heuristic_not_authorship"
    assert legacy["vocabulary"][0]["term"] == "changedword"


def test_span_partition_uses_unicode_codepoints_and_unknown_gaps():

    text = "😀 café\n世界"
    parts = attributed_spans(text, annotation(text, [span(2, 6)]))
    assert [(p["start"], p["end"], p["text"], p["author"]) for p in parts] == [
        (0, 2, "😀 ", "unknown"),
        (2, 6, "café", "owner"),
        (6, 9, "\n世界", "unknown"),
    ]
    assert "".join(p["text"] for p in parts) == text
    assert not parts[0]["profile_eligible"]


@pytest.mark.parametrize(
    "spans",
    [
        [span(0, 4), span(3, 5)],
        [span(-1, 3)],
        [span(0, 6)],
        [span(2, 2)],
        [span(True, 3)],
        [span(0, 3, "human")],
        [span(0, 3, assertion_status="verified")],
        [span(0, 3, evidence="trust me")],
        [span(0, 3, profile_eligible="yes")],
    ],
)
def test_invalid_attribution_is_rejected(spans):

    with pytest.raises(ValueError):
        attributed_spans("hello", annotation("hello", spans))


@pytest.fixture
def canonical(tmp_path):
    with Library.create(tmp_path / "library.sqlite") as library:
        item = Conversation(
            provider="chatgpt",
            source_id="one",
            title="Source",
            created="2026-01-01",
            updated="2026-01-02",
            messages=[Message(role="user", text="😀 café source", id="m1", ts="2026-01-01")],
            extra={
                "alternate_messages": [
                    {"id": "a1", "text": "alternate source", "ts": "2026-01-03"},
                ]
            },
        )
        revision = library.put(item)
        ref = {
            "uid": item.uid,
            "version": revision["version"],
            "revision_digest": revision["digest"],
            "sequence": "main",
            "message": 0,
            "message_id": "m1",
            "text_digest": digest("😀 café source"),
            "start": 2,
            "end": 6,
        }
        yield library, item, ref


def test_exact_revision_resolution_preserves_offsets_time_and_sequence(canonical):

    library, item, ref = canonical
    item.messages[0].text = "new text"
    library.put(item)
    resolved = resolve_source_span(library, ref)
    assert resolved["text"] == "café"
    assert resolved["source"]["version"] == "1.0.0"
    assert resolved["source"]["message_id"] == "m1"
    assert resolved["source_created"] == "2026-01-01"
    assert resolved["message_timestamp"] == "2026-01-01"
    alternate = {
        **ref,
        "sequence": "alternate",
        "message_id": "a1",
        "start": 0,
        "end": 9,
        "text_digest": digest("alternate source"),
    }
    assert resolve_source_span(library, alternate)["text"] == "alternate"
    assert resolve_source_span(library, alternate)["message_timestamp"] == "2026-01-03"


@pytest.mark.parametrize(
    "changes",
    [
        {"version": None},
        {"version": "9.0.0"},
        {"revision_digest": "0" * 64},
        {"text_digest": "0" * 64},
        {"uid": "missing"},
        {"message": 99},
        {"message": True},
        {"message_id": "wrong"},
        {"sequence": "guess"},
        {"start": -1},
        {"end": 999},
        {"end": 2},
    ],
)
def test_invalid_canonical_reference_never_clips_or_guesses(canonical, changes):

    library, _, ref = canonical
    with pytest.raises(ValueError):
        resolve_source_span(library, {**ref, **changes})


def test_reviewed_zettel_keeps_identity_relationships_and_separate_authorship(canonical):

    library, _, ref = canonical
    body = "My derived idea"
    zettel = {
        "id": "idea-permanent-17",
        "title": "A synthesis",
        "body": body,
        "review": {"status": "reviewed", "reviewer": "rob", "evidence": ["review receipt"]},
        "relationships": [{"type": "supports", "target": "idea-older-3"}],
        "attribution": annotation(body, [span(0, len(body), "agent", "model")]),
        "sources": [
            {
                "source": ref,
                "attribution": annotation("😀 café source", [span(2, 6, "third_party", "writer")]),
            }
        ],
    }
    before = library.evidence_fingerprint()
    result = export_zettels(library, [zettel])
    again = export_zettels(library, [zettel])
    assert result == again
    exported = result["zettels"][0]
    assert exported["id"] == "idea-permanent-17"
    assert exported["relationships"] == [{"type": "supports", "target": "idea-older-3"}]
    assert exported["body_attribution"][0]["author"] == "agent"
    assert exported["sources"][0]["text"] == "café"
    assert exported["sources"][0]["attributed_spans"][0]["author"] == "third_party"
    assert library.evidence_fingerprint() == before
    assert zettel["sources"][0]["source"] == ref


@pytest.mark.parametrize(
    "change",
    [
        {"id": ""},
        {"review": {"status": "candidate"}},
        {"sources": []},
        {"relationships": [{"target": "missing-type"}]},
        {"attribution": None},
    ],
)
def test_zettel_rejects_unreviewed_missing_or_invalid_evidence(canonical, change):

    library, _, ref = canonical
    zettel = {
        "id": "stable",
        "title": "Idea",
        "body": "Idea",
        "review": {"status": "reviewed", "reviewer": "rob", "evidence": ["approved"]},
        "sources": [{"source": ref}],
        "relationships": [],
        "attribution": annotation("Idea", []),
    }
    with pytest.raises(ValueError):
        export_zettels(library, [{**zettel, **change}])


def test_library_owner_profile_resolves_annotated_exact_revision(canonical, tmp_path):

    library, _, ref = canonical
    entry = {
        "source": {k: v for k, v in ref.items() if k not in {"start", "end"}},
        "attribution": annotation("😀 café source", [span(2, 6)]),
    }
    owner, other = collect_owner_segments(library, [entry], "rob")
    assert owner == ["café"]
    assert "".join(other) == "😀  source"
    duplicate = copy.deepcopy(entry)
    with pytest.raises(ValueError, match="duplicate"):
        collect_owner_segments(library, [entry, duplicate], "rob")


def test_exported_zettels_can_be_reexported_without_losing_source_assertions(canonical):

    library, _, ref = canonical
    zettel = {
        "id": "stable",
        "title": "Idea",
        "body": "Idea",
        "review": {"status": "reviewed", "reviewer": "rob", "evidence": ["approved"]},
        "sources": [{"source": ref, "attribution": annotation("😀 café source", [span(2, 6)])}],
        "relationships": [{"type": "extends", "target": "earlier"}],
        "attribution": annotation("Idea", []),
    }
    first = export_zettels(library, [zettel])
    second = export_zettels(library, first["zettels"])
    assert second == first


def test_duplicate_message_ids_make_source_reference_ambiguous(canonical):

    library, item, ref = canonical
    item.messages.append(Message(role="user", text="other", id="m1"))
    revision = library.put(item)
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_source_span(
            library, {**ref, "version": revision["version"], "revision_digest": revision["digest"]}
        )


def test_library_profile_uses_strict_persona_pipeline(canonical, tmp_path):
    library, _, ref = canonical
    entries = [
        {
            "source": {k: v for k, v in ref.items() if k not in {"start", "end"}},
            "attribution": annotation("😀 café source", [span(7, 13)]),
        }
    ]
    result = extract_persona_lexicon(tmp_path, "rob", library=library, annotations=entries)
    assert {v["term"] for v in result["vocabulary"]} == {"source"}
    assert result["attribution_mode"] == "strict"
    with pytest.raises(ValueError, match="annotations"):
        extract_persona_lexicon(tmp_path, "rob", library=library, annotations={})


def test_strict_and_legacy_cli_modes_are_explicit():

    parser = build_parser()
    default = parser.parse_args(["corpus", "persona-extract", "--persona", "rob"])
    assert default.mode == "strict"
    legacy = parser.parse_args(
        ["corpus", "persona-extract", "--persona", "rob", "--mode", "legacy-role"]
    )
    assert legacy.mode == "legacy-role"


def test_zettel_and_profile_cli_resolve_the_existing_library(
    canonical, tmp_path, monkeypatch, capsys
):

    library, _, ref = canonical
    monkeypatch.setattr(
        library_commands, "open_library", lambda store: (tmp_path, Library(library.path))
    )
    zettel = {
        "id": "stable",
        "title": "Idea",
        "body": "Idea",
        "review": {"status": "reviewed", "reviewer": "rob", "evidence": ["approved"]},
        "sources": [{"source": ref}],
        "relationships": [],
        "attribution": annotation("Idea", []),
    }
    input_path = tmp_path / "ideas.json"
    input_path.write_text(json.dumps({"schema": "cce.reviewed-zettels.v1", "zettels": [zettel]}))
    parser = build_parser()
    args = parser.parse_args(
        ["library", "zettels", "--input", str(input_path), "--store-root", str(tmp_path)]
    )
    assert library_commands.handle_library(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["zettels"][0]["sources"][0]["text"] == "café"
    annotations_path = tmp_path / "annotations.json"
    annotations_path.write_text(
        json.dumps(
            [
                {
                    "source": {k: v for k, v in ref.items() if k not in {"start", "end"}},
                    "attribution": annotation("😀 café source", [span(7, 13)]),
                }
            ]
        )
    )
    args = parser.parse_args(
        [
            "library",
            "profile",
            "--persona",
            "rob",
            "--annotations",
            str(annotations_path),
            "--store-root",
            str(tmp_path),
        ]
    )
    assert library_commands.handle_library(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert {v["term"] for v in result["vocabulary"]} == {"source"}
    zettel["sources"][0]["source"]["end"] = 999
    input_path.write_text(json.dumps({"schema": "cce.reviewed-zettels.v1", "zettels": [zettel]}))
    args = parser.parse_args(
        ["library", "zettels", "--input", str(input_path), "--store-root", str(tmp_path)]
    )
    assert library_commands.handle_library(args) == 2
    assert capsys.readouterr().out == ""


def test_strict_source_does_not_substitute_curated_assistant_yearnings(tmp_path):
    source = tmp_path / "assistant.json"
    source.write_text(json.dumps([{"role": "assistant", "text": "machine prose"}]))
    result = extract_persona_lexicon(tmp_path, "claude", source)
    assert result["vocabulary"] == []
    assert result["ideal_yearning"] is None
    assert result["yearning_method"] is None


@pytest.mark.parametrize("author", ["agent", "third_party", "unknown"])
def test_nonowner_cannot_enter_profile_even_with_owner_id_and_eligibility_flag(tmp_path, author):
    text = "excludedword"
    source = tmp_path / "source.json"
    source.write_text(
        json.dumps(
            [
                {
                    "role": "user",
                    "text": text,
                    "attribution": annotation(
                        text, [span(0, len(text), author=author, profile_eligible=True)]
                    ),
                }
            ]
        )
    )
    result = extract_persona_lexicon(tmp_path, "rob", source)
    assert result["vocabulary"] == []
    assert result["forbidden_terms"] == []


def test_missing_owner_identity_and_missing_opt_in_stay_excluded(tmp_path):
    text = "excludedword"
    spans = [span(0, len(text), author_id=None), span(0, len(text))]
    del spans[1]["profile_eligible"]
    source = tmp_path / "source.json"
    source.write_text(
        json.dumps([{"text": text, "attribution": annotation(text, [item])} for item in spans])
    )
    assert extract_persona_lexicon(tmp_path, "rob", source)["vocabulary"] == []


def test_sealed_atom_wrapper_preserves_reviewed_zettel_and_exact_source_provenance(canonical):
    library, _, ref = canonical
    zettel = {
        "id": "stable-idea",
        "title": "Idea",
        "body": "Idea",
        "created": "2026-02-01",
        "updated": "2026-02-02",
        "review": {"status": "reviewed", "reviewer": "rob", "evidence": ["approved"]},
        "sources": [{"source": ref}],
        "relationships": [{"type": "supports", "target": "earlier"}],
        "attribution": annotation("Idea", [span(0, 4)]),
    }
    atoms = zettel_atoms(library, [zettel])
    assert len(atoms) == 1
    atom = atoms[0]
    assert atom["schema"] == "cce-knowledge-atom.v1"
    assert atom["id"] == "stable-idea" and atom["text"] == "Idea"
    assert atom["derived_from"] == [ref]
    assert atom["authorship"]["body"][0]["author"] == "owner"
    assert atom["timestamps"] == {"created": "2026-02-01", "updated": "2026-02-02"}
    assert atom["source"]["kind"] == "reviewed_zettel"
    assert atom["zettel"]["relationships"] == [{"type": "supports", "target": "earlier"}]
    assert atom["zettel"]["sources"][0]["text"] == "café"
    assert atom["zettel"]["sources"][0]["message_timestamp"] == "2026-01-01"
    assert atom["zettel"]["sources"][0]["source"]["sequence"] == "main"
    with pytest.raises(ValueError):
        zettel_atoms(library, [{**zettel, "sources": [{"source": {**ref, "end": 999}}]}])


def test_sealed_atom_cli_emits_json_lines_after_validation(
    canonical, tmp_path, monkeypatch, capsys
):
    library, _, ref = canonical
    monkeypatch.setattr(
        library_commands, "open_library", lambda store: (tmp_path, Library(library.path))
    )
    base = {
        "id": "idea-1",
        "title": "Idea",
        "body": "Idea",
        "review": {"status": "reviewed", "reviewer": "rob", "evidence": ["approved"]},
        "sources": [{"source": ref}],
        "relationships": [],
        "attribution": annotation("Idea", []),
    }
    input_path = tmp_path / "reviewed.json"
    input_path.write_text(
        json.dumps(
            {"schema": "cce.reviewed-zettels.v1", "zettels": [base, {**base, "id": "idea-2"}]}
        )
    )
    args = build_parser().parse_args(
        [
            "library",
            "zettels",
            "--input",
            str(input_path),
            "--store-root",
            str(tmp_path),
            "--format",
            "atoms-jsonl",
        ]
    )
    assert library_commands.handle_library(args) == 0
    atoms = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [atom["id"] for atom in atoms] == ["idea-1", "idea-2"]
    assert all(atom["schema"] == "cce-knowledge-atom.v1" for atom in atoms)


@pytest.mark.parametrize("location", ["attribution", "meta"])
@pytest.mark.parametrize(
    ("empty_annotation", "error"),
    [
        (annotation("before edit", []), "digest"),
        ({"schema": "invalid", "text_digest": digest(""), "spans": []}, "schema"),
        (annotation("", [span(0, 1)]), "offsets"),
    ],
)
def test_strict_profile_validates_empty_turn_annotation_beside_owner_text(
    tmp_path, location, empty_annotation, error
):
    source = tmp_path / "turns.json"
    empty_turn = {"text": ""}
    if location == "meta":
        empty_turn["meta"] = {"attribution": empty_annotation}
    else:
        empty_turn["attribution"] = empty_annotation
    source.write_text(
        json.dumps(
            [
                {"text": "cathedral", "attribution": annotation("cathedral", [span(0, 9)])},
                empty_turn,
            ]
        )
    )
    with pytest.raises(ValueError, match=error):
        extract_persona_lexicon(tmp_path, "rob", source)


@pytest.mark.parametrize("location", ["attribution", "meta"])
def test_valid_empty_annotation_contributes_no_profile_words(tmp_path, location):
    source = tmp_path / "turns.json"
    empty_turn = {"text": ""}
    if location == "meta":
        empty_turn["meta"] = {"attribution": annotation("", [])}
    else:
        empty_turn["attribution"] = annotation("", [])
    source.write_text(
        json.dumps(
            [
                {"text": "cathedral", "attribution": annotation("cathedral", [span(0, 9)])},
                empty_turn,
                {"text": ""},
            ]
        )
    )
    result = extract_persona_lexicon(tmp_path, "rob", source)
    assert result["status"] == "EXTRACTED"
    assert {entry["term"]: entry["count"] for entry in result["vocabulary"]} == {"cathedral": 1}


def test_strict_without_source_never_substitutes_curated_assistant_text(tmp_path):
    result = extract_persona_lexicon(tmp_path, "claude")
    assert result["attribution_mode"] == "strict"
    assert result["vocabulary"] == []
    assert result["ideal_yearning"] is None
    assert result["archetypal_pattern"] is None
    assert result["yearning_method"] is None
    assert result["curated_fallback"] is False
    assert result["owner_evidence"] == "none"
    assert result["status"] == "VACUUM (NO OWNER-EVIDENCED SPANS)"
    assert "no owner-evidenced spans" in result["empty_reason"]


def test_curated_fallback_requires_explicit_opt_in_and_is_labelled(tmp_path):
    result = extract_persona_lexicon(tmp_path, "claude", allow_curated=True)
    assert result["attribution_mode"] == "strict"
    assert result["curated_fallback"] is True
    assert result["yearning_method"] == "curated"
    assert "real participant" in result["ideal_yearning"]
    assert result["owner_evidence"] == "none"
    assert result["assurance"] == "curated_scaffold_not_owner_evidence"
    assert result["status"] == "VACUUM (SCAFFOLDED)"
    assert result["vocabulary"] == []
    markdown = render_persona_extract_markdown(result)
    assert "Curated fallback:** yes" in markdown


def test_strict_with_source_and_no_owner_spans_stays_empty_unless_opted_in(tmp_path):
    source = tmp_path / "assistant.json"
    source.write_text(json.dumps([{"role": "assistant", "text": "machine prose"}]))
    plain = extract_persona_lexicon(tmp_path, "claude", source)
    assert plain["ideal_yearning"] is None and plain["curated_fallback"] is False
    opted = extract_persona_lexicon(tmp_path, "claude", source, allow_curated=True)
    assert opted["curated_fallback"] is True and opted["yearning_method"] == "curated"
    assert opted["vocabulary"] == []


def test_curated_opt_in_never_overrides_owner_evidenced_profile(tmp_path):
    text = "cathedral foundation"
    source = tmp_path / "owner.json"
    source.write_text(
        json.dumps(
            [
                {
                    "text": text,
                    "attribution": annotation(text, [span(0, len(text), author_id="claude")]),
                }
            ]
        )
    )
    result = extract_persona_lexicon(tmp_path, "claude", source, allow_curated=True)
    assert {v["term"] for v in result["vocabulary"]} == {"cathedral", "foundation"}
    assert result["curated_fallback"] is False
    assert result["yearning_method"] == "derived"
    assert result["owner_evidence"] == "spans"


def test_legacy_role_keeps_curated_output_without_opt_in(tmp_path):
    result = extract_persona_lexicon(tmp_path, "claude", mode="legacy-role")
    assert result["yearning_method"] == "curated"
    assert "curated_fallback" not in result


def test_persona_extract_cli_exposes_explicit_curated_opt_in():
    parser = build_parser()
    base = ["corpus", "persona-extract", "--persona", "claude"]
    assert parser.parse_args(base).allow_curated is False
    assert parser.parse_args([*base, "--allow-curated"]).allow_curated is True


def _revised_entries(library, item, ref, text="😀 café source", owner_spans=None):
    """Annotate the same unchanged message in two accepted revisions of one source."""
    owner_spans = owner_spans or [span(7, 13)]
    first = {k: v for k, v in ref.items() if k not in {"start", "end"}}
    item.messages.append(Message(role="user", text="unrelated addition", id="m2"))
    revision = library.put(item)
    assert revision["version"] != ref["version"]
    second = {**first, "version": revision["version"], "revision_digest": revision["digest"]}
    return [
        {"source": first, "attribution": annotation(text, owner_spans)},
        {"source": second, "attribution": annotation(text, owner_spans)},
    ]


def test_same_owner_span_in_two_revisions_of_one_message_counts_once(canonical, tmp_path):
    library, item, ref = canonical
    entries = _revised_entries(library, item, ref)
    owner, _ = collect_owner_segments(library, entries, "rob")
    assert owner == ["source"]
    result = extract_persona_lexicon(tmp_path, "rob", library=library, annotations=entries)
    assert {v["term"]: v["count"] for v in result["vocabulary"]} == {"source": 1}


def test_changed_message_text_in_a_later_revision_is_a_distinct_span(canonical):
    library, item, ref = canonical
    item.messages[0].text = "😀 café source revised"
    revision = library.put(item)
    first = {k: v for k, v in ref.items() if k not in {"start", "end"}}
    second = {
        **first,
        "version": revision["version"],
        "revision_digest": revision["digest"],
        "text_digest": digest("😀 café source revised"),
    }
    entries = [
        {"source": first, "attribution": annotation("😀 café source", [span(7, 13)])},
        {
            "source": second,
            "attribution": annotation("😀 café source revised", [span(7, 13), span(14, 21)]),
        },
    ]
    owner, _ = collect_owner_segments(library, entries, "rob")
    assert owner == ["source", "revised"]


def test_identical_owner_spans_within_one_message_are_still_counted_separately(canonical):
    library, item, ref = canonical
    text = "echo echo"
    item.messages[0].text = text
    revision = library.put(item)
    source = {
        **{k: v for k, v in ref.items() if k not in {"start", "end"}},
        "version": revision["version"],
        "revision_digest": revision["digest"],
        "text_digest": digest(text),
    }
    entries = [{"source": source, "attribution": annotation(text, [span(0, 4), span(5, 9)])}]
    owner, _ = collect_owner_segments(library, entries, "rob")
    assert owner == ["echo", "echo"]


def test_later_revision_asserting_a_previously_unattributed_span_still_counts_once(canonical):
    library, item, ref = canonical
    entries = _revised_entries(library, item, ref)
    entries[0]["attribution"] = None
    owner, other = collect_owner_segments(library, entries, "rob")
    assert owner == ["source"]
    assert other == ["😀 café source", "😀 café "]
