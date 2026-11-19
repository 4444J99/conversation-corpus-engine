# IRF-CCE-039 Implementation Plan — CCE Commercial Architecture

**Derived from:** `docs/superpowers/specs/2026-03-31-cce-commercial-architecture-design.md` (APPROVED, S41) and `docs/superpowers/specs/2026-03-31-cce-commercial-architecture-expansion.md` (EXPANSION, S41)
**Status:** Draft — implementation plan
**IRF:** IRF-CCE-039 (P2)
**Horizon mapping:** H1 (Days 1-30), H2 (Days 15-90), H3 (Days 30-180), H4 (Days 60-365), H5 (Days 90-730)

---

## 1. Current State

CCE is a fully functional engine (33 modules, 277 tests, 8 providers, zero runtime dependencies) with 12 ORGAN-III products deployed but no Stripe integration and no revenue. The commercial architecture spec was approved in S41 but no implementation plan was derived.

## 2. Product Framing (Revised)

| What We Were Selling | What We're Actually Selling |
|---|---|
| "Search your AI conversations across providers" | "Your AI conversations become an institution that teaches, governs, verifies, interprets, and compounds" |
| Features: 8 providers, quality gates, federation, MCP server | Features: Five compounding loops + knowledge stack + three delivery vehicles |
| Price: $29/mo | Price: Loop-based tiers ($29 → $99 → $25K → usage-based → $50K+/yr) |
| Moat: Zero dependencies, live session scrapers | Moat: 14 narrative lenses + epistemological tribunal + self-improving governance + evidence authority + education compounding |

## 3. Loop-Based Pricing Tiers

| Tier | Loops Unlocked | Price | Revenue Character |
|---|---|---|---|
| T1 | Loop 1 (memory/search-recall) | $29/mo | Adoption |
| T2 | Loops 1-2 (memory + governance) | $99/mo | Premium |
| T3 | Loops 1-3 + Loop 4 (pilot) | $25K/yr | Enterprise |
| T4 | All 5 loops | $50K+/yr | Full knowledge utility |

## 4. Horizon-Mapped Execution Plan

### H1 (Days 1-30) — Building, $0 revenue
**Goal:** Prepare the foundation for Loop 1 activation.

| Item | Owner | Deliverable | Depends On |
|---|---|---|---|
| Audit existing CCE modules for Loop 1 readiness | CCE maintainer | Module readiness matrix | None |
| Define Loop 1 (memory/search-recall) MVP scope | Product | Scope doc with 3 measurable criteria | Module audit |
| Set up Stripe integration scaffold | Engineering | Stripe client + webhook endpoints (non-live) | None |
| Document CCE-to-application-pipeline bridge | Product | Cash-flow bridge doc (consulting funds product dev) | IRF-CCE-038 (DONE) |

**Exit criteria:** Module readiness matrix complete, Loop 1 scope defined, Stripe scaffold non-live.

### H2 (Days 15-90) — Loop 1 partial, Adoption
**Goal:** Activate Loop 1 (memory) partially and begin user adoption.

| Item | Owner | Deliverable | Depends On |
|---|---|---|---|
| Implement Loop 1 MVP: search-recall across 8 providers | Engineering | Working search-recall, 3 providers minimum | H1 scope doc |
| Quality gates for Loop 1 corpus ingestion | Engineering | 80%+ corpus passes gates | Loop 1 MVP |
| T1 pricing page + payment flow (Stripe live) | Engineering | T1 tier purchasable | Stripe scaffold |
| First 10 T1 subscribers | Sales/Marketing | 10 paying T1 users | T1 pricing live |
| Consulting engagements (Loop all) to fund dev | consult-consul--console | 2-3 engagements | None (cash-flow bridge) |

**Exit criteria:** 3 providers searchable, T1 purchasable, 10 subscribers, consulting revenue funding dev.

### H3 (Days 30-180) — Loop 1 full + Loop 2 basic, First revenue
**Goal:** Complete Loop 1, activate Loop 2 basic, generate first revenue.

| Item | Owner | Deliverable | Depends On |
|---|---|---|---|
| Loop 1 full: all 8 providers searchable with federation | Engineering | All 8 providers, federated search | H2 Loop 1 MVP |
| Loop 2 basic: self-improving quality gates v1 | Engineering | Quality gates that log evaluations | Loop 1 full |
| T2 tier: Loops 1-2 at $99/mo | Product/Engineering | T2 purchasable | Loop 2 basic |
| First T2 subscriber | Sales | 1 T2 user | T2 live |
| Narrative lenses MVP (14 lenses) | Engineering | 14 narrative lenses operational | Loop 1 full |
| Epistemological tribunal MVP | Engineering | Tribunal flags known-unknown claims | Narrative lenses MVP |

**Exit criteria:** All 8 providers searchable, T2 purchasable, 1 T2 subscriber, narrative lenses + tribunal MVP.

### H4 (Days 60-365) — Loops 1-3 + Loop 4 pilot, Premium + enterprise
**Goal:** Unlock premium tiers and enterprise.

| Item | Owner | Deliverable | Depends On |
|---|---|---|---|
| Loop 3 full: cognitive tribunal + scale-threshold grapher | Engineering | Research thread graphing + epistemological interpretation | Loop 2 basic |
| Loop 4 pilot: evidence authority with the-actual-news | Engineering | 5 verified claims published as evidence | Loop 3 full |
| T3 tier: Loops 1-3 + Loop 4 at $25K/yr | Product | T3 purchasable | Loop 4 pilot |
| First T3 enterprise contract | Sales | 1 enterprise T3 contract | T3 live |
| Self-improving governance: gates evolve from evaluations | Engineering | Gates show measurable improvement over time | Loop 2 basic |

**Exit criteria:** T3 purchasable, 1 T3 contract, Loop 4 pilot with 5 evidence claims.

### H5 (Days 90-730) — All 5 loops, Full knowledge utility
**Goal:** Activate all loops, full knowledge utility pricing.

| Item | Owner | Deliverable | Depends On |
|---|---|---|---|
| Loop 5 full: education via classroom-rpg-aetheria | Engineering | Knowledge Crystals from curated corpus | Loop 3 full |
| T4 tier: All 5 loops at $50K+/yr | Product | T4 purchasable | Loop 5 full |
| First T4 enterprise deployment | Sales | 1 T4 deployment | T4 live |
| Loop maturity audit: all loops show compounding metrics | Engineering | Audit report with retention/moat metrics | All loops active |

**Exit criteria:** All 5 loops active, T4 purchasable, 1 T4 deployment, compounding metrics documented.

## 5. Dependencies & Cross-References

| Dependency | Status | Notes |
|---|---|---|
| IRF-CCE-038 (pipeline backlink) | DONE | CCE spec backlinks application-pipeline revenue strategy |
| ORGAN-III vehicles (the-actual-news, classroom-rpg-aetheria, consult-consul--console) | Deployed | 12 ORGAN-III products exist, need CCE integration |
| Stripe account | Not set up | Required for T1+ tiers |
| application-pipeline (cash-flow bridge) | External repo | 4444J99/application-pipeline — provides near-term income |

## 6. Acceptance Criteria (IRF-CCE-039)

- [x] Implementation plan written and saved to `.claude/plans/` (this document)
- [ ] IRF-CCE-039 marked DONE (requires issue comment/close)

## 7. Next Immediate Action

Create this plan at `.claude/plans/2026-11-18-cce-commercial-architecture-implementation.md`, then invoke the `writing-plans` skill (if available) or manually execute H1 items:
1. Module readiness matrix
2. Loop 1 scope doc
3. Stripe scaffold (non-live)
4. Cash-flow bridge documentation

---

*This plan translates the approved commercial architecture spec into executable horizons. Loops compound — each horizon builds on the previous, and revenue begins at H3.*
