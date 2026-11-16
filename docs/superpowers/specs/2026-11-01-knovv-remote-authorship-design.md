# KNOVV remote knowledge and authorship completion

## Intent and authority

The current request is to proceed with the November 1 audit's recommendations: preserve and retrieve the knowledge corpus remotely, encrypt it for enrolled instances, retain a no-added-storage-cost constraint, use attributed Zettelkasten units, and complete browser, Notes, document, and Google Docs capture. This specification implements that request in the existing cooperating repositories. No repository consolidation or source-data relocation is required.

## Approach

Extend CCE's canonical record/revision model and reuse the encrypted publication work from PR #85. Add explicit, digest-bound authorship annotations and reviewed idea units above immutable source spans. Improve existing capture adapters in their owning repositories. Preserve old raw material, identities, binary author partitions, and history; the old human/machine partition is not proof of original authorship.

A new independent knowledge database would duplicate identity authority. Changing every old human/machine value would silently reinterpret history. Both are rejected in favor of additive attributed projections and explicit profile eligibility.

## Required behavior

1. Supplied text and original authorship are distinct. Owner, agent, third-party, and unknown spans remain distinguishable. Mixed material has explicit nonoverlapping, in-range spans; uncovered material is unknown. An annotation is an attributed assertion, never an independent proof. Bind annotations to the exact text digest so stale annotations cannot follow edited text.
2. Strict personal-profile extraction admits only explicitly owner-attributed spans with evidence and an explicit assertion status. Provider role/channel alone never certifies the owner's writing. Retain legacy role heuristics only as an explicitly labelled compatibility option.
3. A reviewed semantic Zettel has a stable caller-supplied identity, a title/body, typed relationships, exact source/revision/span references, and attribution. Generated structural blocks remain candidate material. Source and derivation authorship are separate. Reject missing, stale, invalid, or ambiguous source references instead of silently clipping or guessing.
4. Provider-scoped merge rebuilds cannot silently remove previously bridged library material. Record effective inputs, origins, hashes, generation pointers, and build options in private manifests. Preserve old manifest readers and immutable source objects.
5. Reuse PR #85's encrypted format and cryptography, integrate it with current sharded archives, and exercise publication plus independent clone/restore with synthetic sources. IDs alone never grant decryption. Test wrong-key, tamper, stale checkpoint, rotation, and capacity refusal. Add protected at-rest key custody without printing secrets; do not claim hardware or runtime attestation.
6. Capture adapters must enumerate every requested page/folder, report partial failures, preserve stable IDs and revisions, and treat bylines/roles as evidence rather than inferred ownership. Browser reference capture and article-body clipping are measured separately.
7. No paid resources, plaintext private-corpus publication, or new production credentials are introduced. Actual backup completion, source refresh, remote runtime enrollment, capacity, and recovery remain acceptance checks requiring the existing authorized source host and protected keys.

## Validation and delivery

Use synthetic fixtures, meaningful regression tests, the repository lint/format gates, independent review, and exact-commit publication checks. Source-host acceptance remains distinct from cloud tests. Preserve any unavailable host/key/capacity steps as explicit outstanding gates, with executable commands where available. Publish reviewable code through scoped pull requests; never bypass branch protections or invent live-data receipts.
