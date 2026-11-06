# Unified private knowledge library

CCE materializes one versioned library from normalized web archives, native coding-session inventories, and explicitly selected documents. Its two wings are `ai` (conversation and coding lanes, plus support material) and `authored` (research, creative, and writing collections). Collection placement is not a claim of sole human authorship. For explicit digest-bound authorship, strict owner profiles, and reviewed semantic Zettels over exact revision spans, see [attributed knowledge](attributed-knowledge.md).

The existing registered private corpus store owns the data. Source repositories contain code and synthetic tests only. Existing archives, source files, annotations, and earlier generations are never overwritten. A verified candidate becomes current through an atomic pointer with a publication lock and a check that its base generation is still current.

```text
Web archives + native inventory + document manifest
                         |
                         v
          record -> revision -> source snapshot
                         |
             +-----------+-----------+
             |           |           |
         FTS search   block graph   Markdown vault (opt-in:
                                     |   --vault-profile cards|full)
                                Notion ZIP / CSV
```

## Build and retrieve

Register a private store through the existing `cce corpus-store register` interface before building. Use the same project registry and the real store path, not a compatibility symlink. See `cce corpus-store --help` for the installed interface.

```sh
cce library build --manifest /private/library-sources.json --project-root /private/project --store-root /private/store --publish
cce library build --manifest /private/library-sources.json --project-root /private/project --store-root /private/store --vault-profile cards
cce library export-cards --store-root /private/store --out /private/reading/cards --limit-notes 10000
cce library status --store-root /private/store
cce library search lattice --store-root /private/store --limit 20 --offset 0
cce library search "ledger entries" --store-root /private/store --plan auto --max-seconds 15
cce library read RECORD_UID --store-root /private/store --message 0 --offset 0 --limit 12000
cce library history RECORD_UID --store-root /private/store
cce library read RECORD_UID --store-root /private/store --version 1.1.0
cce library read RECORD_UID --store-root /private/store --digest SNAPSHOT_SHA256
cce library read RECORD_UID --store-root /private/store --sequence alternate --message 0 --limit 12000
cce library read RECORD_UID --store-root /private/store --toc
cce library read RECORD_UID --store-root /private/store --around-query "ledger entries"
cce library read RECORD_UID --store-root /private/store --message 0 --message-end 20 --limit 12000
cce library analyze lattice --output /private/store/derived/analysis/lattice --project-root /private/project --store-root /private/store --limit 5
cce library verify --store-root /private/store
```

`CCE_LIBRARY_STORE_ROOT` supplies the store when a command omits it; the existing `CCE_CORPUS_STORE_ROOT` is a fallback. Search returns a total (exact up to 1000 matches; see [search planning](#search-planning-time-budget-and-capped-totals)), bounded excerpts, stable record identities, a `path` hint and the next offset, and it plans and time-bounds the query. The `path` is the note path of the legacy export layout (`vault/records/UID.md`); under the `none`, `cards` and `full` profiles the note for a record is named by the `path` of its `library-index.jsonl` row (null when it has none), and `cce library read UID` is the authoritative way to open a record under every layout. Read retrieves one message span and reports main/alternate sequence counts; use `--sequence alternate` (MCP `sequence: "alternate"`) to page retained conversation branches. `--around-query`, `--message-end` and `--toc` (with `--toc-mode`, `--toc-offset` and `--toc-limit`) are the command-line forms of the MCP arguments `around_query`, `message_end`, `toc`, `toc_mode`, `toc_offset` and `toc_limit` (see below): both surfaces run the same code, so they accept the same values, refuse the same combinations (`toc_*` without `toc`, `toc` with `around_query` or `message_end`, `around_query` with `offset` or `message_end`) and say so in the same words. History and digest selection also expose retained snapshots that were not selected as the current body. `cce-mcp --library-store-root /private/store` adds three read-only tools: `cce_library_search`, `cce_library_read`, and `cce_library_history`. Clients cannot choose arbitrary filesystem paths through these tools.

### Attachment context and typed-text boundaries

CLI and MCP passage reads report `attachment_count_observed` for the selected
main or alternate message sequence. Each returned message has
`attachment_count_in_message`, bounded `attachments`, `attachments_truncated`,
and `attachment_refs_redacted`. The count describes normalized observations;
partial or metadata-only sources can omit attachments, and metadata never proves
that the original attachment bytes were retained. A read returns at most 20
attachment entries in total, including a multi-message range. Names are limited
to 512 characters, kinds to 128, and refs to 512. Sizes must be nonnegative
integers; unknown or malformed sizes are null.

Attachment refs retain simple identifiers containing letters, digits, dots,
underscores and hyphens. URLs, paths, query strings, fragments and encoded URL
strings are omitted and the redaction flag is set. The archived source remains
unchanged; retrieve original evidence through the private archive when needed.

`typed_chars` is a character offset in the entire normalized message, independent
of the requested read offset and limit. It is returned only for a non-boolean
integer within `0..len(text)`. `typed_chars_status: normalized` means the source
record has that valid prefix boundary; it is not proof of who authored the text.
Missing boundaries are null with status `unknown`, malformed boundaries have
status `invalid`, and a boundary including an inline `[image]` marker alongside
image metadata is null with status `synthetic-marker`. Older ChatGPT readers
inserted those markers and defaulted to the whole message length. Read does not
reorder the original text or infer a replacement boundary for those records.

Compact MCP reads retain the same context under `tyc` (boundary), `tys` (status),
`aco` (sequence count), `acm` (message count), `at` (metadata), `att` (truncated)
and `arr` (refs redacted). Table-of-contents reads remain navigation responses.

Unavailable stores, unreadable pointers and mounts that disappear during pointer
lookup report an unavailable-store error. An accessible store with no published
pointer still reports that no generation has been published. Read paths never
create a missing store.

## Source manifest

```json
{
  "schema": "cce.library-sources.v1",
  "archives": ["/private/store/archive-all"],
  "snapshot_archives": [],
  "estate_databases": ["/private/inventory/session-corpus.db"],
  "documents": [{
    "id": "research",
    "root": "/private/research",
    "include": ["**/*.md", "**/*.pdf", "**/*.docx"],
    "exclude": ["**/.git/**", "**/node_modules/**"],
    "wing": "authored",
    "lane": "research",
    "kind": "document",
    "max_bytes": 20971520
  }],
  "native_max_source_bytes": 67108864,
  "limits": {"max_records": 100000, "max_seconds": 1800, "max_output_bytes": 8589934592}
}
```

The manifest is private: it contains local source locations. Native readers retain every inventory identity, with `full`, `partial`, or `metadata_only` body coverage. They handle Codex, Claude Code, Gemini CLI, OpenCode, Copilot CLI and Antigravity formats. Composite Claude agent identities and explicitly declared Codex ancestor headers retain their source evidence; unexplained mismatches stay unavailable. Antigravity protobuf text recovery has unknown semantic roles and remains partial. Missing or unsupported bodies remain visible. Documents support UTF-8 text, Markdown, JSON, YAML, CSV, Fountain, TeX, Org, static HTML, FDX XML, DOCX XML and embedded PDF text through `pdftotext`; PDF images are not OCR. Legacy `.doc` and `.rtf` are converted to text by macOS `textutil` when present, behind an injectable converter; without it they stay metadata records with reason `legacy_converter_unavailable`. `.epub` text is read in spine order from the zipped XHTML (static text only), and a `.textbundle` (folder, zipped bundle, or `.textpack`) is one record read from its `text.md`, `text.markdown` or `text.txt`; bundle members and assets are never separate records. Apple Pages previews or optional `pages2text` conversion remain partial. Native conversion has a 30-second deadline, CPU and output bounds, and preserves source bytes. `.numbers` and `.key` (file or package) have no text reader: each is a metadata record with reason `unsupported_format` and a `known_unsupported_format` receipt. Every other unsupported file remains a metadata record. `cce library status` reports `body_reasons` (counts of each document `body_reason` for each non-full body status), `unexplained_non_full`, the number of non-full records with no recorded reason across the whole library, and `non_full_by_provider`, the same count split per provider as `{provider: {non_full, explained, unexplained}}` with `non_full` equal to `explained` plus `unexplained` and one entry for every provider in the library (all zero when its records are full). The reason is read from the stored revision and a non-full record is explained by either of two records: a non-empty `body_reason`, which is how documents say why a body is not full, or at least one non-empty string in `findings`, which is how native estate records keep theirs (every non-full native record carries one). `findings` never explains a document, so a document needs a `body_reason`. A non-full record with neither, such as a normalized archive row whose reader recorded none, is counted as unexplained rather than given an invented reason. `body_reasons` is unchanged: it counts `body_reason` values only, so native explanations appear in `non_full_by_provider` and in the build report's findings, not in that histogram. `empty_body` is the reason code for an empty document. `enumerate_documents` in `library_documents.py` counts document-type files under a root by extension and `local` or `dataless` state using `lstat` only, and `scripts/icloud_hydrate.py --plan` lists cloud-only files in batches of at most 5 GiB, each needing twice its size in free space; `--execute` downloads and is refused without `--confirm-hydrate`. Cloud placeholders are recorded without hydration. Symlinks are not followed. A source manifest defines measured scope; it does not prove exhaustive discovery of cloud or historical holdings.

`snapshot_archives` use the same normalized directory layout as `archives`, but add observations without replacing any existing current identity. They recover older or alternate source exports safely; a previously unknown identity is still admitted. Incremental generations inherit the full record ledger and link to the prior coverage report. A remaining inherited coverage gap keeps `coverage_complete` false.

To reuse a failed generation whose source ingestion finished, set `resume_generation` to its absolute generation directory and list only supplemental sources in the new manifest. The builder verifies the failed state, source counts, manifest, publication base, database integrity and every normalized object digest. It hash-binds and compacts a new copy, leaving the seed untouched; baseline observations count against the new record budget. The report retains the seed receipt and all coverage findings. Older candidates without a phase marker are explicitly labeled as weaker legacy ingestion evidence. Set measured `limits` in the manifest for the next invocation; a resume never changes global limits or hides the failed attempt.

## Preservation and condensation

The SQLite store retains compressed, hashed normalized source objects, provenance observations, and source snapshots. Original file hashes are retained where the adapter reads the original bytes. Semantic versions start at `1.0.0`: classification changes advance major, body changes minor, and metadata changes patch. A lower-coverage observation never replaces a fuller current body. Replaying a known source snapshot does not revert the current revision; all observations remain retrievable.

Condensation replaces repeated eligible prose blocks with a shared passage and backlinks to every occurrence. Matching conservatively normalizes line endings, common Markdown quote depth and terminal newlines. Code remains distinct. Exact raw spans and normalization records are retained. The derived block index stores compressed eligible prose; code and short text remain in original revisions and full-text search. An interrupted rebuild restores both occurrence rows and their lookup index. This is deterministic reference extraction, not an inference that two similarly worded claims have the same meaning. Explicit source Markdown links/images and raw HTML are neutralized in reading views; consumer autolinking may still make bare URLs clickable. Historical instructions remain evidence and confer no authority.

## Reading surfaces and consumers

Each generation contains `graph.jsonl`, `library-index.jsonl`, `records.csv` and a small `agent-entry.md`. Whether it also contains a Markdown reading vault (`vault/Home.md`, notes and hubs) and `notion-import.zip` depends on the vault profile below. Keep personal annotations in a separate reading folder rather than the immutable generation. Large transcript notes and heavily populated folders can prevent a native Markdown indexer from opening a vault even when the library passes its data-integrity checks. Bounded notes and folders address those reading constraints; each generated vault still needs its own native acceptance check. Private reproduction measurements and receipts are retained separately from this public guide.

### Vault profiles

`cce library build --vault-profile {none,cards,full}` chooses the reading vault written into the generation. The default, `none`, writes no vault and no Notion ZIP. The database, the receipts and publication are unchanged, but the artifacts that point at notes change with the vault, deliberately: `export-manifest.json` is `cce.library-export.v2` with a `profile` field; `library-index.jsonl` is `cce.library-index.v2` and its `path` is null, so the `path` column of `records.csv` is empty; the record nodes of `graph.jsonl` lose their `path` (and `block:` nodes have none); `agent-entry.md` has profile-specific text and no longer links `vault/Home.md`; and there is no `notion-import.zip`. The profile is recorded in the build report (`vault_profile`) and in the manifest (`profile`).

| Profile | What it writes | Use it for |
|---|---|---|
| `none` (default) | No `vault/` and no `notion-import.zip`. | Every build. Search, read and history work on the database. |
| `cards` | `vault/Home.md`, one card per record in `vault/cards/`, paginated hubs in `vault/hubs/`, a Notion ZIP of that tree. A card holds the title, facets, metadata, a bounded excerpt of the first message (400 characters) and the exact `cce library read` command; it never copies the body. Cards are chosen most recently updated first until the note budget (10,000 notes, counting cards, hub pages and Home) is full; `Home.md` states how many records have no card and how to reach them. | A small reading vault of a large library. |
| `full` | Every record whole, in `vault/records/` (first part) and `vault/parts/` (continuations of about 12 KiB, cut at message boundaries and, inside a long message, at line boundaries; a code fence is closed at the end of a piece and reopened at the start of the next, at the indentation it was opened with when that is one to three spaces (see [fences cut by a part boundary](#fences-cut-by-a-part-boundary)), and a single line longer than a part is cut mid-line on a character boundary), paginated hubs, a Notion ZIP. No per-block notes. | A small library, or a selection, that must be readable in full. A record's header lists its first 20 parents and counts the rest. It renders every note in memory, measures them against every hard cap and refuses before writing anything (see below). |

`cce library export-cards --out DIR [--limit-notes N]` writes the `cards` profile on demand for the current generation. It reads the library and never writes inside the library store. DIR may be empty or an earlier card vault (it carries the `.cce-cards-vault` marker); any other non-empty DIR is refused. It records exactly what it wrote in `.cce-cards-manifest.json` (path, size, SHA-256) and a later run replaces and deletes only files that manifest lists. It refuses, before changing anything, when a listed file was edited or when a file it did not write already sits where a card would go, so annotations are never overwritten; an identical file is adopted, which makes an interrupted run safe to repeat. `--limit-notes` is at most 20,000 (the hard cap). The cards, hubs and Home it would write are measured against every hard cap before anything is written, deleted or listed; a breach refuses with nothing changed. The manifest is rewritten last, only after every write succeeded. If the files you keep in DIR crowd a folder past a cap, the run still completes (the vault it generated fits) and warns on stderr and in the result (`warnings`; `generated_caps` is the verdict on the generated set, `vault_caps` on the whole folder), because those files are yours to move.

`cce library verify-export DIR` recognises a card folder (no `export-manifest.json`, but the `.cce-cards-vault` marker or `.cce-cards-manifest.json`) and checks it instead of a library export. It reports `kind: "cards"` and fails when the marker or the manifest is missing or invalid, when a file the manifest lists is missing, is not a regular file, or differs in size or SHA-256 from what was written ("edited since export", the same condition under which the next `export-cards` refuses to replace it), and when the folder as a whole breaks a hard cap of the vault standard (the same `measure_vault` check an export runs after writing, so files of your own count: `valid` can turn false because of them). Files the manifest does not list are yours and are only counted (`unlisted`); the manifest need not list every file on disk, which is the difference from a library export.

Publication compares source evidence, not reading derivatives: rebuilding unchanged sources under another profile makes `publish` report `changed: false` and leave the current generation in place, so use `export-cards` for a vault on demand. A `full` build that would pass a hard cap fails at its export stage, after ingestion; repeat it with `"resume_mode": "export"` (see the output budget notes) and another profile.

The caps are the Wiki Standard v1 policy defaults, enforced by `library_vault_gate` (note count, total bytes, largest note, widest folder, hub entries; one `warn` or `fail` finding per breach, the warn finding reporting the true largest value of its own tier). The profiles stay inside them by construction. Before any directory exists, planning renders every note of a `cards` or `full` vault in memory and `measure_planned` measures that set against all of the hard caps (note size, folder entries, hub page entries, note count, total bytes); a breach refuses with nothing written. `measure_vault` checks the written tree again after writing, and `verify-export` checks it later:

| Measure | Aim | Warn above | Fail above |
|---|---|---|---|
| Note | 4 KiB | 16 KiB | 64 KiB |
| Folder entries | | 200 | 1,000 |
| Vault notes | | 10,000 | 20,000 |
| Vault bytes | | 50 MiB | 100 MiB |
| Hub page entries | | | 100 |

Folders are planned with the archive's deterministic hash-bucket plan (`bucket-all-bNNN` folders appear once a folder would pass 200 entries) and hubs are paginated at 100 entries and the page byte budget. Nothing is truncated silently: a vault that would pass a hard cap is refused with a message that names `--vault-profile cards`, and a card vault that cannot hold every record says so on its Home note.

**Breaking layout change (`full`).** The `full` profile replaces the legacy bulk layout and differs from it: there is no `vault/blocks/` (a repeated passage is shown inline in each record and stays in the database; `graph.jsonl` still has its `block:` nodes and `reference` edges, without a `path`); records live in `vault/records/` (flat up to 200 records, otherwise in hash-bucket folders; the first part only) with continuations in `vault/parts/`; each hub is `vault/hubs/FACET-SLUG-HASH.md` with `-pNNN` continuation pages and a `FACET-index` hub per open facet; reviewed Zettel pages are not rendered. The export manifest is `cce.library-export.v2` and `library-index.jsonl` rows are `cce.library-index.v2`, where `path` is the record's note (the first part) or null. A v1 reader refuses these files instead of misreading them.

`cce library export` and `verify-export` are unchanged for the legacy layout: the default `--vault-profile legacy` produces the same bytes as before (a test pins them) and verification of existing exports is unchanged. The legacy layout is the one that can exceed the caps, so `export` prints a warning on stderr when the vault it just wrote does; pass `--vault-profile cards|full|none` for the capped layouts. Reviewed Zettel pages (`--zettels`) exist only in the legacy layout and are refused with any other profile.

Consumers of the old layout, and what happens to each:

| Consumer | Under the profiles |
|---|---|
| `library_export.export_library` / `verify_export` | `export_library` takes `vault_profile`; the default (`legacy`) is byte-identical to the layout before the profiles (a test pins it), except that a title or metadata value with a Markdown special is escaped once (`metadata&#95;only`), not twice as before. Escaping is idempotent because an entity already present passes through unchanged, so a source title that literally contains one (typed `Q&amp;A` or `see &#91;1&#93;`) is not told apart from an escaped character: the reader sees the character the entity names (`Q&A`, `see [1]`), not the entity text the title spelled out. `verify_export` verifies existing v1 exports unchanged. It also understands the v2 profiles: `none` (no vault is valid), `cards` and `full` (the caps are checked). |
| `library_build.build_library` | Takes `vault_profile` (default `none`) and always writes through `export_library`; the profile is recorded in the report. |
| `library_commands` (`build`, `export`, `export-cards`, `verify-export`) | `build --vault-profile`, `export --vault-profile` (default `legacy`, warning on stderr when the legacy vault breaks the caps), the new `export-cards`; `verify-export` unchanged for v1, and it also verifies an `export-cards` folder (see above). |
| `scripts/package-obsidian-vault.py` | Works on the `vault/` of `cards` and `full` (tested), given `--source-generation` (required: the manifest names the generation the vault came from) and, optionally, `--agent-entry`; the entry's `vault/Home.md` link becomes the package's own `Home.md`. A fenced block cut by a `full` part boundary packages when its opener is indented one to three spaces, including inside a list item. Other openers keep the splitter's older column-0 lines and can leave a part inside an open fence; the packager then fails closed, naming the note (`verify_export` has no fence check; only the packager's tracker is aware of list containers). The figures are under [fences cut by a part boundary](#fences-cut-by-a-part-boundary). Without a vault it exits with code 2 and a one-line message naming `--vault-profile`, `cce library export --vault-profile cards` and `export-cards` (the routes that work for an already-published generation). It is still the tool for a legacy bulk vault. |
| `scripts/repair-obsidian-inline.py` | Unaffected: it repairs the output of the Obsidian packager through that packager's own ledger and never reads the library layout. |
| `scripts/package-notion-batches.py` | Works on the ZIP of `cards` and `full` (tested). With no ZIP (`none`) it exits with code 2 and a one-line message naming `--vault-profile` and `cce library export --vault-profile cards`. |
| `library-index.jsonl` and `records.csv` (LingFrame and other index readers) | Same fields; schema `cce.library-index.v2` for the profiles, `path` is the note or null (empty in the CSV). |
| `graph.jsonl` | Record nodes carry `path` only when the record has a note; `block:` nodes never do under a profile. |
| `agent-entry.md` | Rewritten per profile (it no longer links `vault/Home.md` when there is no vault). Legacy text unchanged. |
| Small wiki notes, one per reviewed idea or explicitly attributed agent draft (`zettels --format obsidian-md --out DIR`) | Works from the library alone, in any layout; carries exact source tuples and an ownership manifest, and never promotes typed owner claims to human authorship. See [attributed knowledge](attributed-knowledge.md#one-small-note-per-idea-for-a-wiki---format-obsidian-md). |
| Reviewed Zettel export (`export --zettels`) | Legacy layout only; refused with any profile, because the pages cite `vault/records/UID.md#message-N`. |
| My Knowledge Base federation provider (`legacy/my-knowledge-base/src/federation/cce-library-provider.ts`) | It indexes the legacy `vault/records/UID.md` and `vault/blocks/DIGEST.md` notes as documents (shared passages are documents of their own), a real dependency on the layout the profiles drop, so it accepts only a v1 manifest and index. A generation built with any profile, the default included, is refused with a message naming `--vault-profile`; earlier generations still scan. |
| `cce library search` hit `path`, MCP field `pa` | Unchanged hint (see above); use `read` by UID or the index. |
| `archive_read_library`, `archive_commands.library_source_folder`, `library_resume` | Unaffected: they read only `current.json`, `library.sqlite` and the build report, never the vault. |

For large Obsidian libraries built with the legacy layout, use `scripts/package-obsidian-vault.py` to create a fresh reading folder with bounded pages, repaired navigation and a reversible byte ledger. Supply the generation's `vault/` as its source, its `agent-entry.md` through `--agent-entry` and the generation's identity through `--source-generation`; open the resulting folder as a vault, then open `Home.md`. Every original file can be reconstructed and hash-checked, while its original note path remains available as a page index. See [bounded Obsidian packaging](obsidian-vault.md) for the command and verification contract. Native application acceptance remains a separate observation from packaging.

The full ZIP is a lossless import source. For large libraries, use smaller Notion batches: the official [import guidance](https://www.notion.com/help/import-data-into-notion) warns that ZIPs with 10,000 or more files can fail or partially import, even below the 5 GB ZIP limit. It also documents per-file limits of 5 MB on Free and 50 MB on paid plans. Local ZIP parity does not establish successful Notion import, cross-batch links or heading-anchor behavior; verify those in the account. Neither application becomes a new identity authority.

Other knowledge-universe components consume `library-index.jsonl` plus its referenced Markdown files. Each index row includes CCE UID, version, object digest, content digest, source reference, body coverage, and collection taxonomy. Preserve those fields in any derived atom, vector or wiki index; do not replace CCE identity with filenames or drop duplicate provenance. My Knowledge Base consumes the verified reading index; native text-analysis inputs use the bounded revision bridge below.

For native structural analysis, `library analyze` writes bounded original-revision inputs accepted by LingFrame and atomic-substrata, including main and alternate messages. It preserves exact block spans and source identities instead of analyzing display links. See [the native analysis bridge](knowledge-analysis-bridge.md) for model mappings, limits and the observed consumer serialization boundary.

Indented and quoted code receives explicit code fences in reading notes. Ambiguous escaped or multiple-backtick prose examples also use literal wrappers so a legacy inline matcher cannot activate source examples. The wrapper is longer than any embedded backtick run and preserves the literal source text. Source revisions remain unchanged; export validation still rejects active unsafe links. A renderer fix applies to later exports; existing immutable generations retain their producer version and separate acceptance evidence.

Build receipts report source findings and `coverage_complete` separately from successful verification/publication. `coverage_complete=false` can be a valid, useful library with visibly incomplete source bodies. Builds have a wall deadline, record and output budgets, private permissions, and retained failure receipts. A successful code test or ZIP export does not establish exhaustive content recovery or a completed application import.

#### Fences cut by a part boundary

A `full` note longer than a part is cut at line boundaries. Inside a fenced block the part closes the fence and the next part reopens it, and what the splitter writes there depends on how the fence was opened, because the tracker that finds fences is flat: it records the marker, not the container.

- **One to three spaces of indentation**, at top level or inside a list item (`- item` with a fence indented two spaces, `1. item` with three): the part closes and the next part reopens the fence at that indentation, CommonMark's own limit for a fence relative to its container. A column-0 closer would leave the list item, which ends its fence, and open a new top-level fence that swallows the rest of the part and its "next part" link. Over seeded synthetic documents through the real rendering pipeline, every bullet-item document that split (27 of 27) and every ordered-item one (29 of 29) rendered every link; the column-0 lines failed 27 of 27 and 29 of 29.
- **Every other opener keeps the column-0 lines**, as before: no indentation (correct at top level), four columns or more, a tab or other whitespace, a fence inside a blockquote, and a fence opened on a list marker's own line. The flat tracker reads some of these as openers where CommonMark does not (a no-break space is whitespace to it, not indentation), and an indented closer written for them can leave a real fence open that the column-0 closer closes, so they were deliberately not changed. Against the column-0 lines the container-aware splitter broke no document in three seeded runs of 2,000 random documents each, and repaired one or two per run.

What that leaves broken is measured, not argued. In the isolated splitter, with 300 synthetic documents per shape, a cut fence left a part inside an open fence or lost its link in every split document of these shapes except one: lists nested to four columns or more (294 of 294, 297 of 297 and 299 of 299 for three shapes), a tab in a list item (299 of 299), a blockquote (299 of 299), a fence opened on a list marker's line (298 of 299). Through the real pipeline the block renderer re-fences many of these as literal code first, and the rates fall: a blockquote fence failed in 0 of 29 split documents and a marker-line fence in 13 of 28 (20 of 28 with the column-0 lines). Fences opened at four columns or more still reach the splitter: in a seeded run of 3,000 random documents, 878 cuts fell inside a fence opened at four columns or more, in 220 documents. A continuation of an untracked fence renders as prose, not code, and the source's own closing line can open a fence that swallows the rest of its part. The packager fails closed on any note that ends inside an open fence and names it; `verify_export` has no fence check. The figures come from invented documents and describe the splitter, not any library; `tests/test_archive_split_container_fences.py` and `tests/test_library_vault_fence_pipeline.py` pin the behavior.

### Progressive disclosure through the MCP tools

The two MCP tools support a locate-then-read flow that stays inside a token budget.
Search defaults to automatic unit selection; `unit: "conversation"` keeps the previous
retrieval unit. The existing response fields remain available, with additive unit metadata.

- `cce_library_search`: `depth: "L0"` returns only `uid`, a one-line `title` (80 characters at most), `score` (bm25, higher is better) and `read`, within `budget_tokens` (default 900, at most 1200 at L0; at most 20000 at L1). Tokens are estimated as UTF-8 bytes / 4 of the compact JSON text, and the response states the estimate (`token_estimate`, or `tk` in compact mode). Hits that do not fit are left for `next_offset` and `truncated` is true; a budget too small for a single hit is an error. The budget holds for a response with no hit as well: a budgeted L1 response echoes at most 120 characters of the query (a longer one ends with a `...[query truncated, N characters]` marker), and a budget too small for even the empty envelope is an error. `compact: true` shortens keys (the tool schema lists them). Every response states `total` with `total_exact` and `total_label` (the number itself while the count is exact, and `1000+` once more than 1000 records match), and, additively, the `plan` that answered, `partial`, `ranked` and `elapsed_s` (see [search planning](#search-planning-time-budget-and-capped-totals)). `wing` must be `ai` or `authored`, as the tool schema lists it; any other value is a tool error (`isError`), not an empty page.
- Conversation handoff: each conversation hit carries `read`, the exact `cce_library_read` arguments (`uid`, `message`, `offset`, `limit`, plus `sequence` when the best match is in the alternate branch) for the first message that holds every query term, falling back to the first message holding any. `read` holds nothing else, so it can be passed to `cce_library_read` as it is (in compact mode `r` carries the short keys `m`, `o`, `l`, `sq` and the hit's `i` is the uid). Stemming is approximated, so a stemmed-only match, or a match only in the title, can fall back to the start of the record; the hit then carries `located: false` beside `read` (`lc` in compact mode), never inside it. A record whose stored shape cannot be interpreted (for example a malformed alternate sequence) never fails the page that lists it: its hit keeps every normal field, its `read` is the start-of-record arguments, and the hit carries `located: false` and a short content-free `locator_error` (`le` in compact mode), while a direct read of that record raises an error that names it.
- `cce_library_read`: `around_query` centres the passage on the first match at or after `message` and reports `match` (`message`, `start`, `end`, or null); `message_end` reads a message range under a total character `limit` and returns `next_message` / `next_offset` to resume (at most 200 messages per call); `toc: true` lists markdown headings with message and character offsets (or a per-message index when there are none): the ATX headings (`#` to `######`) that open a line outside fenced code, found the way CommonMark finds them (fences open indented 0-3 spaces, or further only inside a list item, and close only on their own character and length; a tab, four spaces, U+2028, form feed or a no-break space before backticks makes indented code or text, not a fence; lines end at `\n`, `\r\n` or `\r` only), so a quoted heading, a setext heading, a heading inside a list item and a `#` line inside a code fence are not entries, paged by `toc_offset` / `toc_limit` with an exact `toc_total`; `compact: true` drops the envelope metadata and shortens keys. Block quotes and list items are tracked the way markdown-it-py reads them: a marker followed by five or more spaces makes its first line indented code (which a following unindented line cannot continue, so a heading indented 1-3 spaces after it is top level), an item that starts empty ends at the next blank line, and a quote line is never a lazy continuation of an item. A test pins 3,000 generated documents (list markers followed by one to five columns or a tab, quotes holding fences and empty lines, empty items; no HTML) to markdown-it-py's entries in offset, level and title, whitespace stripped. HTML blocks and link reference definitions are not modelled (a document that has them can differ), a no-break space at the edge of a title is kept, and containers nested more than 32 deep are read as text.

## Where private text may be written

`cce library export --out`, `cce library export-cards --out`, the three packagers (`package-notion-batches.py`, `package-obsidian-vault.py`, `repair-obsidian-inline.py`), `cce archive project feed|corpus --out`, the sealed-archive source and restore paths, `cce project extract --output` and the command lines of the import modules (`import_chatgpt_export_corpus`, `import_claude_export_corpus`, `import_document_export_corpus`, `import_markdown_document_corpus`, `import_claude_local_session_corpus`, whose `--output-root` defaults to the working directory) write a library's or a provider's text as plaintext into a directory the caller names. Each of them applies `corpus_store.require_git_private` to that directory before it writes anything (the library export, `export-cards`, the packagers, `project extract` and the import command lines before they read anything either), and refuses (exit 2, "Git-visible") what that function calls Git-visible.

**Git-visible, exactly.** Outside every Git work tree a directory is private. Inside one it is private only if all of these hold, as Git itself answers them (`git check-ignore --no-index`, so every rule counts: nested `.gitignore` files, `.git/info/exclude`, the global excludes file, negations):

- Git ignores the directory itself. An existing directory is asked by name. One that does not exist yet is asked by name and, for a rule that only matches directories (`out/`), by the matching rule read back, because `out/*` also matches `out/` and ignores what is inside `out`, not `out`.
- Git also ignores the names an export writes beneath it: `.private-probe`, `x.md`, `x.json`, `x.jsonl` and `x/y.md`. A rule that ignores only dotfiles (`.*`), one extension, or only the old probe name does not make a directory private, and `ignored/*` with `!ignored/vault/` does not either.
- An existing directory holds no tracked file.
- The directory is not the work tree itself, and symlinks are resolved first, so the answer is for where the text would land. Nothing is created to find out.
- Case is settled before Git is asked. On a case-insensitive volume `repo/IGNORED/x` reaches the directory `repo/ignored`, while a repository with `core.ignorecase=false` matches its rules to the spelling it is given. Every path component that exists is therefore respelled the way its parent directory lists it (also across Unicode normalisation), and when that differs from what the caller wrote, both spellings must pass, so the strictest answer wins. Two entries that both fit one name, or a directory that cannot be listed, are refused rather than guessed.

**What Git is told.** The git processes run without `GIT_DIR`, `GIT_WORK_TREE`, `GIT_COMMON_DIR`, `GIT_INDEX_FILE`, `GIT_CEILING_DIRECTORIES`, `GIT_DISCOVERY_ACROSS_FILESYSTEM` and `GIT_NAMESPACE` (a `GIT_DIR` that does not exist makes git say "not a git repository" about every directory). Configuration variables stay, so a developer's own ignore rules count.

**Fail closed.** Git's "not a git repository" means "outside every work tree" only when no `.git` exists at or above the path (git says the same of an empty or damaged `.git`, which is refused). Git missing, an ownership refusal, a permission error or any other unexpected answer is an error, never a pass.

**Environment.** The answer is Git's, asked in the caller's environment, so the verdict for one directory can differ between a developer's shell and a clean one (a CI job, a container, another account):

- `GIT_CONFIG_GLOBAL=/dev/null` does not isolate git. It hides the global config file, but not the default global excludes file (`$XDG_CONFIG_HOME/git/ignore`, else `~/.config/git/ignore`), nor a `core.excludesFile` set in any config that is still read (the system config, the repository's own). A run that must not see a developer's rules sets `HOME` and `XDG_CONFIG_HOME` as well (and `GIT_CONFIG_NOSYSTEM=1` for the system config); the tests do.
- Process-scoped configuration is passed to git on purpose: `GIT_CONFIG_COUNT` with `GIT_CONFIG_KEY_n` / `GIT_CONFIG_VALUE_n`, and `GIT_CONFIG_PARAMETERS`. An excludes file set that way counts as one of the rules, which is how a caller states its own.
- `git` is resolved from `PATH`. One that cannot run, or says "not a git repository" beside a real `.git`, is refused; a different `git` earlier on `PATH` that answers wrongly is not detected, so the check is only as sound as the git it finds.
- Case is read from the disk, not from Git (see above), but Git's own matching of rules to names follows the repository's `core.ignorecase`.

Run the check where the export will run, and with the environment it will run in.

**Limits.** This decides whether the text would be *visible* to Git, not whether it is secret: an ignored directory can still be committed by `git add -f`, swept by a global hook or tool that bypasses ignore rules, or picked up by a backup or sync client. It does not look at a repository's tracked files elsewhere. A directory that ignores its own contents with an inner `.gitignore` containing `*` is refused, because that file does not ignore the directory; ignore the directory from its parent instead.

**Not covered by this guard.** `aps_bridge` (`python -m conversation_corpus_engine.aps_bridge --out`) writes the matched threads as notes into the directory the caller names and is not guarded: its documentation names the canonical `--out` as a sealed `_*-private` store that is tracked, not ignored, which this guard would refuse, so the destination is the operator's responsibility (it has no entry point and no test). `import_chatgpt_local_session_corpus` has no command line of its own; the provider pipeline that calls it passes custody. `cce provider import --output-root` is not an instance of the same gap: it is checked by `authorize_corpus_write`, which requires a strict descendant of the registered private store that Git ignores. `cce project route --destination` only records a path, and `cce project sync` then delivers the project into that routed destination: the destination is an organ or repo checkout by design (the `--organ` and `--repo` fields), so the guard is not applied there and the destination is the operator's responsibility.

## Search planning, time budget and capped totals

### Search units and source passages

The CLI (`--unit`) and MCP (`unit`) accept `auto`, `passage` and `conversation`.
They default to `auto`: queries of up to three words, or queries containing a quoted
phrase, try the passage index first within each query plan. Longer unquoted queries
use the conversation index. Auto tries passage phrase, conversation phrase, passage
AND, conversation AND, then the OR alternatives. A stronger conversation match in
short text therefore takes precedence over a partial passage match. All attempts use
the same deadline; explicit `passage` does not fall back. The Python `Library.search`
defaults to `unit="conversation"` to preserve existing callers. Unit selection is
separate from the `plan` option described below.

The passage index contains one FTS5 row per distinct eligible prose block from the
current main-message sequence. Eligibility uses the existing block rules: at least
120 characters of canonical prose, excluding code and paragraphs containing inline
backticks. Only the block's canonical text is ranked, so a focused paragraph is not
diluted by the length of the surrounding conversation. Facets select the eligible
source occurrences before one deterministic occurrence is returned for each block.
Repeated copies retain all their provenance but occupy one result slot. Titles,
short text, code and alternate sequences remain searchable through `conversation`.

Responses state the selected `unit`. `total`, `total_exact` and `total_label` always
count matching conversations and are explicitly labelled `total_unit: conversation`.
For passage results, `unit_total` and `unit_total_exact` count the distinct matching
blocks. Both counts use the configured cap. A passage page's `offset` and
`next_offset` refer to blocks, not conversations; always follow `next_offset`, since
many passages can belong to one conversation or one passage can occur in many.
If a timeout prevents the conversation count, its reported lower bound is the number
of distinct returned conversation identities, with `total_exact: false`.

Each passage hit carries its block digest and original message `start`/`end` range,
plus executable `read` arguments pinned to the retained source `version`. Offsets
are Python character indices in the original normalized message, including Unicode
and CRLF spelling. They are never UTF-8 byte offsets or offsets in the canonical
block used by the index. Reads return at most 1500 characters; the MCP and CLI place
that window around a query match when a paragraph is longer. Compact responses use
`u` for unit, `nu` for total unit, `un` for passage count and `unx` for its exactness.
Ranking, representative occurrence selection, excerpts and counts share one SQLite
read snapshot.

The store schema is `cce.knowledge-library.v2`. Builds maintain the passage index
alongside the existing blocks and occurrences, including incremental rebuilds.
`Library.create(new_path, previous=old_path)` upgrades a copied v1 candidate directly
from its stored blocks, without re-importing sources. Build migrations obey the
candidate's byte and time limits. Published v1 stores still open read-only and use
conversation search; explicit passage search asks for a new candidate. Export-only
resume preserves its qualified seed without a schema write. `verify` checks passage
coverage, row alignment, eligible current occurrences and character-range validity;
full verification also compares indexed text with the compressed block and digest,
and binds every occurrence to its exact source message partition, offsets and
normalization. Publication identity includes the derived library schema, so a
verified schema-only upgrade can publish even when all source evidence is unchanged.

### Query plans

Ranking every match by bm25 means reading the position lists of each matching record, so the cost grows with the number of matches and with how common the words are, not with the size of the page. On a large library a query of several common words can match thousands of records and take many seconds to minutes, while the same words as a quoted phrase, or one rare word, answer almost at once. Search therefore plans the query, bounds its time and caps its count.

**Plan order.** An unquoted query of two or more words (`plan: "auto"`, the default) runs as:

| step | plan | matches a record when |
|---|---|---|
| 1 | `phrase` | all the words are adjacent and in order |
| 2 | `and` | every word occurs, anywhere in the record |
| 3 | `or` | any word occurs |

The planner stops at the first plan that matches at least one record in the filtered set (the `wing`, `lane`, `provider` and `kind` filters apply; the `offset` and `limit` do not, so every page of one query is answered by the same plan), and the response names it in `plan`. If no plan matches, `plan` is `or` with `total` 0. A single word, or a query that already quotes a phrase, is not planned: it runs as written (every word and quoted phrase must match) and reports `plan: "and"`. `--plan` / `plan` forces one plan (`phrase`, `and` or `or`) and runs only that one; on a quoted or single-word query, `phrase` requires all its words adjacent and `or` accepts any of its words or quoted phrases. Pass the `plan` of one response to the next page to skip re-planning.

`and` is exactly what search did before planning existed: its words were always AND-ed (SQLite FTS5 reads space-separated terms as an implicit AND), never OR-ed, so `plan: "and"` returns identical hits in identical order for a query within the limits below. The `total` is identical only up to the count cap: above 1000 matches it reads `1000` with `total_exact: false`, unless `count_cap=None` is passed, which counts exactly as before. The Python `Library.search` now defaults to `plan="auto"` and `count_cap=1000` (and has no time budget unless `max_seconds` is passed); a caller that needs the previous behavior passes `plan="and", count_cap=None`, as `library analyze` does. `or` is new behavior. A broader fallback can surface records that hold only some of the words, which is why the response says which plan answered.

**Words are not search syntax.** `NOT`, `AND`, `OR`, `NEAR` and column prefixes such as `title:` are searched as ordinary words, never as FTS5 operators. A query such as `NOT alpha` or `title:alpha` needs a record holding all of those words to be answered by the phrase or AND plan; otherwise it falls through to the OR plan, which returns the records holding any of them (the previous search returned nothing for it; this is a deliberate behavior change). A term the tokenizer would discard (only punctuation and underscores, such as `___`) constrains nothing, so it is dropped before the plan is chosen: `alpha ___` is the single word `alpha` and reports `plan: "and"`, not `phrase`.

**Query limits.** Every plan runs on at most the first 1000 characters of the query, at most 32 distinct terms (compared case-insensitively; a repeat is merged without a mention because it cannot change a result) and at most 64 characters of each term, so a very long or machine-made query cannot make SQLite build thousands of phrases (one `snippet()` call over that many phrases cannot be interrupted by the time budget). A cut that falls inside a word drops that fragment. When a limit removed something that could change the answer, the response carries `terms_truncated: true` (`tq` in compact mode) and a `notice`. The phrase plan keeps the first 32 words in order, repeats included; queries within the limits behave exactly as before. Control characters in a query (NUL and the other C0 controls, and DEL) count as spaces; a NUL inside a quoted phrase used to end the FTS5 string early and fail the whole search.

**Cost of OR.** `or` accepts every record with any word, so with common words it matches the most records and is the slowest plan; so is `and` with common words when the phrase misses. When an `or` plan matches more than the count cap, the response carries `hint`: "quote a phrase or add terms for a faster, more precise search" (the command line also prints it to stderr). The time budget below is what bounds the case where the phrase misses and the broader plans are large.

**Time budget.** `--max-seconds` (command line) and `max_seconds` (MCP) default to 15 seconds and must be above 0 and at most 600; the Python `Library.search` has no budget unless `max_seconds` is passed. One deadline covers the whole call (the phrase probe, the counts and the ranking) and is enforced through the SQLite progress handler, so a slow ranking is interrupted rather than waited for. When it runs out the call does not fail: it returns the first matches of one plan in index order (the order records were added) with `partial: true`, `ranked: false`, a `notice` saying so and the same `hint`; their `score` is null. Running out of time while a plan is still being probed means "unknown", not "empty": each plan not yet proven empty gets its own short bounded probe (the further allowance below), and the page comes from the first plan that matches, so a slow phrase probe does not hide an AND plan that has matches. `plan` names the plan that supplied the page and the `notice` also names the plan that was running when time ran out. Producing a page, and each probe, gets a short further allowance of its own (the smaller of `max_seconds` and 2 seconds, but never less than a quarter of a second, so even a tiny budget can produce a page), so a call can exceed `max_seconds` by a few of those. If that allowance cuts the page before its first row, or no plan could be probed in time, `hits` is empty but the plan is not proven empty: `next_offset` repeats the same offset so a pager can retry, and the notice says that having no hits does not mean there are none. If every plan finishes its probe and finds nothing, the empty answer is complete and `partial` is false. A call that finishes in time reports `partial: false` and `ranked: true`. `elapsed_s` is the time the search used, so two identical calls return responses that differ in that field (and a response is not byte-stable across calls). Offsets continue in index order for a partial page, which is not the order of the ranked pages; run a narrower query (quote a phrase, add terms) or raise the budget to get ranked results.

**Capped count.** Counting every match of a huge set is itself costly, so `total` is exact only up to `count_cap` (1000 by default in the Python API, fixed at 1000 for the command line and the MCP tool). When more records match, `total` is the cap, `total_exact` is false and `total_label` is `"1000+"` (compact mode carries `n` and `nx`). The cap changes neither which hits are returned nor their order, and `next_offset` keeps advancing past the cap until the last page. `count_cap=None` counts exactly. A count that the time budget interrupts cannot say how many records match: the page that replaces it reports `total` as the offset plus the hits it found, with `total_exact` false and a label of the form `N+` (`0+` when nothing was found before time ran out), so `total_label` can read `0+`, `5+` or `1000+`; it is always a lower bound, never an exact count.

| field | compact key | meaning |
|---|---|---|
| `plan` | `pn` | the plan that answered (`phrase`, `and` or `or`) |
| `partial` | `pt` | true when the time budget ran out |
| `ranked` | `rk` | false when ranking was abandoned and hits are in index order |
| `elapsed_s` | `es` | seconds the search used |
| `total_exact`, `total_label` | `nx` (flag only) | whether `total` is exact; its text form, e.g. `1000+` |
| `notice`, `hint` | `nt`, `ht` | present only on a partial page or a broad OR |

`cce library analyze QUERY` keeps the previous selection: it runs the `and` plan with an exact matched count, so its receipt is unchanged by planning.

## Retrieval benchmark at the MCP boundary

`scripts/bench_mcp.py --library PATH --queries FILE` calls the library MCP tool handlers in-process for a fixed query file and prints one JSON report: per call wall time, response bytes, estimated tokens (`ceil(bytes / 4)`), calls to reach the expected record or passage, and the partial flag, with p50 and p95 per level (search is reported at the depth the harness requests, which is `L0` asked for explicitly because a search that names no depth is answered at L1; read is L2) for the cold first call, the first pass and warm repeats. The exit code is 0 when every threshold holds, 1 when one fails and 2 when the run is invalid. Thresholds are flags with these defaults: `--max-warm-l0-p95-s 1.0`, `--max-cold-first-call-s 3.0`, `--max-l0-tokens 900`, and the expected unit within `--max-unit-calls 2` and `--max-unit-tokens 2500` on at least `--min-unit-rate 0.9` of queries.

`PATH` is a `library.sqlite` file or a published store root that you own. The harness is read-only: the engine opens the database with SQLite `mode=ro`, only the three read tools are called, and the harness compares the size and mtime of every entry beside the library (plus a SHA-256 of `library.sqlite*` with `--hash`) before and after, exiting 2 if anything changed. The report prints query ids, never query text or paths. Cold means a fresh process and connection; the operating system page cache is not dropped. `tests/bench/queries.json` is a synthetic query set for the synthetic library that `tests/test_bench_harness.py` builds; write your own query file to measure a library you own, and keep that file and its report private.

The harness follows each hit's actual `read` arguments, including message, character
offset, sequence and revision. `expected_message` is retained as legacy fixture
metadata and never supplies a locator. Continuation follows the returned cursors.

`tests/test_library_passage_benchmark.py` documents and generates a second synthetic
acceptance library with 660 conversations, 980 distinct eligible blocks and a length
range of 199 to 64,425 characters. Twenty long conversations contain a late, focused
target paragraph; each target has twelve medium-length distractors containing the
same two query words in a longer paragraph, and 400 short records provide background.
The queries contain real word pairs and no source identifier or message hint. The
regression proves conversation ranking misses the first target at rank one while
passage ranking finds it, then runs the MCP benchmark with the two-call, 2500-token,
90-percent known-item and warm-p95-under-one-second gates. These are reproducible
synthetic acceptance measurements, not measurements of an installed private archive.
