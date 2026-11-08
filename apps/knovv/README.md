# KNOVV private workspace

KNOVV reads CCE source passages, maintained wiki pages and atomic notes in a
browser-decrypted workspace. Capture, editing, citations, typed page links,
backlinks, revision history and conflict recovery share one encrypted journal.
Enrolled devices synchronize directly through Cloudflare while the source Mac is off.

The application lives in this repository alongside the canonical engine. Its
derived browser publication is separate from original records. Browser revisions
return through a marked, source-owned CCE authoring directory; they never rewrite
agent memory, generated wiki files, or the accepted library generation.

## Run and verify locally

From the repository root, install the Python bridge with `uv sync --extra dev --extra sealed`.
From this directory:

```sh
npm ci --no-audit --no-fund
npm test
npm run build
KNOVV_CF_BUILD=1 node_modules/.bin/cf build --mode staging
```

`node tests/preview.mjs` starts a localhost-only synthetic workspace. Its output
identifies a protected synthetic device package and its test passphrase. Import
that package in the browser to exercise search, reading, capture, editing and
reopening. This harness bypasses Access only on loopback; production imports only
`server/worker.ts`, which denies unauthenticated requests.

Python interoperability tests use `KNOVV_PYTHON` when provided, otherwise the
repository's `.venv/bin/python`. CI runs the Python suite and a separate KNOVV job.

## Publication and device custody

Keep credentials, passphrase files, device packages, publication bundles and
receipts in a private directory outside the checkout. Passphrase files contain
exact bytes and must have mode 0600. Keys are protected with scrypt and authenticated
encryption; the browser retains the protected credential, never its passphrase.

Create the publisher identity using the existing `cce sealed identity` command
described in [encrypted publication](../../docs/encrypted-publication.md). Preserve
the owner key and its recovery recipient independently. Enroll a separate key for
each device:

```sh
cce-knovv enroll --owner-key "$OWNER_KEY" --owner-passphrase-file "$OWNER_UNLOCK" \
  --device-key "$DEVICE_KEY" --device-passphrase-file "$DEVICE_UNLOCK" \
  --instance-id "$DEVICE_NAME" --output "$ENROLLMENT"

cce-knovv export --store-root "$STORE_ROOT" --wiki-root "$WIKI_ROOT" \
  --owner-key "$OWNER_KEY" --owner-passphrase-file "$OWNER_UNLOCK" \
  --recipient "$OWNER_RECIPIENT" --recipient "$ENROLLMENT/recipient.json" \
  --budget "$CIPHER_BUDGET_BYTES" --output "$BUNDLE"

cce-knovv package --bundle "$BUNDLE" --owner-public "$OWNER_PUBLIC" \
  --device-key "$DEVICE_KEY" --device-passphrase-file "$DEVICE_UNLOCK" \
  --output "$DEVICE_PACKAGE"
```

`STORE_ROOT` is the registered store containing `library-all/current.json`, not the
`library-all` subdirectory. Export opens its accepted SQLite generation read-only.
All source revisions are retained with original message coordinates and digests.
Wiki source tuples are checked against the cited record revision before a head can
be published. Both wikilinks and ordinary relative Markdown page links become
application links. Unknown authorship stays visible as unknown.

The publication uses compressed, length-framed encrypted data slabs and 256 possible
encrypted locator pages. Identifiers are keyed HMACs. Metadata, search postings,
lookup tables and page content use the same resolver. Browser caches retain only
ciphertext; decoded state lives in memory and is discarded on lock. The original
`cce-sealed.v1` and compressed `cce-sealed.v2` archive readers also have Python/browser
interoperability coverage in `src/sealed.ts`.

`--previous` preserves the vault, stable document IDs and unchanged ciphertext.
Changing recipients requires an explicit `--rotate` export. Rotation creates a new
encryption epoch and keeps earlier epoch keys inside the encrypted catalog so
current devices can read accepted history. A removed recipient cannot unwrap the
new publication. Revocation cannot erase material a device previously decrypted.

Sign a monotonic grant update with `cce-knovv grant --previous-grant "$GRANT"`
plus `--state revoked` or `--state active`, the owner key/unlock arguments, and a new
`--output` path. Apply the signed update through the owner API or the seeding command.
Remove the revoked recipient and rotate before publishing new private material.

## Cloudflare activation

Use a dedicated D1 database, a dedicated R2 bucket and owner-only Cloudflare Access.
The configuration file for the control CLI contains `account`, `database` (UUID),
`bucket`, `owner_public`, `owner_email`, `audience` (Access AUD), and
`max_cipher_bytes`. It contains no decryption key. The default application budget is
512 MiB; an explicitly supplied budget may be at most 8 GiB. Account-wide included
allowances and request usage must be measured before increasing a production budget.
Local export does not establish account capacity or authorize additional charges.

`cce-knovv-cloudflare environment --config "$DEPLOY_CONFIG"` prints the validated
environment values expected by `cloudflare.config.ts`. Supply those values to the
build/deploy process using the existing environment owner. The Worker remains
closed when identity, audience, database or bucket configuration is missing.

```sh
cce-knovv-cloudflare seed --config "$DEPLOY_CONFIG" --bundle "$BUNDLE" \
  --grant "$ENROLLMENT/grant.json"
```

Seeding verifies the entire local ciphertext inventory before any remote mutation.
It refuses a populated database without a matching KNOVV ownership marker, reserves
storage within the configured budget, verifies each R2 object by independent
readback, and publishes the signed head last with a parent comparison. An interrupted
upload retains a bounded reservation and can be retried. Never repurpose or delete
another application's database to resolve capacity.

Build staging with `KNOVV_CF_BUILD=1 node_modules/.bin/cf build --mode staging`,
then deploy its build output with `node_modules/.bin/cf deploy --prebuilt --no-provision`.
Explicit IDs prevent automatic resource provisioning. Keep the current atlas and
legacy Pages application available during acceptance.

Use [Worker-level Access](https://developers.cloudflare.com/workers/configuration/cloudflare-access/#protect-one-worker)
with an exact owner-email allow policy and an all-traffic `worker` destination.
This protects every route and hostname associated with that Worker. Pin its AUD
in `KNOVV_ACCESS_AUD`; the Worker checks the platform-provided `ctx.access` identity
and the owner email before serving either API responses or assets. Do not add a
development identity to production configuration. A first bootstrap deployment
with an empty audience denies all traffic until Access is attached and the real
AUD is configured.

Exercise the staged build with two enrolled devices, including a phone, and verify
an unauthenticated request is rejected. Promote identical application code to
production only after the source-passage, capture/edit/reopen, conflict, revocation,
rotation, restore and measured search checks pass. Keep separate staging data and
configuration. Deployment and real-material acceptance are distinct outcomes.

## Independent recovery and CCE import

```sh
cce-knovv-cloudflare snapshot --config "$DEPLOY_CONFIG" --output "$SNAPSHOT"

cce-knovv restore --bundle "$SNAPSHOT" --owner-public "$OWNER_PUBLIC" \
  --device-key "$DEVICE_KEY" --device-passphrase-file "$DEVICE_UNLOCK" \
  --checkpoint "$DEVICE_PACKAGE" --output "$RESTORED_DOCUMENTS"

cce-knovv import-journal --bundle "$SNAPSHOT" --owner-public "$OWNER_PUBLIC" \
  --device-key "$DEVICE_KEY" --device-passphrase-file "$DEVICE_UNLOCK" \
  --journal "$SNAPSHOT/journal.json" --objects "$SNAPSHOT/objects" \
  --authoring-home "$AUTHORING_HOME"
```

The snapshot reads one consistent D1 observation, downloads uploaded ciphertext,
checks every object digest and writes its head only after readback completes.
Its receipt distinguishes verified ciphertext from decrypted content restoration.
Preserve the complete snapshot in the existing private recovery repository and
verify an independent restore before reporting it archived.

The journal importer validates signatures, parent chains and content before
writing. It preserves immutable revision receipts, original attachment bytes and
small current Markdown notes. Old split-note parts remain preserved but are
excluded from the current source declaration. Reimporting a snapshot is idempotent
and reconciles the declaration after an interruption. Include its `source.json`
through the ordinary CCE candidate-build and promotion path; the accepted library
is never modified in place. Unseen revisions from a now-revoked device require
owner review rather than being silently accepted.

## Operational bounds and visible states

The UI distinguishes local encrypted drafts, remotely synced revisions and pending
archive snapshots. Stale saves retain a conflict draft. File capture preserves
original bytes (4 MiB per file, eight attachments per page); binary text extraction
is reported as pending. URL capture saves a reference; article-body clipping,
browser-history refresh, Notes and Google Docs ingestion continue through their
existing source adapters and are not marked complete by publication.

The ciphertext cache is bounded to 32 MiB; cold reads and uncached offline reads
are different cases. Search decodes postings in a worker and reports bounded result
counts. Reader data slabs are bounded to 32 MiB uncompressed. Journal import is
bounded to 10,000 entries and 64 MiB decoded content per batch. Cloud control
operations have a one-hour wall and per-command timeouts. Publication has a default
one-hour deadline and a shared 15-minute attribution budget; unmeasured authorship
is shown explicitly when that budget expires. These bounds fail visibly; they do
not turn an incomplete publication into a verified head.
