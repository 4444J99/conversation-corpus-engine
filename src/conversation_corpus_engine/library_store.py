"""The versioned record and retrieval model for CCE's unified private library.

Source adapters still produce archive conversations. This store retains every accepted
normalized revision and observation; the current view never deletes absent sources.
It is a materialization of that archive, not a registry of new agent instructions.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import time
import unicodedata
import zlib
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any

from .archive_database import WORD_RE, fts_terms
from .archive_model import Conversation, stable_uid
from .library_blocks import split_blocks
from .web_capture import PROVIDER as WEB_CAPTURE_PROVIDER
from .web_capture import capture_summary

LIBRARY_SCHEMA = "cce.knowledge-library.v1"
MAX_PAGE = 200
# Document records state their reason in ``body_reason`` only; other providers may instead
# record coverage findings, so findings never explain a document.
DOCUMENT_PROVIDER = "documents"

# Search planning. ``auto`` runs an unquoted multi-word query as an adjacent phrase, then as the
# AND of its words, then as the OR of its words, and stops at the first plan that matches.
SEARCH_PLANS = ("auto", "phrase", "and", "or")
CONCRETE_PLANS = SEARCH_PLANS[1:]
# Totals are counted only up to this many matches, so a huge match set is never counted in full.
DEFAULT_COUNT_CAP = 1000
# Every plan runs on at most this much of the query: the first MAX_QUERY_CHARS characters, at most
# MAX_QUERY_TERMS distinct terms, each cut to MAX_TERM_CHARS characters.
MAX_QUERY_CHARS = 1000
MAX_QUERY_TERMS = 32
MAX_TERM_CHARS = 64
MAX_COUNT_CAP = 1_000_000_000
MAX_SEARCH_SECONDS = 600.0
# The time budget applied by the command line and the MCP tool; the Python API has none unless
# the caller passes ``max_seconds``.
DEFAULT_SEARCH_SECONDS = 15.0
# After the budget runs out, the unranked page and each plan probe get their own allowance of at
# most this many seconds (and at most max_seconds)...
UNRANKED_GRACE_SECONDS = 2.0
# ...and never less than this, so even a tiny budget leaves time to produce a page.
MIN_UNRANKED_GRACE_SECONDS = 0.25
# VM instructions between budget checks. SQLite computes bm25 for a long match list inside a few
# instructions per row, so the interval stays small; a test lowers it to exercise a tiny library.
SEARCH_PROGRESS_INSTRUCTIONS = 1000
BROAD_QUERY_HINT = "quote a phrase or add terms for a faster, more precise search"
_TERMS_TRUNCATED_NOTICE = (
    f"the query was shortened to its limits ({MAX_QUERY_TERMS} distinct terms, {MAX_TERM_CHARS} "
    f"characters per term, the first {MAX_QUERY_CHARS} characters) and the search ran on what is left."
)
_UNRANKED_STUCK_TAIL = (
    " No hit arrived before the allowance for this page ran out and the plan is not proven "
    "empty, so next_offset repeats this offset: retry it, or narrow the query."
)
_UNPROVEN_TAIL = (
    " No plan could be searched in time, so having no hits does not mean there are none: "
    "retry with next_offset (the same offset), a narrower query or a larger max_seconds."
)
# The time source of the search budget; a test substitutes a controllable clock.
_clock = time.monotonic


class _BudgetExpired(Exception):
    """The search time budget ran out; raised from the SQLite progress callback."""


def validate_plan(plan: Any) -> str:
    if not isinstance(plan, str) or plan not in SEARCH_PLANS:
        raise ValueError("plan must be one of: " + ", ".join(SEARCH_PLANS))
    return plan


def validate_max_seconds(value: Any) -> float | None:
    """A search time budget in seconds: ``None`` (unbounded) or a finite number in (0, 600]."""
    return None if value is None else validate_search_seconds(value)


def bounded_int(value: Any, minimum: int, maximum: int) -> bool:
    """Whether ``value`` is a real integer in [minimum, maximum]; never raises, whatever it is."""
    try:
        return (
            not isinstance(value, bool) and isinstance(value, int) and minimum <= value <= maximum
        )
    except (OverflowError, TypeError, ValueError):
        return False


def validate_search_seconds(value: Any) -> float:
    """A time budget that must be given: a finite number of seconds in (0, 600].

    Anything else, including an integer too large to be a float, is a ``ValueError``.
    """
    try:
        valid = (
            not isinstance(value, bool)
            and isinstance(value, int | float)
            and math.isfinite(value)
            and 0 < value <= MAX_SEARCH_SECONDS
        )
    except (OverflowError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError(
            f"max_seconds must be a number greater than 0 and at most {MAX_SEARCH_SECONDS:g}"
        )
    return float(value)


def validate_count_cap(value: Any) -> int | None:
    if value is None:
        return None
    if not bounded_int(value, 1, MAX_COUNT_CAP):
        raise ValueError(f"count_cap must be an integer from 1 to {MAX_COUNT_CAP}, or None")
    return int(value)


_TRAILING_FRAGMENT = re.compile(r"\s\S*$")


def _has_token(term: str) -> bool:
    """Whether FTS5 would find a token in ``term``. ``___`` is a word to the query parser but all
    separators to the tokenizer, where it matches nothing and constrains nothing. A character
    the running Python does not know yet counts as a token, so a term is never dropped by mistake.
    """
    return any(char.isalnum() or unicodedata.category(char) in ("Co", "Cn") for char in term)


@dataclass(frozen=True)
class QueryTerms:
    """The bounded terms of a query: what the search actually runs on.

    Every plan sees at most ``MAX_QUERY_TERMS`` distinct terms (compared case-insensitively) of
    at most ``MAX_TERM_CHARS`` characters, taken from the first ``MAX_QUERY_CHARS`` characters,
    so a hostile or machine-made query cannot make SQLite build thousands of phrases.
    """

    phrases: tuple[str, ...]
    words: tuple[str, ...]
    # The words in query order, repeats kept, for the adjacent-phrase plan.
    sequence: tuple[str, ...]
    clipped: bool
    shortened: bool
    terms_cut: bool
    sequence_cut: bool

    def lost(self, plan: str) -> bool:
        """Whether the bounds dropped anything that could change what ``plan`` matches."""
        cut = self.sequence_cut if plan == "phrase" else self.terms_cut
        return self.clipped or self.shortened or cut


def query_terms(query: str) -> QueryTerms:
    examined = query[:MAX_QUERY_CHARS]
    if len(query) > MAX_QUERY_CHARS and not query[MAX_QUERY_CHARS].isspace():
        # The cut fell inside a token: drop that fragment, which would be a term nobody wrote.
        fragment = _TRAILING_FRAGMENT.search(examined)
        if fragment:
            examined = examined[: fragment.start()]
    raw_phrases, raw_words = (
        [term for term in terms if _has_token(term)] for terms in fts_terms(examined)
    )
    shortened = terms_cut = False
    seen: set[str] = set()
    kept: tuple[list[str], list[str]] = ([], [])
    for target, raw in zip(kept, (raw_phrases, raw_words), strict=True):
        for term in raw:
            text = term[:MAX_TERM_CHARS].strip()
            shortened = shortened or len(term) > MAX_TERM_CHARS
            key = " ".join(text.split()).casefold()
            if key in seen:
                continue
            if len(seen) >= MAX_QUERY_TERMS:
                terms_cut = True
                continue
            seen.add(key)
            target.append(text)
    ordered = [word for word in WORD_RE.findall(examined.replace('"', " ")) if _has_token(word)]
    shortened = shortened or any(len(word) > MAX_TERM_CHARS for word in ordered[:MAX_QUERY_TERMS])
    return QueryTerms(
        phrases=tuple(kept[0]),
        words=tuple(kept[1]),
        sequence=tuple(word[:MAX_TERM_CHARS] for word in ordered[:MAX_QUERY_TERMS]),
        clipped=len(query) > MAX_QUERY_CHARS,
        shortened=shortened,
        terms_cut=terms_cut,
        sequence_cut=len(ordered) > MAX_QUERY_TERMS,
    )


def _match(terms: QueryTerms, plan: str) -> str:
    if plan == "phrase":
        return '"' + " ".join(terms.sequence) + '"' if terms.sequence else ""
    units = [*(f'"{phrase}"' for phrase in terms.phrases), *(f'"{word}"' for word in terms.words)]
    return (" OR " if plan == "or" else " ").join(units)


def plan_match(query: str, plan: str) -> str:
    """The FTS5 MATCH expression of one concrete plan; empty when the query has no terms.

    ``and`` is the pre-planner behavior (every word and quoted phrase must match), ``or`` accepts
    any of them, and ``phrase`` requires all the words of the query adjacent and in order.
    """
    if plan not in CONCRETE_PLANS:
        raise ValueError("plan must be one of: " + ", ".join(CONCRETE_PLANS))
    return _match(query_terms(query), plan)


def plan_locator_query(query: str, plan: str) -> str:
    """The query text that best locates a hit's passage when ``plan`` answered it."""
    terms = query_terms(query)
    if plan == "phrase" and terms.sequence:
        return _match(terms, "phrase")
    return " ".join([*(f'"{phrase}"' for phrase in terms.phrases), *terms.words])


def _plan_steps(query: str, plan: str) -> tuple[list[tuple[str, str]], QueryTerms]:
    """The (plan, MATCH expression) steps to try in order, empty when the query has no terms."""
    terms = query_terms(query)
    if plan == "auto":
        # A single term, or a query that already quotes a phrase, is searched as written.
        names = CONCRETE_PLANS if not terms.phrases and len(terms.sequence) > 1 else ("and",)
    else:
        names = (plan,)
    steps: list[tuple[str, str]] = []
    for name in names:
        match = _match(terms, name)
        # A plan whose expression repeats an earlier one (one distinct term) adds nothing.
        if match and all(match != earlier for _, earlier in steps):
            steps.append((name, match))
    return steps, terms


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


def _has_finding(findings: Any) -> bool:
    """True when a record's coverage findings hold at least one non-empty entry.

    Native readers attach a finding for every non-full body (partial reads and metadata-only
    identities alike), so one usable entry is the recorded reason. A value that is not a list,
    or a list with no non-empty string, records no reason.
    """
    return isinstance(findings, list) and any(
        isinstance(entry, str) and entry for entry in findings
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

    def _budget_scope(self, deadline: float | None) -> AbstractContextManager[None]:
        """Interrupt SQL once ``_clock()`` reaches ``deadline`` by raising ``_BudgetExpired``.

        This reuses ``sql_progress``. An enclosing owned scope keeps being consulted, and its own
        interruption still reaches it: this scope re-raises it as an ``OperationalError``, which
        the enclosing scope turns back into its original error.
        """
        if deadline is None:
            return nullcontext()

        def check() -> None:
            if _clock() >= deadline:
                raise _BudgetExpired

        outer = self._owned_sql_handler
        if outer is None:
            return self.sql_progress(check, instructions=SEARCH_PROGRESS_INSTRUCTIONS)
        outer_progress, outer_instructions = outer

        def composed() -> None:
            if outer_progress():
                raise sqlite3.OperationalError("interrupted by an enclosing progress scope")
            check()

        return self.sql_progress(
            composed, instructions=min(SEARCH_PROGRESS_INSTRUCTIONS, outer_instructions)
        )

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
        plan: str = "auto",
        max_seconds: float | None = None,
        count_cap: int | None = DEFAULT_COUNT_CAP,
    ) -> dict[str, Any]:
        """Paged full-text retrieval with a planned match, a time budget and a capped count.

        ``plan="auto"`` runs an unquoted query of several words as an adjacent phrase, then as
        the AND of its words, then as the OR of its words, and answers from the first plan with
        at least one match in the filtered set (the offset plays no part). A single term or a
        query with a quoted phrase runs as written, which is the AND of its terms. A named plan
        (``phrase``, ``and``, ``or``) runs only that plan; ``and`` is the pre-planner behavior.

        ``max_seconds`` bounds the whole call through the SQLite progress handler. When it runs
        out the call still returns: ranking is abandoned and the page is the first matches in
        index order (``ranked`` false, ``partial`` true). ``count_cap`` stops counting matches
        beyond that many (``total_exact`` false, ``total_label`` ``"<cap>+"``); ``None`` counts
        them all. Hits and their order do not depend on the cap.
        """
        if not bounded_int(limit, 1, MAX_PAGE):
            raise ValueError(f"limit must be an integer from 1 to {MAX_PAGE}")
        if not bounded_int(offset, 0, 1000000000):
            raise ValueError("offset must be an integer from 0 to 1000000000")
        plan = validate_plan(plan)
        seconds = validate_max_seconds(max_seconds)
        cap = validate_count_cap(count_cap)
        started = _clock()
        deadline = None if seconds is None else started + seconds
        steps, terms = _plan_steps(query, plan)

        def finish(
            name: str,
            total: int,
            exact: bool,
            hits: list[dict[str, Any]],
            next_offset: int | None,
            *,
            expired_in: str | None = None,
            tail: str = "",
        ) -> dict[str, Any]:
            partial = expired_in is not None
            result: dict[str, Any] = {
                "query": query,
                "total": total,
                "offset": offset,
                "limit": limit,
                "next_offset": next_offset,
                "hits": hits,
                "total_exact": exact,
                "total_label": str(total) if exact else f"{total}+",
                "plan": name,
                "partial": partial,
                "ranked": not partial,
                "elapsed_s": round(_clock() - started, 3),
            }
            notices = []
            if terms.lost(name):
                result["terms_truncated"] = True
                notices.append(_TERMS_TRUNCATED_NOTICE)
            if partial:
                notices.append(
                    f"the {seconds:g} s time budget ran out while running the {expired_in} plan; "
                    f"the {name} plan's matches are shown in index order, not by relevance." + tail
                )
            if notices:
                result["notice"] = " ".join(notices)
            if partial or (name == "or" and not exact):
                result["hint"] = BROAD_QUERY_HINT
            return result

        if not steps:
            return finish("and" if plan == "auto" else plan, 0, True, [], None)
        clauses: list[str] = []
        filters: list[Any] = []
        for column, value in (("wing", wing), ("lane", lane)):
            if value:
                clauses.append(f"r.{column}=?")
                filters.append(value)
        for column, values in (("provider", providers), ("kind", kinds)):
            if values:
                clauses.append(f"r.{column} IN ({','.join('?' for _ in values)})")
                filters.extend(values)

        def source(match: str) -> tuple[str, list[Any]]:
            where = " AND ".join(["record_fts MATCH ?", *clauses])
            # Joined on the row number, never on the stored ``uid`` column: reading that column
            # loads the whole stored conversation body of every match (megabytes each), which
            # made a common-word search on a large library take tens of seconds. ``put`` inserts
            # each search row under its record's row number and ``verify`` checks that alignment.
            return (
                "FROM record_fts JOIN records r ON r.rowid=record_fts.rowid WHERE " + where,
                [match, *filters],
            )

        def check() -> None:
            if deadline is not None and _clock() >= deadline:
                raise _BudgetExpired

        def count(match: str) -> tuple[int, bool]:
            text, params = source(match)
            if cap is None:
                return self.connection.execute("SELECT COUNT(*) " + text, params).fetchone()[
                    0
                ], True
            found = self.connection.execute(
                "SELECT COUNT(*) FROM (SELECT 1 " + text + " LIMIT ?)", [*params, cap + 1]
            ).fetchone()[0]
            return (cap, False) if found > cap else (found, True)

        def page(match: str, order: str) -> Any:
            # One row beyond the page says whether another page follows, whatever the total says.
            text, params = source(match)
            return self.connection.execute(
                "SELECT r.uid,r.title,r.provider,r.kind,r.wing,r.lane,r.current_version AS version,"
                "r.body_status,r.message_count,r.source_ref,snippet(record_fts,-1,'[',']','...',28) AS excerpt "
                + text
                + order
                + " LIMIT ? OFFSET ?",
                [*params, limit + 1, offset],
            )

        def ranked_page(match: str) -> list[sqlite3.Row]:
            """The ranked page, in two steps. SQLite computes every selected column of every
            match before it sorts, so selecting the excerpt in the ranking statement ran
            ``snippet`` over thousands of stored bodies to keep nine. Step one ranks and pages
            on row numbers alone; step two reads the excerpts of just those rows. Both steps
            read one snapshot: a writer that commits between two separate statements would
            otherwise drop a ranked row from the page."""
            text, params = source(match)
            own_snapshot = not self.connection.in_transaction
            if own_snapshot:
                self.connection.execute("BEGIN")
            try:
                keys = [
                    row[0]
                    for row in self.connection.execute(
                        "SELECT record_fts.rowid "
                        + text
                        + " ORDER BY bm25(record_fts,0,5,1),r.uid LIMIT ? OFFSET ?",
                        [*params, limit + 1, offset],
                    )
                ]
                if not keys:
                    return []
                marks = ",".join("?" for _ in keys)
                by_key = {
                    row["key"]: row
                    for row in self.connection.execute(
                        "SELECT record_fts.rowid AS key,r.uid,r.title,r.provider,r.kind,r.wing,"
                        "r.lane,r.current_version AS version,r.body_status,r.message_count,"
                        "r.source_ref,snippet(record_fts,-1,'[',']','...',28) AS excerpt "
                        "FROM record_fts JOIN records r ON r.rowid=record_fts.rowid "
                        f"WHERE record_fts MATCH ? AND record_fts.rowid IN ({marks})",
                        [match, *keys],
                    )
                }
            finally:
                if own_snapshot and self.connection.in_transaction:
                    self.connection.execute("ROLLBACK")
            return [by_key[key] for key in keys if key in by_key]

        def shape(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
            hits = []
            for row in rows[:limit]:
                hit = dict(row)
                hit.pop("key", None)
                hit["title"] = hit["title"][:512]
                hit["source_ref"] = hit["source_ref"][:2048]
                for key in ("kind", "lane"):
                    hit[key] = hit[key][:128]
                hit["excerpt"] = hit["excerpt"][:360]
                hit["path"] = f"vault/records/{hit['uid']}.md"
                if hit["provider"] == WEB_CAPTURE_PROVIDER:
                    # A saved page shows where it came from and when it was fetched; the other
                    # providers' hits keep exactly the keys they had.
                    hit["capture"] = capture_summary(
                        self.record(hit["uid"]).get("provenance") or {}
                    )
                hits.append(hit)
            return hits

        def grace_deadline() -> float:
            allowance = min(seconds or UNRANKED_GRACE_SECONDS, UNRANKED_GRACE_SECONDS)
            return _clock() + max(allowance, MIN_UNRANKED_GRACE_SECONDS)

        def unranked(chosen: str) -> tuple[list[sqlite3.Row], bool]:
            """The first matches of a plan in index order, under a short allowance of their own.
            The flag says that allowance cut the page short."""
            rows: list[sqlite3.Row] = []
            try:
                with self._budget_scope(grace_deadline()):
                    rows.extend(page(chosen, " ORDER BY record_fts.rowid"))
            except _BudgetExpired:
                return rows, True
            return rows, False

        def probe(chosen: str) -> bool | None:
            """Whether a plan matches anything; None when its short allowance ran out first."""
            text, params = source(chosen)
            try:
                with self._budget_scope(grace_deadline()):
                    found = self.connection.execute("SELECT 1 " + text + " LIMIT 1", params)
                    return found.fetchone() is not None
            except _BudgetExpired:
                return None

        def partial_page(
            chosen: str, expired_in: str, rows: list[sqlite3.Row], cut: bool, total: int | None
        ) -> dict[str, Any]:
            """A page of unranked matches. A page cut before its first row is not the end of the
            results, so it repeats its offset for the pager to retry."""
            hits = shape(rows)
            stuck = cut and not hits
            more = bool(hits) and (len(rows) > limit or cut)
            next_offset = offset + len(hits) if more else (offset if stuck else None)
            counted = total is not None
            return finish(
                chosen,
                total if counted else offset + len(hits),
                counted and exact,
                hits,
                next_offset,
                expired_in=expired_in,
                tail=_UNRANKED_STUCK_TAIL if stuck else "",
            )

        index, total, exact = 0, 0, True
        name, match = steps[-1]
        try:
            while index < len(steps):
                name, match = steps[index]
                check()
                with self._budget_scope(deadline):
                    total, exact = count(match)
                if total:
                    break
                index += 1
            rows: list[sqlite3.Row] = []
            if total:
                check()
                with self._budget_scope(deadline):
                    rows = ranked_page(match)
        except _BudgetExpired:
            expired_in = steps[index][0]
            if total:
                # The count finished, so the plan has matches; only its ranking ran out of time.
                unranked_rows, cut = unranked(match)
                return partial_page(name, expired_in, unranked_rows, cut, total)
            # The budget ran out before this plan was known to be empty, which says nothing about
            # the plans after it: take the first plan that a short probe shows to match.
            unproven: str | None = None
            for later_name, later_match in steps[index:]:
                found = probe(later_match)
                if found:
                    unranked_rows, cut = unranked(later_match)
                    return partial_page(later_name, expired_in, unranked_rows, cut, None)
                if found is None and unproven is None:
                    unproven = later_name
            if unproven is not None:
                return finish(
                    unproven,
                    offset,
                    False,
                    [],
                    offset,
                    expired_in=expired_in,
                    tail=_UNPROVEN_TAIL,
                )
            # Every plan finished its probe and found nothing: the empty answer is complete.
            return finish(steps[-1][0], 0, True, [], None)
        hits = shape(rows)
        return finish(name, total, exact, hits, offset + len(hits) if len(rows) > limit else None)

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
        # The reason lives in the stored revision payload; it is not a records column, so
        # existing stores need no migration. Only non-full records are decoded. Documents
        # record a ``body_reason``; native estate records keep their reasons in ``findings``.
        # A non-full record is explained by either, so the count is decided per record and
        # reported per provider rather than inferred from one provider's convention.
        reasons: dict[str, dict[str, int]] = {}
        blank = {"non_full": 0, "explained": 0, "unexplained": 0}
        by_provider: dict[str, dict[str, int]] = {
            provider: dict(blank) for provider in counts["by_provider"]
        }
        for row in self.connection.execute(
            "SELECT r.provider AS provider, r.body_status AS body_status, o.payload AS payload "
            "FROM records r JOIN objects o ON o.digest=r.current_digest "
            "WHERE r.body_status!='full'"
        ):
            if check_callback:
                check_callback()
            extra = json.loads(zlib.decompress(row["payload"])).get("extra") or {}
            split = by_provider.setdefault(row["provider"], dict(blank))
            split["non_full"] += 1
            reason = extra.get("body_reason")
            if isinstance(reason, str) and reason:
                by_reason = reasons.setdefault(row["body_status"], {})
                by_reason[reason] = by_reason.get(reason, 0) + 1
                split["explained"] += 1
            elif row["provider"] != DOCUMENT_PROVIDER and _has_finding(extra.get("findings")):
                split["explained"] += 1
            else:
                split["unexplained"] += 1
        counts["body_reasons"] = reasons
        counts["non_full_by_provider"] = by_provider
        counts["unexplained_non_full"] = sum(item["unexplained"] for item in by_provider.values())
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
        # Search joins its rows to records by row number, so every record must sit under the
        # search row that carries its own uid.
        if self.connection.execute(
            "SELECT 1 FROM records r LEFT JOIN record_fts f ON f.rowid=r.rowid "
            "WHERE f.uid IS NOT r.uid LIMIT 1"
        ).fetchone():
            problems.append("search identity is not aligned with its record row")
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
