# Library query performance: 2026-10-27

The first production candidate exposed a costly repeated-passage summary query. It contained 1,622,332 unique blocks, 2,568,557 occurrences and 292,107 repeated block keys. SQLite scanned the blocks primary-key index, then fetched BLOB-bearing table rows to obtain eligibility and character counts. The same summary ran once at the end of indexing and again in the builder. Export used a similar block-driven query.

A read-only count of repeated keys using the existing occurrence index completed in 0.163 seconds. This identifies a cheaper starting point; it does not measure complete summary latency.

The source change groups repeated occurrence keys before joining block metadata. This works with existing databases and reduces eligible block lookups from all block keys to repeated keys. New index builds also create `blocks_eligible_digest_chars` on `(eligible,digest,chars)` after bulk insertion. SQLite's query plan uses this covering index without fetching block text. Both derived indexes are dropped and rebuilt inside one savepoint; cancellation restores the prior rows and indexes.

The builder reuses the status returned by indexing. Scoped SQLite progress handlers invoke the caller's deadline or resource callback during long SQL execution, propagate its original exception, restore nested owned handlers and clear their state on exit. Existing schemas remain readable without migration.

| Synthetic measurement | Elapsed |
|---|---:|
| Original block-driven query | 16.300 ms |
| Repeated keys grouped first, existing schema | 3.565 ms |
| Repeated keys grouped first, covering index | 1.393 ms |
| Covering index creation | 13.026 ms |

The fixture had 3,840 blocks, 4,608 occurrences, 16 KiB BLOBs and a 32 MiB SQLite cache. All three queries returned exactly 768 repeated blocks, 768 additional occurrences and 12,582,912 repeated characters. The covering index added 307,200 bytes. These small-fixture measurements do not predict a production speedup.

The production candidate was not modified. A bounded read-only trial of the rewritten query on that candidate reached the 10-second limit and was interrupted correctly. Its database still lacks the new covering index. No claim is made that the existing production generation has the new index or achieves the synthetic timings.

Validation: 59 store, build, resume and export tests passed. Regression cases cover exact counts, covering query plans, existing-schema compatibility, SQL-only status and verification cancellation, nested handler restoration, interruption while creating either index, rollback and reuse of the completed status. A three-record resume fixture now requests a 16 MiB output reservation instead of 16 GiB; its preservation assertions, the real storage preflight and negative budget tests remain intact.

Aggregate query measurements and plans are retained privately in `library-status-query-audit-20261027.json` and `library-status-query-benchmark-20261027.json`. This document contains no private source paths, record identifiers or body content.
