# Production CCE provider validation - 2026-10-27

The real CCE federation provider streamed the complete published production reading surface without allocating a second persistent database. Every record and shared-passage note was read and checked against its pinned publication evidence.

The provider and supporting source files matched published commit [`fe1ac33c31c0`](https://github.com/4444J99/my-knowledge-base/commit/fe1ac33c31c0e1a17dc54c4d5ef98def46b19149) byte for byte. The observed runtime was `v26.12.1`.

| Measurement | Observed |
| --- | ---: |
| Canonical records | 27,343 |
| Shared-passage notes | 292,624 |
| Total provider documents | 319,967 |
| Reading-note bytes verified | 5,071,164,156 |
| Largest note bytes | 47,171,302 |
| Elapsed seconds | 107.472 |

| Wing | Records |
| --- | ---: |
| ai | 17,332 |
| authored | 10,011 |

| Source body coverage | Records |
| --- | ---: |
| full | 21,734 |
| metadata_only | 1,470 |
| partial | 4,139 |

Each note's exact SHA-256 and byte count matched the canonical artifact inventory. All record UIDs, semantic versions, payload and body digests, source references, provider/kind/wing/lane classifications, body-coverage labels, and message counts matched the canonical CCE database. Shared blocks retained their exact `block:DIGEST` identities. There were no duplicate or omitted provider documents.

The complete canonical revision-table digest and table counts were unchanged before and after the read, including 30,818 revision records. The SQLite file size and modification time, and the publication pointer, also remained unchanged.

The run enforced a 1000-second process bound, a 900-second provider bound, a 2,048 MiB Node heap bound, a 16 GiB aggregate read bound, and a 512 MiB per-note bound. Bodies were read one note at a time.

The publication report measured 68,059,044 bytes and its export manifest measured 52,224,135 bytes. The per-source metadata limit was 134,217,728 bytes, leaving 66,158,684 bytes above the larger measured file.

Persistent-cache admission was measured separately:

| Capacity measurement | Bytes |
| --- | ---: |
| Estimated federation database, including overhead | 6,860,948,859 |
| Available internal space after provider verification | 13,977,006,080 |
| Required remaining host headroom | 2,147,483,648 |

The measured internal capacity predicate was `true`. The existing federation cache schema stores text directly; this estimate adds no FTS expansion. This invocation created no persistent cache and changed no cache configuration.

Production source coverage remained `false`. Partial and metadata-only records were retained with their original coverage labels. Whole-provider acceptance is established; persistent-cache, HTTP API, web or desktop UI, deployed-application, and original-file custody acceptance are separate observations.

Private receipt SHA-256: `3426cf4d6f4f40ab3faafb66f458e1a2d6f832890e6b78ccf0d9c4de8167222c`. Its exact paths, generation identifiers, source references, and record-level evidence remain private. No personal titles, IDs, or body text are included in this report.
