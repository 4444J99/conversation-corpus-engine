# Repository Guidelines

Global policy: `/Users/4jp/AGENTS.md` applies and cannot be overridden.

## Project Structure & Module Organization
`src/conversation_corpus_engine/` contains the canonical package. Keep cross-corpus logic in `federation.py`, federated canon materialization in `federated_canon.py`, retrieval helpers in `answering.py`, source freshness in `source_lifecycle.py`, policy governance in `source_policy.py`, `governance_policy.py`, `governance_replay.py`, and `governance_candidates.py`, corpus promotion logic in `corpus_diff.py` and `corpus_candidates.py`, composed provider workflows in `provider_import.py` and `provider_refresh.py`, publishable contract logic in `schema_validation.py` plus `schemas/`, and outward-facing export logic in `surface_exports.py`. Put thin CLI wiring in `cli.py`; avoid burying business logic in command handlers. Tests live in `tests/`.

## Build, Test, and Development Commands
- `pip install -e ".[dev]"`: install the package in editable mode.
- `pytest -v`: run the extracted regression suite.
- `ruff check src/ tests/`: lint the codebase.
- `cce corpus list --project-root /path/to/project`: inspect registered corpora.
- `cce federation build --project-root /path/to/project`: materialize federation outputs for a project root.
- `cce provider import --provider gemini --source-drop-root /path/to/source-drop --register --build`: import and register a provider corpus.
- `cce provider bootstrap-eval --provider claude --project-root /path/to/project --full-eval`: seed provider evaluation assets and optionally run a baseline scorecard.
- `cce provider readiness --project-root /path/to/project --source-drop-root /path/to/source-drop`: inspect multi-provider intake readiness.
- `cce provider refresh --provider perplexity --project-root /path/to/project --source-drop-root /path/to/source-drop`: import a fresh candidate, run evaluation, and stage the live-vs-candidate diff in one step.
- `cce schema list`: inspect the published artifact contract catalog.
- `cce schema validate corpus-contract --path /path/to/corpus/contract.json`: validate a generated artifact before promoting or exporting it.
- `cce surface manifest --project-root /path/to/project --source-drop-root /path/to/source-drop`: write the engine-facing externalization manifest.
- `cce surface context --project-root /path/to/project --source-drop-root /path/to/source-drop`: write the MCP-facing context payload.
- `cce surface bundle --project-root /path/to/project --source-drop-root /path/to/source-drop`: write both exported surfaces plus validation results.
- `cce source-policy set --project-root /path/to/project --provider claude --primary-root /path/to/corpus --primary-corpus-id claude-local-session-memory`: record which corpus is authoritative for a provider.
- `cce policy replay --project-root /path/to/project --set-threshold max_stale_corpora=1 --write`: replay live corpora against promotion thresholds and write reports.
- `cce policy stage --project-root /path/to/project --set-threshold max_stale_corpora=1`: stage a reviewable promotion-policy candidate.
- `cce candidate stage --project-root /path/to/project --candidate-root /path/to/candidate-corpus --provider claude`: diff a candidate corpus against the live baseline without mutating the registry.
- `cce candidate promote --project-root /path/to/project --candidate-id latest`: swap the live registry root to an approved candidate and rebuild federation.
- `cce evaluation run --root /path/to/corpus --seed`: seed fixtures and write evaluation scorecards/gates for a corpus.
- `cce review queue --project-root /path/to/project`: inspect open federated review items.
- `cce archive build --export-root /path/to/ai-exports --project-root /path/to/project --corpus-store-root /path/to/store`: ingest raw provider exports and build the vault/wiki, search database, graph and site, then verify them against the manifest (see `docs/archive-surfaces.md`). Archive outputs hold private conversation text and live only in the registered store; tests use synthetic fixtures only.

## Coding Style & Naming Conventions
Use Python 3.11+, 4-space indentation, type hints, `snake_case` modules/functions, and `UPPER_SNAKE_CASE` constants. Prefer `Path` over string paths and keep filesystem writes centralized through helper functions where possible. Avoid hardcoded workstation paths; use repo-relative defaults or explicit CLI arguments.

## Testing Guidelines
Use `pytest` with files named `test_*.py`. Keep fixtures self-contained in temp directories. New federation, source-lifecycle, governance, or corpus-promotion behavior should ship with a regression test.

## Commit & Pull Request Guidelines
Use conventional commits such as `feat: extract federation registry` or `fix: normalize corpus root validation`. PRs should explain what moved from staging, what is now canonical, and what still depends on the old workspace.

## Security & Migration Notes
Do not commit raw provider exports, browser profiles, unsanitized local-session data, or generated `state/`, `reports/`, and `federation/` runtime artifacts. This repo should contain code, contracts, redacted fixtures, and public docs only.

<!-- ORGANVM:AUTO:START -->
## Agent Context (auto-generated — do not edit)

This repo participates in the **ORGAN-I (Theory)** swarm.

### Active Subscriptions
- Event: `governance.updated` → Action: None
- Event: `schema.registry.sync` → Action: None

### Production Responsibilities
- **Produce** `surface-manifest` for META-ORGANVM
- **Produce** `mcp-context` for META-ORGANVM
- **Produce** `surface-bundle` for META-ORGANVM
- **Produce** `corpus-contract-schema` for META-ORGANVM
- **Produce** `evaluation-scorecard` for ORGAN-I
- **Produce** `dashboard-payload` for ORGAN-I
- **Produce** `triage-plan` for ORGAN-I
- **Produce** `review-campaign-report` for ORGAN-I
- **Produce** `review-rollup` for ORGAN-I
- **Produce** `review-packet-hydration` for ORGAN-I
- **Produce** `review-scoreboard` for ORGAN-I
- **Produce** `review-apply-plan` for ORGAN-I
- **Produce** `import-audit` for ORGAN-I
- **Produce** `near-duplicates` for ORGAN-I

### External Dependencies
- **Consume** `provider-exports` from `external`
- **Consume** `governance-rules` from `META-ORGANVM`

### Governance Constraints
- Adhere to unidirectional flow: I→II→III
- Never commit secrets or credentials

*Last synced: 2026-04-14T21:31:49Z*
<!-- ORGANVM:AUTO:END -->
