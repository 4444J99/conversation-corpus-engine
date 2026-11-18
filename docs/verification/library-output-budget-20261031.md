# Library output budget sizing (sanitized)

This page explains how library output budgets are sized and interpreted. It omits real generation sizes, byte counts, and environment-specific measurements.

## How the budget works

A library candidate includes the SQLite database plus exported derivatives (such as vault notes, hubs, graph data, CSV, and archives). `limits.max_output_bytes` caps the whole output directory, including receipts, and the builder charges writes before they happen. Accounting is intentionally conservative (for example, some archive rewrite behavior is charged), so the charged total can exceed the final on-disk size. Size caps with that behavior in mind.

## Sizing guidance

- Finished output size depends on the export layout/profile. Legacy layouts with many per-record/passage notes and archives can require substantially more headroom than database-only or compact vault profiles.
- Set `max_output_bytes` with margin above the expected database-plus-derivatives total, and ensure the disk can satisfy admission/free-space checks.
- Vault profile choice affects derivative size: builds that emit no vault/archive outputs, or only compact card-style outputs, require less derivative headroom than legacy full exports. Re-measure before relaxing caps derived from older layouts.

## Reading build progress

`cce library build` emits progress events (for example on stderr as JSON lines) that include elapsed time. Stages such as indexing, fingerprinting, verification, export, export verification, and artifact hashing can run for extended periods without additional events; long quiet intervals after a stage event usually indicate active work rather than a stall. Export-mode resumes skip stages that are not needed.

## Byte-limit failures

When the output byte limit is exceeded, the builder reports the key accounting values (database size, planned/writing/receipt bytes, working budget, and configured cap) in its error message. If a failure occurs with almost no headroom left, the detailed failure receipt may not fit within the configured cap; in that case the raised error remains the authoritative source for the limit details.
