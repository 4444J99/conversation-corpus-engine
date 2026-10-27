# Bounded Obsidian reading vault

`scripts/package-obsidian-vault.py` creates a fresh reading derivative of a published CCE library vault. It addresses native indexing failures caused by very large individual Markdown notes. It does not change the canonical generation, source vault, database, or earlier reading copies.

```sh
python3 scripts/package-obsidian-vault.py /private/library/generation/vault \
  --agent-entry /private/library/generation/agent-entry.md \
  --output '/private/reading/Knowledge Universe Paginated' \
  --max-note-bytes 262144 \
  --max-source-bytes 134217728 \
  --max-output-bytes 8589934592 \
  --max-seconds 1800
```

The output parent must already exist and the output itself must not exist. Existing outputs are never replaced. A failed attempt remains available for inspection; use a new destination for a subsequent attempt. Source/output overlap and output anywhere within a recognized canonical `library-all/generations/` generation are refused.

Every Markdown output, including record notes, repeated passages, hubs, original-path indexes, and optional agent entry, stays within the note-byte limit. Conservative source segments usually use half that limit, leaving room for navigation rewrites and code-fence wrappers. Dense anchor links receive smaller segments. A single long UTF-8 line can be divided without splitting a character. Splitting changes the reading layout; it does not truncate source text.

The original path becomes a small index linking to numbered parts in the same directory. Large indexes receive bounded index pages. Generated links to `#message-N` headings are redirected to the part containing that heading. Record links without a fragment continue to open the original-path index. Links to an explicitly supplied external agent-entry file are relocated into the derived vault. External links retain their original targets. This is a packager for CCE's generated navigation and inert source bodies, not a general Markdown parser or arbitrary Obsidian-plugin migration tool.

Fenced code is closed and reopened at part boundaries. Quoted fences, indented code after quote prefixes, and exact-length inline backtick spans remain literal. Original source text is not normalized: CRLF, Unicode, trailing newlines, binary non-Markdown files, and link targets remain recoverable exactly.

## Preservation evidence

The hidden `.cce-package/` directory contains:

| File | Purpose |
| --- | --- |
| `sources.jsonl` | Ordered source paths, exact byte sizes, and SHA-256 values |
| `navigation.sqlite` | Generated heading-to-part mapping |
| `ledger.jsonl` | Original byte ranges, rendered body ranges, inverse link edits, and source/rendered hashes |
| `manifest.json` | Completed verification receipt, limits, counts, source-tree hashes, and metadata hashes |

For each ledger segment, reconstruction strips only the recorded reading wrappers and reverses the recorded link edits. The packager checks every segment hash and reconstructs each whole source-file hash from the resulting bytes. It also rereads the full source tree and requires the ordered source-tree digest to match its pre-write value. The success manifest is written last, after reconstruction and output-size checks pass. The generated vault contains the preservation ledger; it does not need another uncompressed copy of the original Markdown.

The script's importable `reconstruct(output, relative_path)` generator yields the exact original bytes. `verify_package(output)` rechecks the ledger, source inventory, navigation database, every reconstructed source file, every index, private generated-file permissions, and all Markdown byte limits. Native `.obsidian` settings are excluded from both source evidence and derivative verification because opening the vault changes them. No source content directory is excluded.

## Bounds and native acceptance

The CLI runs at nice level 10 with a hard POSIX wall timer. Each input file has a configurable byte cap before allocation. Output bytes, including navigation database pages and preservation metadata, share one cap; SQLite page growth is limited before writes. A measured 1 GiB free-space floor remains in force. Generated directories and files use modes 0700 and 0600. Source symlinks and special files are refused.

Synthetic tests cover whole-file and segment reconstruction, Unicode and long lines, CRLF, binary attachments, generated anchor navigation, dense fragment links, literal fenced/inline/quoted code, private modes, tamper detection, source caps, output caps including SQLite growth, canonical-generation isolation, and exclusive creation.

A controlled 2026-10-27 native reproduction indexed a single 47,171,302-byte record and crashed. Its derived test vault contained 392 reading parts plus the original-path index and Home, with a largest note of 131,133 bytes. The native app then completed indexing and rendered a late part. That isolates the individual-note packaging fix; it is not evidence that an entire production vault has completed native indexing. Each new full vault still needs its own native opening, completed-indexing observation, search, and linked-part checks. `native_obsidian_acceptance` therefore remains `unverified` in the packaging manifest and is recorded separately by the native test owner.
