# Compressed sealed storage, version 2

`cce-sealed.v2` is an opt-in storage layout for the existing sealed publication
layer. It packs logical file chunks and projected atoms into bounded slabs,
compresses each slab before encryption, and stores content-addressed ciphertext
shards. The cryptographic algorithms, protected-credential format, recipient
enrollment, app/instance label binding and external trust/checkpoint semantics
are unchanged. This remains a local key-possession boundary, not application
or process attestation.

The implementation is in `archive_sealed_packing.py`, with version dispatch and
coordinated verification, restoration, repair, capacity measurement and history
audit in `archive_sealed.py`. No command in this change creates a remote, makes
a Git commit, pushes data, purchases storage or moves source files.

## Selection and compatibility

New vaults and existing v1 callers retain the v1 default. Select v2 explicitly
with `--storage-format v2` on `sealed plan` or `sealed publish`. When an existing
vault is v2, omitting the flag preserves v2. An explicit v1 downgrade is rejected
with `format-downgrade-refused`; an unknown format is rejected.

New readers support both layouts. V2 changes both the signed `HEAD.cce` format
tag and the encrypted manifest format tag to `cce-sealed.v2`; those tags must
agree. It also introduces a `shards/` directory. Existing v1 readers accept
neither that layout nor the v2 header, so they fail closed instead of interpreting
packed bytes as raw file chunks. The v1 encryption format and its existing tests
are retained.

An explicit v1-to-v2 publication reads the selected originals, packs them into
v2 shards, and installs a new generation. It retains all prior signed v1 objects
and all existing Git history. Retention is counted in the capacity budget. The
old checkpoint protections still apply; retained historical files do not grant
permission to roll back a current checkpoint.

Every selected source file is reconstructed byte for byte from its logical
chunk recipe, including binary data, line endings, empty files, unknown JSON
fields and whitespace. Normalized conversation and explicit atom files also
produce their existing atomic projection. The file recipe and the projection
are separate representations, as in v1. No selected file or atom is silently
truncated or dropped when a bound is exceeded.

## Bounds and memory

| Item | Bound or behavior |
| --- | --- |
| Raw file chunk | Existing 512-KiB default |
| Logical object, including a canonical atom | Existing 4-MiB maximum |
| One uncompressed framed slab | 32 MiB |
| One final signed, padded and encrypted shard | At most 48 MiB, measured after encoding |
| Source JSONL row used for atom projection | 64 MiB by default, independently configurable with `--max-projection-line-bytes` |
| Encrypted manifest's decoded JSON | Existing 16-MiB maximum |
| Total retained tree plus forecast Git storage | Existing explicit storage budget and capacity checks |

The source-row limit is separate from the slab limit: a conversation row can be
larger than a slab while its individual messages fit the logical-object bound.
The archive producer can mark oversized conversation rows; an operator can
choose a larger explicit projection-line bound when reviewing such input.
The v2 reader uses bounded `readline` before JSON parsing, counting bytes and
the line terminator. Over-bound input fails with `projection-line-too-large`
without installing a new HEAD. V1 retains its existing default parser behavior
and rejects a supplied v2-only override rather than ignoring it.

Publication keeps one active plaintext slab and the current compression,
encryption and signature buffers. Pending ciphertext is written to a private
temporary directory; the pending map holds paths and sizes, not every ciphertext
body. Normal exception cleanup removes that temporary directory. Abrupt process
termination can leave private temporary ciphertext, but no plaintext slab or
credential is written there. Source JSON parsing and the bounded manifest also
need memory, so 32 MiB is the slab size, not a claim about the whole process's
resident memory.

Readers retain one decoded slab. Bulk object and atom validation groups objects
by shard instead of traversing canonically sorted object hashes and repeatedly
decoding a large slab for adjacent tiny atoms. Tests count decompressions on a
many-atom fixture. File reconstruction follows the original chunk recipes and
checks each complete file's SHA-256 before accepting the restore.

## Manifest and addressing

The signed outer head has the same fields and meanings as v1: format, vault ID,
generation, previous-head digest, recipient envelopes, encrypted manifest and
signature. The decrypted v2 manifest has exactly these top-level fields:

| Field | Contents |
| --- | --- |
| `format` | `cce-sealed.v2` |
| `files` | Private relative source names mapped to ordered logical object IDs and the exact-file SHA-256 |
| `atoms` | Private atom IDs mapped to logical object IDs |
| `objects` | Logical object ID mapped to shard ID, byte offset and byte length |
| `shards` | Shard ID mapped to ciphertext SHA-256, codec, compressed byte count and decoded byte count |
| `recipients` | Existing approved public-recipient records, inside the encrypted manifest |

A logical object's ID is the existing keyed HMAC over its uncompressed bytes.
Within a slab, every object has an eight-byte big-endian length followed by the
object bytes. The manifest offset points after the length frame. Frames occupy
the slab contiguously; overlaps, gaps, unmapped bytes, negative offsets, boolean
lengths, over-bound lengths and missing shard references are rejected before
decompression. The framing also prevents different object partitions of the
same concatenated text from ambiguously identifying a slab.

The shard ID is the keyed HMAC over the complete framed plaintext slab. Public
filenames are `shards/<first-two-hex>/<64-hex-id>`. They contain no plaintext
source digest, title or path. Equality and reuse remain observable within a
vault, as documented for v1. The compressed codec is an explicit `zlib` value
inside the authenticated encrypted manifest; unknown codecs are refused.

Compression does not change logical identities. An unchanged publication
reuses existing ciphertext and is a no-op. Revisiting a prior selection can
reuse the original signed ciphertext after authenticating and decoding it.
Rotation uses a new secret, producing new addresses while retaining previous
epochs. Capacity accounting includes those retained files.

## Cryptographic context and decoding

V2 reuses the existing X25519 recipient envelopes, HKDF derivation,
ChaCha20-Poly1305 encryption, Ed25519 signatures, keyed HMAC addressing and
4-KiB padding primitives. The primitive/KDF version labels remain unchanged;
the storage version is separated by authenticated context:

| Payload | Authenticated encryption context |
| --- | --- |
| Recipient envelope | `cce-sealed.v2:<vault>:<generation>` |
| Manifest | `cce-sealed.v2:<vault>:<generation>:manifest` |
| Packed shard | `cce-sealed.v2:shard:<shard-id>` |

Retained ciphertext uses the existing detached object-signature payload binding
the vault, address and ciphertext digest. History audit distinguishes `objects/`
and `shards/` in its authenticated allowlist so an alias between layouts cannot
substitute one storage entry for another.

Before decoding a shard, a reader opens a regular file without following the
final symlink and checks the actual byte cap, ciphertext digest and authenticated
encryption. Nonregular mirror entries, including FIFOs, are refused through a
nonblocking descriptor check. The decrypted compressed length must equal the
manifest length. Decompression allows at most the declared decoded length plus
one byte, and requires the exact declared length, end of stream, no unused
trailing bytes and no unconsumed input. Truncated streams, concatenated/trailing
payloads and decompression expansion beyond the bound are rejected. Finally,
the reader verifies the slab HMAC, each length frame and each logical object's
HMAC before returning its bytes.

## Publication, repair and history

All new ciphertext is staged and capacity checked before final paths are
installed. Publication tracks a shard as soon as its atomic write becomes
visible, including the case where a later durability operation fails.
`HEAD.cce` is installed last. A pre-HEAD failure removes only newly created,
unreferenced ciphertext; previously retained objects and shards are preserved.
If HEAD became visible, cleanup preserves the new generation so a resumed
reader can verify it. Tests exercise failures both before and after HEAD and
after a shard becomes visible but before its write returns.

Repair validates required mirror shards against the authenticated target
manifest, including object offsets and HMACs, then privately stages replacement
ciphertext. It rechecks the live HEAD and checkpoint under the publisher lock
before installing replacements. A corrupt or nonregular mirror fails without
modifying the live tree. Restoration remains a separate atomic operation into
a new, explicit private destination.

History audit recognizes both layouts, enforces their individual size bounds,
authenticates retained signatures and ties every reachable blob to an allowed
historical manifest. The existing restrictions on ref names, commit messages,
rollback and inspection budgets remain in force. V1 history through an upgrade,
multiple v2 generations and rotation are tested together.

## Measurement and acceptance

`sealed plan --storage-format v2` executes packing and cryptographic size
measurement without writing staged ciphertext, creating the destination or
advancing the checkpoint. Its additional fields are:

| Field | Meaning |
| --- | --- |
| `storage_format` | Selected versioned format |
| `shards` | Number of shards referenced by this generation |
| `maximum_shard_bytes` | Largest actual signed/padded ciphertext shard |
| `packed_plaintext_bytes` | Unique framed logical-object bytes before compression |
| `packed_compressed_bytes` | Compressed bytes before encryption/padding/signature |
| `projection_line_limit` | Source JSONL projection bound used by this run |

The existing `pending_ciphertext_bytes`, retained tree, Git history/allocation
reserve, planned totals and `fits` still apply. These are measurements of the
selected inputs, not a storage reservation or a promise about a remote host's
accounting. Use the separate literal-canary scanner and authenticated history
audit described in [encrypted publication](encrypted-publication.md).

All new validation uses synthetic sources, disposable test identities and local
Git repositories. No owner credential, destination repository, real corpus
measurement or offsite publication is implied. Those acceptance steps still
require the authorized source host, reviewed scope, chosen storage budget,
credential custody and destination.
