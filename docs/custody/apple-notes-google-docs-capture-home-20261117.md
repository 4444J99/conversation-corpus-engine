# Apple Notes and Google Docs capture home (2026-11-17)

Status note for [#126](https://github.com/4444J99/conversation-corpus-engine/issues/126): decide the new home for Apple Notes and Google Docs capture after `my-knowledge-base` was archived read-only (2026-11-05).

## Decision (recommended)

**Adopt `conversation-corpus-engine` as the canonical capture home** for producing notes and document roots that this engine reads.

Rationale:
- CCE already owns the normalization, reader, identity, authorship, manifest, and sealed/publication substrate that capture must feed. Keeping capture adjacent to that contract reduces drift.
- A working, read-only capture implementation already exists in-tree (`notes_capture.py`, `cce library capture-notes-docs`, documents-reader identity handling, tests, and operator docs), so option 2 ("port capture into this engine as Python") is substantially realized for the local/export-based path.
- Option 1 (a small new private tool repo) remains viable only if the owner later needs to isolate Google/Apple credentials or macOS-only runtime from this engine; in that case the contract should stay the CCE document root + manifest/identity/index format produced today, not a divergent schema.
- Option 3 (unarchive the old repo) is not recommended as a long-term home. The archived repo remains a read-only reference; its hardening branch `claude/knovv-capture-hardening-20261105` (draft PR 150 there) may contain ideas worth reviewing, but capture should not depend on an archived repository.

**Owner action needed:** explicitly ratify CCE as home (or choose isolation into a separate private repo) so future capture work has a clear owner/repo.

## What exists now (in CCE)

- **Apple Notes capture:** reads `NoteStore.sqlite` (+ `-wal`/`-shm`) via read-only copied descriptors in a private temp directory, compares before/after signatures with retries, decodes note bodies with a bounded gzip/protobuf reader, writes one stable-id Markdown file per note plus `notes-index.jsonl`, and emits a `documents` manifest entry (`id: apple-notes`, `identities: notes-index.jsonl`, provider `applenotes`). No application is opened and no source is written. Implementation: `src/conversation_corpus_engine/notes_capture.py`; CLI: `cce library capture-notes-docs`.
- **Google Drive desktop shortcuts:** optional `--drive-root` capture writes metadata-only shortcut notes and ordinary Drive-file manifest entries for local files.
- **Google Docs (export-based):** optional `--google-docs-export` ingests folders of Docs already exported as Markdown/text (e.g. via `rclone copy ... --drive-export-formats md`), with bounded/fail-closed handling for symlinks, oversize/non-UTF-8/empty/duplicate-path files, and optional `--google-docs-ids` mapping from `rclone lsjson` to preserve stable `googledrive` identities. Writes `gdocs/` files + `gdocs-index.jsonl` and manifest entry `id: google-docs`.
- **Reader/identity integration:** `archive_read_library`/`library_documents` recognize `applenotes` and `googledrive` identities so refreshed captures join existing records as revisions rather than duplicates.
- **Docs/tests:** `docs/knowledge-library.md` documents capture behavior and safety properties; `tests/test_notes_capture.py`, `tests/test_archive_read_library.py`, and related tests cover synthetic stores/exports and identity behavior.

## Remaining work / gaps

1. **Owner decision recorded in issue tracker:** close or update #126 with the chosen home and any scope boundaries.
2. **Real-world validation:** run `cce library capture-notes-docs` against the owner's actual Notes database and Drive/export inputs on macOS, review the receipt/findings/counts, and confirm ingest into the target library generation is correct (not performed here).
3. **Live Google Docs fetch/enumeration:** CCE currently consumes *exported* Doc text plus optional id lists; it does not perform Google OAuth/API enumeration or download. If live capture is required, implement it as an explicitly authorized, fail-closed adapter that produces the same export/id inputs or the same CCE capture outputs (credential custody must be owner-approved and kept out of Git/agent-readable paths).
4. **Archived hardening review:** compare `my-knowledge-base` branch `claude/knovv-capture-hardening-20261105` for any still-relevant safeguards (token/file hygiene, Docs scan fail-closed semantics) and port only what applies to the current Python/read-only design.
5. **Authorship/selection workflow:** per-note authorship remains rule-derived plus review hints (`pasted_text_hint` in sidecar, markers preserved); continue treating capture as explicitly selected exports feeding attribution/publication flows rather than automatic publication.
6. **Legacy retirement clarity:** `legacy/my-knowledge-base/` in this repo is a retired snapshot/reference; keep it clearly non-canonical.

## Not done in this run

- No repositories created or unarchived.
- No real Notes/Google data accessed; no credentials or network/API calls used.
- No owner decision can be made unilaterally; this document records the recommended home and concrete remaining work.
