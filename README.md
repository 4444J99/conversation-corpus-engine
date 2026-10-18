# Conversation Corpus Engine

[![CI](https://github.com/organvm-i-theoria/conversation-corpus-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/organvm-i-theoria/conversation-corpus-engine/actions/workflows/ci.yml)

`conversation-corpus-engine` is the canonical Organ I implementation of the AI conversation corpus system extracted from the staging workspace at `Workspace/intake/ai-exports`.

It owns:

- provider/source registration
- provider inbox discovery and readiness reporting
- corpus validation
- source freshness and snapshot logic
- federation across corpora
- federated canon materialization
- answering and retrieval primitives
- source authority policy per provider
- promotion policy replay, staging, review, apply, and rollback
- corpus candidate diffing, review, promotion, and rollback
- provider refresh orchestration across import, evaluation, candidate staging, and optional promotion
- publishable JSON schemas and repo-native artifact validation
- Meta/MCP-facing surface manifests and export bundles
- commercial-awareness metadata for pipeline and surface consumers

It does not own raw intake routing or system-wide discovery contracts. Those belong in Meta integrations such as `alchemia-ingestvm`, `schema-definitions`, `organvm-engine`, and `organvm-mcp-server`.

## Commercial Strategy Linkage

CCE is the institutional memory and product-scale side of the ORGANVM income surface. Its commercial architecture is tracked in [`docs/superpowers/specs/2026-03-31-cce-commercial-architecture-design.md`](docs/superpowers/specs/2026-03-31-cce-commercial-architecture-design.md) and [`docs/superpowers/specs/2026-03-31-cce-commercial-architecture-expansion.md`](docs/superpowers/specs/2026-03-31-cce-commercial-architecture-expansion.md).

The personal-capacity counterpart is [`4444J99/application-pipeline`](https://github.com/4444J99/application-pipeline), whose jobs, grants/residencies, and consulting tracks supply near-term income, market feedback, and enterprise-service leads for CCE Ring 4. This backlink closes `IRF-CCE-038` on the CCE side: the two repos should stay commercially aware of one another rather than treating pipeline revenue and CCE product revenue as separate strategies.

## Install

```bash
pip install -e ".[dev]"
```

For tool-host integration:

```bash
pipx install "conversation-corpus-engine[mcp]"
cce-mcp --project-root /path/to/project
```

The `mcp` extra is dependency-free today; it enables the install target and exposes
the stdio MCP entry point. A Claude Desktop-style server entry can point directly at
the command:

```json
{
  "mcpServers": {
    "conversation-corpus-engine": {
      "command": "cce-mcp",
      "args": ["--project-root", "/path/to/project"]
    }
  }
}
```

## License

MIT

## CLI

```bash
cce corpus list --project-root /path/to/project
cce corpus register /path/to/corpus --project-root /path/to/project --name "Notes Memory"
cce corpus-store register --project-root /path/to/project --root /path/to/private-corpus-store
cce federation build --project-root /path/to/project
cce provider discover --project-root /path/to/project --source-drop-root /path/to/source-drop
cce provider import --provider chatgpt --project-root /path/to/project --corpus-store-root /path/to/private-corpus-store --source-drop-root /path/to/source-drop --register --build
cce provider import --provider gemini --project-root /path/to/project --corpus-store-root /path/to/private-corpus-store --source-drop-root /path/to/source-drop --register --build
cce provider import --provider claude --mode local-session --project-root /path/to/project --corpus-store-root /path/to/private-corpus-store --local-root "/Users/you/Library/Application Support/Claude"
cce provider bootstrap-eval --provider claude --project-root /path/to/project --corpus-store-root /path/to/private-corpus-store --full-eval
cce provider readiness --project-root /path/to/project --source-drop-root /path/to/source-drop --write
cce provider refresh --provider chatgpt --project-root /path/to/project --corpus-store-root /path/to/private-corpus-store --source-drop-root /path/to/source-drop
cce provider refresh --provider perplexity --project-root /path/to/project --corpus-store-root /path/to/private-corpus-store --source-drop-root /path/to/source-drop
cce provider refresh --provider claude --project-root /path/to/project --corpus-store-root /path/to/private-corpus-store --promote --note "refresh and replace live corpus"
cce schema list
cce schema show corpus-contract
cce schema validate corpus-contract --path /path/to/corpus/contract.json
cce surface manifest --project-root /path/to/project --source-drop-root /path/to/source-drop
cce surface context --project-root /path/to/project --source-drop-root /path/to/source-drop
cce surface bundle --project-root /path/to/project --source-drop-root /path/to/source-drop
cce source-policy set --project-root /path/to/project --provider claude --primary-root /path/to/corpus --primary-corpus-id claude-local-session-memory
cce policy replay --project-root /path/to/project --set-threshold max_stale_corpora=1 --write
cce policy stage --project-root /path/to/project --set-threshold max_stale_corpora=1 --note "allow one stale corpus during migration"
cce policy apply --project-root /path/to/project --candidate-id latest --note "promote reviewed threshold"
cce candidate stage --project-root /path/to/project --candidate-root /path/to/candidate-corpus --provider claude --note "stage refreshed corpus"
cce candidate review --project-root /path/to/project --candidate-id latest --decision approve --note "promote candidate"
cce candidate promote --project-root /path/to/project --candidate-id latest --note "replace live corpus"
cce candidate rollback --project-root /path/to/project --target previous --note "restore previous live corpus"
cce migration corpus-v2 --project-root /path/to/project --corpus-store-root /path/to/private-corpus-store --source-root /path/to/legacy-corpus --destination-root /path/to/private-corpus-store/migrated-corpus
cce migration corpus-v2 --project-root /path/to/project --corpus-store-root /path/to/private-corpus-store --source-root /path/to/legacy-corpus --destination-root /path/to/private-corpus-store/migrated-corpus --write
cce evaluation run --root /path/to/corpus --project-root /path/to/project --corpus-store-root /path/to/private-corpus-store --seed --json
cce review queue --project-root /path/to/project
cce review resolve federated-family-merge-... --decision accepted --note "same family"
cce source freshness /path/to/corpus
cce archive build --export-root /path/to/ai-exports --project-root /path/to/project --corpus-store-root /path/to/private-corpus-store
cce archive verify --project-root /path/to/project --corpus-store-root /path/to/private-corpus-store
cce archive search "kubernetes ingress" --provider claude --project-root /path/to/project --corpus-store-root /path/to/private-corpus-store
cce archive serve --project-root /path/to/project --corpus-store-root /path/to/private-corpus-store
cce-mcp --project-root /path/to/project --archive-root /path/to/private-corpus-store/_archive
```

`cce archive` turns raw provider exports (ChatGPT, Claude, Gemini Takeout + NotebookLM, Grok,
Perplexity) into an Obsidian vault/wiki, a static archive site, a SQLite FTS5 knowledge base and
a graph, all inside the private corpus store. See [docs/archive-surfaces.md](docs/archive-surfaces.md).

Corpus-destination writers fail closed unless a store has first been registered and the
configured root exactly matches that active registration. The
`--corpus-store-root` option takes precedence over `CCE_CORPUS_STORE_ROOT`; there is no
implicit fallback. A store must already exist, must not traverse symlinks or contain nested
Git repositories, and must either live outside Git or be wholly ignored and untracked.
Provider imports default to `<store>/<corpus-id>`, while refresh candidates default to
`<store>/provider-refresh/<provider>/<run-id>/candidate-corpus`.

Corpus writers publish `conversation-corpus-engine.v2` collections as immutable
`cce.sharded_collection.v1` generations. A logical collection such as
`corpus/threads-index.json` is stored under `corpus/threads-index.collection/`, with an
atomically replaced `CURRENT` pointer, a verified manifest, and SHA-256-prefix JSONL
shards capped at 8 MiB and 1,000 records. Readers prefer v2 and read legacy v1 JSON only
when no v2 storage directory exists; malformed v2 state fails closed.

`cce migration corpus-v2` is read-only unless `--write` is supplied. The write mode
requires an authorized destination beneath the exactly registered store, preserves the
v1 source, converts copied `source/conversations.json` data to a sharded collection, and
is deterministic on rerun.

## MCP Tools

`cce-mcp` serves MCP over stdio and never writes non-protocol output to stdout. It
currently exposes:

- `cce_search`: search a registered or direct corpus root and return a grounded
  answer, citations, and structured top-hit metadata.
- `cce_list_corpora`: list registered project corpora.
- `cce_surface_context`: build the MCP-facing project context payload.
- `cce_archive_search`: full-text search the private AI archive knowledge base. Only advertised
  when the server is started with `--archive-root` (or `CCE_ARCHIVE_ROOT`); the root is server
  configuration and is never a tool argument. Results are private conversation text.

## Layout

- `src/conversation_corpus_engine/answering.py` — retrieval and answer-building primitives
- `src/conversation_corpus_engine/source_lifecycle.py` — source snapshot and freshness checks
- `src/conversation_corpus_engine/federated_canon.py` — federated review state and canon builders
- `src/conversation_corpus_engine/federation.py` — corpus registry and federation build/query logic
- `src/conversation_corpus_engine/corpus_store.py` — private corpus-store registration and write authorization
- `src/conversation_corpus_engine/sharded_collection.py` — immutable v2 collection publication, validation, and v1-compatible reads
- `src/conversation_corpus_engine/migration.py` — read-only planning and authorized v1-to-v2 corpus migration
- `src/conversation_corpus_engine/import_markdown_document_corpus.py` — generic markdown corpus materialization
- `src/conversation_corpus_engine/import_document_export_corpus.py` — document-style provider export import
- `src/conversation_corpus_engine/import_claude_export_corpus.py` — Claude bundle import
- `src/conversation_corpus_engine/import_claude_local_session_corpus.py` — Claude local-session import
- `src/conversation_corpus_engine/import_chatgpt_export_corpus.py` — ChatGPT conversations.json export import
- `src/conversation_corpus_engine/provider_discovery.py` — source-drop inbox discovery
- `src/conversation_corpus_engine/provider_import.py` — provider import/onboarding orchestration
- `src/conversation_corpus_engine/provider_refresh.py` — provider refresh orchestration over import, evaluation, and corpus promotion
- `src/conversation_corpus_engine/provider_readiness.py` — provider lane readiness and report writing
- `src/conversation_corpus_engine/schema_validation.py` — published schema catalog and lightweight JSON validation
- `src/conversation_corpus_engine/surface_exports.py` — Meta/MCP-facing manifest and context export layer
- `src/conversation_corpus_engine/schemas/` — installable JSON-schema contracts for canonical artifacts
- `src/conversation_corpus_engine/evaluation.py` — seeded/manual evaluation, scorecards, and regression gates
- `src/conversation_corpus_engine/evaluation_bootstrap.py` — provider-aware evaluation bootstrap and report generation
- `src/conversation_corpus_engine/source_policy.py` — per-provider source authority records and reports
- `src/conversation_corpus_engine/governance_policy.py` — live promotion-policy defaults and calibration
- `src/conversation_corpus_engine/governance_replay.py` — replay current corpora against promotion thresholds
- `src/conversation_corpus_engine/governance_candidates.py` — candidate stage/review/apply/rollback workflow
- `src/conversation_corpus_engine/corpus_diff.py` — live-vs-candidate structural and retrieval diffing
- `src/conversation_corpus_engine/corpus_candidates.py` — corpus candidate stage/review/promote/rollback workflow
- `src/conversation_corpus_engine/archive_model.py` — provider-neutral conversation schema, ids, time, atomic JSONL
- `src/conversation_corpus_engine/archive_read_*.py` — streaming readers (chatgpt, claude, gemini, notebooklm, grok, perplexity)
- `src/conversation_corpus_engine/archive_ingest.py` — normalized store, manifest denominators, custody gate
- `src/conversation_corpus_engine/archive_topics.py` — TF-IDF topic hubs and cross-provider neighbours
- `src/conversation_corpus_engine/archive_vault.py` / `archive_kb.py` / `archive_graph.py` / `archive_site.py` / `archive_serve.py` — vault, knowledge base, graph, site and loopback server
- `src/conversation_corpus_engine/archive_commands.py` — `cce archive` command group and cross-surface verification
- `tests/` — extraction-safe regression tests (archive tests use only synthetic fixtures)

## Runtime Outputs

Runtime state is written to `state/`, `federation/`, and `reports/`. Those directories are intentionally ignored so the repo stays focused on canonical code, contracts, tests, and public documentation.

## Status

This repo now serves as the canonical Organ I home of the conversation corpus system. The adjacent Meta consumer path is also live: `schema-definitions` canonizes the outward contracts, `organvm-engine` discovers and validates emitted surface bundles, and `organvm-mcp-server` exposes them to agent sessions. Operational hardening is underway: CI gates all pushes, ChatGPT is a first-class provider alongside Claude/Gemini/Grok/Perplexity/Copilot, and sanitized test fixtures document the expected export formats. The next frontier is downstream dashboard consumption and additional provider adapters.
