"""The versioned record and retrieval model for CCE's unified private library.

Source adapters still produce archive conversations. This store retains every accepted
normalized revision and observation; the current view never deletes absent sources.
It is a materialization of that archive, not a registry of new agent instructions.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any

from .archive_database import fts_query
from .archive_model import Conversation, stable_uid
from .library_blocks import split_blocks

LIBRARY_SCHEMA = "cce.knowledge-library.v1"
MAX_PAGE = 200


def _bounded_sql(method: Any) -> Any:
    @wraps(method)
    def run(self: Library, *args: Any, **kwargs: Any) -> Any:
        with self.sql_progress(kwargs.get("check_callback")):
            return method(self, *args, **kwargs)

    return run


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS objects (
    digest TEXT PRIMARY KEY, payload BLOB NOT NULL, raw_bytes INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS records (
    uid TEXT PRIMARY KEY, provider TEXT NOT NULL, kind TEXT NOT NULL,
    wing TEXT NOT NULL, lane TEXT NOT NULL, title TEXT NOT NULL,
    created TEXT NOT NULL, updated TEXT NOT NULL, body_status TEXT NOT NULL,
    current_version TEXT NOT NULL, current_digest TEXT NOT NULL REFERENCES objects(digest),
    content_digest TEXT NOT NULL, interpretation_digest TEXT NOT NULL,
    message_count INTEGER NOT NULL, source_ref TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revisions (
    uid TEXT NOT NULL REFERENCES records(uid), version TEXT NOT NULL,
    ordinal INTEGER NOT NULL, digest TEXT NOT NULL REFERENCES objects(digest),
    previous_version TEXT, change_kind TEXT NOT NULL,
    PRIMARY KEY(uid, version), UNIQUE(uid, ordinal)
);
CREATE TABLE IF NOT EXISTS observations (
    uid TEXT NOT NULL REFERENCES records(uid), digest TEXT NOT NULL,
    payload TEXT NOT NULL, PRIMARY KEY(uid, digest)
);
CREATE TABLE IF NOT EXISTS snapshots (
    uid TEXT NOT NULL REFERENCES records(uid), digest TEXT NOT NULL REFERENCES objects(digest),
    body_status TEXT NOT NULL, disposition TEXT NOT NULL,
    PRIMARY KEY(uid, digest)
);
CREATE VIRTUAL TABLE IF NOT EXISTS record_fts USING fts5(
    uid UNINDEXED, title, body, tokenize='porter unicode61'
);
CREATE TABLE IF NOT EXISTS blocks (
    digest TEXT PRIMARY KEY, text BLOB NOT NULL, chars INTEGER NOT NULL,
    eligible INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS occurrences (
    uid TEXT NOT NULL REFERENCES records(uid), message_index INTEGER NOT NULL,
    block_index INTEGER NOT NULL, digest TEXT NOT NULL REFERENCES blocks(digest),
    start INTEGER NOT NULL, end INTEGER NOT NULL, normalization TEXT NOT NULL,
    PRIMARY KEY(uid, message_index, block_index)
);
CREATE INDEX IF NOT EXISTS occurrences_digest ON occurrences(digest);
CREATE TABLE IF NOT EXISTS relations (
    src TEXT NOT NULL, dst TEXT NOT NULL, kind TEXT NOT NULL,
    evidence TEXT NOT NULL, PRIMARY KEY(src, dst, kind)
);
"""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _next_version(previous: str | None, change: str) -> str:
    if previous is None:
        return "1.0.0"
    major, minor, patch = map(int, previous.split("."))
    if change == "major":
        return f"{major + 1}.0.0"
    if change == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def _body_digest(payload: dict[str, Any]) -> str:
    return digest_json(
        [
            {key: message.get(key) for key in ("role", "channel", "text", "attachments")}
            for message in payload["messages"]
        ]
        + [{"alternate_messages": payload.get("extra", {}).get("alternate_messages", [])}]
    )


class Library:
    def __init__(self, path: Path, *, writable: bool = False) -> None:
        self.path = path.resolve()
        self.writable = writable
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        uri = self.path.as_uri() + ("?mode=rw" if writable else "?mode=ro")
        self.connection = sqlite3.connect(uri, uri=True)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA cache_size=-32768")
        self._owned_sql_handler: tuple[Any, int] | None = None
        version = self.connection.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        if version is None or version[0] != LIBRARY_SCHEMA:
            self.connection.close()
            raise ValueError("unsupported knowledge library schema")

    @classmethod
    def create(
        cls, path: Path, *, previous: Path | None = None, compact_previous: bool = False
    ) -> Library:
        """Create a new candidate, optionally inheriting the accepted immutable generation.

        The public builder must first authorize its destination through corpus_store. This
        low-level method also refuses replacement and symlinks so a test or caller cannot
        accidentally truncate an existing database.
        """
        if path.exists() or path.is_symlink():
            raise FileExistsError(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        connection = sqlite3.connect(path)
        try:
            if previous is not None:
                with cls(previous) as original:
                    if compact_previous:
                        original.connection.execute("VACUUM main INTO ?", (str(path),))
                    else:
                        original.connection.backup(connection)
            connection.executescript(SCHEMA_SQL)
            connection.execute("INSERT OR IGNORE INTO meta VALUES ('schema', ?)", (LIBRARY_SCHEMA,))
            connection.commit()
        finally:
            connection.close()
        return cls(path, writable=True)

    def __enter__(self) -> Library:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def _write(self) -> None:
        if not self.writable:
            raise ValueError("knowledge library is read-only")

    @contextmanager
    def sql_progress(self, check_callback: Any, *, instructions: int = 10000) -> Iterator[None]:
        """Interrupt long SQL when its budget callback raises; preserve nested scopes.

        SQLite has no progress-handler getter. This scope owns and restores only
        handlers installed through this method; callers should not mix direct
        connection.set_progress_handler calls with active owned scopes.
        """
        if check_callback is None:
            yield
            return
        if isinstance(instructions, bool) or not isinstance(instructions, int) or instructions < 1:
            raise ValueError("SQL progress instructions must be a positive integer")
        previous = self._owned_sql_handler
        errors: list[BaseException] = []

        def progress() -> int:
            try:
                check_callback()
                return 0
            except BaseException as error:
                if not errors:
                    errors.append(error)
                return 1

        self.connection.set_progress_handler(progress, instructions)
        self._owned_sql_handler = (progress, instructions)
        try:
            yield
        except sqlite3.OperationalError:
            if errors:
                raise errors[0] from None
            raise
        finally:
            self.connection.set_progress_handler(*(previous or (None, 0)))
            self._owned_sql_handler = previous

    def put(
        self, conversation: Conversation, *, reconcile: bool = False, observation_only: bool = False
    ) -> dict[str, Any]:
        self._write()
        payload = conversation.to_dict()
        uid = conversation.uid
        if not re.fullmatch(r"[a-z][a-z0-9]*", conversation.provider) or not re.fullmatch(
            r"[a-z0-9][a-z0-9-]{0,199}", uid
        ):
            raise ValueError("library provider and UID must be safe stable identifiers")
        extra = payload["extra"]
        wing = str(extra.get("wing", "ai"))
        lane = str(extra.get("lane", "conversations"))
        body_status = str(
            extra.get("body_status", "full" if conversation.messages else "metadata_only")
        )
        if wing not in {"ai", "authored"}:
            raise ValueError(f"unknown knowledge wing: {wing}")
        if body_status not in {"full", "partial", "metadata_only"}:
            raise ValueError(f"unknown body status: {body_status}")
        raw = canonical_json(payload).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        content_digest = _body_digest(payload)
        interpretation = digest_json(
            [payload["schema"], conversation.provider, conversation.kind, wing, lane]
        )
        previous = self.connection.execute("SELECT * FROM records WHERE uid=?", (uid,)).fetchone()
        observations = [payload["provenance"], *extra.get("source_observations", [])]
        if previous is not None and previous["current_digest"] == digest:
            self._observations(uid, observations)
            self.connection.commit()
            return {
                "uid": uid,
                "version": previous["current_version"],
                "changed": False,
                "digest": digest,
            }
        observed = self.connection.execute(
            "SELECT 1 FROM snapshots WHERE uid=? AND digest=?", (uid, digest)
        ).fetchone()
        quality = {"metadata_only": 0, "partial": 1, "full": 2}
        downgrade = previous is not None and quality[body_status] < quality[previous["body_status"]]
        if previous is not None and (observation_only or downgrade or (reconcile and observed)):
            disposition = (
                "historical_snapshot"
                if observation_only
                else ("lower_body_coverage" if downgrade else "prior_observation")
            )
            with self.connection:
                self.connection.execute(
                    "INSERT OR IGNORE INTO objects VALUES (?,?,?)",
                    (digest, zlib.compress(raw), len(raw)),
                )
                self.connection.execute(
                    "INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?)",
                    (
                        uid,
                        digest,
                        body_status,
                        disposition,
                    ),
                )
                self._observations(uid, observations)
            return {
                "uid": uid,
                "version": previous["current_version"],
                "changed": False,
                "digest": digest,
                "disposition": disposition,
            }
        change = "initial"
        if previous is not None:
            change = "patch"
            if previous["interpretation_digest"] != interpretation:
                change = "major"
            elif previous["content_digest"] != content_digest:
                change = "minor"
        previous_version = previous["current_version"] if previous else None
        version = _next_version(previous_version, change)
        ordinal = self.connection.execute(
            "SELECT COALESCE(MAX(ordinal),0)+1 FROM revisions WHERE uid=?", (uid,)
        ).fetchone()[0]
        source_ref = str(
            payload["provenance"].get("source_path")
            or payload["provenance"].get("source_file")
            or ""
        )
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO objects VALUES (?,?,?)",
                (digest, zlib.compress(raw), len(raw)),
            )
            self.connection.execute(
                "INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(uid) DO UPDATE SET provider=excluded.provider, kind=excluded.kind, "
                "wing=excluded.wing, lane=excluded.lane, title=excluded.title, created=excluded.created, "
                "updated=excluded.updated, body_status=excluded.body_status, current_version=excluded.current_version, "
                "current_digest=excluded.current_digest, content_digest=excluded.content_digest, "
                "interpretation_digest=excluded.interpretation_digest, message_count=excluded.message_count, source_ref=excluded.source_ref",
                (
                    uid,
                    conversation.provider,
                    conversation.kind,
                    wing,
                    lane,
                    conversation.title,
                    conversation.created,
                    conversation.updated,
                    body_status,
                    version,
                    digest,
                    content_digest,
                    interpretation,
                    len(conversation.messages),
                    source_ref,
                ),
            )
            self.connection.execute(
                "INSERT INTO revisions VALUES (?,?,?,?,?,?)",
                (uid, version, ordinal, digest, previous_version, change),
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?)",
                (uid, digest, body_status, "accepted_revision"),
            )
            self._observations(uid, observations)
            record_rowid = self.connection.execute(
                "SELECT rowid FROM records WHERE uid=?", (uid,)
            ).fetchone()[0]
            if previous is not None:
                self.connection.execute("DELETE FROM record_fts WHERE rowid=?", (record_rowid,))
            # One FTS row per identity makes pagination independent of conversation length.
            # All channels remain searchable; excerpts and raw reads are explicitly bounded.
            body = "\n\n".join(dict.fromkeys(message.text for message in conversation.messages))
            alternates = extra.get("alternate_messages", [])
            if isinstance(alternates, list):
                body += "\n\n" + "\n\n".join(
                    str(m.get("text", "")) for m in alternates if isinstance(m, dict)
                )
            self.connection.execute(
                "INSERT INTO record_fts(rowid,uid,title,body) VALUES (?,?,?,?)",
                (record_rowid, uid, conversation.title, body),
            )
            self.connection.execute("DELETE FROM occurrences WHERE uid=?", (uid,))
            self.connection.execute("DELETE FROM relations WHERE src=? AND kind='parent'", (uid,))
            parent = extra.get("parent_session_id")
            if parent:
                self.connection.execute(
                    "INSERT OR REPLACE INTO relations VALUES (?,?,?,?)",
                    (
                        uid,
                        stable_uid(conversation.provider, str(parent)),
                        "parent",
                        canonical_json({"native_parent_id": parent}),
                    ),
                )
        return {
            "uid": uid,
            "version": version,
            "changed": True,
            "change_kind": change,
            "digest": digest,
        }

    def _observations(self, uid: str, observations: list[Any]) -> None:
        for observation in observations:
            if observation:
                serialized = canonical_json(observation)
                self.connection.execute(
                    "INSERT OR IGNORE INTO observations VALUES (?,?,?)",
                    (uid, hashlib.sha256(serialized.encode()).hexdigest(), serialized),
                )

    def record(
        self, uid: str, *, version: str | None = None, digest: str | None = None
    ) -> dict[str, Any]:
        if version is not None and digest is not None:
            raise ValueError("choose a version or a snapshot digest")
        if digest is not None:
            row = self.connection.execute(
                "SELECT o.payload FROM snapshots s JOIN objects o ON o.digest=s.digest WHERE s.uid=? AND s.digest=?",
                (uid, digest),
            ).fetchone()
        elif version is None:
            row = self.connection.execute(
                "SELECT o.payload FROM records r JOIN objects o ON o.digest=r.current_digest WHERE r.uid=?",
                (uid,),
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT o.payload FROM revisions r JOIN objects o ON o.digest=r.digest WHERE r.uid=? AND r.version=?",
                (uid, version),
            ).fetchone()
        if row is None:
            raise KeyError(f"record or revision not found: {uid} {version or 'current'}")
        return json.loads(zlib.decompress(row[0]))

    def history(self, uid: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM revisions WHERE uid=? ORDER BY ordinal", (uid,)
            )
        ]

    def snapshots(self, uid: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM snapshots WHERE uid=? ORDER BY digest", (uid,)
            )
        ]

    def rows(self) -> Iterator[dict[str, Any]]:
        for row in self.connection.execute("SELECT * FROM records ORDER BY uid"):
            yield dict(row)

    @_bounded_sql
    def index_blocks(
        self, *, check_callback: Any = None, uids: list[str] | None = None
    ) -> dict[str, Any]:
        self._write()

        # Rebuild only this candidate's derived occurrence view. Raw revisions remain intact.
        with self.connection:
            # sqlite3 begins implicit transactions on DML, not DROP INDEX. Start
            # explicitly so an interrupted rebuild restores both data and index.
            self.connection.execute("SAVEPOINT block_reindex")
            if uids is None:
                self.connection.execute("DROP INDEX IF EXISTS occurrences_digest")
                self.connection.execute("DROP INDEX IF EXISTS blocks_eligible_digest_chars")
                self.connection.execute("DELETE FROM occurrences")
                self.connection.execute("DELETE FROM blocks")
                selected_rows = self.rows()
            else:
                selected = []
                for uid in sorted(set(uids)):
                    if check_callback:
                        check_callback()
                    row = self.connection.execute(
                        "SELECT * FROM records WHERE uid=?", (uid,)
                    ).fetchone()
                    if row is None:
                        raise KeyError(uid)
                    selected.append(dict(row))
                    self.connection.execute("DELETE FROM occurrences WHERE uid=?", (uid,))
                selected_rows = iter(selected)
            for row in selected_rows:
                if check_callback:
                    check_callback()
                payload = self.record(row["uid"])
                for index, message in enumerate(payload["messages"]):
                    for block_index, block in enumerate(split_blocks(message["text"])):
                        if check_callback and block_index % 500 == 0:
                            check_callback()
                        # Only eligible prose participates in reference condensation.
                        # Code and short fragments remain lossless in source revisions
                        # and full-text search, without a second expanded SQL copy.
                        if not block["eligible"]:
                            continue
                        self.connection.execute(
                            "INSERT OR IGNORE INTO blocks VALUES (?,?,?,?)",
                            (
                                block["digest"],
                                zlib.compress(block["canonical_text"].encode("utf-8")),
                                len(block["canonical_text"]),
                                int(block["eligible"]),
                            ),
                        )
                        self.connection.execute(
                            "INSERT INTO occurrences VALUES (?,?,?,?,?,?,?)",
                            (
                                row["uid"],
                                index,
                                block_index,
                                block["digest"],
                                block["start"],
                                block["end"],
                                canonical_json(block["normalization"]),
                            ),
                        )
            if uids is None:
                self.connection.execute("CREATE INDEX occurrences_digest ON occurrences(digest)")
                self.connection.execute(
                    "CREATE INDEX blocks_eligible_digest_chars ON blocks(eligible,digest,chars)"
                )
            else:
                if check_callback:
                    check_callback()
                # put() removes stale occurrences as soon as current content changes.
                # The digest index makes this anti-join bounded by derived block count;
                # only unreferenced block rows are written, preserving COW savings.
                self.connection.execute(
                    "DELETE FROM blocks WHERE NOT EXISTS "
                    "(SELECT 1 FROM occurrences WHERE occurrences.digest=blocks.digest)"
                )
            self.connection.execute("RELEASE SAVEPOINT block_reindex")
        return self.status(check_callback=check_callback)

    def block_text(self, digest: str) -> str:
        row = self.connection.execute(
            "SELECT text FROM blocks WHERE digest=?", (digest,)
        ).fetchone()
        if row is None:
            raise KeyError(digest)
        value = row[0]
        return zlib.decompress(value).decode("utf-8") if isinstance(value, bytes) else value

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        offset: int = 0,
        wing: str | None = None,
        lane: str | None = None,
        providers: list[str] | None = None,
        kinds: list[str] | None = None,
    ) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE:
            raise ValueError(f"limit must be an integer from 1 to {MAX_PAGE}")
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 1000000000:
            raise ValueError("offset must be an integer from 0 to 1000000000")
        match = fts_query(query)
        empty = {
            "query": query,
            "total": 0,
            "offset": offset,
            "limit": limit,
            "next_offset": None,
            "hits": [],
        }
        if not match:
            return empty
        clauses, params = ["record_fts MATCH ?"], [match]
        for column, value in (("wing", wing), ("lane", lane)):
            if value:
                clauses.append(f"r.{column}=?")
                params.append(value)
        for column, values in (("provider", providers), ("kind", kinds)):
            if values:
                clauses.append(f"r.{column} IN ({','.join('?' for _ in values)})")
                params.extend(values)
        source = "FROM record_fts JOIN records r ON r.uid=record_fts.uid WHERE " + " AND ".join(
            clauses
        )
        total = self.connection.execute("SELECT COUNT(*) " + source, params).fetchone()[0]
        results = self.connection.execute(
            "SELECT r.uid,r.title,r.provider,r.kind,r.wing,r.lane,r.current_version AS version,"
            "r.body_status,r.message_count,r.source_ref,snippet(record_fts,-1,'[',']','...',28) AS excerpt "
            + source
            + " ORDER BY bm25(record_fts,0,5,1),r.uid LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
        hits = []
        for row in results:
            hit = dict(row)
            hit["title"] = hit["title"][:512]
            hit["source_ref"] = hit["source_ref"][:2048]
            for key in ("kind", "lane"):
                hit[key] = hit[key][:128]
            hit["excerpt"] = hit["excerpt"][:360]
            hit["path"] = f"vault/records/{hit['uid']}.md"
            hits.append(hit)
        return {
            **empty,
            "total": total,
            "hits": hits,
            "next_offset": offset + len(hits) if offset + len(hits) < total else None,
        }

    @_bounded_sql
    def status(self, *, check_callback: Any = None) -> dict[str, Any]:
        counts = {
            table: self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "records",
                "revisions",
                "observations",
                "snapshots",
                "blocks",
                "occurrences",
                "relations",
            )
        }
        repeats = self.connection.execute(
            "SELECT COUNT(*),COALESCE(SUM(n-1),0),COALESCE(SUM((n-1)*b.chars),0) "
            "FROM (SELECT digest,COUNT(*) AS n FROM occurrences GROUP BY digest HAVING COUNT(*)>1) repeated "
            "JOIN blocks b ON b.digest=repeated.digest WHERE b.eligible=1"
        ).fetchone()
        counts.update(
            schema=LIBRARY_SCHEMA,
            repeated_blocks=repeats[0],
            reference_occurrences=repeats[1],
            repeated_characters=repeats[2],
        )
        for label, column in (
            ("by_provider", "provider"),
            ("by_wing", "wing"),
            ("by_lane", "lane"),
            ("by_kind", "kind"),
            ("by_body_status", "body_status"),
        ):
            counts[label] = dict(
                self.connection.execute(f"SELECT {column},COUNT(*) FROM records GROUP BY {column}")
            )
        counts["raw_revision_bytes"] = self.connection.execute(
            "SELECT COALESCE(SUM(raw_bytes),0) FROM objects"
        ).fetchone()[0]
        counts["compressed_revision_bytes"] = self.connection.execute(
            "SELECT COALESCE(SUM(length(payload)),0) FROM objects"
        ).fetchone()[0]
        return counts

    def evidence_fingerprint(self) -> str:
        """Bind current records, all retained revisions and unselected source observations."""
        digest = hashlib.sha256()
        for table, order in (
            ("records", "uid"),
            ("revisions", "uid,ordinal"),
            ("observations", "uid,digest"),
            ("snapshots", "uid,digest"),
            ("relations", "src,dst,kind"),
        ):
            digest.update(table.encode())
            for row in self.connection.execute(f"SELECT * FROM {table} ORDER BY {order}"):
                digest.update(canonical_json(dict(row)).encode("utf-8") + b"\n")
        return digest.hexdigest()

    @_bounded_sql
    def verify(self, *, full: bool = True, check_callback: Any = None) -> list[str]:
        problems = []
        integrity = self.connection.execute("PRAGMA integrity_check").fetchall()
        if [row[0] for row in integrity] != ["ok"]:
            problems.extend(str(row[0]) for row in integrity)
        if self.connection.execute("PRAGMA foreign_key_check").fetchone():
            problems.append("library has dangling foreign keys")
        count = self.connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        if self.connection.execute("SELECT COUNT(*) FROM record_fts").fetchone()[0] != count:
            problems.append("search identity count differs from record count")
        if self.connection.execute(
            "SELECT uid FROM record_fts GROUP BY uid HAVING COUNT(*)>1 LIMIT 1"
        ).fetchone():
            problems.append("duplicate search identities")
        if self.connection.execute(
            "SELECT uid FROM records EXCEPT SELECT uid FROM record_fts"
        ).fetchone():
            problems.append("record absent from search")
        if full:
            for row in self.connection.execute("SELECT digest,payload,raw_bytes FROM objects"):
                if check_callback:
                    check_callback()
                try:
                    raw = zlib.decompress(row["payload"])
                    if (
                        hashlib.sha256(raw).hexdigest() != row["digest"]
                        or len(raw) != row["raw_bytes"]
                    ):
                        problems.append(f"revision object hash mismatch: {row['digest']}")
                except zlib.error:
                    problems.append(f"unreadable revision object: {row['digest']}")
        return problems
