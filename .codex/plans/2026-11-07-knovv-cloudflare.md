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
