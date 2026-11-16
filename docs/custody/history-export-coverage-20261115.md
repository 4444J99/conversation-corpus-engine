# History-export coverage ledger (2026-11-15)

This page records which existing exports of the owner's ChatGPT, Gemini and Perplexity histories
were found on the host and cloud drives, what date range each covers, and what range is still
missing. It carries counts, dates and path classes only: no titles, file names or conversation ids
(this repository is public; titles stay in the private store).

## Search performed (read-only)

| Place | Method | Result |
|---|---|---|
| `/Volumes/Archive4T` | name search for takeout / gemini / bard / chatgpt / perplexity / export / conversations / MyActivity | Only the 2026-10-16 exports already recorded in [ai-exports custody](ai-exports-custody.md), their duplicate copies under `Archives/` (scratch-drain and t7-absorbed), and the 2026-11-14 `app-cache-*` staging directories ([app-cache](../app-cache.md)). |
| `~/Downloads`, `~/Desktop`, `~/Documents` | content search for exporter signatures (ChatGPT "Exported:" header with a conversation link; Perplexity logo header) | 4 ChatGPT single-conversation Markdown exports (2026-11-08 and 2026-11-09) and 7 Perplexity single-thread Markdown exports (2026-11-09). |
| Google Drive (`rclone gdrive:`, list only, filtered names) | name filter | 2025 Takeout zips (2025-09-28 to 2025-11-18, small), 2025 to 2026-03 per-chat Gemini and ChatGPT document exports. All end before 2026-10-17. |
| Dropbox (`rclone dropbox:`, list only; the connector plugin needed authorization) | name filter, complete (exit 0) | Google Takeout zips dated 2025-09-28/29 (about 20 files, tens of GiB) and older files. All end before 2026-10-17. |

No extracted Takeout (`MyActivity.json`, a `Gemini Apps` folder) exists outside the archived
2026-10-16 export. No application credential, cookie or token store was read or replayed. The Drive and Dropbox listings used the already-configured rclone remotes (list only; nothing downloaded), which authenticate with their stored tokens.

## Coverage per source after this change

| Source | Archive (cce archive-all) | Found after the archive | Ingested now | Still missing |
|---|---|---|---|---|
| ChatGPT | through 2026-10-16/17 (full export plus 2026-10-17 delta); the app cache adds 8 full bodies up to 2026-11-02 (staged, not yet in a published generation) | 1 conversation, created 2026-10-24, updated 2026-11-08 (Markdown). The other 3 Markdown exports are older conversations already in the archive. | the 1 conversation, as a document record | 2026-10-17 to 2026-11-15, except that one conversation. Needs a new ChatGPT data export. |
| Perplexity | through 2026-10-16 (official export) | 7 threads from the same Space, exported 2026-11-09 (the thread's title line is the owner's typed prompt; the answers are Perplexity's) | the 7 threads, as document records | 2026-10-17 to 2026-11-15, except those 7 threads. The Nov 10-11 typed prompts are not in any found file. Needs a new Perplexity export. |
| Gemini | through 2026-10-16 (Takeout, My Activity > Gemini Apps) | none. The app store lists 36 chats after 2026-10-16 (metadata only, bodies encrypted and not opened). | nothing | 2026-10-17 to 2026-11-15. Needs a new Google Takeout (My Activity > Gemini Apps, JSON). |

The Google Takeout zips from 2025 (Drive and Dropbox) predate the archived 2026-10-16 Takeout,
which is cumulative for My Activity, so they add no range to the archive. They were listed, not
opened.

## How the 8 in-gap files were ingested (3 older ChatGPT exports skipped: already archived)

No archive reader exists for single-conversation Markdown exports, so the normal bounded route is a
`documents` source in a library build manifest (`wing: ai`, `lane: support`, `kind: document`, so
no authorship credit is given). The files were copied (hashes recorded) into a 0700 directory under
the custody staging root, and a candidate generation was built with `max_seconds` 3000, under
`nice` and `timeout`, against the registered store; the published pointer and the existing
generation were not touched.

### Outcome of the bounded build (2026-11-15)

- Ingest phase: 8 observations accepted (the 8 files), no failures.
- The `index_blocks` stage then hit the manifest's 3000-second deadline (`library execution deadline
  exceeded`, observed at 1320 s of its own stage clock when the last progress line printed; the run
  was stopped by the library's deadline, not by `timeout`). The candidate generation
  (`generation-20261115T112506Z-6a27693ffcf7`, about 10 GiB, phase `ingested`) is a failed
  candidate: it was not verified, not published, and `library-all/current.json` still points at
  generation `20261031T162115Z`. Nothing in v1 was modified.
- Why: a build over the 27,382-record base re-indexes the block graph (1.6 million blocks), which
  took 6,875 s on the last full build. A supplemental build with 8 small documents does not escape
  that cost. The 8 files are therefore **not yet searchable** (as of this entry; now searchable, see the update below) in the published library.
- Superseded: the 8 files are now published (see the update below). Do not run the resume step that follows; it was refused on a moved publication pointer and a fresh bounded build was published instead.
- To finish (superseded, kept as history): run the documented resume (`resume_generation` pointing at the failed candidate, declaring only the supplemental documents source, with `limits.max_seconds` of at least 14400 and `limits.max_output_bytes` of 25769803776, because the omitted default of 8 GiB is below the 9.57 GiB base database) as a scheduled or owner-started job, then `cce library publish GENERATION_PATH` with the generation path the build prints (publish verifies the candidate before the pointer moves). The staged inputs and
  their SHA-256 sums are in the custody staging root under `history-exports-find-20261115/`.


Document records do not carry `chatgpt-<id>` or Perplexity uids, so `cce app-cache gap` still
counts those conversations as missing from the provider archive; once the library generation is published, search finds them.

Update: the 8 files are now in the published library; see [library resume attempt](library-resume-attempt-20261115.md). That note corrects the failed candidate to phase `indexing` (per its build-state.json) and 13.3 GB.
