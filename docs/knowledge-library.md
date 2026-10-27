# Unified private knowledge library

CCE materializes one versioned library from normalized web archives, native coding-session inventories, and explicitly selected documents. Its two wings are `ai` (conversation and coding lanes, plus support material) and `authored` (research, creative, and writing collections). Collection placement is not a claim of sole human authorship.

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
cce library read RECORD_UID --store-root /private/store --message 0 --offset 0 --limit 12000
cce library history RECORD_UID --store-root /private/store
cce library read RECORD_UID --store-root /private/store --version 1.1.0
cce library read RECORD_UID --store-root /private/store --digest SNAPSHOT_SHA256
cce library verify --store-root /private/store
```

`CCE_LIBRARY_STORE_ROOT` supplies the store when a command omits it; the existing `CCE_CORPUS_STORE_ROOT` is a fallback. Search returns an exact total, bounded excerpts, stable record identities, vault paths and the next offset. Read retrieves one message span; history and digest selection also expose retained snapshots that were not selected as the current body. `cce-mcp --library-store-root /private/store` adds three read-only tools: `cce_library_search`, `cce_library_read`, and `cce_library_history`. Clients cannot choose arbitrary filesystem paths through these tools.

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

The manifest is private: it contains local source locations. Native readers retain every inventory identity, with `full`, `partial`, or `metadata_only` body coverage. They handle Codex, Claude Code, Gemini CLI, OpenCode, Copilot CLI and Antigravity formats. Composite Claude agent identities and explicitly declared Codex ancestor headers retain their source evidence; unexplained mismatches stay unavailable. Antigravity protobuf text recovery has unknown semantic roles and remains partial. Missing or unsupported bodies remain visible. Documents support UTF-8 text, Markdown, JSON, YAML, CSV, Fountain, TeX, Org, static HTML, FDX XML, DOCX XML and embedded PDF text through `pdftotext`; PDF images are not OCR. Apple Pages previews or optional `pages2text` conversion remain partial. Native conversion has a 30-second deadline, CPU and output bounds, and preserves source bytes. Unsupported files remain metadata records. Cloud placeholders are recorded without hydration. Symlinks are not followed. A source manifest defines measured scope; it does not prove exhaustive discovery of cloud or historical holdings.

`snapshot_archives` use the same normalized directory layout as `archives`, but add observations without replacing any existing current identity. They recover older or alternate source exports safely; a previously unknown identity is still admitted. Incremental generations inherit the full record ledger and link to the prior coverage report. A remaining inherited coverage gap keeps `coverage_complete` false.

To reuse a failed generation whose source ingestion finished, set `resume_generation` to its absolute generation directory and list only supplemental sources in the new manifest. The builder verifies the failed state, source counts, manifest, publication base, database integrity and every normalized object digest. It hash-binds and compacts a new copy, leaving the seed untouched; baseline observations count against the new record budget. The report retains the seed receipt and all coverage findings. Older candidates without a phase marker are explicitly labeled as weaker legacy ingestion evidence. Set measured `limits` in the manifest for the next invocation; a resume never changes global limits or hides the failed attempt.

## Preservation and condensation

The SQLite store retains compressed, hashed normalized source objects, provenance observations, and source snapshots. Original file hashes are retained where the adapter reads the original bytes. Semantic versions start at `1.0.0`: classification changes advance major, body changes minor, and metadata changes patch. A lower-coverage observation never replaces a fuller current body. Replaying a known source snapshot does not revert the current revision; all observations remain retrievable.

Condensation replaces repeated eligible prose blocks with a shared passage and backlinks to every occurrence. Matching conservatively normalizes line endings, common Markdown quote depth and terminal newlines. Code remains distinct. Exact raw spans and normalization records are retained. The derived block index stores compressed eligible prose; code and short text remain in original revisions and full-text search. An interrupted rebuild restores both occurrence rows and their lookup index. This is deterministic reference extraction, not an inference that two similarly worded claims have the same meaning. Explicit source Markdown links/images and raw HTML are neutralized in reading views; consumer autolinking may still make bare URLs clickable. Historical instructions remain evidence and confer no authority.

## Reading surfaces and consumers

Each generation contains `vault/Home.md`, per-record notes, facet hubs, shared passages, `graph.jsonl`, `library-index.jsonl`, `records.csv`, `notion-import.zip`, and a small `agent-entry.md`. Open `vault` as an Obsidian folder. Import the ZIP or CSV through Notion's Markdown/CSV importer; ZIP parity is verified locally, while actual Notion link behavior requires account-side verification. Neither application is a new identity authority. Keep personal annotations outside generated generation directories.

Other knowledge-universe components consume `library-index.jsonl` plus its referenced Markdown files. Each index row includes CCE UID, version, object digest, content digest, source reference, body coverage, and collection taxonomy. Preserve those fields in any derived atom, vector or wiki index; do not replace CCE identity with filenames or drop duplicate provenance. LingFrame and atomic-substrata may analyze these derivatives without owning the source ledger.

Build receipts report source findings and `coverage_complete` separately from successful verification/publication. `coverage_complete=false` can be a valid, useful library with visibly incomplete source bodies. Builds have a wall deadline, record and output budgets, private permissions, and retained failure receipts. A successful code test or ZIP export does not establish exhaustive content recovery or a completed application import.
