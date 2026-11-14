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
| Literal quote fence prematurely closes an unquoted example | Accepted: keep the opener's quote depth and preserve additional literal quote prefixes inside the open fence. |
| Unicode line separators join prose to the next fence | Accepted: preserve every supported trailing line boundary as a rendered newline; regression cases cover all Python line separators and blank paragraphs. |
| Unlisted files admitted during candidate publication | Accepted: the complete candidate inventory must match reported artifacts and the two explicitly allowed final receipts; reject file and directory symlinks. |
| Interrupted Notion receipt writes leave a misleading completion marker | Accepted: charge both receipts before creation, write and sync private temporary files, then publish the completed manifest last. Four failure regressions precede the fix. |
| Notion output can target a different canonical generation | Accepted from independent review: reject output ancestry within any canonical generation, independently of the source location. All 21 Notion tests pass. |
| Splitting an inline code span activates literal Markdown | Accepted: retain inline delimiter state, avoid cutting delimiter runs, and reopen continuations without creating a fence. An independently audited COW repair retains original body bytes and source hashes. Full repair and parser acceptance are reported separately from native application acceptance. |
| Escaped or mismatched backticks incorrectly exempt source images from neutralization | Accepted: preserve ambiguous escaped/multiple-delimiter prose examples verbatim in collision-safe literal blocks. Independent CommonMark parsing verifies that the example is literal and its source bytes remain copyable; ordinary fenced code keeps its established rendering. |
| Indented paragraph/list continuations bypass neutralization | Accepted through explicit literal wrappers for indentation-classified spans, rather than relying on consumer indentation context. The same wrappers avoid false link-validation failures for genuine code examples. |
| Deep HTML nesting repeatedly scans all ancestors | Accepted: maintain hidden-depth accounting as tags enter and leave the stack, including malformed closing-tag recovery. A 60,000-level synthetic document exceeded the former three-second deadline and completes after the change. |
| Terminal-newline detection backtracks on repeated CRLF | Accepted: count terminal newline sequences with a backward linear scan. An unclosed fenced example with 128 CRLF blank lines exceeded the former three-second deadline and passes after the change. |
| Codex event messages disappear when response items contain only tools | Accepted: retain unmatched event messages. Match mirrored representations one-to-one within a source using role/text plus an explicit matching ID or exact nonempty timestamp without conflicting IDs; preserve both source locators. Missing and different timestamps, fractional-second differences, unrelated same-role text and excess occurrences remain distinct. |
| Truncated or invalid gzip aborts the whole native inventory | Accepted: contain EOF/decompression errors as per-source coverage findings; retain the identity and continue other observations and sessions. |
| Percent-encoded Unicode fragment links exceed the page-size estimate | Accepted: reserve actual UTF-8-to-percent-encoded growth plus the part suffix. The synthetic Unicode-path regression fails before the fix and passes afterward; the current production inventory has no non-ASCII source paths. |
| An inline closing delimiter at the start of a page-boundary line becomes a fence | Accepted from production parser comparison: add a non-fence continuation label before three-or-more closing backticks at line start. LF/CR/CRLF and three/four-backtick regressions reproduce the issue; the correction changes only 28 suffix wrappers, adding 616 bytes outside preserved source ranges. |

Validation: 243 scoped library/archive/MCP tests passed. All 145 source/test files pass Ruff formatting; changed-file Ruff checks pass. Repository-wide Ruff also reports four positional-argument findings in untouched archive_corpus, archive_model, federated_canon and provider_readiness modules; that wider result is not represented as passing.

Production ingestion and consumer acceptance have separate private receipts. Coverage gaps remain visible after successful generation validation. No private corpus content belongs in this repository.

The late quote/newline and publication-inventory regressions were reproduced before
repair; all 68 block/build tests pass. The already published production generation
is retained unchanged: its independent acceptance audit already compared the exact
on-disk inventory with the report, and the build checked exported active links.
These source repairs govern later rendering and publication; they do not claim to
have rerendered existing immutable artifacts.

Final source hardening validation: 207 targeted block/build/resume/export/review,
package, command and MCP tests passed. Changed-file Ruff lint and formatting pass.
The installed source was refreshed to the published hardening commit; a fresh real
MCP process passed search, bounded read and revision-history calls against the
unchanged production pointer (333 matches for `lattice`).

Production validation found a disagreement between preserved indented source code and the Markdown link checker. The candidate remained unpublished. The renderer now places indentation-based and quoted code inside explicit collision-safe fences, preserving literal bytes instead of weakening the checker. Space, tab and quote regressions reproduce the original failure and pass after the renderer change. A retained production record was independently rerendered with zero unsafe active links; the failed generation remained unchanged.

Native analysis review also found an empty-query selection ambiguity and eager block allocation before atom limits. The bridge rejects empty queries, preserves nested SQL deadline scopes, and uses a lazy block iterator that checks its budget during scanning. An input with 200,000 potential blocks stopped at an eight-atom bound before creating output, using under 2 MiB of measured Python allocation.

The final block/document regressions pass together (87 tests). These late source
changes do not rewrite the immutable published generation. Its preserved source
observations and a non-resume refresh manifest remain available; claims about the
current generation continue to use its own producer version and acceptance receipts.

The combined library, native, analysis, export, command, MCP and packaging checks
passed 340 tests in 6.20 seconds after these source repairs. All eight changed
Python files pass Ruff lint and formatting. Later production boundary correction
evidence is recorded in the Obsidian guide rather than inferred from unit tests.

Final validation after the line-start suffix and variable-delimiter regressions:
351 targeted tests passed in 6.94 seconds; changed-file Ruff checks passed. The
production derivative retains all earlier repair manifests and changed parts,
and requires independent CommonMark comparison after its exact-byte checks.

## CI receipt (2026-10-27)

Workflow run CI on commit d4656c78c586b5967f8786bf8b49e9322e446d45 completed successfully (https://github.com/4444J99/conversation-corpus-engine/actions/runs/40556034920). Test jobs for Python 3.11/3.12/3.13 and schema-check passed. CodeRabbit and Semgrep checks were still in progress at the time of this note. Mergeable state remains unstable pending those.
