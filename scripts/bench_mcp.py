#!/usr/bin/env python3
"""Retrieval benchmark at the library MCP boundary.

Calls the library MCP tool handlers in-process (through ``CceMcpServer.call_tool``, the same
function the stdio server dispatches to) for a fixed query file and reports, per tool level:
wall time, response size, estimated tokens, calls to reach the expected unit and whether a
response was flagged partial. The report is JSON on stdout. The exit code is 0 when every
threshold holds, 1 when a threshold fails and 2 when the run itself is invalid (bad arguments,
unreadable library or queries, a failed first call, or the library changed during the run).

Read-only guarantee
-------------------
``--library`` names a library the caller owns: either a ``library.sqlite`` file or a published
store root (the directory the MCP server is started with ``--library-store-root``). The harness
only calls the three read-only library tools, and the library engine opens the database with
SQLite ``mode=ro``, so nothing is written, created or checkpointed. The harness also proves it:
it records the size and mtime of every entry beside the library (the library directory for a
file, the whole store for a store root) before and after the run and fails with exit 2 if any
entry was added, removed or modified. ``--hash`` additionally takes the SHA-256 of every
``library.sqlite*`` file before and after. The report never prints the library path, a query
string or any record text; queries are reported by their ids only.

Measurement basis
-----------------
* Wall time is ``time.perf_counter`` around ``CceMcpServer.call_tool``. Each call opens the
  library, runs the query and renders JSON; client and transport time are not included.
* ``response_bytes`` is the UTF-8 size of the text content block of the MCP result.
  ``est_tokens`` is ``ceil(response_bytes / 4)``. This is an estimate, not a tokenizer count.
* Levels: ``cce_library_search`` is L0 (locate), ``cce_library_read`` is L2 (evidence),
  ``cce_library_history`` is ``meta``. There is no L1 (unit) tool yet, so L1 is not reported.
* Cold: the first search of the first query in a fresh Python process (``--cold subprocess``,
  the default) or on a fresh in-process server (``--cold inprocess``). The operating system
  page cache is not dropped (that needs privileges), so cold means a fresh process and a fresh
  SQLite connection, not a cold disk. Warm: ``--repeats`` further full passes after a first pass.
* Calls to unit: the number of tool calls a reader needs to reach the expected record or
  passage. Search pages are followed with ``next_offset``. A query with ``expected_text`` then
  reads messages of the hit; ``expected_message`` is the message to start at (search hits do not
  carry a message index today, so without it the reader starts at message 0 and follows
  ``next_message``, one call per message). ``expected_text`` must fall inside one read window.
* Partial: a response is partial when its structured content carries ``partial: true``. Calls
  slower than ``--partial-after-s`` that are not flagged partial are counted for information;
  they are not a gate.

Query file (``cce.bench-queries.v1``)::

    {"schema": "cce.bench-queries.v1",
     "queries": [{"id": "q1", "query": "words", "expected_uid": "chatgpt-abc",
                  "expected_text": "optional passage", "expected_message": 0,
                  "filters": {"wing": "ai", "lane": "x", "provider": ["p"], "kind": ["k"]}}]}
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from conversation_corpus_engine import library_mcp
from conversation_corpus_engine.library_store import Library
from conversation_corpus_engine.mcp_server import CceMcpServer

REPORT_SCHEMA = "cce.bench-mcp-report.v1"
QUERIES_SCHEMA = "cce.bench-queries.v1"
TOKEN_ESTIMATE = (
    "ceil(response_bytes / 4); response_bytes is the UTF-8 size of the MCP text content block"
)
LEVELS = {
    library_mcp.SEARCH_TOOL: "L0",
    library_mcp.READ_TOOL: "L2",
    library_mcp.HISTORY_TOOL: "meta",
}
QUERY_KEYS = {"id", "query", "expected_uid", "expected_text", "expected_message", "filters"}
FILTER_KEYS = {"wing", "lane", "provider", "kind"}
HASHED_PREFIX = "library.sqlite"


class HarnessError(Exception):
    """The run is invalid; reported on stderr with exit code 2."""


# --- arguments ---------------------------------------------------------------------------


def _bounded(name: str, low: int, high: int) -> Any:
    def parse(value: str) -> int:
        number = int(value)
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f"{name} must be from {low} to {high}")
        return number

    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Benchmark the library MCP tools (read-only; see the module docstring)."
    )
    parser.add_argument(
        "--library",
        type=Path,
        required=True,
        help="a library.sqlite file or a published store root that you own; opened read-only",
    )
    parser.add_argument("--queries", type=Path, required=True, help="query file (JSON)")
    parser.add_argument("--json", action="store_true", help="JSON is the only output format")
    parser.add_argument("--repeats", type=_bounded("repeats", 1, 1000), default=5)
    parser.add_argument("--cold", choices=["subprocess", "inprocess"], default="subprocess")
    parser.add_argument("--l0-limit", type=_bounded("l0-limit", 1, 200), default=8)
    parser.add_argument("--read-limit", type=_bounded("read-limit", 1, 32000), default=1500)
    parser.add_argument("--max-calls", type=_bounded("max-calls", 1, 100), default=6)
    parser.add_argument("--hash", action="store_true", help="SHA-256 the library files too")
    parser.add_argument("--include-calls", action="store_true", help="add every call to the report")
    parser.add_argument("--partial-after-s", type=float, default=5.0)
    parser.add_argument("--max-warm-l0-p95-s", type=float, default=1.0)
    parser.add_argument("--max-cold-first-call-s", type=float, default=3.0)
    parser.add_argument("--max-l0-tokens", type=int, default=900)
    parser.add_argument("--max-unit-calls", type=int, default=2)
    parser.add_argument("--max-unit-tokens", type=int, default=2500)
    parser.add_argument("--min-unit-rate", type=float, default=0.9)
    parser.add_argument("--cold-probe", action="store_true", help=argparse.SUPPRESS)
    return parser


# --- queries -----------------------------------------------------------------------------


def load_queries(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HarnessError(f"queries file is unreadable: {type(exc).__name__}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != QUERIES_SCHEMA:
        raise HarnessError(f"queries file must declare schema {QUERIES_SCHEMA}")
    queries = payload.get("queries")
    if not isinstance(queries, list) or not queries:
        raise HarnessError("queries must be a nonempty list")
    seen: set[str] = set()
    for item in queries:
        if not isinstance(item, dict) or set(item) - QUERY_KEYS:
            raise HarnessError("each query must be an object with only known keys")
        for key in ("id", "query", "expected_uid"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise HarnessError(f"queries: {key} must be a nonempty string")
        if item["id"] in seen:
            raise HarnessError("queries: ids must be unique")
        seen.add(item["id"])
        if "expected_text" in item and (
            not isinstance(item["expected_text"], str) or not item["expected_text"]
        ):
            raise HarnessError("queries: expected_text must be a nonempty string")
        message = item.get("expected_message", 0)
        if isinstance(message, bool) or not isinstance(message, int) or message < 0:
            raise HarnessError("queries: expected_message must be a nonnegative integer")
        filters = item.get("filters", {})
        if not isinstance(filters, dict) or set(filters) - FILTER_KEYS:
            raise HarnessError("queries: filters may only use wing, lane, provider and kind")
    return queries


# --- library target, read-only proof -------------------------------------------------------


class Target:
    """A library the caller owns, as a database file or a published store root."""

    def __init__(self, path: Path) -> None:
        if not path.exists():
            raise HarnessError("library path does not exist")
        self.path = path.absolute()
        self.mode = "file" if path.is_file() else "store"
        if self.mode == "store" and not path.is_dir():
            raise HarnessError("library must be a file or a directory")

    @property
    def watched_root(self) -> Path:
        return self.path.parent if self.mode == "file" else self.path

    def server(self) -> CceMcpServer:
        root = self.watched_root
        return CceMcpServer(default_project_root=root, library_store_root=root)

    def hashed(self, relative: str) -> bool:
        name = Path(relative).name
        if self.mode == "file":
            return "/" not in relative and name.startswith(self.path.name)
        return name.startswith(HASHED_PREFIX)


@contextmanager
def pinned_library(target: Target) -> Iterator[None]:
    """For a database file, route the unmodified handlers to that file, read-only."""
    if target.mode != "file":
        yield
        return
    original = library_mcp.open_library
    database = target.path

    def open_file(_store_root: Path) -> tuple[Path, Library]:
        return database.parent, Library(database)

    library_mcp.open_library = open_file
    try:
        yield
    finally:
        library_mcp.open_library = original


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fingerprint(target: Target, *, content: bool) -> dict[str, list[Any]]:
    """Size and mtime of every watched entry; a SHA-256 for library.sqlite* when asked."""
    entries: dict[str, list[Any]] = {}
    root = target.watched_root
    recursive = target.mode == "store"

    def walk(directory: Path) -> None:
        with os.scandir(directory) as scanner:
            children = sorted(scanner, key=lambda entry: entry.name)
        for entry in children:
            relative = Path(entry.path).relative_to(root).as_posix()
            status = entry.stat(follow_symlinks=False)
            if entry.is_dir(follow_symlinks=False):
                entries[relative + "/"] = [0, 0, ""]
                if recursive:
                    walk(Path(entry.path))
                continue
            digest = _sha256(Path(entry.path)) if content and target.hashed(relative) else ""
            entries[relative] = [status.st_size, status.st_mtime_ns, digest]

    walk(root)
    return entries


def integrity(
    before: dict[str, list[Any]], after: dict[str, list[Any]], *, content: bool
) -> dict[str, Any]:
    changed = sorted(
        name for name in before.keys() | after.keys() if before.get(name) != after.get(name)
    )
    return {
        "unchanged": not changed,
        "content_hashed": content,
        "watched_entries": len(before),
        "hashed_files": sum(1 for value in before.values() if value[2]),
        "changed_entries": changed,
    }


# --- measurement -------------------------------------------------------------------------


def percentile(values: list[float], rank: int) -> float | None:
    """Nearest-rank percentile; None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(1, math.ceil(rank * len(ordered) / 100)) - 1]


def estimate_tokens(byte_count: int) -> int:
    return math.ceil(byte_count / 4)


def invoke(
    server: CceMcpServer, tool: str, arguments: dict[str, Any], *, phase: str, query_id: str
) -> dict[str, Any]:
    started = time.perf_counter()
    result: dict[str, Any] | None = None
    error: str | None = None
    try:
        result = server.call_tool(tool, arguments)
    except Exception as exc:
        error = type(exc).__name__
    wall = time.perf_counter() - started
    text, structured, wire = "", {}, 0
    if result is not None:
        text = result["content"][0]["text"]
        structured = result.get("structuredContent") or {}
        wire = len(json.dumps(result).encode("utf-8"))
        if result.get("isError"):
            error = str(structured.get("error", "tool error"))[:200]
    size = len(text.encode("utf-8"))
    return {
        "query_id": query_id,
        "phase": phase,
        "tool": tool,
        "level": LEVELS.get(tool, "other"),
        "wall_s": wall,
        "response_bytes": size,
        "est_tokens": estimate_tokens(size),
        "wire_bytes": wire,
        "partial": structured.get("partial") is True,
        "error": error,
        "_structured": structured,
    }


def first_search_arguments(query: dict[str, Any], limit: int) -> dict[str, Any]:
    return {"query": query["query"], "limit": limit, "offset": 0, **query.get("filters", {})}


def run_query(
    server: CceMcpServer, query: dict[str, Any], args: argparse.Namespace, phase: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Play one reader: search pages until the expected record, then read toward the passage."""
    calls: list[dict[str, Any]] = []
    expected = query["expected_uid"]

    def call(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        record = invoke(server, tool, arguments, phase=phase, query_id=query["id"])
        calls.append(record)
        return record

    found_rank = None
    offset: int | None = 0
    while offset is not None and len(calls) < args.max_calls:
        arguments = first_search_arguments(query, args.l0_limit)
        record = call(library_mcp.SEARCH_TOOL, {**arguments, "offset": offset})
        if record["error"]:
            break
        structured = record["_structured"]
        for position, hit in enumerate(structured.get("hits", [])):
            if hit.get("uid") == expected:
                found_rank = offset + position + 1
                break
        if found_rank is not None:
            break
        offset = structured.get("next_offset")
    reached = False
    if found_rank is not None and "expected_text" not in query:
        reached = True
    elif found_rank is not None:
        message, text_offset = query.get("expected_message", 0), 0
        while len(calls) < args.max_calls:
            record = call(
                library_mcp.READ_TOOL,
                {
                    "uid": expected,
                    "message": message,
                    "offset": text_offset,
                    "limit": args.read_limit,
                },
            )
            if record["error"]:
                break
            structured = record["_structured"]
            if query["expected_text"] in structured.get("text", ""):
                reached = True
                break
            if structured.get("next_offset") is not None:
                text_offset = structured["next_offset"]
            elif structured.get("next_message") is not None:
                message, text_offset = structured["next_message"], 0
            else:
                break
    tokens = sum(item["est_tokens"] for item in calls)
    outcome = {
        "id": query["id"],
        "reached": reached,
        "found_rank": found_rank,
        "calls_issued": len(calls),
        "calls_to_unit": len(calls) if reached else None,
        "tokens_to_unit": tokens if reached else None,
        "within_budget": reached
        and len(calls) <= args.max_unit_calls
        and tokens <= args.max_unit_tokens,
        "partial": any(item["partial"] for item in calls),
        "error": next((item["error"] for item in calls if item["error"]), None),
    }
    return calls, outcome


def summary(values: list[float], digits: int | None = None) -> dict[str, float | None]:
    def shape(value: float | None) -> float | None:
        return value if value is None or digits is None else round(value, digits)

    return {
        "p50": shape(percentile(values, 50)),
        "p95": shape(percentile(values, 95)),
        "max": shape(max(values) if values else None),
    }


def level_stats(calls: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(calls),
        "wall_s": summary([item["wall_s"] for item in calls], 6),
        "response_bytes": summary([item["response_bytes"] for item in calls]),
        "est_tokens": summary([item["est_tokens"] for item in calls]),
        "partial_calls": sum(1 for item in calls if item["partial"]),
        "errors": sum(1 for item in calls if item["error"]),
    }


def levels_report(calls: list[dict[str, Any]]) -> dict[str, Any]:
    report: dict[str, Any] = {}
    for level in sorted({item["level"] for item in calls}):
        report[level] = {
            phase: level_stats(
                [item for item in calls if item["level"] == level and item["phase"] == phase]
            )
            for phase in ("cold", "first", "warm")
            if any(item["level"] == level and item["phase"] == phase for item in calls)
        }
    return report


def check(name: str, value: float | None, limit: float, relation: str) -> dict[str, Any]:
    if value is None:
        ok = False
    elif relation == "<":
        ok = value < limit
    elif relation == "<=":
        ok = value <= limit
    else:
        ok = value >= limit
    shown = round(value, 6) if isinstance(value, float) else value
    return {"name": name, "value": shown, "limit": limit, "relation": relation, "ok": ok}


# --- cold --------------------------------------------------------------------------------


def public(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key != "_structured"}


def cold_inprocess(
    target: Target, query: dict[str, Any], args: argparse.Namespace
) -> dict[str, Any]:
    with pinned_library(target):
        return invoke(
            target.server(),
            library_mcp.SEARCH_TOOL,
            first_search_arguments(query, args.l0_limit),
            phase="cold",
            query_id=query["id"],
        )


def cold_subprocess(args: argparse.Namespace) -> tuple[dict[str, Any], float]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--cold-probe",
        "--library",
        str(args.library),
        "--queries",
        str(args.queries),
        "--l0-limit",
        str(args.l0_limit),
    ]
    started = time.perf_counter()
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HarnessError(f"cold probe did not run: {type(exc).__name__}") from exc
    elapsed = time.perf_counter() - started
    try:
        record = json.loads(done.stdout)["record"]
    except (ValueError, KeyError, TypeError) as exc:
        message = done.stderr.strip().splitlines()[-1:] or ["no output"]
        raise HarnessError(f"cold probe failed: {message[0][:200]}") from exc
    return record, elapsed


def cold_probe(args: argparse.Namespace) -> int:
    target = Target(args.library)
    query = load_queries(args.queries)[0]
    record = public(cold_inprocess(target, query, args))
    json.dump({"record": record}, sys.stdout)
    return 0


# --- run ---------------------------------------------------------------------------------


def benchmark(args: argparse.Namespace) -> int:
    target = Target(args.library)
    queries = load_queries(args.queries)
    before = fingerprint(target, content=args.hash)
    process_wall: float | None = None
    if args.cold == "subprocess":
        cold, process_wall = cold_subprocess(args)
    else:
        cold = public(cold_inprocess(target, queries[0], args))
    if cold["error"]:
        raise HarnessError(f"first call failed: {cold['error']}")
    calls = [cold]
    outcomes: list[dict[str, Any]] = []
    with pinned_library(target):
        server = target.server()
        for query in queries:
            query_calls, outcome = run_query(server, query, args, "first")
            calls.extend(query_calls)
            outcomes.append(outcome)
        for _ in range(args.repeats):
            for query in queries:
                calls.extend(run_query(server, query, args, "warm")[0])
    after = fingerprint(target, content=args.hash)
    proof = integrity(before, after, content=args.hash)
    levels = levels_report(calls)
    l0_calls = [item for item in calls if item["level"] == "L0"]
    reached = sum(1 for item in outcomes if item["reached"])
    within = sum(1 for item in outcomes if item["within_budget"])
    warm_l0 = levels.get("L0", {}).get("warm", {}).get("wall_s", {}).get("p95")
    checks = [
        check("warm_l0_p95_s", warm_l0, args.max_warm_l0_p95_s, "<"),
        check("cold_first_call_s", cold["wall_s"], args.max_cold_first_call_s, "<"),
        check(
            "l0_response_tokens",
            max((item["est_tokens"] for item in l0_calls), default=None),
            args.max_l0_tokens,
            "<=",
        ),
        check("unit_within_budget_rate", within / len(outcomes), args.min_unit_rate, ">="),
    ]
    slow = [item for item in calls if item["wall_s"] > args.partial_after_s]
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "token_estimate": TOKEN_ESTIMATE,
        "timing": "perf_counter around CceMcpServer.call_tool, library opened per call",
        "library": {"mode": target.mode, "integrity": proof},
        "config": {
            "repeats": args.repeats,
            "l0_limit": args.l0_limit,
            "read_limit_chars": args.read_limit,
            "max_calls_per_query": args.max_calls,
            "unit_budget": {"calls": args.max_unit_calls, "tokens": args.max_unit_tokens},
        },
        "cold": {
            "mode": args.cold,
            "first_call_s": round(cold["wall_s"], 6),
            "response_bytes": cold["response_bytes"],
            "est_tokens": cold["est_tokens"],
            "process_wall_s": None if process_wall is None else round(process_wall, 6),
            "page_cache": "not dropped",
        },
        "levels": levels,
        "units": {
            "queries": len(outcomes),
            "reached": reached,
            "within_budget": within,
            "within_budget_rate": round(within / len(outcomes), 6),
        },
        "queries": outcomes,
        "partial": {
            "partial_calls": sum(1 for item in calls if item["partial"]),
            "partial_after_s": args.partial_after_s,
            "calls_over_partial_after_s": len(slow),
            "slow_calls_not_flagged_partial": sum(1 for item in slow if not item["partial"]),
        },
        "checks": checks,
        "pass": proof["unchanged"] and all(item["ok"] for item in checks),
    }
    if args.include_calls:
        report["calls"] = [{**public(item), "wall_s": round(item["wall_s"], 6)} for item in calls]
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    if not proof["unchanged"]:
        print("bench_mcp: library entries changed during the run", file=sys.stderr)
        return 2
    return 0 if report["pass"] else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return cold_probe(args) if args.cold_probe else benchmark(args)
    except HarnessError as exc:
        print(f"bench_mcp: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
