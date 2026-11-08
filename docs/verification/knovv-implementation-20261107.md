# KNOVV application implementation and acceptance

The implementation is on `codex/knovv-cloudflare-20261107`, draft PR #155.
It adds the private browser workspace to CCE: wiki/projects/Zettels, source-bound
reading and search, capture, attachments, editing, citations, typed links,
backlinks, retained conflict drafts, revision history and enrolled-device sync.
Cloudflare holds ciphertext and signed opaque journal metadata. Original source
records, accepted library generations and generated agent wiki files remain read-only.

## Validation

- 44 TypeScript/application tests pass across 15 files; the subsequent JWT router
  regression adds one passing authentication test. The integration tests run
  the actual API against local D1/R2 and SQLite Durable Objects and separately enrolled keys. They exercise
  synchronization, offline reopening, retained conflicts, revocation, key rotation,
  exact attachment recovery and attributed agent writes.
- Browser, Worker and Node agent production builds pass. The native Cloudflare
  build and staged deployment with Durable Objects/Assets/R2 pass.
- The full Python regression measured 4,345 passes and two subprocess timing
  failures under host load (Copilot blank spans and sealed FIFO refusal). Both
  pass in an isolated rerun with their original limits unchanged. The latest
  implicated Python bridge suite passes 28 tests.
- Python bridge tests exercise sealed format parity, protected enrollment, source
  citation validation, immutable journal/attachment recovery, grant monotonicity,
  paged snapshot integrity and resumable import.
- Earlier synthetic browser QA observed search, source opening, capture, save,
  lock and reopening. Final visual checks are blocked by the browser's extension
  popup. Physical-phone, alternate-host and Mac-off production checks are pending.

## Single final review and dispositions

One fresh independent review covered base `2f424b9` through implementation `99c4008`.
All thirteen Critical/Important findings were accepted and addressed in one fix pass,
with failing behavior reproduced before the relevant repair:

1. Save completions preserve later editor text and route changes.
2. Revision adoption validates document binding, ancestry, sequence binding and
   monotonic enrollment generations, with serialized checkpoint updates.
3. Plaintext is bounded before compression, so staged revisions remain readable.
4. Lock clears decrypted keys/state even when draft persistence fails.
5. Durable encrypted head metadata survives body eviction; bootstrap catalogs are
   retained separately. Decoded caches have byte budgets.
6. Citation offsets convert browser UTF-16/casefold coordinates to source Unicode
   code points and remain within the cited source span.
7. Evidence selection includes verified synced notes and preserves newer text.
8. Every publication refresh replaces search state and invalidates old results.
9. Canonical authoring imports retain source URLs, typed relationships and metadata.
10. Snapshots page immutable objects and revisions against a fixed cutoff; imports
    resume in bounded batches without a lifetime history ceiling.
11. Enrolled local agents read/write through the same signed CAS journal, declare
    attribution, preserve conflicts and use protected local state and Access auth.
12. Device grant seeding reads back the exact requested state, rejecting stale
    revocations instead of reporting success.
13. History navigation exposes revisions beyond the first fifty with bounded pages.

Two minor findings are deferred: backlinks can remain stale while staying on an
already-open page, and relative Markdown section links lose their section target.
No second reviewer or repeated review gate was used.

## Operational rulings and remaining acceptance

- The account still has ten D1 databases and rejects another creation. This is
  superseded as a deployment blocker by the dedicated SQLite Durable Object
  backend. No existing database was reused/deleted and no paid upgrade was enabled.
- Staging is deployed behind Worker-level Access, with an exact owner allow rule
  and a 24-hour app-scoped publisher service credential. The service identity is
  restricted to owner-signed maintenance requests. Anonymous requests redirect
  to Access; service requests to ordinary data routes are denied.
- Staging seeded 88 synthetic ciphertext objects (371,104 bytes), verified every
  upload by readback and published the head last. An independent cloud snapshot
  verified all 88 objects; the enrolled device restored all eight documents.
  This is synthetic hosted verification, not private-corpus or phone acceptance.
- The static-asset router omits `ctx.access`; the application verifies signed
  Access JWTs against a pinned issuer/audience and trusted JWKS, in addition to
  supporting the platform identity context. Wrong issuer/audience/signatures
  and a service token on ordinary data routes fail in regression coverage.
- Existing Sites and legacy Pages entrypoints remain available during acceptance.
- Revocation protects future epochs; it cannot erase previously decrypted material.
- Existing source adapters remain with their current owners. URL reference capture
  does not claim article extraction or a refresh of Notes/Docs/browser history.
- A first private full-corpus export exceeded its unchanged publication deadline
  after 849 seconds and published no head. Partial ciphertext is retained privately.
  Token indexing now uses sequential inserts followed by one ordered index build;
  new search catalogs use finer hash shards and shared-title ranges. The later
  full-source export hit its unchanged 3,600-second default deadline after about
  6.7 million packed values. It published no head and uploaded no private content.
  Its partial ciphertext and failure receipt remain private; the cold-export
  performance predicate is not passed. Profiling continues before another run.
- Three-second Zettel subprocess limits remain unchanged. Shared lexical path checks
  were extracted from a heavyweight export import; the implicated 95-test suite
  passes, including interruption and FIFO refusal checks.
- Deployed runtime, independent encrypted recovery, physical-device acceptance and
  measured real-data search performance are separate remaining predicates. Local
  checks do not establish them.
