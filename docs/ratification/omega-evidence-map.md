# Omega Evidence Map — CCE Contributions

**Archetype:** THE GOVERNOR | **IRF:** IRF-CCE-035 | **Tracking:** [Issue #21](https://github.com/organvm-i-theoria/conversation-corpus-engine/issues/21)

The Omega criteria are system-level existence tests, ratified and governed in
`meta-organvm/organvm-corpvs-testamentvm/`. The conversation-corpus-engine (CCE)
is an **evidence provider**, not the ratifier (see
[`playbooks/handoffs/gh-14-omega-ratification.md`](../../playbooks/handoffs/gh-14-omega-ratification.md)).

This map records which Omega criteria CCE advances and the artifacts that
demonstrate compliance. It follows the criterion format established in
`meta-organvm/organvm-corpvs-testamentvm/`. CCE supplies and maintains the
evidence; meta-organvm assesses pass/fail.

---

## Commercial Viability Criteria (#9, #10)

Criteria #9 and #10 are the **commercial viability** Omega tests — the system
must convert its capabilities into real revenue. CCE is the designated revenue
engine for ORGAN-I (see the revenue evolution in §8 of the commercial design
spec). The commercial architecture spec is therefore the governing evidence
document for both criteria.

**Commercial spec:**
[`docs/superpowers/specs/2026-03-31-cce-commercial-architecture-design.md`](../superpowers/specs/2026-03-31-cce-commercial-architecture-design.md)
(Status: APPROVED, S41), extended by
[`docs/superpowers/specs/2026-03-31-cce-commercial-architecture-expansion.md`](../superpowers/specs/2026-03-31-cce-commercial-architecture-expansion.md)
and implemented per
[`.claude/plans/irf-cce-039-commercial-implementation-plan.md`](../../.claude/plans/irf-cce-039-commercial-implementation-plan.md).

---

#### #9: First Invoice — PENDING

**Criterion:** A first invoice must be sent to a human being — the system
produces at least one real, billable transaction.

**Status:** Not yet met. Routed through the commercial architecture. The path to
first invoice is specified in the commercial design spec §6 H3 ("Generate
Revenue, Days 30-180"): *Stripe integration → first invoice → omega #9*. The
Ring 1 web app ($29/mo search/recall) is the first billable surface.

**Evidence (commercial spec):**
- **(a) Commercial spec:** §6 H3 sequences Stripe integration to first invoice;
  §10 Verification item *"First invoice sent to a human being (omega #9)"*.
- **(b) Constitutional bridge:** §7 requires `conversation-corpus--surfaces`
  (ORGAN-II) and `conversation-corpus--product` (ORGAN-III, Stripe/billing) so
  the I→III signal chain stays unidirectional (ARCHIVE_PACKET → … → STATE_MODEL: billing → Customer $).
- **(c) Gate contract candidate:** §9 REP-002 (`digestive--measure` metabolizes
  TRANSACTION signals; evidence: "Stripe integration functional").

**Measurement:** A Stripe invoice record exists for a paying human; billing
STATE_MODEL emitted by the product repo. (§9 REP-003: `revenue_status: live` in registry.)

**Gap:** `conversation-corpus--surfaces` and `conversation-corpus--product`
repos do not yet exist; Stripe integration not yet wired.

---

#### #10: Revenue ≥ Operating Costs — PENDING

**Criterion:** Recurring revenue must meet or exceed operating costs — the
system sustains itself.

**Status:** Not yet met. Downstream of #9. The commercial spec §6 H4-H5 and the
loop-based pricing model (expansion §4) describe how recurring revenue scales
past operating costs: $29/mo (Loop 1) → $99/mo (Loop 3) → $25K+/yr enterprise
(all loops).

**Evidence (commercial spec):**
- **(a) Commercial spec:** §10 Verification item *"Revenue >= operating costs
  (omega #10)"*; §8 Revenue Evolution (H4: "CCE products generate recurring
  revenue"; H5: CCE as the studio's revenue engine).
- **(b) Pricing architecture:** expansion §4 loop-based pricing tiers map each
  compounding loop to a price point; the enterprise tier ($50K+/yr) unlocks all
  five loops.
- **(c) Cash-flow bridge:** expansion §6 — Loops 1-2 plus consulting (via
  `consult-consul--console`) fund development of Loops 3-5.

**Measurement:** Trailing-period revenue ≥ trailing-period operating costs,
evidenced by billing STATE_MODEL aggregates from the product repo.

**Gap:** No live revenue yet (blocked on #9); product/billing surfaces not built.

---

## Other CCE-Adjacent Criteria (reference)

These are tracked elsewhere and are listed here only for completeness of the
evidence map; they are not the subject of IRF-CCE-035.

- **#16 — Self-documentation:** CCE ships `CLAUDE.md`, `seed.yaml`, and a
  per-module test suite. Evidence is in-repo.
- **#18 / OM-MEM-001 — Autopoietic Memory:** proposed, not ratified. Spec:
  [`docs/ratification/OM-MEM-001-specification.md`](OM-MEM-001-specification.md);
  tracking [Issue #14](https://github.com/organvm-i-theoria/conversation-corpus-engine/issues/14).

---

*CCE maintains this evidence; ratification and the canonical Omega Evidence Map
live in `meta-organvm/organvm-corpvs-testamentvm/`.*
