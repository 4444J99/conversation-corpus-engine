# IRF-CCE-033 H1 Status Check (2026-11-17)

## Scope

Repository-side investigation and in-repo follow-through for issue #20 ("Execute CCE commercial architecture — H1 actions") on `conversation-corpus-engine` (branch `main`, no push performed).

## What is already present in-repo

- Packaging/distribution scaffolding for H1: `pyproject.toml` defines `conversation-corpus-engine`, `cce`/`cce-mcp` scripts, and the dependency-free `mcp` extra; README documents `pipx install "conversation-corpus-engine[mcp]"` and `cce-mcp` usage.
- MCP server implementation and tests: `src/conversation_corpus_engine/mcp_server.py`, `tests/test_mcp_server.py`.
- Commercial H1 readiness contract/CLI: `src/conversation_corpus_engine/commercial_architecture.py`, `cce commercial h1 ...`, schema `commercial-h1-readiness`, tests in `tests/test_commercial_architecture.py`.
- H1 repo artifacts: `docs/commercial/h1/irf-cce-033-h1-completion-plan.md`, `docs/commercial/h1/launch-posts.md`, `docs/commercial/h1/conversation-corpus--surfaces-repo-brief.md`, `docs/commercial/h1/landing-page/`.
- Derived implementation plan: `.claude/plans/irf-cce-039-commercial-implementation-plan.md`.

## H1 deliverables (issue framing)

| Deliverable | In-repo state | Remaining work |
| --- | --- | --- |
| Push `cce` to PyPI | Package metadata/build readiness present (`python -m build`/`twine upload` path documented) | External: PyPI ownership/credentials, publish release artifacts, verify install from index |
| Package MCP server as `cce[mcp]` | Completed in-repo (`mcp` extra + `cce-mcp`/`cce mcp serve`) | External release/publish verification on PyPI as desired |
| HN/social launch post | Launch copy/checklist staged in `docs/commercial/h1/launch-posts.md` | External: authenticated posting, live links/receipts |
| Create `conversation-corpus--surfaces` repo | ORGAN-II brief/boundaries/backlog staged in-repo | External: GitHub org repo creation and bootstrap from brief |
| Landing page with email capture | Static Netlify Forms-compatible page staged in `docs/commercial/h1/landing-page/` | External: deploy, wire/verify form sink, domain as needed |

## In-repo fixes made in this pass

- Corrected `commercial_h1` readiness `mcp_distribution.tools` to match actual MCP tools (`cce_search`, `cce_list_corpora`, `cce_surface_context`).
- Corrected H1 launch/plan repo and issue links to the canonical repository (`https://github.com/4444J99/conversation-corpus-engine`).

## Blockers / external gates

- PyPI publish: requires package owner access and credentials.
- `conversation-corpus--surfaces` repo creation: requires GitHub organization/admin action.
- HN/social launch: requires authenticated operator accounts and timing/approval.
- Landing deployment and email capture verification: requires hosting/form provider setup.
- LobeHub paid-tier enablement (H1.6 in IRF-CCE-039): external platform action outside this repo.

## Suggested next in-repo-safe steps

1. Cut/publish a tagged release and attach artifacts (owner-gated), then smoke-test `pipx install "conversation-corpus-engine[mcp]"`.
2. Bootstrap `conversation-corpus--surfaces` from `docs/commercial/h1/conversation-corpus--surfaces-repo-brief.md` once repo creation is approved.
3. Deploy `docs/commercial/h1/landing-page/`, verify form submissions, and update launch links.
4. Post using `docs/commercial/h1/launch-posts.md` and record public receipts back into issue/docs.

## Addendum (2026-11-23): remaining three deliverables executed

- HN/social launch copy is now a dedicated launch artifact at `docs/launch/hn-post.md`, with a canonical GitHub-tagged install command (the PyPI project is not published yet).
- A self-contained landing page is staged at `docs/launch/landing-page.html` and published in the new ORGAN-II bridge repo: https://github.com/4444J99/conversation-corpus--surfaces (public, `main` pushed). Its `apps/landing/` directory carries the page and a GitHub Pages workflow; the interface contract, SDK scaffold, MCP docs, and product brief are bootstrapped there.
- GitHub Pages was enabled on 2026-11-23 and a deployment was verified at https://4444j99.github.io/conversation-corpus--surfaces/.
- External actions that cannot be verified from here remain: waitlist form sink wiring/test, PyPI publish, and authenticated HN/social posting. Live links and receipts should be recorded in issue #20 after those actions.

