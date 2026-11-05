# Attributed knowledge and reviewed semantic Zettels

CCE retains supplied messages and old `human`/`machine` author containers unchanged.
A provider's user role, an authored-file lane, a byline, and the fact that a person
saved some text do not certify original authorship. New projections distinguish
`owner`, `agent`, `third_party`, and `unknown` spans using explicit assertions.
No model or external service is needed to validate or export these projections.

## Digest-bound span assertions

The `cce.span-attribution.v1` annotation belongs to the **entire exact message
text**. `text_digest` is the lowercase hexadecimal SHA-256 of that string's UTF-8
bytes. Do not trim, normalize Unicode, collapse whitespace, or use a digest of
only the quoted span. Positions are half-open Python Unicode codepoint offsets,
not UTF-8 byte positions, UTF-16 positions, or grapheme-cluster positions.
For example, in `😀 café`, `start: 2, end: 6` selects `café`.

```json
{
  "schema": "cce.span-attribution.v1",
  "text_digest": "<sha256 of the entire exact message text>",
  "spans": [
    {
      "start": 0,
      "end": 9,
      "author": "owner",
      "author_id": "rob",
      "evidence": ["Owner reviewed the original writing; receipt reference"],
      "assertion_status": "asserted",
      "profile_eligible": true
    }
  ]
}
```

Spans must be nonempty, in range, and nonoverlapping; input ordering does not
matter. An invalid or stale annotation fails validation. Missing annotations and
uncovered characters become `unknown`, with no owner eligibility. Explicit
spans require an `assertion_status` of `asserted`, `disputed`, or `retracted`.
Evidence is a list of nonempty strings, such as review receipts or source
references; an empty list supplies no evidence. `profile_eligible` defaults to
false. Attribution is a supplied assertion with evidence, **not independent
verification**, regardless of the status or wording of its evidence.

A strict owner profile includes a span only when all of these conditions hold:
its author is `owner`, its exact `author_id` matches the requested persona,
its status is `asserted`, evidence is present, and `profile_eligible` is explicitly
true. Setting that flag on an agent, third party, unknown, disputed, retracted,
or different owner's span does not admit it. Role and channel are irrelevant to
this decision. Owner text surrounding a quote and agent text saved inside a note
can therefore remain distinct without changing their raw source.

## Strict profiles and compatibility

`cce corpus persona-extract --persona rob --source-corpus /private/transcripts
--json` now defaults to strict mode. Place an annotation on each transcript
message's `attribution` field, or on `meta.attribution` for normalized messages.
Existing transcript envelopes, JSONL messages, and provider mapping shapes are
supported. Annotation offsets apply to the extractor's message string: content
parts are joined with one space, as in the existing transcript adapter. Prefer
canonical Library messages when exact provider normalization matters.
Unannotated imported text and old pair summaries cannot enter a strict profile.
Strict profiles never substitute curated assistant-generated yearnings when owner
evidence is absent, with or without source input. With no owner-evidenced spans the
strict profile is empty and says so: `owner_evidence` is `"none"`, `curated_fallback`
is `false`, and a call with no source at all reports the status
`VACUUM (NO OWNER-EVIDENCED SPANS)` with an `empty_reason`. Curated text is reachable
only through the explicit opt-in `--allow-curated` (Python keyword `allow_curated=True`,
strict mode only). Its output is labelled: `curated_fallback: true`, `yearning_method:
"curated"`, assurance `curated_scaffold_not_owner_evidence`, and the Markdown and CLI
text name it. The curated text is assistant-generated, never owner evidence, and
owner-evidenced vocabulary always replaces it (`owner_evidence: "spans"`).

To request historical role heuristics explicitly, add `--mode legacy-role`.
JSON, Markdown, and human-readable CLI output label this mode. Its assurance is
`role_heuristic_not_authorship`; it must not be treated as verified owner writing.
The Python function `extract_persona_lexicon` has the same strict default and
keyword-only `mode` option.

For canonical source revisions, use:

```bash
cce library profile --store-root /private/store --persona rob \
  --annotations /private/message-annotations.json
```

The input is a JSON list, each entry containing `source` and `attribution`.
`source` uses the exact canonical reference described below, without `start` and
`end`; span boundaries belong to its whole-message annotation. The chosen set of
messages is explicit, and duplicate references are rejected to prevent inflated
frequencies. The Python equivalent is
`extract_persona_lexicon(project_root, owner_id, library=library,
annotations=entries)`. All canonical source resolution uses existing
`Library.history` and `Library.record` APIs; there is no second identity database.

## Exact canonical source references

Every Zettel source requires all of the following fields:

```json
{
  "uid": "chatgpt-example",
  "version": "1.0.0",
  "revision_digest": "<digest from the exact accepted history row>",
  "sequence": "main",
  "message": 0,
  "message_id": "message-id-from-that-revision",
  "text_digest": "<sha256 of that entire message's exact text>",
  "start": 2,
  "end": 6
}
```

`revision_digest` identifies the canonical revision object; it is distinct from
the message's `text_digest`. `sequence` must be `main` or `alternate`. The message
index and message ID must agree, and the ID must be unique within that sequence.
Missing versions, stale digests, invalid indices, ambiguous IDs, and out-of-range
or empty spans are rejected. No current-version fallback, fuzzy match, or clipping
is performed. Old accepted revisions remain resolvable after the current record
changes. Retained unaccepted observation snapshots require a future explicit
reference contract and cannot be silently substituted for an accepted revision.

`resolve_source_span(library, reference)` returns the exact source text and copied
reference together with source creation/update timestamps, the message timestamp,
and source role/channel as descriptive metadata. Main and alternate references
remain distinct throughout export. Neither role nor timestamp changes attribution.

## Reviewed semantic Zettels

Structural blocks and repetition candidates remain candidates. CCE does not
infer semantic review or generate idea text. Supply the reviewed idea and a
stable ID, then validate and export it:

```bash
cce library zettels --store-root /private/store --input /private/reviewed-ideas.json
```

The input envelope is `{"schema": "cce.reviewed-zettels.v1", "zettels": [...]}`.
Each Zettel has a nonempty `id`, `title`, and `body`; `review` with `status:
"reviewed"`, a nonempty `reviewer`, and a nonempty list of evidence strings;
`attribution` for the exact new body; a list of typed `relationships` using
`{"type": "supports", "target": "another-stable-id"}`; and at least one source.
A source entry has `source` containing the exact reference above and optionally
`attribution` containing a digest-bound annotation of the **whole source message**.
Missing source attribution remains unknown. `title_attribution` optionally
annotates the title separately; an unannotated title remains unknown.

The export includes `body_attribution` and `title_spans` partitions for the new
text. Each resolved source includes its exact `text`, timestamps, original
annotation, and `attributed_spans` intersected with the requested quotation using
source-message offsets. These are separate assertions: an owner-authored source
quote cannot certify an agent-written synthesis, and an owner-written synthesis
cannot certify its quoted third-party material.

Caller IDs, relationships, source references, and review assertions survive
re-export. Duplicate Zettel IDs are rejected. Relationship types are caller-defined
nonempty strings, and targets may refer to a unit outside the current batch.
Exports can be passed back to `export_zettels` and revalidated against the same
canonical library. Derived fields are recomputed from source evidence each time.
All output is returned as JSON on stdout; use an appropriate private destination
when saving it. The library database, source objects, and binary author containers
are not modified by these read-only projections.

## Persisting reviewed Zettels in a sealed archive

Use the explicit knowledge-atom projection to persist and retrieve the reviewed
unit itself alongside its complete provenance:

```bash
cce library zettels --store-root /private/store --input /private/reviewed-ideas.json \
  --format atoms-jsonl > /private/store/reviewed-ideas.atoms.jsonl
```

This format validates every Zettel and every referenced span before emitting any
line. Each JSONL row uses `schema: "cce-knowledge-atom.v1"` and includes `id`,
`text` (the new body), `authorship` (the new body/title partitions and assertion
assurance), `source` (the unit's reviewed status), `timestamps`, and `derived_from`
(the exact canonical source references). The complete validated reviewed Zettel,
including title, relationships, review evidence, source quotations, source
attribution, sequence distinctions, and source timestamps, is preserved under
`zettel`. `created` and `updated` on the input Zettel supply the new unit's optional
timestamps; absent values remain empty rather than being inferred from sources.

The Python API is `zettel_atoms(library, zettels)`. Select the resulting private
`.atoms.jsonl` file explicitly when publishing a sealed archive. Sealed archive
list/read then exposes the stable Zettel unit rather than only a source-file
container. The importer independently enforces its per-unit capacity and duplicate
ID limits (currently 4 MiB per canonical unit). It preserves the full wrapper and
can restore the original input file bytes. Attribution and review remain supplied
assertions after encryption; encryption does not verify their truth.
