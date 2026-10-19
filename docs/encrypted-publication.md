# Encrypted archive publication

`cce sealed` is an opt-in publication layer for existing archive files. It does not replace
ingest, the normalized layer, the prompt ledger, federation, the vault, search, graph or site.
Install `pip install -e '.[dev,sealed]'` for development or the `sealed` extra from an exact
reviewed revision for operation. Existing CCE commands do not require cryptography.

## Security boundary

| Plane | Contents | Authority / custody |
|---|---|---|
| Read-only sources | Existing exports and approved personal documents | Existing independent physical backups; never moved or deleted |
| Private normalized store | Conversations, atomic annotations and generated archive surfaces | Existing registered, Git-private store; ignored Limen stores remain supported |
| Private selection | Explicit relative file list; no recursive implicit inclusion | Operator selects publication scope; selecting a file approves all its bytes |
| Ciphertext publication | Opaque objects and signed encrypted `HEAD.cce` | Dedicated Git tree; transport credentials cannot decrypt |
| Enrolled instance | X25519 private key plus app/instance labels | Key possession grants decryption; IDs alone grant nothing |
| Publisher | Externally pinned Ed25519 signing key and enrolled encryption credential | Can authorize recipients, scopes and new signed snapshots |
| Checkpoint / recovery | Vault ID, owner public key, accepted generation and HEAD digest | Outside Git, transferred through a trusted channel; offline owner recovery identity is a recipient |

The current credential adapter writes **unencrypted private-key JSON**, mode 0600 on POSIX,
outside every Git worktree. It is suitable for a protected local runtime, not a claim that a
JSON app ID authenticates a process. Production acceptance requires measured protected key
custody (for example an encrypted local credential store plus explicit recovery custody).
Windows ACL enforcement and hardware-backed/non-exportable keys are not implemented here.
An administrator or process that reads the credential can act as that instance. Attested remote
processing requires a separate verified release policy; this CLI does not implement it.

Public recipient records contain app/instance labels and an encryption public key. Keep these
outside Git too. The publisher's signed HEAD is the enrollment authorization; a recipient file
does not enroll itself. The owner must verify each device key's fingerprint through an
independent trusted channel before selecting it. Every snapshot grants each listed device access
to all selected content. Use a separate vault/trust root for a smaller disclosure scope.

## Reproducible workflow

Paths below are placeholders. `PRIVATE` is a protected directory **outside Git**, `SOURCE` is
the existing private archive root, and `CIPHER` is a new dedicated Git worktree. Do not use a
code repository, old plaintext repository, browser profile or backup device as `CIPHER`.
The CLI generates no commit messages, pushes, repository visibility changes or paid resources.

Prepare two local JSON inputs under `PRIVATE`: `labels.json` with `app_id` and `instance_id`,
and `selection.json`, a JSON array of exact relative paths under `SOURCE`. Include
`manifest-all.json`, the selected normalized provider collections and any desired reading
surfaces if those need exact restoration. Missing source files fail publication. Nothing
outside the selection is silently included, including source attachments and raw exports.

```bash
# Do this on each intended device; use a different protected key for each device.
cce sealed identity --labels "$PRIVATE/labels.json" --key "$PRIVATE/device.key" \
  --recipient "$PRIVATE/device.recipient.json"

# On the owner/publisher device only, initialize one new vault trust root.
cce sealed init --key "$PRIVATE/device.key" --trust "$PRIVATE/trust.json"

# Initialize an empty dedicated repository before the first publication.
git init -b main "$CIPHER"
cce sealed publish --source "$SOURCE" --selection "$PRIVATE/selection.json" \
  --repo "$CIPHER" --key "$PRIVATE/device.key" --trust "$PRIVATE/trust.json" \
  --recipient "$PRIVATE/device.recipient.json" \
  --recipient "$PRIVATE/recovery.recipient.json"
cce sealed verify --repo "$CIPHER" --key "$PRIVATE/device.key" --trust "$PRIVATE/trust.json"
cce sealed status --repo "$CIPHER" --key "$PRIVATE/device.key" --trust "$PRIVATE/trust.json"
cce sealed audit-history --repo "$CIPHER" --key "$PRIVATE/device.key" --trust "$PRIVATE/trust.json"

# Fixed, content-free message; do not put source names, topics or dates in Git metadata.
git -C "$CIPHER" add -- HEAD.cce objects
git -C "$CIPHER" commit -m 'chore: update encrypted archive'
cce sealed audit-history --repo "$CIPHER" --key "$PRIVATE/device.key" --trust "$PRIVATE/trust.json"
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
  --key "$PRIVATE/device.key" --trust "$PRIVATE/trust.json"
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

Normalized `collection-*-conversations.jsonl` files must use `archive-conversation.v2`. Each
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
cce sealed units --repo "$CIPHER" --key "$PRIVATE/device.key" \
  --trust "$PRIVATE/trust.json" --out "$PRIVATE/unit-index.json"
cce sealed unit --repo "$CIPHER" --key "$PRIVATE/device.key" \
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
  --key "$PRIVATE/device.key" --trust "$PRIVATE/trust.json"
```

Repair copies only authenticated matching ciphertext. A missing/corrupt HEAD may be restored from
a mirror only when it exactly matches the private accepted checkpoint. A stale mirror cannot roll
the checkpoint back. Missing unit repair requires an intact ciphertext copy (another clone, an
appropriately checked-out historical revision or independent backup). If no copy exists,
restoration fails without producing a partial destination. Re-publication from original plaintext
can make a new snapshot, but fresh randomized encryption cannot reproduce a lost old ciphertext
object's exact digest. Preserve the existing raw-source backups throughout.

## Capacity and disclosure limits

Encryption does not make GitHub unlimited free storage. As checked on 2026-10-19, GitHub
[recommends repositories ideally below 1 GB and strongly recommends below 5 GB](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github),
[enforces a 2-GB push limit and an approximately 100-MB individual object limit](https://docs.github.com/en/repositories/creating-and-managing-repositories/repository-limits).
The smaller objects here avoid the individual-object ceiling; they do not bypass repository or
account limits. Git LFS has separate [storage/bandwidth billing](https://docs.github.com/en/billing/concepts/product-billing/git-lfs)
and plan-specific limits; it is not enabled, purchased or assumed free by this workflow.

The default 512-MiB budget checks the complete retained ciphertext tree and an upper estimate of
the existing Git object store plus new pending ciphertext. `status` reports these independently.
The live tree is not the complete history size, pack compression/host accounting may differ, and
rotation creates essentially a new copy. Object reuse reduces unchanged snapshots; padding and
the exact-file-plus-atomic projection add overhead. The 2026-10-18 raw-source receipt covers
3,467,404 kilobytes: copying that wholesale would already exceed a single GitHub push and this
default budget. Actual normalized selected bytes and their update cadence remain unmeasured here.
Measure them locally and forecast retained history before deciding whether any no-paid-storage
remote can hold the approved scope. Do not create extra repositories to evade host policy.

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
operations occurred. Existing T7Recovery custody receipts remain intact and are not reverified by
synthetic tests. Private memory packet 2026-10-16-v1.1 / projection r2 and individual ledger records
were not independently retrieved during this implementation; no historical approval is treated as
authority for corpus publication or credential enrollment.
