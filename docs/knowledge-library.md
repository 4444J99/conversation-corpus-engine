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
         FTS search   block graph   Markdown vault
                                     |
                                Notion ZIP / CSV
```

## Build and retrieve

Register a private store through the existing `cce corpus-store register` interface before building. Use the same project registry and the real store path, not a compatibility symlink. See `cce corpus-store --help` for the installed interface.

```sh
cce library build --manifest /private/library-sources.json --project-root /private/project --store-root /private/store --publish
cce library status --store-root /private/store
cce library search lattice --store-root /private/store --limit 20 --offset 0
cce library search "ledger entries" --store-root /private/store --plan auto --max-seconds 15
cce library read RECORD_UID --store-root /private/store --message 0 --offset 0 --limit 12000
cce library history RECORD_UID --store-root /private/store
cce library read RECORD_UID --store-root /private/store --version 1.1.0
cce library read RECORD_UID --store-root /private/store --digest SNAPSHOT_SHA256
cce library read RECORD_UID --store-root /private/store --sequence alternate --message 0 --limit 12000
cce library analyze lattice --output /private/store/derived/analysis/lattice --project-root /private/project --store-root /private/store --limit 5
cce library verify --store-root /private/store
```

`CCE_LIBRARY_STORE_ROOT` supplies the store when a command omits it; the existing `CCE_CORPUS_STORE_ROOT` is a fallback. Search returns a total (exact up to 1000 matches), bounded excerpts, stable record identities, vault paths and the next offset, and it plans and time-bounds the query (see [search planning](#search-planning-time-budget-and-capped-totals)). Read retrieves one message span and reports main/alternate sequence counts; use `--sequence alternate` (MCP `sequence: "alternate"`) to page retained conversation branches. History and digest selection also expose retained snapshots that were not selected as the current body. `cce-mcp --library-store-root /private/store` adds three read-only tools: `cce_library_search`, `cce_library_read`, and `cce_library_history`. Clients cannot choose arbitrary filesystem paths through these tools.

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

Each generation contains `vault/Home.md`, per-record notes, facet hubs, shared passages, `graph.jsonl`, `library-index.jsonl`, `records.csv`, `notion-import.zip`, and a small `agent-entry.md`. Keep personal annotations in a separate reading folder rather than the immutable generation. Large transcript notes can crash a native Markdown indexer even when the complete library passes its data-integrity checks: an observed Obsidian 1.14.5 crash was reproduced with one unchanged 47 MB note.

For large Obsidian libraries, use `scripts/package-obsidian-vault.py` to create a fresh reading folder with bounded pages, repaired navigation and a reversible byte ledger. Supply the generation's `vault/` as its source and its `agent-entry.md` through `--agent-entry`; open the resulting folder as a vault, then open `Home.md`. Every original file can be reconstructed and hash-checked, while its original note path remains available as a page index. See [bounded Obsidian packaging](obsidian-vault.md) for the command and verification contract. Native application acceptance remains a separate observation from packaging.

The full ZIP is a lossless import source. For large libraries, use smaller Notion batches: the official [import guidance](https://www.notion.com/help/import-data-into-notion) warns that ZIPs with 10,000 or more files can fail or partially import, even below the 5 GB ZIP limit. It also documents per-file limits of 5 MB on Free and 50 MB on paid plans. Local ZIP parity does not establish successful Notion import, cross-batch links or heading-anchor behavior; verify those in the account. Neither application becomes a new identity authority.

Other knowledge-universe components consume `library-index.jsonl` plus its referenced Markdown files. Each index row includes CCE UID, version, object digest, content digest, source reference, body coverage, and collection taxonomy. Preserve those fields in any derived atom, vector or wiki index; do not replace CCE identity with filenames or drop duplicate provenance. My Knowledge Base consumes the verified reading index; native text-analysis inputs use the bounded revision bridge below.

For native structural analysis, `library analyze` writes bounded original-revision inputs accepted by LingFrame and atomic-substrata, including main and alternate messages. It preserves exact block spans and source identities instead of analyzing display links. See [the native analysis bridge](knowledge-analysis-bridge.md) for model mappings, limits and the observed consumer serialization boundary.

Indented and quoted code receives explicit code fences in reading notes. Ambiguous escaped or multiple-backtick prose examples also use literal wrappers so a legacy inline matcher cannot activate source examples. The wrapper is longer than any embedded backtick run and preserves the literal source text. Source revisions remain unchanged; export validation still rejects active unsafe links. A renderer fix applies to later exports; existing immutable generations retain their producer version and separate acceptance evidence.

Build receipts report source findings and `coverage_complete` separately from successful verification/publication. `coverage_complete=false` can be a valid, useful library with visibly incomplete source bodies. Builds have a wall deadline, record and output budgets, private permissions, and retained failure receipts. A successful code test or ZIP export does not establish exhaustive content recovery or a completed application import.

### Progressive disclosure through the MCP tools

The two MCP tools support a locate-then-read flow that stays inside a token budget. Defaults keep the previous output (new fields are additive).

- `cce_library_search`: `depth: "L0"` returns only `uid`, a one-line `title` (80 characters at most), `score` (bm25, higher is better) and `read`, within `budget_tokens` (default 900, at most 1200 at L0; at most 20000 at L1). Tokens are estimated as UTF-8 bytes / 4 of the compact JSON text, and the response states the estimate (`token_estimate`, or `tk` in compact mode). Hits that do not fit are left for `next_offset` and `truncated` is true; a budget too small for a single hit is an error. `compact: true` shortens keys (the tool schema lists them). Every response states `total` with `total_exact` and `total_label` (the number itself while the count is exact, and `1000+` once more than 1000 records match), and, additively, the `plan` that answered, `partial`, `ranked` and `elapsed_s` (see [search planning](#search-planning-time-budget-and-capped-totals)).
- Passage handoff: every hit carries `read`, the exact `cce_library_read` arguments (`uid`, `message`, `offset`, `limit`, plus `sequence` when the best match is in the alternate branch) for the first message that holds every query term, falling back to the first message holding any. Stemming is approximated, so a stemmed-only match can fall back to the start of the record, which is flagged `located: false`. A record whose stored shape cannot be interpreted (for example a malformed alternate sequence) never fails the page that lists it: its hit keeps every normal field and its `read` is the start-of-record locator with `located: false` and a short content-free `locator_error` (`le` in compact mode), while a direct read of that record raises an error that names it.
- `cce_library_read`: `around_query` centres the passage on the first match at or after `message` and reports `match` (`message`, `start`, `end`, or null); `message_end` reads a message range under a total character `limit` and returns `next_message` / `next_offset` to resume (at most 200 messages per call); `toc: true` lists markdown headings with message and character offsets (or a per-message index when there are none), paged by `toc_offset` / `toc_limit` with an exact `toc_total`; `compact: true` drops the envelope metadata and shortens keys.

### Search planning, time budget and capped totals

Ranking every match by bm25 means reading the position lists of each matching record, so the cost grows with the number of matches and with how common the words are, not with the size of the page. On a large library a query of several common words can match thousands of records and take many seconds to minutes, while the same words as a quoted phrase, or one rare word, answer almost at once. Search therefore plans the query, bounds its time and caps its count.

**Plan order.** An unquoted query of two or more words (`plan: "auto"`, the default) runs as:

| step | plan | matches a record when |
|---|---|---|
| 1 | `phrase` | all the words are adjacent and in order |
| 2 | `and` | every word occurs, anywhere in the record |
| 3 | `or` | any word occurs |

The planner stops at the first plan that matches at least one record in the filtered set (the `wing`, `lane`, `provider` and `kind` filters apply; the `offset` and `limit` do not, so every page of one query is answered by the same plan), and the response names it in `plan`. If no plan matches, `plan` is `or` with `total` 0. A single word, or a query that already quotes a phrase, is not planned: it runs as written (every word and quoted phrase must match) and reports `plan: "and"`. `--plan` / `plan` forces one plan (`phrase`, `and` or `or`) and runs only that one; on a quoted or single-word query, `phrase` requires all its words adjacent and `or` accepts any of its words or quoted phrases. Pass the `plan` of one response to the next page to skip re-planning.

`and` is exactly what search did before planning existed: its words were always AND-ed (SQLite FTS5 reads space-separated terms as an implicit AND), never OR-ed, so `plan: "and"` returns identical hits in identical order. `or` is new behavior. A broader fallback can surface records that hold only some of the words, which is why the response says which plan answered.

**Query limits.** Every plan runs on at most the first 1000 characters of the query, at most 32 distinct terms (compared case-insensitively; a repeat is merged without a mention because it cannot change a result) and at most 64 characters of each term, so a very long or machine-made query cannot make SQLite build thousands of phrases (one `snippet()` call over that many phrases cannot be interrupted by the time budget). A cut that falls inside a word drops that fragment. When a limit removed something that could change the answer, the response carries `terms_truncated: true` (`tq` in compact mode) and a `notice`. The phrase plan keeps the first 32 words in order, repeats included; queries within the limits behave exactly as before. Control characters in a query (NUL and the other C0 controls, and DEL) count as spaces; a NUL inside a quoted phrase used to end the FTS5 string early and fail the whole search.

**Cost of OR.** `or` accepts every record with any word, so with common words it matches the most records and is the slowest plan; so is `and` with common words when the phrase misses. When an `or` plan matches more than the count cap, the response carries `hint`: "quote a phrase or add terms for a faster, more precise search" (the command line also prints it to stderr). The time budget below is what bounds the case where the phrase misses and the broader plans are large.

**Time budget.** `--max-seconds` (command line) and `max_seconds` (MCP) default to 15 seconds and must be above 0 and at most 600; the Python `Library.search` has no budget unless `max_seconds` is passed. One deadline covers the whole call (the phrase probe, the counts and the ranking) and is enforced through the SQLite progress handler, so a slow ranking is interrupted rather than waited for. When it runs out the call does not fail: it returns the first matches of one plan in index order (the order records were added) with `partial: true`, `ranked: false`, a `notice` saying so and the same `hint`; their `score` is null. Running out of time while a plan is still being probed means "unknown", not "empty": each plan not yet proven empty gets its own short bounded probe (the further allowance below), and the page comes from the first plan that matches, so a slow phrase probe does not hide an AND plan that has matches. `plan` names the plan that supplied the page and the `notice` also names the plan that was running when time ran out. Producing a page, and each probe, gets a short further allowance of its own (the smaller of `max_seconds` and 2 seconds, but never less than a quarter of a second, so even a tiny budget can produce a page), so a call can exceed `max_seconds` by a few of those. If that allowance cuts the page before its first row, or no plan could be probed in time, `hits` is empty but the plan is not proven empty: `next_offset` repeats the same offset so a pager can retry, and the notice says that having no hits does not mean there are none. If every plan finishes its probe and finds nothing, the empty answer is complete and `partial` is false. A call that finishes in time reports `partial: false` and `ranked: true`. `elapsed_s` is the time the search used. Offsets continue in index order for a partial page, which is not the order of the ranked pages; run a narrower query (quote a phrase, add terms) or raise the budget to get ranked results.

**Capped count.** Counting every match of a huge set is itself costly, so `total` is exact only up to `count_cap` (1000 by default in the Python API, fixed at 1000 for the command line and the MCP tool). When more records match, `total` is the cap, `total_exact` is false and `total_label` is `"1000+"` (compact mode carries `n` and `nx`). The cap changes neither which hits are returned nor their order, and `next_offset` keeps advancing past the cap until the last page. `count_cap=None` counts exactly.

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

`scripts/bench_mcp.py --library PATH --queries FILE` calls the library MCP tool handlers in-process for a fixed query file and prints one JSON report: per call wall time, response bytes, estimated tokens (`ceil(bytes / 4)`), calls to reach the expected record or passage, and the partial flag, with p50 and p95 per level (search is L0, read is L2) for the cold first call, the first pass and warm repeats. The exit code is 0 when every threshold holds, 1 when one fails and 2 when the run is invalid. Thresholds are flags with these defaults: `--max-warm-l0-p95-s 1.0`, `--max-cold-first-call-s 3.0`, `--max-l0-tokens 900`, and the expected unit within `--max-unit-calls 2` and `--max-unit-tokens 2500` on at least `--min-unit-rate 0.9` of queries.

`PATH` is a `library.sqlite` file or a published store root that you own. The harness is read-only: the engine opens the database with SQLite `mode=ro`, only the three read tools are called, and the harness compares the size and mtime of every entry beside the library (plus a SHA-256 of `library.sqlite*` with `--hash`) before and after, exiting 2 if anything changed. The report prints query ids, never query text or paths. Cold means a fresh process and connection; the operating system page cache is not dropped. `tests/bench/queries.json` is a synthetic query set for the synthetic library that `tests/test_bench_harness.py` builds; write your own query file to measure a library you own, and keep that file and its report private.
