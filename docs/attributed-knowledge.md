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

`--write` saves `persona-extract-<persona>-<timestamp>.json` and `.md` under
`docs/storefront/_generated/` of the project root. That directory is git-ignored in
this repository (`/docs/storefront/_generated/`) because the files can hold private
owner text; another `--project-root` is not covered by this repository's ignore rule,
so keep its generated directory out of version control yourself. The persona id (and
any `timestamp`) must be a safe filename token: path separators, NUL bytes, empty and
dots-only values are rejected with an error and nothing is written; other unusual
characters become `-` with a short digest of the original appended so distinct ids
cannot collide. Without a `timestamp` the stem ends in `latest`, and the existing
`-latest` files are **overwritten** by every write; they are labelled as such. Each
output begins with a marker naming the attribution mode (a `generated_marker` object
first in the JSON, an HTML comment first in the Markdown) including
`curated_fallback` and the assurance label.

For canonical source revisions, use:

```bash
cce library profile --store-root /private/store --persona rob \
  --annotations /private/message-annotations.json
```

The input is a JSON list, each entry containing `source` and `attribution`.
`source` uses the exact canonical reference described below, without `start` and
`end`; span boundaries belong to its whole-message annotation. The chosen set of
messages is explicit, and duplicate references are rejected to prevent inflated
frequencies. A message left unchanged across several accepted revisions of one source
may be annotated in each revision; each distinct span is counted once, keyed by source
`uid`, `sequence`, `message_id`, and the SHA-256 of the span text (plus its occurrence
ordinal among identical spans of that message and its owner eligibility, so a phrase
that really repeats inside one message still counts each time). The Python equivalent is
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

## Reviewed Zettel pages in the library export

The reading export can also render the reviewed Zettels. Supply the same
`cce.reviewed-zettels.v1` envelope that `cce library zettels` accepts:

```bash
cce library export --store-root /private/store --zettels /private/reviewed-ideas.json \
  --out /private/zettel-export
cce library verify-export /private/zettel-export --zettels /private/reviewed-ideas.json --json
```

The Python equivalents are `export_library(library, destination, zettels=envelope)` and
`verify_export(destination, expected_zettels=count)`. The envelope is validated against the
exact canonical library first (the same validation as `export_zettels`); an invalid
envelope writes nothing. A Zettel id longer than 512 characters is refused because the
id is shown on its page. Without the option the export is byte-for-byte unchanged and its
manifest has no `zettels` key.

- **One page per Zettel** at `vault/zettels/<h>.md`, where `<h>` is the first 16 hex digits of
  the SHA-256 of the caller's id (ids are not safe file names; the id stays visible and is
  bound to the file name by `verify-export`). A page never exceeds 16 KiB: every caller
  field (body, title, ids shown in spans, timestamps, roles, channels, reviewer and
  evidence, relationship targets) and every list is clipped, with a visible "truncated"
  statement where content is omitted, and a
  quotation shows at most 400 source characters ("Quotation truncated", with the exact
  offsets). The reviewed envelope remains the complete record.
- **Frontmatter keys** (in this order): `id`, `type: "zettel"`, `title`, `summary`, `created`,
  `updated`, `author`, `authorship`, `status`, `sources`, `links`, `aliases`, `tags`.
  `created` and `updated` come only from the Zettel and stay empty when absent. `author` is
  the single author category of the body partition, `mixed` when it has several, and
  `unknown` when empty. Each `sources` entry is
  `cce:<uid>@<version>#m<message>:<start>-<end> sha256:<digest>`; `<message>` is the
  zero-based index in the canonical reference, `<digest>` is the whole source message's
  `text_digest`, and an alternate-sequence source uses `#a<message>` instead of `#m<message>`.
  Values are inert: brackets, angle brackets and backticks are unicode-escaped so no
  caller text becomes a live link.
- **Body sections** carry the neutralized body, authorship spans, review, typed relationships
  (a link when the target is in the batch, plain text marked "not present in this export"
  otherwise), the sources with each exact quotation as a blockquote, and a "Linked from"
  list built by relationship inversion.
- **Anchors and revisions.** A source on the record's current revision links to
  `../records/<uid>.md#message-N` (1-based) or `#alternate-N`. A source on an older
  accepted revision links to the record without a fragment and is marked historical, since the
  record note shows the current revision.
- **Backlinks.** Each cited record note gets a "Zettels citing this record" section.
- **Hubs.** `vault/hubs/zettels-1.md`, `zettels-2.md`, ... list at most 100 Zettels per page and
  link to each other; Home links to the first. `graph.jsonl` gains `zettel` nodes, an
  `unresolved_zettel` node per relationship target outside the batch, `derived_from` edges to
  the cited record and `relation:<type>` edges.
- **Verification.** `verify_export` counts the Zettel pages on disk against the manifest and,
  with `expected_zettels`, against the envelope; checks the hub pagination and graph counts;
  and reports a Zettel link whose `#message-N` or `#alternate-N` target is absent as
  `unresolved source anchor`. `cce library verify-export --json` reports `exported`,
  `reviewed` and `unresolved_anchors`.

These pages are reading derivatives. They do not create a vault of record, block notes or any
new attribution; the supplied assertions keep the assurance stated in the export.

## Deterministic authorship rules

`cce library attribute` derives span attributions from the exact text of every message of a
published library generation. A provider's `user` role is not proof that the owner wrote a
span: injected system text, command output and tool results sit inside user turns, pasted logs,
quotes, tables, emails and code are not the owner's prose, an assistant's answer can be pasted
back into a turn, and AI-written skill and instruction files are filed under documents. The
engine (`conversation_corpus_engine.attribution_rules`, version `cce.attribution-rules@3`)
therefore labels a span `owner` only when an owner-granting rule claims it and no other rule
does. When a rule is ambiguous the text becomes `unknown` or a non-owner class, never owner.
**No rule is profile-eligible until the precision gate below is met**, and every span the
engine writes has `profile_eligible: false` until then.

### Partition and classes

Each message is partitioned into contiguous, non-overlapping spans that cover every character.
A span carries its half-open codepoint range, an author class of the existing contract, the id of
the rule that decided it, and the SHA-256 of the whole message text, so it can never be applied
to different text. The classes are the contract's: `owner`, `agent` (everything a machine wrote,
injected or printed), `third_party` (quoted, pasted, copied or repeated text) and `unknown`.
Characters no rule claims are `unknown`, rule id `unattributed`, whitespace included. An owner
span never starts or ends with a blank character (whitespace, control characters, zero-width or
other format characters such as U+200B and U+FEFF, and combining or enclosing marks with nothing
to sit on) and is never only blank characters; a combining mark that follows a visible letter
(an accent in decomposed text) is part of it. The engine checks each partition before returning
it, and `cce library attribute --report` checks it again. Text that cannot be encoded as UTF-8
(a lone surrogate) has no digest: `attribute_message` returns one `unknown` span for it, and the
commands count the message as `unreadable_messages`, treat it as unknown and keep it out of the
annotations, the census and the sample; it does not abort the run.

### Rules version

The version string (`cce.attribution-rules@3`) names the rule semantics and every artifact carries
it: span ids (so a span id from another version never matches), the annotation evidence
(`cce.attribution-rules@3:rule:<id>`), the rows of review samples and the rows of the review
ledger. When the rules or the gate change meaning the version is bumped (`@3` replaced the
150-character pasted-back threshold, the row-level gate and the sample format of `@2`). Ledger
rows of any other version, and rows of an older ledger schema (`cce.attribution-review-ledger.v1`
and `.v2`, which lack the sample binding), are **stale**: read, reported as `stale_rows` and never
counted, so reviews must be redone for the new rules. `review-record` refuses a sample drawn under
another version or schema (it has no header, or the wrong one) and asks for a new one.

### Rule precedence

Rules run in this order; the first rule to claim a character keeps it and a later rule only
receives what is still unclaimed. Every rule that can grant `owner` runs after every other rule,
so no higher rule can lose a character to an owner claim.

| # | Rule id | Class | Decides |
|---|---------|-------|---------|
| 1 | `agent_family` | agent | assistant, model, tool and system turns of every provider; user turns on a machine channel; provider metadata naming an agent; a coding CLI's subagent prompts and turns that `archive_provenance.classify_user_turn` calls harness text |
| 2 | `document_origin` | agent | a record whose provenance says an agent produced it (`agent_authored`, `generated_by`, ...), the agent memory and plan families, agent instruction or skill files by file name or folder, and for documents, notes and plans a generated-by footer, co-author line or skill frontmatter anywhere in the text |
| 3 | `inline_injection` | agent | wrapper blocks inside a turn: the archive's injected tags (`system-reminder`, `local-command-*`, ...), `command-name`, `command-message`, `command-args`, tool-result and attachment blocks. Same-name tags nest (a depth counter), quoted attribute values may hold angle brackets, and an unclosed block runs to the end |
| 4 | `typed_remainder` | agent | on a user turn of the `typed` channel, the text after the first `typed_chars` characters |
| 5 | `code` | third_party | fenced code blocks and indented code (two or more indented lines after a blank line) |
| 6 | `quote` | third_party | markdown blockquote lines (and the lazy continuation lines that follow one), passages of 40 or more characters inside matching quotation marks, and pasted-content wrappers |
| 7 | `log_trace` | agent | timestamped log lines, Python tracebacks, stack traces (an exception header with its frame, or two frames, even at two lines) and runs of three or more tool-output shaped lines |
| 8 | `pasted_block` | third_party | a pasted email: a `From:`, `To:`, `Subject:` or `Date:` header line that anything follows, two header lines in a row, an original-message or forward marker, or an on-date-wrote line; it runs to the end of the message because its extent cannot be proven. And a markdown table: two or more consecutive lines that each hold two or more pipes (no separator row needed), two or more consecutive lines that each hold the same number of pipes (rows without outer pipes, such as `Name | Cost`), or lines with pipes that include a separator row. Consequence: owner text written after a pasted email in the same message is third party too; put your own words before the paste or in a separate message |
| 9 | `boilerplate` | third_party | later occurrences of a long text that repeats verbatim after whitespace normalization |
| 10 | `pasted_back` | third_party | text a user turn pastes back from an earlier assistant turn of the same conversation (see below) |
| 11 | `ambiguous` | unknown | a paragraph with an odd number of ASCII double quotes; and a note or document of the owner's own library that states an AI had a hand in it anywhere (see below) |
| 12 | `typed_prefix` | owner | the first `typed_chars` characters of a user turn on the `typed` channel |
| 13 | `note_owner` | owner | the body of a note or document in an authored-library family (Apple Notes, Drive, authored documents) |
| - | `unattributed` | unknown | everything left over |

Rules 4 and 12 apply only when the record states `typed_chars` and not to an authored family,
whose reader sets `typed_chars` to the whole body. Rule 13 does not claim a message whose
metadata marks an agent origin or a record carrying an AI marker. Two consequences differ from the
archive's own author column, which is looser: the slash-command wrapper blocks are machine here
although the person typed the command, and a CLI turn that begins with an injected tag is machine
as a whole.

**AI markers (rule 11).** The whole text of every message of a note or document is searched, with
no window; one marker anywhere makes every message of that record unknown, because section
boundaries cannot be proven. A marker is any of: *generated, written, drafted, composed, edited,
summarised, translated, suggested, answered, ...* followed by *by, using, with, via* or *through*
an assistant; *asked, used, had, let, got, prompted, using, with, via, through, from, by, per* or
*according to* followed by the name of an assistant (ChatGPT, GPT, Claude, Gemini, Copilot, Bard,
Llama, Mistral, Grok, DeepSeek, Perplexity, ...) or by *a/an* LLM, AI, A.I., chatbot, language
model, assistant or AI assistant; *with the help of* an assistant; *as an AI*; *AI-generated,
-assisted, -written, -drafted*; a name followed by *said, says, wrote, suggested, replied,
answered, gave me, told me*; `Prompt:` followed by a code fence, a blockquote or a quotation
mark; and a `Co-Authored-By:` trailer. Ordinary words (*written by my mother*, *generated with
love*, *an assistant manager*, *prompted by the news*) are not markers. The match is deliberately
broad: a person's name that is also an assistant's (*asked Claude*) taints the note, which only
costs recall.

**Pasted-back text (rule 10).** `AssistantEcho` compares each user turn with the assistant turns
before it in the same conversation. Both sides are put in a match form (NFKC, case-folded, list
markers and numbering, emphasis, headings, backticks, punctuation, smart quotes and dashes dropped,
whitespace collapsed), so re-casing, rewrapping, bulleting, renumbering or stripping markup does not
hide a paste. A paragraph of at least 60 match-form characters that holds a stretch of 60 or more
characters shared with an assistant turn is pasted back; so is a run of adjacent paragraphs, each
touched by a match, whose joined text is at least 300 characters (as written or in match form) and
that holds a shared stretch of at least 100. Fenced code blocks between the paragraphs of a run do
not break it, and consecutive user turns with no assistant turn between them are matched as one
run (at most two million characters wait at a time). A paragraph that quotes a shorter phrase (59
shared characters or fewer) is never a candidate, nor is a short paragraph on its own. The
thresholds are parameters of `AssistantEcho`. On 1,000 generated conversations no short owner turn
that shared fewer than 60 characters with the assistant turn was carved; text that does share 60 or
more verbatim characters (a long quoted phrase, a list of identifiers the assistant used, a draft the
assistant quoted back) is carved by design, which costs recall, never precision. **Known limits:**
only the first 400,000 match-form characters of an assistant turn are indexed (a paste of text
beyond that head is not recognised), at most 400,000 characters of assistant text are indexed per
conversation (the oldest turns go first, the newest included in the count), each indexed turn is
an array of 64-bit window hashes, never text, and matching across conversations is not attempted.
The length strata below stay as defence in depth for anything it misses.

Boilerplate is decided by a census over the slice that is scanned. A paragraph, or a whole
multi-paragraph message, of at least 200 characters (`--repeat-min-chars`) that occurs at least
3 times (`--repeat-min-occurrences`) is repeated; its first occurrence in scan order (record uid,
main sequence before alternate, message index) is left to the other rules and every later one is
boilerplate. A different slice, such as one cut by `--limit-records`, can therefore decide a span
differently. The census keeps one digest per distinct long unit, never the text.

### Reports, annotations and bounds

```bash
cce library attribute --report --library /private/store/library-all --owner-id rob [--json] [--ledger L]
cce library attribute apply --library /private/store/library-all --out /private/annotations.json \
  --owner-id rob [--ledger L]
cce library profile --store-root /private/store --persona rob --annotations /private/annotations.json
```

`--library` accepts a `library.sqlite` file, a generation folder or a store folder; without it
`--store-root` or the library store variable is used. Every command opens the generation
read-only (SQLite `mode=ro`) and never writes into it. The only files a command writes are the
ones you name (`--out`, `--ledger`), and each is subject to the output rules below.
`--limit-records N` scans the first N records in uid order and `--deadline-seconds` (default 900,
a finite number above zero) is a wall-clock budget: a deadline during the census, or while `apply`
matches the ledger, stops the command with exit status 3 and writes nothing; one during the scan
marks the result partial and exits 3. The census is a first pass over the slice, so a scan reads
the slice twice, and `apply` with a ledger reads it three times (census, ledger matching,
annotations).

`--owner-id` (and `--reviewer`) must show something: an identity of only spaces, zero-width or
format characters or lone combining marks is refused, and so is the bare name of an AI agent
(`claude`, `chatgpt`, `gpt`, `gemini`, `copilot`, `codex`, `cursor`, `opencode`, `assistant`,
`agent`, `system`, `bot`, in any case), with exit status 2.

`--report` prints, per rule and length stratum, spans, characters, share of all characters,
reviewed spans, precision and eligibility; the partition check (characters covered, overlaps,
gaps); the five largest categories (rules, by characters); the ledger accounting; and the
(rule, stratum) pairs that passed the gate. `--json` prints the same as JSON. `apply` writes a
JSON list of `{source, attribution}` entries in the form `cce library profile --annotations`
already reads: `source` is the exact canonical reference without offsets and `attribution` a
`cce.span-attribution.v1` annotation whose spans are the non-unknown spans (uncovered text is
unknown by the contract). Messages whose id is empty or not unique in their sequence cannot be
referenced exactly and are counted, not annotated. The file holds offsets, ids and evidence
(`cce.attribution-rules@3:rule:<id>`, `stratum:<S|M|L>`, `span:<id>`, and `owner_verdict:<verdict>`
for a span the owner answered), no message text. A span the owner answered also carries
`owner_verdict` as a key of its own. Owner spans carry `--owner-id` as `author_id`; without it no
span can reach a strict profile.

### Output rules

- **Outside the store.** `apply --out`, `review-sample --out` and the ledger given to
  `review-record` must be outside the library generation, its parent folder and the library
  store (found through the builder's `library-all` folder, a store folder given as `--library`,
  `--store-root` or the configured store), the store root included. A bare generation with no
  discoverable store protects its folder and its parent. The layout rule, then: for a library at
  `<dir>/library.sqlite`, nothing may be written under `<dir>` or under `<dir>/..`, so keep the
  library two levels below the folder outputs go to (library in `/private/lib/gen/`, outputs in
  `/private/out/`). A library under the builder's `library-all/generations/<name>` layout
  protects the whole store above `library-all` instead. Paths are compared literally and by file
  identity (device and inode of every existing ancestor, the nearest existing one for a path whose
  tail does not exist yet), so another spelling of the same folder, such as another letter case on
  a case-insensitive volume, is refused as well.
- **Git custody.** Each destination directory must pass `require_git_private`, the check of the
  archive writers, before any work starts; what counts as Git-visible is defined there, not here,
  and a destination that fails it is refused with nothing written.
- **Private files.** Missing directories are created with mode 0700, files exclusively with mode
  0600 and never through a symlink; the ledger is created 0600.
- **Never overwritten.** An existing output is refused before any work starts, and publication is
  a hard link that fails if the name is taken, so a file that appears at the output while the scan
  is running is never replaced: the run exits 2 with `output already exists`, the file is left as
  it is and the temporary file is removed. This needs a filesystem that supports hard links.
- **Partial output.** `apply` and `review-sample` write to `<out>.partial` and publish it only
  when the whole library was scanned. A scan cut short by `--limit-records` or the deadline leaves
  its output at `<out>.partial` (the summary and every sample row say `partial: true`); the
  deadline also exits 3. A leftover `<out>.partial` from an earlier run is refused with a one-line
  message before the census starts.

### The precision gate and owner review

A (rule, length stratum) is profile-eligible only when the owner-review ledger holds at least
100 reviewed spans of that rule and stratum at the current rules version, at least 95 percent of
them confirmed (`100 * confirmed >= 95 * reviewed`, integer arithmetic). The strata are **S**
(up to 240 characters), **M** (241 to 2000) and **L** (more). Short reviews therefore never vouch
for long spans: a long pasted answer typed as one turn stays ineligible until 100 spans of its own
stratum were reviewed.

**Eligibility is decided per span.** A span is `profile_eligible: true` only when it is the
owner's, its own (rule, stratum) passed the gate, `--owner-id` and `--ledger` are given, and the
owner did not answer `no` or `unsure` for this very span. A span the owner confirmed (`yes`) is
eligible only while its stratum is open. A span the ledger names but cannot count cleanly (rows
that name it as different rules or strata, or that misdescribe it, or whose text hash is not its
text) is held back as `unsure`. `apply` reports `spans_withheld_by_verdict`.

**Every drawn row is accounted for.** The gate cannot be passed by leaving the hard rows blank.
`review-record` records every row of the sample: a row without a verdict is recorded as `unsure`,
which counts as reviewed and not confirmed, and there is no `skip`. Each ledger row carries the
digest of the sample it answers and how many rows that sample drew for its rule and stratum, and
precision is over rows drawn: a drawn row the ledger holds no row for also counts as `unsure`
(reported as `missing_rows`). The next `review-sample` leaves out the spans the owner already
reviewed, blank ones included, so a blank row is never drawn again to be skipped again. Recording
a sample again re-answers all of its rows: the last answer wins, so repeat the answers you want to
keep.

Which ledger rows count, in order: a row is **stale** (another rules version or an older ledger
schema) and ignored; a row whose reviewer is not the `--owner-id` identity is a **non-owner row**,
listed by reviewer and never counted (without `--owner-id` nothing counts); one reviewer's rows
that name the same span as different rules or strata are **quarantined**, as are malformed lines,
and never counted; a row whose span id does not exist in the current scan under the same rule and
stratum is an **orphan** (a fabricated id, an edited rule or stratum, or a message whose text
changed, since the id includes the text digest) and is never counted; a row whose span exists but
whose full text does not have the SHA-256 the row answered is a **text mismatch** and is never
counted; of the rest a span counts once, by its last answer, and earlier answers to it are
reported as **superseded**. A scan cut short by `--limit-records` counts only the spans it saw, so
decide eligibility from a full scan. Malformed lines and conflicting rows are reported, not fatal;
if one of them was a `no`, the precision is overstated, so look at `quarantined_rows`.

**What this protects, honestly.** The reviewer and the review time are plain strings the caller
supplies; `--owner-id` is self-reported. A process that can write the ledger and read the library
can append rows with real span ids and the owner's name, and the gate cannot tell them from the
owner's own; the sample digest and the library fingerprint are integrity checks that anyone can
recompute, not authentication. The gate protects against mistakes, omission and fabrication
(made-up ids, a rule or text edited in a row, an edited sample, a reviewer that is not the owner,
blank rows dropped), not against an adversary holding the owner's permissions. Nor can it know a
sample that was drawn and never recorded: a different `--seed` draws a different sample, so
record every sample you draw.

The ledger is append-only JSONL, one object per line with exactly these keys: `schema`
(`cce.attribution-review-ledger.v3`), `rules_version`, `span_id`, `rule`, `stratum` (`S`, `M` or
`L`), `verdict` (`yes`, `no` or `unsure`), `reviewer`, `reviewed_at` (a UTC time such as
`2026-01-02T03:04:05Z`), `sample_digest`, `text_sha256` (the SHA-256 of the full span text the
owner was shown) and `drawn` (rows that sample drew for the rule and stratum). It holds no text.
Appends read, check and write under one exclusive lock, taken without blocking and retried for up
to 30 seconds; after that the append fails with `ledger is locked by another process` (exit 2) and
writes nothing. A row that names a span one reviewer already reviewed as a different rule or
stratum is refused with nothing written (re-answering under the same rule and stratum is
allowed). If the ledger does not end in a newline an earlier write was cut short: the append
writes a newline first, the fragment becomes one malformed line (quarantined, never counted) and
`review-record` reports `repaired_torn_line: true`.

```bash
cce library attribute review-sample --library /private/store/library-all --owner-id rob \
  [--ledger /private/ledger.jsonl] --out /private/sample.jsonl
# fill in "verdict": "yes" | "no" | "unsure" on each row, then
cce library attribute review-record --library /private/store/library-all \
  --ledger /private/ledger.jsonl --sample /private/sample.jsonl --answers /private/sample.jsonl \
  --reviewer rob
```

`review-sample` needs `--owner-id`: a sample is drawn for one owner. It draws, per rule and
stratum, up to `--per-stratum` (alias `--per-rule`, default 100) distinct span texts, chosen by a
seeded hash so the sample is the same for the same library and does not depend on scan order.
Every stratum is sampled. A span of up to 360 characters is shown whole; a longer one as its first
240 characters, `[...N chars omitted...]` and its last 120, with its exact `length`. Each row shows
the span text, the SHA-256 of the full span text, the rule and its description, the claimed author
class and the stratum, nothing about the record. The first line of the file is a **header**:
the row count, a SHA-256 digest over the rows (answers left out), the library fingerprint (a digest
of every record's uid, version and revision digest), the owner the sample was drawn for, the seed,
and the number of rows drawn per `rule:stratum`. Passing `--ledger` leaves out only the spans the
owner (`--owner-id`) already reviewed: a current row of that identity whose rule, stratum and text
hash match the span in this scan. Rows of other reviewers, rows of other versions and rows naming
spans that are not in the scan hide nothing. Without `--out` the sample goes to standard
output. A sample file holds private text: keep it out of version control.

`review-record` requires `--sample` (the file `review-sample` wrote), `--answers` and `--library`
or `--store-root` (so the ledger is kept out of the store). It re-hashes the sample and refuses one
that was edited, cut, hand-written or drawn from another library, or for another owner than
`--reviewer`; it refuses an answer whose span id, rule and stratum are not a row of the sample, a
span answered twice, a verdict other than `yes`, `no` or `unsure`, and a file with JSON nested too
deeply to read (`unreadable`), all before anything is written. It stamps the current UTC time
unless `--reviewed-at` is given. The engine's own functions take the time as a parameter and never
read the clock.
