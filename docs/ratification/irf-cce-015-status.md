# IRF-CCE-015 Status: OM-MEM-001 Ratification

**Archetype:** THE GOVERNOR | **IRF:** IRF-CCE-015 | **Issue:** [#14](https://github.com/organvm-i-theoria/conversation-corpus-engine/issues/14)
**Status (in-repo):** evidence/spec prepared; amendment routed for decision ([organvm-engine#239](https://github.com/4444J99/organvm-engine/issues/239)); ratification pending human governance
**Updated:** 2026-11-19

## Scope

Issue #14 / IRF-CCE-015 tracks ratification of OM-MEM-001 in the universal omega scorecard. Required outcomes stated in the issue:

1. Add a criterion-authoring or amendment path in `meta-organvm/organvm-engine`.
2. Ratify OM-MEM-001 and propagate it into the live omega scorecard.
3. Record the resulting scorecard/evidence impact back into the IRF.

## In-repo evidence and artifacts (CCE as evidence provider)

- Criterion specification: [`OM-MEM-001-specification.md`](OM-MEM-001-specification.md)
- Structured ratification request (amendment packet): [`om-mem-001-ratification-request.json`](om-mem-001-ratification-request.json) — decision request routed to the canonical amendment owner as [organvm-engine#239](https://github.com/4444J99/organvm-engine/issues/239) on 2026-11-19
- Ratification handoff: [`../../playbooks/handoffs/gh-14-omega-ratification.md`](../../playbooks/handoffs/gh-14-omega-ratification.md)
- Omega evidence map (CCE contributions): [`omega-evidence-map.md`](omega-evidence-map.md) — lists OM-MEM-001 as proposed, not ratified
- Inquiry tracking: [`../../inquiry-log.yaml`](../../inquiry-log.yaml) — `INQ-CCE-001` (Autopoietic loop and OM-MEM-001)
- Issue context: GH#14 comments include the S40 criterion specification and execution-plan guidance; disposition remains proposed pending upstream decision

## Work remaining (external / governance)

- **Ratification decision (human gate):** accept / revise / reject OM-MEM-001 per the decision request at [organvm-engine#239](https://github.com/4444J99/organvm-engine/issues/239) (constitution: omega amendments require documented rationale, human approval by @4444j99, and propagation to affected documents). Note: if accepted, the criterion needs the next free scorecard id (proposed #21) because the draft spec's #18 collides with live omega #18 (First Organic Revenue).
- **Upstream tooling (meta-organvm/organvm-engine):** criterion remains absent from the live scorecard (`scorecard.py` exposes only status/check/update); the amendment packet documents the required propagation surfaces if ratified. Building a criterion-author/amend command is upstream work, not addressed in this repo.
- **Propagation (post-acceptance):** update live omega scorecard/state in the canonical owner (`organvm-engine` scorecard, `organvm-corpvs-testamentvm` `there+back-again.md` and `docs/evaluation/omega-evidence-map.md`) once ratified.
- **IRF receipt (post-ratification):** record decision, exact scorecard/evidence commits, and resulting criterion count/impact back into IRF-CCE-015 tracking surfaces (issue, handoff/evidence docs, and any canonical IRF record).

## Smallest valid in-repo action completed

Local artifacts aligned to reflect that the OM-MEM-001 specification is drafted, posted to GH#14, and routed to the canonical amendment owner as a decision request ([organvm-engine#239](https://github.com/4444J99/organvm-engine/issues/239), 2026-11-19, packaged in [`om-mem-001-ratification-request.json`](om-mem-001-ratification-request.json)). The criterion is **not ratified**; no scorecard count changes and no MET status is asserted. Ratification, propagation, and the IRF receipt remain pending the human governance decision.
