# Derived-store custody and sealed publication: decisions (2026-11-10)

Aggregates only; no keys, passphrases or hostnames. The owner delegated these decisions
("everything has full approval"); each is derived from the registries and records cited.

## Derived stores and library generations (issue #107)

Decision: **do not copy the regenerable derived stores offsite.** Adopt the recommendation in the
2026-11-05 measured-scope note on #107.

- Irreplaceable inputs already have independent copies: raw provider exports (internal disk plus
  Time Machine, restore tested 2026-10-31) and the published library database (encrypted offsite
  copy, 20 of 20 parts hash-identical, decryption proven end to end 2026-11-05).
- The archive tree is derived: replaying the engine reader over the capture plus export reproduced
  the previous store exactly (2,898 of 2,898 row digests). Library artifacts are exported from the
  database. A from-scratch full rebuild was not performed; "regenerable" rests on replay evidence.
- Spending roughly 70-110 GiB of bandwidth on a hotspot-constrained link for data that rebuilds in
  hours is not justified. Retained previous trees (pre-restore rollback tree, older library
  generations) are rollback holdings: keep on the working disk, reap per the headroom plan once the
  published generation is verified; they are not custody obligations.
- Reversal condition: if a source behind a retained revision is confirmed gone and not in the
  database copy, copy that single generation with the same encrypted, quota-gated method.

## Sealed (encrypted) publication, issue #129

Status observed 2026-11-10 (not a claim of acceptance):

- Format: cce-sealed.v2 packing merged (#150); synthetic-identity tests only in that change.
- A real-data attempt exists in the KNOVV Cloudflare lane (staging directory of 2026-11-08): its
  run receipt records `production_accepted: false`, `uploaded: false`; the export stage failed at
  the 7,200 s migration limit. No completed real publication exists, so **no real-data restore in
  an independent clone can be verified yet**. That verification stays open and is gated on a
  completed export, not on a decision.
- Enrollment decision: production enrollment uses the identities generated for that lane
  (one publisher, desktop and phone recipients, plus an offline recovery recipient required by
  `docs/encrypted-publication.md`). Their key files and unlock material stay outside Git and outside
  any agent-readable shared path; moving them into the owner's password manager is the custody
  step, and no agent copies them into chat, CI secrets or a repository.
- Key decision: new identities require a passphrase of at least 12 bytes, generated at random and
  held in a single password-manager item. Losing all enrolled keys loses only the offsite copy, never
  the originals.
- Remote selection: the offsite remote for the library database is the existing encrypted Drive
  overlay (shared rclone client, retiring this year, so it is not used for new bulk copies). For
  sealed git publication the destination is a private repository or the Cloudflare account already
  used by the KNOVV lane; the choice is made by the lane that owns the publication (PR #155) and is
  verified for identity, policy and measured capacity before any push, per the sealed-publication
  procedure.
- Acceptance gate (unchanged): canary publish and unseal bit-identical with the right key, failure
  with a wrong key or app identity, then real shards in bounded batches, then restore into a fresh
  clone and compare against the manifest.
