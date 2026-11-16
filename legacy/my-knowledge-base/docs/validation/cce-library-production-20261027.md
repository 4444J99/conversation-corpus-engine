# CCE production consumer validation - 2026-10-27

The existing federation provider and indexer consumed one complete, published CCE production generation into a private persistent federation cache. This validation used real production reading artifacts, including both AI and authored material, canonical shared passages, and incomplete-body records.

The tested adapter and its registry, migrations, and logger matched published commit [`fe1ac33c31c0`](https://github.com/4444J99/my-knowledge-base/commit/fe1ac33c31c0e1a17dc54c4d5ef98def46b19149) byte for byte before execution. Runtime: `v26.12.1`. The validation made no changes to CCE publication, source files, or canonical revision history.

| Measurement | Observed |
| --- | ---: |
| Canonical records | 27,343 |
| Shared-passage blocks | 292,624 |
| Initial cache inserts | 319,967 |
| Replayed records skipped unchanged | 319,967 |
| Replay inserts | 0 |
| Reading-note bytes | 5,071,164,156 |
| Persistent SQLite cache bytes | 6,072,045,568 |
| Acceptance elapsed seconds | 303.984 |

| Wing | Records |
| --- | ---: |
| ai | 17,332 |
| authored | 10,011 |

| Source body coverage | Records |
| --- | ---: |
| full | 21,734 |
| metadata_only | 1,470 |
| partial | 4,139 |

Every cached reading note matched the exact byte count and SHA-256 declared by the pinned CCE report. Every record retained its CCE UID, semantic version, normalized digest, body digest, source reference, wing, provider, coverage status, and canonical note URI. Block identities remained `block:DIGEST`. Same-generation replay preserved cache row IDs, hashes, metadata, and indexing timestamps.

A separate scratch cache used the same production generation to exercise rollback. Cancellation after 8 actual insert attempts left 0 persisted documents; the scan recorded status `cancelled` and error code `SCAN_CANCELLED`. The primary cache remained intact. SQLite integrity returned `ok`, and its foreign-key check returned no findings.

The canonical CCE database retained 30,818 revision records. Its complete revision-table digest, table counts, file size, and modification time were unchanged before and after consumption. The publication pointer also remained unchanged. Original payloads and historical revisions remain owned by CCE; the downstream cache stores the current condensed reading view.

Direct SQLite lookup was exercised against the persistent cache:

| Query | Returned, capped at five | Milliseconds |
| --- | ---: | ---: |
| `limen` | 5 | 1.619 |
| `research` | 5 | 9.872 |
| `creative` | 5 | 48.002 |

The invocation used a 1500-second process bound, a 900-second per-scan bound, a 2,048 MiB Node heap bound, a 16 GiB aggregate read bound, and a 512 MiB per-note bound. Its cache allocation was estimated before ingestion and required at least 2 GiB of remaining host headroom. Originals and failure evidence were preserved.

The publication report and export manifest measured 68,059,044 and 52,224,135 bytes respectively; the configured metadata limit was 134,217,728 bytes. The child process used 120.504 user CPU seconds and 75.195 system CPU seconds, with a maximum resident size of 1,390,346,240 bytes.

Production source coverage was reported as `false` and was preserved without upgrading partial or metadata-only records. This establishes provider/indexer and persistent-cache acceptance. It does not establish HTTP API, web or desktop UI, deployed-service, or original-file custody acceptance.

The detailed private verification receipt has SHA-256 `debdad6b7157e5cb867eabd3df5d39615709c29ae2edb0ce34047ae480ca63a4`. It retains exact generation, publication, source, cache, and scan-run evidence. Personal paths, titles, record IDs, and body content are omitted here.
