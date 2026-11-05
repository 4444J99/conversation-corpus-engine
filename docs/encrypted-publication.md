# Encrypted archive publication

`cce sealed` is an opt-in publication layer for existing archive files. It does not replace
ingest, the normalized layer, the prompt ledger, federation, the vault, search, graph or site.
Install `pip install -e '.[dev,sealed]'` for development or the `sealed` extra from an exact
reviewed revision for operation. Existing CCE commands do not require cryptography.

## Security boundary

| Plane | Contents | Authority / custody |
|---|---|---|
| Read-only sources | Existing exports and approved personal documents | Existing independent physical backups; never moved or deleted |
| Private normalized store | Conversations, atomic annotations and generated archive surfaces | Existing registered, Git-private store; ignored local stores remain supported |
| Private selection | Explicit relative file list; no recursive implicit inclusion | Operator selects publication scope; selecting a file approves all its bytes |
| Ciphertext publication | Opaque objects and signed encrypted `HEAD.cce` | Dedicated Git tree; transport credentials cannot decrypt |
| Enrolled instance | X25519 private key plus app/instance labels | Key possession grants decryption; IDs alone grant nothing |
| Publisher | Externally pinned Ed25519 signing key and enrolled encryption credential | Can authorize recipients, scopes and new signed snapshots |
| Checkpoint / recovery | Vault ID, owner public key, accepted generation and HEAD digest | Outside Git, transferred through a trusted channel; offline owner recovery identity is a recipient |

New credentials use `cce-protected-identity.v1`: a random 16-byte salt, fixed Scrypt
(N=32768, r=8, p=1, 32-byte key) and authenticated ChaCha20-Poly1305 encryption. Private key
JSON exists only after explicit unlocking in process memory. Publication remains `cce-sealed.v1`;
its format, algorithms and recipient semantics are unchanged. Credential files are mode 0600 on
POSIX and outside Git. Supply exact passphrase bytes from a mode-0600 private file with
`--passphrase-file`, or a protected producer through `--passphrase-stdin`. The CLI has no secret
argument and does not print keys or passphrases. Keep the passphrase separate from its credential;
this software cannot protect a key when an attacker can read both or inspect an unlocked process.
A strong passphrase and protected recovery custody remain operator responsibilities.

Legacy unencrypted JSON is available only through `--legacy-plaintext-key` and the explicitly
named `create_legacy_identity` / `load_legacy_identity` Python compatibility functions. New CLI
calls require exactly one unlock route. Do not use the legacy route as evidence of protected
at-rest custody. Hardware nonexportability, binary/process attestation, Windows ACL enforcement,
and production device enrollment are not implemented here.

Public recipient records contain app/instance labels and an encryption public key. Keep these
outside Git too. The publisher's signed HEAD is the enrollment authorization; a recipient file
does not enroll itself. The owner must verify each device key's fingerprint through an
independent trusted channel before selecting it. Every snapshot grants each listed device access
to all selected content. Use a separate vault/trust root for a smaller disclosure scope.

Print a recipient's fingerprint with the read-only `fingerprint` command. It needs no unlock,
writes nothing and accepts only a public recipient record (exactly `app_id`, `instance_id` and a
lowercase 64-hex `encryption_public`); a credential file, a malformed record or any extra field is
refused with `invalid-recipient` and nothing from the file is echoed:

```bash
cce sealed fingerprint --recipient "$PRIVATE/device.recipient.json"
# {"algorithm": "sha256", "fingerprint": "<64 hex characters>"}
```

The fingerprint is SHA-256 over `cce-sealed.v1:recipient-fingerprint:` followed by the canonical
JSON of the recipient record, so it covers the public key **and** its app/instance labels: a
changed label or key yields a different value. The device owner runs this on the device and reads
the value to the publisher over an independent channel; the publisher runs it on the recipient
file they are about to enroll and compares the two before running `publish`. The labels
themselves are not printed.

## Reproducible workflow

Paths below are placeholders. `PRIVATE` is a protected directory **outside Git**, `SOURCE` is
the existing private archive root, and `CIPHER` is a new dedicated Git worktree. Do not use a
code repository, old plaintext repository, browser profile or backup device as `CIPHER`.
The CLI generates no commit messages, pushes, repository visibility changes or paid resources.

Prepare a protected passphrase file `unlock` containing exact bytes (including any newline),
mode 0600, then prepare two local JSON inputs under `PRIVATE`: `labels.json` with `app_id` and `instance_id`,
and `selection.json`, a JSON array of exact relative paths under `SOURCE`. Include
`manifest-all.json`, the selected normalized provider month/rollover shards and any desired reading
surfaces if those need exact restoration. Missing source files fail publication. Nothing
outside the selection is silently included, including source attachments and raw exports.

```bash
# Do this on each intended device; use a different protected key for each device.
cce sealed identity --labels "$PRIVATE/labels.json" --key "$PRIVATE/device.key" --passphrase-file "$PRIVATE/unlock" \
  --recipient "$PRIVATE/device.recipient.json"

# On the owner/publisher device only, initialize one new vault trust root.
cce sealed init --key "$PRIVATE/device.key" --passphrase-file "$PRIVATE/unlock" --trust "$PRIVATE/trust.json"

# Initialize an empty dedicated repository before the first publication.
git init -b main "$CIPHER"
cce sealed publish --source "$SOURCE" --selection "$PRIVATE/selection.json" \
  --repo "$CIPHER" --key "$PRIVATE/device.key" --passphrase-file "$PRIVATE/unlock" --trust "$PRIVATE/trust.json" \
  --recipient "$PRIVATE/device.recipient.json" \
  --recipient "$PRIVATE/recovery.recipient.json"
cce sealed verify --repo "$CIPHER" --key "$PRIVATE/device.key" --passphrase-file "$PRIVATE/unlock" --trust "$PRIVATE/trust.json"
cce sealed status --repo "$CIPHER" --key "$PRIVATE/device.key" --passphrase-file "$PRIVATE/unlock" --trust "$PRIVATE/trust.json"
cce sealed audit-history --repo "$CIPHER" --key "$PRIVATE/device.key" --passphrase-file "$PRIVATE/unlock" --trust "$PRIVATE/trust.json"

# Fixed, content-free message; do not put source names, topics or dates in Git metadata.
git -C "$CIPHER" add -- HEAD.cce objects
git -C "$CIPHER" commit -m 'chore: update encrypted archive'
cce sealed audit-history --repo "$CIPHER" --key "$PRIVATE/device.key" --passphrase-file "$PRIVATE/unlock" --trust "$PRIVATE/trust.json"
```

Repeat publish/verify/status/audit/commit/audit for an incremental update. Commit each accepted
snapshot before the next update or rotation: history audit accounts for retained old objects
through their signed historical manifests. Publishing repeatedly before initializing/committing
Git is valid for local restore, but may leave retained objects that history audit cannot account
for. It fails closed rather than deleting them. Unchanged content and recipient policy leaves
ciphertext and HEAD byte-identical; the CLI reports `changed: false` (no new Git commit needed).
Secure encryption is randomized across fresh vaults and rotations; reproducible workflow does
not mean deterministic ciphertext from plaintext.

Before any remote push, independently confirm the remote identity, repository policy and measured
capacity, then perform the normal Git push and verify remote/local OID parity. No real-data
publication was performed during implementation. Do not place decryption keys in GitHub secrets
or hosted Actions just to test transport. Disable content previews/indexers on plaintext staging
where the host requires it; this CLI does not configure other applications.

For a new device, create its credential locally, verify its recipient record with the owner,
and republish with **all** desired recipients and `--rotate`. The CLI requires the publisher
to remain a recipient. Transfer the updated trust/checkpoint file to the device via a trusted
channel; clone ciphertext using ordinary Git credentials, then restore:

```bash
cce sealed restore --repo "$CIPHER" --out "$PRIVATE/restored" \
  --key "$PRIVATE/device.key" --passphrase-file "$PRIVATE/unlock" --trust "$PRIVATE/trust.json"
```

`--out` must not exist and must be outside every Git worktree. All referenced objects and complete
file digests are checked before the destination is renamed into place. Files are mode 0600,
directories 0700 on POSIX; source permissions/ACLs/xattrs are not a file-content archive contract.
Restore preserves exact bytes and relative file paths, including empty files and binary bytes.
Register the reconstructed archive with the existing corpus-store interface and run the existing
archive verification before using its reading interfaces. Source-root locators inside restored
metadata remain historical provenance; restoration does not assert those source paths exist on
the new device. This is selected-file reconstruction, not a physical-source backup replacement.

## Atomic units and authorship

Normalized `shard-<provider>-<yyyymm>[-NNN].jsonl`, `undated` shards (including bounded
fan-out subfolders), and legacy `collection-*-conversations.jsonl` files must use `archive-conversation.v2`. Each
message, including an alternate message, becomes `cce-knowledge-atom.v1` with its original message
payload, source path, conversation ID, branch/index, reader provenance, timestamps and derivation
links. The unit is message-sized: a durable addressable substrate for reviewed Zettelkasten notes,
not a claim that every message expresses exactly one idea. Semantic splitting is a later reviewed
derivation; keep links to the message and selected source file.

Role, channel, model/provider and `typed_chars` preserve evidence about who produced which span.
`typed_chars` separates a user-role typed prefix from appended attachment text; the full original
message remains available. A vendor `user` role **does not prove** the operator wrote the text,
owns every embedded quotation, or owns the prompt. The automatic projection sets
`original_writing` and `prompt_owner` to `unverified`; no imported text gains operator authority.

For an explicit human-authored note, prompt attribution or mixed-author document, include a
selected file ending `.atoms.jsonl`. Each row must have `schema: cce-knowledge-atom.v1`, a stable
`id`, `text`, `authorship`, `source`, `timestamps`, and `derived_from`. These fields are preserved
exactly. `authorship` should record claimed author, human/prompt/model/quote category, evidence,
status and per-span ranges for mixed writing; unknown authors remain unknown. An assertion is
not promoted to independently verified authorship. Use separate rows or span annotations for
different AI systems and quoted material. Editing notes should preserve IDs; new derivations
get new IDs with `derived_from` edges.

Browser history/bookmarks/reading lists, iOS Notes and Google Docs can already be preserved as
explicitly selected export files (plus `.atoms.jsonl` annotations). Their capture authorization,
provider-specific readers and authorship resolution are **not implemented by this change**.
Do not infer all Notes text is original writing or all browser entries were read. Preserve export
format, source timestamps and uncertainty in the annotations. Raw-byte recipes ensure unknown
fields are recoverable even before a reader exists.

```bash
# Unit IDs and unit text are sensitive; write them to private files, never stdout/argv.
cce sealed units --repo "$CIPHER" --key "$PRIVATE/device.key" --passphrase-file "$PRIVATE/unlock" \
  --trust "$PRIVATE/trust.json" --out "$PRIVATE/unit-index.json"
cce sealed unit --repo "$CIPHER" --key "$PRIVATE/device.key" --passphrase-file "$PRIVATE/unlock" \
  --trust "$PRIVATE/trust.json" --id-file "$PRIVATE/unit-id.txt" --out "$PRIVATE/unit.json"
```

## Format and integrity

`HEAD.cce` is canonical JSON signed by Ed25519. It exposes only a random vault identifier,
format, generation, previous ciphertext HEAD digest, randomized wrapped-key slots, encrypted
manifest and signature. The owner public key is pinned outside the repository. X25519 ephemeral
key agreement, HKDF-SHA256 and ChaCha20-Poly1305 wrap one random 256-bit epoch secret for each
recipient; authenticated context binds vault/generation and the encrypted recipient record binds
the app/instance labels to its public key. The encrypted manifest contains file recipes, plaintext
source digests, private unit IDs and paths, recipient records and ciphertext digests.

Object names are `objects/<first-two-hex>/<64-hex>`: HMAC-SHA256 of plaintext under a
domain-separated epoch key, **never unkeyed plaintext hashes**. Each object uses a random 96-bit
nonce, authenticated format/purpose/address context and 4096-byte padding buckets with a private
length prefix. Reuse requires validated ciphertext. ChaCha20-Poly1305 and HMAC use different HKDF
domains. Each object also carries an owner signature bound to the vault, opaque address and
ciphertext digest. Every retained object is authenticated, including old epochs and objects not
referenced by the current manifest; an opaque filename cannot conceal arbitrary plaintext.
File chunks are 512 KiB; individual unit plaintext is capped at 4 MiB, manifest plaintext
at 16 MiB, recipients at 64. A too-large unit fails the whole publication rather than truncating.
The implementation retains pending ciphertext in memory up to its publication budget; plan
memory headroom and freeze selected files during publication. File changes detected while being
read fail the run. Source roots and output roots cannot overlap; symlinks are refused.

`verify` authenticates HEAD, every referenced object, all reconstruction recipes and unit IDs.
Private checkpoints reject older generations and same-generation forks. First enrollment needs
a fresh owner-supplied checkpoint: an old clone plus an old checkpoint cannot prove freshness.
The previous-HEAD digest is signed lineage evidence, not a consensus or online freshness service.
Serialize writers; lock directories fail closed and are not automatically stolen. An interrupted
write may leave unreferenced ciphertext or a lock; investigate it under custody before manually
clearing anything. The CLI does not delete or garbage-collect objects or history.

`audit-history` examines every reachable commit/ref, requires the fixed content-free commit
message above, validates historical signed manifests, and checks historical/current object bytes
against authenticated ciphertext digests. Run it as an owner identity that can decrypt historical
epochs; a newly enrolled device may lack old-epoch access. It rejects unknown paths and historical
plaintext even if deleted from the current worktree. It does not inspect Git reflogs, unreachable
objects, provider caches, hidden server refs, branch-name privacy, or other copies. Begin with a
fresh dedicated repository; this is an audit, **not sanitization** of a formerly plaintext repo.
The audit is bounded: it inspects at most 10,000 commits, gives each Git subprocess 30 seconds,
and stops with `history-audit-limit` once the Git output it has read (and the hash metadata it
retains, one entry per 4 KiB of cap) exceeds an aggregate cap. The cap defaults to 512 MiB and
is set with `audit-history --budget BYTES`; `publish` and `plan` take the same cap for their
pre-publication history audit as `--audit-budget BYTES`. It is independent of the storage
`--budget`, so a small storage forecast budget does not abort history auditing. Raise the cap
for a long history rather than disabling it; a negative value is refused (`invalid-budget`).
Only `main`/`master` branches, matching remote-tracking refs and lightweight `snapshot-N` tags
are accepted. Annotated tags, notes, replace refs and arbitrary ref names are refused; annotated
tag messages can otherwise carry plaintext outside the commit-tree audit.

## Rotation and recovery

Changing recipients requires `publish --rotate` with the complete desired list; it re-encrypts
all currently selected files/units under a fresh random epoch. Ciphertext in old Git history stays
available to old recipients. Removal cannot revoke their already obtained plaintext, keys or old
history. Owner signing-key replacement and replacement of the publisher's historical encryption
identity require a separately reviewed trust-root migration; `--rotate` rotates the data epoch.

Enroll an offline recovery identity at the start, protect its credential independently and test
restoration from a second physical custody location. Never rely on a GitHub token as a decryption
recovery key. Loss of all enrolled private keys is unrecoverable. An owner can add recipients only
while they possess an authorized encryption credential and the signing credential.

```bash
cce sealed repair --repo "$CIPHER" --mirror "$INDEPENDENT_CIPHER_COPY" \
  --key "$PRIVATE/device.key" --passphrase-file "$PRIVATE/unlock" --trust "$PRIVATE/trust.json"
```

Repair copies only authenticated matching ciphertext. A missing/corrupt HEAD may be restored from
a mirror only when it exactly matches the private accepted checkpoint. A stale mirror cannot roll
the checkpoint back. Missing unit repair requires an intact ciphertext copy (another clone, an
appropriately checked-out historical revision or independent backup). If no copy exists,
restoration fails without producing a partial destination. Re-publication from original plaintext
can make a new snapshot, but fresh randomized encryption cannot reproduce a lost old ciphertext
object's exact digest. Preserve the existing raw-source backups throughout.

## Capacity and disclosure limits

Encryption does not make GitHub unlimited free storage. Rechecked on 2026-11-01, GitHub
[recommends repositories ideally below 1 GB and strongly recommends below 5 GB](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github),
[enforces a 2-GB push limit and an approximately 100-MB individual object limit](https://docs.github.com/en/repositories/creating-and-managing-repositories/repository-limits).
The smaller objects here avoid the individual-object ceiling; they do not bypass repository or
account limits. Git LFS has separate [storage/bandwidth billing](https://docs.github.com/en/billing/concepts/product-billing/git-lfs)
and plan-specific limits; it is not enabled, purchased or assumed free by this workflow.

Run a read-only forecast with the same selection and recipients before publishing:

```bash
cce sealed plan --source "$SOURCE" --selection "$PRIVATE/selection.json" \
  --repo "$CIPHER" --key "$PRIVATE/device.key" --passphrase-file "$PRIVATE/unlock" \
  --trust "$PRIVATE/trust.json" --recipient "$PRIVATE/device.recipient.json" \
  --recipient "$PRIVATE/recovery.recipient.json"
```

`plan` validates current ciphertext and history and reports selected plaintext bytes, current
ciphertext bytes, retained object bytes, new pending ciphertext bytes, existing Git object-store
bytes, a Git metadata/allocation reserve, planned tree/history/combined bytes, budget and `fits`.
It does not create the destination,
write ciphertext or advance the private checkpoint. Unchanged inputs produce identical counts;
`--rotate` forecasts a new epoch. This is an observation, not a storage reservation.

The default 512-MiB budget applies to the sum of the complete retained ciphertext tree and a
conservative next Git snapshot estimate (existing Git object-store bytes, pending object and
new HEAD bytes, plus 1 MiB and 4 KiB per retained/pending object and HEAD for loose-object
allocation and rewritten tree/commit metadata). `publish` rechecks it before installing ciphertext.
A no-op still checks current
storage against the budget. `status` reports current tree and Git storage independently. Pending
publication ciphertext remains bounded by the budget; planning retains lengths rather than
pending ciphertext. Run `status` again after the required Git commit and compare its tree plus Git
object-store bytes to the approved budget before pushing. Host accounting, unusually large
allocation units or signed commit metadata, remote retention, transfer limits and update cadence
require separate acceptance measurements. Encryption padding and the
exact-file-plus-atom projection add overhead, and rotation retains previous epochs. No paid quota,
LFS purchase, extra repositories or unlimited free hosting are assumed.

Residual observable information includes ciphertext count/size buckets, equality/reuse, change
patterns, recipient count, random vault identity, generations, Git author and commit times.
No source text, plaintext digests, paths, source IDs, app labels or source timestamps are emitted
in publication files or CLI diagnostics. Other software, shell history, host swap/backups and
credential custody are outside this program's enforcement. Generic diagnostics deliberately omit
exception details. The layer provides neither encrypted remote search nor secure deletion.

## Real-data acceptance remains separate

Required local evidence: current source/backup attachment and custody verification; actual selected
corpus inventory and capacity forecast; reviewed per-file publication scope; resolved authorship
annotations where claimed; protected owner/device/recovery keys and verified enrollment; current
checkpoint transfer; two-device restore and existing archive verification; complete reachable Git
history audit; intended private remote identity and exact-ref parity. No receipt here claims those
operations occurred. Existing physical-backup custody evidence remains independent of these
synthetic tests. Historical packets are context, not permission for publication or enrollment.

On the authorized source host, retain a private acceptance receipt with: exact engine commit;
source inventory and freshness/backup custody; reviewed selection digest and selected byte count;
`sealed plan` JSON for first publication, incremental updates and rotation against actual free
quota; protected owner/device/recovery credential custody and independently verified recipient
fingerprints; publication/verification/status/history-audit JSON; exact pushed ref OID and a fresh
independent clone OID; checkpoint digest transferred through a trusted channel; restored per-file
SHA-256 equality for every selected file; positive unit count and existing archive verification on
the reconstructed store. Exercise wrong-key, ciphertext tamper, stale-checkpoint and restore
refusal using disposable copies. Keep this evidence private and content-free in public reports.
The existing source host and protected owner keys are unavailable in this environment, so these
live acceptance gates remain outstanding.
