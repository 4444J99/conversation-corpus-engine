from __future__ import annotations

import gzip
import json
import sqlite3
from pathlib import Path

from conversation_corpus_engine.library_native import _protobuf_strings, iter_estate


def estate(tmp_path: Path, rows, observations=()):
    path = tmp_path / "estate.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE sessions (agent_family TEXT, session_id TEXT, custody_path TEXT, "
            "parent_session_id TEXT, prompt_title TEXT, prompt_snippet TEXT, started_at TEXT, "
            "ended_at TEXT, project_root TEXT);"
            "CREATE TABLE source_observations (agent_family TEXT, source_path TEXT, "
            "object_key TEXT, native_id TEXT);"
        )
        for family, sid, source in rows:
            connection.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    family,
                    sid,
                    str(source),
                    "parent",
                    "A synthetic session",
                    "metadata preview",
                    "",
                    "",
                    "",
                ),
            )
        connection.executemany("INSERT INTO source_observations VALUES (?,?,?,?)", observations)
    return path


def jsonl(path: Path, rows):
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return path


def codex_message(event_id, text, role="user"):
    return {
        "type": "response_item",
        "timestamp": "2026-01-01T00:00:00Z",
        "payload": {
            "type": "message",
            "id": event_id,
            "role": role,
            "content": [{"type": "input_text", "text": text}],
        },
    }


def test_codex_nested_events_keep_repeated_text_and_tool_roles(tmp_path):
    source = jsonl(
        tmp_path / "rollout.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "s1"}},
            codex_message("u1", "repeat"),
            codex_message("u2", "repeat"),
            codex_message("s", "system context", "developer"),
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call1",
                    "output": "tool result",
                },
            },
        ],
    )
    before = source.read_bytes()
    report = {}
    records = list(iter_estate(estate(tmp_path, [("codex", "s1", source)]), report=report))
    item = records[0]
    assert [message.text for message in item.messages] == [
        "repeat",
        "repeat",
        "system context",
        "tool result",
    ]
    assert len({message.id for message in item.messages}) == 4
    assert [message.role for message in item.messages] == ["user", "user", "system", "tool"]
    assert item.extra["body_status"] == "full"
    assert item.extra["parent_session_id"] == "parent"
    assert item.extra["lane"] == "coding"
    assert report["records"] == 1
    assert source.read_bytes() == before


def test_same_native_id_in_two_families_is_not_collapsed(tmp_path):
    source = tmp_path / "missing.jsonl"
    records = list(
        iter_estate(estate(tmp_path, [("codex", "same", source), ("claude", "same", source)]))
    )
    assert {item.provider for item in records} == {"codex", "claudecode"}
    assert len({item.uid for item in records}) == 2
    assert all(item.extra["body_status"] == "metadata_only" for item in records)
    assert all(not item.messages for item in records)


def test_duplicate_observations_preserve_changed_native_message(tmp_path):
    first = jsonl(tmp_path / "a.jsonl", [codex_message("u", "first")])
    second = jsonl(tmp_path / "b.jsonl", [codex_message("u", "changed")])
    db = estate(tmp_path, [("codex", "s", first)], [("codex", str(second), "session_meta", "s")])
    item = next(iter_estate(db))
    assert {m.text for m in item.messages} == {"first", "changed"}
    assert len({m.id for m in item.messages}) == 2
    assert len(item.extra["source_observations"]) == 2
    assert any("revision_of" in message.meta for message in item.messages)


def test_same_event_replayed_in_two_files_keeps_locations(tmp_path):
    first = jsonl(tmp_path / "a.jsonl", [codex_message("u", "same")])
    second = jsonl(tmp_path / "b.jsonl", [codex_message("u", "same")])
    db = estate(tmp_path, [("codex", "s", first)], [("codex", str(second), "session_meta", "s")])
    item = next(iter_estate(db))
    assert len(item.messages) == 1
    assert len(item.messages[0].meta["source_locations"]) == 2


def test_claude_blocks_preserve_tool_thinking_and_parent_metadata(tmp_path):
    source = jsonl(
        tmp_path / "claude.jsonl",
        [
            {
                "type": "assistant",
                "uuid": "a",
                "sessionId": "s",
                "parentUuid": "p",
                "isSidechain": True,
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "reply"},
                        {"type": "thinking", "thinking": "reason"},
                        {
                            "type": "tool_use",
                            "id": "call",
                            "name": "read",
                            "input": {"path": "synthetic"},
                        },
                        {"type": "tool_result", "tool_use_id": "call", "content": "result"},
                    ],
                },
            }
        ],
    )
    item = next(iter_estate(estate(tmp_path, [("claude", "s", source)])))
    assert [m.channel for m in item.messages] == ["reply", "thinking", "tool", "tool"]
    assert item.messages[0].meta["parent_id"] == "p"
    assert item.messages[0].meta["sidechain"] is True


def test_gemini_census_resolves_relative_path_and_patch_messages(tmp_path):
    source = jsonl(
        tmp_path / "session.jsonl",
        [
            {"sessionId": "g", "startTime": "2026-01-01T00:00:00Z"},
            {"$set": {"messages": [{"id": "m", "type": "user", "content": [{"text": "ask"}]}]}},
            {"id": "a", "type": "gemini", "content": "answer"},
        ],
    )
    census = tmp_path / "census.json"
    census.write_text(
        json.dumps(
            {
                "root_path": str(tmp_path),
                "sessions": [{"session_id": "g", "relative_path": source.name}],
            }
        )
    )
    db = estate(tmp_path, [("gemini", "g", census)], [("gemini", str(census), "sessions/0", "g")])
    item = next(iter_estate(db))
    assert item.provider == "geminicli"
    assert [m.text for m in item.messages] == ["ask", "answer"]
    assert item.extra["body_status"] == "full"


def test_copilot_events_directory_and_missing_body_are_honest(tmp_path):
    directory = tmp_path / "state"
    directory.mkdir()
    jsonl(
        directory / "events.jsonl",
        [
            {"type": "user.message", "id": "u", "data": {"content": "hello"}},
            {"type": "assistant.message", "id": "a", "data": {"content": "world"}},
            {"type": "tool.execution_complete", "id": "t", "data": {"result": {"content": "done"}}},
        ],
    )
    item = next(iter_estate(estate(tmp_path, [("copilot", "c", directory)])))
    assert item.provider == "copilotcli"
    assert [m.role for m in item.messages] == ["user", "assistant", "tool"]


def test_opencode_reads_legacy_and_v2_in_read_only_mode(tmp_path):
    source = tmp_path / "opencode.db"
    with sqlite3.connect(source) as c:
        c.executescript(
            "CREATE TABLE message (id TEXT, session_id TEXT, time_created INT, data TEXT); CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, time_created INT, data TEXT); CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT, seq INT, time_created INT, data TEXT);"
        )
        c.execute(
            "INSERT INTO message VALUES (?,?,?,?)", ("u", "s", 1, json.dumps({"role": "user"}))
        )
        c.execute(
            "INSERT INTO part VALUES (?,?,?,?,?)",
            ("p", "u", "s", 1, json.dumps({"type": "text", "text": "legacy"})),
        )
        c.execute(
            "INSERT INTO session_message VALUES (?,?,?,?,?,?)",
            (
                "a",
                "s",
                "assistant",
                2,
                2,
                json.dumps({"content": [{"type": "text", "text": "modern"}]}),
            ),
        )
    before = source.read_bytes()
    item = next(iter_estate(estate(tmp_path, [("opencode", "s", source)])))
    assert [m.text for m in item.messages] == ["legacy", "modern"]
    assert source.read_bytes() == before
    assert item.provenance["source_sha256"]
    assert item.provenance["hash_scope"] == "selected-native-records"


def test_limits_and_malformed_lines_preserve_identity_and_report_partial(tmp_path):
    source = jsonl(tmp_path / "native.jsonl", [codex_message("u", "valid")])
    with source.open("a") as handle:
        handle.write("{bad-json\n")
    db = estate(tmp_path, [("codex", "s", source)])
    item = next(iter_estate(db))
    assert item.messages[0].text == "valid"
    assert item.extra["body_status"] == "partial"
    assert any("invalid_json" in f for f in item.extra["findings"])
    limited = next(iter_estate(db, max_source_bytes=3))
    assert limited.extra["body_status"] == "metadata_only"
    assert any("source_too_large" in f for f in limited.extra["findings"])


def test_gzip_source_and_unrelated_native_session_are_checked(tmp_path):
    source = tmp_path / "native.jsonl.gz"
    with gzip.open(source, "wt") as stream:
        stream.write(json.dumps({"type": "session_meta", "payload": {"id": "other"}}) + "\n")
        stream.write(json.dumps(codex_message("u", "wrong session")) + "\n")
    item = next(iter_estate(estate(tmp_path, [("codex", "expected", source)])))
    assert not item.messages
    assert item.extra["body_status"] == "metadata_only"
    assert any("identity_mismatch" in f for f in item.extra["findings"])


def test_antigravity_binary_steps_are_not_invented_as_transcript(tmp_path):
    source = tmp_path / "ag.db"
    with sqlite3.connect(source) as c:
        c.executescript(
            "CREATE TABLE trajectory_meta (trajectory_id TEXT); CREATE TABLE steps (idx INT, step_payload BLOB);"
        )
        c.execute("INSERT INTO trajectory_meta VALUES (?)", ("ag",))
        c.execute("INSERT INTO steps VALUES (?,?)", (1, b"\xff\x00binary"))
    item = next(iter_estate(estate(tmp_path, [("antigravity", "ag", source)])))
    assert not item.messages
    assert item.extra["body_status"] == "metadata_only"
    assert any("unsupported_binary" in finding for finding in item.extra["findings"])


def test_codex_state_database_resolves_native_body_without_false_partial(tmp_path):
    transcript = jsonl(tmp_path / "rollout-s.jsonl", [codex_message("u", "body")])
    source = tmp_path / "state.sqlite"
    with sqlite3.connect(source) as c:
        c.execute("CREATE TABLE threads (id TEXT,rollout_path TEXT)")
        c.execute("INSERT INTO threads VALUES (?,?)", ("s", str(transcript)))
    item = next(iter_estate(estate(tmp_path, [("codex", "s", source)])))
    assert [m.text for m in item.messages] == ["body"]
    assert item.extra["body_status"] == "full"


def test_copilot_sql_turns_and_tools_are_real_body(tmp_path):
    source = tmp_path / "copilot.db"
    with sqlite3.connect(source) as c:
        c.executescript(
            "CREATE TABLE turns (id INT,session_id TEXT,turn_index INT,user_message TEXT,assistant_response TEXT,timestamp TEXT); CREATE TABLE forge_trajectory_events (id INT,session_id TEXT,tool_call_id TEXT,turn_index INT,event_type TEXT,command TEXT,output TEXT,exit_code INT,created_at TEXT);"
        )
        c.execute(
            "INSERT INTO turns VALUES (?,?,?,?,?,?)",
            (1, "s", 0, "question", "answer", "2026-01-01"),
        )
        c.execute(
            "INSERT INTO forge_trajectory_events VALUES (?,?,?,?,?,?,?,?,?)",
            (2, "s", "call", 0, "tool", "echo test", "test", 0, "2026-01-01"),
        )
    item = next(iter_estate(estate(tmp_path, [("copilot", "s", source)])))
    assert [m.role for m in item.messages] == ["user", "assistant", "tool"]
    assert [m.text for m in item.messages[:2]] == ["question", "answer"]


def test_antigravity_cascade_identity_accepts_distinct_trajectory(tmp_path):
    source = tmp_path / "ag.db"
    with sqlite3.connect(source) as c:
        c.executescript(
            "CREATE TABLE trajectory_meta (trajectory_id TEXT,cascade_id TEXT); CREATE TABLE steps (idx INT,step_payload BLOB);"
        )
        c.execute("INSERT INTO trajectory_meta VALUES (?,?)", ("trajectory", "ag"))
        c.execute(
            "INSERT INTO steps VALUES (?,?)", (0, json.dumps({"role": "user", "text": "available"}))
        )
    item = next(iter_estate(estate(tmp_path, [("antigravity", "ag", source)])))
    assert [m.text for m in item.messages] == ["available"]
    assert not any("identity_mismatch" in f for f in item.extra["findings"])


def test_census_cannot_follow_symlink_outside_declared_root(tmp_path):
    allowed = tmp_path / "native"
    allowed.mkdir()
    outside = jsonl(
        tmp_path / "unrelated.jsonl", [{"id": "u", "type": "user", "content": "outside"}]
    )
    (allowed / "session.jsonl").symlink_to(outside)
    census = tmp_path / "census.json"
    census.write_text(
        json.dumps(
            {
                "root_path": str(allowed),
                "sessions": [{"session_id": "s", "relative_path": "session.jsonl"}],
            }
        )
    )
    item = next(iter_estate(estate(tmp_path, [("gemini", "s", census)])))
    assert not item.messages
    assert any("census_path_escape" in f for f in item.extra["findings"])


def test_antigravity_protobuf_leaf_text_keeps_field_path_without_invented_role(tmp_path):
    text = b"Searchable synthetic message"
    nested = bytes([26, len(text)]) + text
    wire = bytes([42, len(nested)]) + nested
    source = tmp_path / "ag.db"
    with sqlite3.connect(source) as c:
        c.executescript(
            "CREATE TABLE trajectory_meta (trajectory_id TEXT,cascade_id TEXT); CREATE TABLE steps (idx INT,step_payload BLOB);"
        )
        c.execute("INSERT INTO trajectory_meta VALUES (?,?)", ("trajectory", "ag"))
        c.execute("INSERT INTO steps VALUES (?,?)", (0, wire))
    item = next(iter_estate(estate(tmp_path, [("antigravity", "ag", source)])))
    assert [m.text for m in item.messages] == [text.decode()]
    assert item.messages[0].role == "system"
    assert item.messages[0].meta["protobuf_field_path"] == "5[0].3[0]"
    assert item.messages[0].meta["semantic_role"] == "unknown"
    assert item.extra["body_status"] == "partial"


def test_protobuf_decoder_rejects_truncation_and_bounds_depth():
    assert list(_protobuf_strings(b"\x2a\xff")) == []
    nested = b"\x0a\x04text"
    for _ in range(20):
        nested = bytes([10, len(nested)]) + nested
    assert list(_protobuf_strings(nested)) == []


def test_directory_transcript_symlink_cannot_escape_declared_directory(tmp_path):
    outside = jsonl(
        tmp_path / "outside.jsonl", [{"type": "user.message", "data": {"content": "outside"}}]
    )
    directory = tmp_path / "session"
    directory.mkdir()
    (directory / "events.jsonl").symlink_to(outside)
    item = next(iter_estate(estate(tmp_path, [("copilot", "s", directory)])))
    assert not item.messages
    assert any("directory_source_escape" in f for f in item.extra["findings"])


def test_decompressed_source_limit_is_enforced(tmp_path):
    source = tmp_path / "source.jsonl.gz"
    with gzip.open(source, "wt") as handle:
        handle.write(json.dumps(codex_message("u", "a" * 10000)))
    item = next(iter_estate(estate(tmp_path, [("codex", "s", source)]), max_source_bytes=512))
    assert not item.messages
    assert any("source_too_large" in f for f in item.extra["findings"])
