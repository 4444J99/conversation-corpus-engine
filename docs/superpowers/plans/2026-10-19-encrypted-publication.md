# Encrypted Publication Implementation Plan

**Goal:** Add synthetic-tested, opt-in encrypted publication/restoration to CCE.
**Architecture:** Separate cryptographic envelopes, private-unit projection, object lifecycle and
thin CLI wiring. Limen documents orchestration; CCE remains the archive implementation owner.
**Tech stack:** Python 3.11+, optional cryptography>=46,<51, pytest.
**Spec:** ../specs/2026-10-19-encrypted-publication.md

## Global constraints

No real corpus, production credentials, automatic publication, backup mutation or interface removal.
Use an isolated topic branch in this session's new clone; implement inline under the user's request.

## Review focus

Malicious paths/symlinks; wrong credentials or spoofed labels; truncated/tampered storage;
rollback and epoch revocation; plaintext leaks through error handling or Git-visible output.

## Task 1: Authenticated encrypted object lifecycle

- [x] Write tests in tests/test_archive_sealed.py for exact reconstruction, multi-device access,
  rejected impostors, incremental reuse, rotation, tampering, repair and private-output guards.
- [x] Run tests and observe missing implementation failure.
- [x] Implement archive_sealed_crypto.py (keys, envelopes, padding, signatures),
  archive_sealed.py (publish/open/verify/restore/repair, checkpoints and storage budget).
- [x] Run the targeted tests, lint and type checks; commit tested code.

## Task 2: Atomic projection and CLI

- [x] Write tests for main/alternate message atoms, uncertain prompt ownership, preserved explicit
  authorship annotations and sanitized CLI errors.
- [x] Implement archive_sealed_units.py and archive_sealed_commands.py, add lazy CLI dispatch,
  optional dependency and CI installation. Projection consumes selected normalized files only.
- [x] Run tests and existing archive/CLI regressions. Record evidence and commit.

## Task 3: Operational integration and review

- [x] Document commands, format, custody/recovery, current GitHub limits and live acceptance gates.
- [x] Add a linked Limen runbook preserving the existing physical-backup receipt and interface.
- [x] Run the CCE suite and applicable Limen scoped gate; obtain a whole-change review.
- [ ] Publish topic branches/draft PRs with exact-head evidence; do not merge or publish real data.
