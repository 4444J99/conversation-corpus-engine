# KNOVV Cloudflare implementation

Goal: a private, browser-decrypted wiki with source reading, Zettels, capture, editing,
cross-device synchronization and encrypted recovery, built on CCE's canonical identities.

Approved design: the November 7 KNOVV application plan in the owning Codex chat.
Code and synthetic fixtures are public; private content and deployment receipts stay outside Git.

## Tasks and checks

- [ ] 1. Encryption and contracts: interoperate with CCE sealed v1/v2; protected device keys,
  signed enrollment and revision contracts. Test wrong keys, tampering, bounds and Python parity.
- [ ] 2. Cloudflare API: Access authentication, R2 immutable objects, D1 atomic revision journal,
  device enrollment/revocation, cursor sync, conflict and idempotency behavior. Real local D1/R2 tests.
- [ ] 3. Browser workspace: wiki, atomic notes, source citations, backlinks, history, encrypted cache,
  search worker, capture/editor, conflict recovery and two-device synchronization. Build/typecheck/UI QA.
- [ ] 4. CCE bridge: read-only canonical export, sealed publication, journal import and recovery;
  maintain attribution, source revisions and capture receipts. Synthetic and private real-data validation.
- [ ] 5. Deployment: inspect account capacity, provision scoped resources, protect all routes,
  deploy exact build, independently exercise runtime and restore; publish code through a PR.

## Acceptance

An enrolled device can find actual source passages, read wiki and Zettels, follow exact citations,
capture and edit, reopen, and see another device's changes without the source host running.
Unauthenticated requests, stale saves, revoked devices and invalid signatures fail closed.
Content and keys do not enter hosted plaintext, persisted browser caches, logs or public Git.
Retain originals, existing worktrees, current site and independent backups throughout.

## Execution ledger

- Isolated branch starts at 2f424b9. Existing source checkout and other branches preserved.
- Ruling: native worktree creation did not return after several minutes; use an external sibling
  Git worktree. Reconcile any late native result rather than taking over its checkout.
- Writer lease and host admission unmeasured: retired Limen is not activated. This branch and
  checkout are unique to this chat.
- Ruling: use the current approved design with task-level test cycles; no repeated design gate.
- Encryption, API, browser workspace and CCE bridge are implemented with synthetic coverage.
  The TypeScript suite reports 18 passing tests, including real local D1/R2 and two-device
  synchronization, conflicts, revocation, rotation, offline reopening and exact attachment bytes.
  Python bridge tests cover publication/restore, packed ciphertext reuse, source citation checks,
  protected enrollment, monotonic grant updates and journal/attachment recovery.
- Browser QA previously observed search/open/capture/save/lock/reopen in the synthetic preview.
  Final visual and phone checks are pending: Chrome currently blocks automation while another
  extension UI is open. Do not substitute synthetic checks for real-material acceptance.
- Ruling: pack application objects and shard their encrypted locators after measuring the
  canonical corpus. Per-page padding and a monolithic content map would not scale. Keep the
  default 512 MiB budget, require explicit larger budgets, and fail before a partial head.
  Cost if wrong: cold-read and publication performance need further measured tuning.
- Ruling: preserve the existing Pages application and Sites atlas during the transition.
  The new Worker remains closed without Access identity and an exact pinned audience.
  Cost if wrong: transition retains multiple visible entrypoints until acceptance.
- Production deployment is blocked by the Cloudflare account's D1 database-count limit.
  A dedicated R2 bucket was created; no private publication or credentials were uploaded.
  No other application's database was reused or deleted, and no paid upgrade was enabled.
- Full Python regression, final builds, real-data export/restore and the single fresh branch
  review are tracked in the owning chat and private verification receipt. They remain separate
  from deployment, two physical device acceptance and production performance measurements.


### Review repair and measured validation

The single fresh independent branch review is complete. All thirteen important
findings were accepted, reproduced and repaired in one pass. The durable disposition
ledger is `docs/verification/knovv-implementation-20261107.md`; minor deferred items
and operational acceptance rulings remain visible there.

Current completed checks: 31 application tests, 21 Python bridge tests, the implicated
95-test projection/Zettel suite, production browser/Worker/agent builds, native
Cloudflare build, Ruff/format and the commit secret scan. A final full Python run
and bounded real-corpus export measurement are still in progress; do not infer
production acceptance from these component checks.

Export scratch work now separates the sequential token stream from packed locator
construction. Locators store full binary hashes internally, preserving their full
published identifiers and unchanged cache, plaintext, storage and deadline bounds.
The existing account still has ten D1 databases and no KNOVV database as of the
latest live read. No paid capacity change or private upload has been made.
