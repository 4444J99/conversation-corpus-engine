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

from collections.abc import Callable, Iterable
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
    Nothing is written when the sources are unchanged (same fingerprint, no uid differs).
    """
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
    ledger = load_lineage(paths)
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
    if ledger and ledger[-1]["fingerprint"] == fingerprint and not (added or changed or removed):
        record = None
    write_jsonl_atomic(
        paths.digests,
        (
            {"provider": name, "uid": uid, "digest": digest}
            for name, rows in sorted(current.items())
            for uid, digest in sorted(rows.items())
        ),
    )
    if record is not None:
        write_jsonl_atomic(paths.lineage, [*ledger, record])
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
