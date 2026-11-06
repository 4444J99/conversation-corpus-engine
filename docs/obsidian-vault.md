# Bounded Obsidian reading vault

`scripts/package-obsidian-vault.py` creates a fresh reading derivative of a published CCE library vault. It addresses native indexing failures caused by very large individual Markdown notes. It does not change the canonical generation, source vault, database, or earlier reading copies.

```sh
uv sync --extra obsidian
uv run --extra obsidian python scripts/package-obsidian-vault.py /private/library/generation/vault \
  --agent-entry /private/library/generation/agent-entry.md \
  --source-generation 20261027 \
  --output '/private/reading/Knowledge Universe Paginated' \
  --max-note-bytes 262144 \
  --max-source-bytes 134217728 \
  --max-output-bytes 8589934592 \
  --max-seconds 1800
```

A generation built with the default `--vault-profile none` has no `vault/` to package; the script then fails naming `--vault-profile`. Write one from the published generation with `cce library export --vault-profile cards --out DIR` (one capped card per record; `full` keeps whole records in linked parts) and package `DIR/vault`, or write a card folder with `cce library export-cards --out DIR`. Building again with another profile does not help while the sources are unchanged, because publication ignores the profile. Those layouts already keep every note under 64 KiB, every folder under 200 entries and every hub under 100 entries, so this packager is mainly for a legacy bulk vault (see [vault profiles](knowledge-library.md#vault-profiles)); it still accepts a profile vault.

The `obsidian` extra installs the required `markdown-it-py==4.2.0`; the boundary mapper refuses other parser versions. The development extra includes the same pin for tests.

The output parent must already exist and the output itself must not exist. Existing outputs are never replaced. A failed attempt remains available for inspection; use a new destination for a subsequent attempt. Source/output overlap and output anywhere within a recognized canonical `library-all/generations/` generation are refused. So is an output that Git can see (inside a work tree and not ignored by it), before the source is read: the derivative holds the library as plaintext. `scripts/repair-obsidian-inline.py` applies the same rule to its `--output`.

Every Markdown output, including record notes, repeated passages, hubs, original-path indexes, and optional agent entry, stays within the note-byte limit. Conservative source segments usually use half that limit, leaving room for navigation rewrites and code-fence wrappers. Dense anchor links receive smaller segments. The bound includes UTF-8 paths expanding into percent-encoded navigation targets. A single long UTF-8 line can be divided without splitting a character. Splitting changes the reading layout; it does not truncate source text.

The original path becomes a small index linking to numbered parts in the same directory. Large indexes receive bounded index pages. Generated links to `#message-N` headings are redirected to the part containing that heading. Record links without a fragment continue to open the original-path index. Links to an explicitly supplied external agent-entry file are relocated into the derived vault. Links written inside the agent entry itself are rebased the same way: a link that names the vault directory (`vault/Home.md`, written relative to the generation directory) resolves to its sibling in the package (`Home.md`). External links retain their original targets. This packager handles CCE's generated navigation and preserves source bodies reversibly. Rendering checks compare against the existing canonical Markdown baseline, which can already contain active links and generic HTML. They do not establish equivalence for arbitrary Obsidian plugins.

Fenced code is closed and reopened at part boundaries. A rendered note that ends inside an open fence fails packaging, and fails verification, instead of shipping; split parts are exempt by construction because their wrappers close any fence they carry over. The fence tracker follows fences inside list items (marker lines and content-indented continuation lines, including fences opened through alternating list and blockquote prefixes, and a tab after a list marker's space expands to the next tab stop when measuring the item's content indent and the fence's column) and ignores fence markers inside raw HTML type-1 blocks (<pre>, <script>, <style>, <textarea>, which end only at their matching closing tag, never at a blank line, including blocks opened through alternating list and blockquote prefixes such as `- > <pre>`) and HTML comments; a literal block opened inside a list item ends when a later line leaves that item, a fence opened inside a list item likewise ends when a later line leaves that item (including a fence line outside its content indent, which then opens a new fence, or a blockquote starting outside the item), and a fence that crosses a part boundary is reopened in the next part with its list prefix, so its content-indented closing line closes it there too, and a part that ends inside a list fence closes it with a footer indented to the fence's own column rather than a column-zero line that would end the item and open a new fence. The same tracker drives link rewriting, anchor collection, and navigation growth, so a link after a list-contained fence, <pre> block, or comment is rewritten rather than stranded as code. The manifest records `source_generation`, the caller-supplied identity of the library generation the source vault came from; packaging refuses to run without it, so a package never needs its generation derived from content hashes after the fact. Quoted fences and indented code after quote prefixes retain their code context. Inline boundaries are resolved by the pinned CommonMark parser, including multiline code spans, exact-length backtick runs, and block-container indentation. The mapper traces original UTF-8 byte coordinates, verifies the actual delimiter bytes, and refuses uncertain mappings. It parses inline content only in blocks crossing part boundaries. Inline code that crosses a boundary is explicitly closed and reopened, and a split never cuts through a backtick delimiter run. A continuation label keeps a long inline delimiter from being interpreted as a block fence. These wrappers lie outside the ledger's source-body byte range. Original source text is not normalized: CRLF, Unicode, trailing newlines, binary non-Markdown files, and link targets remain recoverable exactly.

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

Synthetic tests cover whole-file and segment reconstruction, Unicode and long lines, CRLF, binary attachments, generated anchor navigation, dense fragment links, literal fenced/inline/quoted code, multiline inline spans, Unicode navigation expansion, private modes, tamper detection, source caps, output caps including SQLite growth, canonical-generation isolation, and exclusive creation.

A controlled native reproduction isolated an individual-note indexing failure: the original large note failed to index, while a bounded derivative completed indexing and rendered a late part. That establishes the individual-note packaging fix only. Each new full vault still needs its own native opening, completed-indexing observation, search, and linked-part checks. `native_obsidian_acceptance` therefore remains `unverified` in the packaging manifest and is recorded separately by the native test owner. Private reproduction measurements and receipt identifiers are retained in the owner's private repository notes.

## Audit method and acceptance limits

Full-package verification reconstructs every original source file to its recorded SHA-256 and compares the complete source-tree digest before and after packaging. This checks preservation of source bytes. It does not, by itself, establish equivalent Markdown rendering or successful native indexing.

A line-scoped scan proved insufficient for inline-code boundaries: it missed multiline spans and reported some false crossings. The replacement audit uses the pinned CommonMark parser to inspect every split source note and its part boundaries, verifies source and rendered-part hashes against the package ledger, and checks that no split cuts through a delimiter. An independent inverse-edit audit checks that navigation rewrites do not occur inside crossing code spans.

Repairs create a fresh derivative or retain prior metadata and changed parts in hidden repair history. Correcting an audit requires rewriting the union of the old and new affected parts, including reversions of obsolete wrappers. The original package and canonical generation remain unchanged, and the updated manifest records source-package and audit hashes as lineage. Full reconstruction and metadata verification run again after repair.

Inline-code continuation wrappers need their own rendering check. A closing backtick run at the start of a line can become a fenced block; a continuation label prevents that interpretation. Independent CommonMark acceptance compares repaired parts with the canonical per-note baseline and checks for additional image, external-link, or raw-HTML tokens. The comparison uses token multisets, so it does not prove token-position equivalence or behavior under arbitrary native plugins. Existing source notes can already contain active tokens.

The recorded audits passed byte reconstruction and the scoped CommonMark comparison. Full-vault native acceptance remained unmeasured when the computer-use control was unavailable. Small-vault indexing, full-package byte verification, boundary-rendering acceptance, and full-vault native acceptance remain separate observations. Detailed private measurements, receipt filenames, earlier failed receipts, and repair history are retained in the owner's private repository notes.
