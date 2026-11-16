# CCE Commercial Architecture — Design Specification

**Date:** 2026-03-31
**Status:** APPROVED
**Session:** S41
**Scope:** Revenue architecture for conversation-corpus-engine and its position in the ORGANVM income surface
**Cross-repo reference:** [`4444J99/application-pipeline`](https://github.com/4444J99/application-pipeline) is the personal-capacity income counterpart for jobs, grants/residencies, and consulting.
**IRF:** IRF-CCE-038 DONE — CCE docs now backlink the pipeline revenue strategy.

---

## 1. Problem Statement

The conversation-corpus-engine is a fully functional ENGINE (33 modules, 277 tests,
8 providers, zero runtime dependencies) with no revenue. 12 ORGAN-III products are
deployed with no Stripe integration. The application-pipeline generates personal
income (jobs/grants/consulting) but is capacity-bounded. The system needs scalable
revenue from its institutional capabilities, not just its operator's hours.

The organism is also in metamorphosis: the old 8-organ numbered model is dissolving
into a biological mechanism model (`a-organvm`). The commercial architecture must
work in both vocabularies.

## 2. Dual Vocabulary

| Concept | v1 (Current Organs) | v2 (Emerging Mechanisms) |
|---------|-------------------|------------------------|
| Core engine | ORGAN-I (Theoria) | mneme--remember |
| Audience surfaces | ORGAN-II (Poiesis) | integumentary--present |
| Commercial products | ORGAN-III (Ergon) | reproductive--generate |
| Quality gates | ORGAN-IV (Taxis) | immune--verify |
| Distribution | ORGAN-VII (Kerygma) | circulatory--route |
| Personal labor | 4444J99 (Pipeline) | circulatory--contribute |
| Pricing/billing | (none explicit) | digestive--measure |
| Enterprise services | (consulting) | muscular--execute |

The spec is written in v1 for present execution. Section 9 provides the v2
translation for organism absorption.

## 3. The Income Surface

Revenue exists on a spectrum from personal capacity to institutional scale.
Every node on the surface is a distinct income source with its own physics.

### 3.1 Five Bands

| Band | Physics | Capacity | Examples |
|------|---------|----------|----------|
| **I. Labor** | hours x rate | Bounded by time | Job salary, adjunct teaching |
| **II. Awards** | quality x fit | Lumpy, non-recurring | Grants, residencies, fellowships, writing |
| **III. Services** | relationships x rate | Semi-scalable | Consulting, enterprise deploy, custom adapters |
| **IV. Products** | users x price | Scalable, recurring | SaaS, API, licensing |
| **V. Platforms** | network x marketplace cut | Exponential | ORGANVM marketplace, white-label, embed fees |

Band III is the **bridge zone** where personal capacity meets institutional
scale. The same human activity produces different revenue depending on framing:
- "I'll configure your AI governance" = consulting ($125/hr) = Band III
- "Deploy CCE in your org" = enterprise services ($25K/yr) = Band III-IV
- "Subscribe to managed corpus" = SaaS ($29/mo) = Band IV
- "Embed our API" = platform ($0.01/conversation) = Band IV-V

### 3.2 Concentric Rings (CCE-specific)

```
Ring 0: Engine (free, open source) — EXISTS NOW
Ring 1: SaaS web app ($29/mo consumer) — BUILD (30 days)
Ring 2: Platform API + MCP server (usage-based) — BUILD (1-2 weeks)
Ring 3: Cross-module premium (narratological lenses, knowledge base) — BUILD (H3)
Ring 4: Enterprise services ($5-50K/yr) — ACTIVATE (H4)
```

### 3.3 Full Node Map

| Node | Band | System | Horizon | Monthly Range | Infrastructure |
|------|------|--------|---------|---------------|---------------|
| Adjunct teaching | I | External | Now | $1K | Exists |
| Emergency grants | II | Pipeline | H1 | $0-2K lumpy | Exists |
| Art-tech grants | II | Pipeline | H1-H2 | $0-5K lumpy | Exists |
| Writing income | II | Pipeline | H1 | $250-3K/piece | Exists |
| a-i--skills marketplace | IV | ORGAN-IV | H1 | $0-1K | LobeHub indexed |
| CCE MCP server | IV | CCE Ring 2 | H1 | $0 (adoption) | Partial |
| Job salary | I | Pipeline | H2-H3 | $8-15K | Exists |
| public-process paid archive | IV | ORGAN-V | H2 | $0-2K | Needs Stripe+auth |
| community-hub membership | IV | ORGAN-VI | H2 | $0-1K | Needs Stripe+tiers |
| CCE SaaS web app | IV | CCE Ring 1 | H3 | $0-5K | Needs web app |
| CCE API usage | IV | CCE Ring 2 | H3 | $0-3K | Needs billing |
| Consulting | III | Pipeline | H3 | $2-10K | Exists |
| public-record-data-scrapper | IV | ORGAN-III | H3 | $0-5K | Deployed, needs pricing |
| agentic-titan licensing | IV | ORGAN-IV | H3-H4 | $0-5K | Needs SaaS layer |
| CCE enterprise services | III-IV | CCE Ring 4 | H4 | $5-15K | Needs sales motion |
| metasystem-master licensing | IV | ORGAN-II | H4 | $0-5K | Deployed |
| CCE cross-module premium | IV | CCE Ring 3 | H4 | $1-5K | Needs module wiring |
| CCE white-label | V | CCE Ring 2+4 | H5 | $5-20K | Needs brand + trust |
| ORGANVM platform marketplace | V | All | H5 | $5-50K | Needs platform |
| Styx behavioral blockchain | V | ORGAN-III | H5 | Unknown | Stripe integrated |

## 4. Audience Segments

Derived from CCE's capabilities, not prescribed:

| Segment | Pain | Product Surface | Price | Band |
|---------|------|----------------|-------|------|
| **The Rememberer** | Lost AI conversation history | Web app (Ring 1) | $29/mo | IV |
| **The Archivist** | AI conversations ARE work product | Web app + export (Ring 1) | $29-49/mo | IV |
| **The Builder** | Need conversation data as infra | API + MCP (Ring 2) | $100-500/mo | IV |
| **The Researcher** | Need curated conversation corpora | Engine + lenses (Ring 0+3) | $500-5K/yr | IV |
| **The Governor** | No visibility into org AI usage | Dashboard + governance (Ring 4) | $5-50K/yr | III-IV |

All segments served simultaneously through different surfaces.
No beachhead selection required — the engine is the center, rings radiate.

First dollar most likely from: **AI consulting firms** or **AI-native startups**
(daily pain, fast decision cycles, budget authority).

## 5. Pricing Architecture

| Tier | Price | Includes | Audience |
|------|-------|----------|----------|
| **Free** | $0 | CLI engine, 1 provider, local only | Builders evaluating |
| **Professional** | $29/mo | Web app, all providers, federation, search | Rememberers, Archivists |
| **Team** | $99/mo | Professional + 5 seats + shared corpora | Small teams |
| **Builder** | $100-500/mo | API access, MCP server, schema contracts, usage-based | Integrators |
| **Enterprise** | $25-50K/yr | Self-hosted, custom adapters, governance config, SLA | Governors |

$29/mo, not $15/mo. Below $30 feels like a toy. Above $30 needs procurement.
$29 expenses without approval.

## 6. Horizon-Mapped Execution

### H1: Prove It Works (Days 1-30)

- Push `cce` to PyPI with clean README
- Package MCP server as `pipx install cce[mcp]`
- Post on Hacker News: "Show HN: Search all your AI conversations from one place"
- Create `conversation-corpus--surfaces` repo (ORGAN-II bridge, constitutional)
- Create landing page with email capture
- Enable a-i--skills paid tiers on LobeHub

### H2: Validate Externally (Days 15-90)

- Ship MCP server to Claude Code MCP registry
- Write Python SDK wrapper in surfaces repo
- Blog post: "Why your AI conversations are disappearing"
- Stranger test: 1 external dev uses CCE end-to-end
- Web app design + prototype (conversation search)
- Add Stripe + auth to public-process essays

### H3: Generate Revenue (Days 30-180)

- Ring 1 launch: web app for search/recall ($29/mo)
- Ring 2 billing: usage-based API for integrators
- Ring 3 activation: narratological lens analysis as premium
- Stripe integration → first invoice → omega #9
- Community-hub membership tiers + event ticketing

### H4: Build Community (Days 60-365)

- First enterprise engagement ($5K+)
- Community salon: "Managing AI memory at scale"
- Open-source contributor program
- 3rd-party adapter marketplace
- Enterprise case study published

### H5: Achieve Recognition (Days 90-730)

- Conference talk: "Conversation Corpus Federation"
- White-label licensing for large integrators
- ORGANVM platform launch (multi-module marketplace)
- Research paper on federated entity resolution

## 7. Constitutional Compliance (v1)

### ORGAN-II Bridge

The design requires a new repo: `conversation-corpus--surfaces` (ORGAN-II).
This is the constitutional bridge between engine (ORGAN-I) and product (ORGAN-III).
Without it, the I→III dependency skip violates the unidirectional flow.

- Formation type: FORM (shapes engine output into audience-specific interfaces)
- Contains: web app, SDK wrapper, MCP server packaging, API gateway
- Signal: consumes STATE_MODEL + VALIDATION_RECORD from CCE, emits INTERFACE_CONTRACT

### ORGAN-III Product Repo

A second new repo: `conversation-corpus--product` (ORGAN-III).
Contains Stripe integration, pricing logic, license management, billing API.

- Signal: consumes INTERFACE_CONTRACT from surfaces, emits STATE_MODEL (billing state)

### Signal Chain

```
Provider exports (ARCHIVE_PACKET)
  → CCE engine (ANNOTATED_CORPUS, VALIDATION_RECORD, STATE_MODEL)
    → Surfaces (INTERFACE_CONTRACT)
      → Product (STATE_MODEL: billing)
        → Customer ($)
```

No back-edges. No skipped organs. No formation violations.

## 8. Application Pipeline Symbiosis

The pipeline and CCE are not competing revenue strategies. They are the same
income equation with different parameter weightings:

- Pipeline Pillar 1 (Jobs) → builds CCE domain expertise
- Pipeline Pillar 2 (Grants) → validates CCE research credibility, advances omega
- Pipeline Pillar 3 (Consulting) → IS CCE Ring 4 (enterprise services)
- Pipeline Identity #9 (Founder/Operator) → IS the CCE commercial persona
- Pipeline Identity #5 (Independent Engineer) → builds CCE engineering credibility
- SGO research → papers on conversation federation = CCE marketing + omega #14

### 8.1 Cross-Repo Commercial Awareness

The canonical pipeline counterpart is
[`4444J99/application-pipeline`](https://github.com/4444J99/application-pipeline).
CCE owns the governed conversation corpus, provider federation, schemas, and
surface exports that can become recurring product revenue. The pipeline owns the
near-term application and relationship infrastructure that turns operator effort
into jobs, grants/residencies, writing, and consulting income.

Commercial planning must keep these docs linked in both directions:

- CCE docs point to `application-pipeline` as the jobs/grants/consulting income
  bridge that funds and validates CCE commercialization.
- Pipeline docs should point back to this spec as the product-scale successor to
  the same income surface, especially where consulting engagements become CCE
  Ring 4 enterprise services.

`IRF-CCE-038` is DONE for this repo: the CCE commercial architecture now carries
an explicit pipeline backlink and names the shared revenue model.

### Revenue Evolution

```
H1-H2: Pipeline earns NOW (labor + awards)
H3:    Pipeline consulting bridges to CCE services (Band III)
H4:    CCE products generate recurring revenue (Band IV)
H5:    Pipeline Pillar 1 (jobs) becomes optional
       CCE is the studio's revenue engine (Band IV-V)
```

## 9. Translation to v2 (Organism Vocabulary)

When the organism absorbs this spec, the following mapping applies:

### Mechanism Assignment

| Commercial Function | Mechanism | Verb | Signal Signature |
|-------------------|-----------|------|------------------|
| Persist conversation memory | mneme | remember | ARCHIVE_PACKET → ANNOTATED_CORPUS @ on-demand |
| Present to audiences | integumentary | present | STATE_MODEL → INTERFACE_CONTRACT @ continuous |
| Route value between surfaces | circulatory | route | INTERFACE_CONTRACT → TRACE @ event-driven |
| Verify corpus quality | immune | verify | ANNOTATED_CORPUS → VALIDATION_RECORD @ on-demand |
| Execute enterprise work | muscular | execute | CONTRACT → TRACE @ on-demand |
| Generate commercial surfaces | reproductive | instantiate | INTERFACE_CONTRACT → PRODUCT @ on-demand |
| Metabolize payments | digestive | measure | TRANSACTION → STATE_MODEL @ event-driven |
| Score audience fit | immune | score | QUERY → VALIDATION_RECORD @ on-demand |
| Govern promotion lifecycle | nervous | govern | RULE_PROPOSAL → STATE_MODEL @ event-driven |

### Gate Contract (Candidate)

When the organism is ready, this spec becomes a gate contract:

```yaml
name: reproductive--instantiate
mechanism: reproductive
verb: instantiate
state: CALLING
source_modules:
  - conversation-corpus-engine (mneme--remember)
  - a-i--skills (integumentary--emit)
  - public-process (integumentary--report)
gates:
  - id: REP-001
    check: "At least 1 mechanism emits INTERFACE_CONTRACT"
    evidence: "integumentary--present exists and passes"
  - id: REP-002
    check: "digestive--measure can metabolize TRANSACTION signals"
    evidence: "Stripe integration functional"
  - id: REP-003
    check: "At least 1 paying customer exists"
    evidence: "revenue_status: live in registry"
```

This gate contract encodes how the organism learns to sell: any mechanism that
can emit INTERFACE_CONTRACT can be commercialized by routing through
reproductive--instantiate → digestive--measure.

**The function that derives revenue surfaces from signal signatures is the
organism's learned commercial intelligence.** It is not prescribed. It is
discovered through this spec, then codified as a gate contract, then applied
to any future mechanism that wants to generate income.

## 10. Verification

- [ ] PyPI package published and installable
- [ ] MCP server functional: `cce[mcp]` serves corpus queries
- [ ] Landing page live with email capture
- [ ] Stripe test integration passing
- [ ] First external user completes full import cycle
- [ ] Web app MVP: search across providers, view conversation timeline
- [ ] First invoice sent to a human being (omega #9)
- [ ] Revenue >= operating costs (omega #10)

## 11. Tribunal Consensus (Design Review)

Three personas reviewed this design (S41):

1. **The Cynic** (Market Reality): First buyer is AI consulting firms or AI-native
   startups. $29/mo, not $15. Moat survives provider competition because CCE is
   neutral — providers will never build a neutral federation layer.

2. **The Architect** (System Integrity): ORGAN-II bridge (surfaces repo) and
   ORGAN-III product repo are constitutionally required. Signal chain verified:
   no back-edges, no skipped organs. v2 mechanism mapping preserves the guarantee.

3. **The Accelerant** (Speed to Revenue): Ship MCP server this week. Create
   surface repo next week. Web app in 30 days. Charge $29/month from day 31.
   Everything else is H2-H5.

---

*This spec is v1 (present vocabulary). It will be translated to v2 (organism
vocabulary) when the organism reaches sufficient maturity (>=3 functions in
a-organvm). The translation pattern itself becomes a function:
reproductive--instantiate.*
