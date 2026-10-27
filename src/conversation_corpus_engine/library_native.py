"""Read the estate's declared identities and available native bodies without mutation.

The estate index is the inventory authority, not a claim that every body is available.
Each inventory identity is emitted, including missing/unsupported sources. Native event
identity, rather than text equality, controls replay folding. Changed event payloads are
retained as revisions. Database digests cover selected records, never pretend to hash an
entire mutable multi-gigabyte source database.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import shutil
import sqlite3
import subprocess
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .archive_model import Conversation, Message, to_iso

READER = "library-native@1.1.0"
PROVIDERS = {
    "codex": "codex",
    "claude": "claudecode",
    "gemini": "geminicli",
    "opencode": "opencode",
    "antigravity": "antigravity",
    "copilot": "copilotcli",
    "cursor": "cursor",
}
MAX_DATABASE_ROWS = 100_000
SOURCE_TIMEOUT = 30


def _digest(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("BEGIN")
    deadline = time.monotonic() + SOURCE_TIMEOUT
    connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
    return connection


def _role(value: Any) -> str:
    role = str(value or "system").lower()
    if role in {"human", "user"}:
        return "user"
    if role in {"assistant", "gemini", "model", "ai"}:
        return "assistant"
    return "tool" if role == "tool" else "system"


def _blocks(content: Any, role: str) -> Iterator[tuple[str, str, str, dict[str, Any]]]:
    """Yield textual blocks with channels; opaque images stay source evidence only."""
    channel = {"user": "typed", "assistant": "reply", "tool": "tool"}.get(role, "system")
    if isinstance(content, str):
        if content:
            yield role, channel, content, {}
    elif isinstance(content, list):
        for block in content:
            yield from _blocks(block, role)
    elif isinstance(content, dict):
        kind = str(content.get("type", ""))
        if kind in {"thinking", "reasoning"}:
            text = content.get("thinking", content.get("text", content.get("summary", "")))
            if isinstance(text, list):
                text = "\n".join(str(x.get("text", "")) for x in text if isinstance(x, dict))
            if text:
                yield "assistant", "thinking", str(text), {"kind": "reasoning"}
        elif (
            kind in {"tool_use", "tool_result", "tool", "functionCall", "functionResponse"}
            or "functionCall" in content
            or "functionResponse" in content
        ):
            yield (
                "tool",
                "tool",
                json.dumps(content, sort_keys=True, ensure_ascii=False),
                {"kind": "tool"},
            )
        elif isinstance(content.get("text"), str):
            yield role, channel, content["text"], {}
        elif "parts" in content:
            yield from _blocks(content["parts"], role)
        elif "content" in content:
            yield from _blocks(content["content"], role)


class _Body:
    def __init__(self, native_id: str, limit: int):
        self.native_id = native_id
        self.limit = limit
        self.messages: list[Message] = []
        self.findings: list[str] = []
        self.observations: list[dict[str, Any]] = []
        self.seen: dict[tuple[str, str], Message] = {}
        self.versions: dict[str, str] = {}
        self.read_paths: set[str] = set()
        self.parent: str | None = None

    def finding(self, code: str, path: Path, detail: str = "") -> None:
        entry = f"{code}:{path}" + (f":{detail}" if detail else "")
        if entry not in self.findings:
            self.findings.append(entry)

    def identity(self, path: Path, evidence: dict[str, Any]) -> None:
        """Keep the native evidence that binds a declared source to its inventory identity."""
        for observation in self.observations:
            if observation["source_path"] == str(path):
                observation.setdefault("native_identity", evidence)

    def add(
        self, event: dict[str, Any], content: Any, role: str, locator: str, *, kind: str = ""
    ) -> list[Message]:
        observed: list[Message] = []
        native_event = str(
            event.get("id")
            or event.get("uuid")
            or event.get("call_id")
            or locator.rsplit("#", 1)[-1]
        )
        timestamp = to_iso(event.get("timestamp") or event.get("ts") or event.get("time_created"))
        for index, (resolved_role, channel, text, extra) in enumerate(_blocks(content, role)):
            identity = f"{kind}:{native_event}:{index}"
            signature = _digest([resolved_role, channel, text])
            location = {"source_locator": locator, "native_event_id": native_event}
            if (identity, signature) in self.seen:
                old = self.seen[(identity, signature)]
                if location not in old.meta["source_locations"]:
                    old.meta["source_locations"].append(location)
                observed.append(old)
                continue
            message_id = "native-" + _digest([self.native_id, identity])[:24]
            meta: dict[str, Any] = {
                "native_event_id": native_event,
                "source_locations": [location],
                **extra,
            }
            if identity in self.versions:
                meta["revision_of"] = message_id
                message_id += "-" + signature[:12]
            else:
                self.versions[identity] = signature
            for source, destination in (
                ("parentUuid", "parent_id"),
                ("parentId", "parent_id"),
                ("isSidechain", "sidechain"),
            ):
                if source in event:
                    meta[destination] = event[source]
            message = Message(
                id=message_id,
                role=resolved_role,
                channel=channel,
                text=text,
                ts=timestamp,
                meta=meta,
            )
            self.seen[(identity, signature)] = message
            self.messages.append(message)
            observed.append(message)
        return observed


def _bytes(path: Path, body: _Body) -> tuple[bytes, str]:
    before = path.stat()
    if before.st_size > body.limit:
        raise ValueError("source_too_large")
    with path.open("rb") as handle:
        raw = handle.read(body.limit + 1)
    if len(raw) > body.limit:
        raise ValueError("source_too_large")
    digest = hashlib.sha256(raw).hexdigest()
    if path.suffix == ".gz":
        with gzip.GzipFile(fileobj=io.BytesIO(raw), mode="rb") as stream:
            raw = stream.read(body.limit + 1)
    elif path.suffix == ".zst":
        executable = shutil.which("zstd")
        if not executable:
            raise ValueError("zstd_unavailable")
        with subprocess.Popen(
            [executable, "-q", "-d", "-c", "--", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ) as process:
            timer = threading.Timer(SOURCE_TIMEOUT, process.kill)
            timer.start()
            try:
                assert process.stdout is not None
                raw = process.stdout.read(body.limit + 1)
                if len(raw) > body.limit:
                    process.kill()
                code = process.wait(timeout=2)
                if code and len(raw) <= body.limit:
                    raise ValueError("zstd_decode_failed_or_timeout")
            finally:
                timer.cancel()
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=2)
    if len(raw) > body.limit:
        raise ValueError("source_too_large")
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("source_changed_during_read")
    return raw, digest


def _codex_parents(payload: dict[str, Any]) -> list[str]:
    values = [payload.get(key) for key in ("parent_session_id", "forked_from_id", "parent_thread_id")]
    source = payload.get("source")
    if isinstance(source, dict):
        subagent = source.get("subagent")
        spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
        if isinstance(spawn, dict):
            values.append(spawn.get("parent_thread_id"))
    return list(dict.fromkeys(value for value in values if isinstance(value, str) and value))


def _codex(records: list[tuple[int, dict[str, Any]]], path: Path, body: _Body) -> None:
    headers = [
        payload
        for _, record in records
        if record.get("type") == "session_meta"
        and isinstance(payload := record.get("payload"), dict)
        and (payload.get("id") or payload.get("session_id"))
    ]
    if headers:
        primary = headers[0]
        primary_id = primary.get("id") or primary.get("session_id")
        if primary_id != body.native_id:
            body.finding("identity_mismatch", path)
            return
        # Forks contain inherited session_meta rows. They are accepted only through
        # parent edges explicitly declared by an already proven native header.
        allowed = {body.native_id}
        changed = True
        while changed:
            previous = len(allowed)
            for header in headers:
                if (header.get("id") or header.get("session_id")) in allowed:
                    allowed.update(_codex_parents(header))
            changed = len(allowed) != previous
        inherited = set()
        for header in headers:
            identity = header.get("id") or header.get("session_id")
            if identity not in allowed:
                body.finding("identity_mismatch", path)
                return
            if identity != body.native_id:
                inherited.add(identity)
        parents = _codex_parents(primary)
        if parents:
            body.parent = parents[0]
        body.identity(path, {
            "kind": "codex-session", "inventory_id": body.native_id,
            "primary_id": primary_id, "declared_parent_ids": parents,
            "inherited_ids": sorted(inherited),
        })
    has_messages = any(r.get("type") == "response_item" for _, r in records)
    for line, record in records:
        payload = record.get("payload", record)
        if not isinstance(payload, dict):
            continue
        event = {**payload, "timestamp": record.get("timestamp", payload.get("timestamp"))}
        locator = f"{path}#line-{line}"
        if record.get("type") == "response_item":
            kind = payload.get("type")
            if kind == "message":
                body.add(event, payload.get("content", ""), _role(payload.get("role")), locator)
            elif kind in {
                "function_call",
                "custom_tool_call",
                "function_call_output",
                "custom_tool_call_output",
            }:
                value = payload.get("output", payload.get("arguments", payload.get("input", "")))
                body.add(
                    event,
                    value if isinstance(value, (str, list)) else json.dumps(value),
                    "tool",
                    locator,
                    kind=str(kind),
                )
            elif kind == "reasoning":
                if payload.get("summary"):
                    body.add(
                        event,
                        {"type": "reasoning", "summary": payload["summary"]},
                        "assistant",
                        locator,
                    )
                if payload.get("encrypted_content"):
                    body.finding("encrypted_reasoning_not_decoded", path)
        elif record.get("type") == "event_msg" and not has_messages:
            kind = payload.get("type")
            if kind in {"user_message", "agent_message"}:
                body.add(
                    event,
                    payload.get("message", ""),
                    "user" if kind == "user_message" else "assistant",
                    locator,
                )
        elif record.get("type") == "compacted":
            body.finding("compaction_snapshot_retained_in_source", path)
            if isinstance(payload.get("message"), str) and payload["message"]:
                body.add(event, payload["message"], "system", locator, kind="compaction")


def _claude(records: list[tuple[int, dict[str, Any]]], path: Path, body: _Body) -> None:
    parent, separator, agent = body.native_id.partition("/agent/")
    composite = bool(separator and parent and agent and "/" not in agent)
    proven = composite and any(
        record.get("sessionId") == parent and record.get("agentId") == agent
        for _, record in records
    )
    if proven:
        body.parent = parent
        body.identity(path, {
            "kind": "claude-subagent", "inventory_id": body.native_id,
            "parent_session_id": parent, "agent_id": agent,
        })
    for line, record in records:
        mismatch = (
            not proven or (
                bool(record.get("sessionId") or record.get("agentId"))
                and (record.get("sessionId") != parent or record.get("agentId") != agent)
            )
        ) if composite else bool(record.get("sessionId") and record["sessionId"] != body.native_id)
        if mismatch:
            body.finding("identity_mismatch", path, str(line))
            continue
        event = record.get("message", {})
        if isinstance(event, dict) and "content" in event:
            body.add(
                record,
                event["content"],
                _role(event.get("role", record.get("type"))),
                f"{path}#line-{line}",
            )
        elif record.get("type") in {"system", "summary"}:
            content = record.get("content", record.get("summary", record.get("message", "")))
            body.add(record, content, "system", f"{path}#line-{line}")


def _gemini_record(record: dict[str, Any], path: Path, body: _Body, locator: str) -> None:
    if record.get("sessionId") and record["sessionId"] != body.native_id:
        body.finding("identity_mismatch", path)
        return
    messages = record.get("messages")
    if isinstance(messages, list):
        for index, item in enumerate(messages):
            if isinstance(item, dict):
                _gemini_record(item, path, body, f"{locator}/messages/{index}")
    for op in ("$set", "$append", "$push"):
        patch = record.get(op)
        if not isinstance(patch, dict):
            continue
        for key, value in patch.items():
            if key == "messages" and isinstance(value, list):
                for index, item in enumerate(value):
                    if isinstance(item, dict):
                        _gemini_record(item, path, body, f"{locator}/{op}/{index}")
            elif key.startswith("messages"):
                if isinstance(value, dict) and ("content" in value or "type" in value):
                    _gemini_record(value, path, body, f"{locator}/{op}/{key}")
                else:
                    body.finding("unsupported_message_patch", path, key)
    if "content" in record:
        body.add(record, record["content"], _role(record.get("role", record.get("type"))), locator)
    if record.get("thoughts"):
        body.add(
            record,
            {"type": "thinking", "thinking": json.dumps(record["thoughts"], ensure_ascii=False)},
            "assistant",
            locator,
            kind="thoughts",
        )
    if record.get("toolCalls"):
        body.add(
            record,
            {"type": "tool", "calls": record["toolCalls"]},
            "tool",
            locator,
            kind="toolcalls",
        )


def _copilot(records: list[tuple[int, dict[str, Any]]], path: Path, body: _Body) -> None:
    for line, record in records:
        kind = str(record.get("type", ""))
        data = record.get("data", {})
        if not isinstance(data, dict):
            continue
        if (
            kind == "session.start"
            and data.get("sessionId")
            and data["sessionId"] != body.native_id
        ):
            body.finding("identity_mismatch", path)
            return
        locator = f"{path}#line-{line}"
        if kind in {"user.message", "assistant.message", "system.message"}:
            body.add(
                record, data.get("content", ""), _role(kind.split(".", maxsplit=1)[0]), locator
            )
        elif kind.startswith("tool."):
            body.add(
                record,
                json.dumps(data, sort_keys=True, ensure_ascii=False),
                "tool",
                locator,
                kind=kind,
            )
        elif kind in {"session.compaction_complete", "session.info"} and data.get("message"):
            body.add(record, data["message"], "system", locator, kind=kind)


def _native_records(
    family: str, records: list[tuple[int, dict[str, Any]]], path: Path, body: _Body
) -> None:
    if family == "codex":
        _codex(records, path, body)
    elif family == "claude":
        _claude(records, path, body)
    elif family == "copilot":
        _copilot(records, path, body)
    elif family == "gemini":
        for line, record in records:
            _gemini_record(record, path, body, f"{path}#line-{line}")
    elif family == "antigravity":
        for line, record in records:
            if "messages" in record:
                for index, message in enumerate(record["messages"]):
                    if isinstance(message, dict):
                        body.add(
                            message,
                            message.get("content", message.get("text", "")),
                            _role(message.get("role")),
                            f"{path}#line-{line}/messages/{index}",
                        )
            elif "role" in record:
                body.add(
                    record,
                    record.get("content", record.get("text", "")),
                    _role(record["role"]),
                    f"{path}#line-{line}",
                )
    else:
        body.finding("unsupported_provider_body", path, family)


def _census_path(
    payload: dict[str, Any], native_id: str, object_key: str, path: Path
) -> Path | None:
    selected: Any = payload
    try:
        for part in object_key.split("/"):
            selected = selected[int(part)] if isinstance(selected, list) else selected[part]
    except (KeyError, ValueError, TypeError, IndexError):
        selected = None
    if not isinstance(selected, dict):
        sessions = payload.get("sessions", [])
        if isinstance(sessions, dict):
            selected = sessions.get(native_id)
        elif isinstance(sessions, list):
            selected = next(
                (
                    item
                    for item in sessions
                    if isinstance(item, dict)
                    and (item.get("session_id") or item.get("id")) == native_id
                ),
                None,
            )
    if not isinstance(selected, dict):
        return None
    value = (
        selected.get("relative_path")
        or selected.get("path")
        or selected.get("file_path")
        or selected.get("transcript_path")
    )
    if not isinstance(value, str) or not value:
        return None
    candidate = Path(value).expanduser()
    root = Path(payload.get("root_path") or path.parent).expanduser()
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("census_path_escape")
    return resolved


def _wire_fields(data: bytes) -> list[tuple[str, bytes]] | None:
    """Parse wire framing only. Never load or execute serialized objects or code."""
    position = 0
    occurrences: Counter[int] = Counter()
    fields: list[tuple[str, bytes]] = []

    def varint() -> int:
        nonlocal position
        value = 0
        for shift in range(0, 70, 7):
            if position >= len(data):
                raise ValueError("truncated protobuf varint")
            byte = data[position]
            position += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise ValueError("oversized protobuf varint")

    try:
        count = 0
        while position < len(data):
            count += 1
            if count > MAX_DATABASE_ROWS:
                return None
            tag = varint()
            number, wire = tag >> 3, tag & 7
            if not number or number >= 1 << 29:
                return None
            if wire == 0:
                varint()
            elif wire in {1, 5}:
                position += 8 if wire == 1 else 4
            elif wire == 2:
                size = varint()
                end = position + size
                if end > len(data):
                    return None
                identity = f"{number}[{occurrences[number]}]"
                occurrences[number] += 1
                fields.append((identity, data[position:end]))
                position = end
            else:
                return None
            if position > len(data):
                return None
    except ValueError:
        return None
    return fields


def _protobuf_strings(data: bytes, path: str = "", depth: int = 0) -> Iterator[tuple[str, str]]:
    """Recover printable UTF-8 leaves with wire paths, explicitly without role semantics.

    A protobuf length-delimited field may be bytes, text or a nested message. Complete
    valid framing is recursively inspected first. Unknown binary leaves stay in the
    preserved source; recovered strings are labeled partial evidence, never a decoded
    conversation. Recursion and field counts are bounded.
    """
    if depth > 16:
        return
    fields = _wire_fields(data)
    if fields:
        for identity, value in fields:
            yield from _protobuf_strings(value, f"{path}.{identity}".lstrip("."), depth + 1)
    elif depth:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return
        if text.strip() and all(
            character.isprintable() or character in "\t\r\n" for character in text
        ):
            yield path, text


def _database(path: Path, family: str, body: _Body, observation: dict[str, Any]) -> None:
    connection = _connect(path)
    selected_digest = hashlib.sha256()
    used_bytes = 0

    def rows(sql: str, parameters: tuple[Any, ...] = ()) -> Iterator[dict[str, Any]]:
        nonlocal used_bytes
        for index, row in enumerate(connection.execute(sql, parameters)):
            if index >= MAX_DATABASE_ROWS:
                body.finding("database_row_limit", path)
                break
            value = dict(row)
            used_bytes += len(json.dumps(value, default=str).encode("utf-8"))
            if used_bytes > body.limit:
                body.finding("source_too_large", path)
                break
            selected_digest.update(json.dumps(value, sort_keys=True, default=str).encode("utf-8"))
            selected_digest.update(b"\n")
            yield value

    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if family == "codex" and "threads" in tables:
            observation["content_role"] = "native-metadata"
            for row in rows("SELECT id,rollout_path FROM threads WHERE id=?", (body.native_id,)):
                candidate = Path(row["rollout_path"]).expanduser()
                if not candidate.resolve().is_relative_to(path.parent.resolve()):
                    body.finding("native_path_escape", path)
                    continue
                if (
                    not candidate.exists()
                    and candidate.with_suffix(candidate.suffix + ".zst").is_file()
                ):
                    candidate = candidate.with_suffix(candidate.suffix + ".zst")
                _read_source(candidate, family, body, f"threads/{body.native_id}/rollout_path")
        elif family == "copilot" and "turns" in tables:
            for row in rows(
                "SELECT id,turn_index,user_message,assistant_response,timestamp FROM turns WHERE session_id=? ORDER BY turn_index,id",
                (body.native_id,),
            ):
                for field, role in (("user_message", "user"), ("assistant_response", "assistant")):
                    if row[field]:
                        body.add(
                            {"id": f"turn-{row['id']}-{role}", "timestamp": row["timestamp"]},
                            row[field],
                            role,
                            f"{path}#turns/{row['id']}/{field}",
                        )
            if "forge_trajectory_events" in tables:
                for row in rows(
                    "SELECT id,tool_call_id,turn_index,event_type,command,output,exit_code,created_at FROM forge_trajectory_events WHERE session_id=? ORDER BY id",
                    (body.native_id,),
                ):
                    body.add(
                        {"id": f"tool-{row['id']}", "timestamp": row["created_at"]},
                        json.dumps(row, sort_keys=True, ensure_ascii=False),
                        "tool",
                        f"{path}#forge_trajectory_events/{row['id']}",
                    )
            if "checkpoints" in tables:
                for row in rows(
                    "SELECT id,checkpoint_number,title,overview,history,work_done,technical_details,important_files,next_steps,created_at FROM checkpoints WHERE session_id=? ORDER BY checkpoint_number,id",
                    (body.native_id,),
                ):
                    content = "\n\n".join(
                        f"{key}: {value}"
                        for key, value in row.items()
                        if value and key not in {"id", "checkpoint_number", "created_at"}
                    )
                    body.add(
                        {"id": f"checkpoint-{row['id']}", "timestamp": row["created_at"]},
                        content,
                        "system",
                        f"{path}#checkpoints/{row['id']}",
                        kind="checkpoint",
                    )
        elif family == "opencode":
            if {"message", "part"} <= tables:
                for row in rows(
                    "SELECT m.id, m.time_created, m.data AS info, p.id AS part_id, p.data AS content FROM message m LEFT JOIN part p ON p.message_id=m.id WHERE m.session_id=? ORDER BY m.time_created,m.id,p.time_created,p.id",
                    (body.native_id,),
                ):
                    info = json.loads(row["info"])
                    if row["content"]:
                        event = {
                            "id": row["part_id"] or row["id"],
                            "timestamp": row["time_created"],
                        }
                        body.add(
                            event,
                            json.loads(row["content"]),
                            _role(info.get("role")),
                            f"{path}#part/{row['part_id']}",
                            kind="legacy",
                        )
            if "session_message" in tables:
                for row in rows(
                    "SELECT id,type,seq,time_created,data FROM session_message WHERE session_id=? ORDER BY seq,id",
                    (body.native_id,),
                ):
                    data = json.loads(row["data"])
                    content = data.get("content", data.get("text", ""))
                    body.add(
                        {"id": row["id"], "timestamp": row["time_created"]},
                        content,
                        _role(row["type"]),
                        f"{path}#session_message/{row['id']}",
                        kind="v2",
                    )
                    if data.get("error"):
                        body.add(
                            {"id": row["id"], "timestamp": row["time_created"]},
                            json.dumps(data["error"], sort_keys=True),
                            "system",
                            f"{path}#session_message/{row['id']}/error",
                            kind="error",
                        )
            if not ({"message", "session_message"} & tables):
                body.finding("unsupported_database_schema", path)
        elif family == "antigravity":
            if "steps" in tables:
                if "trajectory_meta" in tables:
                    columns = {
                        r[1] for r in connection.execute("PRAGMA table_info(trajectory_meta)")
                    }
                    identity_column = "cascade_id" if "cascade_id" in columns else "trajectory_id"
                    ids = {
                        str(r[0])
                        for r in connection.execute(
                            f"SELECT {identity_column} FROM trajectory_meta LIMIT 10"
                        )
                    }
                    if ids and body.native_id not in ids:
                        body.finding("identity_mismatch", path)
                        return
                for row in rows("SELECT idx,step_payload FROM steps ORDER BY idx"):
                    value = row["step_payload"]
                    try:
                        payload = json.loads(value)
                    except (ValueError, TypeError, UnicodeDecodeError):
                        recovered = False
                        if isinstance(value, bytes):
                            for field_path, text in _protobuf_strings(value):
                                recovered = True
                                recovered_messages = body.add(
                                    {"id": f"step-{row['idx']}-{field_path}"},
                                    text,
                                    "system",
                                    f"{path}#steps/{row['idx']}/wire/{field_path}",
                                    kind="protobuf-leaf",
                                )
                                for recovered_message in recovered_messages:
                                    recovered_message.meta.update(
                                        {
                                            "source_encoding": "protobuf-wire",
                                            "protobuf_field_path": field_path,
                                            "semantic_role": "unknown",
                                        }
                                    )
                        body.finding(
                            "protobuf_text_recovered_without_schema"
                            if recovered
                            else "unsupported_binary_step_payload",
                            path,
                        )
                        continue
                    if isinstance(payload, dict):
                        body.add(
                            {"id": f"step-{row['idx']}"},
                            payload.get("content", payload.get("text", "")),
                            _role(payload.get("role")),
                            f"{path}#steps/{row['idx']}",
                        )
            elif "conversation_summaries" in tables:
                for _ in rows(
                    "SELECT conversation_id,title,preview,step_count,last_modified_time,parent_conversation_id FROM conversation_summaries WHERE conversation_id=?",
                    (body.native_id,),
                ):
                    pass
                body.finding("summary_metadata_only", path)
            else:
                body.finding("unsupported_database_schema", path)
        else:
            body.finding("unsupported_database_provider", path, family)
        observation["sha256"] = selected_digest.hexdigest()
        observation["hash_scope"] = "selected-native-records"
    finally:
        connection.close()


def _read_source(
    path: Path, family: str, body: _Body, object_key: str = "", depth: int = 0
) -> None:
    identity = str(path)
    observation: dict[str, Any] = {
        "source_path": identity,
        "object_key": object_key,
        "reader": READER,
    }
    if observation in body.observations:
        return
    if identity in body.read_paths:
        previous = next((item for item in body.observations if item["source_path"] == identity), {})
        body.observations.append({**previous, **observation})
        return
    body.observations.append(observation)
    if depth > 3:
        body.finding("source_reference_depth_limit", path)
        return
    body.read_paths.add(identity)
    try:
        if path.is_dir():
            files = [
                p
                for name in (
                    "events.jsonl",
                    "session.jsonl",
                    "messages.jsonl",
                    "conversation.json",
                    "messages.json",
                )
                if (p := path / name).is_file()
            ]
            if not files:
                body.finding("directory_has_no_supported_transcript", path)
            for child in files:
                if not child.resolve().is_relative_to(path.resolve()):
                    body.finding("directory_source_escape", child)
                    continue
                _read_source(child, family, body, depth=depth + 1)
            return
        if not path.is_file():
            body.finding("source_missing", path)
            return
        if path.suffix in {".db", ".sqlite", ".sqlite3"}:
            _database(path, family, body, observation)
            return
        base_suffix = path.with_suffix("").suffix if path.suffix in {".gz", ".zst"} else path.suffix
        if base_suffix not in {".json", ".jsonl"} and not (
            family == "antigravity" and base_suffix in {".txt", ".md"}
        ):
            body.finding("unsupported_source_format", path)
            return
        raw, digest = _bytes(path, body)
        observation.update(sha256=digest, hash_scope="source-file", size_bytes=path.stat().st_size)
        text = raw.decode("utf-8")
        base_suffix = path.with_suffix("").suffix if path.suffix in {".gz", ".zst"} else path.suffix
        if base_suffix == ".jsonl":
            records = []
            for number, line in enumerate(text.splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    if isinstance(record, dict):
                        records.append((number, record))
                    else:
                        body.finding("invalid_record_shape", path, str(number))
                except json.JSONDecodeError:
                    body.finding("invalid_json", path, str(number))
            _native_records(family, records, path, body)
        elif base_suffix == ".json":
            payload = json.loads(text)
            if isinstance(payload, dict) and (
                "sessions" in payload or object_key.startswith("sessions/")
            ):
                resolved = _census_path(payload, body.native_id, object_key, path)
                if resolved:
                    _read_source(resolved, family, body, depth=depth + 1)
                else:
                    body.finding("census_without_transcript_path", path)
            elif isinstance(payload, dict):
                _native_records(family, [(1, payload)], path, body)
            elif isinstance(payload, list):
                _native_records(
                    family,
                    [(i + 1, r) for i, r in enumerate(payload) if isinstance(r, dict)],
                    path,
                    body,
                )
            else:
                body.finding("unsupported_json_shape", path)
        elif family == "antigravity" and base_suffix in {".txt", ".md"}:
            body.add({"id": "text-artifact"}, text, "system", str(path), kind="artifact")
            body.finding("text_artifact_not_native_transcript", path)
        else:
            body.finding("unsupported_source_format", path)
    except (OSError, ValueError, TypeError, sqlite3.Error, subprocess.SubprocessError) as exc:
        body.finding("source_error", path, str(exc))


def iter_estate(
    database: Path, *, report: dict[str, Any] | None = None, max_source_bytes: int = 536870912
) -> Iterator[Conversation]:
    """Yield every inventory identity, attaching real bodies or explicit coverage findings.

    ``max_source_bytes`` bounds both decompressed files and selected SQL records per source.
    No source is written and no arbitrary account/credential tables are queried. ``full``
    means all supported text in the declared observations was read; it is not an assertion
    about undiscovered estate data. Reports contain counts only, never transcript text.
    """
    if max_source_bytes < 1:
        raise ValueError("max_source_bytes must be positive")
    report = report if report is not None else {}
    counters: Counter[str] = Counter()
    families: Counter[str] = Counter()
    findings: Counter[str] = Counter()
    connection = _connect(Path(database))
    try:
        # The inventory query is independent of per-source deadlines and may stream for hours.
        connection.set_progress_handler(None, 0)
        observations: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
        table_names = {
            r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "source_observations" in table_names:
            for row in connection.execute(
                "SELECT agent_family,native_id,source_path,object_key FROM source_observations ORDER BY agent_family,native_id,source_path,object_key"
            ):
                observations[(row["agent_family"], row["native_id"])].append(
                    (row["source_path"], row["object_key"])
                )
        for row in connection.execute("SELECT * FROM sessions ORDER BY agent_family,session_id"):
            record = dict(row)
            family = str(record["agent_family"])
            native_id = str(record["session_id"])
            body = _Body(native_id, max_source_bytes)
            if family == "copilot" and native_id.startswith("."):
                body.finding(
                    "inventory_artifact_not_native_session",
                    Path(record.get("custody_path") or database),
                )
            declared = observations.get((family, native_id), [])
            custody = record.get("custody_path")
            if custody and not any(path == custody for path, _ in declared):
                declared = [(custody, "custody_path"), *declared]
            for source, key in declared:
                _read_source(Path(source).expanduser(), family, body, str(key))
            if not declared:
                body.finding("no_declared_source", Path(database))
            status = (
                "metadata_only" if not body.messages else "partial" if body.findings else "full"
            )
            if not body.messages and not body.findings:
                body.finding("no_textual_messages", Path(custody or database))
            primary = next((item for item in body.observations if item.get("sha256")), {})
            provenance = {
                "source_file": primary.get("source_path", str(custody or database)),
                "source_sha256": primary.get("sha256", ""),
                "hash_scope": primary.get("hash_scope", "unmeasured"),
                "reader": READER,
                "inventory_database": str(database),
            }
            item = Conversation(
                provider=PROVIDERS.get(family, family),
                source_id=native_id,
                title=record.get("prompt_title") or f"{family} session {native_id}",
                created=to_iso(record.get("started_at")),
                updated=to_iso(record.get("ended_at")),
                collection=record.get("project_root") or "",
                messages=body.messages,
                provenance=provenance,
                extra={
                    "wing": "ai",
                    "lane": "coding",
                    "body_status": status,
                    "native_id": native_id,
                    "parent_session_id": body.parent or record.get("parent_session_id"),
                    "source_observations": body.observations,
                    "findings": body.findings,
                    "metadata_snippet": record.get("prompt_snippet") or "",
                    "session_kind": record.get("session_kind", "unknown"),
                },
            )
            counters["records"] += 1
            counters[status] += 1
            counters["messages"] += len(body.messages)
            counters["source_findings"] += len(body.findings)
            findings.update(value.split(":", 1)[0] for value in body.findings)
            families[family] += 1
            report.update(dict(counters))
            report["families"] = dict(families)
            report["findings"] = dict(findings)
            yield item
    finally:
        connection.close()
