"""The lineage of generations: what each ingest added, changed or removed.

A generation is one ingest that produced a new manifest. The manifest itself is overwritten, so
what one generation did to the one before would be lost; this module keeps it. Two files sit in
the archive folder beside the manifest:

* the digest file holds the content digest of every conversation as of the latest generation
  (`content_digest`: everything but where it was read from), one row each;
* the ledger holds one row per generation: its number, the source fingerprint of its manifest,
  the fingerprint of the generation before (its parent), and the uids that generation added,
  changed or removed against that parent.

A generation is named by its fingerprint and the data's own latest day, never by a clock, so two
runs over the same sources write the same ledger. Re-ingesting unchanged sources adds nothing.
Recent changes and per-page history on the site are read from the ledger, not by diffing pages.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterable
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .archive_model import iter_jsonl, write_jsonl_atomic

if TYPE_CHECKING:
    from .archive_ingest import ArchivePaths

# What a generation can do to a conversation, in the order a ledger row lists them.
CHANGE_KINDS = ("added", "changed", "removed")


def load_lineage(paths: ArchivePaths) -> list[dict[str, Any]]:
    """The generations recorded so far, oldest first (an archive with none has an empty list)."""
    if not paths.lineage.is_file():
        return []
    return list(iter_jsonl(paths.lineage))


def load_digests(paths: ArchivePaths) -> dict[str, dict[str, str]]:
    """provider -> uid -> content digest, as of the latest generation."""
    found: dict[str, dict[str, str]] = {}
    if paths.digests.is_file():
        for row in iter_jsonl(paths.digests):
            found.setdefault(row["provider"], {})[row["uid"]] = row["digest"]
    return found


def _write_pending(path: Path, rows: Iterable[dict[str, Any]]) -> str:
    """Write the new baseline beside the real one and make it durable; return the sha256 of its
    bytes, which the ledger row will carry as the proof of which baseline it describes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with path.open("wb") as handle:
        for row in rows:
            line = (json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
            digest.update(line)
            handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    return digest.hexdigest()


def _commit_baseline(pending: Path, baseline: Path) -> None:
    """Move the pending baseline over the real one in a single atomic step."""
    pending.replace(baseline)
    with suppress(OSError):  # best effort: make the rename durable
        descriptor = os.open(baseline.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def finish_interrupted_commit(paths: ArchivePaths, ledger: list[dict[str, Any]]) -> str:
    """Settle a generation whose commit was cut short, before any diff is computed.

    A commit writes the new baseline to `<baseline>.pending`, appends the ledger row (which carries
    the sha256 of that file) and renames the pending file over the baseline. If the pending file is
    what the last row says it is, the row was written and only the rename is missing: finish it.
    Otherwise no row describes it (the crash came first, or the file is partial), and it is
    discarded. Either way the baseline and the ledger agree afterwards, whatever the sources hold
    now. Returns "" (nothing was pending), "finished" or "discarded".
    """
    pending = paths.digests_pending
    if not pending.is_file():
        return ""
    if ledger and ledger[-1].get("baseline") == _file_digest(pending):
        _commit_baseline(pending, paths.digests)
        return "finished"
    pending.unlink(missing_ok=True)
    return "discarded"


def record_generation(
    paths: ArchivePaths,
    manifest: dict[str, Any],
    digests: dict[str, dict[str, str]],
    *,
    read: Iterable[str],
    day: str = "",
    digest_store: Callable[[str], dict[str, str]] | None = None,
) -> dict[str, Any] | None:
    """Record the generation `manifest` is, against the one before; return its ledger row.

    `digests` are the digests of the providers this ingest read (provider -> uid -> digest).
    A provider listed in the manifest that this ingest did not read successfully keeps its
    previous digests (a merge carries it over, a failed read leaves its previous shards in
    place); a provider the manifest does not list, or lists as absent, holds nothing. A carried
    provider that has no digests yet (a store that predates the lineage, merged one provider at
    a time) is digested from the shards it already holds, through `digest_store(provider)`, so
    its conversations are part of this generation's baseline and not "added" by the next one.
    Nothing is recorded when the sources are unchanged (same fingerprint, no uid differs).

    The next ingest diffs against the baseline, so the baseline and the ledger must change
    together. They are two files, so a generation is committed in three steps, each safe to
    stop after: (1) the new baseline is written and synced to `<baseline>.pending`; (2) the ledger
    row is appended, carrying the sha256 of the pending file; (3) the pending file is renamed over
    the baseline. Every run begins with `finish_interrupted_commit`, which completes step 3 if the
    row exists and throws the pending file away if it does not, so what a crash leaves behind
    never depends on what the next run reads: changed sources are diffed against the baseline
    the ledger describes, and no generation is lost or recorded twice.

    The one thing recovery cannot reconstruct is a baseline file that is simply gone (no pending
    file either): it is rewritten from the current content, without a new generation, only when
    the sources are those of the last row (same fingerprint). Otherwise the next generation lists
    every conversation as added.
    """
    ledger = load_lineage(paths)
    finish_interrupted_commit(paths, ledger)
    baseline_present = paths.digests.is_file()
    previous = load_digests(paths)
    read_now = set(read)
    current: dict[str, dict[str, str]] = {}
    for name, info in manifest["providers"].items():
        if name in read_now and info.get("status") == "ok":
            current[name] = dict(digests.get(name, {}))
        elif info.get("status") != "absent":
            if name in previous:
                current[name] = previous[name]
            elif digest_store is not None:
                current[name] = digest_store(name)
    before = {uid: digest for rows in previous.values() for uid, digest in rows.items()}
    after = {uid: digest for rows in current.values() for uid, digest in rows.items()}
    added = sorted(after.keys() - before.keys())
    removed = sorted(before.keys() - after.keys())
    changed = sorted(uid for uid in after.keys() & before.keys() if after[uid] != before[uid])
    fingerprint = manifest["source_fingerprint"]
    record: dict[str, Any] | None = {
        "generation": len(ledger) + 1,
        "fingerprint": fingerprint,
        "parent": ledger[-1]["fingerprint"] if ledger else "",
        "day": day,
        "conversations": len(after),
        "added": added,
        "changed": changed,
        "removed": removed,
    }
    same_sources = bool(ledger) and ledger[-1]["fingerprint"] == fingerprint
    if same_sources and (not (added or changed or removed) or not baseline_present):
        record = None

    def baseline_rows() -> Iterable[dict[str, Any]]:
        return (
            {"provider": name, "uid": uid, "digest": digest}
            for name, rows in sorted(current.items())
            for uid, digest in sorted(rows.items())
        )

    if record is None:
        write_jsonl_atomic(paths.digests, baseline_rows())
        return None
    record["baseline"] = _write_pending(paths.digests_pending, baseline_rows())
    write_jsonl_atomic(paths.lineage, [*ledger, record])
    _commit_baseline(paths.digests_pending, paths.digests)
    return record


def history_by_uid(ledger: list[dict[str, Any]]) -> dict[str, list[tuple[int, str]]]:
    """For each uid, the (generation, kind) pairs that touched it, newest first.

    `kind` is `added`, `changed` or `removed`.
    """
    found: dict[str, list[tuple[int, str]]] = {}
    for record in ledger:
        for kind in CHANGE_KINDS:
            for uid in record[kind]:
                found.setdefault(uid, []).append((record["generation"], kind))
    return {uid: sorted(entries, reverse=True) for uid, entries in found.items()}
