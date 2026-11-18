# AI exports: custody record (sanitized)

This page is a public, method-focused custody record for raw AI chat exports. It replaces earlier public text that included real library figures, receipt names, and private filesystem paths. Detailed counts, sizes, hashes, device identifiers, and storage locations are retained only in the owner's private notes and private receipts.

## What was corrected

An earlier custody record overstated independence of backup copies by treating locations on the same physical storage as independent and by relying on a removable volume that was no longer present in the same form. The correction established that:

- Raw exports must be inventoried by content and source, not by assumed device names.
- Independent copies require different physical devices, with any remaining single-disk or single-host limitations stated explicitly.
- Derived outputs that can be regenerated from raw exports are tracked separately from irreplaceable raw inputs.

## Custody method

The custody process for raw AI exports follows these steps:

1. **Inventory**: identify raw export roots and distinguish raw inputs from derived stores. Record file inventories and integrity evidence in private receipts.
2. **Integrity checks**: compute hashes for files where practical, and use size/archive checks or previously recorded manifests for very large files. Compare originals and copies read-only.
3. **Replication**: create at least one additional copy on a physically independent device when available. Copy read-only from originals and verify the copy against the source inventory.
4. **Verification**: perform read-back verification (hash checks, structural comparisons, and archive tests as applicable) and record results in a private, dated receipt.
5. **Coverage review**: document which services or backup systems do or do not cover the export locations, without publishing private paths or infrastructure details.

## Current public status

- Raw AI exports have undergone inventory and integrity verification as recorded in private custody evidence.
- An additional copy on physically independent storage was created and verified according to the method above; residual risks (such as lack of offsite/versioned copy or dependence on a single host) are tracked privately.
- Detailed per-provider figures, byte totals, paths, device metadata, and receipt identifiers are intentionally omitted from this public repository.

## Open items (general)

- Maintain an offsite or versioned copy where practical, subject to bandwidth, cost, and owner approval.
- Re-home any copy that depends on hardware scheduled for retirement.
- Re-run read-back verification periodically or after storage changes, retaining evidence in private receipts.
