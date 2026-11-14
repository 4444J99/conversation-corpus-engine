from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from conversation_corpus_engine.archive_model import (
    CHANNELS,
    LEGACY_SCHEMA,
    SCHEMA,
    Attachment,
    Conversation,
    Message,
    SourceTracker,
    assign_message_fields,
    channel_for,
    derived_source_id,
    finalize_conversation,
    iter_conversations,
    message_id_fallback,
    message_reference,
    normalize_role,
    resolve_source_id,
    stable_uid,
    to_iso,
    write_jsonl_atomic,
)
from conversation_corpus_engine.archive_naming import (
    InvalidName,
    conversation_name,
    hash_identity,
    parse_name,
    validate_name,
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


def _conversation(*messages: Message, **fields) -> Conversation:
    defaults = {"provider": "chatgpt", "source_id": "abc-123", "title": "T"}
    return Conversation(**{**defaults, **fields}, messages=list(messages))


def test_message_carries_id_channel_and_typed_characters() -> None:
    plain = Message(role="user", text="héllo")
    assert (plain.id, plain.channel, plain.typed_chars) == ("", "", 5)
    appended = Message(role="user", text="typed plus attachment", typed_chars=5)
    assert appended.typed_chars == 5
    payload = appended.to_dict()
    assert {"id", "channel", "typed_chars"} <= set(payload)
    assert Message.from_dict(payload) == appended
    assert Message.from_dict({"role": "user", "text": "ab"}).typed_chars == 2
    assert Message.from_dict({"role": "user", "text": "ab", "typed_chars": True}).typed_chars == 2


def test_schema_strings_are_plain_and_the_previous_one_still_loads() -> None:
    assert SCHEMA == "archive-conversation.v2"
    assert LEGACY_SCHEMA == "cce.archive.conversation.v1"
    current = _conversation(Message(role="user", text="q", ts="2025-01-01T00:00:00Z"))
    assert current.to_dict()["schema"] == SCHEMA
    legacy = current.to_dict()
    legacy["schema"] = LEGACY_SCHEMA
    for message in legacy["messages"]:
        for key in ("id", "channel", "typed_chars"):
            message.pop(key)
    upgraded = Conversation.from_dict(legacy)
    (message,) = upgraded.messages
    assert re.fullmatch(r"message-chatgpt-[0-9a-f]{16}", message.id)
    assert (message.channel, message.typed_chars) == ("typed", 1)
    untouched = Conversation.from_dict(current.to_dict())
    assert untouched.messages[0].id == "" and untouched.messages[0].channel == ""
    bad = current.to_dict()
    bad["schema"] = "archive-conversation.v9"
    with pytest.raises(ValueError, match="unsupported conversation schema"):
        Conversation.from_dict(bad)


@pytest.mark.parametrize(
    ("role", "kind", "conversation_kind", "channel"),
    [
        ("user", "text", "chat", "typed"),
        ("user", "code", "search", "typed"),
        ("user", "media-prompt", "activity", "typed"),
        ("assistant", "text", "chat", "reply"),
        ("assistant", "canvas", "chat", "reply"),
        ("assistant", "reasoning", "chat", "thinking"),
        ("assistant", "tool", "chat", "tool"),
        ("tool", "tool", "chat", "tool"),
        ("tool", "text", "chat", "tool"),
        ("system", "scheduled-actions", "activity", "system"),
        ("system", "prompt-template", "notebook", "project"),
        ("user", "text", "notebook", "project"),
        ("system", "summary", "memory", "memory"),
        ("system", "memory-file", "memory", "memory"),
    ],
)
def test_channel_table(role: str, kind: str, conversation_kind: str, channel: str) -> None:
    message = Message(role=role, text="x", meta={"kind": kind})
    assert channel_for(message, conversation_kind) == channel
    assert channel in CHANNELS
    assert channel_for(Message(role="user", text="x"), "chat") == "typed"


def test_fallback_message_id_is_stable_and_repeats_stay_distinct() -> None:
    expected_hash = hash_identity(
        "\0".join(["claude", "claude-u1", "user", "2025-01-01T00:00:00Z", "hi"])
    )
    first = message_id_fallback("claude", "claude-u1", "user", "2025-01-01T00:00:00Z", "hi")
    assert first == f"message-claude-{expected_hash}"
    assert parse_name(first).kind == "message"
    again = message_id_fallback("claude", "claude-u1", "user", "2025-01-01T00:00:00Z", "hi", 1)
    assert again != first and again == message_id_fallback(
        "claude", "claude-u1", "user", "2025-01-01T00:00:00Z", "hi", 1
    )
    other = message_id_fallback("claude", "claude-u2", "user", "2025-01-01T00:00:00Z", "hi")
    assert other != first


def test_assign_message_fields_keeps_vendor_ids_and_fills_the_rest() -> None:
    conversation = _conversation(
        Message(role="user", text="one", ts="t", id="vendor-1"),
        Message(role="assistant", text="two", ts="t"),
        Message(role="assistant", text="two", ts="t"),
        Message(role="assistant", text="three", ts="t", id="vendor-1"),
        Message(role="assistant", text="why", ts="t", meta={"kind": "reasoning"}),
    )
    assign_message_fields(conversation)
    ids = [m.id for m in conversation.messages]
    assert ids[0] == "vendor-1"
    assert len(set(ids)) == 5, "repeats and a duplicated vendor id are all kept distinct"
    assert all(validate_name(i) == [] for i in ids[1:])
    assert [m.channel for m in conversation.messages] == [
        "typed",
        "reply",
        "reply",
        "reply",
        "thinking",
    ]
    snapshot = [m.to_dict() for m in conversation.messages]
    assign_message_fields(conversation)
    assert [m.to_dict() for m in conversation.messages] == snapshot


def test_a_conversation_with_no_vendor_id_gets_a_derived_one() -> None:
    texts = ["a", "b", "c", "d"]
    messages = [Message(role="user", text=t) for t in texts]
    expected = (
        f"derived-{hash_identity(chr(0).join(['grok', '2025-01-01T00:00:00Z', 'a', 'b', 'c']))}"
    )
    assert derived_source_id("grok", "2025-01-01T00:00:00Z", messages) == expected
    messages[3].text = "changed after the first three"
    assert derived_source_id("grok", "2025-01-01T00:00:00Z", messages) == expected
    conversation = Conversation(
        provider="grok", source_id="", title="", created="2025-01-01T00:00:00Z", messages=messages
    )
    assert resolve_source_id(conversation) == "empty"
    assert conversation.source_id == expected and conversation.uid == f"grok-{expected}"
    assert conversation.extra["source_id_derived"] == "empty"
    assert resolve_source_id(conversation) == "", "resolving twice changes nothing"


def test_an_id_the_naming_contract_cannot_express_is_derived_and_remembered() -> None:
    conversation = _conversation(Message(role="user", text="q"), source_id="a--b")
    assert stable_uid("chatgpt", "a--b") == "chatgpt-a--b"
    with pytest.raises(InvalidName):
        conversation_name("chatgpt", conversation.uid)
    assert resolve_source_id(conversation) == "unnameable"
    assert conversation.extra["vendor_id"] == "a--b"
    assert conversation.source_id.startswith("derived-")
    assert conversation_name("chatgpt", conversation.uid) == f"conversation-{conversation.uid}"
    fine = _conversation(Message(role="user", text="q"), source_id="abc-123")
    assert resolve_source_id(fine) == "" and fine.source_id == "abc-123"
    assert "source_id_derived" not in fine.extra
    with pytest.raises(InvalidName):
        resolve_source_id(_conversation(source_id="x", provider="nowhere"))


def test_finalize_resolves_the_id_before_minting_message_ids() -> None:
    conversation = _conversation(Message(role="user", text="q", ts="t"), source_id="")
    assert finalize_conversation(conversation) == "empty"
    (message,) = conversation.messages
    assert message.id == message_id_fallback("chatgpt", conversation.uid, "user", "t", "q")


def test_message_reference_hashes_vendor_ids_with_their_conversation() -> None:
    reference = message_reference("chatgpt", "chatgpt-abc-123", "node-1")
    assert reference == f"message-chatgpt-{hash_identity('chatgpt-abc-123' + chr(0) + 'node-1')}"
    assert reference != message_reference("chatgpt", "chatgpt-zzz", "node-1")
    minted = message_id_fallback("chatgpt", "chatgpt-abc-123", "user", "t", "q")
    assert message_reference("chatgpt", "chatgpt-abc-123", minted) == minted
    assert json.dumps({"reference": reference})
