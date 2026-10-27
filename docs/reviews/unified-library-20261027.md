# Unified library review dispositions - 2026-10-27

| Finding | Disposition |
| --- | --- |
| Anonymous native events collide across files | Accepted: full source locator fallback; only explicit native IDs prove replay equivalence. |
| Anonymous false revision relationships | Accepted intent; rejected anonymous cross-file text folding because equal text does not prove identity. |
| Alternate branches searchable but unreadable | Accepted: bounded CLI/MCP main/alternate sequences and counts. |
| Receipts exceed output budget | Accepted: receipt-aware checkpoint accounting and bounded reserved failure evidence. |
| Indented code visibly escaped | Accepted: preserve recognized space/tab/quoted indented code. |
| Direct publication path normalization | Accepted: expand and normalize project, store and generation paths. |
| Oversized CLI search offset | Accepted: validate integer bounds before SQLite binding. |
| Unbounded temporary PDF text | Accepted: child RLIMIT_FSIZE and 30-second timeout enforce the bound; stdout streaming is unnecessary. |
| Missing record reported as unknown MCP tool | Accepted: translate KeyError only inside the library branch. |
| Formatting/environment constant duplication | Accepted: full source/test format check passes; MCP imports STORE_ENV. |
| Interrupted index rebuild loses lookup index | Accepted from independent review: savepoint protects DDL and occurrence changes together. |

Validation: 243 scoped library/archive/MCP tests passed. All 145 source/test files pass Ruff formatting; changed-file Ruff checks pass. Repository-wide Ruff also reports four positional-argument findings in untouched archive_corpus, archive_model, federated_canon and provider_readiness modules; that wider result is not represented as passing.

Production ingestion and consumer acceptance have separate private receipts. Coverage gaps remain visible after successful generation validation. No private corpus content belongs in this repository.

Production validation found a disagreement between preserved indented source code and the Markdown link checker. The candidate remained unpublished. The renderer now places indentation-based and quoted code inside explicit collision-safe fences, preserving literal bytes instead of weakening the checker. Space, tab and quote regressions reproduce the original failure and pass after the renderer change. A retained production record was independently rerendered with zero unsafe active links; the failed generation remained unchanged.

Native analysis review also found an empty-query selection ambiguity and eager block allocation before atom limits. The bridge rejects empty queries, preserves nested SQL deadline scopes, and uses a lazy block iterator that checks its budget during scanning. An input with 200,000 potential blocks stopped at an eight-atom bound before creating output, using under 2 MiB of measured Python allocation.

## CI receipt (2026-10-27)

Workflow run CI on commit d4656c78c586b5967f8786bf8b49e9322e446d45 completed successfully (https://github.com/4444J99/conversation-corpus-engine/actions/runs/40556034920). Test jobs for Python 3.11/3.12/3.13 and schema-check passed. CodeRabbit and Semgrep checks were still in progress at the time of this note. Mergeable state remains unstable pending those.
