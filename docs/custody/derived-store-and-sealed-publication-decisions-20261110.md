# Derived-store custody and sealed publication: decisions (sanitized)

Aggregates only; no keys, passphrases, hostnames, private paths, receipt names, or real-library figures. Decisions are summarized at a method/policy level; detailed measurements and receipts are retained in the owner's private notes.

## Derived stores and library generations

**Decision:** do not copy regenerable derived stores offsite by default.

- Irreplaceable inputs (raw provider exports and the published library database) should have independent, verified copies using the approved custody method.
- Archive and library derivative artifacts are generally regenerable from retained inputs and the database; copy decisions should weigh rebuild cost against bandwidth/storage constraints.
- Retained previous trees/generations are rollback holdings on working storage and are reaped according to headroom policy once the published generation is verified; they are not custody obligations by default.
- **Reversal condition:** if a source behind a retained revision is confirmed missing and not present in the database copy, copy that specific generation using the same encrypted, quota-aware method.

## Sealed (encrypted) publication

**Status (dated observation, not acceptance):** sealed publication tooling and format work exist, but a completed real-data publication and independent-clone restore verification had not been demonstrated at the time of this record.

- A real-data publication attempt in a separate lane did not complete within its execution limits and did not produce an accepted uploaded publication. Independent restore verification remains open and gated on a completed export/publication.
- **Enrollment/custody principles:** production enrollment uses separately held publisher, device, and offline recovery identities/keys as required by procedure. Key material and unlock secrets stay outside Git and outside agent-readable shared paths, with split custody so compromise of one location does not decrypt the corpus.
- **Key handling:** new identities require strong randomly generated secrets held apart from key files. Loss of all enrolled keys affects only the offsite copy, not the originals.
- **Remote selection:** destination choice for encrypted publication is made by the owning lane, with identity, policy, and capacity verified before any push according to the sealed-publication procedure.
- **Acceptance gate (unchanged):** canary publish/unseal verifies bit-identical restore with the correct key and failure with a wrong key or app identity; only then proceed with bounded real-data publication and an independent-clone restore compared against the manifest.
