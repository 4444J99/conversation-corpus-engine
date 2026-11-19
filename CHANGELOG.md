# Changelog

All notable changes to this project should be documented in this file.

This project follows semantic versioning as reflected in git tags and GitHub Releases.

## [Unreleased]

- IRF-CCE-026 (#15): ChatGPT local-session adapter now queries both the active and archived conversation lists (`is_archived=false|true`). Discovery sums both buckets and reports `active_count`/`archived_count`; the bundle importer paginates, tags, and de-duplicates both lists so archived conversations are no longer silently invisible to discovery, import, or the acquisition report. The 633→4 degradation itself remains server-side (session scope) and still requires operator re-auth or the official data export per `playbooks/scope-recovery.md`; `scope_preflight_check()` continues to fail closed.

## [0.3.1] - 2026-11-18

- Release-readiness and packaging metadata improvements tracked under issue #35 (EV Activation Audit).
- Document IRF-CCE-038 cross-repo commercial awareness status (CCE-side backlinks/metadata in place; reciprocal application-pipeline backlog item remains open as #23).
- IRF-CCE-036: add `planned_produces` edges in `seed.yaml` for ORGAN-III vehicles `organvm-iii-ergon/the-actual-news`, `organvm-iii-ergon/classroom-rpg-aetheria`, and `organvm-iii-ergon/consult-consul--console` (#22).
- IRF-CCE-031: populate `inquiry-log.yaml` with CCE research activities and mark tracked in-repo status done for issue #18.

## [0.3.0] - 2026-11-18 (released)

- Released as installable package (sdist + wheel) at https://github.com/4444J99/conversation-corpus-engine/releases/tag/v0.3.0.
- Resolves #35 (EV Activation Audit: ship-soon).
