# IRF-CCE-038: Cross-repo commercial awareness gap (CCE ↔ application-pipeline)

**IRF:** IRF-CCE-038 (P2)  
**Issue:** [#23](https://github.com/4444J99/conversation-corpus-engine/issues/23) — *Cross-repo commercial awareness gap (CCE ↔ application-pipeline)*  
**Status (CCE in-repo):** CCE-side cross-references and commercial-awareness linkage implemented; cross-repo completion depends on reciprocal backlink in `4444J99/application-pipeline` and issue closeout evidence.

## Problem statement (from issue #23)

The income surface spans CCE and application-pipeline but neither repo's docs reference the other's revenue strategy. The CCE spec references the pipeline's three-pillar model but there was no durable backlink symmetry. Needs cross-reference or shared strategic index.

## Acceptance criteria (issue #23)

- [x] **CCE side:** cross-reference added in CCE docs and commercial metadata to `4444J99/application-pipeline` as the personal-capacity income counterpart (jobs, grants/residencies, consulting) for the shared ORGANVM income surface.
- [ ] **application-pipeline side:** reciprocal cross-reference added in `4444J99/application-pipeline` docs to `4444J99/conversation-corpus-engine` as the institutional memory / product-scale counterpart. (External to this repo; may be completed locally if that checkout is available, but must be committed/pushed in that repo separately.)
- [ ] **IRF-CCE-038 marked DONE (global):** requires both repos' docs/backlinks and issue #23 closeout after verification. CCE docs/specs already record CCE-side completion language; do not claim global DONE until reciprocal linkage is verified.

## CCE-side evidence (present in this repo)

| Location | Evidence |
| --- | --- |
| `README.md` | Commercial Strategy Linkage section links `application-pipeline`, states it supplies near-term income/market feedback/enterprise-service leads for CCE Ring 4, and records CCE-side closure intent for IRF-CCE-038. |
| `docs/superpowers/specs/2026-03-31-cce-commercial-architecture-design.md` | Cross-repo reference header, §8 Application Pipeline Symbiosis, and explicit CCE-side IRF-CCE-038 completion statements. |
| `docs/superpowers/specs/2026-03-31-cce-commercial-architecture-expansion.md` | Pipeline backlink demand/status recorded for CCE-side context. |
| `src/conversation_corpus_engine/surface_exports.py` | Commercial awareness metadata includes `pipeline_repo: "4444J99/application-pipeline"` and relationship fields for surface consumers. |
| `tests/test_surface_exports.py` | Tests assert pipeline linkage in exported metadata. |
| `.claude/plans/irf-cce-039-commercial-implementation-plan.md` | Cross-cutting plan notes pipeline symbiosis/backlink continuity (IRF-CCE-038 context). |

## Remaining work

1. **Reciprocal backlink in application-pipeline:** add an explicit reference to `4444J99/conversation-corpus-engine` in that repo's docs (e.g. README / strategy or system-linkage section) describing CCE as the institutional memory, governed corpus/federation, and product-surface counterpart.
2. **Verify symmetry:** confirm both repos describe the same shared income-surface relationship without contradictory framing.
3. **Issue closeout:** once reciprocal backlink is committed/merged (and pushed per repo workflow), update issue #23 checkboxes/comments and mark IRF-CCE-038 DONE globally if both acceptance criteria are satisfied.

## 2026-11-17 investigation note

- Investigated from `conversation-corpus-engine` (`main`) via `gh issue view 23` and repository search for `application-pipeline`, `commercial awareness`, and `IRF-CCE-038`.
- CCE-side backlink and metadata were already present in docs and code; this plan file records durable in-repo status and remaining external dependency for issue #23.
