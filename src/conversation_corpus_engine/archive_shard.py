"""Provider-month shards and the deterministic folder plan.

Two ideas keep every folder and every data file small without losing the way back:

* The normalized layer is sliced by provider and month. A conversation lands in the shard of the
  month it was created in (`undated` when the export gave none), and a month that outgrows the
  shard size rolls to a numbered sibling. A row is never cut, so the largest conversation is also
  the lower bound of a shard.
* The vault and the site place notes and pages in a folder per month, and spill a month that
  outgrows the folder limit into a folder per day, then into hash buckets. The plan is a pure
  function of the conversations, so a rebuild puts every file where it was.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import OrderedDict, defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import IO, Any

from . import archive_layout as layout

UNDATED = layout.UNDATED_DIRNAME
_MONTH_STAMP_RE = re.compile(r"^(\d{4})-(\d{2})")
_DAY_STAMP_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
# `shard_subject` pads to three digits but never caps the number, so a month may pass shard 999.
_SEQUENCE_RE = re.compile(r"^(?P<month>\d{6}|undated)(?:-(?P<sequence>\d{3,}))?$")
MAX_OPEN_SHARDS = 16
# A bucket is planned at this share of the folder limit, so hash imbalance rarely forces a retry.
BUCKET_FILL = 0.7


def month_key(stamp: str) -> str:
    """`yyyymm` of a normalized timestamp, or `undated` when it holds no real month."""
    match = _MONTH_STAMP_RE.match(stamp or "")
    if not match or not 1 <= int(match.group(2)) <= 12:
        return UNDATED
    return f"{match.group(1)}{match.group(2)}"


def day_key(stamp: str) -> str:
    """`yyyymmdd` of a normalized timestamp, or `` when it holds no real calendar day."""
    match = _DAY_STAMP_RE.match(stamp or "")
    if not match:
        return ""
    year, month, day = (int(group) for group in match.groups())
    if not (1 <= month <= 12 and 1 <= day <= 31 and year >= 1000):
        return ""
    return f"{match.group(1)}{match.group(2)}{match.group(3)}"


def shard_subject(month: str, sequence: int = 1) -> str:
    """The subject word of a shard: the month, then `-NNN` from the second shard of a month."""
    return month if sequence <= 1 else f"{month}-{sequence:03d}"


def parse_shard_subject(subject: str) -> tuple[str, int]:
    """Inverse of `shard_subject`; raises ValueError for a subject this module did not mint."""
    match = _SEQUENCE_RE.match(subject)
    if not match:
        raise ValueError(f"not a shard subject: {subject!r}")
    return match.group("month"), int(match.group("sequence") or 1)


def shard_sort_key(subject: str) -> tuple[int, str, int]:
    """Months in calendar order, `undated` last, a month's numbered shards in order."""
    month, sequence = parse_shard_subject(subject)
    return (1 if month == UNDATED else 0, month, sequence)


class ShardWriter:
    """Stream conversations into provider-month shards, with the index rows beside them.

    Rows go to the open shard of their month; a month rolls to the next numbered shard before a
    row that would take the current one past `shard_bytes` (a lone oversize row gets a shard of
    its own). At most `MAX_OPEN_SHARDS` files are open at once, so a store of hundreds of months
    never runs out of file descriptors.
    """

    def __init__(self, directory: Path, provider: str, *, shard_bytes: int) -> None:
        self.directory = directory
        self.provider = provider
        self.shard_bytes = shard_bytes
        directory.mkdir(parents=True, exist_ok=True)
        self._handles: OrderedDict[Path, IO[bytes]] = OrderedDict()
        self._sequence: dict[str, int] = {}
        self._bytes: dict[tuple[str, int], int] = defaultdict(int)
        self._rows: dict[tuple[str, int], int] = defaultdict(int)

    def _handle(self, path: Path) -> IO[bytes]:
        handle = self._handles.get(path)
        if handle is not None:
            self._handles.move_to_end(path)
            return handle
        handle = path.open("ab")
        self._handles[path] = handle
        while len(self._handles) > MAX_OPEN_SHARDS:
            _, oldest = self._handles.popitem(last=False)
            oldest.close()
        return handle

    def add(self, month: str, conversation_line: bytes, index_line: bytes) -> str:
        """Append one conversation row and its index row; returns the subject of its shard."""
        sequence = self._sequence.get(month, 1)
        used = self._bytes[(month, sequence)]
        if used and used + len(conversation_line) > self.shard_bytes:
            sequence += 1
            self._sequence[month] = sequence
        subject = shard_subject(month, sequence)
        self._handle(self.directory / layout.shard_filename(self.provider, subject)).write(
            conversation_line
        )
        self._handle(self.directory / layout.index_shard_filename(self.provider, subject)).write(
            index_line
        )
        self._bytes[(month, sequence)] += len(conversation_line)
        self._rows[(month, sequence)] += 1
        return subject

    def close(self) -> list[dict[str, Any]]:
        """Close every file and describe the shards, in the order they are read."""
        while self._handles:
            _, handle = self._handles.popitem(last=False)
            handle.close()
        shards = [
            {
                "subject": shard_subject(month, sequence),
                "conversations": self._rows[(month, sequence)],
                "bytes": self._bytes[(month, sequence)],
            }
            for month, sequence in self._rows
        ]
        return sorted(shards, key=lambda shard: shard_sort_key(shard["subject"]))


# --- the folder plan --------------------------------------------------------------------------


def _bucket_index(uid: str, buckets: int) -> int:
    return int(hashlib.sha256(uid.encode("utf-8")).hexdigest()[:8], 16) % buckets


def _bucket_path(index: int, buckets: int, limit: int) -> str:
    """The folder path of leaf bucket `index` among `buckets`, no level holding more than `limit`.

    One level while the buckets fit `limit`; otherwise the index is written in base `limit`, one
    bucket folder per digit, so even a tiny limit keeps every folder inside it.
    """
    base = max(limit, 2)
    digits: list[int] = []
    remaining, span = index, buckets
    while True:
        digits.append(remaining % base)
        remaining //= base
        span = math.ceil(span / base)
        if span <= 1:
            break
    return "/".join(layout.bucket_folder(digit) for digit in reversed(digits))


def _spill(members: list[tuple[str, int]], limit: int) -> dict[str, str]:
    """Assign members (uid, weight) to hash buckets so no bucket's weight passes `limit`.

    Returns uid -> the bucket's folder path (`bucket-…`, or nested ones when there are more
    buckets than `limit`).
    """
    total = sum(weight for _, weight in members)
    buckets = max(2, math.ceil(total / (limit * BUCKET_FILL)))
    while True:
        loads: dict[int, int] = defaultdict(int)
        assignment = {}
        for uid, weight in members:
            index = _bucket_index(uid, buckets)
            assignment[uid] = index
            loads[index] += weight
        if max(loads.values()) <= limit:
            return {uid: _bucket_path(index, buckets, limit) for uid, index in assignment.items()}
        buckets *= 2


def plan_conversation_folders(items: Iterable[tuple[str, str, int]], limit: int) -> dict[str, str]:
    """Place conversations in folders: uid -> relative folder path (POSIX separators).

    `items` are `(uid, stamp, weight)`: the weight is how many entries the conversation adds to its
    folder (1 for a note, 2 when it also has a folder of parts). A month folder holds its
    conversations directly while their weight fits `limit`; otherwise it holds one folder per
    calendar day, and a day that is still too full is spread over hash buckets. Undated
    conversations go to `undated` and spill into buckets directly.
    """
    by_month: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
    for uid, stamp, weight in items:
        by_month[month_key(stamp)].append((uid, stamp, weight))
    plan: dict[str, str] = {}
    for month, members in sorted(by_month.items()):
        folder = layout.month_folder(month)
        if sum(weight for _, _, weight in members) <= limit:
            plan.update({uid: folder for uid, _, _ in members})
            continue
        by_day: dict[str, list[tuple[str, int]]] = defaultdict(list)
        if month == UNDATED:
            by_day[""] = [(uid, weight) for uid, _, weight in members]
        else:
            for uid, stamp, weight in members:
                by_day[day_key(stamp)].append((uid, weight))
        # A month has at most 31 days, so day folders only outgrow `limit` under a tiny limit;
        # then they sit one level down in hash buckets so the month folder stays inside it.
        days = sorted(day for day in by_day if day)
        day_parent: dict[str, str] = {}
        if len(days) > limit:
            for day, bucket in _spill([(day, 1) for day in days], limit).items():
                day_parent[day] = f"{folder}/{bucket}"
        for day, group in sorted(by_day.items()):
            parent = day_parent.get(day, folder)
            where = f"{parent}/{layout.day_folder(day)}" if day else folder
            if sum(weight for _, weight in group) <= limit:
                plan.update({uid: where for uid, _ in group})
                continue
            for uid, bucket in _spill(group, limit).items():
                plan[uid] = f"{where}/{bucket}"
    return plan


def plan_flat_folders(names: Iterable[str], limit: int) -> dict[str, str]:
    """Place a flat set of files: name -> `` while they fit `limit`, else a hash bucket folder."""
    unique = sorted(set(names))
    if len(unique) <= limit:
        return {name: "" for name in unique}
    return _spill([(name, 1) for name in unique], limit)


def part_folder(position: int, total: int, limit: int) -> str:
    """The sub-folder of part `position` (0-based) among `total`: `` while they fit `limit`."""
    if total <= limit:
        return ""
    return _bucket_path(position // limit, math.ceil(total / limit), limit)
