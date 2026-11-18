# History-export coverage ledger (sanitized)

This page summarizes, in aggregate and method-focused terms, how coverage of existing ChatGPT, Gemini, and Perplexity history exports was assessed. It omits private filesystem paths, file names, conversation identifiers/titles, exact counts, and generation identifiers. Detailed evidence is retained in the owner's private notes.

## Search method (read-only)

Coverage was reviewed by:
- searching local archive and staging locations for known export/takeout naming patterns and previously recorded export copies,
- checking common local download/document locations for single-conversation export files using format signatures,
- listing already-configured cloud remotes by name filters only (no content downloaded), and
- avoiding reads of application credential stores, cookies, or tokens.

The review distinguished archived full exports, duplicate copies of the same exports, app-cache staging data, and newer ad hoc single-conversation exports.

## Coverage outcome (general)

- **ChatGPT**: a historical full export (and a later delta) was present in the archive; limited newer material was found outside the archive in ad hoc exports. A complete gap-closing export may still be required for periods after the archived export, except for any separately ingested items.
- **Perplexity**: a historical official export was present in the archive; a small number of newer thread exports were found outside the archive. Additional export coverage may be required for later periods.
- **Gemini**: a historical Takeout export was present in the archive; no newer extracted Takeout content was found in the searched locations. A new Takeout export is required to cover later activity.

Older cloud Takeout copies that predate the archived cumulative export do not extend coverage beyond it.

## Ingestion method for ad hoc exports

Where newer single-conversation/thread exports were found but no dedicated archive reader existed, they were handled through a bounded, non-destructive library-build workflow:

1. Copy selected files into a private staging area and record hashes.
2. Build a candidate generation as a `documents` source (support lane, no authorship credit) with bounded time/output limits, without modifying the published pointer or existing generation.
3. If the build exceeds bounds, retain the failed candidate and staged inputs for a later bounded resume or fresh build under owner-scheduled conditions.
4. Publish only after successful verification moves the pointer.

Searchability, gap-reporting behavior, and publication state depend on the outcome of that bounded build/publish process and are tracked in private operational records.
