# KNOVV acceptance audit, 2026-11-05

Independent, read-only audit of the KNOVV status report against live state. Evidence-bound: merged,
installed and run-on-real-data are separate states. No secrets or private content are recorded here.

## Merged (verified from GitHub)
| Repo | PR | Merge commit | `main` head equals merge |
|---|---|---|---|
| conversation-corpus-engine | #111 (and #85, an ancestor of `main`) | `630e4da` | yes |
| my-knowledge-base | #146 | `c626b6a` | yes |
| browser-state | #1 | `9ae72ed` | yes |

Issues #104 and #107 are open. #107 carries a recommendation (no extra offsite copy of regenerable data), not a hold.

## Gates reproduced at the merged commits
| Repo | Reported | Observed |
|---|---|---|
| CCE | ruff clean, 184 files formatted, 12 schemas, 1,678 tests in CI | ruff clean, 184 formatted, 12 schemas loaded, 1,677 passed plus 1 passing when global git config is disabled (Python 3.13, 3.12 not installed) |
| MKB | 1,534 tests in 101 suites, all passing | 1,533 passed, 1 failed (`manager.test.ts` times out: it does not mock Apple Notes) |
| browser-state | 8 tests | 8 of 8 passed with HOME and destinations redirected; real destinations untouched |

The one CCE failure (`test_history_audit_rejects_annotated_tag_payload`) is host-config dependent: a global
`tag.gpgsign=true` makes the test's `git tag -a` fail. It is a test hermeticity defect.

## Live versus merged
The installed `cce` (uv tool, 0.3.0) predates the merge: it has no `archive_sealed*` modules and no
`cryptography`. Nothing has run against real sources. No Zettels or attribution annotations exist in the real
corpus. Hosted CI is billing-locked, so local gates are the evidence.

## Where the report overstated (each is addressed by a hardening branch)
- Strict profile: with no source input it returns curated assistant text under the strict label.
- Google Docs projection drops link targets, flattens nesting and tab boundaries and still reports complete; with no root configured it defaults to the whole Drive.
- browser-state `history.py` leaves raw History copies in the temp dir and, on an unreadable store, overwrites the canonical inventory with fewer records and exits 0.
- Sealed publication: an interrupted write leaves a truncated object that blocks later publish, verify and restore; the capacity plan fails open to 0 when git size cannot be read; the audit cap ignores the budget.
- #104: export ZIP hash not recorded; live-capture merge protection covers only ChatGPT; a merge against a missing source silently marks it absent.
- Several documented bounds and claims have no test that isolates them (label binding, format bounds, credential file modes).
- The MKB test suite can spawn the real macOS Notes exporter (`osascript`), contradicting the docs.

## CodeQL on my-knowledge-base
Cause: GitHub Actions billing lock. The job never started (empty runner, zero steps, 2 to 3 seconds, annotation
about failed payments or spending limit). Not a code or CodeQL defect.

## Hardening work in flight
Five local branches, test-first, each verified by a separate read-only verifier before any PR: sealed
preflight and fingerprint, MKB capture hardening, #104 archive receipts, browser-state history hardening,
strict-profile fix. The end-state plan that sequences the follow-on slices lives in the private
`my-knowledge-base` repository, because it carries aggregate figures about personal data.
