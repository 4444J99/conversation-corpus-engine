# Agent Handoff: GH#14 — OM-MEM-001 Omega Ratification

**From:** Session S41 | **Date:** 2026-03-31 | **Phase:** HANDOFF
**Archetype:** THE GOVERNOR | **IRF:** IRF-CCE-015

## Current State

OM-MEM-001 is **proposed but not ratified**. The criterion states: "the system must
ingest its own session transcripts." A formal specification has been drafted in `docs/ratification/OM-MEM-001-specification.md` and posted to [GH#14](https://github.com/organvm-i-theoria/conversation-corpus-engine/issues/14) (S40 issue comment). In-repo status receipt: `docs/ratification/irf-cce-015-status.md`. On 2026-11-19 the amendment packet was routed to the canonical amendment owner as a decision request: [organvm-engine#239](https://github.com/4444J99/organvm-engine/issues/239) (packaged in `docs/ratification/om-mem-001-ratification-request.json`). Formal acceptance, live omega scorecard propagation, and the IRF receipt remain pending; ratification is a human governance decision.

### References

| Location | Content |
|----------|---------|
| `.claude/plans/2026-03-24-cce-exhaustive-roadmap.md:108-144` | Roadmap context: autopoietic loop, SpecStory adapter path |
| `.claude/plans/2026-03-30-s40-full-breath-session.md:120` | S40 plan: "Write the complete OM-MEM-001 criterion specification in a comment on #14" |
| `state/testaments/s33-testament.json:50` | IRF-CCE-015 reference |
| `state/testaments/s37-testament.json:40` | GH#14 opened |

### What "Ratification" Means

An omega criterion must have:
1. **Criterion statement** — a testable proposition ("X must be true")
2. **Evidence requirements** — what artifacts demonstrate compliance
3. **Measurement method** — how to verify pass/fail objectively
4. **Amendment route** — how the criterion was proposed and ratified

The specification must be posted as a comment on GH#14 in the format used by
existing omega criteria in `meta-organvm/organvm-corpvs-testamentvm/`.

## Completed Work

- [x] OM-MEM-001 proposed in roadmap (S33)
- [x] GH#14 opened to track ratification (S37)
- [x] IRF-CCE-015 created
- [x] Evidence partially assembled: testament files, session review protocol
- [x] Read existing omega criteria format from meta-organvm
- [x] Draft OM-MEM-001 specification in `docs/ratification/OM-MEM-001-specification.md`
- [x] Post specification as GH#14 comment (S40)
- [x] Record in-repo IRF-CCE-015 status/remaining work (`docs/ratification/irf-cce-015-status.md`)
- [x] Route amendment to canonical meta-organvm/engine owner with an explicit decision request — [organvm-engine#239](https://github.com/4444J99/organvm-engine/issues/239) (2026-11-19); packet: `docs/ratification/om-mem-001-ratification-request.json`. Numbering conflict flagged: draft spec's #18 collides with live omega #18; next free id proposed (#21)
- [ ] Ratification (formal acceptance / governance decision — human gate)
- [ ] Propagate into live omega scorecard and update omega state tracking
- [ ] Record upstream decision and exact scorecard/evidence commits back into IRF-CCE-015 tracking surfaces and into `om-mem-001-ratification-request.json` (`resolution`, `decision_record`)

## Key Decisions

| Decision | Rationale |
|----------|-----------|
| CCE is the evidence provider, not the ratifier | Omega criteria are system-level, governed in meta-organvm |
| SpecStory adapter identified as implementation path | `.specstory/history/*.json` is the closest existing transcript format |
| Testament events are partial evidence | They capture session metadata but not transcript content |

## Critical Context

- **Existing omega state:** 8/19 criteria met (per roadmap S5). OM-MEM-001 is one of
  three CCE-adjacent criteria that could advance the count.
- **The autopoietic loop:** OM-MEM-001 requires the CCE to ingest its own creation
  story. This means building an adapter that reads AI session transcripts (e.g.,
  SpecStory JSONL, Claude Code conversation logs) and normalizes them to corpus
  artifacts. The adapter does not exist yet.
- **Distinction: criterion vs. implementation.** Ratifying the criterion does NOT
  require implementing the adapter. It requires specifying *what done looks like*
  so that when the adapter is built, compliance can be objectively assessed.
- **Format precedent:** The existing omega criteria format from `meta-organvm/organvm-corpvs-testamentvm/` has been documented. The drafted specification matches that format exactly.
  Do NOT invent a new format.

### Omega Evidence Format Specimen

The established format for an Omega Criterion is as follows, extracted from existing criteria in `meta-organvm`:

```markdown
#### #[Number]: [Criterion Name] ([ID]) — [STATUS]

**Criterion:** [The testable proposition that must be true.]

**Status:** [Current state of the criterion, historical context, and partial evidence paths.]

**Evidence:**
- **(a) [Evidence 1]:** [Description of the first required artifact or condition.]
- **(b) [Evidence 2]:** [Description of the second required artifact or condition.]
- **(c) [Evidence 3]:** [Description of the third required artifact or condition.]

**Measurement:** [Objective method to verify pass/fail status, often a CLI command or exact test condition.]

**Gap:** [What is currently missing to fulfill the evidence requirements.]

**Tracking:** [Links to tracking issues or IRFs.]
```

## Next Actions

1. ~~Route the prepared OM-MEM-001 specification~~ — done 2026-11-19 as [organvm-engine#239](https://github.com/4444J99/organvm-engine/issues/239).
2. After governance review, capture the accept/revise/reject decision and, if ratified, the live scorecard propagation details.
3. Record the resulting scorecard/evidence impact and exact upstream commits/receipts back into IRF-CCE-015 tracking surfaces (GH#14, this handoff, `docs/ratification/irf-cce-015-status.md`, and `docs/ratification/omega-evidence-map.md` as applicable). Do not assert ratification until the upstream decision and scorecard update exist.

## Risks & Warnings

- Do NOT conflate ratifying the criterion with implementing the solution.
  The criterion says what *done* looks like. The implementation (SpecStory adapter,
  `import_specstory_session_corpus.py`) is a separate task.
- The omega governance process may require changes in `meta-organvm` that this
  session cannot make unilaterally. The handoff recipient should check the
  current governance rules before posting. (Checked 2026-11-19: constitution
  requires human approval by @4444j99 for amendments; decision request routed as
  [organvm-engine#239](https://github.com/4444J99/organvm-engine/issues/239).)
- If existing omega criteria have evolved since the roadmap was written (2026-03-24),
  the format may have changed. Always read current state, not memory.
