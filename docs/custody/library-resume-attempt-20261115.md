# Library resume attempt for in-gap exports (sanitized)

This note records a dated, method-focused account of attempts to incorporate a small set of newly found export documents into the published library. It omits private filesystem paths, generation identifiers, hashes, byte sizes, exact timings/counts, and receipt names. Detailed measurements are retained in the owner's private notes.

## Attempt 1: resume a failed candidate

- **Goal**: resume indexing/export work from a previously failed candidate that had ingested the supplemental documents.
- **Method**: run a bounded `cce library build` in resume mode against the failed candidate, with declared limits and controlled resource usage, without modifying the published generation.
- **Outcome**: the resume was refused during qualification because the candidate's recorded publication/base pointer no longer matched the current publication state. No new generation was created and the seed was not modified.
- **Interpretation**: the base-pointer binding acted as a safety check. The cause of the pointer-state difference was investigated separately; the refusal was treated as correct behavior rather than bypassed.

## Diagnosis of the published generation (read-only)

A read-only diagnosis compared published artifacts against recorded metadata and exercised verification/search behavior:
- Core database/content artifacts were found consistent for the checked records, while separate verification issues (such as metadata drift or stray filesystem artifacts) could still cause a full generation verification to fail.
- These observations were treated as distinct from the resume refusal and were not repaired by modifying rollback-held generations.

## Attempt 2: fresh supplemental build

- **Goal**: complete incorporation of the same supplemental documents via a fresh bounded build (no resume) when the prior seed was unusable.
- **Method**: run a bounded build with measured limits and controlled logging, verify the resulting candidate, and publish by moving the pointer only after verification succeeded. A rollback reference to the prior publication state was retained privately.
- **Outcome**: the fresh build and publish completed successfully for the supplemental documents, and subsequent search confirmed the added material was available in the published library.
- **Follow-up**: failed/intermediate candidates and any unattributed publication-state drift were retained for later diagnosis and cleanup according to storage/headroom policy. Rollback-held generations were left unmodified.
