# conversation-corpus--surfaces Repo Brief

## Purpose

`conversation-corpus--surfaces` is the ORGAN-II bridge required by the commercial architecture. It shapes CCE engine output into audience-facing and tool-facing interfaces without putting billing or product-state logic inside the ORGAN-I engine.

## Constitutional Boundary

| Concern | Owner |
| --- | --- |
| Provider import, validation, federation, retrieval, source policy | `conversation-corpus-engine` |
| Web app, SDK wrapper, MCP packaging docs, API gateway, interface contracts | `conversation-corpus--surfaces` |
| Stripe, pricing, licenses, seats, invoices, billing API | `conversation-corpus--product` |

## Signal Contract

```text
CCE engine
  consumes: ARCHIVE_PACKET
  emits: ANNOTATED_CORPUS, VALIDATION_RECORD, STATE_MODEL

conversation-corpus--surfaces
  consumes: STATE_MODEL, VALIDATION_RECORD
  emits: INTERFACE_CONTRACT
```

No ORGAN-I to ORGAN-III dependency should be introduced. Surfaces must be the bridge.

## Initial Repository Layout

```text
conversation-corpus--surfaces/
  README.md
  apps/
    landing/
    web-search/
  packages/
    python-sdk/
    interface-contracts/
  mcp/
    claude-desktop.md
    registry-submission.md
  docs/
    product-brief.md
    loop-1-memory.md
```

## First Backlog

1. Import this H1 landing page into `apps/landing`.
2. Add a Python SDK wrapper around `cce surface context`, `cce surface bundle`, and `cce-mcp`.
3. Define `INTERFACE_CONTRACT` JSON schema for Ring 1 and Ring 2 surfaces.
4. Build a read-only conversation search prototype for Loop 1 memory.
5. Prepare Claude Code MCP registry submission using the `cce-mcp` tool list.

## Non-Goals

- No Stripe integration.
- No license checks.
- No mutation of CCE registry state.
- No raw provider export storage.
