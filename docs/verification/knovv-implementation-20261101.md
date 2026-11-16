# KNOVV implementation verification — 2026-11-01

This receipt establishes implemented behavior using synthetic fixtures. It does not certify a
production source refresh, device enrollment, offsite backup, or publication of a private corpus.

## Tested code and scope

Base: `05df0b84bb34177767daaa516f720a005e53b3f4` (current main when work began and when publication
was prepared). Final tested code: `990902a46987436b54b06388090c9f122dddcdbf`.

The implementation reuses the encrypted-publication work from PR #85 and adapts it to current
bounded shards. It adds protected software credentials, read-only capacity forecasts, bounded
history inspection, exact span attribution, reviewed semantic Zettels, strict owner profiles,
and preservation/input receipts for incremental archive builds. Raw source text, canonical
revision history and the older binary author containers are retained.

## Results

| Gate | Result |
| --- | --- |
| Complete engine suite, Python 3.12 with loopback access | **1,675 passed, 3 skipped**, 100.42 seconds |
| Ruff lint and format | Passed; 184 Python files formatted |
| Focused mypy | Passed; seven attribution/knowledge/encryption modules |
| Published schema loading | Every published schema loaded |
| Diff whitespace | Passed |
| Independent authorship review | Stale empty-text annotation issue corrected; no unsafe owner-admission bypass found in reviewed scope |
| Independent encryption/preservation review | All reported blockers closed at the exact tested code revision |
| Companion source-adapter verification | 30 synthetic tests and scoped strict TypeScript check passed; independent review closed three Google completeness defects |
| Companion browser-history route | Four synthetic subprocess tests, shell syntax and Python compilation passed |

The three skipped engine cases exercise unreadable file/folder modes and explicitly skip under
root, where those modes would not prove access denial. They were not disabled by this change.
Companion adapter tests used a scoped source checkout and an isolated dependency environment;
they do not establish a full-project build/test pass or actual provider behavior on a user's host.

## End-to-end evidence

`tests/test_knovv_roundtrip.py` creates six synthetic canonical source cases: an owner prompt
containing a quotation, a note containing saved model text, an assistant reply, a clipped article,
a mixed document, and an unattributed import. Its assertions establish that:

- Only the explicitly asserted, evidenced, opted-in spans of the chosen owner enter the profile.
  The remaining source text is retained with agent, third-party or unknown attribution.
- Reviewed Zettels have stable caller IDs, relationships, independent body/title attribution and
  exact UID/revision/digest/message/Unicode-offset source references.
- A later accepted source edit does not change the older revision cited by a Zettel.
- All six reviewed units survive the compatible atomic export, encrypted publication, an
  independent local `git clone --no-local`, enumeration and retrieval without alteration.
- A wrong private key using the same app/instance labels cannot verify the archive.
- The three selected files, including the canonical database and annotations, restore with the
  same SHA-256 hashes. The restored database produces identical reviewed units and owner spans.
- The ciphertext working tree does not expose the tested unit IDs or source text.

This is an independent Git clone of synthetic data, not evidence that a remote production
repository or enrolled application has been exercised.

## Review-driven failure cases

Meaningful failing tests preceded the fixes. The final regression set covers:

- Missing library input, absent bridged identities, same-basename captures, and fixed-name deltas
  refreshed with only newer records. Preflight refuses loss before writing any chosen provider.
- Capture identity protection surviving absorption into an account export and subsequent merges.
  Failed rebuilds keep the previous shard/input receipt alongside the failed attempt.
- Content reversion and deselection/reselection reusing authenticated retained ciphertext.
- A capacity forecast covering the measured first Git commit, with explicit metadata/allocation
  headroom. Capacity refusal leaves the accepted publication/checkpoint intact.
- Historical oversized blobs, invalid paths, aggregate inspection limits, and address aliases
  rejected before unbounded body or metadata retention. Ordinary history and rotation still work.
- Stale attribution on empty messages, Unicode spans, overlaps, missing evidence, disputed or
  retracted assertions, incorrect owner identity and ambiguous source references.

## Operational acceptance still required

Follow [encrypted publication](../encrypted-publication.md) and
[attributed knowledge](../attributed-knowledge.md) on the authorized source host. Record the exact
engine revision, fresh capture receipts and physical backup custody before publication. Select a
bounded dataset, inspect its first/update/rotation forecasts against actual available storage,
and verify actual Git object/tree bytes after the commit and before pushing. Enroll the intended
owner, application/device and recovery keys through protected custody. Then push the ciphertext,
record the remote ref, clone independently, transfer the pinned checkpoint through a trusted
route, and compare every restored file hash plus archive/profile/unit verification.

The implemented boundary is possession of protected enrolled keys. App/instance labels are
bound to those recipient keys; they do not attest the running binary or make software keys
hardware-nonexportable. Existing recipients retain access to old ciphertext epochs. Git storage
and bandwidth remain finite; the default budget and forecasts do not promise unlimited free
hosting. No paid storage, LFS purchase or production credentials were created for these tests.
