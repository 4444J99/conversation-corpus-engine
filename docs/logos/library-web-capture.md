# Source-bound web capture intake

A saved page is useful as evidence only while the retrieved words remain connected to
the page that supplied them. The web-capture reader accepts explicit offline JSONL
shards and keeps the requested URL, redirect destination, fetch time, exact content
hash, extractor and rights note alongside the unchanged page text. Wikipedia adds
its language, resolved title, page and revision identifiers, redirect history and
category evidence. Search shows the URL and fetch time and hands off to an exact
retained revision and character range.

The implementation integrates the original C13 reader contribution with the current
passage-search and attribution work. It preserves the `webcapture` provider, `page`
kind, `web` lane, stable `pg-` identity and tool-message representation. The existing
`ai` wing and machine author container are storage categories for material outside
the owner's writing; they do not assert that a page was written by an AI. The first
attribution rule labels the complete page `third_party`, including code, quotations
and text claiming to be owner or assistant speech.

Adding that first rule advanced the rule semantics to `cce.attribution-rules@8`.
The provider-only guard advances them again to `@9`: it strips and case-folds the
provider, ignores kind and wing, and protects directly constructed rule inputs.
Earlier samples and reviews remain preserved, are explicitly reported as stale, and
do not open owner-profile eligibility. The ledger and sample schemas remain v6 and
the existing distinct-text review thresholds remain in force. This is a code and
contract change; no owner review campaign is claimed to have occurred.

Two-pass intake keeps at most three candidate locators per page in memory. Success outranks
failure; capture time, content digest, metadata digest and relative shard name make
duplicate selection deterministic. If the winner's shard rotates, the second pass records
the refusal and tries the next retained candidate. Every pass checks the execution budget, including
directory discovery and draining an oversized row. Distinct candidates share the
build's remaining record allowance, and detailed findings are capped with an explicit
omission count and one top-level truncation finding. Unknown field names never enter
diagnostics. UTC stamps require ASCII digits, but acceptance does not depend on the
reader's wall clock: offline source evidence must remain reproducible. Failed or missing
pages contain no body, remain metadata-only, and
make coverage incomplete. Refused Wikipedia targets produce findings only.

Search metadata is a small projection keyed by the immutable normalized object digest
in the existing SQLite metadata table. Full verification compares it to the source
record. This avoids decoding a multi-megabyte page merely to display a URL and time,
keeps passage ranking within its existing snapshot, and needs no storage-schema bump.
Changing a page creates a new source revision and a separately bound projection.

The integration tests use invented pages. They cover three-page build/search/rebuild
acceptance, exact third-party attribution, both producer schemas, case-sensitive Unicode
Wikipedia titles, URL/key substitution, altered digests, malformed list fields,
duplicate metadata ties, body-free outcomes, bounded discovery and reads, resume-source
qualification, immutable provenance, compact MCP fields and long-page search. Producer
compatibility is pinned to browser tool commit
`a8dc102ce1fc641d9454f0e3f2a6cf7d6d08d6ce`; no test crawls production pages or establishes
that a person's saved-page collection is exhaustive.

## Reconciliation of the concurrent follow-up

The original reader PR146 advanced after its first head was incorporated. The follow-up
review preserves its new immutable head `b95b1d4f0bb01a16340e3c2e011031eec59fdac7`
and the previously merged integration. It adopts the provider-only attribution guard,
count-only unknown-field diagnostics, ASCII timestamps, bounded alternate candidates
and top-level finding omission count. Synthetic regressions first reproduced these gaps
against the merged integration before the fixes were applied.

The existing source-digest-bound search projection, exact producer URL normalization,
canonical metadata ties, enum refusal, directory error findings and deadline checks remain
the accepted implementations. The alternative SQL table and host/path-only identity
comparison are superseded by those existing guarantees. The proposed clock-relative
future-time refusal is not adopted because it would make the same offline input change
validity over time. All earlier source commits remain retained; no original branch is
overwritten or removed by this reconciliation. Hosted checks and integration acceptance
are recorded on the follow-up pull request.
