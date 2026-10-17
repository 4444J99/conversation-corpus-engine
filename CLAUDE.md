# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Canonical ENGINE for multi-provider AI conversation memory. Functional class: ENGINE. Formation type: GENERATOR. Signal signature: `(Σ,Π,Θ) → (Σ,Π,Ω,Δ)`. Zero runtime dependencies beyond stdlib.

Owns: provider import, corpus validation, evaluation, federation, governance policy, review-queue triage and review-assist workflows, Meta/MCP surface exports.

Sibling deployment site: `../conversation-corpus-site/` (RESERVOIR, FORM-RES-001, not git). It hosts the live corpora, federation outputs, and operator artifacts consumed by this engine. ChatGPT is the genesis provider with the oldest manually curated gold fixtures.

## Constitutional Context

This repo exists within a system transitioning from numbered organs to named functions (SPEC-019). The current state:

- **Placement:** Theoria (knowledge AND memory). META is genome (law only). Proof: `post-flood/specs/PROOF-reservoir-placement.md`
- **Direction:** SPEC-019 System Manifestation defines the liquid model — formations declare function participation (`participates_in`) rather than organ ownership. Signal composability via set intersection. Mneme (memory) is the 8th physiological function.
- **Signal vocabulary:** 14 post-flood classes per Formation Protocol §8.1. Greek letter variables: Σ=ANNOTATED_CORPUS, Π=ARCHIVE_PACKET, Θ=EXECUTION_TRACE, Ω=VALIDATION_RECORD, Δ=STATE_MODEL, Λ=RULE_PROPOSAL, Ι=INTERFACE_CONTRACT. See `seed.yaml` for this repo's full signal I/O.
- **Reservoir Law:** RESERVOIR formations cannot emit ONT_FRAGMENT (Φ) or RULE_PROPOSAL (Λ). The site obeys this; the engine enforces it.
- **Functional class is orthogonal to organ.** Do not use the dependency DAG (I→II→III) to decide where knowledge lives — use the information graph (E^info), which is constitutionally cyclic.

## Install

```bash
# Global install via pipx (from GitHub — no PyPI needed)
pipx install git+https://github.com/organvm-i-theoria/conversation-corpus-engine.git

# Development install (editable with test/lint deps)
pip install -e ".[dev]"
```

## Commands

```bash

# Tests
python -m pytest tests/ -v
python -m pytest tests/test_file.py::ClassName::test_name -v   # single test

# Lint + format (both required — CI runs both)
pipx run ruff check src/ tests/
pipx run ruff format --check src/ tests/

# Operate against the deployment site
export $(cat ../conversation-corpus-site/.cce-env | grep -v '^#' | xargs)
cce corpus list
cce provider readiness --write
cce surface bundle
cce evaluation run --root /path/to/corpus --seed --json
```

### CLI Command Tree

The `cce` entrypoint has 14 top-level command groups. All accept `--project-root` (default: `CCE_PROJECT_ROOT` or repo root) where relevant, and most operational commands accept `--json` for machine output.

```
cce corpus      list | register           # manage corpus registry
cce federation  build                      # materialize cross-corpus indices
cce migration   seed-from-staging          # bootstrap registry from legacy staging root
cce provider    discover | readiness | import | bootstrap-eval | refresh
cce project     extract                    # extract ChatGPT project files + conversations to local dir
cce schema      list | show | validate     # inspect/validate the 10 JSON schema contracts
cce surface     manifest | context | bundle # Meta/MCP-facing surface exports
cce source-policy  show | set | history    # per-provider source authority
cce policy      show | replay | stage | review | apply | rollback  # promotion thresholds
cce candidate   show | history | stage | review | promote | rollback  # corpus candidates
cce evaluation  run                        # regression gate evaluation
cce review      queue | history | resolve | triage | assist | campaign | campaign-index | packet-hydrate | campaign-scoreboard | campaign-rollup | reject-stage | apply-plan | sample-summary | sample-propose | sample-compare
cce source      freshness                  # source staleness check
cce dashboard                              # operator-facing health summary
cce archive     ingest | analyze | vault | database | graph | site | build | verify | search | serve | names | verify-names | fixture | project feed  # browsable archive surfaces (docs/archive-surfaces.md)
```

**`provider refresh`** is the primary operational workflow — orchestrates import → bootstrap eval → run eval → stage candidate → (optionally) review → promote in a single command. Use `--approve --promote` for end-to-end auto-promote.

**`--throttle SECONDS`** (float, default 0.0) — CPU throttle for background operation. Injects `time.sleep(throttle)` every 50 iterations during near-duplicate detection. Combined with `taskpolicy -b` shell wrapping in `scripts/refresh_local_sessions.sh`, prevents corpus refresh from saturating performance cores. Recommended: `--throttle 0.001` for background runs.

Providers: `chatgpt`, `claude`, `gemini`, `grok`, `perplexity`, `copilot`, `deepseek`, `mistral`. Both ChatGPT and Claude support `--mode local-session` for live desktop-app extraction. ChatGPT reads from `~/Library/HTTPStorages/com.openai.chat.binarycookies` (Apple binary cookie jar). Claude reads from `~/Library/Application Support/Claude` (Chromium Cookies SQLite).

## Architecture

33 modules in `src/conversation_corpus_engine/`, flat structure. No subpackages. 10 JSON schemas are bundled as package data in `src/conversation_corpus_engine/schemas/`. Every module has a dedicated `tests/test_<module>.py`.

### Project Root Directories

- `state/` — mutable operational state: federation registry, federated review queue, canonical decisions, testaments
- `reports/` — generated readiness reports, `surfaces/` subdirectory for Meta/MCP exports
- `federation/` — materialized cross-corpus federated indices (families, entities, actions, doctrine briefs, conflict reports, lineage maps)
- `playbooks/` — operational runbooks: provider onboarding, review campaigns, scope recovery, agent handoffs (`handoffs/` subdirectory)
- `templates/` — testament event schema, session canon template
- `promotion-policy.json` — live promotion threshold config at repo root

### Module Relationships

`answering.py` is the shared utility layer — `load_json`, `write_json`, `write_markdown`, `slugify`, `tokenize`, `search_documents_v4`, `build_answer`. Nearly every other module imports from it. `paths.py` provides `default_project_root()` and path constants (`REPO_ROOT`, `PACKAGE_ROOT`).

**Provider pipeline:** `provider_catalog.py` defines `PROVIDER_CONFIG` (8 providers with adapter types, inbox paths, corpus ID conventions). `provider_discovery.py` scans source-drop inboxes using detection functions from `provider_exports.py`. `provider_import.py` routes each provider to its adapter. `provider_readiness.py` aggregates status across all providers. `provider_refresh.py` orchestrates the full import→eval→stage→promote lifecycle.

**Import adapters** produce identical corpus artifact sets (threads-index, pairs-index, doctrine-briefs, canonical-families, etc.):
- `import_chatgpt_export_corpus.py` — walks ChatGPT `mapping` tree (parent/children pointers), linearizes by `create_time`
- `import_chatgpt_local_session_corpus.py` — reads from `~/Library/HTTPStorages/com.openai.chat.binarycookies` via `chatgpt_local_session.py`, authenticates to `chatgpt.com/api/auth/session`, fetches conversations live, delegates to `import_chatgpt_export_corpus` for corpus generation
- `import_claude_export_corpus.py` — parses Claude `conversations.json` + `users.json` bundle
- `import_claude_local_session_corpus.py` — reads from `~/Library/Application Support/Claude` via `claude_local_session.py`
- `import_document_export_corpus.py` — generic multi-format (md/html/json/csv/zip) → normalizes to markdown → delegates to `import_markdown_document_corpus.py`

**Evaluation:** `evaluation.py` runs seeded/manual gold fixtures through 8 regression gates. `evaluation_bootstrap.py` scaffolds initial gold fixtures for new providers.

**Governance layer:** `governance_policy.py` manages promotion thresholds (defaults: zero tolerance for failures). `governance_replay.py` enables what-if threshold testing against active corpora. `governance_candidates.py` implements the stage→review→apply→rollback workflow for policy changes.

**Corpus lifecycle:** `corpus_candidates.py` implements stage→review→promote→rollback for corpus data. `corpus_diff.py` computes diffs between candidate and baseline. Both candidate workflows (corpus and policy) share the same 4-phase pattern.

**Source management:** `source_policy.py` tracks per-provider source authority (primary/fallback roots, manual vs auto decisions). `source_lifecycle.py` computes source freshness via hash-based change detection.

**Federation:** `federation.py` materializes cross-corpus indices. `federated_canon.py` manages the human review queue (5 review types: entity-alias, family-merge, action-merge, unresolved-merge, contradiction) and now stabilizes new review IDs when truncated slugs would otherwise collide.

**Schema validation:** `schema_validation.py` implements a stdlib-only JSON Schema validator (no `jsonschema` dependency) supporting type checks, required properties, const/enum, nested objects, and arrays. `surface_exports.py` assembles META-facing manifests validated against these schemas.

**Operator tools:** `dashboard.py` aggregates corpora gates, federation stats, review queue, and provider readiness into a single `cce dashboard` view. `triage.py` provides policy-driven auto-resolution of federated review items plus the entity-alias review-assist surface: grouped assist reports, packet sampling, assistant proposal sidecars, manual/proposal comparison, campaign indexing, rollups, packet hydration, scoreboards, reject-stage previews, and disabled apply-plan contracts.

### Evaluation Gates

8 regression gates with pass/warn thresholds (`evaluation.py:GATE_THRESHOLDS`):

| Gate | Direction | Pass | Warn |
|------|-----------|------|------|
| `family_stability.exact_member_match_rate` | min | 1.0 | 0.9 |
| `retrieval_metrics.family_hit_at_1` | min | 0.9 | 0.75 |
| `retrieval_metrics.thread_hit_at_1` | min | 0.5 | 0.3 |
| `retrieval_metrics.pair_hit_at_3` | min | 0.5 | 0.25 |
| `answer_metrics.state_match_rate` | min | 0.9 | 0.75 |
| `answer_metrics.required_citation_coverage_avg` | min | 0.9 | 0.75 |
| `answer_metrics.forbidden_citation_violation_rate` | max | 0.0 | 0.1 |
| `answer_metrics.abstention_match_rate` | min | 0.9 | 0.75 |

### Adding a Provider

1. Add entry to `PROVIDER_CONFIG` in `provider_catalog.py`
2. Add detection function in `provider_exports.py` (e.g., `looks_like_X_export`)
3. Wire detection mode in `provider_discovery.py:summarize_provider()`
4. Create `import_X_export_corpus.py` (follow `import_chatgpt_export_corpus.py` pattern)
5. Add routing in `provider_import.py` (module-level import, branch in `resolve_provider_import_source` and `import_provider_corpus`)
6. Add to all `choices=[...]` in `cli.py` (6 occurrences)

### Pre-commit Hook

A gitleaks secret scanner runs on commit. Dict comprehensions with `token:` keys trigger false positives. Place `# allow-secret` on the line containing `token:`, not on the `for` clause — ruff format moves comments to the `for` line but the scanner checks the `token:` line.

## Testing

Tests use pytest with shared fixtures in `tests/conftest.py`:

- **Fixtures:** `workspace` (isolated tmp_path with project/ + source-drop/), `project_root`, `source_drop_root`
- **Helpers:** `seed_minimal_corpus(root, corpus_id=, gate_state=, thread_count=)` — creates a valid corpus tree with configurable evaluation gates. `seed_provider_inbox(source_drop_root, provider, files)` — populates a provider inbox. `write_markdown_sources(root, files)` — writes markdown files for import testing.

All tests use `tmp_path` — never touch production data directories.

## Environment Variables

- `CCE_PROJECT_ROOT` — project root (default: repo root; production: `../conversation-corpus-site/`)
- `CCE_SOURCE_DROP_ROOT` — source-drop inbox location

## Conventions

- Conventional Commits with imperative mood
- stdlib only for runtime — `pytest` and `ruff` are dev-only
- Ruff config: line-length 100, target py311, selects E/F/W/I/B/PTH/RET/SIM/COM/PL. Ignores COM812 (conflicts with formatter), E501 (formatter handles length), and PLR complexity limits (0911/0912/0913/0915/2004)
- ChatGPT is the genesis provider — adapter type `chatgpt-history` aliased to `chatgpt-export` for backward compat
- Signal vocabulary: 14 post-flood classes (see `seed.yaml` for this repo's signal I/O)
- The system is transitioning to one-org flat hierarchy with `--` naming (identity--role). This repo's future name: `conversation-corpus--engine`

<!-- ORGANVM:AUTO:START -->
## System Context (auto-generated — do not edit)

**Organ:** ORGAN-I (Theory) | **Tier:** standard | **Status:** GRADUATED
**Org:** `organvm-i-theoria` | **Repo:** `conversation-corpus-engine`

### Edges
- **Produces** → `META-ORGANVM`: surface-manifest
- **Produces** → `META-ORGANVM`: mcp-context
- **Produces** → `META-ORGANVM`: surface-bundle
- **Produces** → `META-ORGANVM`: corpus-contract-schema
- **Produces** → `ORGAN-I`: evaluation-scorecard
- **Produces** → `ORGAN-I`: dashboard-payload
- **Produces** → `ORGAN-I`: triage-plan
- **Produces** → `ORGAN-I`: review-campaign-report
- **Produces** → `ORGAN-I`: review-rollup
- **Produces** → `ORGAN-I`: review-packet-hydration
- **Produces** → `ORGAN-I`: review-scoreboard
- **Produces** → `ORGAN-I`: review-apply-plan
- **Produces** → `ORGAN-I`: import-audit
- **Produces** → `ORGAN-I`: near-duplicates
- **Consumes** ← `external`: provider-exports
- **Consumes** ← `META-ORGANVM`: governance-rules

### Siblings in Theory
`recursive-engine--generative-entity`, `organon-noumenon--ontogenetic-morphe`, `auto-revision-epistemic-engine`, `narratological-algorithmic-lenses`, `call-function--ontological`, `sema-metra--alchemica-mundi`, `cognitive-archaelogy-tribunal`, `a-recursive-root`, `radix-recursiva-solve-coagula-redi`, `.github`, `nexus--babel-alexandria`, `4-ivi374-F0Rivi4`, `cog-init-1-0-`, `linguistic-atomization-framework`, `my-knowledge-base` ... and 10 more

### Governance
- Foundational theory layer. No upstream dependencies.

*Last synced: 2026-04-14T21:31:48Z*

## Active Handoff Protocol

If `.conductor/active-handoff.md` exists, **READ IT FIRST** before doing any work.
It contains constraints, locked files, conventions, and completed work from the
originating agent. You MUST honor all constraints listed there.

If the handoff says "CROSS-VERIFICATION REQUIRED", your self-assessment will
NOT be trusted. A different agent will verify your output against these constraints.

## Session Review Protocol

At the end of each session that produces or modifies files:
1. Run `organvm session review --latest` to get a session summary
2. Check for unimplemented plans: `organvm session plans --project .`
3. Export significant sessions: `organvm session export <id> --slug <slug>`
4. Run `organvm prompts distill --dry-run` to detect uncovered operational patterns

Transcripts are on-demand (never committed):
- `organvm session transcript <id>` — conversation summary
- `organvm session transcript <id> --unabridged` — full audit trail
- `organvm session prompts <id>` — human prompts only


## System Library

Plans: 269 indexed | Chains: 5 available | SOPs: 121 active
Discover: `organvm plans search <query>` | `organvm chains list` | `organvm sop lifecycle`
Library: `meta-organvm/praxis-perpetua/library/`


## Active Directives

| Scope | Phase | Name | Description |
|-------|-------|------|-------------|
| system | any | atomic-clock | The Atomic Clock |
| system | any | execution-sequence | Execution Sequence |
| system | any | multi-agent-dispatch | Multi-Agent Dispatch |
| system | any | session-handoff-avalanche | Session Handoff Avalanche |
| system | any | system-loops | System Loops |
| system | any | prompting-standards | Prompting Standards |
| system | any | research-standards-bibliography | APPENDIX: Research Standards Bibliography |
| system | any | phase-closing-and-forward-plan | METADOC: Phase-Closing Commemoration & Forward Attack Plan |
| system | any | research-standards | METADOC: Architectural Typology & Research Standards |
| system | any | sop-ecosystem | METADOC: SOP Ecosystem — Taxonomy, Inventory & Coverage |
| system | any | autonomous-content-syndication | SOP: Autonomous Content Syndication (The Broadcast Protocol) |
| system | any | autopoietic-systems-diagnostics | SOP: Autopoietic Systems Diagnostics (The Mirror of Eternity) |
| system | any | background-task-resilience | background-task-resilience |
| system | any | cicd-resilience-and-recovery | SOP: CI/CD Pipeline Resilience & Recovery |
| system | any | community-event-facilitation | SOP: Community Event Facilitation (The Dialectic Crucible) |
| system | any | context-window-conservation | context-window-conservation |
| system | any | conversation-to-content-pipeline | SOP — Conversation-to-Content Pipeline |
| system | any | cross-agent-handoff | SOP: Cross-Agent Session Handoff |
| system | any | cross-channel-publishing-metrics | SOP: Cross-Channel Publishing Metrics (The Echo Protocol) |
| system | any | data-migration-and-backup | SOP: Data Migration and Backup Protocol (The Memory Vault) |
| system | any | document-audit-feature-extraction | SOP: Document Audit & Feature Extraction |
| system | any | dynamic-lens-assembly | SOP: Dynamic Lens Assembly |
| system | any | essay-publishing-and-distribution | SOP: Essay Publishing & Distribution |
| system | any | formal-methods-applied-protocols | SOP: Formal Methods Applied Protocols |
| system | any | formal-methods-master-taxonomy | SOP: Formal Methods Master Taxonomy (The Blueprint of Proof) |
| system | any | formal-methods-tla-pluscal | SOP: Formal Methods — TLA+ and PlusCal Verification (The Blueprint Verifier) |
| system | any | generative-art-deployment | SOP: Generative Art Deployment (The Gallery Protocol) |
| system | any | market-gap-analysis | SOP: Full-Breath Market-Gap Analysis & Defensive Parrying |
| system | any | mcp-server-fleet-management | SOP: MCP Server Fleet Management (The Server Protocol) |
| system | any | multi-agent-swarm-orchestration | SOP: Multi-Agent Swarm Orchestration (The Polymorphic Swarm) |
| system | any | network-testament-protocol | SOP: Network Testament Protocol (The Mirror Protocol) |
| system | any | open-source-licensing-and-ip | SOP: Open Source Licensing and IP (The Commons Protocol) |
| system | any | performance-interface-design | SOP: Performance Interface Design (The Stage Protocol) |
| system | any | pitch-deck-rollout | SOP: Pitch Deck Generation & Rollout |
| system | any | polymorphic-agent-testing | SOP: Polymorphic Agent Testing (The Adversarial Protocol) |
| system | any | promotion-and-state-transitions | SOP: Promotion & State Transitions |
| system | any | recursive-study-feedback | SOP: Recursive Study & Feedback Loop (The Ouroboros) |
| system | any | repo-onboarding-and-habitat-creation | SOP: Repo Onboarding & Habitat Creation |
| system | any | research-to-implementation-pipeline | SOP: Research-to-Implementation Pipeline (The Gold Path) |
| system | any | security-and-accessibility-audit | SOP: Security & Accessibility Audit |
| system | any | session-self-critique | session-self-critique |
| system | any | smart-contract-audit-and-legal-wrap | SOP: Smart Contract Audit and Legal Wrap (The Ledger Protocol) |
| system | any | source-evaluation-and-bibliography | SOP: Source Evaluation & Annotated Bibliography (The Refinery) |
| system | any | stranger-test-protocol | SOP: Stranger Test Protocol |
| system | any | strategic-foresight-and-futures | SOP: Strategic Foresight & Futures (The Telescope) |
| system | any | styx-pipeline-traversal | SOP: Styx Pipeline Traversal (The 7-Organ Transmutation) |
| system | any | system-dashboard-telemetry | SOP: System Dashboard Telemetry (The Panopticon Protocol) |
| system | any | the-descent-protocol | the-descent-protocol |
| system | any | the-membrane-protocol | the-membrane-protocol |
| system | any | theoretical-concept-versioning | SOP: Theoretical Concept Versioning (The Epistemic Protocol) |
| system | any | theory-to-concrete-gate | theory-to-concrete-gate |
| system | any | typological-hermeneutic-analysis | SOP: Typological & Hermeneutic Analysis (The Archaeology) |
| unknown | any | gpt-to-os | SOP_GPT_TO_OS.md |
| unknown | any | index | SOP_INDEX.md |
| unknown | any | obsidian-sync | SOP_OBSIDIAN_SYNC.md |

Linked skills: cicd-resilience-and-recovery, continuous-learning-agent, evaluation-to-growth, genesis-dna, multi-agent-workforce-planner, promotion-and-state-transitions, quality-gate-baseline-calibration, repo-onboarding-and-habitat-creation, structural-integrity-audit


**Prompting (Anthropic)**: context 200K tokens, format: XML tags, thinking: extended thinking (budget_tokens)


## Live System Variables (Ontologia)

| Variable | Value | Scope | Updated |
|----------|-------|-------|---------|
| `active_repos` | 89 | global | 2026-04-14 |
| `archived_repos` | 54 | global | 2026-04-14 |
| `ci_workflows` | 107 | global | 2026-04-14 |
| `code_files` | 0 | global | 2026-04-14 |
| `dependency_edges` | 60 | global | 2026-04-14 |
| `operational_organs` | 10 | global | 2026-04-14 |
| `published_essays` | 29 | global | 2026-04-14 |
| `repos_with_tests` | 0 | global | 2026-04-14 |
| `sprints_completed` | 33 | global | 2026-04-14 |
| `test_files` | 0 | global | 2026-04-14 |
| `total_organs` | 10 | global | 2026-04-14 |
| `total_repos` | 145 | global | 2026-04-14 |
| `total_words_formatted` | 0 | global | 2026-04-14 |
| `total_words_numeric` | 0 | global | 2026-04-14 |
| `total_words_short` | 0K+ | global | 2026-04-14 |

Metrics: 9 registered | Observations: 32128 recorded
Resolve: `organvm ontologia status` | Refresh: `organvm refresh`


## System Density (auto-generated)

AMMOI: 58% | Edges: 42 | Tensions: 33 | Clusters: 5 | Adv: 23 | Events(24h): 32336
Structure: 8 organs / 145 repos / 1654 components (depth 17) | Inference: 98% | Organs: META-ORGANVM:65%, ORGAN-I:53%, ORGAN-II:48%, ORGAN-III:54% +5 more
Last pulse: 2026-04-14T21:31:36 | Δ24h: -1.0% | Δ7d: n/a


## Dialect Identity (Trivium)

**Dialect:** FORMAL_LOGIC | **Classical Parallel:** Logic | **Translation Role:** The Grammar — defines well-formedness in any dialect

Strongest translations: III (formal), IV (formal), META (formal)

Scan: `organvm trivium scan I <OTHER>` | Matrix: `organvm trivium matrix` | Synthesize: `organvm trivium synthesize`


## Logos Documentation Layer

**Status:** MISSING | **Symmetry:** 0.5 (GHOST)

Nature demands a documentation counterpart. This formation maintains its narrative record in `docs/logos/`.

### The Tetradic Counterpart
- **[Telos (Idealized Form)](../docs/logos/telos.md)** — The dream and theoretical grounding.
- **[Pragma (Concrete State)](../docs/logos/pragma.md)** — The honest account of what exists.
- **[Praxis (Remediation Plan)](../docs/logos/praxis.md)** — The attack vectors for evolution.
- **[Receptio (Reception)](../docs/logos/receptio.md)** — The account of the constructed polis.

### Alchemical I/O
- **[Source & Transmutation](../docs/logos/alchemical-io.md)** — Narrative of inputs, process, and returns.



*Compliance: Implementation exists without record.*

<!-- ORGANVM:AUTO:END -->
