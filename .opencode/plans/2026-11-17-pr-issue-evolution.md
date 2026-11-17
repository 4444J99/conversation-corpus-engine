# PR / Issue Evolution Report — `conversation-corpus-engine`

**Date:** 2026-11-17  
**Stream:** B (PR / Issue Evolution) of the Autonomous Ecosystem Retirement & Enrichment plan  
**Method:** full remote inventory via `gh` (all states), heuristic supersession detection, local git/oss evidence. Machine-readable evidence: `.opencode/plans/stream-b/2026-11-17-pr-issue-data.json`  
**Staging run:** `09_Engineering/custody-staging/run-20261117-104235-session-retirement/`

> Supersession links below are **evidence-backed candidates**, not licensed merges. No remote PR was closed and no history was rewritten by this pass. Destructive actions are listed as ready-to-run commands for the owner or the orchestration stream.

## 1. Repository state

| Field | Value |
|---|---|
| Local path | `/Volumes/Archive4T/Projects/conversation-corpus-engine` |
| GitHub remote | `4444J99/conversation-corpus-engine` |
| Upstream archived | no |
| Default branch (remote) | main |
| Local branch | `main` |
| Dirty files | 0 |
| Linked worktrees | 1 (canonical checkout) |
| Last commit | 2026-11-15T22:43:18 — fix: deferred P3 hardening from #186 (KNOVV) and #187 (credential mask |
| Plan surfaces | .codex/plans, .claude/plans |

## 2. PR inventory

| Metric | Count |
|---|---|
| PRs (all states) | 140 |
| Merged | 120 |
| Open | 0 |
| Closed unmerged | 20 |

Closed-unmerged classification:

| Class | Count |
|---|---|
| closed without successor | 16 |
| superseded (rewrite series) | 3 |
| producer retired (Limen/Jules) | 1 |

## 3. Evolution decisions (old → new)

| Old PR | Old title | Successor | Basis |
|---|---|---|---|
| #69 | chore(deps): bump softprops/action-gh-release from 3.0.2 to 3.0.3 in the | #162 | superseded (rewrite series) |
| #76 | feat: derived naming contract, normalized layer v2, prompt feed and corp | #79 | superseded (rewrite series) |
| #182 | build(knovv): single workerd binary via npm override | #183 | superseded (rewrite series) |

## 4. Items marked archived

- **120 merged PRs** — resolved, no action; history is the archive.
- **3 closed-unmerged PRs with a traced successor** — archived as superseded; each row in §3 is a complete old → new mapping.
- **1 Limen/Jules PRs closed without successor** — archived by producer retirement. Limen is retired by standing rule; these branches have no surviving producer and no merge path.
- **16 PRs closed without a traced successor** — archived as abandoned. Titles are retained in the evidence file in case a later re-cut needs the original scope.

## 5. Open PR triage

_No open PRs._
## 6. Issues

| Metric | Count |
|---|---|
| Issues (all states) | 55 |
| Open | 23 |
| Open and idle > 45 days | 13 |

Most common labels on open issues: `jules` (3).

Longest-idle open issues (sample):

| Issue | Title | Idle |
|---|---|---|
| #35 | EV Activation Audit: ship-soon — canonical conversation-memory engine, no inst | 77d |
| #26 | Gemini local-session adapter — full content path | 77d |
| #24 | IRF-CCE-039: Derive implementation plan from commercial architecture spec | 77d |
| #23 | IRF-CCE-038: Cross-repo commercial awareness gap (CCE ↔ application-pipeline) | 77d |
| #22 | IRF-CCE-036: seed.yaml planned produces edges for 3 ORGAN-III vehicles | 77d |
| #21 | IRF-CCE-035: Omega evidence map — note commercial spec for criteria #9/#10 | 77d |
| #20 | IRF-CCE-033: Execute CCE commercial architecture — H1 actions | 77d |
| #19 | IRF-CCE-032: Omega evidence format specimen missing from GH#14 handoff | 77d |

## 7. Follow-up flags

- No open PRs; 20 closed-unmerged items are mostly Limen series retries whose merged successors are already on main.

## 8. Provenance and limits

- PR/issue inventory: `gh pr list`/`gh issue list --state all` for `4444J99/conversation-corpus-engine`, captured 2026-11-17.
- Supersession classes: explicit `supersedes #N` in title; dependency retry (same package + directory, newer bump merged); rewrite series (identical title stem, later PR merged); producer retirement (Limen/Jules branch, no successor); otherwise abandoned.
- Heuristic limit: a title-stem match can link two genuinely different changes. Treat §3 as a review queue, not as settled provenance.
- No remote mutation was performed: no PR closed, no issue closed, no branch deleted.
