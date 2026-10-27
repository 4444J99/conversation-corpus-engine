# Bounded Notion ZIP batches

The canonical library export remains the complete source. The standalone [batch packer](../scripts/package-notion-batches.py) creates exclusive private derivatives without rebuilding or modifying the published generation.

Notion documents a 5 GB ZIP limit and warns that imports containing at least 10,000 files may fail or import partially. Its Markdown/CSV file limits are 5 MB on Free and 50 MB on paid plans. The packer reports these as decimal byte thresholds. Notion also recommends wrapping the contents in a folder. [Official import guidance](https://www.notion.com/help/import-data-into-notion), checked 2026-10-27.

```sh
python3 scripts/package-notion-batches.py \
  /private/store/library-all/generations/PUBLISHED/notion-import.zip \
  --output /private/store/derived/notion-batches-UNIQUE \
  --max-seconds 1800 \
  --max-output-bytes 8589934592
```

Choose a new output directory outside the published generation. The CLI lowers process priority. It has a hard POSIX wall timer, cooperative checkpoints, a logical output cap, and a measured 1 GiB free-space floor. Failed attempts retain their partial outputs and failure receipt; the completed manifest is written last. Existing destinations and source symlinks are rejected.

| Setting | Default / meaning |
| --- | --- |
| `--max-entries` | 5,000 total ZIP entries, including the wrapper directory |
| `--max-batch-bytes` | 262,144,000 final compressed ZIP bytes (250 MiB) |
| `--max-seconds` | 1,800 seconds for both passes and verification |
| `--max-output-bytes` | 8 GiB for ZIPs, ledger and receipts; planning admits measured output plus reserve |
| `--wrapper` | `library`; one common parent folder in every batch |
| Per-file Free threshold | 5,000,000 uncompressed bytes |
| Per-file paid threshold | 50,000,000 uncompressed bytes |

| Original member | Wrapped member |
| --- | --- |
| `vault/records/UID.md` | `library/vault/records/UID.md` |
| `records.csv` | `library/records.csv` |

The original relative path and uncompressed bytes are preserved exactly once below the wrapper. Duplicate contents remain distinct original members. Original names are sorted for deterministic grouping, timestamps and output attributes are fixed, and compression settings are recorded. Repeated runs with the same source, limits and compression runtime produce identical ZIPs, member ledger and manifest. The elapsed-time receipt is intentionally separate.

The first pass streams each source member through a normal `zipfile.ZipFile` into a seekable counting sink. This measures actual compressed bytes and headers without uncompressed scratch files. The plan is streamed to `members.jsonl`. The second pass writes each ZIP exclusively, then reopens it to verify every member's exact uncompressed size and SHA-256. Source ZIP SHA-256 is checked before and after the run. There is no custom ZIP writer or compressed-data splice.

A member that cannot fit alone is preserved in a separate `oversized-*.zip`, explicitly marked `oversized_preservation`. It is not called a compliant batch. No oversized member is silently omitted, truncated or split. `manifest.json` and the generated `README.md` report oversized items and counts above each per-file plan threshold. A compressed batch below 250 MiB can still contain a file that exceeds a plan's individual-file cap.

`manifest.json` binds each archive hash and the complete `members.jsonl` ledger; the ledger maps original paths to wrapped paths and destination ZIPs with per-member content hashes. It also records compatibility limits, counts and source identity. All output files are private (0600), in a private directory (0700).

Import each compliant batch through Notion's ZIP importer. Cross-batch links, heading anchors and native import behavior remain unverified; packaging does not establish a successful Notion import. Keep the canonical library and its raw revision retrieval available.

Focused regression coverage includes exact-once identity and byte preservation, deterministic repeated runs, Unicode member paths, the 5,000-entry boundary, exact compressed-byte caps, explicit oversized preservation, source mutation, corrupt CRC, duplicate and unsafe paths, symlinks, existing destinations, and storage/output bounds.
