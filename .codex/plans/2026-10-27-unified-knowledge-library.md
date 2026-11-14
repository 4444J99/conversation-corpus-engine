# Unified knowledge library - 2026-10-27

Owner: conversation-corpus-engine. Branch: feat/unified-knowledge-library-20261027.
Base: e15d73e416507a4e943f873934808551d74b5578.
Native session: 01a17b20-bc8f-7b31-8fff-2339c2ae5c8d.

## Authorized outcome

Extend the existing canonical archive into one private knowledge library. Its AI wing has
conversation and coding-session lanes; its authored wing holds research, creative writing
and other documents. Memories, plans and artifacts retain their kinds and explicit links.
Every session starts with a small navigation entry and retrieves sources on demand.
Obsidian and Notion consume generated views of the same versioned records.

## Verified starting point

CCE already owns normalized conversation records, provenance, an Obsidian vault, SQLite FTS,
graph exports and an MCP archive-search tool. The deployed web archive contains 5,958 records.
The separately repaired native estate catalog declares 8,854 provider-qualified metadata
identities; metadata coverage is not full-body or custody proof. The catalog is a read-only
input to this work. Existing originals, receipts, active processes and sibling branches stay
under their owners. Previous plans in my-knowledge-base describe this same unification;
that repository is a consumer/analysis layer, not a second identity authority.

Local Git allocation is unavailable to the current conductor-only keeper identity.
Source delivery uses the authenticated GitHub connector on this dedicated branch, with
portable tests against a hash-recorded public source fixture. This does not grant executor
authority, create a local Git worktree, or claim a deployed runtime.

## Implementation

1. Add streaming native-estate readers that resolve published identities to available bodies,
   preserving provider, parent lineage, source observations and explicit missing/unsupported
   body findings. Do not invent terminal states or add overlapping snapshot counts.
2. Add explicitly configured document/plan/memory/artifact sources with deterministic IDs,
   content hashes and per-file findings; retain originals and avoid unrestricted home scans.
3. Extend CCE with a versioned library builder over existing normalized records plus those
   sources. Preserve immutable normalized revisions, all observations and content-addressed
   blocks. Distinct identities never disappear because their text is equal.
4. Condense only verified repeated text spans into backlinks. Keep exact raw text recoverable,
   preserve speaker/order, and separate historical instructions/claims from current authority.
5. Derive one paginated full-text query surface, graph and linked Markdown views. Provide
   stable IDs, semantic versions, source citations, Obsidian navigation, portable Notion
   import material and a small agent entry. Keep search results bounded independently of
   total matches. User-authored notes are never pruned as generated files.
6. Integrate CLI and MCP retrieval. Publish source and tests, build an immutable private
   candidate, verify accounting/links/search/revisions/idempotence and then publish its pointer.
   Reconcile accessible original web exports to recover differing duplicates where possible.
7. Register the durable continuation and explicit source-coverage gaps in the existing owner.
   Repair source-owned runtime configuration only with exact ownership and readback evidence.

## Version contract

First observation is 1.0.0. A text/body change increments MINOR; metadata/provenance-only
changes increment PATCH; a change to the record's interpretation/classification contract
increments MAJOR. Versions describe the library contract, not inferred factual correctness.
Every revision retains its predecessor and exact normalized hash. A repeated unchanged
source is idempotent. Missing sources never erase earlier accepted records.

## Acceptance

- Two wings and both AI lanes share one canonical record/revision/query model.
- Counts derive from accepted records, with observations, revisions, identities and missing
  body evidence reported separately; every source is accounted for.
- Same native ID in different providers remains distinct; same text in different records
  retains both identities/provenances. Updates preserve earlier revisions.
- Exact repeats condense; changed quotations and unique text remain. Referenced blocks resolve.
- One-word retrieval pages more than 100 synthetic records without long-session starvation,
  reports exact total and offers bounded snippets/links.
- Markdown links resolve and both export formats reference the same IDs/versions.
- All writes remain in the registered private store, new files private, publication atomic,
  source originals untouched, failed builds retained without changing current.
- Synthetic regression tests, relevant existing archive tests and lint pass. Live acceptance
  reports measured coverage and runtime observation separately from source publication.
