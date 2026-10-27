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
