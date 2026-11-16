# Library resume attempt for the 8 in-gap exports (2026-11-15)

Counts, hashes and timestamps only. Follows [history-export coverage](history-export-coverage-20261115.md).
Owner registry row: IRF-CCE-043 (organvm-corpvs-testamentvm PR 642).

Times without a zone below are the host's local time, five hours behind UTC (11:25Z is 06:25 local).

## Attempt 1: `resume_generation` on the failed candidate (refused)

- Candidate: `generation-20261115T112506Z-6a27693ffcf7`, state `failed`, phase `indexing`, 8 accepted
  observations, seed `library.sqlite` 13.3 GB (the earlier note said about 10 GiB).
- Command: `cce library build` with a manifest naming only the supplemental documents source,
  `resume_generation` = the candidate, `limits.max_seconds` 14400, `max_output_bytes` 25769803776,
  under `taskpolicy -b`, `nice -n 15`, a 15000 s alarm, log files in the custody staging root.
  Free space 160 GiB; host load about 3.
- Result after 573.6 s (`qualify_resume` stage, which hashes and verifies the 13 GB seed):
  `library: resume base pointer differs from current publication`. No new generation directory
  was created and the seed was not modified (its mtime is unchanged at 09:30).
- Cause: the candidate recorded the publication pointer it was built on. The published pointer
  names the same generation (`20261031T162115Z-614ce4c057f6`), `published_at` and
  `source_manifest_hash`, but three fingerprints differ.

| Pointer field | Recorded in the candidate (built 06:25) | Current `current.json` (rewritten 08:32) |
|---|---|---|
| `record_fingerprint` | `078458e2...` | `baf1153b...` |
| `evidence_fingerprint` | `46602d14...` | `12f1f564...` |
| `report_sha256` | `aa429f79...` | `46630475...` |

  Published generation files were rewritten in place that morning: `library.sqlite` 08:22,
  `build-state.json` 08:31, `report.json` and `current.json` 08:32. The writer is not attributed
  (no transcript search hit; no cce process other than `cce-mcp` was running). The base-pointer
  binding is a safety check and was not bypassed; the seed's receipt was not edited.

## Diagnosis of the published generation (read-only)

A script re-checked every artifact in `report.json` of the published generation against its recorded
size and SHA-256 (not a replacement for `cce library verify`; it is the same comparison, listing
every failure instead of stopping at the first):

- 320,375 artifacts match, including `library.sqlite` (the database the pointer selects) and all vault blocks.
- `Library.verify(full=False)` on the published database reports no problems.
- Two things make `cce library verify` of the published generation fail:
  - `agent-entry.md` differs from its recorded hash (file mtime 2026-11-01 11:51, after publication on 2026-10-31).
  - Finder droppings `.DS_Store` and `vault/.DS_Store` are present (`candidate contains unexpected file`).
  Both predate the 08:22 writes, so neither explains the pointer change.
- Search still works: record count 27,382 and 1,628,594 blocks are unchanged.

A `.DS_Store` placed in any candidate generation directory also fails `publish`; keep Finder away
from `library-all/generations/`.

## Attempt 2: one fresh supplemental build (diagnosed retry)

The seed is unusable (its base moved). The documented alternative is a build with the same documents
manifest and no `resume_generation`, with the measured limits (`max_seconds` 14400,
`max_output_bytes` 25769803776), under `taskpolicy -c utility`, `nice -n 10`, a 15000 s alarm and
bounded logs.

## Outcome of attempt 2

Result (2026-11-15, one run, no second attempt needed):

- Candidate `generation-20261115T151135Z-fc76b8f1bdb1`: 27,390 records (27,382 base plus the 8
  documents), 1,629,052 blocks, 0 findings, `coverage_complete` false (inherited from the base).
  Stage clocks: `index_blocks` began at 2,393 s and ended at 5,866 s, `export` began at 6,378 s,
  `verify_export` at 11,889 s, `artifacts` at 13,696 s, finished at 13,930 s of the 14,400 s limit
  (470 s of margin). Artifact bytes 17,241,454,789 against the 25,769,803,776 limit.
- The build's own verification passed. `cce library publish GENERATION_PATH` then verified the
  candidate and moved the pointer (`changed: true`, `published_at` 2026-11-15T19:18:37Z).
- Rollback pointer (the whole `current.json` before publish; hashes and names only):

  ```json
  {"coverage_complete": false,
   "evidence_fingerprint": "12f1f564b18c4d4b9fa2f177d4f3693f4b39af7e978bd675bf4b795788fc043c",
   "generation": "generations/generation-20261031T162115Z-614ce4c057f6",
   "published_at": "2026-10-31T20:47:59.777378+00:00",
   "record_fingerprint": "baf1153b4107bf626c27f490d7ac6c32df0f173eb182d4a5bb583a95b5334e77",
   "report_sha256": "4663047580584a5ab2e29275eb1d8f722dfd7c978524900038b4a02e59496ecb",
   "schema": "cce.library-current.v1",
   "source_manifest_hash": "29f76de0585c36d9380b4f8d5e0f20dfcc481df43fbc25e312afc719c61140b9"}
  ```

  A copy is in the custody staging root (`cce-resume-20261115/rollback-current-pre-publish.json`,
  file SHA-256 `3524767fa3ec8d01d888ed322504942432579da3e4715336696625cfe58d68e3`). The previous
  generation was not modified by this work: its `library.sqlite` mtime is still 08:22:04 (written
  before this run started) and it has no `-wal`, `-shm` or `-journal` file. Rollback is a manual
  restore of that file over `library-all/current.json` (take `publication.lock`; write a temporary file in the
  same directory, then rename it so readers never see a partial file; restore from the staged copy and check
  its SHA-256, using the inline JSON above, which is a key-sorted rendering, only as the fallback), not
  `cce library publish`: publish verifies the generation first, and the previous generation fails
  `verify_candidate` (see the diagnosis above), so the tool would refuse it.
- Searchability: `cce library search` against the new generation returns the 8 staged exports
  as `documents` records (wing `ai`, lane `support`, kind `document`), found by content terms (7
  of 8) and by title (the 8th). Document records rose from 8,788 to 8,796.
- Not done: the failed seed `generation-20261115T112506Z-6a27693ffcf7` (13.3 GB) stays on disk as
  a rollback/diagnostic holding; reap it per the headroom plan. The change of three pointer fingerprints (writes at
  08:22 to 08:32) on the previous generation remains unattributed. Its `agent-entry.md` hash
  mismatch and two `.DS_Store` files are unrepaired (a generation held as rollback should not be
  edited here).
- Host: load average between 2.7 and 5.7 at the points observed; free space 160 GiB before and 138 GiB at a mid-export observation.
