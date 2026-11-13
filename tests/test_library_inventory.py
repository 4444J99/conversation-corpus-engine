"""The native inventory producer: identity parity, multi-root folding, safety and the reader.

Every source here is a synthetic fixture in a temporary directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from conversation_corpus_engine import library_inventory
from conversation_corpus_engine.cli import build_parser
from conversation_corpus_engine.library_commands import handle_library
from conversation_corpus_engine.library_inventory import InventorySources, build_inventory
from conversation_corpus_engine.library_native import iter_estate

ZSTD = shutil.which("zstd")
SENTINEL = "SYNTHETIC-TRANSCRIPT-SENTINEL"
THREAD = "11111111-2222-3333-4444-555555555555"
PARENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OTHER = "99999999-8888-7777-6666-555555555555"


def jsonl(path: Path, rows: list[Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def zst(path: Path, rows: list[Any]) -> Path:
    plain = jsonl(path.with_suffix(""), rows)
    subprocess.run(["zstd", "-q", "--rm", "-f", str(plain), "-o", str(path)], check=True)
    return path


def database(path: Path, script: str, *statements: tuple[str, tuple[Any, ...]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(script)
        for sql, parameters in statements:
            connection.execute(sql, parameters)
    return path


def claude_event(session: str, text: str = "hello", **extra: Any) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": f"u-{text}",
        "sessionId": session,
        "timestamp": "2026-01-01T00:00:00Z",
        "cwd": "/synthetic/project",
        "gitBranch": "main",
        "message": {"role": "user", "content": text},
        **extra,
    }


def codex_meta(thread: str = THREAD, **payload: Any) -> dict[str, Any]:
    return {
        "type": "session_meta",
        "payload": {
            "id": thread,
            "timestamp": "2026-01-01T00:00:00Z",
            "cwd": "/synthetic/project",
            "source": "cli",
            **payload,
        },
    }


def codex_message(event: str, text: str, role: str = "user") -> dict[str, Any]:
    return {
        "type": "response_item",
        "timestamp": "2026-01-01T00:00:01Z",
        "payload": {
            "type": "message",
            "id": event,
            "role": role,
            "content": [{"type": "input_text", "text": text}],
        },
    }


def run_inventory(tmp_path: Path, name: str = "out", **kwargs: Any) -> dict[str, Any]:
    names = {key: tuple(value) for key, value in kwargs.items()}
    return build_inventory(InventorySources(**names), tmp_path / name)


def rows(tmp_path: Path, name: str = "out") -> dict[tuple[str, str], dict[str, Any]]:
    with sqlite3.connect(tmp_path / name / "session-corpus.db") as connection:
        connection.row_factory = sqlite3.Row
        return {
            (r["agent_family"], r["session_id"]): dict(r)
            for r in connection.execute("SELECT * FROM sessions")
        }


def observed(tmp_path: Path, family: str, name: str = "out") -> list[tuple[str, str, str]]:
    with sqlite3.connect(tmp_path / name / "session-corpus.db") as connection:
        return connection.execute(
            "SELECT source_path,object_key,native_id FROM source_observations "
            "WHERE agent_family=? ORDER BY source_path,object_key",
            (family,),
        ).fetchall()


def findings(tmp_path: Path, family: str, name: str = "out") -> set[str]:
    with sqlite3.connect(tmp_path / name / "session-corpus.db") as connection:
        return {
            r[0]
            for r in connection.execute(
                "SELECT reason FROM unresolved_source_observations WHERE agent_family=?",
                (family,),
            )
        }


def kinds(tmp_path: Path, family: str, name: str = "out") -> dict[str, tuple[str, str | None]]:
    return {
        sid: (row["session_kind"], row["parent_session_id"])
        for (fam, sid), row in rows(tmp_path, name).items()
        if fam == family
    }


# ---------------------------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------------------------


def test_claude_root_subagent_sidechain_and_non_session_files(tmp_path):
    projects = tmp_path / "claude"
    jsonl(projects / "p1" / "s1.jsonl", [claude_event("s1", isSidechain=False)])
    jsonl(
        projects / "p1" / "s1" / "subagents" / "agent-a1.jsonl",
        [claude_event("s1", "child", agentId="a1", isSidechain=True)],
    )
    jsonl(projects / "p1" / "loose.jsonl", [claude_event("s2", "x", isSidechain=True)])
    jsonl(projects / "p1" / "unflagged.jsonl", [claude_event("s3")])
    (projects / "p1" / "empty.jsonl").write_text("")
    jsonl(projects / "p1" / "job" / "journal.jsonl", [{"type": "launched"}, {"type": "result"}])
    receipt = run_inventory(tmp_path, claude_projects=[projects])

    assert kinds(tmp_path, "claude") == {
        "s1": ("root", None),
        "s1/agent/a1": ("subagent", "s1"),
        "s3": ("unknown", None),
    }
    assert ("claude", "s2") not in rows(tmp_path)
    assert {"agent_identity_missing"} <= findings(tmp_path, "claude")
    family = receipt["families"]["claude"]
    assert family["sessions_by_kind"] == {"root": 1, "subagent": 1, "unknown": 1}
    assert family["artifacts_by_classification"] == {
        "agent_execution_journal": 1,
        "empty_transcript": 1,
    }
    row = rows(tmp_path)[("claude", "s1")]
    assert (row["started_at"], row["project_root"], row["primary_branch"]) == (
        "2026-01-01T00:00:00Z",
        "/synthetic/project",
        "main",
    )
    assert row["prompt_title"] == "Claude session s1"
    assert row["status"] == "unknown"
    assert row["custody_path"] == str((projects / "p1" / "s1.jsonl").resolve())


def test_claude_transcript_with_several_identities_is_read_to_the_end(tmp_path):
    projects = tmp_path / "claude"
    jsonl(
        projects / "p" / "mixed.jsonl",
        [claude_event("first", isSidechain=False)]
        + [{"type": "progress"} for _ in range(100)]
        + [claude_event("late", "late", isSidechain=False)],
    )
    run_inventory(tmp_path, claude_projects=[projects])
    assert set(kinds(tmp_path, "claude")) == {"first", "late"}


def test_malformed_lines_and_bad_agent_identity_become_findings(tmp_path):
    projects = tmp_path / "claude"
    path = projects / "p" / "s.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(claude_event("s", isSidechain=False))
        + "\n{broken\n[1]\n"
        + json.dumps(claude_event("s", agentId="", isSidechain=True))
        + "\n"
    )
    run_inventory(tmp_path, claude_projects=[projects])
    assert kinds(tmp_path, "claude") == {"s": ("root", None)}
    assert {"invalid_json", "invalid_event", "invalid_agent_identity"} <= findings(
        tmp_path, "claude"
    )


# ---------------------------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------------------------


def test_codex_identity_comes_from_the_payload_not_the_file_name(tmp_path):
    home = tmp_path / "codex"
    jsonl(
        home / "sessions" / "2026" / "rollout-misleading-name.jsonl",
        [codex_meta(THREAD, session_id="ignored-because-id-wins"), codex_message("m", "hi")],
    )
    jsonl(
        home / "sessions" / "second.jsonl",
        [{"type": "session_meta", "payload": {"session_id": "legacy"}}],
    )
    run_inventory(tmp_path, codex_roots=[home])
    found = rows(tmp_path)
    assert set(found) == {("codex", THREAD), ("codex", "legacy")}
    assert found[("codex", THREAD)]["session_kind"] == "root"
    assert found[("codex", THREAD)]["project_root"] == "/synthetic/project"
    assert found[("codex", "legacy")]["session_kind"] == "unknown"
    assert [o[1] for o in observed(tmp_path, "codex")] == ["session_meta", "session_meta"]


def test_codex_session_meta_is_found_only_in_the_first_lines(tmp_path):
    home = tmp_path / "codex"
    early = [{"type": "noise"} for _ in range(63)] + [codex_meta("early")]
    late = [{"type": "noise"} for _ in range(64)] + [codex_meta("late")]
    jsonl(home / "sessions" / "early.jsonl", early)
    jsonl(home / "sessions" / "late.jsonl", late)
    run_inventory(tmp_path, codex_roots=[home])
    assert set(kinds(tmp_path, "codex")) == {"early"}
    assert "native_identity_missing" in findings(tmp_path, "codex")


@pytest.mark.parametrize(
    ("payload", "expected", "finding"),
    [
        ({"parent_thread_id": PARENT}, ("subagent", PARENT), None),
        (
            {"source": {"subagent": {"thread_spawn": {"parent_thread_id": PARENT}}}},
            ("subagent", PARENT),
            None,
        ),
        ({"source": {"subagent": {}}}, ("subagent", None), "parent_identity_missing"),
        (
            {
                "parent_thread_id": PARENT,
                "source": {"subagent": {"thread_spawn": {"parent_thread_id": OTHER}}},
            },
            ("unknown", None),
            "conflicting_parent_identity",
        ),
        ({"parent_thread_id": THREAD}, ("unknown", None), "conflicting_parent_identity"),
        ({"source": "vscode"}, ("root", None), None),
        ({"source": "mcp"}, ("unknown", None), None),
    ],
)
def test_codex_rollout_kind_and_parent(tmp_path, payload, expected, finding):
    home = tmp_path / "codex"
    jsonl(home / "sessions" / "r.jsonl", [codex_meta(THREAD, **payload)])
    run_inventory(tmp_path, codex_roots=[home])
    assert kinds(tmp_path, "codex") == {THREAD: expected}
    assert (finding in findings(tmp_path, "codex")) if finding else True


def state_database(
    home: Path, edges: list[tuple[str, str]], threads: list[tuple[Any, ...]]
) -> Path:
    return database(
        home / "state_5.sqlite",
        "CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT);"
        "CREATE TABLE threads (id TEXT, rollout_path TEXT, source TEXT, cwd TEXT, title TEXT,"
        " git_branch TEXT, git_sha TEXT, created_at INT);",
        *[("INSERT INTO thread_spawn_edges VALUES (?,?)", edge) for edge in edges],
        *[("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?)", row) for row in threads],
    )


def test_codex_state_database_supplies_parents_and_threads(tmp_path):
    home = tmp_path / "codex"
    rollout = jsonl(home / "sessions" / "r.jsonl", [codex_meta(THREAD, source="exec")])
    state_database(
        home,
        [(PARENT, THREAD)],
        [
            (
                THREAD,
                str(rollout),
                "cli",
                "/synthetic/cwd",
                "Thread title",
                "main",
                "abc",
                1767225600,
            ),
            (PARENT, "", '"vscode"', "/synthetic/parent", None, None, None, None),
        ],
    )
    run_inventory(tmp_path, codex_roots=[home])
    assert kinds(tmp_path, "codex") == {THREAD: ("subagent", PARENT), PARENT: ("root", None)}
    row = rows(tmp_path)[("codex", THREAD)]
    assert row["prompt_title"] == "Codex session " + THREAD  # the rollout upsert wins last
    assert row["started_at"].startswith("2026-01-01")
    keys = {o[1] for o in observed(tmp_path, "codex")}
    assert {f"threads/{THREAD}", f"threads/{PARENT}", "session_meta"} <= keys
    assert f"thread_spawn_edges/{PARENT}/{THREAD}" in keys


def test_codex_conflict_with_a_state_thread_row_keeps_the_state_edge(tmp_path):
    # The state database is read first. A rollout naming a second parent is a conflict, and its
    # unknown upsert restores the kind and parent the thread row established.
    home = tmp_path / "codex"
    jsonl(home / "sessions" / "r.jsonl", [codex_meta(THREAD, parent_thread_id=OTHER)])
    state_database(home, [(PARENT, THREAD)], [(THREAD, "", "cli", None, None, None, None, None)])
    run_inventory(tmp_path, codex_roots=[home])
    assert kinds(tmp_path, "codex") == {THREAD: ("subagent", PARENT)}
    assert "conflicting_parent_identity" in findings(tmp_path, "codex")


def test_codex_conflict_without_a_state_thread_row_is_unknown(tmp_path):
    home = tmp_path / "codex"
    jsonl(home / "sessions" / "r.jsonl", [codex_meta(THREAD, parent_thread_id=OTHER)])
    state_database(home, [(PARENT, THREAD)], [])
    run_inventory(tmp_path, codex_roots=[home])
    assert kinds(tmp_path, "codex") == {THREAD: ("unknown", None)}


def test_codex_copies_that_disagree_about_the_parent_become_unknown(tmp_path):
    home = tmp_path / "codex"
    jsonl(home / "sessions" / "a.jsonl", [codex_meta(THREAD, parent_thread_id=PARENT)])
    jsonl(home / "archived_sessions" / "b.jsonl", [codex_meta(THREAD, parent_thread_id=OTHER)])
    run_inventory(tmp_path, codex_roots=[home])
    assert kinds(tmp_path, "codex") == {THREAD: ("unknown", None)}
    assert len(observed(tmp_path, "codex")) == 2


def test_codex_conflicting_edges_in_the_state_database(tmp_path):
    home = tmp_path / "codex"
    state_database(
        home,
        [(PARENT, THREAD), (OTHER, THREAD)],
        [(THREAD, "", "cli", None, None, None, None, None)],
    )
    run_inventory(tmp_path, codex_roots=[home])
    assert kinds(tmp_path, "codex") == {THREAD: ("unknown", None)}
    assert "conflicting_parent_identity" in findings(tmp_path, "codex")


@pytest.mark.skipif(ZSTD is None, reason="zstd is not installed")
def test_codex_compressed_rollout_has_the_same_identity_and_works_without_a_census(tmp_path):
    home = tmp_path / "codex"
    zst(home / "archived_sessions" / f"rollout-{THREAD}.jsonl.zst", [codex_meta(THREAD)])
    run_inventory(tmp_path, codex_roots=[home])
    assert kinds(tmp_path, "codex") == {THREAD: ("root", None)}
    (path, key, native) = observed(tmp_path, "codex")[0]
    assert path.endswith(".jsonl.zst") and (key, native) == ("session_meta", THREAD)


@pytest.mark.skipif(ZSTD is None, reason="zstd is not installed")
def test_codex_census_relocation_requires_a_name_that_matches_the_header(tmp_path):
    home = tmp_path / "codex"
    zst(home / "archived_sessions" / f"rollout-{THREAD}.jsonl.zst", [codex_meta(THREAD)])
    zst(home / "archived_sessions" / f"rollout-{PARENT}.jsonl.zst", [codex_meta(OTHER)])
    census = tmp_path / "census.json"
    census.write_text(
        json.dumps(
            {
                "sessions": [
                    {"relative_path": f"sessions/2026/rollout-{THREAD}.jsonl"},
                    {"relative_path": f"sessions/2026/rollout-{PARENT}.jsonl"},
                    {"relative_path": "sessions/2026/rollout-gone.jsonl"},
                    {"relative_path": "../escape.jsonl"},
                    {"nothing": True},
                ]
            }
        )
    )
    run_inventory(tmp_path, codex_roots=[home], codex_censuses=[census])
    # Identity is the header's, whether or not the census agrees with the file name.
    assert set(kinds(tmp_path, "codex")) == {THREAD, OTHER}
    assert {
        "relocated_native_identity_mismatch",
        "native_source_missing",
        "native_path_missing",
    } <= findings(tmp_path, "codex")
    with sqlite3.connect(tmp_path / "out" / "session-corpus.db") as connection:
        artifacts = connection.execute(
            "SELECT classification,count(*) FROM source_artifact_observations "
            "WHERE agent_family='codex' GROUP BY classification"
        ).fetchall()
    assert artifacts == [("census_row_not_reader_resolvable", 1)]


def test_codex_census_row_is_an_observation_when_the_reader_can_follow_it(tmp_path):
    home = tmp_path / "codex"
    jsonl(home / "sessions" / "r.jsonl", [codex_meta(THREAD), codex_message("m", "census body")])
    census = home / "census.json"
    census.write_text(json.dumps({"sessions": [{"relative_path": "sessions/r.jsonl"}]}))
    run_inventory(tmp_path, codex_roots=[home], codex_censuses=[census])
    assert (str(census.resolve()), "sessions/0", THREAD) in observed(tmp_path, "codex")
    estate = next(iter_estate(tmp_path / "out" / "session-corpus.db"))
    assert estate.extra["body_status"] == "full"
    assert [m.text for m in estate.messages] == ["census body"]


def test_codex_census_alone_cannot_create_an_identity(tmp_path):
    home = tmp_path / "codex"
    home.mkdir()
    census = tmp_path / "census.json"
    census.write_text(json.dumps({"sessions": [{"relative_path": "sessions/x.jsonl"}]}))
    run_inventory(tmp_path, codex_roots=[home], codex_censuses=[census])
    assert rows(tmp_path) == {}


# ---------------------------------------------------------------------------------------------
# Gemini, Antigravity, Copilot, OpenCode
# ---------------------------------------------------------------------------------------------


def test_gemini_census_rows(tmp_path):
    census = tmp_path / "gemini.json"
    census.write_text(
        json.dumps(
            {
                "sessions": [
                    {"session_id": "g1", "kind": "main", "start_time": "2026-01-01", "title": "T"},
                    {"session_id": "g2", "parent_session_id": "g1"},
                    {"session_id": "g3", "kind": "subagent"},
                    {"session_id": "g4", "parent_session_id": "g4", "kind": "main"},
                    {"session_id": "g5", "kind": "other"},
                    {"title": "no identity"},
                    "not a record",
                ]
            }
        )
    )
    run_inventory(tmp_path, gemini_sources=[census])
    assert kinds(tmp_path, "gemini") == {
        "g1": ("root", None),
        "g2": ("subagent", "g1"),
        "g3": ("subagent", None),
        "g4": ("unknown", None),
        "g5": ("unknown", None),
    }
    assert {"native_identity_missing", "invalid_record", "self_parent_identity"} <= findings(
        tmp_path, "gemini"
    )
    assert rows(tmp_path)[("gemini", "g1")]["prompt_title"] == "T"
    assert ("sessions/0", "g1") in {(o[1], o[2]) for o in observed(tmp_path, "gemini")}


def test_antigravity_directory_database_and_census_forms(tmp_path):
    cli = tmp_path / "agy"
    database(
        cli / "conversation_summaries.db",
        "CREATE TABLE conversation_summaries (conversation_id TEXT, title TEXT, preview TEXT,"
        " parent_conversation_id TEXT, workspace_uris_db TEXT);",
        (
            "INSERT INTO conversation_summaries VALUES (?,?,?,?,?)",
            ("a1", "Title", "Preview", None, "file:///synthetic/ws,file:///other"),
        ),
        (
            "INSERT INTO conversation_summaries VALUES (?,?,?,?,?)",
            ("a2", "Child", "", "a1", None),
        ),
    )
    database(
        cli / "conversations" / "a3.db",
        "CREATE TABLE trajectory_meta (cascade_id TEXT);",
        ("INSERT INTO trajectory_meta VALUES (?)", ("a3",)),
    )
    census = tmp_path / "agy-census.json"
    census.write_text(
        json.dumps(
            {
                "sessions": {
                    "a4": {"title": "from census", "is_subagent": False},
                    "a5": {"conversation_id": "other-id"},
                },
                "tm_cli": {"sessions": [{"uuid": "a6", "nesting_depth": 2}, {"title": "x"}]},
            }
        )
    )
    run_inventory(tmp_path, antigravity_sources=[cli, census])
    assert kinds(tmp_path, "antigravity") == {
        "a1": ("unknown", None),
        "a2": ("subagent", "a1"),
        "a3": ("unknown", None),
        "a4": ("root", None),
        "a6": ("subagent", None),
    }
    assert {"conflicting_native_identity", "native_identity_missing"} <= findings(
        tmp_path, "antigravity"
    )
    first = rows(tmp_path)[("antigravity", "a1")]
    assert (first["prompt_title"], first["prompt_snippet"], first["project_root"]) == (
        "Title",
        "Preview",
        "/synthetic/ws",
    )
    keys = {o[1] for o in observed(tmp_path, "antigravity")}
    assert json.dumps(["conversation_summaries", "a1", 0], separators=(",", ":")) in keys
    assert "sessions/a4" in keys and "tm_cli/sessions/0" in keys


def test_antigravity_wal_mode_archive_without_shm_is_a_findings_snapshot(tmp_path):
    source = tmp_path / "agy" / "conversation_summaries.db"
    source.parent.mkdir()
    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE conversation_summaries (conversation_id TEXT)")
        connection.execute("INSERT INTO conversation_summaries VALUES ('w1')")
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    for sidecar in source.parent.glob("*-*"):
        sidecar.unlink()
    before = sorted(p.name for p in source.parent.iterdir())
    receipt = run_inventory(tmp_path, antigravity_sources=[source.parent])
    assert set(kinds(tmp_path, "antigravity")) == {"w1"}
    assert sorted(p.name for p in source.parent.iterdir()) == before
    assert receipt["families"]["antigravity"]["sessions"] == 1
    assert receipt["families"]["antigravity"]["custody_findings"] == {
        "historical_wal_custody_unverified": 1
    }


def test_copilot_database_and_session_state_are_unioned_not_doubled(tmp_path):
    store = database(
        tmp_path / "copilot" / "session-store.db",
        "CREATE TABLE sessions (id TEXT, cwd TEXT, branch TEXT, created_at TEXT, summary TEXT);",
        (
            "INSERT INTO sessions VALUES (?,?,?,?,?)",
            ("c1", "/synthetic/db", "dev", "2026-01-01", "Stored"),
        ),
        ("INSERT INTO sessions VALUES (?,?,?,?,?)", ("c2", None, None, None, None)),
    )
    state = tmp_path / "copilot" / "session-state"
    jsonl(
        state / "c1" / "events.jsonl",
        [
            {
                "type": "session.start",
                "data": {
                    "sessionId": "c1",
                    "startTime": "t",
                    "context": {"cwd": "/d", "branch": "b"},
                },
            }
        ],
    )
    jsonl(state / "c3" / "events.jsonl", [{"type": "user.message", "data": {"content": "x"}}])
    jsonl(
        state / "c4" / "events.jsonl",
        [{"type": "session.start", "data": {"sessionId": "different"}}],
    )
    (state / "loose-file.txt").parent.mkdir(parents=True, exist_ok=True)
    (state / "loose-file.txt").write_text("not a session")
    (state / ".hidden").mkdir()
    run_inventory(tmp_path, copilot_state_dirs=[state], copilot_databases=[store])
    assert kinds(tmp_path, "copilot") == {
        "c1": ("unknown", None),
        "c2": ("unknown", None),
        "c3": ("unknown", None),
        ".hidden": ("unknown", None),
    }
    assert "directory_native_identity_conflict" in findings(tmp_path, "copilot")
    c1 = rows(tmp_path)[("copilot", "c1")]
    # started_at keeps the first non-null value (the store's); the directory fills the rest.
    assert (c1["project_root"], c1["primary_branch"], c1["started_at"]) == ("/d", "b", "2026-01-01")
    c1_keys = {(o[1]) for o in observed(tmp_path, "copilot") if o[2] == "c1"}
    assert c1_keys == {"sessions/c1", "session-state"}


def test_opencode_sessions_v1_and_v2_with_parents(tmp_path):
    source = database(
        tmp_path / "opencode.db",
        "CREATE TABLE session (id TEXT, title TEXT, time_created INT, time_updated INT,"
        " directory TEXT, parent_id TEXT);"
        "CREATE TABLE session_v2 (id TEXT, title TEXT, time_created INT, time_updated INT,"
        " directory TEXT);",
        (
            "INSERT INTO session VALUES (?,?,?,?,?,?)",
            ("o1", "Root", 1767225600000, 1767225601000, "/d", None),
        ),
        (
            "INSERT INTO session VALUES (?,?,?,?,?,?)",
            ("o2", None, 1767225600000, 1767225601000, "/d", "o1"),
        ),
        (
            "INSERT INTO session_v2 VALUES (?,?,?,?,?)",
            ("o3", "V2", 1767225600000, 1767225601000, "/e"),
        ),
    )
    run_inventory(tmp_path, opencode_databases=[source])
    assert kinds(tmp_path, "opencode") == {
        "o1": ("root", None),
        "o2": ("subagent", "o1"),
        "o3": ("unknown", None),
    }
    o1 = rows(tmp_path)[("opencode", "o1")]
    assert o1["started_at"].startswith("2026-01-01")
    assert o1["prompt_title"] == "Root"
    assert rows(tmp_path)[("opencode", "o2")]["prompt_title"] == "OpenCode session o2"
    assert {o[1] for o in observed(tmp_path, "opencode")} == {
        "session/o1",
        "session/o2",
        "session_v2/o3",
    }


def test_same_native_id_in_two_families_stays_two_rows(tmp_path):
    projects = tmp_path / "claude"
    jsonl(projects / "p" / "s.jsonl", [claude_event("shared", isSidechain=False)])
    census = tmp_path / "gemini.json"
    census.write_text(json.dumps({"sessions": [{"session_id": "shared"}]}))
    run_inventory(tmp_path, claude_projects=[projects], gemini_sources=[census])
    assert set(rows(tmp_path)) == {("claude", "shared"), ("gemini", "shared")}


# ---------------------------------------------------------------------------------------------
# Multi-root folding
# ---------------------------------------------------------------------------------------------


def test_identical_copies_across_roots_are_observed_once(tmp_path):
    rows_ = [claude_event("s", isSidechain=False)]
    live = jsonl(tmp_path / "live" / "p" / "s.jsonl", rows_)
    archive_a = jsonl(tmp_path / "archive-a" / "p" / "s.jsonl", rows_)
    jsonl(tmp_path / "archive-b" / "q" / "copy.jsonl", rows_)
    receipt = run_inventory(
        tmp_path,
        claude_projects=[tmp_path / "live", tmp_path / "archive-a", tmp_path / "archive-b"],
    )
    assert [o[0] for o in observed(tmp_path, "claude")] == [str(live.resolve())]
    assert receipt["families"]["claude"]["folded_duplicates"] == 2
    assert receipt["totals"]["folded_duplicates"] == 2
    assert archive_a.exists()
    assert len(rows(tmp_path)) == 1


def test_differing_copies_are_all_observed_under_one_identity(tmp_path):
    base = [claude_event("s", isSidechain=False)]
    jsonl(
        tmp_path / "live" / "p" / "s.jsonl", base + [claude_event("s", "newer", isSidechain=False)]
    )
    jsonl(tmp_path / "archive" / "p" / "s.jsonl", base)
    receipt = run_inventory(tmp_path, claude_projects=[tmp_path / "live", tmp_path / "archive"])
    assert len(rows(tmp_path)) == 1
    assert len(observed(tmp_path, "claude")) == 2
    assert receipt["families"]["claude"]["folded_duplicates"] == 0
    assert receipt["families"]["claude"]["observed_source_files"] == 2


def test_folding_keeps_the_first_in_declaration_order(tmp_path):
    rows_ = [claude_event("s", isSidechain=False)]
    jsonl(tmp_path / "a" / "p" / "s.jsonl", rows_)
    second = jsonl(tmp_path / "b" / "p" / "s.jsonl", rows_)
    run_inventory(tmp_path, claude_projects=[tmp_path / "b", tmp_path / "a"])
    assert [o[0] for o in observed(tmp_path, "claude")] == [str(second.resolve())]


def test_same_size_but_different_bytes_are_not_folded(tmp_path):
    jsonl(tmp_path / "a" / "p" / "s.jsonl", [claude_event("s", "aaaa", isSidechain=False)])
    jsonl(tmp_path / "b" / "p" / "s.jsonl", [claude_event("s", "bbbb", isSidechain=False)])
    receipt = run_inventory(tmp_path, claude_projects=[tmp_path / "a", tmp_path / "b"])
    assert receipt["families"]["claude"]["folded_duplicates"] == 0
    assert len(observed(tmp_path, "claude")) == 2


def test_codex_live_and_archived_copies_fold_and_a_changed_database_does_not(tmp_path):
    rollout = [codex_meta(THREAD), codex_message("m", "same")]
    jsonl(tmp_path / "live" / "sessions" / "r.jsonl", rollout)
    jsonl(tmp_path / "old" / "archived_sessions" / "r.jsonl", rollout)
    state_database(tmp_path / "live", [], [(THREAD, "", "cli", None, None, None, None, None)])
    state_database(tmp_path / "old", [], [(THREAD, "", "cli", None, None, None, None, 5)])
    receipt = run_inventory(tmp_path, codex_roots=[tmp_path / "live", tmp_path / "old"])
    family = receipt["families"]["codex"]
    assert family["folded_duplicates"] == 1  # only the rollout; the databases differ
    assert family["sessions"] == 1
    assert family["source_observations"] == 3  # rollout, and the thread row in each database


def test_database_copies_fold_only_when_their_pending_journal_matches(tmp_path):
    first = database(
        tmp_path / "a" / "o.db",
        "CREATE TABLE session (id TEXT, title TEXT, time_created INT, time_updated INT,"
        " directory TEXT);",
        ("INSERT INTO session VALUES ('o1','T',1,1,'/d')", ()),
    )
    second = tmp_path / "b" / "o.db"
    second.parent.mkdir()
    shutil.copyfile(first, second)
    receipt = run_inventory(tmp_path, opencode_databases=[first, second])
    assert receipt["families"]["opencode"]["folded_duplicates"] == 1
    assert len(observed(tmp_path, "opencode")) == 1


def test_copilot_session_state_directories_fold_by_their_event_log(tmp_path):
    events = [{"type": "session.start", "data": {"sessionId": "c1"}}]
    jsonl(tmp_path / "a" / "c1" / "events.jsonl", events)
    jsonl(tmp_path / "b" / "c1" / "events.jsonl", events)
    (tmp_path / "a" / "empty-1").mkdir()
    (tmp_path / "b" / "empty-1").mkdir()
    receipt = run_inventory(tmp_path, copilot_state_dirs=[tmp_path / "a", tmp_path / "b"])
    assert receipt["families"]["copilot"]["folded_duplicates"] == 1
    assert len(observed(tmp_path, "copilot")) == 3  # c1 once, empty-1 in each root


def test_a_repeated_declaration_counts_once(tmp_path):
    projects = tmp_path / "claude"
    jsonl(projects / "p" / "s.jsonl", [claude_event("s", isSidechain=False)])
    receipt = run_inventory(tmp_path, claude_projects=[projects, projects])
    assert receipt["families"]["claude"]["folded_duplicates"] == 0
    assert len(observed(tmp_path, "claude")) == 1


# ---------------------------------------------------------------------------------------------
# The reader consumes what the producer writes
# ---------------------------------------------------------------------------------------------


def test_claude_inventory_feeds_iter_estate_a_full_body(tmp_path):
    projects = tmp_path / "claude"
    jsonl(
        projects / "p" / "s1.jsonl",
        [
            claude_event("s1", "question", isSidechain=False),
            {
                **claude_event("s1", "answer", isSidechain=False),
                "message": {"role": "assistant", "content": [{"type": "text", "text": "answer"}]},
            },
        ],
    )
    jsonl(
        projects / "p" / "agent-a1.jsonl",
        [claude_event("s1", "child work", agentId="a1", isSidechain=True)],
    )
    run_inventory(tmp_path, claude_projects=[projects])
    report: dict[str, Any] = {}
    records = {
        r.source_id: r for r in iter_estate(tmp_path / "out" / "session-corpus.db", report=report)
    }
    assert set(records) == {"s1", "s1/agent/a1"}
    root, child = records["s1"], records["s1/agent/a1"]
    assert root.extra["body_status"] == "full"
    assert [m.text for m in root.messages] == ["question", "answer"]
    assert (child.extra["body_status"], len(child.messages)) == ("full", 1)
    assert child.extra["parent_session_id"] == "s1"
    assert child.extra["session_kind"] == "subagent"
    assert report["full"] == 2


def test_codex_inventory_feeds_iter_estate_through_rollout_and_state_database(tmp_path):
    home = tmp_path / "codex"
    rollout = jsonl(
        home / "sessions" / "r.jsonl",
        [
            codex_meta(THREAD),
            codex_message("m1", "ask"),
            codex_message("m2", "reply", "assistant"),
        ],
    )
    state_database(home, [], [(THREAD, str(rollout), "cli", "/d", "T", "main", "abc", 1)])
    run_inventory(tmp_path, codex_roots=[home])
    (record,) = list(iter_estate(tmp_path / "out" / "session-corpus.db"))
    assert record.provider == "codex"
    assert record.source_id == THREAD
    assert [m.text for m in record.messages] == ["ask", "reply"]
    assert record.extra["body_status"] == "full"
    assert record.extra["session_kind"] == "root"


@pytest.mark.skipif(ZSTD is None, reason="zstd is not installed")
def test_compressed_codex_rollout_reads_as_a_full_body(tmp_path):
    home = tmp_path / "codex"
    zst(
        home / "archived_sessions" / f"rollout-{THREAD}.jsonl.zst",
        [codex_meta(THREAD), codex_message("m", "from a compressed rollout")],
    )
    run_inventory(tmp_path, codex_roots=[home])
    (record,) = list(iter_estate(tmp_path / "out" / "session-corpus.db"))
    assert [m.text for m in record.messages] == ["from a compressed rollout"]
    assert record.extra["body_status"] == "full"


def test_other_families_are_readable_by_the_reader(tmp_path):
    state = tmp_path / "copilot-state"
    jsonl(
        state / "c1" / "events.jsonl",
        [
            {"type": "session.start", "data": {"sessionId": "c1"}},
            {"type": "user.message", "id": "u", "data": {"content": "hi"}},
        ],
    )
    source = database(
        tmp_path / "opencode.db",
        "CREATE TABLE session (id TEXT, title TEXT, time_created INT, time_updated INT,"
        " directory TEXT);"
        "CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT, seq INT,"
        " time_created INT, data TEXT);",
        ("INSERT INTO session VALUES ('o1','T',1,1,'/d')", ()),
        (
            "INSERT INTO session_message VALUES ('m','o1','user',1,1,?)",
            (json.dumps({"content": "opencode body"}),),
        ),
    )
    run_inventory(tmp_path, copilot_state_dirs=[state], opencode_databases=[source])
    records = {r.source_id: r for r in iter_estate(tmp_path / "out" / "session-corpus.db")}
    assert [m.text for m in records["c1"].messages] == ["hi"]
    assert [m.text for m in records["o1"].messages] == ["opencode body"]
    assert records["o1"].extra["body_status"] == "full"


# ---------------------------------------------------------------------------------------------
# Safety, output and the command
# ---------------------------------------------------------------------------------------------


def snapshot(root: Path) -> dict[str, tuple[int, int, bytes]]:
    return {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns, p.read_bytes())
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_sources_are_never_written_and_leave_no_sidecars(tmp_path):
    root = tmp_path / "sources"
    jsonl(root / "claude" / "p" / "s.jsonl", [claude_event("s", isSidechain=False)])
    jsonl(root / "codex" / "sessions" / "r.jsonl", [codex_meta(THREAD)])
    state_database(
        root / "codex", [(PARENT, THREAD)], [(THREAD, "", "cli", None, None, None, None, 1)]
    )
    database(
        root / "opencode.db",
        "CREATE TABLE session (id TEXT, title TEXT, time_created INT, time_updated INT,"
        " directory TEXT);",
        ("INSERT INTO session VALUES ('o','T',1,1,'/d')", ()),
    )
    wal = root / "wal.db"
    with sqlite3.connect(wal) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE conversation_summaries (conversation_id TEXT)")
        connection.execute("INSERT INTO conversation_summaries VALUES ('w')")
    before = snapshot(root)
    run_inventory(
        tmp_path,
        claude_projects=[root / "claude"],
        codex_roots=[root / "codex"],
        opencode_databases=[root / "opencode.db"],
        antigravity_sources=[wal],
    )
    # Not a byte of any source file, the WAL database's sidecars included, may change.
    assert snapshot(root) == before
    assert set(kinds(tmp_path, "antigravity")) == {"w"}


def test_symlinks_that_leave_the_declared_root_are_not_followed(tmp_path):
    outside = jsonl(tmp_path / "outside" / "s.jsonl", [claude_event("outside", isSidechain=False)])
    inside = jsonl(
        tmp_path / "claude" / "p" / "real.jsonl", [claude_event("inside", isSidechain=False)]
    )
    (tmp_path / "claude" / "p" / "escape.jsonl").symlink_to(outside)
    (tmp_path / "claude" / "p" / "alias.jsonl").symlink_to(inside)
    (tmp_path / "claude" / "linked-dir").symlink_to(outside.parent)
    home = tmp_path / "codex"
    jsonl(tmp_path / "elsewhere" / "sessions" / "r.jsonl", [codex_meta("elsewhere")])
    home.mkdir()
    (home / "sessions").symlink_to(tmp_path / "elsewhere" / "sessions")
    run_inventory(tmp_path, claude_projects=[tmp_path / "claude"], codex_roots=[home])
    assert set(kinds(tmp_path, "claude")) == {"inside"}
    assert len(observed(tmp_path, "claude")) == 1
    assert {"symlink_escape", "directory_symlink_not_followed"} <= findings(tmp_path, "claude")
    assert kinds(tmp_path, "codex") == {}
    assert "symlink_escape" in findings(tmp_path, "codex")


def test_unreadable_or_malformed_sources_are_findings_not_crashes(tmp_path):
    broken_db = tmp_path / "broken.db"
    broken_db.write_bytes(b"this is not a sqlite database" * 100)
    wrong_schema = database(tmp_path / "other.db", "CREATE TABLE unrelated (x INT);")
    bad_census = tmp_path / "census.json"
    bad_census.write_text("{not json")
    list_census = tmp_path / "list.json"
    list_census.write_text("[1, 2]")
    valid = database(
        tmp_path / "good.db",
        "CREATE TABLE session (id TEXT, title TEXT, time_created INT, time_updated INT,"
        " directory TEXT);",
        ("INSERT INTO session VALUES ('good','T',1,1,'/d')", ()),
    )
    receipt = run_inventory(
        tmp_path,
        opencode_databases=[broken_db, wrong_schema, tmp_path / "missing.db", valid],
        gemini_sources=[bad_census, list_census],
        antigravity_sources=[tmp_path / "missing-dir"],
        codex_roots=[tmp_path / "no-codex"],
        claude_projects=[tmp_path / "no-claude"],
    )
    assert set(kinds(tmp_path, "opencode")) == {"good"}
    assert {"source_database_error", "unsupported_opencode_schema", "source_missing"} <= findings(
        tmp_path, "opencode"
    )
    assert {"source_malformed", "unsupported_census"} <= findings(tmp_path, "gemini")
    assert receipt["status"] == "complete"
    assert receipt["findings_by_code"]["source_missing"] >= 4


def test_a_failed_source_leaves_none_of_its_partial_rows(tmp_path):
    source = database(
        tmp_path / "opencode.db",
        "CREATE TABLE session (id TEXT, title TEXT, time_created INT, time_updated INT,"
        " directory TEXT);"
        "CREATE TABLE session_v2 (id TEXT);",
        ("INSERT INTO session VALUES ('o1','T',1,1,'/d')", ()),
    )
    run_inventory(tmp_path, opencode_databases=[source])
    assert rows(tmp_path) == {}
    assert findings(tmp_path, "opencode") == {"unsupported_opencode_schema"}


def test_output_directory_must_not_exist_and_is_private(tmp_path):
    projects = tmp_path / "claude"
    jsonl(projects / "p" / "s.jsonl", [claude_event("s", isSidechain=False)])
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(FileExistsError):
        build_inventory(InventorySources(claude_projects=[projects]), out)
    assert list(out.iterdir()) == []
    fresh = tmp_path / "fresh"
    build_inventory(InventorySources(claude_projects=[projects]), fresh)
    assert fresh.stat().st_mode & 0o777 == 0o700
    assert (fresh / "session-corpus.db").stat().st_mode & 0o777 == 0o600
    assert (fresh / "inventory-receipt.json").stat().st_mode & 0o777 == 0o600
    assert sorted(p.name for p in fresh.iterdir()) == [
        "inventory-receipt.json",
        "session-corpus.db",
    ]


def test_output_may_not_be_inside_a_source_and_a_source_is_required(tmp_path):
    projects = tmp_path / "claude"
    projects.mkdir()
    with pytest.raises(ValueError, match="inside a declared source"):
        build_inventory(InventorySources(claude_projects=[projects]), projects / "out")
    assert not (projects / "out").exists()
    with pytest.raises(ValueError, match="at least one source"):
        build_inventory(InventorySources(), tmp_path / "none")
    with pytest.raises(ValueError, match="positive"):
        build_inventory(InventorySources(claude_projects=[projects]), tmp_path / "x", max_seconds=0)


def test_output_schema_is_what_the_reader_expects(tmp_path):
    projects = tmp_path / "claude"
    jsonl(projects / "p" / "s.jsonl", [claude_event("s", isSidechain=False)])
    run_inventory(tmp_path, claude_projects=[projects])
    with sqlite3.connect(tmp_path / "out" / "session-corpus.db") as connection:
        columns = {
            table: [(r[1], r[5]) for r in connection.execute(f"PRAGMA table_info({table})")]
            for table in ("sessions", "source_observations")
        }
        tables = {
            r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert [name for name, _ in columns["sessions"]] == [
        "session_id", "agent_family", "session_kind", "parent_session_id", "started_at",
        "ended_at", "project_root", "primary_branch", "head_commit", "prompt_title",
        "prompt_snippet", "status", "irf_owner", "custody_path",
    ]  # fmt: skip
    assert {n for n, pk in columns["sessions"] if pk} == {"session_id", "agent_family"}
    assert [name for name, _ in columns["source_observations"]] == [
        "agent_family", "source_path", "object_key", "native_id",
    ]  # fmt: skip
    assert {n for n, pk in columns["source_observations"] if pk} == {
        "agent_family", "source_path", "object_key",
    }  # fmt: skip
    assert {"source_custody_findings", "source_artifact_observations"} <= tables
    assert journal == "delete"


def test_receipt_is_counts_and_paths_only_and_binds_the_database(tmp_path):
    projects = tmp_path / "claude"
    jsonl(projects / "p" / "s.jsonl", [claude_event("s", SENTINEL, isSidechain=False)])
    receipt = run_inventory(tmp_path, claude_projects=[projects])
    text = (tmp_path / "out" / "inventory-receipt.json").read_text()
    assert json.loads(text) == receipt
    assert (
        SENTINEL not in text
        and SENTINEL.encode() not in (tmp_path / "out" / "session-corpus.db").read_bytes()
    )
    assert receipt["schema"] == "cce.library-inventory-receipt"
    assert receipt["schema_version"] == 1
    assert receipt["implementation"] == library_inventory.IMPLEMENTATION
    assert receipt["reader"].startswith("library-native@")
    assert receipt["status"] == "complete"
    assert receipt["declared_roots"]["claude_projects"] == [str(projects.absolute())]
    assert receipt["families"]["claude"]["sessions_by_kind"] == {"root": 1}
    assert receipt["totals"]["sessions"] == 1
    assert receipt["started_at"] <= receipt["finished_at"]
    database_bytes = (tmp_path / "out" / "session-corpus.db").read_bytes()
    assert receipt["database"]["sha256"] == hashlib.sha256(database_bytes).hexdigest()
    assert receipt["database"]["bytes"] == len(database_bytes)
    assert receipt["database"]["integrity"] == ["ok"]


def test_time_budget_stops_the_run_and_marks_the_receipt_incomplete(tmp_path):
    projects = tmp_path / "claude"
    jsonl(projects / "p" / "s.jsonl", [claude_event("s", isSidechain=False)])
    receipt = build_inventory(
        InventorySources(claude_projects=[projects]), tmp_path / "out", max_seconds=1e-9
    )
    assert receipt["status"] == "incomplete"
    assert (tmp_path / "out" / "inventory-receipt.json").is_file()


# ---------------------------------------------------------------------------------------------
# Review round 1: folding scope, containment, sidecars, decoding, budgets
# ---------------------------------------------------------------------------------------------


def artifacts(tmp_path: Path, family: str, name: str = "out") -> dict[str, int]:
    with sqlite3.connect(tmp_path / name / "session-corpus.db") as connection:
        return dict(
            connection.execute(
                "SELECT classification,count(*) FROM source_artifact_observations "
                "WHERE agent_family=? GROUP BY classification",
                (family,),
            )
        )


def finding_paths(tmp_path: Path, family: str, reason: str, name: str = "out") -> set[str]:
    with sqlite3.connect(tmp_path / name / "session-corpus.db") as connection:
        return {
            r[0]
            for r in connection.execute(
                "SELECT source_path FROM unresolved_source_observations "
                "WHERE agent_family=? AND reason=?",
                (family, reason),
            )
        }


def test_the_last_differing_copy_visited_is_the_custody_copy(tmp_path):
    base = [claude_event("s", isSidechain=False)]
    jsonl(tmp_path / "first" / "p" / "s.jsonl", base)
    second = jsonl(
        tmp_path / "second" / "p" / "s.jsonl", base + [claude_event("s", "more", isSidechain=False)]
    )
    run_inventory(tmp_path, claude_projects=[tmp_path / "first", tmp_path / "second"])
    row = rows(tmp_path)[("claude", "s")]
    assert row["custody_path"] == str(second.resolve())
    assert row["started_at"] == "2026-01-01T00:00:00Z"


def test_symlink_findings_name_the_symlink_not_its_target(tmp_path):
    target = tmp_path / "shared" / "memory"
    target.mkdir(parents=True)
    projects = tmp_path / "claude"
    (projects / "a").mkdir(parents=True)
    (projects / "b").mkdir(parents=True)
    (projects / "a" / "memory").symlink_to(target)
    (projects / "b" / "memory").symlink_to(target)
    outside = jsonl(tmp_path / "outside" / "s.jsonl", [claude_event("x", isSidechain=False)])
    (projects / "a" / "one.jsonl").symlink_to(outside)
    (projects / "b" / "two.jsonl").symlink_to(outside)
    receipt = run_inventory(tmp_path, claude_projects=[projects])
    assert receipt["families"]["claude"]["findings_by_code"] == {
        "directory_symlink_not_followed": 2,
        "symlink_escape": 2,
    }
    assert finding_paths(tmp_path, "claude", "directory_symlink_not_followed") == {
        str(projects.resolve() / "a" / "memory"),
        str(projects.resolve() / "b" / "memory"),
    }


def test_copilot_directories_with_different_names_never_fold(tmp_path):
    for name in ("alpha", "beta"):
        jsonl(tmp_path / "state" / name / "events.jsonl", [{"type": "user.message", "data": {}}])
    for name in ("empty-a", "empty-b"):
        (tmp_path / "state" / name).mkdir()
        (tmp_path / "state" / name / "events.jsonl").write_text("")
    receipt = run_inventory(tmp_path, copilot_state_dirs=[tmp_path / "state"])
    assert set(kinds(tmp_path, "copilot")) == {"alpha", "beta", "empty-a", "empty-b"}
    assert receipt["families"]["copilot"]["folded_duplicates"] == 0


def test_copilot_event_log_symlink_leaving_the_root_is_not_followed(tmp_path):
    outside = jsonl(
        tmp_path / "outside" / "events.jsonl",
        [
            {
                "type": "session.start",
                "data": {"sessionId": "linked", "context": {"cwd": "/must/not/be/read"}},
            }
        ],
    )
    (tmp_path / "state" / "linked").mkdir(parents=True)
    (tmp_path / "state" / "linked" / "events.jsonl").symlink_to(outside)
    run_inventory(tmp_path, copilot_state_dirs=[tmp_path / "state"])
    assert "symlink_escape" in findings(tmp_path, "copilot")
    assert set(kinds(tmp_path, "copilot")) == {"linked"}
    assert rows(tmp_path)[("copilot", "linked")]["project_root"] is None


def test_empty_claude_transcripts_are_each_accounted_for(tmp_path):
    for name in ("a.jsonl", "b.jsonl", "c.jsonl"):
        (tmp_path / "claude" / "p").mkdir(parents=True, exist_ok=True)
        (tmp_path / "claude" / "p" / name).write_text("")
    receipt = run_inventory(tmp_path, claude_projects=[tmp_path / "claude"])
    assert artifacts(tmp_path, "claude") == {"empty_transcript": 3}
    assert receipt["families"]["claude"]["folded_duplicates"] == 0


def test_census_row_naming_a_folded_copy_is_still_accounted_for(tmp_path):
    rollout = [codex_meta(THREAD), codex_message("m", "same")]
    jsonl(tmp_path / "live" / "sessions" / "r.jsonl", rollout)
    jsonl(tmp_path / "old" / "sessions" / "nested" / "r.jsonl", rollout)
    census = tmp_path / "old" / "census.json"
    census.write_text(json.dumps({"sessions": [{"relative_path": "sessions/nested/r.jsonl"}]}))
    receipt = run_inventory(
        tmp_path, codex_roots=[tmp_path / "live", tmp_path / "old"], codex_censuses=[census]
    )
    assert receipt["families"]["codex"]["folded_duplicates"] == 1
    assert (str(census.resolve()), "sessions/0", THREAD) in observed(tmp_path, "codex")
    assert artifacts(tmp_path, "codex") == {}


def wal_pair(directory: Path, keep: str) -> Path:
    """A WAL-mode database copied beside only one of its sidecars, as a backup might leave it."""
    directory.mkdir(parents=True)
    live = directory.parent / f"{directory.name}-live" / "conversation_summaries.db"
    live.parent.mkdir()
    connection = sqlite3.connect(live)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE conversation_summaries (conversation_id TEXT)")
        connection.execute("INSERT INTO conversation_summaries VALUES ('w1')")
        connection.commit()
        if keep == "shm":
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        target = directory / live.name
        shutil.copyfile(live, target)
        shutil.copyfile(str(live) + "-" + keep, str(target) + "-" + keep)
    finally:
        connection.close()
    return target


@pytest.mark.parametrize("keep", ["wal", "shm"])
def test_a_wal_database_with_one_sidecar_never_gains_the_other(tmp_path, keep):
    source = wal_pair(tmp_path / "backup", keep)
    before = sorted(p.name for p in source.parent.iterdir())
    assert len(before) == 2
    run_inventory(tmp_path, antigravity_sources=[source])
    assert sorted(p.name for p in source.parent.iterdir()) == before
    if keep == "wal":
        # The log holds the only copy of the data; reading the base alone would be wrong.
        assert findings(tmp_path, "antigravity") == {"database_requires_its_journal"}
        assert rows(tmp_path) == {}
    else:
        assert set(kinds(tmp_path, "antigravity")) == {"w1"}


@pytest.mark.skipif(ZSTD is None, reason="zstd is not installed")
def test_a_truncated_zstd_stream_is_a_decode_failure(tmp_path):
    lines = [codex_meta(THREAD)] + [codex_message(f"m{i}", "x" * 4000) for i in range(300)]
    path = zst(tmp_path / "rollout.jsonl.zst", lines)
    run = library_inventory._Run(sqlite3.connect(":memory:"), 60)
    with library_inventory._text_stream(run, path) as stream:
        assert len(stream.read().splitlines()) == len(lines)
    data = path.read_bytes()
    path.write_bytes(data[: len(data) * 3 // 4])
    for _ in range(5):  # the race the check closes is timing-dependent, so repeat it
        with (
            pytest.raises(library_inventory._Problem) as raised,
            library_inventory._text_stream(run, path) as stream,
        ):
            stream.read()
        assert raised.value.code == "source_decode_failed"
    # Abandoning the stream early, as the Codex header probe does, is not a failure.
    events = library_inventory._jsonl_events(run, "codex", path)
    assert next(events)[0] == 1
    events.close()


def test_unreadable_files_and_folders_are_findings(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root reads everything")
    projects = tmp_path / "claude"
    jsonl(projects / "p" / "ok.jsonl", [claude_event("ok", isSidechain=False)])
    locked_file = jsonl(projects / "p" / "locked.jsonl", [claude_event("locked")])
    locked_dir = projects / "closed"
    jsonl(locked_dir / "s.jsonl", [claude_event("hidden")])
    locked_file.chmod(0)
    locked_dir.chmod(0)
    try:
        receipt = run_inventory(tmp_path, claude_projects=[projects])
    finally:
        locked_file.chmod(0o600)
        locked_dir.chmod(0o700)
    assert set(kinds(tmp_path, "claude")) == {"ok"}
    assert findings(tmp_path, "claude") == {"source_unreadable"}
    assert finding_paths(tmp_path, "claude", "source_unreadable") == {
        str(locked_file.resolve()),
        str(locked_dir.resolve()),
    }
    assert receipt["status"] == "complete"


def test_a_database_held_under_an_exclusive_lock_is_a_finding(tmp_path):
    locked = database(
        tmp_path / "locked.db",
        "CREATE TABLE session (id TEXT, title TEXT, time_created INT, time_updated INT,"
        " directory TEXT);",
        ("INSERT INTO session VALUES ('o1','T',1,1,'/d')", ()),
    )
    free = database(
        tmp_path / "free.db",
        "CREATE TABLE session (id TEXT, title TEXT, time_created INT, time_updated INT,"
        " directory TEXT);",
        ("INSERT INTO session VALUES ('o2','T',1,1,'/d')", ()),
    )
    holder = sqlite3.connect(locked, isolation_level=None)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        receipt = run_inventory(tmp_path, opencode_databases=[locked, free])
    finally:
        holder.close()
    assert set(kinds(tmp_path, "opencode")) == {"o2"}
    assert findings(tmp_path, "opencode") == {"source_database_error"}
    assert receipt["status"] == "complete"


@pytest.mark.parametrize("budget", [float("nan"), float("inf"), -1.0, 0.0])
def test_the_time_budget_must_be_a_positive_finite_number(tmp_path, budget):
    projects = tmp_path / "claude"
    jsonl(projects / "p" / "s.jsonl", [claude_event("s", isSidechain=False)])
    with pytest.raises(ValueError, match="positive"):
        build_inventory(
            InventorySources(claude_projects=[projects]), tmp_path / "x", max_seconds=budget
        )


@pytest.mark.skipif(ZSTD is None, reason="zstd is not installed")
def test_a_very_large_time_budget_still_decodes(tmp_path):
    home = tmp_path / "codex"
    zst(home / "sessions" / f"rollout-{THREAD}.jsonl.zst", [codex_meta(THREAD)])
    receipt = build_inventory(
        InventorySources(codex_roots=[home]), tmp_path / "out", max_seconds=1e12
    )
    assert receipt["status"] == "complete"
    assert kinds(tmp_path, "codex") == {THREAD: ("root", None)}


# ---------------------------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------------------------


def command(*arguments: str) -> int:
    return handle_library(build_parser().parse_args(["library", "inventory", *arguments]))


def test_command_prints_the_receipt_and_accepts_repeated_roots(tmp_path, capsys):
    jsonl(tmp_path / "a" / "p" / "s.jsonl", [claude_event("s", isSidechain=False)])
    jsonl(tmp_path / "b" / "p" / "s.jsonl", [claude_event("s", isSidechain=False)])
    home = tmp_path / "codex"
    jsonl(home / "sessions" / "r.jsonl", [codex_meta(THREAD)])
    code = command(
        "--claude-projects", str(tmp_path / "a"),
        "--claude-projects", str(tmp_path / "b"),
        "--codex-root", str(home),
        "--out", str(tmp_path / "out"),
        "--json",
    )  # fmt: skip
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["families"]["claude"]["folded_duplicates"] == 1
    assert printed["families"]["codex"]["sessions"] == 1
    assert printed == json.loads((tmp_path / "out" / "inventory-receipt.json").read_text())


def test_command_refuses_an_existing_output_and_a_missing_source_declaration(tmp_path, capsys):
    (tmp_path / "out").mkdir()
    assert command("--claude-projects", str(tmp_path), "--out", str(tmp_path / "out")) == 2
    assert "already exists" in capsys.readouterr().err
    assert command("--out", str(tmp_path / "fresh")) == 2
    assert "at least one source" in capsys.readouterr().err
    assert not (tmp_path / "fresh").exists()


def test_command_exits_three_when_the_budget_runs_out(tmp_path, capsys):
    jsonl(tmp_path / "a" / "p" / "s.jsonl", [claude_event("s", isSidechain=False)])
    code = command(
        "--claude-projects", str(tmp_path / "a"),
        "--out", str(tmp_path / "out"),
        "--max-seconds", "0.000000001",
    )  # fmt: skip
    assert code == 3
    assert json.loads(capsys.readouterr().out)["status"] == "incomplete"


def test_command_has_no_default_source_locations():
    args = build_parser().parse_args(["library", "inventory", "--out", "somewhere"])
    for name in (
        "claude_projects", "codex_root", "codex_census", "gemini_source", "antigravity_source",
        "copilot_state_dir", "copilot_database", "opencode_database",
    ):  # fmt: skip
        assert getattr(args, name) == []


# ---------------------------------------------------------------------------------------------
# Review findings: OpenCode identity merge, WAL sources that must stay byte-identical, Copilot
# directories that fold only when every transcript the reader can open is the same
# ---------------------------------------------------------------------------------------------

OPENCODE_WITH_PARENT = (
    "CREATE TABLE session (id TEXT, title TEXT, time_created INT, time_updated INT,"
    " directory TEXT, parent_id TEXT);"
)
OPENCODE_WITHOUT_PARENT = (
    "CREATE TABLE session (id TEXT, title TEXT, time_created INT, time_updated INT,"
    " directory TEXT);"
)


def opencode_copy(path: Path, title: str, parent: str | None | bool = False) -> Path:
    """An OpenCode database holding session ``child``; ``parent=False`` omits the column."""
    if parent is False:
        return database(
            path,
            OPENCODE_WITHOUT_PARENT,
            ("INSERT INTO session VALUES ('child',?,1,1,'/d')", (title,)),
        )
    return database(
        path,
        OPENCODE_WITH_PARENT,
        ("INSERT INTO session VALUES ('child',?,1,1,'/d',?)", (title, parent)),
    )


def test_opencode_copy_without_parent_evidence_cannot_erase_a_proven_subagent(tmp_path):
    proven = opencode_copy(tmp_path / "a" / "o.db", "proven", "p1")
    bare = opencode_copy(tmp_path / "b" / "o.db", "bare")
    run_inventory(tmp_path, opencode_databases=[proven, bare])
    assert kinds(tmp_path, "opencode") == {"child": ("subagent", "p1")}
    assert findings(tmp_path, "opencode") == set()


def test_opencode_session_v2_without_parent_does_not_erase_the_session_table_parent(tmp_path):
    source = database(
        tmp_path / "o.db",
        OPENCODE_WITH_PARENT + "CREATE TABLE session_v2 (id TEXT, title TEXT, time_created INT,"
        " time_updated INT, directory TEXT);",
        ("INSERT INTO session VALUES ('child','T',1,1,'/d','p1')", ()),
        ("INSERT INTO session_v2 VALUES ('child','T2',2,2,'/d')", ()),
    )
    run_inventory(tmp_path, opencode_databases=[source])
    assert kinds(tmp_path, "opencode") == {"child": ("subagent", "p1")}


def test_opencode_later_copy_with_parent_evidence_upgrades_an_unknown_kind(tmp_path):
    bare = opencode_copy(tmp_path / "a" / "o.db", "bare")
    proven = opencode_copy(tmp_path / "b" / "o.db", "proven", "p1")
    run_inventory(tmp_path, opencode_databases=[bare, proven])
    assert kinds(tmp_path, "opencode") == {"child": ("subagent", "p1")}


@pytest.mark.parametrize("second", ["p2", None])
def test_opencode_contradictory_parent_claims_are_a_finding_not_an_overwrite(tmp_path, second):
    first = opencode_copy(tmp_path / "a" / "o.db", "first", "p1")
    other = opencode_copy(tmp_path / "b" / "o.db", "second", second)
    run_inventory(tmp_path, opencode_databases=[first, other])
    assert kinds(tmp_path, "opencode") == {"child": ("unknown", None)}
    assert "conflicting_parent_identity" in findings(tmp_path, "opencode")
    assert finding_paths(tmp_path, "opencode", "conflicting_parent_identity") == {
        str(other.resolve())
    }


def test_opencode_agreeing_copies_keep_the_relationship_without_a_finding(tmp_path):
    first = opencode_copy(tmp_path / "a" / "o.db", "first", "p1")
    second = opencode_copy(tmp_path / "b" / "o.db", "second", "p1")
    run_inventory(tmp_path, opencode_databases=[first, second])
    assert kinds(tmp_path, "opencode") == {"child": ("subagent", "p1")}
    assert findings(tmp_path, "opencode") == set()
    assert rows(tmp_path)[("opencode", "child")]["prompt_title"] == "second"


WAL_SESSION = (
    "CREATE TABLE session (id TEXT, title TEXT, time_created INT, time_updated INT,"
    " directory TEXT);"
)


def live_wal_copy(tmp_path: Path) -> Path:
    """A copied WAL-mode database with writable -wal and -shm; one row lives only in the log."""
    live = tmp_path / "live" / "o.db"
    live.parent.mkdir()
    holder = sqlite3.connect(live)
    try:
        holder.execute("PRAGMA journal_mode=WAL")
        holder.execute("PRAGMA wal_autocheckpoint=0")
        holder.execute(WAL_SESSION.rstrip(";"))
        holder.execute("INSERT INTO session VALUES ('base','T',1,1,'/d')")
        holder.commit()
        holder.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        holder.execute("INSERT INTO session VALUES ('pending','T',1,1,'/d')")
        holder.commit()
        target = tmp_path / "backup" / "o.db"
        target.parent.mkdir()
        for suffix in ("", "-wal", "-shm"):
            shutil.copyfile(str(live) + suffix, str(target) + suffix)
    finally:
        holder.close()
    for suffix in ("-wal", "-shm"):
        assert Path(str(target) + suffix).stat().st_size > 0
    return target


def test_wal_source_with_both_sidecars_is_read_without_changing_any_byte(tmp_path):
    source = live_wal_copy(tmp_path)
    before = snapshot(source.parent)
    assert set(before) == {"o.db", "o.db-wal", "o.db-shm"}
    receipt = run_inventory(tmp_path, opencode_databases=[source])
    assert snapshot(source.parent) == before
    # The pending row exists only in the log, so the log must have been read.
    assert set(kinds(tmp_path, "opencode")) == {"base", "pending"}
    assert findings(tmp_path, "opencode") == set()
    assert receipt["status"] == "complete"


def test_wal_snapshot_lives_in_a_private_directory_that_is_removed_afterwards(
    tmp_path, monkeypatch
):
    source = live_wal_copy(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(library_inventory.tempfile, "tempdir", str(scratch))
    opened: list[str] = []
    original = library_inventory._connect_readonly

    def recording(run, uri):
        opened.append(uri)
        return original(run, uri)

    monkeypatch.setattr(library_inventory, "_connect_readonly", recording)
    run_inventory(tmp_path, opencode_databases=[source])
    assert set(kinds(tmp_path, "opencode")) == {"base", "pending"}
    assert len(opened) == 1
    assert opened[0].startswith(scratch.as_uri())
    assert source.parent.as_uri() not in opened[0]
    assert list(scratch.iterdir()) == []


def test_wal_snapshot_is_never_placed_inside_a_declared_source(tmp_path, monkeypatch):
    source = live_wal_copy(tmp_path)
    monkeypatch.setattr(library_inventory.tempfile, "tempdir", str(source.parent))
    before = snapshot(source.parent)
    run_inventory(tmp_path, opencode_databases=[source])
    assert snapshot(source.parent) == before
    assert rows(tmp_path) == {}
    assert findings(tmp_path, "opencode") == {"snapshot_location_unsafe"}


def test_a_wal_source_too_large_to_snapshot_is_a_finding_not_an_unbounded_copy(
    tmp_path, monkeypatch
):
    source = live_wal_copy(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(library_inventory.tempfile, "tempdir", str(scratch))
    monkeypatch.setattr(library_inventory, "SNAPSHOT_MAX_BYTES", 1024)
    before = snapshot(source.parent)
    receipt = run_inventory(tmp_path, opencode_databases=[source])
    assert snapshot(source.parent) == before
    assert list(scratch.iterdir()) == []
    assert rows(tmp_path) == {}
    assert findings(tmp_path, "opencode") == {"database_too_large_to_snapshot"}
    assert receipt["status"] == "complete"


def test_a_wal_source_that_keeps_changing_while_copied_is_a_finding(tmp_path, monkeypatch):
    source = live_wal_copy(tmp_path)
    original = library_inventory._copy_hashed

    def racing(run, origin, destination):
        digest = original(run, origin, destination)
        if str(origin).endswith("-wal"):
            with origin.open("ab") as handle:
                handle.write(b"\0")
        return digest

    monkeypatch.setattr(library_inventory, "_copy_hashed", racing)
    run_inventory(tmp_path, opencode_databases=[source])
    assert rows(tmp_path) == {}
    assert findings(tmp_path, "opencode") == {"database_changed_during_snapshot"}


def test_a_wal_source_that_settles_after_one_attempt_is_read(tmp_path, monkeypatch):
    source = live_wal_copy(tmp_path)
    original = library_inventory._copy_hashed
    attempts = {"count": 0}

    def racing_once(run, origin, destination):
        digest = original(run, origin, destination)
        if str(origin).endswith("-wal"):
            attempts["count"] += 1
            if attempts["count"] == 1:
                os.utime(origin, ns=(1, 1))
        return digest

    monkeypatch.setattr(library_inventory, "_copy_hashed", racing_once)
    run_inventory(tmp_path, opencode_databases=[source])
    assert attempts["count"] == 2
    assert set(kinds(tmp_path, "opencode")) == {"base", "pending"}
    assert findings(tmp_path, "opencode") == set()


def copilot_copy(root: Path, name: str, **files: str) -> Path:
    """A Copilot session-state directory whose transcript files hold the given text."""
    directory = root / name
    directory.mkdir(parents=True)
    for file_name, text in files.items():
        (directory / file_name.replace("__", ".")).write_text(text, encoding="utf-8")
    return directory


START = json.dumps({"type": "session.start", "data": {"sessionId": "c1"}}) + "\n"


def test_copilot_directories_with_one_event_log_but_different_messages_do_not_fold(tmp_path):
    copilot_copy(tmp_path / "a", "c1", events__jsonl=START, messages__jsonl="first body\n")
    copilot_copy(tmp_path / "b", "c1", events__jsonl=START, messages__jsonl="second body\n")
    receipt = run_inventory(tmp_path, copilot_state_dirs=[tmp_path / "a", tmp_path / "b"])
    assert receipt["families"]["copilot"]["folded_duplicates"] == 0
    assert {o[0] for o in observed(tmp_path, "copilot")} == {
        str((tmp_path / "a" / "c1").resolve()),
        str((tmp_path / "b" / "c1").resolve()),
    }


@pytest.mark.parametrize(
    "name", ["session.jsonl", "messages.jsonl", "conversation.json", "messages.json"]
)
def test_every_reader_supported_transcript_name_takes_part_in_the_fingerprint(tmp_path, name):
    key = name.replace(".", "__")
    copilot_copy(tmp_path / "a", "c1", events__jsonl=START, **{key: "one\n"})
    copilot_copy(tmp_path / "b", "c1", events__jsonl=START, **{key: "two\n"})
    receipt = run_inventory(tmp_path, copilot_state_dirs=[tmp_path / "a", tmp_path / "b"])
    assert receipt["families"]["copilot"]["folded_duplicates"] == 0
    assert len(observed(tmp_path, "copilot")) == 2


def test_copilot_directories_that_hold_the_same_text_under_different_names_do_not_fold(tmp_path):
    copilot_copy(tmp_path / "a", "c1", events__jsonl=START, messages__jsonl="body\n")
    copilot_copy(tmp_path / "b", "c1", events__jsonl=START, messages__json="body\n")
    receipt = run_inventory(tmp_path, copilot_state_dirs=[tmp_path / "a", tmp_path / "b"])
    assert receipt["families"]["copilot"]["folded_duplicates"] == 0
    assert len(observed(tmp_path, "copilot")) == 2


def test_copilot_directory_with_an_extra_empty_transcript_is_not_the_same_directory(tmp_path):
    copilot_copy(tmp_path / "a", "c1", events__jsonl=START)
    copilot_copy(tmp_path / "b", "c1", events__jsonl=START, messages__jsonl="")
    receipt = run_inventory(tmp_path, copilot_state_dirs=[tmp_path / "a", tmp_path / "b"])
    assert receipt["families"]["copilot"]["folded_duplicates"] == 0
    assert len(observed(tmp_path, "copilot")) == 2


def test_copilot_directories_fold_when_every_transcript_file_matches(tmp_path):
    files = {"events__jsonl": START, "messages__jsonl": "body\n", "conversation__json": "{}\n"}
    kept = copilot_copy(tmp_path / "a", "c1", **files)
    copilot_copy(tmp_path / "b", "c1", **files)
    receipt = run_inventory(tmp_path, copilot_state_dirs=[tmp_path / "a", tmp_path / "b"])
    assert receipt["families"]["copilot"]["folded_duplicates"] == 1
    assert [o[0] for o in observed(tmp_path, "copilot")] == [str(kept.resolve())]


def test_copilot_directory_with_an_escaping_transcript_symlink_is_never_folded(tmp_path):
    outside = tmp_path / "outside" / "messages.jsonl"
    outside.parent.mkdir()
    outside.write_text("secret\n")
    for side in ("a", "b"):
        directory = copilot_copy(tmp_path / side, "c1", events__jsonl=START)
        (directory / "messages.jsonl").symlink_to(outside)
    receipt = run_inventory(tmp_path, copilot_state_dirs=[tmp_path / "a", tmp_path / "b"])
    assert receipt["families"]["copilot"]["folded_duplicates"] == 0
    assert len(observed(tmp_path, "copilot")) == 2
    assert "symlink_escape" in findings(tmp_path, "copilot")


# ---------------------------------------------------------------------------------------------
# Review round 3: parentless same-kind copies, census bases, census deadlines
# ---------------------------------------------------------------------------------------------


def test_a_parentless_copy_of_the_same_kind_keeps_the_established_parent(tmp_path):
    first = tmp_path / "a" / "census.json"
    second = tmp_path / "b" / "census.json"
    for path, record in (
        (first, {"session_id": "s", "parent_session_id": "p"}),
        (second, {"session_id": "s", "kind": "subagent"}),
    ):
        path.parent.mkdir()
        path.write_text(json.dumps({"sessions": [record]}))
    run_inventory(tmp_path, gemini_sources=[first, second])
    assert kinds(tmp_path, "gemini")["s"] == ("subagent", "p")
    assert "conflicting_parent_identity" not in findings(tmp_path, "gemini")


def test_identical_censuses_with_different_relative_bases_are_not_folded(tmp_path):
    body = json.dumps({"sessions": [{"session_id": "s", "path": "body.json"}]})
    for side in ("a", "b"):
        (tmp_path / side).mkdir()
        (tmp_path / side / "census.json").write_text(body)
    receipt = run_inventory(
        tmp_path, gemini_sources=[tmp_path / "a" / "census.json", tmp_path / "b" / "census.json"]
    )
    assert receipt["families"]["gemini"]["folded_duplicates"] == 0
    assert len(observed(tmp_path, "gemini")) == 2


def test_identical_censuses_sharing_a_base_still_fold(tmp_path):
    body = json.dumps(
        {"root_path": str(tmp_path / "base"), "sessions": [{"session_id": "s", "path": "x"}]}
    )
    for side in ("a", "b"):
        (tmp_path / side).mkdir()
        (tmp_path / side / "census.json").write_text(body)
    receipt = run_inventory(
        tmp_path, gemini_sources=[tmp_path / "a" / "census.json", tmp_path / "b" / "census.json"]
    )
    assert receipt["families"]["gemini"]["folded_duplicates"] == 1


def test_a_large_census_stops_at_the_deadline_and_reports_incomplete(tmp_path):
    census = tmp_path / "census.json"
    census.write_text(json.dumps({"sessions": [{"session_id": f"s{i}"} for i in range(20000)]}))
    receipt = build_inventory(
        InventorySources(gemini_sources=[census]), tmp_path / "out", max_seconds=0.01
    )
    assert receipt["status"] == "incomplete"
    assert len(kinds(tmp_path, "gemini")) < 20000


def agy_census(directory: Path, record_container: dict[str, Any], body: bool) -> Path:
    directory.mkdir(parents=True)
    if body:
        (directory / "body.json").write_text(json.dumps({"role": "user", "content": "agy body"}))
    census = directory / "census.json"
    census.write_text(json.dumps(record_container))
    return census


def test_nested_antigravity_census_references_are_reader_resolvable(tmp_path):
    record = {"uuid": "s", "path": "body.json"}
    top = agy_census(tmp_path / "top", {"sessions": [record]}, body=True)
    nested = agy_census(tmp_path / "nested", {"tm_cli": {"sessions": [record]}}, body=True)
    run_inventory(tmp_path, "top-out", antigravity_sources=[top])
    run_inventory(tmp_path, "nested-out", antigravity_sources=[nested])
    expected = [
        m.text for r in iter_estate(tmp_path / "top-out" / "session-corpus.db") for m in r.messages
    ]
    got = [
        m.text
        for r in iter_estate(tmp_path / "nested-out" / "session-corpus.db")
        for m in r.messages
    ]
    assert expected
    assert got == expected


def test_identical_antigravity_censuses_with_different_bases_are_not_folded(tmp_path):
    record = {"sessions": [{"uuid": "s", "path": "body.json"}]}
    first = agy_census(tmp_path / "a", record, body=False)
    second = agy_census(tmp_path / "b", record, body=True)
    receipt = run_inventory(tmp_path, antigravity_sources=[first, second])
    assert receipt["families"]["antigravity"]["folded_duplicates"] == 0
    messages = [
        m.text for r in iter_estate(tmp_path / "out" / "session-corpus.db") for m in r.messages
    ]
    assert messages == ["agy body"]


def test_opencode_row_processing_stops_at_the_deadline(tmp_path):
    source = database(
        tmp_path / "opencode.db",
        "CREATE TABLE session (id TEXT, title TEXT, time_created INT, time_updated INT,"
        " directory TEXT);",
    )
    with sqlite3.connect(source) as connection:
        connection.executemany(
            "INSERT INTO session VALUES (?,?,1,1,'/d')", [(f"o{i}", "T") for i in range(20000)]
        )
    receipt = build_inventory(
        InventorySources(opencode_databases=[source]), tmp_path / "out", max_seconds=0.01
    )
    assert receipt["status"] == "incomplete"
    assert len(kinds(tmp_path, "opencode")) < 20000


def test_a_deeply_nested_census_is_a_finding_and_later_sources_still_run(tmp_path):
    deep = tmp_path / "deep.json"
    deep.write_text("[" * 200000 + "]" * 200000)
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"sessions": [{"session_id": "g"}]}))
    receipt = run_inventory(tmp_path, gemini_sources=[deep, good])
    assert receipt["status"] == "complete"
    assert "g" in kinds(tmp_path, "gemini")


def test_claude_session_id_alias_is_identity_filtered_by_the_reader(tmp_path):
    projects = tmp_path / "claude"
    jsonl(
        projects / "p" / "t.jsonl",
        [
            {"type": "user", "session_id": "s1", "message": {"role": "user", "content": "one"}},
            {"type": "user", "session_id": "s2", "message": {"role": "user", "content": "two"}},
        ],
    )
    run_inventory(tmp_path, claude_projects=[projects])
    texts = {
        r.source_id: [m.text for m in r.messages]
        for r in iter_estate(tmp_path / "out" / "session-corpus.db")
    }
    assert texts == {"s1": ["one"], "s2": ["two"]}
