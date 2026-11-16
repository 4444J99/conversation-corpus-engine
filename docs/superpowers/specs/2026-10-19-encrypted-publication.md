# Encrypted archive publication v1

Extend CCE, preserving every existing archive and physical custody interface. Plaintext sources,
selection lists, annotations, restored files and credentials remain outside Git. The publication
directory contains only opaque ciphertext objects and an owner-signed encrypted HEAD. No real
corpus or production keys are used in development. App and instance IDs are labels; possession
of an owner-enrolled X25519 private key grants decryption, not those labels. An externally pinned
Ed25519 owner key grants publication authority. A copied credential is the same principal; this
CLI does not claim hardware attestation or protection against its host administrator.

Use cryptography's X25519, HKDF-SHA256, ChaCha20-Poly1305 and Ed25519. Encrypt a random epoch
secret separately to each enrolled recipient. Bind envelopes to the vault and recipient labels
inside ciphertext. Sign the complete HEAD, including envelopes and encrypted manifest. Keep
the signing trust root, vault identifier and latest accepted HEAD checkpoint outside Git.
Reject rollback/equivocation relative to the checkpoint; disconnected/new devices need a fresh
owner-provided checkpoint. Git alone cannot prove freshness.

Objects are HMAC-SHA256 addressed with a domain-separated epoch key, padded to 4096-byte
buckets, encrypted with random 96-bit nonces and authenticated purpose/address context.
The encrypted manifest contains private paths, source digests, reconstruction recipes,
unit IDs, source provenance, message roles/channels, timestamps and derivations. Preserve exact
selected bytes using 512-KiB chunks; also project each normalized v2 message as an addressable
atom, keeping all original fields. Export roles are evidence, never proof of original writing or
prompt ownership. Preserve explicit imported cce-knowledge-atom.v1 annotations without promoting
their assertions to verified facts. No new browser, Notes or Docs capture engine is introduced.

Incremental publication reuses validated ciphertext in the same epoch; unchanged input and
recipient policy is a no-op. Rotation generates a fresh epoch and re-encrypts all current units.
Removed recipients retain access to old history and old epoch secrets; removal must rotate.
No automatic pruning, history rewriting, backup deletion, visibility changes or real-data push.
Missing/corrupt objects are repaired only from ciphertext matching authenticated manifest hashes;
if none is available restore fails without a partial destination. Ciphertext cannot reconstruct
information that is absent from all copies. Restore uses a private staging directory and refuses
an existing destination, unsafe paths and symlinks.

Residual leakage: object counts, padded sizes, reuse/change patterns, recipient count, Git author
and commit times. There are no plaintext hashes, source names, app IDs or source timestamps in
publication files. Logs use fixed error codes and aggregate counts only. Secure erasure, search
over ciphertext, online revocation and attested remote execution are outside v1.

Apply a default 512-MiB live-tree budget and bounded encrypted manifest; report actual live bytes
and Git object-store bytes separately. Git history grows on changes/rotation; fragmentation does
not create unlimited storage. GitHub policy, push/object limits and LFS quotas must be assessed
against measured private data before publication, without purchasing storage implicitly.
