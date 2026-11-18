# Bounded knowledge archive adoption: sanitized acceptance record

This public record summarizes the methods, outcomes, and caveats of a bounded knowledge-archive adoption effort. It omits real library figures, receipt names, private paths, device identifiers, and other sensitive measurements; detailed evidence is retained in the owner's private notes.

Status terms: **merged** = on the main branch; **published** = a store pointer or selected name changed under the owning builder's verification; **verified** = independent read-only checks reproduced the claim.

## Acceptance summary

| Criterion | Status (summary) |
|---|---|
| Independent copy of raw AI exports identified, integrity verified, representative restore | **Pass.** Additional copies on physically independent storage were verified by hash/read-back checks and representative restore tests. |
| Earlier claim that a same-volume directory was an independent backup | **Withdrawn.** It was on the same physical device and lacked restore evidence. |
| Library database independently copied (encrypted, offsite) | **In progress, externally constrained.** Staging and partial transfer occurred under transfer/quota constraints; decryptability required owner-held material and remained separately verifiable. |
| Other derived stores independently copied | **Not started.** Destination/approval decision required. |
| Record-level reconciliation of absent/changed source records | **Pass.** Differences were traced to a specific capture source and reconciled by replaying engine readers against original sources; reconstructed candidates with data loss were rejected. |
| Missing material restored into a retained candidate, labeled honestly, verified before publication | **Pass.** Candidate built from original sources, independently verified, promoted by controlled exchange, and prior state retained. |
| Indexing/build diagnosis and refresh completed, verified, published | **Pass.** Long-running phases were diagnosed with evidence; failures due to output limits were resolved via documented resume/publish flows. Coverage/completeness limits remained visible where sources were unavailable. |
| Search latency cause found; improvement with identical results measured | **Pass; merged.** Read-only command overhead was removed and query behavior improved while preserving result correctness in tested cases. |
| Generation-bound search index | **Not delivered, by measurement.** Evaluated approach did not meet correctness/performance/invalidation requirements. |
| Fresh-session recall across agents | **Partial.** Results varied by agent, environment, and availability; some runs were blocked by quotas/credentials or environment constraints. No fabrication was found in graded answers. |
| Interactive reading-surface navigation (Obsidian) | **Unverified.** GUI observation was incomplete. |
| Static link audit of reading copy | **Pass with defects.** Most internal links resolved; specific navigation/packaging defects were identified for follow-up. |
| Symlink/test-environment failure accounted for and fixed | **Pass; merged.** Test isolation was corrected. |
| Main CI green | **Pass** at the time of the original record (subject to later changes). |

## Method and caveats

- **ChatGPT reconciliation**: compared selected archive state against source captures/exports, replayed readers to reproduce prior state, rejected lossy reconstructions, and required provenance labeling in receipts (with engine limitations noted separately).
- **Library refresh**: distinguished stalls from long silent phases using progress evidence; used export/resume modes with adequate limits; treated findings/coverage reductions as properties of reader/source sets rather than automatic repair.
- **Search latency**: profiled read-only operations, removed unnecessary custody scans, and validated correctness with differential comparisons; rejected an alternative index design that regressed semantics or freshness guarantees.
- **Reading copy audit**: performed static link and pagination checks over the generated reading copy; identified unresolved generated links and packaging defects without claiming native-app acceptance.
- **Fresh-session recall**: ran isolated headless sessions with controlled access to stores/evidence, graded answers against pre-registered criteria, and documented environment/simulation limitations (including deny-rule and store-unavailability modeling caveats).

## Open items (general)

- Complete and verify independent encrypted/offsite copies where pending, with owner-held decrypt material.
- Improve agent discovery/UX for default store paths and multi-word search latency.
- Address provider credential/quota availability for evaluation environments.
- Resolve engine issues around provenance/invocation recording, reading-copy packaging, and disposition of uncommitted external edits.
- Harden recall-test design for agents lacking deny controls and for true store-unavailability scenarios.
