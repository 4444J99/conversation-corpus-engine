# Observable source capture

Capture reports describe the adapter's stated projection. They do not certify an
account-wide archive, attachment backup, complete authorship, or historical
revision archive. A URL inventory is a reference inventory until its body is
successfully captured.

## Reports and compatibility

GoogleDocsSource, AppleNotesSource, and WebClipperSource expose
`exportAllWithReport(options)` returning `{ items, report }`. A report has source,
status (`complete`, `partial`, or `failed`), and per-operation receipts. Receipts
identify a source item or folder, list/export stage, status, and safe reason or
limitations. Receipt statuses distinguish captured bodies, incomplete projections,
failed operations, and reference-only URLs.

The existing array-returning `exportAll` remains available. Incomplete capture
throws `IncompleteCaptureError`, whose `items` and `report` retain successful
partial results. Callers that need partial data should use `exportAllWithReport`
or inspect that error. Each adapter also exposes `lastCaptureReport`.
SourceManager retains partial items from this error and records reports in
`lastCaptureReports`; its diagnostics print counts, never document bodies.

A complete report with no items means the requested scope was inspected and
empty. Missing authentication, inaccessible folders, and unavailable Apple Notes
produce failed reports rather than successful empty exports.

## Google Docs

Existing OAuth scopes remain read-only. Authenticate using the existing Google
Docs authentication workflow and set `GOOGLE_DOCS_FOLDER_ID` for one root, or
`GOOGLE_DOCS_FOLDER_IDS` to a comma-separated list of explicit folder roots. The
latter takes precedence; absent either setting, the root is `root` (My Drive).
An explicitly configured empty, whitespace-only, or comma-only root list fails
configuration validation instead of selecting no scope.

Each traversed folder, including configured roots and the `root` alias, is first
resolved through Drive metadata. Its identity, folder MIME type, trash state and
canListChildren capability must be valid before an empty child query can count as
complete. Failed or inaccessible roots produce receipts while other roots remain
exportable. The resolved ID is used in the parent filter, including when `root`
resolves to the actual My Drive root ID.

Shared-drive folder metadata supplies driveId. Listing uses supportsAllDrives and
includeItemsFromAllDrives, with corpora=drive and the resolved driveId for shared
drives, or corpora=user for My Drive. The parent filter is retained on every page
and nested-folder query; unrelated drive/account files are not enumerated. Direct
document metadata retrieval also supports shared drives.

Every root is traversed recursively. The adapter requests 100 Drive entries per
page, follows nextPageToken to exhaustion, deduplicates document and folder IDs,
and terminates folder cycles. Repeated page tokens, incompleteSearch, malformed
list responses, and failed pages produce receipts. Earlier discovered documents
remain exportable after a later page or folder fails. There are no automatic
retries; one scan is capped at 10,000 folder-metadata/listing requests and each API fetch has a
30-second timeout.

The Docs API request includes all tabs; child tabs are traversed. Projection and
completeness checks share the same selected document parts. Every selected tab
must have a body content array; omissions are identified by tab ID (or traversal
path when the ID is absent). A valid sibling or an unused legacy top-level body
cannot mask a missing tab body. Text from other valid tabs remains recoverable.
Existing
paragraph, heading, list, and simple table conversion produces Markdown. Missing
bodies and known unsupported content (including images, nested tables, equations,
footnotes, headers/footers, and suggested edits) mark that document incomplete.
The resulting text can still be inspected through partial items. Comments,
historical revisions, and attachment bytes are outside this projection.

Document IDs stay stable. Metadata retains Docs revision ID, Drive version,
source creation/modification timestamps when available, and a separate capture
timestamp. Fallback timestamps are explicitly marked as capture timestamps.
Drive owners and lastModifyingUser remain source metadata; neither is treated as
span authorship. Folder listing and document export occur over time and are not
an atomic Drive snapshot.

Official API behavior:

- https://developers.google.com/workspace/drive/api/reference/rest/v3/files/list
- https://developers.google.com/workspace/docs/api/how-tos/tabs
- https://developers.google.com/workspace/drive/api/guides/enable-shareddrives
- https://developers.google.com/workspace/drive/api/reference/rest/v3/files/get

## Explicit web queue

The clipper never reads browsing history. With no URLs, it does no browsing.
Queues contain at most 100 input entries; normalized duplicate URLs are visited
once. Only HTTP(S) URLs without embedded credentials are accepted.

Create an explicit newline-delimited file (blank lines and `#` comment lines are
ignored), then run:

```bash
npx tsx src/clip-queue-cli.ts urls.txt capture.json
```

The output JSON contains captured bodies and per-URL receipts. Exit code 0 means
complete capture; 2 means partial/failed capture with a written report; 1 means
the command could not produce a report. This command writes a capture artifact;
it does not insert URL references into the knowledge database.

The existing single-page command remains available:

```bash
npx tsx src/clip-url-cli.ts https://example.org/article
```

`clipUrl(url)` preserves its item-or-null API. `exportItem(url)` throws the
structured capture error when no body was captured. Both update lastCaptureReport.
The normalized requested URL is the stable identity (fragment removed, query
preserved); SHA-256 of captured article HTML is a separate content revision.
Captures preserve article HTML, exact Readability textContent, source/final URL,
byline and source metadata. Capture timestamps are not publication timestamps.
A Readability failure yields a reference-only receipt, not an empty document.
HTTP and navigation failures yield failed receipts. Redirects do not change the
requested URL's identity. The capture covers the extracted article; linked pages
and attachment bytes are excluded.

## Apple Notes

Run the existing on-demand source adapter on macOS, or export manually:

```bash
osascript -l JavaScript scripts/export-apple-notes.js > notes.json
```

The JXA script emits a versioned object containing notes and a report with
`discoveredNotes`, `exportedNotes`, `failedNotes`, `failedAccounts`, and receipts.
It reads Notes without modifying notes or installing a background service.
Account-list failures and individual note failures remain visible; successful
notes retain exact IDs, HTML bodies, account metadata, and source dates. A failure
to read a note's ID is represented by an account/index receipt, not an invented
stable note identity. Counts cannot include notes inside unreadable accounts.

Attachment bytes are explicitly marked absent. The adapter accepts older array
exports for compatibility but marks their failure accounting incomplete. A macOS
permission or script failure returns an unavailable report. The osascript process
has a two-minute timeout and a 50 MiB output bound; larger collections may need a
future streaming exporter. No macOS or private account execution is part of the
synthetic regression suite.

## Verification

Focused synthetic tests live beside the adapters, with a queue command test in
src/clip-queue-cli.test.ts. They exercise 100+ documents, recursive folders,
duplicates/cycles, repeated tokens, failed list pages, failed exports, nested tabs,
unsupported content, stable web identity/revisions, bounded opt-in queues,
reference-only/failure receipts, partial manager ingestion, and failed Notes reads.
They mock network/browser and Notes boundaries and never use live accounts.
