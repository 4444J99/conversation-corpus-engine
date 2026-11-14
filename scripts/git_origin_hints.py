#!/usr/bin/env python3
"""Derive authorship hints for repository files from git history alone (stdlib only).

Library documents that came out of software repositories can only be told apart by who wrote
them in git: a commit that carries a `Co-Authored-By: Claude`-style trailer, a bot author or an
agent footer is agent-signed text. The library's backup copies carry no `.git`, so the history is
fetched separately as history-only clones (bare or blobless, `<name>.git` or `<name>/` inside one
directory) and read here.

    git_origin_hints.py --repos-dir CLONES --wanted wanted.jsonl --out hints.jsonl
    git_origin_hints.py --repos-dir CLONES --all --out hints.jsonl

`wanted.jsonl` has one `{"repo": "<name>", "path": "<repo-relative posix path>"}` per line.
The output has one `cce.origin-hints.v1` row per file with history, sorted by repo and path:

    {"schema": "cce.origin-hints.v1", "repo": ..., "path": ...,
     "origin": "agent" | "mixed" | "human",
     "commits": {"total": n, "agent": a, "human": h},
     "signals": [sorted unique strings such as "trailer:co-authored-by:claude"],
     "basis": "git-log"}

A commit is agent-signed when any of these holds: a `Co-authored-by:` line (any case, anywhere in
the message) names an AI by name or email; the author or the committer name or
email names an AI; the message ends with the agent footer (`Generated with [Claude Code]`, or a
line starting with the robot emoji and `Generated with`). An AI is named when a whole word of
the name or email is one of claude, anthropic, gemini, chatgpt, openai, copilot, codex, opencode,
cursor, antigravity, aider, devin, jules, dependabot, renovate, the pair `github` `actions`, when
the text holds `[bot]`, or the address `noreply@anthropic.com`. A file is `agent` when every
commit touching it is agent-signed, `human` when none is, otherwise `mixed`.

One streaming `git log --no-merges --no-renames --name-only -z` pass is made per repository (never
one git call per file); memory is proportional to the number of distinct files kept, not to the
number of commits. Progress goes to standard error as counts only: never a commit message, an
author name or an author email.

WHAT THIS CANNOT SEE
- Renames are not followed: a renamed file starts its history at the rename commit, and the
  commits before it belong to the old path (which is listed too, with `--all`).
- A squashed or hand-rewritten commit by a human may contain agent text: it counts as human
  unless the message still holds a `Co-authored-by:` line of an AI (a local squash keeps them).
  A line that merely quotes such a trailer is counted too: this errs toward agent.
- A commit that deletes a file counts as touching it (so a human removing or moving an agent
  file makes it `mixed`).
- A human committing agent output without a trailer, a footer or an agent identity counts as
  human; an agent committing under a human identity does too.
- Text that existed before the file entered git, or lived outside git, has no history here.
- Files whose history was rewritten (squash, filter, force-push) show only the rewritten history.
- Only commits reachable from `HEAD` are read; merge commits are excluded, so text that exists
  only in a merge resolution is not seen.
- A person whose name or address holds one of the AI words as a whole word (a first name such as
  Jules or Claude) is read as that AI. The `signals` show which word matched, so a wrong match
  can be found; it only ever removes owner credit downstream.
- With `--max-commits-per-repo` only the newest commits are read; every row of such a repository
  carries the signal `window:max-commits`, and older commits are unseen.
- A path that differs only in Unicode normalisation is the same file: paths are NFC-normalised.

Exit status: 0 done; 2 bad arguments or input, or a requested repository is missing (names only
are reported and nothing is written); 3 git failed on a repository (nothing is written).
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
from collections.abc import Iterator
from pathlib import Path
from typing import Any

SCHEMA = "cce.origin-hints.v1"
BASIS = "git-log"
WINDOW_SIGNAL = "window:max-commits"

AI_WORDS = frozenset(
    {
        "claude",
        "anthropic",
        "gemini",
        "chatgpt",
        "openai",
        "copilot",
        "codex",
        "opencode",
        "cursor",
        "antigravity",
        "aider",
        "devin",
        "jules",
        "dependabot",
        "renovate",
    }
)
ANTHROPIC_NOREPLY = "noreply@anthropic.com"
_WORD_SPLIT = re.compile(r"[\W_]+")
_TRAILER = re.compile(r"^co-authored-by:\s*(.*)$", re.IGNORECASE)
_NAME_EMAIL = re.compile(r"^(.*?)\s*<([^>]*)>\s*$")
FOOTER_LINES_CHECKED = 3
READ_SIZE = 1 << 20
EXIT_INPUT = 2
EXIT_GIT = 3


class InputError(Exception):
    """A problem with the arguments or the input files."""


class GitFailure(Exception):
    """Git failed on, or produced output that cannot be read for, one repository."""


def ai_labels(text: str) -> frozenset[str]:
    """Which AI names a name or an address holds as whole words."""
    lowered = unicodedata.normalize("NFKC", text).casefold()
    found = set()
    if "[bot]" in lowered:
        found.add("bot")
    if ANTHROPIC_NOREPLY in lowered:
        found.add(ANTHROPIC_NOREPLY)
    words = [word for word in _WORD_SPLIT.split(lowered) if word]
    found.update(word for word in words if word in AI_WORDS)
    if any(pair == ("github", "actions") for pair in zip(words, words[1:], strict=False)):
        found.add("github-actions")
    return frozenset(found)


@functools.lru_cache(maxsize=65536)
def identity_signals(
    author_name: str, author_email: str, committer_name: str, committer_email: str
) -> frozenset[str]:
    signals: set[str] = set()
    for prefix, value in (
        ("author", author_name),
        ("author-email", author_email),
        ("committer", committer_name),
        ("committer-email", committer_email),
    ):
        signals.update(f"{prefix}:{label}" for label in ai_labels(value))
    return frozenset(signals)


def message_signals(message: str) -> frozenset[str]:
    """Trailer and footer signals of a commit message. A `Co-authored-by:` line counts wherever
    it stands in the message (a squash message holds the trailers of every squashed commit in
    its middle); the agent footer counts when it is among the last non-empty lines."""
    signals: set[str] = set()
    lines = [line.rstrip() for line in message.replace("\r\n", "\n").split("\n")]
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return frozenset()
    for line in lines:
        match = _TRAILER.match(line.strip())
        if not match:
            continue
        value = match.group(1)
        named = _NAME_EMAIL.match(value)
        parts = named.groups() if named else (value, "")
        for part in parts:
            signals.update(f"trailer:co-authored-by:{label}" for label in ai_labels(part))
    tail = [line.strip() for line in lines if line.strip()][-FOOTER_LINES_CHECKED:]
    if any(
        "generated with [claude code]" in line.casefold()
        or line.startswith("\N{ROBOT FACE} Generated with")
        for line in tail
    ):
        signals.add("footer:generated-with")
    return frozenset(signals)


# --- reading git ----------------------------------------------------------------------------


def _tokens(stream: Any) -> Iterator[bytes]:
    """The NUL-separated tokens of a binary stream, in bounded chunks."""
    rest = b""
    while True:
        chunk = stream.read(READ_SIZE)
        if not chunk:
            break
        pieces = (rest + chunk).split(b"\0")
        rest = pieces.pop()
        yield from pieces
    if rest:
        yield rest


class Commit:
    __slots__ = ("files", "signals")

    def __init__(self, signals: frozenset[str], files: list[bytes]) -> None:
        self.signals = signals
        self.files = files


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def commits_of(stream: Any) -> Iterator[Commit]:
    """Parse `git log -z --name-only --format=%x00%H%x00%an%x00%ae%x00%cn%x00%ce%x00%B`.

    Every commit starts with an empty token (a file name is never empty), then holds six header
    tokens, then its file names; the first file name carries one leading newline git adds. Git
    cannot put a NUL into a name, an address or a message, so the grammar is unambiguous."""
    header: list[bytes] = []
    files: list[bytes] = []
    inside = False
    first_file = True

    def finish() -> Commit:
        signals = identity_signals(*(_decode(item) for item in header[1:5])) | message_signals(
            _decode(header[5])
        )
        return Commit(signals, files)

    for token in _tokens(stream):
        if not token and not (inside and len(header) < 6):
            if inside:
                yield finish()
            header, files, inside, first_file = [], [], True, True
            continue
        if not inside:
            raise ValueError("unexpected output from git log")
        if len(header) < 6:
            header.append(token)
            continue
        name = token[1:] if first_file and token.startswith(b"\n") else token
        first_file = False
        if name:
            files.append(name)
    if inside:
        if len(header) < 6:
            raise ValueError("git log output ended inside a commit")
        yield finish()


def normal_path(raw: str) -> str:
    return unicodedata.normalize("NFC", raw.replace("\\", "/"))


class RepoResult:
    def __init__(self) -> None:
        self.commits = 0
        self.counts: dict[str, list[int]] = {}
        self.masks: dict[str, int] = {}
        self.window_full = False


class SignalIndex:
    """Signals as bits, so a file costs two small integers however many signals it saw."""

    def __init__(self) -> None:
        self.bits: dict[str, int] = {}
        self.names: list[str] = []

    def mask(self, signals: frozenset[str]) -> int:
        value = 0
        for signal in signals:
            if signal not in self.bits:
                self.bits[signal] = len(self.names)
                self.names.append(signal)
            value |= 1 << self.bits[signal]
        return value

    def names_of(self, mask: int) -> list[str]:
        return sorted(name for name, bit in self.bits.items() if mask >> bit & 1)


def read_repo(
    git_dir: Path,
    *,
    wanted: set[str] | None,
    limit: int | None,
    index: SignalIndex,
    progress: Any,
) -> RepoResult:
    """One streaming pass over the history reachable from HEAD."""
    result = RepoResult()
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1"}
    # Name the git directory explicitly, so a plain folder inside some other repository is not
    # mistaken for a repository of its own.
    inner = git_dir / ".git"
    explicit = f"--git-dir={inner if inner.exists() else git_dir}"
    probe = subprocess.run(
        ["git", explicit, "rev-parse", "--git-dir"],
        capture_output=True,
        check=False,
        env=environment,
    )
    if probe.returncode != 0:
        line = _decode(probe.stderr).strip().splitlines()[:1]
        raise subprocess.CalledProcessError(
            probe.returncode, "git rev-parse", stderr=line[0][:200] if line else ""
        )
    head = subprocess.run(
        ["git", explicit, "rev-parse", "--verify", "--quiet", "HEAD^{commit}"],
        capture_output=True,
        check=False,
        env=environment,
    )
    if head.returncode != 0:
        return result
    command = [
        "git",
        explicit,
        "-c",
        "core.quotepath=false",
        "-c",
        "diff.renames=false",
        "log",
        "--no-merges",
        "--no-renames",
        "--name-only",
        "-z",
        "--format=%x00%H%x00%an%x00%ae%x00%cn%x00%ce%x00%B",
    ]
    if limit is not None:
        command.append(f"--max-count={limit}")
    with (
        tempfile.TemporaryFile() as errors,
        subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=errors, env=environment
        ) as process,
    ):
        assert process.stdout is not None
        try:
            for commit in commits_of(process.stdout):
                result.commits += 1
                if result.commits % 50000 == 0:
                    progress(f"  {result.commits} commits read")
                mask = index.mask(commit.signals)
                agent = 1 if commit.signals else 0
                seen: set[str] = set()
                for raw in commit.files:
                    path = normal_path(raw.decode("utf-8", errors="surrogateescape"))
                    if path in seen or (wanted is not None and path not in wanted):
                        continue
                    seen.add(path)
                    entry = result.counts.setdefault(path, [0, 0])
                    entry[0] += 1
                    entry[1] += agent
                    if agent:
                        result.masks[path] = result.masks.get(path, 0) | mask
        except ValueError:
            process.kill()
            raise
        code = process.wait()
        errors.seek(0)
        error = errors.read(4096)
    if code != 0:
        line = _decode(error).strip().splitlines()[:1]
        raise subprocess.CalledProcessError(code, "git log", stderr=line[0][:200] if line else "")
    result.window_full = limit is not None and result.commits >= limit
    return result


# --- input and output -----------------------------------------------------------------------


def _clean_relative(value: Any, what: str, line: int) -> str:
    if not isinstance(value, str) or not value:
        raise InputError(f"wanted line {line}: {what} must be a non-empty string")
    text = normal_path(value)
    parts = text.split("/")
    if text.startswith("/") or any(part in ("", ".", "..") for part in parts):
        raise InputError(f"wanted line {line}: {what} must be a clean relative path")
    return text


def read_wanted(path: Path) -> dict[str, dict[str, set[str]]]:
    """repo -> NFC path -> the spellings the wanted file used for it."""
    wanted: dict[str, dict[str, set[str]]] = {}
    try:
        handle = path.open(encoding="utf-8")
    except OSError as error:
        raise InputError(f"cannot read the wanted file: {error.strerror}") from None
    with handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                raise InputError(f"wanted line {number}: not JSON") from None
            if not isinstance(row, dict):
                raise InputError(f"wanted line {number}: not an object")
            repo = _clean_relative(row.get("repo"), "repo", number)
            spelled = row.get("path")
            clean = _clean_relative(spelled, "path", number)
            wanted.setdefault(repo, {}).setdefault(clean, set()).add(spelled)
    return wanted


def find_repo(repos_dir: Path, name: str) -> Path | None:
    for candidate in (repos_dir / f"{name}.git", repos_dir / name):
        if candidate.is_dir():
            return candidate
    return None


def discover(repos_dir: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    try:
        children = sorted(repos_dir.iterdir())
    except OSError as error:
        raise InputError(f"cannot read the repositories directory: {error.strerror}") from None
    for child in children:
        if not child.is_dir():
            continue
        name = child.name.removesuffix(".git")
        bare = (child / "HEAD").is_file() and (child / "objects").is_dir()
        if (child / ".git").exists() or bare:
            found.setdefault(name, child)
            if child.name.endswith(".git"):
                found[name] = child
    return found


def hint_row(
    repo: str, path: str, counts: list[int], mask: int, index: SignalIndex, window: bool
) -> dict[str, Any]:
    total, agent = counts
    origin = "agent" if agent == total else "human" if agent == 0 else "mixed"
    signals = index.names_of(mask)
    if window:
        signals = sorted({*signals, WINDOW_SIGNAL})
    return {
        "schema": SCHEMA,
        "repo": repo,
        "path": path,
        "origin": origin,
        "commits": {"total": total, "agent": agent, "human": total - agent},
        "signals": signals,
        "basis": BASIS,
    }


def write_atomically(out: Path | None, lines: Iterator[str]) -> None:
    """Write the lines as they are produced to a private file and publish it only when the
    iterator finished; on any error nothing is published (standard output included)."""
    if out is None:
        with tempfile.TemporaryFile("w+", encoding="utf-8", newline="\n") as spool:
            for line in lines:
                spool.write(line)
            spool.seek(0)
            while chunk := spool.read(READ_SIZE):
                sys.stdout.write(chunk)
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=out.parent, prefix=f".{out.name}.", suffix=".part")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            for line in lines:
                handle.write(line)
        Path(name).replace(out)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(name).unlink()
        raise


def _positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="git_origin_hints",
        description="Authorship hints (cce.origin-hints.v1) from the git history of clones.",
    )
    parser.add_argument(
        "--repos-dir",
        type=Path,
        required=True,
        help="Directory of repositories, each <name>.git or <name>/ (bare or blobless clones)",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--wanted", type=Path, help='JSONL of {"repo": ..., "path": ...} rows')
    source.add_argument("--all", action="store_true", help="Emit every file seen in every repo")
    parser.add_argument("--out", type=Path, help="Hints JSONL to write (default: standard output)")
    parser.add_argument(
        "--max-commits-per-repo",
        type=_positive,
        default=None,
        help="Read only the newest N commits of each repository (default: unlimited)",
    )
    return parser


def run(args: argparse.Namespace) -> int:
    def progress(text: str) -> None:
        print(text, file=sys.stderr, flush=True)

    repos_dir: Path = args.repos_dir
    wanted: dict[str, dict[str, set[str]]] | None = None
    if args.all:
        repos = discover(repos_dir)
    else:
        wanted = read_wanted(args.wanted)
        repos = {}
        missing = []
        for name in sorted(wanted):
            found = find_repo(repos_dir, name)
            if found is None:
                missing.append(name)
            else:
                repos[name] = found
        if missing:
            progress(f"missing repositories ({len(missing)}): {', '.join(missing)}")
            return EXIT_INPUT
    index = SignalIndex()
    totals = {"rows": 0, "unmatched": 0}

    def lines() -> Iterator[str]:
        """The output, repository by repository in name order and file by file in path order;
        only one repository's files are held at a time."""
        for number, name in enumerate(sorted(repos), 1):
            progress(f"repo {number}/{len(repos)}: reading")
            wanted_paths = set(wanted[name]) if wanted is not None else None
            try:
                result = read_repo(
                    repos[name],
                    wanted=wanted_paths,
                    limit=args.max_commits_per_repo,
                    index=index,
                    progress=progress,
                )
            except subprocess.CalledProcessError as error:
                raise GitFailure(
                    f"git failed on repository {name} (exit {error.returncode}): {error.stderr}"
                ) from None
            except (OSError, ValueError) as error:
                raise GitFailure(
                    f"git output could not be read for repository {name}: {error}"
                ) from None
            spelled_rows: list[tuple[str, dict[str, Any]]] = []
            for path in result.counts:
                counts, mask = result.counts[path], result.masks.get(path, 0)
                spellings = wanted[name][path] if wanted is not None else {path}
                spelled_rows.extend(
                    (
                        spelled,
                        hint_row(name, spelled, counts, mask, index, result.window_full),
                    )
                    for spelled in spellings
                )
            spelled_rows.sort(key=lambda item: item[0])
            if wanted is not None:
                totals["unmatched"] += len(wanted[name]) - len(result.counts)
            progress(
                f"repo {number}/{len(repos)}: {result.commits} commits, {len(result.counts)} files"
            )
            for _, row in spelled_rows:
                totals["rows"] += 1
                yield json.dumps(row, ensure_ascii=True) + "\n"

    try:
        write_atomically(args.out, lines())
    except GitFailure as error:
        progress(str(error))
        return EXIT_GIT
    progress(f"done: {totals['rows']} hints, {totals['unmatched']} wanted files without history")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except InputError as error:
        print(f"git_origin_hints: {error}", file=sys.stderr)
        return EXIT_INPUT


if __name__ == "__main__":
    sys.exit(main())
