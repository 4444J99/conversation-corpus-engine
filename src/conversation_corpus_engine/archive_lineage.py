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

The ledger and the digest file vouch for each other: a row carries the sha256 of the baseline it
describes. When the two disagree and nothing explains it (no pending baseline that the row
describes), the baseline has been altered, deleted or restored from elsewhere, and a new
generation diffed against it would be wrong; that is `LineageError`, never a silent repair.
`cce archive build --rebuild-lineage` is the explicit remedy.
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
# The words verify and the ingest refusal share, so one search finds either.
BASELINE_DIVERGED = "lineage baseline diverged from the ledger"
REBUILD_REMEDY = "cce archive build --rebuild-lineage"


class LineageError(ValueError):
    """The lineage cannot be trusted or read: a damaged ledger, or a baseline the ledger does not
    describe. Nothing has been changed when this is raised."""


def read_ledger(paths: ArchivePaths) -> tuple[list[dict[str, Any]], str]:
    """The ledger rows that can be read, and why the rest cannot ("" when all of it can).

    A line that is not a JSON object (a write cut short leaves one at the end) ends the readable
    rows: it and everything after it is the unreadable remainder.
    """
    if not paths.lineage.is_file():
        return [], ""
    rows: list[dict[str, Any]] = []
    for number, raw in enumerate(paths.lineage.read_bytes().splitlines(), 1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw.decode("utf-8"))
        except ValueError:  # includes UnicodeDecodeError and json.JSONDecodeError
            return rows, f"line {number} is not JSON (the file may have been cut short)"
        if not isinstance(row, dict):
            return rows, f"line {number} is not a generation row"
        rows.append(row)
    return rows, ""


def load_lineage(paths: ArchivePaths) -> list[dict[str, Any]]:
    """The generations recorded so far, oldest first (an archive with none has an empty list).

    A ledger with an unreadable line raises `LineageError` naming the file and the remedy: the
    rows after it would be guesses, and a bare JSON error says nothing about what to do.
    """
    rows, damage = read_ledger(paths)
    if damage:
        raise LineageError(
            f"{paths.lineage.name} is damaged: {damage}, after {len(rows)} readable "
            f"generation(s). Restore it from a copy, or run `{REBUILD_REMEDY}`: that keeps "
            f"every readable row, saves a copy of the damaged file as {paths.lineage.name}.damaged "
            f"and records one generation flagged rebuilt"
        )
    return rows


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


def baseline_state(paths: ArchivePaths, ledger: list[dict[str, Any]]) -> str:
    """How the digest file and its pending file stand against what the last ledger row says.

    * `none`: the row carries no baseline hash (an empty ledger, or one written before rows
      carried it): there is nothing to compare;
    * `finish`: the pending file is exactly what the row describes, so only the rename is missing;
    * `agree`: the digest file is what the row describes (a pending file beside it is stale);
    * `missing`: there is no digest file and no pending file the row describes;
    * `diverged`: the digest file is not what the row describes and no pending file is either.
      A crash cannot leave this behind (it leaves `finish` or `agree`); a deleted or edited
      file does, and so does a digest file restored from another time.
    """
    expected = ledger[-1].get("baseline") if ledger else None
    if not expected:
        return "none"
    pending = paths.digests_pending
    if pending.is_file() and _file_digest(pending) == expected:
        return "finish"
    if not paths.digests.is_file():
        return "missing"
    return "agree" if _file_digest(paths.digests) == expected else "diverged"


def diverged_message(paths: ArchivePaths, ledger: list[dict[str, Any]]) -> str:
    return (
        f"{BASELINE_DIVERGED}: {paths.digests.name} is not the baseline that generation "
        f"{ledger[-1].get('generation', len(ledger))} of {paths.lineage.name} records, and no "
        f"{paths.digests_pending.name} describes it either, so the file was altered, deleted or "
        "restored from another time. A new generation would be diffed against a stale baseline "
        f"and repeat what the ledger already holds. If the ledger is right, restore "
        f"{paths.digests.name}; otherwise run `{REBUILD_REMEDY}`, which re-baselines from the "
        "current sources and appends one generation flagged rebuilt (it deletes no ledger row)"
    )


def check_lineage(paths: ArchivePaths) -> None:
    """Raise `LineageError` if the lineage cannot be trusted, before an ingest reads anything."""
    ledger = load_lineage(paths)
    if baseline_state(paths, ledger) == "diverged":
        raise LineageError(diverged_message(paths, ledger))


def finish_interrupted_commit(
    paths: ArchivePaths, ledger: list[dict[str, Any]], *, rebuild: bool = False
) -> str:
    """Settle a generation whose commit was cut short, before any diff is computed.

    A commit writes the new baseline to `<baseline>.pending`, appends the ledger row (which carries
    the sha256 of that file) and renames the pending file over the baseline. If the pending file is
    what the last row says it is, the row was written and only the rename is missing: finish it.
    If it is not and the baseline is what the row describes, no row describes the pending file
    (the crash came first, or the file is partial) and it is discarded. Either way the baseline and
    the ledger agree afterwards, whatever the sources hold now.

    If neither the baseline nor a pending file is what the last row describes (`diverged`), the
    ledger holds a generation whose baseline is gone: that is `LineageError`, and the pending file
    is left where it is as evidence. `rebuild` accepts it and discards the pending file, because
    the baseline is about to be written again from the current sources. Returns "" (nothing was
    pending), "finished" or "discarded".
    """
    state = baseline_state(paths, ledger)
    if state == "diverged" and not rebuild:
        raise LineageError(diverged_message(paths, ledger))
    pending = paths.digests_pending
    if not pending.is_file():
        return ""
    if state == "finish":
        _commit_baseline(pending, paths.digests)
        return "finished"
    pending.unlink(missing_ok=True)
    return "discarded"


def _save_damaged_ledger(paths: ArchivePaths) -> None:
    """Keep a copy of a damaged ledger before a rebuild rewrites it; an earlier copy stays."""
    data = paths.lineage.read_bytes()
    target = paths.lineage.with_name(paths.lineage.name + ".damaged")
    number = 1
    while target.exists():
        number += 1
        target = paths.lineage.with_name(f"{paths.lineage.name}.damaged-{number}")
    target.write_bytes(data)


def record_generation(
    paths: ArchivePaths,
    manifest: dict[str, Any],
    digests: dict[str, dict[str, str]],
    *,
    read: Iterable[str],
    day: str = "",
    digest_store: Callable[[str], dict[str, str]] | None = None,
    rebuild: bool = False,
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

    A baseline that exists but is not the one the last row describes, with no pending file that
    is, raises `LineageError` (see `baseline_state`): recording on top of it would diff the
    sources against a stale baseline and write the same generation twice. So does a damaged
    ledger. `rebuild` is the explicit remedy and appends exactly one row, flagged
    `"rebuilt": true`: every readable row stays, a damaged ledger is first copied to
    `<ledger>.damaged`, the baseline is digested again from the current sources (the stale one is
    not trusted, so a provider this ingest did not read is digested from its stored shards), and
    the row lists no change, because the baseline that would say what changed is the one that
    cannot be trusted.
    """
    if rebuild:
        ledger, damage = read_ledger(paths)
        if damage:
            _save_damaged_ledger(paths)
    else:
        ledger = load_lineage(paths)
    finish_interrupted_commit(paths, ledger, rebuild=rebuild)
    baseline_present = paths.digests.is_file()
    previous = {} if rebuild else load_digests(paths)
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
    after = {uid: digest for rows in current.values() for uid, digest in rows.items()}
    before = (
        after
        if rebuild
        else {uid: digest for rows in previous.values() for uid, digest in rows.items()}
    )
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
    if rebuild:
        record["rebuilt"] = True
    same_sources = bool(ledger) and ledger[-1]["fingerprint"] == fingerprint
    if not rebuild and same_sources and (not (added or changed or removed) or not baseline_present):
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
