# Encrypted publication implementation evidence — 2026-10-19

Status: implementation and synthetic verification complete; real-data acceptance remains open.
Base: `e15d73e416507a4e943f873934808551d74b5578`. Scope: CCE encrypted publication layer and
Limen operational documentation; no merge, live corpus processing, production enrollment or
real-data remote publication. No physical backup was mounted, modified or reverified.

## Verification

- Baseline: `PYTHONPATH=src python -m pytest -q` — 1002 passed, 3 skipped.
- Final implementation: same full suite — 1026 passed, 3 skipped (14.41 seconds).
- New synthetic suite: 24 tests cover key-possession authorization, wrong owner trust, independent
  device restore, newly enrolled devices across rotation, byte-exact files/chunks, incremental
  no-op, recipient removal/rotation, integrity failures, mirror repair and checkpoint-bound HEAD
  recovery, rollback/equivocation, private/Git path guards, symlinks, budget refusal, atomic
  provenance/authorship, malformed input diagnostics and a full existing-archive CLI round trip.
- `python -m ruff check src/ tests/` — passed.
- `python -m ruff format --check src/ tests/` — 131 files already formatted.
- `python -m mypy --follow-imports=silent src/conversation_corpus_engine/archive_sealed*.py` —
  passed, four source modules. A broader import-following invocation reported seven errors in
  unchanged `answering.py`, `federation.py`, `federated_canon.py`, `sharded_collection.py`;
  repository-wide clean typing is not claimed.
- Limen `bash scripts/verify-scoped.sh` — passed all five implicated gates: syntax-changed,
  diff-hygiene, check-docs-manifest, check-docs-exports and check-note-links.

## Review and corrections

An independent reviewer examined the code, spec and tests and reproduced four defects. Each
received a regression that failed before correction and passed afterward:

1. Arbitrary plaintext in an opaque-looking orphan object: fixed by vault/address-bound owner
   signatures on each ciphertext object and authentication of every retained object.
2. Concurrent HEAD recovery overwriting a newer snapshot: fixed by comparing the observed HEAD
   bytes and checkpoint again under the writer lock before mutation.
3. Annotated-tag payload omitted by commit-tree audit: fixed by rejecting non-commit refs and
   permitting only generic main/master/remote-tracking or numeric snapshot ref names.
4. Restore installed before failed checkpoint acceptance: fixed by accepting the authenticated
   checkpoint before installing the restored directory.

A related nonempty-lock-directory concealment case was reproduced and fixed. Source-change
detection compares identity/size/mtime/ctime, excluding read-induced atime changes. Message-level
model metadata wins when available; a conversation-level model remains explicitly contextual,
and no model is assigned as the author of human-role text. Export role does not prove prompt
ownership or original authorship.

Ruling: implementation proceeded inline under the user's explicit implementation request, in a
fresh isolated clone/topic branch. Routine skill approval menus were not treated as a new
execution gate. Scope did not expand to source capture, attestation, owner trust migration or
paid storage. Their absence is documented rather than simulated.

## Acceptance still requiring local access or owner custody

Local corpus selection/inventory/capacity and change-rate measurement; device public-key identity
verification and protected production key/recovery custody; fresh checkpoint transfer; two-device
real-data restore plus archive verification; complete intended-remote history/capacity audit and
exact-ref publication parity. IDs are descriptive labels, not authorization credentials. POSIX
file-key custody is implemented; Windows ACL/hardware-key/attested runtime enforcement is not.

The private memory packet `2026-10-16-v1.1` / projection `2026-10-16-v1.1-r2` and individual ledger
records were not independently retrieved here. Accessible repository architecture and the unchanged
2026-10-18 physical-custody receipt were read. Historical approvals are not fresh authorization.
The referenced workstation-global `/Users/4jp/AGENTS.md` was unavailable in this Linux workspace.
