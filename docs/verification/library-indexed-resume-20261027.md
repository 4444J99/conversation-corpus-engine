# Completed-index recovery verification (sanitized)

This page describes the library builder's recovery modes and the verification performed for them. It omits private paths, exact byte measurements, and host-specific receipt details.

## Resume modes

The library builder accepts explicit `resume_mode` values in the source manifest:

| Mode | Required failed-seed evidence | New sources | Database work |
| --- | --- | --- | --- |
| `ingested` (default) | Completed source counts and full normalized-object verification | Allowed | Existing compact copy and full index rebuild |
| `export` | Failed export/verification phase with matching fingerprints and verification evidence | Rejected | Open seed read-only; reuse index and regenerate exports |
| `indexed` | Same completed-index proof as `export` | Allowed within normal limits | Clone/copy writable; reindex only changed current records |

All modes require an absolute `resume_generation` under the configured library store. Resume never modifies the failed generation or publishes automatically. Each new generation retains its source manifest, qualified seed receipt, inherited findings, prior source report, and observation counts.

## Example recovery manifest

```json
{
  "schema": "cce.library-sources.v1",
  "resume_generation": "/path/to/registered/store/library-all/generations/<failed-generation>",
  "resume_mode": "indexed",
  "archives": ["/path/to/registered/supplement"],
  "limits": {
    "max_records": 100000,
    "max_seconds": 3600,
    "max_output_bytes": 17179869184
  }
}
```

Use the normal `cce library build --manifest ...` command with a manifest like this. Publication still requires `cce library publish` after completed export verification.

## Behavior and checks

- On supported filesystems, recovery uses copy-on-write cloning when available; otherwise it falls back to bounded copying with appropriate admission checks and free-space monitoring.
- Output budgets and free-space floors remain enforced; logical output caps are unchanged.
- Incremental indexing retains unchanged occurrences, rebuilds changed records in a savepoint, and removes derived blocks with no current occurrences while preserving source objects, historical revisions, and provenance.
- Verification covered targeted store/build/resume tests, lint/format checks, and synthetic clone-capability validation on the custody filesystem. These results validate the recovery mechanism; they do not by themselves establish production publication.
