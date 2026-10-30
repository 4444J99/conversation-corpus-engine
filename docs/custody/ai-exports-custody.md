# AI exports: custody record (corrected 2026-10-30)

This page is the current Custody record for the raw AI chat exports (ChatGPT, Claude, Gemini and NotebookLM, Grok, Perplexity). It replaces the Custody section of `docs/architecture/ai-exports-archive.md` and the receipt `docs/receipts/receipt-custody-ai-exports-second-copy-20261018.json` in `4444J99/limen`. That repository is archived read-only, so both are stale and frozen there; do not rely on them. The numbers below are owned by [`receipt-custody-ai-exports-second-copy-20261030.json`](receipt-custody-ai-exports-second-copy-20261030.json) beside this page. Counts, sizes, hashes and paths only; no conversation text.

## What the old record got wrong

The old receipt said `independent_physical_devices: true`, with the second copy on a volume named T7Recovery. On 2026-10-30:

- No mounted volume carries T7Recovery's recorded UUID. The only T7 attached (`diskutil`: media name PSSD T7, 1.0 TB, disk6) now carries the volume `TimeMachine`, used only for Time Machine, and the content it held was absorbed into `/Volumes/Archive4T/t7-absorbed-20261025`. That T7Recovery was this disk is inferred from the name and that folder, not from a retained UUID.
- Scratch, Archive4T and Ingress are volumes of one APFS container on one physical disk (disk8). Time Machine excludes Scratch and Archive4T.
- The receipt's exclusion "ChatGPT is already replicated on two independent devices" also leaned on T7Recovery. The raw ChatGPT zip and the later delta file existed once, on Archive4T.

So until the copy below was made, no raw export had a copy on a different physical device.

## Where the raw exports are (observed 2026-10-30)

| Root | Path | Files | Bytes |
|---|---|---:|---:|
| R1 | `/Volumes/Scratch/ai-exports` | 17,107 | 8,459,455,280 |
| R2 | `/Volumes/Archive4T/09_Engineering/limen-private/chatgpt-account-export-20261017` | 1,789 | 4,139,223,379 |
| R3 | `/Volumes/Archive4T/t7-absorbed-20261025/item-0035` | 3,538 | 3,529,805,101 |

R1 holds all five providers plus the ChatGPT export unpacked. R2 holds the raw ChatGPT zip, the delta file and the 2026-10-17 working set. R3 is the content that used to sit on the T7, absorbed into Archive4T: claude, gemini, grok and perplexity, identical to R1 except for ten `.DS_Store` files. All three are on disk8. `/Volumes/Scratch/cce-corpus-store` is derived output (regenerable from the raw exports) and is not copied.

Hashes were computed for every file of 90 MB or less (R1 17,100 of 17,107, R2 1,787 of 1,789, R3 3,534 of 3,538). The larger files are checked by size, and for zips by a CRC member test; the raw ChatGPT zip and the delta file rely on the sha256 recorded in R2's own `_custody/MANIFEST.sha256`, which agrees with fresh hashes on all 1,782 entries that could be re-hashed.

## Single-copy content before the new copy

- `chatgpt-export-2026-10-16.zip` (3,716,726,167 bytes) and `chatgpt-bodies-20261017-delta.json` (90,879,501 bytes): R2 only. A name search over Archive4T, Scratch, Ingress and the CleanUnique image finds no other copy. The delta file is not a member of the zip.
- R2's working set (1,753 files, 1,732 of them under `review/`): R2 only.
- `knowledge-base-brainstorms-chatgpt.txt` (26,018 bytes): R1 only.
- The ChatGPT extract in R1 (13,558 files) is the raw zip unpacked: all 13,554 zip members are present at the same size, plus four extras (two `.DS_Store`, one duplicate inventory file).
- claude, gemini, grok, perplexity: two copies (R1, R3), same disk.

## What else covers them

| Service | Finding (read-only) |
|---|---|
| Time Machine | Scratch, Archive4T and `ai-exports` are excluded (`tmutil isexcluded`). The internal `Workspace` is included. |
| Backblaze | Not installed: no app, no `/Library/Backblaze.bzpkg`, no launch daemon or agent, no process. Archive4T has only a `.bzvol` marker; its newest ping log is 2026-07-23. |
| Dropbox | Not installed: no app, no sync folder; `~/.dropbox` leftovers, `unlink.db` dated 2026-10-24. |
| iCloud Drive, Google Drive for desktop | No export root by name to depth 5. |
| `rclone` remote `gdrive:limen-custody` | 226 objects, 2.322 GiB, encrypted archives and receipts; none named as an AI export. |
| OneDrive | Empty apart from `.Trash`. |
| Internal disk before the copy | No copy of the raw zip, delta, takeout, Grok or Perplexity files by name (home-tree `find` and Spotlight); the source path named in the 2026-10-17 ChatGPT receipt no longer exists. |

## The second copy

`/Users/4jp/Workspace/titan/state/private/custody-ai-exports-second-copy-20261030` on the internal disk (disk0, volume Data, FileVault on), mode 0700:

- `scratch-ai-exports/` is R1 whole (17,107 files, 8,459,455,280 bytes).
- `archive4t-chatgpt-replica/` is R2 whole (1,789 files, 4,139,223,379 bytes), keeping its read-only modes.
- `_run/` holds the hash manifests taken from the originals before the transfer, the large-file lists, the inventory statistics and the check logs.

Made with `rsync -a` (no `--delete`), 2026-10-30 21:24Z to 21:30Z, from the originals, which were only read. Read-back, with the manifests stored on the copy:

- `shasum -a 256 -c` over R1's manifest: 17,100 OK, 0 failed. Over R2's manifest: 1,787 OK, 0 failed.
- R2's own custody manifest (recorded 2026-10-17, covering the raw zip and the delta file): 1,784 OK, 0 failed.
- Every path, size and modification time compared between each original and its copy: no file missing either way, no size or time difference. No original file has a modification time later than the start of the hashing.
- Zip CRC tests passed on the Gemini takeout zip, the Grok zip and the raw ChatGPT zip.

The copy carries content, permission bits and modification times; extended attributes, ACLs and birth times are not carried (`openrsync -a`).

After this, every raw root has two copies on two physical devices (disk8, disk0). The path is inside Time Machine's scope; the latest backup (2026-10-30 17:03 local) predates the copy, so no backup has captured it yet.

## Open items

- No offsite or versioned copy exists. About 12.6 GB would be a bulk cloud upload, which is not made over the phone hotspot.
- The new copy is on the Mac's internal disk. It must be moved to another independent device before this Mac is retired.
- Re-run the read-back at any time: from the copy, `cd scratch-ai-exports` and `shasum -a 256 -c ../_run/r1-scratch-ai-exports.manifest.sha256`; `cd archive4t-chatgpt-replica` and `shasum -a 256 -c _custody/MANIFEST.sha256`.
