from __future__ import annotations

from pathlib import Path

from conversation_corpus_engine.archive_model import (
    Attachment,
    Conversation,
    Message,
    SourceTracker,
    iter_conversations,
    normalize_role,
    stable_uid,
    to_iso,
    write_jsonl_atomic,
)


def test_to_iso_normalizes_every_supported_shape() -> None:
    assert to_iso(1_700_000_000) == "2023-11-14T22:13:20Z"
    assert to_iso(1_700_000_000_000) == "2023-11-14T22:13:20Z"
    assert to_iso("1700000000.5") == "2023-11-14T22:13:20Z"
    assert to_iso("2025-06-01T10:00:00.123Z") == "2025-06-01T10:00:00Z"
    assert to_iso("2025-06-01T12:00:00+02:00") == "2025-06-01T10:00:00Z"
    assert to_iso("2025-06-01T10:00:00") == "2025-06-01T10:00:00Z"
    assert to_iso("2025-05-01T10:00:00.123456789Z") == "2025-05-01T10:00:00Z"
    assert to_iso({"$date": {"$numberLong": "1700000000000"}}) == "2023-11-14T22:13:20Z"
    assert to_iso({"$date": "2025-06-01T10:00:00Z"}) == "2025-06-01T10:00:00Z"
    assert to_iso(None) == ""
    assert to_iso("not a date") == ""
    assert to_iso(True) == ""


def test_stable_uid_keeps_plain_ids_and_hashes_the_rest() -> None:
    assert stable_uid("chatgpt", "abc-123") == "chatgpt-abc-123"
    odd = stable_uid("gemini", "Session 2025/06")
    assert odd.startswith("gemini-session-2025-06-")
    assert odd == stable_uid("gemini", "Session 2025/06")
    assert odd != stable_uid("gemini", "Session 2025_06")
    assert stable_uid("claude", "ABC") != stable_uid("claude", "abc")


def test_normalize_role_maps_vendor_vocabulary() -> None:
    assert normalize_role("human") == "user"
    assert normalize_role("ASSISTANT") == "assistant"
    assert normalize_role("grok") == "assistant"
    assert normalize_role("tool") == "tool"
    assert normalize_role("") == "system"


def test_conversation_round_trips_through_jsonl(tmp_path: Path) -> None:
    original = Conversation(
        provider="claude",
        source_id="u-1",
        title="Round trip",
        created="2025-01-01T00:00:00Z",
        messages=[
            Message(
                role="user",
                text="héllo wörld",
                ts="2025-01-01T00:00:00Z",
                attachments=[Attachment(name="a.png", kind="image", size=3, ref="r")],
                meta={"kind": "text"},
            )
        ],
        provenance={"source_file": "c.json"},
        extra={"k": 1},
    )
    path = tmp_path / "c.jsonl"
    assert write_jsonl_atomic(path, [original.to_dict()]) == 1
    (restored,) = list(iter_conversations(path))
    assert restored.to_dict() == original.to_dict()
    assert restored.uid == "claude-u-1"
    assert restored.attachment_count == 1
    assert not list(tmp_path.glob(".*.tmp"))


def test_index_row_summarizes_without_bodies() -> None:
    conversation = Conversation(
        provider="grok",
        source_id="g",
        title="T",
        messages=[Message(role="user", text="word " * 200, ts="2025-01-02T00:00:00Z")],
    )
    row = conversation.index_row()
    assert row["message_count"] == 1
    assert row["word_count"] == 200
    assert row["last_activity"] == "2025-01-02T00:00:00Z"
    assert len(row["snippet"]) <= 240
    assert "messages" not in row


def test_source_tracker_hashes_once_and_counts(tmp_path: Path) -> None:
    source = tmp_path / "a.json"
    source.write_text("{}", encoding="utf-8")
    tracker = SourceTracker(tmp_path, "reader@1")
    first = tracker.provenance(source)
    source.write_text("changed", encoding="utf-8")
    assert tracker.provenance(source)["source_sha256"] == first["source_sha256"]
    assert first["source_file"] == "a.json"
    tracker.note("x")
    tracker.note("x", 2)
    assert tracker.counters["x"] == 3
    assert tracker.hashes() == {"a.json": first["source_sha256"]}
    assert tracker.files_read == ["a.json"]
