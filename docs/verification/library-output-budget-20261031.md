# Library output budget sizing - 2026-10-31

A library candidate holds the SQLite database plus every derivative exported from it (vault notes, hubs, graph, CSV, Notion ZIP, search entry). `limits.max_output_bytes` caps the whole directory, receipts included, and the builder charges each write before it happens. `library_export._CheckedBinary` deliberately charges ZIP local-header rewrites as well, so the charged total can exceed the final on-disk size. That conservative accounting is unchanged; size the cap with it in mind.

## Measured generations (aggregate numbers only)

| Generation | Database bytes | Output | Cap | Result |
| --- | --- | --- | --- | --- |
| Published 2026-10-27 | 8,472,334,336 (about 7.89 GiB) | artifacts about 15.2 GiB | 16 GiB | Verified and published |
| Refreshed candidate | 10,272,907,264 (about 9.57 GiB) | tripped during export | 16 GiB | `library output byte limit exceeded` |

The refreshed candidate had finished ingestion, indexing, the fingerprints and full database verification before the cap tripped, so nothing had to be redone: it was resumable with `resume_mode: "export"`, `limits.max_output_bytes` raised to 24 GiB (25,769,803,776) and a larger `limits.max_seconds`. Export resume clones the verified database and regenerates only the exports (see `library-indexed-resume-20261027.md`).

## Sizing rule of thumb

Derivatives come to about 0.7 to 0.9 times the database, so the finished output is about 1.7 to 1.9 times the database. The 2026-10-27 generation sits at the upper edge of that range (15.2 GiB against a 7.89 GiB database). Set `max_output_bytes` to at least twice the database size before a build, and leave extra headroom (the 24 GiB resume was about 2.5 times the refreshed database) for the ZIP header rewrites that are charged on top. A cap that is only a little above 1.7 times the database will pass on a small corpus and trip during export on a larger one. The cap also drives the admission check (free space must cover the cap plus 1 GiB outside export-reuse modes), so raise it only as far as the disk allows.

## Reading a build while it runs

`cce library build` prints one `{"library_progress": {...}}` line per event on stderr. Every event carries `elapsed_seconds` since the build started. The stages, in order, and what the silence after each one means:

| Stage | Marks the start of | Typical long-running work |
| --- | --- | --- |
| `index_blocks` | block indexing | Rebuilds the occurrence view, then computes the status summary; the next event only appears when both are committed |
| `fingerprint` | record and evidence fingerprints | Two passes over every row and retained revision |
| `verify` | full database verification | Integrity check plus decompress-and-hash of every stored revision object |
| `export` | derivative export | Writes the vault, graph, CSV and ZIP; the bytes are charged against the cap as they are written |
| `verify_export` | export verification (carries `database_bytes`, `planned_output_bytes`, `working_budget`) | Re-reads the written tree and ZIP |
| `artifacts` | per-file hashing of the whole output | One full read of every artifact |

Ingestion also emits `{"accepted": ..., "changed": ...}` every 100 observations. Export-mode resumes skip `index_blocks` and `verify`. A candidate that sits quietly for minutes after one of these events is working, not stalled; judge it by the last event plus its `elapsed_seconds` against `limits.max_seconds`.

## Byte-limit failures name their numbers

The main raise now reads `library output byte limit exceeded: database=... planned=... writing=... receipts=... budget=... max_output_bytes=...`, where `database` is the last measured database size, `planned` the bytes already charged to exports, `writing` the write about to be charged, `receipts` the receipt files, `budget` the working budget (`max_output_bytes` less the 4096-byte failure reserve) and `max_output_bytes` the configured cap. The leading phrase is unchanged. The same string is stored in the failed `build-state.json`, so the numbers survive the process.
