# Offsite packing and reading-copy status (2026-11-17, run 20261117-104235)

Factual status for issues #105 (Obsidian reading copy), #126 (Apple Notes/Google Docs capture home), and #129 (encrypted offsite copy). No encryption keys, vault repositories, or remote publication were created; no secrets were handled.

## #105 Obsidian reading copy defects

**Code-level fixes in HEAD:** the packaged-vault toolchain already includes the fixes described in the task context (idempotent/safe handling in the packager, open-fence validation/fail-closed behavior, and agent-entry relocation/rebasing). See `scripts/package-obsidian-vault.py`, `scripts/repair-obsidian-inline.py`, and `docs/obsidian-vault.md` (agent-entry links written as `vault/Home.md` relative to a generation are rebased to `Home.md` inside the package; rendered Markdown ending inside an open fence fails packaging/verification).

**Packaged vault path search (dated host check):**
- No canonical packaged "Knowledge Universe" reading vault was identified in targeted searches of the owner's storage locations.
- Packaged-derivative markers (`.cce-package`) were found only in fixture/smoke review and test outputs under private staging locations. These appeared to be non-canonical packaged outputs rather than a user reading vault.
- Library generation vault sources were found in smoke fixtures, but no non-fixture packaged reading copy was identified.

**Conclusion:** the actual packaged "Knowledge Universe" vault path is not present/identifiable on this host from available paths. The vault needs to be regenerated from the current published library generation once that generation/vault source is identified. Regeneration path (per `docs/obsidian-vault.md`): export or locate a `vault/` for the target generation (`cce library export-cards --out DIR` or `cce library export --vault-profile cards|full --out DIR` for a published generation) and run `uv run --extra obsidian python scripts/package-obsidian-vault.py <vault> --agent-entry <generation>/agent-entry.md --source-generation <id> --output <new-destination> ...` into a fresh destination (existing outputs are never replaced). Existing fixture `.cce-package` outputs under titan staging were not modified.

## #129 Encrypted offsite copy

**Already present:**
- `cce sealed` subcommands `identity`, `init`, `fingerprint`, `publish`, `plan`, `verify`, `restore`, `repair`, `unit`, `units`, `status`, `audit-history` (`src/conversation_corpus_engine/archive_sealed_commands.py`, `archive_sealed.py`).
- Sealed storage v2 packing (`archive_sealed_packing.py`, `docs/encrypted-packing-v2.md`): bounded slabs, `zlib` compression before encryption, content-addressed signed shards in `shards/`, and a hard cap of **48 MiB per final signed/padded encrypted shard** (`SHARD_LIMIT`); plan/publish report `shards`, `maximum_shard_bytes`, `packed_plaintext_bytes`, `packed_compressed_bytes`, etc.
- `scripts/vault_canary_check.py` (read-only planted-marker scan via `archive_sealed_canary`).
- Procedure and acceptance gates documented in `docs/encrypted-publication.md` and `docs/custody/derived-store-and-sealed-publication-decisions-20261110.md`.

**Added (this run):** `scripts/pack_offsite_shards.py` — a keyless, no-encryption orchestration helper that measures a source directory (or explicit `--selection` JSON list), computes a deterministic first-fit-decreasing packing plan into shards of at most **48 MiB** by default, writes a `cce.offsite-shard-pack.v1` plan, and can optionally materialize plaintext shard directories for owner-reviewed transport/prep. It does not create credentials, trust roots, vault repos, or ciphertext; for actual encrypted publication use `cce sealed plan/publish --storage-format v2` with owner-provided identity/trust/recipients.

**Ready now (no owner approval required):**
- Source selection review, dry-run size measurement and 48 MiB shard packing plans via `scripts/pack_offsite_shards.py`.
- Encrypted canary/plan/verify/restore workflows against synthetic or owner-provided sources once credentials exist (`cce sealed plan --storage-format v2 ...` measures real packed/encrypted shard sizes without installing a head in plan mode).
- Canary scanning via `scripts/vault_canary_check.py`.

**Requires owner approval / owner-held material (not created here):**
- Creation and custody of protected identities/recipient records, passphrases/unlock material, and trust/checkpoint roots (kept outside Git and outside agent-readable paths; split custody per prior decisions).
- Creation/selection of the dedicated ciphertext vault repository/remote and any paid storage or publication action.
- Real-data sealed publication, history audit/commit/push, and independent-clone restore verification (acceptance gate remains open until completed on authorized data).
- Any bulk upload or network publication (none performed).

## #126 Apple Notes/Google Docs capture home

**What exists:**
- Legacy capture implementations and tests in `legacy/my-knowledge-base/`: `src/sources/apple-notes.ts`, `src/sources/google-docs.ts` (plus tests) and `scripts/export-apple-notes.js`; legacy UI/ops/docs references (e.g. `web-react` source filters, `docs/SOURCE_CAPTURE.md`, `docs/OPERATIONS.md`).
- Current-engine awareness/placeholders: `archive_read_library` includes an `applenotes` family/kind; docs and specs discuss preserving Notes/Google Docs/browser material as explicitly selected exports plus `.atoms.jsonl` annotations (`docs/encrypted-publication.md`, `docs/attributed-knowledge.md`, `docs/superpowers/specs/2026-11-01-knovv-remote-authorship-design.md`, `docs/superpowers/plans/2026-11-01-knovv-remote-authorship.md`). Capture authorization, provider-specific readers, and authorship resolution are not implemented as a complete production capture home in this engine.
- KNOVV app/plan text notes browser-history refresh, Notes, and Google Docs ingestion as continuing through existing adapters/lanes.

**Recommendation (owner decision required):** keep `conversation-corpus-engine` as the canonical normalization/attribution/publication substrate and avoid creating a new repo without an explicit owner choice. Preferred path: **port** the viable, reviewed capture logic from `legacy/my-knowledge-base` into a dedicated, maintained capture surface owned alongside the engine (or a clearly-scoped package/module) with explicit auth, enumeration, partial-failure accounting, stable IDs/revisions, and `.atoms.jsonl`/provenance output that feeds the engine's existing selection and sealing flows. **Unarchive/revive** legacy code only as reference or a short-lived bridge if porting is blocked; do not treat legacy as the long-term home. **New repo** only if the owner wants capture isolated operationally (credentials/runtime) from the engine, with a defined contract (export format + annotations) back into CCE. No repositories were created or unarchived.

**Blockers/needs owner decision:** target home (in-repo module vs separate owned repo), credential/runtime custody for Apple Notes/Google APIs, and scope/priority versus KNOVV adapter work.
