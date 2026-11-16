# Omega Evidence Map

**Scope:** CCE-local evidence notes for the system-wide omega evidence map.
**IRF:** IRF-CCE-035
**Issue:** [organvm-i-theoria/conversation-corpus-engine#21](https://github.com/organvm-i-theoria/conversation-corpus-engine/issues/21)
**Status:** DONE for CCE evidence capture.
**Last updated:** 2026-06-19

This document records evidence that originates in the conversation corpus engine.
Formal omega criterion status remains governed by the upstream testament system;
CCE supplies evidence and implementation references, but does not ratify omega
criteria by itself.

## Commercial Criteria Evidence

The S41 commercial architecture gives omega criteria #9 and #10 a concrete
implementation path. At the 2026-06-01 audit, IRF-CCE-035 identified that this
commercial spec existed in CCE but was not represented in an evidence map.

| Criterion | CCE evidence | Evidence effect |
|-----------|--------------|-----------------|
| #9 | [`2026-03-31-cce-commercial-architecture-design.md`](../superpowers/specs/2026-03-31-cce-commercial-architecture-design.md) maps H3 revenue work to "Stripe integration -> first invoice -> omega #9" and lists "First invoice sent to a human being (omega #9)" in verification. | Moves the CCE-side evidence state from not started to planned: the required commercial milestone is named, sequenced, and tied to a verification checklist. |
| #10 | [`2026-03-31-cce-commercial-architecture-design.md`](../superpowers/specs/2026-03-31-cce-commercial-architecture-design.md) lists "Revenue >= operating costs (omega #10)" in verification and defines paid tiers, product rings, and billing surfaces. | Moves the CCE-side evidence state from not started to planned: break-even revenue is connected to product packaging and billing work, but no revenue proof is claimed. |

## Commercial Architecture References

- Design spec: [`docs/superpowers/specs/2026-03-31-cce-commercial-architecture-design.md`](../superpowers/specs/2026-03-31-cce-commercial-architecture-design.md)
- Expansion spec: [`docs/superpowers/specs/2026-03-31-cce-commercial-architecture-expansion.md`](../superpowers/specs/2026-03-31-cce-commercial-architecture-expansion.md)
- H3 milestone path: design spec lines 146-152, especially the first-invoice link to omega #9.
- Verification checklist: design spec lines 274-283, including the invoice and operating-cost checks.

## Expansion Note

The expansion spec identifies five compounding loops: Memory, Governance,
Knowledge, Authority, and Education. These loops may support future omega
criteria or amendments, but this CCE evidence note does not propose or ratify
new criteria. Any new criterion must be staged through the upstream omega
governance process.

## Compatibility Note

The CCE issue was opened on 2026-03-31 against the then-current commercial
reading of omega #9 and #10. The upstream omega evidence map later revised those
criteria names and sequencing. This file preserves the CCE commercial evidence
for the audit trail while avoiding a claim that either criterion is met.

## Completion Record

IRF-CCE-035 is complete on the CCE side because:

- The evidence map now references the CCE commercial design and expansion specs.
- Criteria #9 and #10 have explicit CCE commercial-planning evidence.
- The expansion's five-loop note is recorded as future governance input, not as
  an unratified criterion change.
