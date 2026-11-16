# Completed-index recovery verification - 2026-10-27

The library builder accepts explicit `resume_mode` values in the source manifest:

| Mode | Required failed-seed evidence | New sources | Database work |
| --- | --- | --- | --- |
| `ingested` (default) | Completed source counts and full normalized-object verification | Allowed | Existing compact copy and full index rebuild |
| `export` | Failed `exporting`/`verifying` phase, matching status and record/evidence fingerprints, full verification | Rejected | Clone/copy opened read-only; reuse index and regenerate exports |
| `indexed` | Same completed-index proof as `export` | Allowed within normal record/byte/deadline limits | Clone/copy writable; reindex only UIDs whose current records changed |

All modes require an absolute `resume_generation` under the configured library store. Resume never modifies the failed generation or publishes automatically. Every new generation retains its source manifest, qualified seed receipt, inherited findings, prior source report, baseline observation count and new observation count.

Example recovery manifest (replace explicit paths with registered paths):

```json
{
  "schema": "cce.library-sources.v1",
  "resume_generation": "/registered/store/library-all/generations/generation-failed",
  "resume_mode": "indexed",
  "archives": ["/registered/supplement"],
  "limits": {
    "max_records": 100000,
    "max_seconds": 3600,
    "max_output_bytes": 17179869184
  }
}
```

The normal `cce library build --manifest ...` command consumes this manifest. Publication still requires `cce library publish` after completed export verification.

On macOS, recovery uses the exclusive native `clonefile` operation. COW admission requires `max_output_bytes - seed_database_bytes + mutation_reserve + 1 GiB`, with a 1 GiB mutation reserve for `indexed` mode and zero for read-only `export`. Actual free space is monitored through ingestion SQL, indexing and export callbacks. The full logical output cap remains unchanged. If the filesystem cannot clone, an ordinary bounded copy requires the entire output budget plus the 1 GiB floor. A failed clone never overwrites an existing destination.

The receipt reports copy method, free bytes before/after cloning, filesystem-reported allocated bytes, admission reserve, source hashes before/after copying, and the copied database hash before any supplemental changes. Allocated blocks on APFS can include shared extents; that number is not a unique-physical-byte measurement.

Incremental indexing retains unchanged occurrences, rebuilds changed UIDs in a savepoint, and removes only derived blocks with no current occurrences. Source objects, historical revisions and provenance remain intact. Its regression test compares the resulting blocks, occurrences and status with a full rebuild, and checks rollback after interruption.

Validation: 61 targeted store/build/resume tests passed. Scoped Ruff lint and formatting checks passed. A synthetic 330,000-byte clone on the custody filesystem returned `copy_on_write`, with identical source-before/source-after/candidate SHA-256 and unchanged measured free space (15,688,122,368 bytes before and after). This validates the recovery mechanism and host clone capability; it does not itself establish production publication.
