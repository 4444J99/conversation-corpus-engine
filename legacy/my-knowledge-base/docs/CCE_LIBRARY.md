# CCE knowledge library source

The existing federation API can read a published Conversation Corpus Engine
(CCE) library. CCE remains the owner of record identities, normalized revisions,
original-source provenance, repeated passages, and generation publication.
My Knowledge Base caches the verified reading notes in its existing
`federated_documents` table. This adds no identity database or ingestion authority.

Register a source through `POST /api/federation/sources` using the registered CCE
store root as `rootPath`. The following is the **tested production profile** for
the 2026-10-27 library, whose 68,059,044-byte publication report exceeds the
general 64 MiB metadata default. Existing API authentication and
`FEDERATION_ALLOWED_ROOTS` restrictions still apply.

```json
{
  "name": "CCE knowledge library",
  "kind": "local-filesystem",
  "rootPath": "/absolute/path/to/registered-cce-store",
  "metadata": {
    "format": "cce.library-index.v1",
    "maxRecords": 100000,
    "maxBlocks": 1000000,
    "maxFileSizeBytes": 536870912,
    "maxMetadataBytes": 134217728,
    "maxIndexBytes": 134217728,
    "maxIndexRowBytes": 65536,
    "maxTotalBytes": 17179869184,
    "maxSeconds": 900
  }
}
```

These limits are configured per source; the example does not change general
defaults. Unconfigured sources retain 128 MiB per note, 64 MiB metadata, 8 GiB
total reads and 300 seconds per scan. All limits must be positive safe integers.
Measure each new publication before choosing its profile; the tested profile
retains 100,000 records, 1,000,000 blocks and the other listed index bounds.
See the [whole-provider validation](validation/cce-library-provider-production-20261027.md)
and [persistent-cache validation](validation/cce-library-production-20261027.md)
for exact production counts, resource measurements, replay and rollback evidence.
These establish native provider/indexer acceptance; HTTP and web deployment
were not tested. A limit, malformed record, missing file, changed source,
or failed hash check fails the scan visibly; records are never silently omitted
to fit a limit. Include/exclude globs do not select CCE membership: its pinned
export manifest and compact index own that membership.

The provider indexes the legacy per-record and shared-passage notes, so it scans
only generations with a v1 export manifest. A generation built with
`cce library build --vault-profile none|cards|full` (the CCE default is `none`)
has a v2 manifest and no shared-passage notes; the scan then fails with a message
naming `--vault-profile` instead of indexing a partial set.

Enqueue a scan through `POST /api/federation/sources/:id/scan` as usual. Both
`incremental` and `full` modes read the complete published CCE record set.
CCE scans retain previously cached identities that are absent from a later
generation and report `retainedCount`; they never delete those identities.
Ordinary filesystem sources retain their existing scan behavior.

```text
STORE/library-all/current.json
  -> generation/report.json (publication SHA-256)
  -> library-index.jsonl + export-manifest.json (artifact SHA-256)
  -> vault/records/UID.md + vault/blocks/DIGEST.md (artifact SHA-256)
  -> existing federated_documents + scan-run evidence
```

| CCE authority | Federation representation |
| --- | --- |
| Stable record UID | Exact `external_id`, unique within the registered source |
| Canonical graph block ID | Exact `block:DIGEST` external ID |
| Relative canonical note path | `path`, preserved across generations |
| Immutable generation and note | `canonicalVaultPath` and `canonicalVaultUri` metadata |
| Revision version and normalized payload digest | `cceVersion`, `cceDigest` |
| Normalized message-body digest | `cceContentDigest` |
| Source reference | `cceSourceRef`, retained as data without following it |
| Reading-note bytes | `cceArtifactSha256`; distinct from original-source hashes |
| Publication and index evidence | `cceReportSha256`, `cceIndexSha256` |
| Wing, lane, provider, kind, body coverage | Corresponding `cce*` metadata fields |

Search with the existing `GET /api/federation/search?q=...&sourceId=...` endpoint.
Both AI and authored records remain visible, including metadata-only records.
Shared prose is searchable once as its canonical block note, whose contents
include backlinks to original occurrences. The cache is the condensed reading
surface; CCE's bounded `library search`, `read`, and `history` commands remain the
authority for original message search and retained revisions.

The adapter streams the index and reads one bounded note at a time inside the
existing SQLite transaction. It validates stable IDs, contained relative paths,
symlink absence, publication/report/index hashes, inventory counts, and each note's
exact size and hash. It checks the publication pointer again before commit. A
later artifact failure rolls back earlier cache updates from that scan. A replay
of the same generation preserves row IDs and reports unchanged records as skipped.

Notes and source instructions are historical evidence, never executable input.
The adapter performs no upload, network fetch, source deletion, note-link traversal,
or mutation of the CCE store. Raw provider exports and original authored documents
remain under their existing custody and access rules.

Focused validation:

```sh
node node_modules/vitest/vitest.mjs run src/federation/cce-library-provider.test.ts
```
