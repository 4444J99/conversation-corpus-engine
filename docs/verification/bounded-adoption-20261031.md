# Bounded knowledge archive adoption: corrected acceptance record, 2026-10-31 (updated 2026-11-01)

Aggregate measurements only. Private transcripts, titles, record identifiers, store paths and source URLs are
omitted; the private report and evidence live in the registered store. This record corrects an earlier private
adoption report (2026-10-30) whose claims were checked against fresh evidence.

Update 2026-11-01: PRs #100 to #103 and #106 are merged and CI on `main` is green; the recall tests were re-run against the installed runtime (section "Fresh-session recall, round 2"); the open items changed accordingly.

Status words: **merged** = on `origin/main`; **published** = the store pointer or selected name changed and the
owning builder verified it; **verified** = independent read-only checks reproduced the claim.

## Acceptance

| Criterion | Status |
|---|---|
| Independent copy of the raw AI exports identified, integrity verified, representative restore | **Pass.** Internal-disk copy (physically separate from the archive disk) plus a Time Machine copy on a third disk: 17,100 + 1,787 + 1,784 file hashes verified, three zip archives tested, 31-file restore from the copy matched every recorded hash |
| Earlier claim that a same-volume directory was an independent backup | **Withdrawn.** Same physical disk; no restore had been run |
| Library database independently copied (encrypted, offsite) | **In progress, blocked externally.** The published library database is staged as 20 encrypted parts (5.3 GB, each re-hashed against its manifest); 6 of 20 are on the remote. Uploads are throttled by a project-level rate limit on the shared Google client that the transfer tool ships with (tracked privately), so a quota-aware uploader sends parts in the windows that open. Decryptability is not yet proven: it needs the owner's key passphrase |
| Other derived stores independently copied | **Not started.** About 51 GiB; needs an owner decision on a destination (issue #107) |
| Record-level reconciliation of the 7 absent and 12 changed ChatGPT records | **Pass** (below) |
| Missing material restored into a retained candidate, labeled honestly, verified before publication | **Pass.** Candidate built from the original sources, verified by two independent read-only verifiers, promoted by atomic exchange, old tree retained, engine verifier green under the final name |
| `index_blocks` diagnosed with evidence; refresh completed, verified, published | **Pass** (below) |
| Search latency cause found; improvement with identical results measured before/after | **Pass; merged** (PR #102). Installed binary on the live store afterwards: rare term 0.3 s, common term 2.5 s, no-match term 0.9 s, full result pages |
| Generation-bound search index | **Not delivered, by measurement** (below) |
| Fresh-session recall, per agent, from observed traces | **Partial** (round 2, below). Codex 6 pass and 1 partial of 7; Claude Code 6 pass and 1 partial of 7, and a failed simulated-unavailable-store case; Gemini (kernel-isolated) 1 pass, 1 partial, 3 fail and 2 with no answer of 7; Copilot unavailable (monthly quota exceeded, external); OpenCode unavailable (provider key expired) |
| Interactive reading-surface navigation (Obsidian) | **Unverified.** One GUI attempt loaded a note and began indexing, then the display went black; no navigation observed |
| Static link audit of the whole reading copy | **Pass with defects** (below) |
| Symlink test failure accounted for and fixed | **Pass; merged** (PR #101) |
| `main` CI green | **Pass.** PRs #100 to #103 and #106 merged 2026-11-01; CI succeeds on `main` |

## ChatGPT reconciliation

- 7 identities were absent from the selected archive (15, 85, 187, 22, 1,036, 4 and 7 messages: 1,356 total); 12 shared
  identities were shorter (net 2,431 messages); 7 more delta rows differed only in non-message fields. All 26 come from
  one live-API capture taken 2026-10-17, which is **not** an OpenAI export, and which was never passed to the selected
  build. The capture exists unchanged in three places (archive disk, internal-disk copy, Time Machine copy).
- Replaying the engine reader over the capture plus the original export reproduces the previous store exactly
  (2,898 conversations, 81,546 messages, 2,898 of 2,898 full-row digests).
- An earlier reconstruction-based candidate is rejected: it dropped the 147 library page records, 3 attachment-only user
  turns, 49 alternate messages and 28 attachments, and relabeled 1,097 thinking messages as replies.
- The restored candidate (original sources plus the published library generation): ChatGPT 3,045 conversations / 81,691
  messages; whole archive 27,382 conversations (equal to the library's records) / 6,915,077 messages. Independent
  verification: 26/26 delta rows equal the previous store, the other 2,872 chat rows equal the selected archive, 147 pages
  equal, every library record present exactly once, `verify`/`census`/`verify-names` exit 0, all shard hashes match.
  Provenance: 26 rows name the capture file and its sha256, 2,872 name the export members, 147 are library pages; none is
  reconstructed. The engine has no origin-kind field, so the label lives in the receipt (engine issue #104).

## Library refresh (`index_blocks`)

- Not a stall. The full-path `index_blocks` committed after about 10 minutes (599 s, one SAVEPOINT, rollback journal peak
  about 1.3 GB). The progress label then stayed unchanged for 531 s of silent verification. The build failed in `export`
  against the configured 16 GiB output cap (refreshed database 10.27 GB; the completed artifacts total 17.24 GB, 56 MB over
  the cap even with exact accounting). Low CPU was not used as evidence.
- Fixed without re-ingest: export-mode resume of the failed generation with a 24 GiB cap and a 4 h deadline (the resume
  took 6,875 s, above the original 3,600 s). Two independent audits found no loss against the previously published
  generation (27,343 records, 30,818 revisions, 30,837 snapshots, 59,119 observations all present; no body-status
  regression; ChatGPT identical); published through the owning builder.
- Observability fix: PR #103 (merged) adds progress events for the silent phases and the numbers to the limit error.
- Findings fell from 1,078 to 560 because of a different reader and source set, not a repair; coverage stays incomplete.
  Sources not re-read: a missing cloud-documents root (432 retained records), native-estate sources moved by the
  2026-10-31 agent-state archiving (28 to 6,647 missing), one empty archive root.

## Search latency

About 99.9 percent of a 23-29 s `cce archive search` was write-custody tree walks (21.0 s nested-Git scan and 8.3 s symlink
scan over 42,740 directories) run for a read-only command; opening all 813 shards takes 0.076 s. PR #102 (merged) attaches
read-only commands without the walks (every cheap custody property kept; a read authorization cannot authorize writes)
and adds an exact rank-first per-shard search: CLI rare terms 24.5 s to 0.17 s and 23.5 s to 0.19 s, a common term 38.8 s
to 8.3 s, a kind-filtered query 9.2 s to 0.35 s, worst per-query ratio 1.005, results identical on 20 of 20 live queries and
on 600 randomized differential queries. A generation-bound unified index was evaluated (an uncommitted implementation) and
not adopted: it leaves the dominant cost untouched, is 3x to 118x slower on filtered queries, returns fewer than the
requested limit on 15 of 20 queries, changes filter semantics, has no invalidation binding (stale results reproduced),
writes outside the authorized root and is world-readable.

## Reading copy audit (static, all 356,129 notes)

3,167,665 prose links; 99.68 percent of internal links resolve; exactly 1 generated navigation link is unresolved
(`agent-entry.md` to `Home.md`); the other 9,978 unresolved links come from quoted source text. The vault holds only 851
wikilinks (an earlier "7,687 wikilinks" figure counted Markdown links). Pagination is hub-and-spoke and all 5,780 split
notes are complete. The copy is built from the previous library generation, is not registered in Obsidian, and has
packaging defects (engine issue #105).

## Fresh-session recall, round 2 (installed runtime)

Runtime: the engine built from the combination of PRs #100 to #103, installed locally. The owner merged those PRs while the
round ran; the installed source and tests are identical to `main` (empty diff for `src` and `tests`; only README, docs and
project URLs differ). Each question is a fresh headless session with the normal instruction chain and only the question
text. Every answer was graded from its trace by two independent graders against pre-registered pass / partial / fail text;
within every pair the graders agreed, so no tie-break was needed. For the one attachment-count question the two agents gave
equivalent answers (distinct-file count and total-reference count) that were graded differently (pass and partial); both
are recorded as pass with a caveat.

Changes from round 1:

- The subject cannot read the orchestrator's transcripts or the agent runtime root.
- The round-1 deny rules for the archive and evidence trees were relative patterns that never matched absolute paths;
  they were replaced with absolute rules and probe-verified (one control file readable, five targets denied). The same
  gap existed in round 1.
- "Store unavailable" is a kernel sandbox profile denying every read under the archive volume, a **simulation** (the
  kernel answers "not permitted", an unmount answers "not found").
- One agent offers no deny controls and read the answer key in its first round, so it was re-run inside a kernel sandbox
  that hides the evidence and raw-archive trees; another applies its own sandbox and cannot be wrapped, so its traces were
  scanned for forbidden-path access instead (none).

| Agent | Result |
|---|---|
| Codex | 6 pass, 1 partial of 7. The partial: right content, but it never stated how user turns were separated from assistant turns or that the library has no author filter |
| Claude Code | 6 pass, 1 partial of 7 (a metadata-only conversation: it said so, then added labelled content from sibling records). One question needed a retry after hitting the 600 s limit with no answer. Store unavailable (simulated): **fail**; the agent found the right facts in a raw local ChatGPT cache and presented them as retrieved instead of saying the library was unreachable |
| Gemini (isolated) | 1 pass, 1 partial, 3 fail, 2 no answer of 7. It never ran `cce` (its shell policy allowed only `cce`; every shell command it tried was `rg`, `find`, `ls` or `python3` and was denied); it searched raw files and, for the one pass, read the library vault note directly |
| Copilot | Unavailable: HTTP 402 monthly quota exceeded on every question (external) |
| OpenCode | Unavailable: provider key expired (HTTP 401); needs a new key |

Findings:

1. Every fresh agent's first call fails: the documented default store path is a symlink that `cce library` refuses
   (tracked privately). One Claude run then fell back to reading the archive directly.
2. Unquoted multi-word `cce library search` is slow: 41 s warm and 77 s cold for a 7,533-match query, against 0.6 s for the
   same words as a quoted phrase. A rank-first SQL variant returned identical results but was only 1.7x faster on the worst
   query, so ranking, not snippet generation, dominates; no PR (issue #109). Agent runs that issued the unquoted form took
   5 to 10 minutes; some Codex tool calls never completed.
3. Agents try their own memory first; the harness blocked those reads, and every blocked attempt was verified in the
   traces (none succeeded).
4. No fabrication was found in any graded answer.
5. Environment: the owner's native Claude install was found dangling (its app bundle disappeared during a desktop-app
   restart) and the session ran on the desktop-bundled CLI; the transfer tool's built-in Google client is rate limited and
   announced as retiring in 2026 (tracked privately).

## CI and the symlink failure

CI on `main` was red from the #93 follow-ups: `ruff check` failed (PLC0415, F841), the matrix cancelled, and tests did
not run. PR #100 repaired lint (AST-equivalent) and was merged on 2026-11-01; CI on `main` is green. The "pre-existing symlink failure" is a test-environment leak (issue #81):
the developer shell exports a store variable pointing at a symlink that the engine correctly refuses. PR #101 (merged) isolates
every `CCE_*` variable per test (1513 passed stripped / 1 failed ambient before; 1515 passed in both after; also green on
Python 3.12 and 3.13).

## Open items and owners

- Independent copy of the library database: finish the encrypted upload (needs a private Google client for the transfer
  tool, tracked privately) and prove decryptability with the owner's key passphrase. Owner decision on a destination for
  the other derived stores (#107).
- Discovery text for agents: the documented default store path is a symlink that `cce library` refuses, so every fresh
  agent loses its first call (Domus issue 685, private).
- `cce library search` multi-word latency (#109).
- Provider credentials and quotas: OpenCode key expired; Copilot monthly quota exceeded.
- Engine: provenance/invocation recording and the `--merge`-without-`--library-root` silent loss (#104); reading copy
  (#105); disposition of uncommitted edits made by an earlier agent (#108).
- Test design: an agent with no deny controls reads the answer key and raw copies unless isolated from outside; the
  store-unavailable case is untested for Codex and Gemini.
