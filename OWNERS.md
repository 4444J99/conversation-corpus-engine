# OWNERS.md

Module ownership for the conversation-corpus-engine. Each module belongs to exactly
one archetype. Cross-archetype imports are permitted but edge direction matters.

## Archetypes

### THE ACQUISITOR — Provider Intake & Session Management

**Domain:** Everything that brings data into the system. Session adapters, cookie
parsing, API clients, import pipelines, provider catalog, source authority.

**Boundary:** Writes to corpus directories and source-drop state. Never writes to
federation state or governance state.

**Modules (19):**

| Module | Role |
|--------|------|
| `chatgpt_local_session` | ChatGPT desktop/Chrome cookie session client |
| `claude_local_session` | Claude desktop Chromium session client |
| `import_chatgpt_export_corpus` | ChatGPT JSON export → corpus artifacts |
| `import_chatgpt_local_session_corpus` | ChatGPT live session → corpus artifacts |
| `import_claude_export_corpus` | Claude JSON export → corpus artifacts |
| `import_claude_local_session_corpus` | Claude live session → corpus artifacts |
| `import_document_export_corpus` | Generic multi-format document → corpus |
| `import_markdown_document_corpus` | Markdown → corpus artifacts |
| `provider_catalog` | Validated runtime provider-manifest loader and compatibility read model |
| `provider_discovery` | Source-drop inbox scanner |
| `source_adapter_registry` | Provider-neutral redacted-bundle format adapters |
| `authority_projection` | Role-aware authority, lane, and adoption projection |
| `authority_ingest` | Deterministic envelope/event generation plus coverage and parity receipts |
| `provider_exports` | Export format detection functions |
| `provider_import` | Import routing dispatcher |
| `provider_readiness` | Cross-provider status aggregation |
| `provider_refresh` | Full import→eval→stage→promote lifecycle |
| `source_policy` | Per-provider source authority (primary/fallback) |
| `source_lifecycle` | Source freshness via hash-based change detection |

**Skills:** `data-ingestion-pipeline`, `data-pipeline-architect`, `session-lifecycle-patterns`, `data-backup-patterns`

**Issues:** #15 (API scope degradation), #16 (wrong Projects endpoint)

---

### THE EVALUATOR — Quality Gates & Schema Contracts

**Domain:** Regression evaluation, gold fixture management, schema validation.
Determines whether data passes quality thresholds.

**Boundary:** Read-only access to corpus data. Writes evaluation scorecards and
schema validation results. Must be independent of THE GOVERNOR — evaluation
cannot be influenced by promotion desire.

**Modules (3):**

| Module | Role |
|--------|------|
| `evaluation` | 8 regression gates with pass/warn thresholds |
| `evaluation_bootstrap` | Gold fixture scaffolding for new providers |
| `schema_validation` | stdlib-only JSON Schema validator |

**Skills:** `testing-patterns`, `tdd-workflow`, `verification-loop`, `coding-standards-enforcer`

**Issues:** None currently open.

---

### THE FEDERATOR — Cross-Corpus Federation & Review

**Domain:** Materializing cross-corpus indices, entity resolution, the human
review queue, triage automation, and review-assist campaigns.

**Boundary:** Reads from all corpora. Writes only to `state/` (queue, history,
decisions) and `federation/` (indices). Never modifies corpus data directly.

**Modules (3):**

| Module | Role |
|--------|------|
| `federation` | Cross-corpus index materialization |
| `federated_canon` | Human review queue (5 review types), review-ID generation |
| `triage` | Policy-driven auto-resolution, review-assist campaigns |

**Skills:** `knowledge-graph-builder`, `research-synthesis-workflow`, `knowledge-architecture`

**Issues:** #13 (review-ID migration)

---

### THE GOVERNOR — Promotion Policy & Corpus Lifecycle

**Domain:** Promotion thresholds, corpus candidate workflow (stage→review→promote→rollback),
governance policy changes, corpus diffing.

**Boundary:** Reads evaluation results and federation state. Writes to corpus candidate
staging and promotion-policy.json. Cannot override THE EVALUATOR's gate verdicts.

**Modules (5):**

| Module | Role |
|--------|------|
| `governance_policy` | Promotion threshold management |
| `governance_replay` | What-if threshold testing against active corpora |
| `governance_candidates` | Stage→review→apply→rollback for policy changes |
| `corpus_candidates` | Stage→review→promote→rollback for corpus data |
| `corpus_diff` | Candidate vs. baseline diffing |

**Skills:** `continuous-learning-agent`, `configuration-management`, `repo-onboarding-flow`

**Issues:** #14 (omega ratification)

---

### THE OPERATOR — CLI, Dashboard & Infrastructure

**Domain:** The surface layer. CLI argument parsing, operator dashboard, Meta/MCP
exports, shared utilities, path constants, migrations.

**Boundary:** Orchestrates all other archetypes via CLI commands. Writes to `reports/`
and surface manifests. Owns the shared utility layer (`answering.py`, `paths.py`)
that all other archetypes depend on.

**Modules (6):**

| Module | Role |
|--------|------|
| `cli` | Entrypoint, argument parsing, subcommand routing |
| `dashboard` | Operator-facing health summary |
| `surface_exports` | Meta/MCP manifest assembly |
| `answering` | Shared utilities: load/write JSON, slugify, tokenize, search |
| `paths` | `REPO_ROOT`, `PACKAGE_ROOT`, `default_project_root()` |
| `migration` | Data migration helpers |

**Skills:** `cli-tool-design`, `api-design-patterns`, `cross-agent-handoff`, `agent-swarm-orchestrator`

**Issues:** #11 (S33 testament), #12 (S37 testament)

---

## Boundary Rules

1. Each module belongs to exactly ONE archetype
2. Cross-archetype imports are permitted (flat module structure)
3. THE EVALUATOR must be independent of THE GOVERNOR
4. THE ACQUISITOR never writes to federation or governance state
5. THE FEDERATOR reads from all corpora but writes only to `state/` and `federation/`
6. THE GOVERNOR cannot override evaluation gate verdicts
7. THE OPERATOR owns shared utilities; all archetypes may import from `answering` and `paths`

## Module Count

| Archetype | Modules | % |
|-----------|---------|---|
| ACQUISITOR | 19 | 53% |
| GOVERNOR | 5 | 14% |
| OPERATOR | 6 | 17% |
| EVALUATOR | 3 | 8% |
| FEDERATOR | 3 | 8% |
| **Total** | **36** | **100%** |
