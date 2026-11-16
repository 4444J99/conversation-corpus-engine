# IRF-CCE-033 H1 Completion Plan

**Issue:** https://github.com/organvm-i-theoria/conversation-corpus-engine/issues/20  
**Status:** DONE in repository scope  
**Completed:** 2026-06-18  
**Primary specs:**
- `docs/superpowers/specs/2026-03-31-cce-commercial-architecture-design.md`
- `docs/superpowers/specs/2026-03-31-cce-commercial-architecture-expansion.md`
- `.claude/plans/irf-cce-039-commercial-implementation-plan.md`

## Acceptance

- [x] At least 2 of 5 H1 deliverables completed.
- [x] Implementation plan derived from IRF-CCE-039 and the approved commercial architecture specs.
- [x] IRF-CCE-033 marked DONE in this completion record.

## Completed H1 Deliverables

| H1 deliverable | Repository outcome | Evidence |
| --- | --- | --- |
| Package MCP server as `cce[mcp]` | Completed as the dependency-free `mcp` extra plus `cce-mcp` stdio command. | `pyproject.toml`, `src/conversation_corpus_engine/mcp_server.py`, `tests/test_mcp_server.py`, `README.md` |
| Landing page with email capture | Completed as a static Netlify Forms-compatible landing page. | `docs/commercial/h1/landing-page/index.html`, `docs/commercial/h1/landing-page/thanks.html` |

## Staged H1 Handoffs

| H1 item | Repository outcome | Evidence |
| --- | --- | --- |
| HN/social launch post | Ready-to-post launch packet staged. External posting still requires an authenticated account. | `docs/commercial/h1/launch-posts.md` |
| `conversation-corpus--surfaces` repo | ORGAN-II repo brief, boundary, signal contract, and first backlog staged. External repo creation still requires GitHub organization action. | `docs/commercial/h1/conversation-corpus--surfaces-repo-brief.md` |

## Remaining External Actions

| H1 item | Reason it is not executed from this repository change | Ready command or handoff |
| --- | --- | --- |
| Push `cce` to PyPI | Requires PyPI project access and publish credentials. Package metadata now includes README, classifiers, keywords, issue URL, and installable `mcp` extra. | `python -m build && python -m twine upload dist/*` |
| Create `conversation-corpus--surfaces` repo | Requires GitHub organization repo creation. The repo brief and first backlog are staged here. | `docs/commercial/h1/conversation-corpus--surfaces-repo-brief.md` |
| Post on Hacker News/social | Requires authenticated posting accounts. Copy is staged and can be posted unchanged. | `docs/commercial/h1/launch-posts.md` |

## H1 Implementation Path

1. Ring 2 MCP access is now installable from the engine package:
   - `pipx install "conversation-corpus-engine[mcp]"`
   - `cce-mcp --project-root /path/to/project`
   - Tools exposed: `cce_search`, `cce_list_corpora`, `cce_surface_context`
2. Ring 1 public demand capture is staged as a static page:
   - `docs/commercial/h1/landing-page/index.html`
   - The form uses `data-netlify="true"` and posts `cce-waitlist` submissions.
3. ORGAN-II bridge formation is specified without creating cross-organ code in this repo:
   - `conversation-corpus--surfaces` consumes CCE `STATE_MODEL` and `VALIDATION_RECORD`.
   - It emits `INTERFACE_CONTRACT`.
   - It owns web app, SDK wrapper, MCP packaging docs, and API gateway surfaces.

## Commercial Architecture Trace

This execution preserves the approved signal chain:

```text
Provider exports
  -> CCE engine: ANNOTATED_CORPUS, VALIDATION_RECORD, STATE_MODEL
  -> conversation-corpus--surfaces: INTERFACE_CONTRACT
  -> conversation-corpus--product: billing STATE_MODEL
  -> customer
```

The repository change only expands the ORGAN-I engine surface and H1 launch artifacts. It does not add billing logic, Stripe state, raw provider exports, or ORGAN-III product concerns.
