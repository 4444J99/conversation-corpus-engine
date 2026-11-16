# Surface Precedent Process Review

**Atom ID:** surface-precedent-process-review
**Status:** ACTIVE
**Review Date:** 2026-06-09
**Scope:** Internal precedent processes, external precedent instances, surface taxonomy, product manifestation, and domain implementation patterns.

---

## Purpose

This review converts the prompt requirement — "all surfaces require surfacing" — into a design constraint for My Knowledge Base. A **surface** is any place where latent knowledge can appear, move, transform, be searched, be judged, or be delivered as a product. The system already treats conversations and documents as source material; this review expands the frame so every internal and external surface becomes explicit, inspectable, and mappable to product and domain outcomes.

The governing conclusion is simple: **knowledge infrastructure is incomplete when it only stores atoms; it must also surface every surface through which atoms become useful.**

---

## Review Inputs

### Internal Precedent Processes

The internal precedent set is the repository's existing theory and implementation corpus:

| Internal process | Existing precedent | Surface implication |
| --- | --- | --- |
| Four-operation methodology | Export, atomization, retrieval, intelligence extraction in `00_METHODOLOGY.md` | Treat every capability as a pipeline stage with visible inputs, outputs, failures, and downstream consumers. |
| Core principles | Atomicity, federated sources, multi-modal search, extracted intelligence in `01-core-principles.md` | A surface is not trusted unless it preserves provenance, retrieval, and auditability. |
| CAL architecture | Strategy-routed atomization in `02-cal-architecture.md` | Chunking strategies are transformation surfaces, not invisible implementation details. |
| Cross-genre deployment | Programming, creative writing, research, architecture, and performance lenses in `03-cross-genre-deployment.md` | Domain-specific retrieval lenses are product surfaces. |
| Precedent analysis | PKM, AI memory, and retrieval products in `04-precedent-analysis.md` | External products are compared by surface coverage, not only by feature names. |
| System architecture | Source manager, storage, search, APIs, UI, intelligence, and operations in `docs/ARCHITECTURE.md` | Runtime components should expose observable contracts and boundary artifacts. |
| Operations practice | Backups, migrations, monitoring, and release evidence in `docs/OPERATIONS.md` and `docs/RELEASE_EVIDENCE_TEMPLATE.md` | Operational surfaces are first-class knowledge surfaces because they determine trust. |

### External Instances Studied

External examples were reviewed for the surfaces they expose and hide. The goal is not to clone them; it is to identify missed surface categories and product manifestation patterns.

| External instance | Exposed surfaces | Hidden or weak surfaces | Product lesson |
| --- | --- | --- | --- |
| Obsidian | Local markdown vault, backlinks, graph view, Canvas cards, plugins, Publish | AI conversation ingestion, automated decomposition, corpus-level intelligence | Strong local authoring surfaces become more powerful when paired with automated atomization. |
| Logseq | Blocks, pages, block references, graph, queries, flashcards, PDF highlights | LLM-native corpus intelligence and automated multi-source federation | Block-level addressability validates atom-level identity, but manual linking remains labor-intensive. |
| Notion AI / Enterprise Search | Workspace pages, databases, connectors, Q&A, enterprise search | Exportable transparent memory graph and local-first atom ownership | Market-grade productization favors unified workspace surfaces but may weaken portability. |
| Readwise / Reader | Highlights, Reader library, Ghostreader prompts, daily review, CLI | AI conversation import and generalized atom graph | Review and resurfacing are distinct product surfaces, not merely search features. |
| Mem | Meeting capture, notes, collections, API, related-note surfacing | Transparent local atomization and inspectable ranking | "Thought partner" positioning turns surfacing into an assistant UX, but opacity is a risk. |
| Reflect | Daily notes, backlinks, advanced search, AI chat with notes | Cross-platform export/ingestion and transformation audit | AI chat is a useful product manifestation when it remains grounded in searchable notes. |
| ChatGPT Memory | Personalization controls, saved-memory management, data export flow | Full memory graph, exact ranking/retrieval mechanics, cross-vendor portability | Memory must be user-visible and exportable to be trustworthy infrastructure. |
| Claude Projects | Project knowledge bases, uploaded files, scoped context, shared team projects | Cross-project atomization and independent external retrieval | Project context is a useful bounded surface but should not be the only organizational unit. |
| Academic memory systems | Memory Sandbox-style inspectable memories; LLM-Wiki-style structured wiki/tool operations | Production-grade personal corpus workflows | Research prototypes clarify interaction requirements: view, edit, link, retrieve, and correct memory objects. |

References are listed in [External Source Notes](#external-source-notes).

---

## Surface Taxonomy

Every surface should be assigned to at least one class. A mature feature should normally traverse several classes rather than appearing only as a backend object.

| Surface class | Definition | Internal examples | Required surfacing |
| --- | --- | --- | --- |
| Source surface | Where knowledge originates before normalization | Claude, Gemini, ChatGPT exports, local markdown, PDFs, web captures | Source ID, import path, timestamp, owner/context, redaction status. |
| Transformation surface | Where knowledge changes shape | Atomizer, semantic chunker, PDF windowing, code extraction, enrichment | Strategy selected, confidence, discarded content, chunk boundaries, cost/time. |
| Storage surface | Where knowledge persists | SQLite tables, FTS5 indexes, Chroma collections, JSON exports, backups | Schema version, record counts, index health, restore path. |
| Retrieval surface | Where knowledge is found | Full-text, semantic, hybrid, tags, keywords, graph traversal | Query mode, ranking explanation, filters, failed/no-result queries. |
| Intelligence surface | Where latent meaning is extracted | Insight extraction, smart tagging, relationship detection, summaries | Prompt/version, model, cache usage, cost, confidence, reviewer override path. |
| Interface surface | Where humans or agents interact | REST API, WebSocket events, React tabs, CLI commands, native shells | Route/command contracts, response shape, error mode, affordance label. |
| Operational surface | Where reliability and governance are proven | Migrations, seed, backup, monitoring, release evidence, health audits | Check status, artifact link, owner, remediation queue. |
| Product surface | Where capability becomes a packaged outcome | Search UI, Obsidian export, GitHub Pages curation, domain reports | User promise, workflow entry point, evidence of value, domain mapping. |
| Governance surface | Where rules constrain the system | AGENTS.md, ORGAN constraints, security posture, PR evidence | Rule source, compliance check, exception record, unidirectional flow impact. |
| External exchange surface | Where this system emits or receives material from other ecosystems | Public-process essays, ORGANVM organs, academic notes, market collateral | Contract, recipient domain, allowed fields, redaction/export policy. |

---

## Surface Registry Requirement

A feature is not complete until its surfaces are registered. The registry can begin as documentation and later become an API-backed catalog.

Minimum fields:

```yaml
surface_id: unique-kebab-case-id
class: source | transformation | storage | retrieval | intelligence | interface | operational | product | governance | external-exchange
owner_component: source-manager | atomizer | search | api | web | scripts | docs | organvm
input_contract: data or action accepted
output_contract: artifact, state change, or user-visible result produced
provenance_required: true
observability_required: true
redaction_required: true | false
review_cadence: per-release | monthly | per-domain-launch | on-governance-update
related_domains:
  - academic
  - market
  - engineering
  - creative
failure_mode: what becomes invisible if this surface is not surfaced
```

This registry should be used as an acceptance gate for new capability work:

1. **Name the surface.** Do not allow anonymous transformations or implicit UX promises.
2. **Declare the class.** One implementation may create multiple surfaces.
3. **Expose provenance.** Source and transformation lineage must survive export and retrieval.
4. **Expose observability.** The operator must be able to inspect health, counts, failures, and costs.
5. **Map to product manifestation.** If no user/domain can benefit from the surface, keep it internal or defer it.
6. **Map to governance.** Security, redaction, and unidirectional ORGAN flow constraints must be explicit.

---

## Product Manifestation Model

Product manifestation is the process by which an internal surface becomes an external or user-facing capability. The same atom can manifest through several products; the product is the packaging, not the knowledge itself.

| Manifestation layer | Question | Example manifestation | Completion evidence |
| --- | --- | --- | --- |
| Raw capability | What can the system do? | Ingest a Claude export; atomize a PDF; run hybrid search. | Command/API succeeds and produces auditable artifacts. |
| Operator workflow | How does the primary user invoke it? | CLI command, web tab, saved query, release checklist. | Documented path and repeatable check. |
| Domain product | Who receives the value? | Literature review, market brief, engineering decision log, creative motif atlas. | Domain-specific output with provenance. |
| External interface | How can another system consume it? | JSON export, Obsidian vault, GitHub Pages index, API response. | Stable contract and redaction policy. |
| Feedback loop | How does use improve the system? | Search analytics, missing-surface backlog, relationship correction, tag refinement. | Recorded correction or follow-up issue. |

Product surfaces should avoid two failure modes:

- **Backend-only success:** a feature exists in code but no user/domain can discover or use it.
- **UX-only success:** a surface looks available but hides provenance, transformation logic, or failure states.

---

## Domain Implementation Patterns

### Academic Domain

Academic implementation treats atoms as citable research units and surfaces argument lineage.

Required surfaces:

- Source provenance: paper, note, conversation, citation, dataset, or class artifact.
- Transformation trail: chunking boundary, citation extraction, summary prompt, relationship type.
- Retrieval lenses: topic, author, method, claim, contradiction, evidence strength.
- Product manifestations: literature review, annotated bibliography, research memo, syllabus/date extraction, grant or article outline.

Acceptance questions:

1. Can a claim be traced back to the source atom and original source?
2. Can contradictions and unresolved questions be surfaced alongside affirming evidence?
3. Can academic outputs distinguish direct source references from LLM-generated synthesis?

### Market Domain

Market implementation treats atoms as evidence for positioning, customer language, competitive analysis, and product packaging.

Required surfaces:

- Source provenance: customer conversation, market note, competitor page, sales artifact, meeting note.
- Transformation trail: extraction of pain points, buying signals, objections, feature claims.
- Retrieval lenses: segment, problem, willingness-to-pay signal, competitor, channel, urgency.
- Product manifestations: positioning brief, landing-page copy, sales FAQ, competitive matrix, roadmap evidence pack.

Acceptance questions:

1. Can every market claim be backed by a surfaced source or marked as hypothesis?
2. Can signals be segmented without losing original context?
3. Can private corpus evidence be redacted before external collateral is produced?

### Engineering Domain

Engineering implementation treats atoms as reusable decisions, implementation patterns, and incident knowledge.

Required surfaces:

- Source provenance: code discussion, ADR, issue, PR, runtime probe, benchmark, incident report.
- Transformation trail: decision extraction, code-block parsing, dependency mapping, failure classification.
- Retrieval lenses: component, API contract, migration, test, risk, performance, security.
- Product manifestations: ADR, implementation plan, bug triage, release evidence, runbook update.

Acceptance questions:

1. Can a decision be found by both terms and meaning?
2. Can downstream code or operations show which atom justified a change?
3. Can release evidence prove the product surface did not regress?

### Creative / Performance Domain

Creative implementation treats atoms as motifs, constraints, fragments, and staging material.

Required surfaces:

- Source provenance: draft, conversation, reference image note, choreography note, performance system artifact.
- Transformation trail: motif extraction, genre lens, semantic associations, version lineage.
- Retrieval lenses: mood, image, character, rhythm, scene function, audience affordance.
- Product manifestations: essay, score, scene map, portfolio page, Omni-Dromenon cue or module.

Acceptance questions:

1. Can associative surfacing happen without erasing source lineage?
2. Can a motif travel across writing, code, and performance without becoming an untraceable blob?
3. Can public presentation separate publishable material from private substrate?

### Governance / Operations Domain

Governance implementation treats policies, audits, and release checks as knowledge surfaces rather than bureaucracy.

Required surfaces:

- Source provenance: AGENTS.md rule, ORGAN constraint, audit finding, release checklist, migration output.
- Transformation trail: compliance review, remediation mapping, risk classification.
- Retrieval lenses: rule, affected component, severity, due date, release blocker.
- Product manifestations: PR body, release evidence, remediation backlog, health-audit response.

Acceptance questions:

1. Can each change show which governance surfaces it touched?
2. Can a reviewer inspect tests and checks without rerunning the entire system?
3. Can secrets, credentials, and private material be proven absent from emitted artifacts?

---

## Internal + External Surface Matrix

| Surface class | Internal manifestation | External analog | Gap / action |
| --- | --- | --- | --- |
| Source | Source manager and raw exports | ChatGPT data export; Claude project uploads; Readwise imports | Add a documented source-surface registry for every importer. |
| Transformation | CAL atomization and enrichment | Logseq blocks; Readwise highlights; LLM-Wiki compilation | Expose chunking/atomization decisions as inspectable artifacts. |
| Storage | SQLite, FTS5, Chroma, backups | Obsidian markdown vault; Notion workspace; Mem cloud notes | Preserve local-first inspectability while supporting product exports. |
| Retrieval | FTS, semantic, hybrid, graph | Obsidian graph/search; Reflect AI chat; Notion Enterprise Search | Add ranking explanations and no-result analytics to surface retrieval behavior. |
| Intelligence | InsightExtractor, SmartTagger, relationships, summaries | Ghostreader, Mem related notes, ChatGPT Memory | Record prompt/model/cost metadata and user correction hooks. |
| Interface | REST, WebSocket, React tabs, CLI | PKM apps, AI chats, APIs, CLIs | Treat every route/command/tab as a named surface with owner and contract. |
| Product | Obsidian export, GitHub Pages, reports | Publish, Reader review, Notion wiki, Reflect notes | Package domain outputs around surfaced provenance, not generic dashboards. |
| Governance | AGENTS, ORGAN constraints, release evidence | Enterprise controls, audit logs, admin settings | Make compliance surfaces queryable and tied to PR/release evidence. |

---

## Implementation Backlog

### P0: Documentation and Acceptance Gates

- Create a living **Surface Registry** document or data file using the schema above.
- Add "surface impact" to PR/release evidence: source, transformation, retrieval, intelligence, interface, product, governance.
- Update precedent analysis to score tools by surface coverage as well as four-operation coverage.

### P1: Observability Surfaces

- Emit chunking decision summaries for atomization runs: selected strategy, chunk count, discarded units, confidence, elapsed time.
- Add retrieval explanation metadata for hybrid search: FTS rank, semantic rank, RRF contribution, filters applied.
- Add intelligence metadata: model, prompt version, cache hit/miss, estimated cost, output confidence.

### P2: Product Surfaces

- Define domain-specific saved lenses for academic, market, engineering, creative, and governance workflows.
- Add export profiles per domain, each with redaction defaults and provenance requirements.
- Add a missing-surface backlog generated from failed searches, unsupported imports, and opaque transformations.

### P3: External Exchange Surfaces

- Formalize contracts for ORGANVM downstream consumers.
- Publish sanitized domain products through approved external channels.
- Track feedback from external products back into atom relationships, tags, and retrieval lenses.

---

## External Source Notes

The external review used public documentation and product pages available on 2026-06-09:

- Obsidian Canvas product page: https://obsidian.md/canvas
- Logseq documentation index: https://docs.logseq.com/
- Notion Enterprise Search help: https://www.notion.com/en-gb/help/enterprise-search
- Readwise Ghostreader documentation: https://docs.readwise.io/reader/guides/ghostreader/overview
- Readwise CLI page: https://readwise.io/cli
- Mem API documentation: https://docs.mem.ai/
- Mem team usage guide: https://help.mem.ai/guides/using-mem-as-a-team
- Reflect advanced search documentation: https://reflect.academy/advanced-search
- ChatGPT Memory FAQ: https://help.openai.com/en/articles/8590148-personalization-and-data-controls-faq
- ChatGPT data export help: https://help.openai.com/en/articles/7260999-how-do-i-export-my-chatgpt-history-and-data
- Claude Projects help: https://support.claude.com/en/articles/9517075-what-are-projects
- Memory Sandbox paper: https://arxiv.org/abs/2308.01542
- LLM-Wiki paper search record: https://arxiv.org/abs/2605.25480

---

## Related Documents

- `00_METHODOLOGY.md` -- Four-operation methodology used as the internal process baseline.
- `01-core-principles.md` -- Governing principles that constrain surface design.
- `02-cal-architecture.md` -- Transformation-surface architecture for atomization.
- `03-cross-genre-deployment.md` -- Domain lens precedent for product manifestation.
- `04-precedent-analysis.md` -- External precedent comparison expanded by this review.
- `docs/ARCHITECTURE.md` -- Runtime component surfaces and data flow.
- `docs/OPERATIONS.md` -- Operational and governance surfaces.
