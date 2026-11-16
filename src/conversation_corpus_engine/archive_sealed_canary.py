"""Read-only, bounded literal-canary checks; diagnostics never echo inspected content.

This checks the working tree and, when present, reachable local Git objects.
It is a planted-marker regression check, not a general secret detector or proof
of cryptographic security. It never creates credentials, commits or a remote.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import selectors
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

READ_BYTES = 65536
DEFAULT_BUDGET = 512 * 1024 * 1024
MAX_GIT_OBJECT = 48 * 1024 * 1024
GIT_TIMEOUT = 30
DEFAULT_SECONDS = 300.0
MAX_MARKER_BYTES = 4096
MAX_MARKERS = 128


class CanaryError(ValueError):
    """Fixed non-sensitive error codes only."""


class _Scanner:
    def __init__(self, markers: Sequence[bytes], budget: int, max_seconds: float, clock=None):
        if (
            not markers
            or len(markers) > MAX_MARKERS
            or any(not isinstance(m, bytes) or not 1 <= len(m) <= MAX_MARKER_BYTES for m in markers)
        ):
            raise CanaryError("invalid-markers")
        if type(budget) is not int or budget < 1:
            raise CanaryError("invalid-budget")
        if not math.isfinite(max_seconds) or max_seconds <= 0:
            raise CanaryError("invalid-deadline")
        self.clock = clock or time.monotonic
        self.deadline = self.clock() + max_seconds
        self.markers = list(dict.fromkeys(markers))
        self.budget = budget
        self.inspected = 0
        self.matched: set[int] = set()
        self.overlap = max(map(len, self.markers)) - 1

    def check(self) -> None:
        if self.clock() >= self.deadline:
            raise CanaryError("scan-deadline-exceeded")

    def inspect(self, data: bytes, tail: bytes = b"") -> bytes:
        self.check()
        self.inspected += len(data)
        if self.inspected > self.budget:
            raise CanaryError("scan-budget-exceeded")
        combined = tail + data
        self.matched.update(i for i, marker in enumerate(self.markers) if marker in combined)
        return combined[-self.overlap :] if self.overlap else b""

    def file(self, path: Path) -> None:
        self.check()
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode):
            raise CanaryError("symlink-refused")
        if not stat.S_ISREG(before.st_mode):
            raise CanaryError("non-regular-file")
        fd = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        with os.fdopen(fd, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise CanaryError("non-regular-file")
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise CanaryError("tree-changed-during-scan")
            tail = b""
            while chunk := handle.read(READ_BYTES):
                tail = self.inspect(chunk, tail)
            after = os.fstat(handle.fileno())
        if any(
            getattr(before, field) != getattr(after, field)
            for field in ("st_size", "st_mtime_ns", "st_ctime_ns")
        ):
            raise CanaryError("tree-changed-during-scan")
        self.check()

    def git(self, root: Path, *args: str, limit: int) -> bytes:
        self.check()
        limit = min(limit, self.budget - self.inspected)
        if limit < 1:
            raise CanaryError("scan-budget-exceeded")
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_OPTIONAL_LOCKS="0",
            GIT_TERMINAL_PROMPT="0",
            GIT_NO_LAZY_FETCH="1",
        )
        try:
            with subprocess.Popen(
                ["git", "--no-replace-objects", "-C", str(root), *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
            ) as process:
                try:
                    assert process.stdout is not None
                    data = bytearray()
                    tail = b""
                    deadline = min(self.deadline, self.clock() + GIT_TIMEOUT)
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stdout, selectors.EVENT_READ)
                        while True:
                            self.check()
                            remaining = deadline - self.clock()
                            if remaining <= 0 or not selector.select(remaining):
                                self.check()
                                raise CanaryError("git-inspection-failed")
                            chunk = os.read(
                                process.stdout.fileno(), min(READ_BYTES, limit + 1 - len(data))
                            )
                            if not chunk:
                                break
                            if len(data) + len(chunk) > limit:
                                raise CanaryError("scan-budget-exceeded")
                            tail = self.inspect(chunk, tail)
                            data.extend(chunk)
                    if process.wait(timeout=max(0.001, deadline - self.clock())):
                        raise CanaryError("git-inspection-failed")
                    self.check()
                    return bytes(data)
                except BaseException:
                    process.kill()
                    process.wait()
                    raise
        except (OSError, subprocess.SubprocessError) as exc:
            raise CanaryError("git-inspection-failed") from exc


def scan(
    root: Path,
    markers: Sequence[bytes],
    *,
    budget: int = DEFAULT_BUDGET,
    max_seconds: float = DEFAULT_SECONDS,
    clock: Callable[[], float] | None = None,
) -> dict:
    """Inspect literal byte markers without modifying the selected tree or Git state.

    The budget includes file/path bytes and Git inspection output (so committed
    data is normally counted twice). Git history means reachable local objects;
    reflogs, unreachable/remote-only objects and transformed markers are outside
    this report. Run the authenticated sealed-history audit separately.
    """
    checker = _Scanner(markers, budget, max_seconds, clock)
    root = Path(os.path.abspath(root.expanduser()))  # noqa: PTH100 - check before resolving
    try:
        if any(p.is_symlink() for p in (root, *root.parents)):
            raise CanaryError("symlink-refused")
        if not root.is_dir():
            raise CanaryError("tree-unavailable")

        def unreadable(_error: OSError) -> None:
            raise CanaryError("tree-inspection-failed")

        files = 0
        for base, dirs, names in os.walk(root, followlinks=False, onerror=unreadable):
            checker.check()
            directory = Path(base)
            for name in [*dirs, *names]:
                checker.check()
                path = directory / name
                if path.is_symlink():
                    raise CanaryError("symlink-refused")
            if directory == root:
                dirs[:] = [name for name in dirs if name != ".git"]
            scan_names = [name for name in names if directory != root or name != ".git"]
            for name in dirs:
                checker.inspect(os.fsencode((directory / name).relative_to(root)))
            for name in scan_names:
                path = directory / name
                checker.inspect(os.fsencode(path.relative_to(root)))
                checker.file(path)
                files += 1
        git_history = (root / ".git").exists()
        objects = 0
        if git_history:
            shallow = checker.git(root, "rev-parse", "--is-shallow-repository", limit=128).strip()
            if shallow != b"false":
                raise CanaryError("incomplete-git-history")
            refs = checker.git(root, "for-each-ref", "--format=%(refname)", limit=8 * 1024 * 1024)
            del refs
            raw = checker.git(
                root, "rev-list", "--objects", "--all", "--no-object-names", limit=8 * 1024 * 1024
            )
            for oid in dict.fromkeys(raw.splitlines()):
                if not re.fullmatch(rb"[0-9a-f]{40}(?:[0-9a-f]{24})?", oid):
                    raise CanaryError("git-inspection-failed")
                kind = checker.git(root, "cat-file", "-t", oid.decode("ascii"), limit=128).strip()
                if kind not in (b"blob", b"tree", b"commit", b"tag"):
                    raise CanaryError("git-inspection-failed")
                # Raw tree objects preserve non-ASCII names; pretty printing
                # escapes them and could hide a literal filename marker.
                checker.git(
                    root,
                    "cat-file",
                    kind.decode("ascii"),
                    oid.decode("ascii"),
                    limit=MAX_GIT_OBJECT,
                )
                objects += 1
        checker.check()
        return {
            "schema": "cce-vault-canary.v1",
            "passed": not checker.matched,
            "markers": len(checker.markers),
            "matched_markers": len(checker.matched),
            "working_files": files,
            "git_history_scanned": git_history,
            "git_objects": objects,
            "inspected_bytes": checker.inspected,
        }
    except OSError as exc:
        raise CanaryError("tree-inspection-failed") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only literal-canary scan of a vault tree.")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument(
        "--markers", type=Path, required=True, help="Private JSON array of marker strings"
    )
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    parser.add_argument("--max-seconds", type=float, default=DEFAULT_SECONDS)
    args = parser.parse_args(argv)
    try:
        with args.markers.open("rb") as handle:
            raw = handle.read(65537)
        if len(raw) > 65536:
            raise CanaryError("invalid-markers")
        values = json.loads(raw)
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise CanaryError("invalid-markers")
        result = scan(
            args.repo,
            [value.encode("utf-8") for value in values],
            budget=args.budget,
            max_seconds=args.max_seconds,
        )
        print(json.dumps(result, sort_keys=True))
        return 0 if result["passed"] else 1
    except CanaryError as exc:
        print(f"[canary] {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError):
        print("[canary] inspection-failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
