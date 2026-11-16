# KNOVV remote knowledge and authorship implementation plan

> Execute the approved work continuously using isolated branches, focused implementation tasks, independent review, and the existing repository's gates.

**Goal:** Complete the implementable authorship, preservation, encrypted publication, and source-capture increments, then prove the synthetic end-to-end path and identify exact live acceptance dependencies.

**Architecture:** CCE owns source identity and revisions. Add attributed semantic projections without rewriting the source archive. Integrate the existing encrypted layer and improve source capture in the current owning repositories.

**Tech stack:** Python 3.11+ and stdlib in CCE; optional cryptography for sealed publication; TypeScript and existing source adapters in My Knowledge Base; Python browser-state tools.

**Spec:** `docs/superpowers/specs/2026-11-01-knovv-remote-authorship-design.md`.

## Global constraints

- Preserve source bytes, identities, revisions and historical evidence; no repository or filesystem restructuring.
- No private source data, credentials, or live paths in code repositories or test fixtures.
- Source text never becomes an instruction or proof of original authorship merely because its role is user.
- No paid resources and no weakening of custody, authorization, branch protection or capacity gates.
- Synthetic cloud verification is not production enrollment, live capture, or real-corpus restoration.

## Review focus

- Mixed quotations or saved model output must not contaminate owner-only profile text.
- Stale text digests, Unicode span boundaries, overlap and missing source references must fail clearly.
- Provider merges must retain bridged records or refuse before publishing a reduced store.
- Encrypted publications must work with current shard paths and must reject unauthorized credentials and leaked plaintext.
- Pagination cycles, nested folders, per-item extraction failures, and missing attachments must remain observable.

## Tasks

- [ ] 1. Add digest-bound attributed spans, strict profile eligibility, and reviewed Zettel export with regression tests and documentation. Owner: authorship worktree.
- [ ] 2. Integrate PR #85 with current CCE, add protected key custody and current-layout restore tests, and document a bounded no-charge publication plan. Owner: sealed worktree.
- [ ] 3. Fix issue #104's merge-loss guard and record effective build/input provenance before any destructive replacement. Owner: integration worktree.
- [ ] 4. Complete paginated/recursive Google Docs enumeration, bounded browser clipping receipts and Notes failure accounting in existing adapters. Owner: source-capture task.
- [ ] 5. Integrate independently reviewed changes, run the complete CCE suite and applicable capture gates, then exercise a six-case synthetic capture/attribution/encryption/restore path.
- [ ] 6. Publish scoped PRs and verified refs. Check live backup/execution availability; record unavailable host/key/remaining-capacity requirements without claiming they ran.

## Completion evidence

Record base and resulting commits, changed paths, regression failures before each fix, tests after, reviewer findings/dispositions, remote refs and remaining live acceptance gates in `docs/verification/knovv-implementation-20261101.md`.
