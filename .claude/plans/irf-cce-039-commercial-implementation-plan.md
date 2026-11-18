# IRF-CCE-039: Commercial Architecture Implementation Plan

**IRF:** IRF-CCE-039  
**Status:** DONE (plan derived and coverage-mapped in-repo)  
**Derived from:**
- `docs/superpowers/specs/2026-03-31-cce-commercial-architecture-design.md` (S41, APPROVED)
- `docs/superpowers/specs/2026-03-31-cce-commercial-architecture-expansion.md` (S41, EXPANSION)

**Scope:** Planning artifact only. This closes the IRF-CCE-039 gap (implementation plan derived from approved specs). It does **not** claim external execution (PyPI publish, GitHub org repo creation, HN posting, Stripe go-live, revenue) unless separately evidenced elsewhere.

## Coverage summary

| Horizon | Spec source | Requirement coverage | Plan status |
| --- | --- | --- | --- |
| H1 | Design §6 H1 | 6/6 items mapped below | Mapped; some repo artifacts staged under IRF-CCE-033, external actions remain owner-gated |
| H2 | Design §6 H2 | 6/6 items mapped below | Mapped (future execution) |
| H3 | Design §6 H3 | 5/5 items mapped below | Mapped (future execution) |
| H4 | Design §6 H4 | 5/5 items mapped below | Mapped (future execution) |
| H5 | Design §6 H5 | 4/4 items mapped below | Mapped (future execution) |
| Cross-cutting | Design §§3,5,7,8,9,10,11; Expansion §§1-7 | Rings/tiers/loops, constitutional bridge, pipeline symbiosis, v2 translation, gates, verification targets | Mapped |

## H1-H5 requirement-to-plan matrix

| Horizon | Requirement (spec) | Owner | Dependencies | Acceptance predicate | Planned work / notes |
| --- | --- | --- | --- | --- | --- |
| H1 | Push `cce` to PyPI with clean README | CCE maintainer (ORGAN-I/release owner) | Build/publish credentials, package metadata readiness, CI green | `python -m build` succeeds and `twine upload dist/*` publishes installable package; README renders on PyPI | Execute release from tagged/approved commit; verify `pipx install conversation-corpus-engine` path |
| H1 | Package MCP server as `pipx install cce[mcp]` | CCE maintainer | PyPI/package extra wiring, MCP stdio stability, docs | Extra installs and `cce-mcp` starts; MCP contract tests pass (`tests/test_mcp_server.py`) | Maintain `mcp` extra and CLI entrypoint; include install docs in README/launch artifacts |
| H1 | Post on Hacker News: "Show HN: Search all your AI conversations from one place" | Founder/operator (distribution owner) | Landing page/live install path, launch copy, authenticated HN account | Post submitted with working links and monitored for replies; outcome recorded | Use staged copy in `docs/commercial/h1/launch-posts.md`; external posting is account-gated |
| H1 | Create `conversation-corpus--surfaces` repo (ORGAN-II bridge, constitutional) | Org/repo admin + CCE/surfaces maintainers | GitHub org permissions, boundary/signal contract brief | Repo exists with stated formation/boundary, consumes CCE `STATE_MODEL`/`VALIDATION_RECORD`, emits `INTERFACE_CONTRACT` | Bootstrap from `docs/commercial/h1/conversation-corpus--surfaces-repo-brief.md`; no billing/raw-export logic in ORGAN-I |
| H1 | Create landing page with email capture | Surfaces/product-marketing owner | Hosting target (e.g. Netlify), form backend/capture sink, copy | Public page deployed with functional email capture and confirmation/thanks path | Stage/deploy `docs/commercial/h1/landing-page/`; verify form submissions end-to-end |
| H1 | Enable a-i--skills paid tiers on LobeHub | Product/commercial owner for skills surface | LobeHub account/access, pricing/tier definition, payment setup | Paid tiers visible and purchasable/configured per platform constraints | External platform action; track completion evidence outside this repo |
| H2 | Ship MCP server to Claude Code MCP registry | CCE maintainer + surfaces/docs owner | Stable MCP package/release, registry submission access, listing metadata | Registry listing published and install/use path verified by external tester | Prepare submission metadata from packaged `cce[mcp]`; validate against registry requirements |
| H2 | Write Python SDK wrapper in surfaces repo | `conversation-corpus--surfaces` maintainer | Surfaces repo created, CCE API/contracts stable, packaging decisions | SDK package/import path documented with basic integration example and tests | Implement in ORGAN-II bridge repo; keep engine/product concerns separated |
| H2 | Blog post: "Why your AI conversations are disappearing" | Founder/operator (content owner) | Editorial review, distribution channel, links to landing/install | Post published with canonical links and tracked CTA | Draft/publish via chosen channel; align claims with shipped capability |
| H2 | Stranger test: 1 external dev uses CCE end-to-end | CCE maintainer (test coordinator) | Install docs, sample corpus path, external participant | External developer completes import/query/export (or MCP) flow with recorded feedback/issues | Recruit tester, capture transcript/notes, file follow-up issues |
| H2 | Web app design + prototype (conversation search) | Surfaces/product design + frontend owner | Surfaces repo/stack choice, auth/search UX scope, CCE query interfaces | Prototype demonstrates cross-provider search UX against sample/real corpus | Deliver design artifacts and clickable/runnable prototype in surfaces context |
| H2 | Add Stripe + auth to public-process essays | Public-process/product owner | Stripe account, auth mechanism, essay surface integration | Authenticated paid access path works in test/live mode as configured | External to CCE engine; coordinate with public-process implementation |
| H3 | Ring 1 launch: web app for search/recall ($29/mo) | Surfaces/product + billing owners | H2 prototype, auth, billing, hosting/ops | Paying users can subscribe ($29/mo) and use search/recall (Loop 1) | Launch MVP web app; map to "Rememberer/Archivist" segments |
| H3 | Ring 2 billing: usage-based API for integrators | Surfaces/API + product/billing owners | API gateway, metering, Stripe/billing integration, quotas | Usage metered and billed correctly for builder tier ($100-500/mo band) | Implement API keys/metering/invoicing in appropriate repos (surfaces/product) |
| H3 | Ring 3 activation: narratological lens analysis as premium | CCE/surfaces maintainers + lens/module owners | Lens integration contracts, premium entitlement checks, performance constraints | Premium users can run lens analysis (Loop 3/knowledge features) per defined tier ($99/mo path) | Wire cross-module premium surfaces without breaking engine boundaries |
| H3 | Stripe integration → first invoice → omega #9 | Product/billing owner | Legal/pricing readiness, Stripe live config, customer onboarding | First real invoice sent/paid evidence recorded (omega #9 criterion support) | Execute billing go-live; record evidence in canonical omega/commercial tracking |
| H3 | Community-hub membership tiers + event ticketing | Community-hub/product owner | Membership/ticketing platform integration, Stripe/payments | Membership tiers and ticket purchase flow operational | External surface work; coordinate with commercial funnel |
| H4 | First enterprise engagement ($5K+) | Founder/operator + enterprise delivery owner | Enterprise offer/SLA, deployment model, sales pipeline | Signed/paid engagement >= $5K with scope and delivery plan | Activate consulting/enterprise motion (Band III-IV, Loop 2 governance value) |
| H4 | Community salon: "Managing AI memory at scale" | Community/distribution owner | Venue/channel, audience, content/materials | Event held/published with attendance or artifact evidence | Run salon and capture learnings for product/community loop |
| H4 | Open-source contributor program | CCE maintainer/community owner | Contribution docs, issue triage, review bandwidth | Program launched (guides, labeled issues/onboarding path) with first contributor cycle | Define CONTRIBUTING/onboarding process and recognition path |
| H4 | 3rd-party adapter marketplace | Surfaces/platform + ecosystem owners | Adapter contract stability, submission/review process, packaging | External adapter submission/discovery/install flow exists | Specify adapter interface and marketplace governance before scale |
| H4 | Enterprise case study published | Founder/operator + customer (consent) | Successful engagement, approval to publish, evidence | Public/anonymized case study published with permitted claims | Produce case study tied to delivered outcomes |
| H5 | Conference talk: "Conversation Corpus Federation" | Founder/operator (speaker) | Talk proposal acceptance, materials, demo readiness | Talk delivered/accepted with public artifact (slides/video/listing) | Submit CFPs and present federation story/evidence |
| H5 | White-label licensing for large integrators | Commercial/legal + product owners | Licensing terms, deployment/support model, integrator demand | Signed white-label/license agreement and delivery plan | Develop licensing package (Loop-enabled productization) |
| H5 | ORGANVM platform launch (multi-module marketplace) | Platform/product owners across organs | Multi-module packaging, billing/identity, governance, marketplace ops | Platform launched with discoverable modules and transaction/usage path | Coordinate cross-organ platform release; depends on prior rings/loops |
| H5 | Research paper on federated entity resolution | Research/engineering authors | Experimental results/data, writing, venue/review process | Paper submitted/accepted/published per venue | Produce research artifact from federation/entity-resolution work |

## Cross-cutting plan requirements (design + expansion)

| Area | Requirement | Owner | Dependencies | Acceptance predicate | Notes |
| --- | --- | --- | --- | --- | --- |
| Constitutional bridge | Maintain `CCE engine -> surfaces -> product` signal chain (no I→III skip) | CCE/surfaces/product maintainers | Repo boundaries respected, contracts emitted | Surfaces emits `INTERFACE_CONTRACT`; product emits billing `STATE_MODEL` | Design §7 |
| Revenue architecture | Preserve concentric rings (0-4) and band framing in planning/execution | Product/commercial owners | Shared vocabulary across docs/repos | Roadmap/issues map to ring/band targets without contradictory pricing claims | Design §§3,5 |
| Loop activation | Activate loops by horizon (L1 H2-H3, L2 H3-H4, L3 H3-H4, L4 H4 pilot, L5 H4+) | Module/product owners | Module integrations, entitlements, delivery vehicles | Each launched tier/vehicle documents which loop(s) it enables | Expansion §§2,5 |
| Pipeline symbiosis | Keep application-pipeline backlink and shared income-surface framing | Founder/operator + maintainers | Cross-repo doc awareness | CCE docs/plans continue to reference pipeline as Band I-III bridge (IRF-CCE-038 context) | Design §8 / Expansion §6 |
| Gate contracts | Track REP-001/002/003 evidence as implementation progresses | Product/engineering owners | Surfaces/product repos, billing instrumentation | Evidence recorded for interface contract, Stripe transaction metabolism, and first paying customer/revenue live status | Design §9 / plan checkpoints |

## Verification targets (from design §10)

These remain execution targets, not claims of completion by this plan:
- [ ] PyPI package published and installable
- [ ] MCP server functional: `cce[mcp]` serves corpus queries
- [ ] Landing page live with email capture
- [ ] Stripe test integration passing
- [ ] First external user completes full import cycle
- [ ] Web app MVP: search across providers, view conversation timeline
- [ ] First invoice sent to a human being (omega #9)
- [ ] Revenue >= operating costs (omega #10)

## IRF-CCE-039 closeout note (in-repo)

- **Planning gap closed:** implementation plan derived from both approved S41 specs and mapped across H1-H5 with owner/dependency/acceptance predicates.
- **Canonical in-repo record for this issue:** this plan file (`.claude/plans/irf-cce-039-commercial-implementation-plan.md`).
- **Related evidence:** issue #24 discussion/comments; existing H1 execution/staging artifacts under `docs/commercial/h1/` and `docs/superpowers/specs/2026-03-31-cce-commercial-architecture-*.md`.
