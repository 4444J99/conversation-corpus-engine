"""Inventory native coding-agent sessions into the database `library_native` reads.

The knowledge library ingests native sessions through an "estate database": a SQLite inventory
named in the source manifest under ``estate_databases``. This module is the producer for that
inventory. Every source root is declared by the caller; nothing is discovered from a home
directory, and sources are never written: no byte of a source or of a SQLite sidecar changes
(see ``_native_database``), and symlinks that leave their declared root are not followed.

Identity is the contract. One native session is one ``sessions`` row, whichever roots hold a
copy of it, and the identity rules are the ones the earlier estate corpus builder used, so an
existing library does not grow duplicate records:

* Claude Code: the transcript ``sessionId``; a subagent is ``<sessionId>/agent/<agentId>`` with
  parent ``<sessionId>``.
* Codex: the ``session_meta`` payload ``id`` (never the file name), with parents from the thread
  state database edges, ``parent_thread_id`` and ``source.subagent.thread_spawn``.
* Gemini and Antigravity: the ids in census files and conversation databases.
* Copilot: session-store rows and ``session-state`` directory names.
* OpenCode: ``session`` and ``session_v2`` rows.

Processing order is fixed so a rerun over the same sources gives the same inventory: families are
independent; Codex reads every state database (declaration order) before every rollout, so parent
edges from any root apply to rollouts from any root; copies are visited in declaration order and
then by sorted path. Byte-identical copies (same family, size and SHA-256) are observed once and
counted as folded duplicates; copies that differ are all observed. A problem with one source is a
finding for that source and never aborts the run. The receipt holds counts and paths, never text.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import io
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from .library_native import READER

IMPLEMENTATION = "library-inventory@1.0.0"
RECEIPT_SCHEMA = "cce.library-inventory-receipt"
RECEIPT_SCHEMA_VERSION = 1
DATABASE_NAME = "session-corpus.db"
RECEIPT_NAME = "inventory-receipt.json"
FAMILIES = ("claude", "codex", "gemini", "antigravity", "copilot", "opencode")
DEFAULT_MAX_SECONDS = 7200.0
CODEX_STATE_DATABASE = "state_5.sqlite"
CODEX_HEAD_LINES = 64
DATABASE_SUFFIXES = (".db", ".sqlite", ".sqlite3")
CHUNK = 1 << 20
SNAPSHOT_MAX_BYTES = 2 << 30
SNAPSHOT_ATTEMPTS = 3
# Every transcript file the reader opens in a session-state directory (library_native).
COPILOT_TRANSCRIPTS = (
    "events.jsonl",
    "session.jsonl",
    "messages.jsonl",
    "conversation.json",
    "messages.json",
)
UUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


@dataclass(frozen=True)
class InventorySources:
    """Explicit, repeatable source declarations; nothing here has a default location."""

    claude_projects: Sequence[Path] = ()
    codex_roots: Sequence[Path] = ()
    codex_censuses: Sequence[Path] = ()
    gemini_sources: Sequence[Path] = ()
    antigravity_sources: Sequence[Path] = ()
    copilot_state_dirs: Sequence[Path] = ()
    copilot_databases: Sequence[Path] = ()
    opencode_databases: Sequence[Path] = ()

    def declared(self) -> dict[str, list[str]]:
        names = (
            "claude_projects",
            "codex_roots",
            "codex_censuses",
            "gemini_sources",
            "antigravity_sources",
            "copilot_state_dirs",
            "copilot_databases",
            "opencode_databases",
        )
        return {name: [str(Path(p).absolute()) for p in getattr(self, name)] for name in names}

    def empty(self) -> bool:
        return not any(self.declared().values())


class _Deadline(Exception):
    """The --max-seconds budget ran out; the run stops and reports itself incomplete."""


class _Problem(Exception):
    """A source cannot be inventoried; ``code`` is the finding recorded for it."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _Run:
    def __init__(
        self, conn: sqlite3.Connection, max_seconds: float, protected: Iterable[Path] = ()
    ):
        self.conn = conn
        # Declared source roots: no private copy may be created inside one.
        self.protected = tuple(Path(root).resolve() for root in protected)
        self.deadline = time.monotonic() + max_seconds
        self.folded: Counter[str] = Counter()
        self.savepoints = 0

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def expired(self) -> bool:
        return self.remaining() <= 0

    def check(self) -> None:
        if self.expired():
            raise _Deadline


def _key(path: Path | str) -> str:
    return str(Path(path).resolve())


def _literal(path: Path | str) -> Path:
    """The absolute path as written, without following a symlink in its last component."""
    return Path(path).absolute()


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _epoch_iso(value: Any, divisor: float = 1.0) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.datetime.fromtimestamp(value / divisor, datetime.timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


# ---------------------------------------------------------------------------------------------
# Output schema (the tables library_native reads, plus the findings the producer keeps)
# ---------------------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE sessions (
    session_id TEXT NOT NULL,
    agent_family TEXT NOT NULL,
    session_kind TEXT NOT NULL,
    parent_session_id TEXT,
    started_at TEXT,
    ended_at TEXT,
    project_root TEXT,
    primary_branch TEXT,
    head_commit TEXT,
    prompt_title TEXT,
    prompt_snippet TEXT,
    status TEXT NOT NULL,
    irf_owner TEXT,
    custody_path TEXT NOT NULL,
    PRIMARY KEY (session_id, agent_family)
);
CREATE INDEX idx_sessions_agent ON sessions(agent_family);
CREATE INDEX idx_sessions_kind ON sessions(session_kind);
CREATE INDEX idx_sessions_status ON sessions(status);
CREATE TABLE source_observations (
    agent_family TEXT NOT NULL,
    source_path TEXT NOT NULL,
    object_key TEXT NOT NULL,
    native_id TEXT NOT NULL,
    PRIMARY KEY (agent_family, source_path, object_key)
);
CREATE TABLE source_artifact_observations (
    agent_family TEXT NOT NULL, source_path TEXT NOT NULL,
    object_key TEXT NOT NULL, classification TEXT NOT NULL,
    PRIMARY KEY(agent_family, source_path, object_key)
);
CREATE TABLE unresolved_source_observations (
    agent_family TEXT NOT NULL, source_path TEXT NOT NULL,
    object_key TEXT NOT NULL, reason TEXT NOT NULL,
    PRIMARY KEY(agent_family, source_path, object_key, reason)
);
CREATE TABLE source_custody_findings (
    agent_family TEXT NOT NULL, source_path TEXT NOT NULL,
    object_key TEXT NOT NULL, scope TEXT NOT NULL, finding TEXT NOT NULL,
    base_sha256 TEXT NOT NULL,
    PRIMARY KEY(agent_family, source_path, object_key, scope, finding)
);
"""


def _observe(conn: sqlite3.Connection, family: str, path: Path, object_key: str, native_id: str):
    conn.execute(
        "INSERT OR IGNORE INTO source_observations VALUES (?,?,?,?)",
        (family, _key(path), str(object_key), native_id),
    )


def _artifact(conn: sqlite3.Connection, family: str, path: Path, object_key: str, label: str):
    """Account for measured artifacts that do not supply a native session record."""
    conn.execute(
        "INSERT OR REPLACE INTO source_artifact_observations VALUES (?,?,?,?)",
        (family, _key(path), object_key, label),
    )


def _unresolved(
    conn: sqlite3.Connection,
    family: str,
    path: Path,
    object_key: str,
    reason: str,
    *,
    literal: bool = False,
):
    """Record a finding. ``literal`` keeps a symlink's own path instead of its target's."""
    conn.execute(
        "INSERT OR IGNORE INTO unresolved_source_observations VALUES (?,?,?,?)",
        (family, str(_literal(path)) if literal else _key(path), str(object_key), reason),
    )


def _merge_identity(
    conn: sqlite3.Connection, family: str, sid: str, source: Path, kind: str, parent: str | None
) -> tuple[str, str | None]:
    """Merge one copy's kind and parent claim into what the session already has.

    A copy that lacks parent evidence (``unknown``) never erases an established relationship, and
    contradictory claims become an ``unknown`` session with a ``conflicting_parent_identity``
    finding instead of the later copy silently winning. Every family goes through this merge.
    """
    existing = conn.execute(
        "SELECT session_kind,parent_session_id FROM sessions WHERE agent_family=? AND session_id=?",
        (family, sid),
    ).fetchone()
    parent_conflicted = conn.execute(
        "SELECT 1 FROM unresolved_source_observations "
        "WHERE agent_family=? AND object_key=? AND reason='conflicting_parent_identity' LIMIT 1",
        (family, f"session/{sid}"),
    ).fetchone()
    if existing:
        old_kind, old_parent = existing
        if (parent and old_parent and parent != old_parent) or (
            kind in ("root", "subagent") and old_kind in ("root", "subagent") and kind != old_kind
        ):
            _unresolved(conn, family, source, f"session/{sid}", "conflicting_parent_identity")
            kind, parent = "unknown", None
        elif kind == "unknown":
            kind, parent = old_kind, old_parent
    if parent_conflicted:
        kind, parent = "unknown", None
    return kind, parent


def _upsert(
    conn: sqlite3.Connection,
    family: str,
    sid: Any,
    source: Path,
    *,
    kind: str = "unknown",
    parent: str | None = None,
    started: str | None = None,
    project: str | None = None,
    title: str | None = None,
    branch: str | None = None,
    head: str | None = None,
    snippet: str = "",
) -> None:
    if not isinstance(sid, str) or not sid:
        raise _Problem("native_identity_missing")
    kind, parent = _merge_identity(conn, family, sid, source, kind, parent)
    conn.execute(
        """
        INSERT INTO sessions(session_id,agent_family,session_kind,parent_session_id,
            started_at,ended_at,project_root,primary_branch,head_commit,prompt_title,
            prompt_snippet,status,irf_owner,custody_path)
        VALUES (?,?,?,?,?,NULL,?,?,?,?,?,'unknown',NULL,?)
        ON CONFLICT(session_id,agent_family) DO UPDATE SET
            session_kind=excluded.session_kind,parent_session_id=excluded.parent_session_id,
            started_at=COALESCE(sessions.started_at,excluded.started_at),ended_at=NULL,
            project_root=COALESCE(excluded.project_root,sessions.project_root),
            primary_branch=COALESCE(excluded.primary_branch,sessions.primary_branch),
            head_commit=COALESCE(excluded.head_commit,sessions.head_commit),
            prompt_title=COALESCE(excluded.prompt_title,sessions.prompt_title),
            prompt_snippet=excluded.prompt_snippet,status='unknown',irf_owner=NULL,
            custody_path=excluded.custody_path
        """,
        (sid, family, kind, parent, started, project, branch, head, title, snippet, _key(source)),
    )


@contextlib.contextmanager
def _source(run: _Run, family: str, path: Path, object_key: str = "source") -> Iterator[None]:
    """Inventory one source in a savepoint; a failure rolls it back and becomes a finding."""
    run.savepoints += 1
    name = f"source_{run.savepoints}"
    run.conn.execute(f"SAVEPOINT {name}")

    def undo() -> None:
        run.conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
        run.conn.execute(f"RELEASE SAVEPOINT {name}")

    try:
        yield
    except _Deadline:
        undo()
        raise
    except (
        _Problem,
        sqlite3.Error,
        OSError,
        subprocess.SubprocessError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        IndexError,
        RecursionError,
    ) as error:
        undo()
        if run.expired():
            raise _Deadline from error
        if isinstance(error, _Problem):
            code = error.code
        elif isinstance(error, sqlite3.Error):
            code = "source_database_error"
        elif isinstance(error, (OSError, subprocess.SubprocessError)):
            code = "source_unreadable"
        else:
            code = "source_malformed"
        _unresolved(run.conn, family, path, object_key, code)
    else:
        run.conn.execute(f"RELEASE SAVEPOINT {name}")


# ---------------------------------------------------------------------------------------------
# Source discovery and folding
# ---------------------------------------------------------------------------------------------


def _declared(values: Iterable[Path]) -> list[Path]:
    """Resolve declarations; a repeated one counts once, at its last position."""
    result: list[Path] = []
    for value in values:
        path = Path(value).resolve()
        if path in result:
            result.remove(path)
        result.append(path)
    return result


def _roots(run: _Run, family: str, values: Iterable[Path]) -> list[Path]:
    roots = []
    for path in _declared(values):
        if path.is_dir():
            roots.append(path)
        else:
            reason = "source_missing" if not path.exists() else "source_not_directory"
            _unresolved(run.conn, family, path, "source", reason)
    return roots


def _walk_files(
    run: _Run, family: str, base: Path, start: Path, suffixes: tuple[str, ...]
) -> list[Path]:
    """Regular files under ``start`` that stay inside ``base``; symlinks are checked, not trusted.

    Directory symlinks are not followed. A file symlink is kept, as its target, only when the
    target lies inside the declared root.
    """
    found: set[Path] = set()

    def unreadable(error: OSError) -> None:
        _unresolved(
            run.conn, family, Path(error.filename or start), "directory", "source_unreadable"
        )

    for directory, dirnames, filenames in os.walk(start, followlinks=False, onerror=unreadable):
        run.check()
        here = Path(directory)
        for name in dirnames:
            if (here / name).is_symlink():
                _unresolved(
                    run.conn,
                    family,
                    here / name,
                    "directory",
                    "directory_symlink_not_followed",
                    literal=True,
                )
        for name in filenames:
            if not name.endswith(suffixes):
                continue
            candidate = here / name
            if candidate.is_symlink():
                try:
                    target = candidate.resolve(strict=True)
                except (OSError, RuntimeError):
                    _unresolved(
                        run.conn, family, candidate, "file", "symlink_unresolvable", literal=True
                    )
                    continue
                if not target.is_relative_to(base):
                    _unresolved(run.conn, family, candidate, "file", "symlink_escape", literal=True)
                    continue
                if target.is_file():
                    found.add(target)
            else:
                found.add(candidate)
    return sorted(found)


def _contained(run: _Run, family: str, base: Path, path: Path) -> Path | None:
    """``path`` resolved, if it is inside ``base``; otherwise a finding and None."""
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        _unresolved(run.conn, family, path, "file", "symlink_unresolvable", literal=True)
        return None
    if not resolved.is_relative_to(base):
        _unresolved(run.conn, family, path, "file", "symlink_escape", literal=True)
        return None
    return resolved


def _file_sha256(run: _Run, path: Path, digest: Any = None) -> Any:
    digest = digest or hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            run.check()
            digest.update(chunk)
    return digest


def _plain_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _fingerprint(run: _Run, path: Path) -> str:
    digest = _file_sha256(run, path)
    if path.name.endswith(DATABASE_SUFFIXES):
        # A copy with a pending journal is not the same database as its bare base file.
        for suffix in ("-wal", "-journal"):
            sidecar = Path(str(path) + suffix)
            if sidecar.is_file() and sidecar.stat().st_size:
                digest.update(suffix.encode())
                _file_sha256(run, sidecar, digest)
    return digest.hexdigest()


def _file_size(target: Any) -> int | None:
    return target.stat().st_size


def _bundle_size(target: Sequence[tuple[str, Path]]) -> int | None:
    return sum(path.stat().st_size for _, path in target)


def _bundle_fingerprint(run: _Run, target: Sequence[tuple[str, Path]]) -> str:
    """Hash which transcript names are present together with every file's size and bytes."""
    digest = hashlib.sha256()
    for name, path in sorted(target):
        digest.update(f"{name}\0{path.stat().st_size}\0".encode())
        _file_sha256(run, path, digest)
    return digest.hexdigest()


def _fold(
    run: _Run,
    family: str,
    items: Sequence[Any],
    probe: Callable[[Any], Any] = lambda item: item,
    scope: Callable[[Any], Any] = lambda item: None,
    twins: dict[Any, Any] | None = None,
    measure: Callable[[Any], int | None] = _file_size,
    identify: Callable[[_Run, Any], str] = _fingerprint,
) -> list[Any]:
    """Keep the first of each set of byte-identical copies; count the others as folded.

    ``items`` arrive in precedence order (declaration order, then sorted path). Only items whose
    size matches another's are hashed. An item with nothing to fingerprint, or with no bytes, is
    always kept: empty files carry no identity and are accounted for one by one. Copies are
    identical only within one ``scope`` (for a Copilot session-state directory its name, because
    the name is the identity). A folded item is mapped to the kept copy in ``twins`` when given.
    ``measure`` and ``identify`` size and fingerprint what ``probe`` returns; the defaults are a
    single file, and a Copilot directory supplies the bundle of every transcript the reader opens.
    """
    sizes: dict[tuple[Any, int], int] = Counter()
    measured: dict[int, int | None] = {}
    for index, item in enumerate(items):
        target = probe(item)
        try:
            size = measure(target) if target is not None else None
        except OSError:
            size = None
        measured[index] = size
        if size:
            sizes[(scope(item), size)] += 1
    kept: list[Any] = []
    seen: dict[tuple[Any, int, str], Any] = {}
    for index, item in enumerate(items):
        size = measured[index]
        target = probe(item)
        if not size or target is None or sizes[(scope(item), size)] < 2:
            kept.append(item)
            continue
        try:
            identity = (scope(item), size, identify(run, target))
        except OSError:
            kept.append(item)
            continue
        if identity in seen:
            run.folded[family] += 1
            if twins is not None:
                twins[item] = seen[identity]
            continue
        seen[identity] = item
        kept.append(item)
    return kept


# ---------------------------------------------------------------------------------------------
# Streaming and database readers
# ---------------------------------------------------------------------------------------------


@contextlib.contextmanager
def _text_stream(run: _Run, path: Path) -> Iterator[Any]:
    """Read a (possibly zstd-compressed) text file without extracting it to disk.

    The caller reads to the end or leaves by raising (closing the generator does). A stream that
    ends cleanly is accepted only when ``zstd`` itself then exits with status 0.
    """
    if not path.name.endswith(".zst"):
        with path.open(encoding="utf-8", errors="replace") as stream:
            yield stream
        return
    executable = shutil.which("zstd")
    if not executable:
        raise _Problem("zstd_unavailable")
    process = subprocess.Popen(
        [executable, "-dc", "--", str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    budget = min(max(run.remaining(), 0.1), threading.TIMEOUT_MAX)
    timer = threading.Timer(budget, process.kill)
    timer.start()
    failed = False
    try:
        assert process.stdout is not None
        yield io.TextIOWrapper(process.stdout, encoding="utf-8", errors="replace")
    except BaseException:
        failed = True
        raise
    finally:
        timer.cancel()
        if failed and process.poll() is None:
            process.kill()
        try:
            # After a clean end of stream zstd is about to exit; wait for its real status.
            process.wait(timeout=None if failed else budget)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            exited = False
        else:
            exited = True
        if process.stdout is not None:
            process.stdout.close()
    if not failed and not (exited and process.returncode == 0):
        raise _Problem("source_decode_failed")


def _jsonl_events(run: _Run, family: str, path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    with _text_stream(run, path) as stream:
        for number, line in enumerate(stream, 1):
            if number % 1000 == 0:
                run.check()
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except (ValueError, RecursionError):
                _unresolved(run.conn, family, path, f"line/{number}", "invalid_json")
                continue
            if not isinstance(event, dict):
                _unresolved(run.conn, family, path, f"line/{number}", "invalid_event")
                continue
            yield number, event


def _connect_readonly(run: _Run, uri: str) -> sqlite3.Connection:
    connection = sqlite3.connect(uri, uri=True, timeout=2)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.Error:
        connection.close()
        raise
    connection.set_progress_handler(lambda: int(run.expired()), 10_000)
    return connection


def _is_wal_mode(path: Path) -> bool:
    """True when the database header says the file is in WAL mode."""
    with path.open("rb") as handle:
        header = handle.read(20)
    return len(header) == 20 and header[18] == 2 and header[19] == 2


def _stamp(path: Path) -> tuple[int, int, int]:
    status = path.stat()
    return status.st_size, status.st_mtime_ns, status.st_ino


def _copy_hashed(run: _Run, origin: Path, destination: Path) -> str:
    """Copy ``origin`` to a new private file and return the SHA-256 of the bytes copied."""
    digest = hashlib.sha256()
    descriptor = os.open(destination, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with origin.open("rb") as source, os.fdopen(descriptor, "wb") as target:
        while chunk := source.read(CHUNK):
            run.check()
            digest.update(chunk)
            target.write(chunk)
    return digest.hexdigest()


@contextlib.contextmanager
def _private_snapshot(run: _Run, path: Path) -> Iterator[Path]:
    """Yield a private copy of a WAL-mode database and its write-ahead log; remove it afterwards.

    Even a ``mode=ro`` connection may rewrite an existing ``-shm`` file, so a WAL-mode source
    whose sidecars are both present is never opened in place. The database and its non-empty log
    are copied into a fresh 0700 directory under the system temporary directory (never inside the
    source's directory or a declared source) and the copy is opened instead; the ``-shm`` file is
    not touched at all. The copy counts only when the source's size, modification time and inode
    are unchanged and its bytes hash the same before and after the copy, retried a few times for
    a source that is being written. A source larger than ``SNAPSHOT_MAX_BYTES`` (or than the free
    space of the temporary directory) is a finding, never an unbounded copy.
    """
    log = Path(str(path) + "-wal")
    base = Path(tempfile.gettempdir())
    resolved = base.resolve()
    if any(resolved.is_relative_to(root) for root in (path.parent.resolve(), *run.protected)):
        raise _Problem("snapshot_location_unsafe")
    scratch = Path(tempfile.mkdtemp(prefix="cce-inventory-snapshot-", dir=base))
    try:
        copy = scratch / "snapshot.sqlite"
        copied_log = Path(str(copy) + "-wal")
        for _ in range(SNAPSHOT_ATTEMPTS):
            before = (_stamp(path), _stamp(log))
            members = [(path, copy)] + ([(log, copied_log)] if before[1][0] else [])
            size = sum(stamp[0] for stamp in before)
            if size > SNAPSHOT_MAX_BYTES or size > shutil.disk_usage(scratch).free // 2:
                raise _Problem("database_too_large_to_snapshot")
            for target in (copy, copied_log):
                target.unlink(missing_ok=True)
            copied = [_copy_hashed(run, origin, target) for origin, target in members]
            if (_stamp(path), _stamp(log)) == before and copied == [
                _file_sha256(run, origin).hexdigest() for origin, _ in members
            ]:
                break
        else:
            raise _Problem("database_changed_during_snapshot")
        yield copy
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


@contextlib.contextmanager
def _native_database(run: _Run, family: str, path: Path) -> Iterator[sqlite3.Connection]:
    """Open a native SQLite source so that no byte of it, or of a sidecar, changes.

    * WAL mode with both ``-wal`` and ``-shm`` present: a validated private snapshot of the
      database and its log is read (``_private_snapshot``), so pending log content is seen.
    * WAL mode missing either sidecar (an archived copy): opening it, even read-only, would make
      SQLite create the missing one. A stable, double-hashed base-file snapshot is read instead,
      for metadata only, and only with no nonempty WAL or rollback journal; its historical WAL
      custody stays a recorded finding.
    * Otherwise the file is opened in place with ``mode=ro``, which writes nothing.
    """
    uri = path.as_uri()
    wal_mode = _is_wal_mode(path)
    if wal_mode and all(Path(str(path) + s).exists() for s in ("-wal", "-shm")):
        with _private_snapshot(run, path) as copy:
            copied = _connect_readonly(run, copy.as_uri())
            try:
                yield copied
            finally:
                copied.close()
        return
    snapshot = None
    connection = None
    if not wal_mode:
        try:
            connection = _connect_readonly(run, uri + "?mode=ro")
        except sqlite3.OperationalError as error:
            if "unable to open database file" not in str(error):
                raise
    if connection is None:
        sidecars = [Path(str(path) + suffix) for suffix in ("-wal", "-journal")]
        if any(p.exists() and p.stat().st_size for p in sidecars):
            raise _Problem("database_requires_its_journal")
        before = path.stat()
        digest = _file_sha256(run, path).hexdigest()
        connection = _connect_readonly(run, uri + "?mode=ro&immutable=1")
        snapshot = (before, digest, sidecars)
    try:
        yield connection
    finally:
        connection.close()
    if snapshot:
        before, digest, sidecars = snapshot
        after = path.stat()
        if (
            (before.st_size, before.st_mtime_ns, before.st_ino)
            != (after.st_size, after.st_mtime_ns, after.st_ino)
            or _file_sha256(run, path).hexdigest() != digest
            or any(p.exists() and p.stat().st_size for p in sidecars)
        ):
            raise _Problem("database_changed_during_snapshot")
        run.conn.execute(
            """INSERT INTO source_custody_findings VALUES (?,?,?,?,?,?)
            ON CONFLICT(agent_family,source_path,object_key,scope,finding)
            DO UPDATE SET base_sha256=excluded.base_sha256""",
            (
                family,
                _key(path),
                "database-base",
                "base_snapshot_metadata",
                "historical_wal_custody_unverified",
                digest,
            ),
        )


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in connection.execute(f'PRAGMA table_info("{table}")')}


# ---------------------------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------------------------


def _claude(run: _Run, roots: Sequence[Path]) -> None:
    files: list[Path] = []
    for root in _roots(run, "claude", roots):
        files.extend(_walk_files(run, "claude", root, root, (".jsonl",)))
    for path in _fold(run, "claude", list(dict.fromkeys(files))):
        run.check()
        with _source(run, "claude", path, "file"):
            _claude_transcript(run, path)


def _claude_transcript(run: _Run, path: Path) -> None:
    conn = run.conn
    identities: dict[str, dict[str, Any]] = {}
    event_types: set[Any] = set()
    event_count = 0
    # A transcript can carry several sessionIds, so the whole file is read.
    for _, event in _jsonl_events(run, "claude", path):
        event_types.add(event.get("type"))
        event_count += 1
        session = event.get("sessionId") or event.get("session_id")
        if not isinstance(session, str) or not session:
            continue
        agent = event.get("agentId")
        if agent is not None and (not isinstance(agent, str) or not agent):
            _unresolved(conn, "claude", path, "metadata", "invalid_agent_identity")
            continue
        if agent:
            sid, parent, kind = f"{session}/agent/{agent}", session, "subagent"
        elif event.get("isSidechain") is True:
            _unresolved(conn, "claude", path, "metadata", "agent_identity_missing")
            continue
        else:
            sid, parent = session, None
            kind = "root" if event.get("isSidechain") is False else "unknown"
        metadata = identities.setdefault(sid, {"parent": parent, "kind": kind})
        if metadata["kind"] == "unknown" and kind == "root":
            metadata["kind"] = kind
        for target, name in (("started", "timestamp"), ("project", "cwd"), ("branch", "gitBranch")):
            if _text(event.get(name)) and target not in metadata:
                metadata[target] = event[name]
    if not identities:
        if path.stat().st_size == 0:
            _artifact(conn, "claude", path, "file", "empty_transcript")
        elif (
            path.name == "journal.jsonl"
            and event_count
            and event_types <= {"launched", "started", "result", "failed"}
        ):
            _artifact(conn, "claude", path, "file", "agent_execution_journal")
        else:
            _unresolved(conn, "claude", path, "file", "native_identity_missing")
    for sid, metadata in identities.items():
        _upsert(
            conn,
            "claude",
            sid,
            path,
            **metadata,
            title=f"Claude session {sid}",
            snippet="Native transcript metadata; lifecycle unverified",
        )
        _observe(conn, "claude", path, f"session/{sid}", sid)


# ---------------------------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------------------------


def _codex(run: _Run, roots: Sequence[Path], censuses: Sequence[Path]) -> None:
    family, conn = "codex", run.conn
    codex_roots = _roots(run, family, roots)
    edges: dict[str, dict[str, Path]] = {}

    state_databases = []
    for root in codex_roots:
        database = root / CODEX_STATE_DATABASE
        if database.is_file() and (resolved := _contained(run, family, root, database)):
            state_databases.append(resolved)
    for database in _fold(run, family, state_databases):
        run.check()
        found: dict[str, set[str]] | None = None
        with _source(run, family, database, "threads"):
            found = _codex_state(run, database)
        for child, parents in (found or {}).items():
            for parent in parents:
                edges.setdefault(child, {}).setdefault(parent, database)

    discovered: dict[str, list[Path]] = defaultdict(list)
    ordered: list[Path] = []
    for root in codex_roots:
        for sub in ("sessions", "archived_sessions"):
            directory = root / sub
            if not directory.is_dir() or not _contained(run, family, root, directory):
                continue
            for path in _walk_files(run, family, root, directory, (".jsonl", ".jsonl.zst")):
                discovered[path.name.removesuffix(".zst")].append(path)
                ordered.append(path)

    # A census is optional. It can name rollouts outside the usual directories and records where
    # the owner's own listing found each one; identity never depends on it.
    references: dict[Path, list[tuple[Path, int, bool, str, Any]]] = defaultdict(list)
    for census in _declared(censuses):
        data = None
        with _source(run, family, census, "census"):
            data = json.loads(census.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("sessions"), list):
                raise _Problem("unsupported_census")
        if not isinstance(data, dict) or not isinstance(data.get("sessions"), list):
            continue
        for index, record in enumerate(data["sessions"]):
            relative = record.get("relative_path") if isinstance(record, dict) else None
            if not isinstance(relative, str) or not relative:
                _unresolved(conn, family, census, f"sessions/{index}", "native_path_missing")
                continue
            located, recovered = None, False
            for root in codex_roots:
                candidate = (root / relative).resolve()
                if not candidate.is_relative_to(root):
                    _unresolved(conn, family, census, f"sessions/{index}", "census_path_escape")
                elif candidate.is_file():
                    located = candidate
                    break
            if located is None:
                matches = discovered.get(Path(relative).name.removesuffix(".zst"), [])
                if len(matches) == 1:
                    located, recovered = matches[0], True
                else:
                    reason = "native_source_missing" if not matches else "native_source_ambiguous"
                    _unresolved(conn, family, census, f"sessions/{index}", reason)
                    continue
            references[located].append((census, index, recovered, relative, data.get("root_path")))
            if located not in ordered:
                ordered.append(located)

    twins: dict[Path, Path] = {}
    identified: dict[Path, str] = {}
    for path in _fold(run, family, list(dict.fromkeys(ordered)), twins=twins):
        run.check()
        sid = None
        with _source(run, family, path, "file"):
            sid = _codex_rollout(run, path, edges)
        if sid:
            identified[path] = sid
            for census, index, recovered, relative, census_root in references.get(path, []):
                _codex_census_row(run, census, index, recovered, relative, census_root, path, sid)
    # A census row that names a folded copy still reaches a byte-identical rollout, so it is
    # accounted for against the identity its kept twin proved.
    for copy, kept in twins.items():
        if sid := identified.get(kept):
            for census, index, recovered, relative, census_root in references.get(copy, []):
                _codex_census_row(run, census, index, recovered, relative, census_root, copy, sid)


def _codex_state(run: _Run, database: Path) -> dict[str, set[str]]:
    """Index the thread state database; return its parent edges (child -> parents)."""
    conn = run.conn
    edges: dict[str, set[str]] = {}
    rows: list[tuple[Any, ...]] = []
    with _native_database(run, "codex", database) as native:
        tables = _table_names(native)
        if "thread_spawn_edges" in tables:
            for parent, child in native.execute(
                "SELECT parent_thread_id,child_thread_id FROM thread_spawn_edges"
            ):
                if isinstance(parent, str) and parent and isinstance(child, str) and child:
                    edges.setdefault(child, set()).add(parent)
        if "threads" in tables:
            columns = _columns(native, "threads")
            if "id" not in columns:
                raise _Problem("unsupported_threads_schema")
            names = ("source", "cwd", "title", "git_branch", "git_sha", "created_at")
            fields = [f'"{name}"' if name in columns else "NULL" for name in names]
            rows = list(native.execute("SELECT id," + ",".join(fields) + " FROM threads"))
    for index, (sid, raw_source, cwd, title, branch, head, created) in enumerate(rows):
        if not isinstance(sid, str) or not sid:
            _unresolved(conn, "codex", database, f"threads/row-{index}", "native_identity_missing")
            continue
        source = raw_source
        if isinstance(raw_source, str):
            with contextlib.suppress(ValueError):
                source = json.loads(raw_source)
        parents = edges.get(sid, set())
        parent = next(iter(parents)) if len(parents) == 1 else None
        kind = (
            "subagent" if parent else ("root" if source in ("cli", "vscode", "exec") else "unknown")
        )
        if len(parents) > 1 or sid in parents:
            parent, kind = None, "unknown"
            _unresolved(conn, "codex", database, f"threads/{sid}", "conflicting_parent_identity")
        _upsert(
            conn,
            "codex",
            sid,
            database,
            kind=kind,
            parent=parent,
            started=_epoch_iso(created) or _text(created),
            project=_text(cwd),
            branch=_text(branch),
            head=_text(head),
            title=_text(title) or f"Codex session {sid}",
        )
        _observe(conn, "codex", database, f"threads/{sid}", sid)
    return edges


def _codex_rollout(run: _Run, path: Path, edges: dict[str, dict[str, Path]]) -> str | None:
    """Identify one rollout from its ``session_meta`` header; returns the thread id."""
    conn = run.conn
    metadata = None
    events = _jsonl_events(run, "codex", path)
    try:
        for number, event in events:
            if event.get("type") == "session_meta" and isinstance(event.get("payload"), dict):
                metadata = event["payload"]
                break
            if number >= CODEX_HEAD_LINES:
                break
    finally:
        events.close()
    if metadata is None:
        _unresolved(conn, "codex", path, "metadata", "native_identity_missing")
        return None
    sid = metadata.get("id") or metadata.get("session_id")
    if not isinstance(sid, str) or not sid:
        _unresolved(conn, "codex", path, "metadata", "native_identity_missing")
        return None
    parents = set(edges.get(sid, ()))
    parent = metadata.get("parent_thread_id")
    if isinstance(parent, str) and parent:
        parents.add(parent)
    source = metadata.get("source")
    subagent = source.get("subagent") if isinstance(source, dict) else None
    spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
    if isinstance(spawn, dict) and isinstance(spawn.get("parent_thread_id"), str):
        parents.add(spawn["parent_thread_id"])
    parent = next(iter(parents)) if len(parents) == 1 else None
    if len(parents) > 1 or sid in parents:
        parent = None
        kind = "unknown"
        _unresolved(conn, "codex", path, "metadata", "conflicting_parent_identity")
    elif parent:
        kind = "subagent"
    elif subagent is not None:
        kind = "subagent"
        _unresolved(conn, "codex", path, "metadata", "parent_identity_missing")
    else:
        # Absence of a parent field alone is not proof of a root session.
        kind = "root" if source in ("cli", "vscode", "exec") else "unknown"
    git = metadata.get("git") if isinstance(metadata.get("git"), dict) else {}
    _upsert(
        conn,
        "codex",
        sid,
        path,
        kind=kind,
        parent=parent,
        started=_text(metadata.get("timestamp")),
        project=_text(metadata.get("cwd")),
        branch=_text(git.get("branch")),
        head=_text(git.get("commit_hash")),
        title=f"Codex session {sid}",
        snippet="Native rollout metadata; lifecycle unverified",
    )
    _observe(conn, "codex", path, "session_meta", sid)
    for edge_parent, edge_source in edges.get(sid, {}).items():
        _observe(conn, "codex", edge_source, f"thread_spawn_edges/{edge_parent}/{sid}", sid)
    return sid


def _codex_census_row(
    run: _Run,
    census: Path,
    index: int,
    recovered: bool,
    relative: str,
    census_root: Any,
    rollout: Path,
    sid: str,
) -> None:
    """Record a census row as an observation when the reader can follow it to the rollout."""
    conn = run.conn
    if recovered:
        # A relocated or compressed rollout is accepted only when its file name carries the
        # thread id its header proved; an archival suffix may add a second uuid after it.
        match = UUID_PATTERN.search(Path(relative).name)
        try:
            expected = str(UUID(match.group(0))) if match else None
        except ValueError:
            expected = None
        if expected != sid:
            _unresolved(conn, "codex", rollout, "metadata", "relocated_native_identity_mismatch")
            return
    base = Path(census_root if isinstance(census_root, str) and census_root else census.parent)
    candidate = Path(relative).expanduser()
    reachable = candidate if candidate.is_absolute() else base / candidate
    key = f"sessions/{index}"
    if not recovered and reachable.resolve() == rollout:
        _observe(conn, "codex", census, key, sid)
    else:
        # The reader resolves census paths against root_path, else the census directory.
        _artifact(conn, "codex", census, key, "census_row_not_reader_resolvable")


# ---------------------------------------------------------------------------------------------
# Gemini and Antigravity
# ---------------------------------------------------------------------------------------------


def _json_file(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _gemini(run: _Run, sources: Sequence[Path]) -> None:
    files = []
    for path in _declared(sources):
        if path.is_file():
            files.append(path)
        else:
            _unresolved(run.conn, "gemini", path, "source", "source_missing")
    for path in _fold(run, "gemini", files):
        run.check()
        with _source(run, "gemini", path):
            _gemini_census(run, path)


def _gemini_census(run: _Run, path: Path) -> None:
    conn = run.conn
    data = _json_file(path)
    if not isinstance(data, dict) or not isinstance(data.get("sessions"), list):
        raise _Problem("unsupported_census")
    for index, record in enumerate(data["sessions"]):
        key = f"sessions/{index}"
        if not isinstance(record, dict):
            _unresolved(conn, "gemini", path, key, "invalid_record")
            continue
        sid = record.get("session_id")
        if not isinstance(sid, str) or not sid.strip():
            _unresolved(conn, "gemini", path, key, "native_identity_missing")
            continue
        parent = _text(record.get("parent_session_id"))
        native_kind = record.get("kind")
        kind = (
            "subagent"
            if parent or native_kind == "subagent"
            else "root"
            if native_kind in ("main", "root")
            else "unknown"
        )
        if parent == sid:
            _unresolved(conn, "gemini", path, key, "self_parent_identity")
            parent, kind = None, "unknown"
        _upsert(
            conn,
            "gemini",
            sid,
            path,
            kind=kind,
            parent=parent,
            started=_text(record.get("start_time")),
            project=_text(record.get("project_root")),
            title=_text(record.get("title")),
        )
        _observe(conn, "gemini", path, key, sid)


def _antigravity(run: _Run, sources: Sequence[Path]) -> None:
    family = "antigravity"
    units: list[tuple[str, Path]] = []
    for path in _declared(sources):
        if path.is_dir():
            databases = []
            try:
                candidates = sorted((path / "conversations").glob("*.db"))
            except OSError:
                candidates = []
                _unresolved(run.conn, family, path, "source", "source_unreadable")
            for candidate in candidates:
                if resolved := _contained(run, family, path, candidate):
                    databases.append(resolved)
            summary = path / "conversation_summaries.db"
            if summary.is_file() and (resolved := _contained(run, family, path, summary)):
                databases.append(resolved)
            if not databases:
                _unresolved(run.conn, family, path, "source", "no_native_databases")
            units.extend(("database", database) for database in databases)
        elif path.is_file():
            units.append(("database" if path.suffix in DATABASE_SUFFIXES else "census", path))
        else:
            _unresolved(run.conn, family, path, "source", "source_missing")
    for kind, path in _fold(run, family, units, probe=lambda unit: unit[1]):
        run.check()
        with _source(run, family, path):
            if kind == "database":
                _antigravity_database(run, path)
            else:
                _antigravity_census(run, path)


def _antigravity_database(run: _Run, path: Path) -> None:
    records: list[tuple[str, dict[str, Any]]] = []
    with _native_database(run, "antigravity", path) as native:
        native.row_factory = sqlite3.Row
        tables = _table_names(native)
        if "conversation_summaries" in tables:
            records = [
                ("conversation_summaries", dict(row))
                for row in native.execute("SELECT * FROM conversation_summaries")
            ]
        elif "trajectory_meta" in tables:
            records = [
                ("trajectory_meta", dict(row))
                for row in native.execute("SELECT cascade_id FROM trajectory_meta")
            ]
        else:
            raise _Problem("unsupported_antigravity_database")
    occurrences: Counter[tuple[str, str]] = Counter()
    for index, (table, record) in enumerate(records):
        sid = (
            record.get("conversation_id")
            if table == "conversation_summaries"
            else record.get("cascade_id")
        )
        if isinstance(sid, str) and sid.strip():
            # A repeated native ID is one session but multiple source rows.
            ordinal = occurrences[(table, sid)]
            occurrences[(table, sid)] += 1
            key = json.dumps([table, sid, ordinal], separators=(",", ":"))
        else:
            key = json.dumps([table, "unresolved-row", index], separators=(",", ":"))
        _antigravity_record(run, path, key, record)


def _antigravity_census(run: _Run, path: Path) -> None:
    data = _json_file(path)
    if not isinstance(data, dict):
        raise _Problem("unsupported_census")
    sources = [("sessions", data["sessions"])] if "sessions" in data else []
    for name in ("tm_cli", "evac_cli"):
        if isinstance(data.get(name), dict) and "sessions" in data[name]:
            sources.append((f"{name}/sessions", data[name]["sessions"]))
    if not sources:
        raise _Problem("unsupported_census")
    for label, records in sources:
        if isinstance(records, dict):
            for key, record in records.items():
                _antigravity_record(run, path, f"{label}/{key}", record, claimed_id=key)
        elif isinstance(records, list):
            for index, record in enumerate(records):
                _antigravity_record(run, path, f"{label}/{index}", record)
        else:
            raise _Problem("unsupported_census")


def _antigravity_record(
    run: _Run, path: Path, key: str, record: Any, claimed_id: str | None = None
) -> None:
    conn = run.conn
    if not isinstance(record, dict):
        _unresolved(conn, "antigravity", path, key, "invalid_record")
        return
    native_ids = {
        v
        for v in (
            record.get("conversation_id"),
            record.get("uuid"),
            record.get("cascade_id"),
            claimed_id,
        )
        if isinstance(v, str) and v.strip()
    }
    if len(native_ids) != 1:
        reason = "native_identity_missing" if not native_ids else "conflicting_native_identity"
        _unresolved(conn, "antigravity", path, key, reason)
        return
    sid = next(iter(native_ids))
    parent = _text(record.get("parent_conversation_id"))
    depth = record.get("nesting_depth")
    if parent or record.get("is_subagent") is True or (isinstance(depth, int) and depth > 0):
        kind = "subagent"
    elif record.get("is_subagent") is False or depth == 0:
        kind = "root"
    else:
        kind = "unknown"
    if parent == sid:
        _unresolved(conn, "antigravity", path, key, "self_parent_identity")
        parent, kind = None, "unknown"
    uris = record.get("workspace_uris_db", record.get("workspace_uris"))
    if isinstance(uris, str):
        uris = uris.split(",")
    project = (
        uris[0].removeprefix("file://")
        if isinstance(uris, list) and uris and isinstance(uris[0], str)
        else None
    )
    preview = record.get("preview")
    _upsert(
        conn,
        "antigravity",
        sid,
        path,
        kind=kind,
        parent=parent,
        project=project,
        title=_text(record.get("title")),
        snippet=preview if isinstance(preview, str) else "",
    )
    _observe(conn, "antigravity", path, key, sid)


# ---------------------------------------------------------------------------------------------
# Copilot
# ---------------------------------------------------------------------------------------------


def _copilot(run: _Run, state_dirs: Sequence[Path], databases: Sequence[Path]) -> None:
    family = "copilot"
    stores = []
    for path in _declared(databases):
        if path.is_file():
            stores.append(path)
        else:
            _unresolved(run.conn, family, path, "source", "source_missing")
    for path in _fold(run, family, stores):
        run.check()
        with _source(run, family, path):
            _copilot_database(run, path)

    directories: list[Path] = []
    events: dict[Path, Path | None] = {}
    transcripts: dict[Path, list[tuple[str, Path]] | None] = {}
    for root in _roots(run, family, state_dirs):
        try:
            children = sorted(root.iterdir())
        except OSError:
            _unresolved(run.conn, family, root, "source", "source_unreadable")
            continue
        for directory in children:
            if not directory.is_dir() or not (resolved := _contained(run, family, root, directory)):
                continue
            if resolved in events:
                continue
            directories.append(resolved)
            # Every transcript file the reader opens is read through the same containment rule
            # as the directory. A name that is present but cannot be trusted (it escapes the
            # root, or is not a regular file) leaves the directory unfoldable.
            found: list[tuple[str, Path]] = []
            trusted = True
            for name in COPILOT_TRANSCRIPTS:
                candidate = resolved / name
                if not os.path.lexists(candidate):
                    continue
                contained = _contained(run, family, root, candidate)
                if contained is not None and contained.is_file():
                    found.append((name, contained))
                else:
                    trusted = False
            events[resolved] = next((path for name, path in found if name == "events.jsonl"), None)
            transcripts[resolved] = found if trusted and found else None

    # A session-state directory's name is its identity, so only same-named copies can be copies,
    # and then only when every transcript file the reader can open is byte-identical.
    kept = _fold(
        run,
        family,
        directories,
        probe=lambda directory: transcripts[directory],
        scope=lambda directory: directory.name,
        measure=_bundle_size,
        identify=_bundle_fingerprint,
    )
    for directory in kept:
        run.check()
        with _source(run, family, directory, "session-state"):
            _copilot_directory(run, directory, events[directory])


def _copilot_database(run: _Run, path: Path) -> None:
    conn = run.conn
    rows: list[tuple[Any, ...]] = []
    with _native_database(run, "copilot", path) as native:
        columns = _columns(native, "sessions")
        if "id" not in columns:
            raise _Problem("unsupported_copilot_schema")
        fields = [
            f'"{n}"' if n in columns else "NULL" for n in ("cwd", "branch", "created_at", "summary")
        ]
        rows = list(native.execute("SELECT id," + ",".join(fields) + " FROM sessions"))
    for index, (sid, cwd, branch, created, summary) in enumerate(rows):
        if not isinstance(sid, str) or not sid:
            _unresolved(conn, "copilot", path, f"sessions/row-{index}", "native_identity_missing")
            continue
        _upsert(
            conn,
            "copilot",
            sid,
            path,
            started=_text(created),
            project=_text(cwd),
            branch=_text(branch),
            title=_text(summary) or f"Copilot session {sid}",
        )
        _observe(conn, "copilot", path, f"sessions/{sid}", sid)


def _copilot_directory(run: _Run, directory: Path, events: Path | None) -> None:
    conn = run.conn
    sid = directory.name
    metadata: dict[str, Any] = {}
    if events is not None:
        for _, event in _jsonl_events(run, "copilot", events):
            data = event.get("data")
            if event.get("type") != "session.start" or not isinstance(data, dict):
                continue
            native_sid = data.get("sessionId")
            if native_sid is not None and native_sid != sid:
                raise _Problem("directory_native_identity_conflict")
            context = data.get("context") if isinstance(data.get("context"), dict) else {}
            metadata = {
                "started": _text(data.get("startTime")),
                "project": _text(context.get("cwd")),
                "branch": _text(context.get("branch")),
            }
    # A session-state directory is a separate observation, never an extra session.
    _upsert(
        conn,
        "copilot",
        sid,
        directory,
        **metadata,
        title=f"Copilot session {sid}",
        snippet="Native session state; lifecycle unverified",
    )
    _observe(conn, "copilot", directory, "session-state", sid)


# ---------------------------------------------------------------------------------------------
# OpenCode
# ---------------------------------------------------------------------------------------------


def _opencode(run: _Run, databases: Sequence[Path]) -> None:
    files = []
    for path in _declared(databases):
        if path.is_file():
            files.append(path)
        else:
            _unresolved(run.conn, "opencode", path, "source", "source_missing")
    for path in _fold(run, "opencode", files):
        run.check()
        with _source(run, "opencode", path):
            _opencode_database(run, path)


def _opencode_database(run: _Run, source: Path) -> None:
    conn = run.conn
    gathered: list[tuple[str, bool, list[tuple[Any, ...]]]] = []
    with _native_database(run, "opencode", source) as native:
        present = [t for t in ("session", "session_v2") if t in _table_names(native)]
        if not present:
            raise _Problem("unsupported_opencode_schema")
        for table in present:
            columns = _columns(native, table)
            if not {"id", "title", "time_created", "time_updated", "directory"} <= columns:
                raise _Problem("unsupported_opencode_schema")
            parent_column = '"parent_id"' if "parent_id" in columns else "NULL"
            rows = list(
                native.execute(
                    f"SELECT id,title,time_created,time_updated,directory,{parent_column} "
                    f'FROM "{table}"'
                )
            )
            gathered.append((table, "parent_id" in columns, rows))
    for table, has_parent, rows in gathered:
        for index, (sid, title, created, updated, directory, raw_parent) in enumerate(rows):
            if not isinstance(sid, str) or not sid:
                _unresolved(
                    conn, "opencode", source, f"{table}/row-{index}", "native_identity_missing"
                )
                continue
            parent = _text(raw_parent)
            # A copy without parent evidence cannot erase a proven relationship, and copies that
            # disagree become a finding; the other columns keep this family's last-copy-wins.
            kind, parent = _merge_identity(
                conn,
                "opencode",
                sid,
                source,
                "subagent" if parent else ("root" if has_parent else "unknown"),
                parent,
            )
            conn.execute(
                """
                INSERT INTO sessions(session_id,agent_family,session_kind,parent_session_id,
                    started_at,ended_at,project_root,prompt_title,prompt_snippet,status,irf_owner,
                    custody_path)
                VALUES (?,'opencode',?,?,?,NULL,?,?,?,'unknown',NULL,?)
                ON CONFLICT(session_id,agent_family) DO UPDATE SET
                    session_kind=excluded.session_kind,parent_session_id=excluded.parent_session_id,
                    started_at=excluded.started_at,ended_at=NULL,project_root=excluded.project_root,
                    prompt_title=excluded.prompt_title,prompt_snippet=excluded.prompt_snippet,
                    status='unknown',irf_owner=NULL,custody_path=excluded.custody_path
                """,
                (
                    sid,
                    kind,
                    parent,
                    _epoch_iso(created, 1000),
                    _text(directory),
                    _text(title) or f"OpenCode session {sid}",
                    f"Observed native record; last updated {_epoch_iso(updated, 1000)}",
                    str(source),
                ),
            )
            conn.execute(
                "INSERT INTO source_observations VALUES ('opencode',?,?,?) "
                "ON CONFLICT(agent_family,source_path,object_key) DO NOTHING",
                (str(source), f"{table}/{sid}", sid),
            )


# ---------------------------------------------------------------------------------------------
# Receipt and the build entry point
# ---------------------------------------------------------------------------------------------


def _grouped(conn: sqlite3.Connection, sql: str) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for family, label, count in conn.execute(sql):
        result.setdefault(family, {})[label] = count
    return result


def _receipt(
    conn: sqlite3.Connection,
    run: _Run,
    sources: InventorySources,
    *,
    complete: bool,
    started: str,
    finished: str,
) -> dict[str, Any]:
    kinds = _grouped(
        conn,
        "SELECT agent_family, session_kind, count(*) FROM sessions "
        "GROUP BY agent_family, session_kind",
    )
    findings = _grouped(
        conn,
        "SELECT agent_family, reason, count(*) FROM unresolved_source_observations "
        "GROUP BY agent_family, reason",
    )
    artifacts = _grouped(
        conn,
        "SELECT agent_family, classification, count(*) FROM source_artifact_observations "
        "GROUP BY agent_family, classification",
    )
    custody = _grouped(
        conn,
        "SELECT agent_family, finding, count(*) FROM source_custody_findings "
        "GROUP BY agent_family, finding",
    )
    observations = dict(
        conn.execute("SELECT agent_family, count(*) FROM source_observations GROUP BY agent_family")
    )
    files = dict(
        conn.execute(
            "SELECT agent_family, count(DISTINCT source_path) FROM source_observations "
            "GROUP BY agent_family"
        )
    )

    def total(by_family: dict[str, dict[str, int]]) -> dict[str, int]:
        merged: Counter[str] = Counter()
        for counts in by_family.values():
            merged.update(counts)
        return dict(sorted(merged.items()))

    families = {
        family: {
            "sessions": sum(kinds.get(family, {}).values()),
            "sessions_by_kind": dict(sorted(kinds.get(family, {}).items())),
            "source_observations": observations.get(family, 0),
            "observed_source_files": files.get(family, 0),
            "folded_duplicates": run.folded.get(family, 0),
            "findings_by_code": dict(sorted(findings.get(family, {}).items())),
            "artifacts_by_classification": dict(sorted(artifacts.get(family, {}).items())),
            "custody_findings": dict(sorted(custody.get(family, {}).items())),
        }
        for family in FAMILIES
    }
    return {
        "schema": RECEIPT_SCHEMA,
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "implementation": IMPLEMENTATION,
        "reader": READER,
        "status": "complete" if complete else "incomplete",
        "declared_roots": sources.declared(),
        "families": families,
        "totals": {
            "sessions": sum(f["sessions"] for f in families.values()),
            "source_observations": sum(observations.values()),
            "folded_duplicates": sum(run.folded.values()),
            "findings": sum(sum(c.values()) for c in findings.values()),
        },
        "findings_by_code": total(findings),
        "artifacts_by_classification": total(artifacts),
        "started_at": started,
        "finished_at": finished,
    }


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _prepare_output(out: Path, sources: InventorySources) -> Path:
    if os.path.lexists(out):
        raise FileExistsError(f"inventory output already exists: {out}")
    destination = Path(out).absolute()
    resolved = destination.parent.resolve() / destination.name
    for values in sources.declared().values():
        for declared in values:
            if resolved.is_relative_to(Path(declared).resolve()):
                raise ValueError("inventory output may not be inside a declared source")
    destination.mkdir(mode=0o700)
    destination.chmod(0o700)
    return destination


def build_inventory(
    sources: InventorySources, out: Path, *, max_seconds: float = DEFAULT_MAX_SECONDS
) -> dict[str, Any]:
    """Write ``session-corpus.db`` and ``inventory-receipt.json`` into the new directory ``out``.

    Returns the receipt. ``status`` is ``incomplete`` when the time budget ran out; the files are
    still written so the partial work is inspectable, but it is not a usable inventory.
    """
    if not math.isfinite(max_seconds) or max_seconds <= 0:
        raise ValueError("max_seconds must be a positive, finite number")
    if sources.empty():
        raise ValueError("declare at least one source")
    destination = _prepare_output(Path(out), sources)
    database = destination / DATABASE_NAME
    descriptor = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    started = _now()
    conn = sqlite3.connect(database, isolation_level=None)
    complete = True
    try:
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("PRAGMA synchronous = OFF")
        conn.executescript(SCHEMA)
        run = _Run(
            conn,
            max_seconds,
            [Path(root) for roots in sources.declared().values() for root in roots],
        )
        conn.execute("BEGIN")
        try:
            _gemini(run, sources.gemini_sources)
            _antigravity(run, sources.antigravity_sources)
            _codex(run, sources.codex_roots, sources.codex_censuses)
            _claude(run, sources.claude_projects)
            _copilot(run, sources.copilot_state_dirs, sources.copilot_databases)
            _opencode(run, sources.opencode_databases)
        except _Deadline:
            complete = False
        conn.execute("COMMIT")
        integrity = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        receipt = _receipt(conn, run, sources, complete=complete, started=started, finished=_now())
    finally:
        conn.close()
    digest = _plain_sha256(database)
    receipt["database"] = {
        "file": DATABASE_NAME,
        "sha256": digest,
        "bytes": database.stat().st_size,
        "integrity": integrity,
    }
    receipt["finished_at"] = _now()
    descriptor = os.open(destination / RECEIPT_NAME, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(receipt, stream, indent=2, sort_keys=True)
        stream.write("\n")
    return receipt
