import json
from pathlib import Path

import pytest

from conversation_corpus_engine import archive_read_chatgpt, library_commands, library_mcp
from conversation_corpus_engine.archive_model import Attachment, Conversation, Message
from conversation_corpus_engine.library_commands import open_library, read_record
from conversation_corpus_engine.library_mcp import READ_TOOL
from conversation_corpus_engine.library_read_metadata import message_metadata
from conversation_corpus_engine.library_store import Library


def test_read_exposes_observed_attachments_and_typed_boundary(tmp_path):
    with Library.create(tmp_path / "library.sqlite") as library:
        item = Conversation(
            provider="chatgpt",
            source_id="attachments",
            title="Evidence",
            messages=[
                Message(
                    role="user",
                    text="typed attachment text",
                    typed_chars=5,
                    attachments=[Attachment(name="one.pdf", size=42, ref="file-one")],
                ),
                Message(role="user", text="second", attachments=[Attachment(name="two.png")]),
            ],
        )
        library.put(item)
        before = library.evidence_fingerprint()
        first = read_record(library, item.uid, limit=4)
        assert first["attachment_count_observed"] == 2
        assert "observed" in first["attachment_notice"]
        assert first["typed_chars"] == 5
        assert first["typed_chars_status"] == "normalized"
        assert first["attachments"] == [
            {"name": "one.pdf", "kind": "file", "size": 42, "ref": "file-one"}
        ]
        assert first["attachment_count_in_message"] == 1
        second = read_record(library, item.uid, message=1)
        assert second["attachments"][0]["name"] == "two.png"
        assert library.evidence_fingerprint() == before


def test_partial_read_bounds_metadata_and_labels_counts(tmp_path):
    with Library.create(tmp_path / "library.sqlite") as library:
        item = Conversation(
            provider="claude",
            source_id="partial",
            title="Partial",
            messages=[
                Message(
                    role="user",
                    text="x",
                    attachments=[Attachment(name="a" * 600) for _ in range(21)],
                )
            ],
            extra={"body_status": "partial"},
        )
        library.put(item)
        result = read_record(library, item.uid)
        assert result["body_status"] == "partial"
        assert result["attachment_count_observed"] == 21
        assert result["attachment_count_in_message"] == 21
        assert len(result["attachments"]) == 20
        assert len(result["attachments"][0]["name"]) == 512
        assert result["attachments_truncated"] is True


@pytest.mark.parametrize("typed", [None, True, -1, 99, "4"])
def test_unknown_or_invalid_boundary_is_not_invented(typed):
    result = message_metadata({"text": "hello", "typed_chars": typed})
    assert result["typed_chars"] is None
    assert result["typed_chars_status"] == ("unknown" if typed is None else "invalid")


@pytest.mark.parametrize("typed", [0, 5])
def test_boundary_endpoints_are_valid(typed):
    assert message_metadata({"text": "hello", "typed_chars": typed})["typed_chars"] == typed


@pytest.mark.parametrize(
    "ref",
    [
        "https://media.example/file?signature=synthetic-secret",
        "https://synthetic-user:synthetic-password@media.example/file",
        "//media.example/file?token=synthetic-secret",
        "https%3A%2F%2Fmedia.example%2Ffile%3Ftoken%3Dsynthetic-secret",
        "file:///private/example/file.pdf",
        "file-one?access_token=synthetic-secret",
        "file-one#synthetic-secret",
    ],
)
def test_attachment_reference_cannot_export_a_url_or_credential(ref):
    result = message_metadata({"attachments": [{"name": "file.pdf", "ref": ref}]})
    assert result["attachments"][0]["ref"] == ""
    assert result["attachment_refs_redacted"] is True
    assert "synthetic-secret" not in json.dumps(result)


@pytest.mark.parametrize("size", [-1, True, "42", None])
def test_invalid_size_is_unknown(size):
    result = message_metadata({"attachments": [{"name": "one", "size": size}]})
    assert result["attachments"][0]["size"] is None


@pytest.mark.parametrize("size", [0, 42])
def test_nonnegative_size_is_retained(size):
    result = message_metadata({"attachments": [{"name": "one", "size": size}]})
    assert result["attachments"][0]["size"] == size


def test_reader_generated_image_marker_never_counts_as_typed_text(tmp_path):
    node = {
        "id": "image-message",
        "message": {
            "author": {"role": "user"},
            "content": {
                "content_type": "multimodal_text",
                "parts": [
                    "before",
                    {
                        "content_type": "image_asset_pointer",
                        "asset_pointer": "file-service://file-example",
                    },
                    "after",
                ],
            },
        },
    }
    message, _ = archive_read_chatgpt._node_message(node, {})
    original = message.text
    assert original == "before\n\n[image]\n\nafter"
    with Library.create(tmp_path / "library.sqlite") as library:
        item = Conversation(
            provider="chatgpt", source_id="image", title="Image", messages=[message]
        )
        library.put(item)
        result = read_record(library, item.uid)
        assert result["text"] == original
        assert result["typed_chars"] is None
        assert result["typed_chars_status"] == "synthetic-marker"


def test_recorded_prefix_before_image_is_still_valid():
    result = message_metadata(
        {
            "text": "typed\n\n[image]",
            "typed_chars": 5,
            "attachments": [{"kind": "image", "name": "image.png"}],
        }
    )
    assert result["typed_chars"] == 5


def test_ranges_share_one_attachment_budget_and_alternates_are_separate(tmp_path, monkeypatch):
    path = tmp_path / "library.sqlite"
    with Library.create(path) as library:
        item = Conversation(
            provider="chatgpt",
            source_id="many",
            title="Many",
            messages=[
                Message(
                    role="user", text="x", attachments=[Attachment(name="a") for _ in range(20)]
                )
                for _ in range(3)
            ],
            extra={
                "alternate_messages": [
                    {
                        "role": "user",
                        "text": "alternate",
                        "typed_chars": 9,
                        "attachments": [{"name": "alternate.pdf", "ref": "file-alternate"}],
                    }
                ]
            },
        )
        library.put(item)
        result = read_record(library, item.uid, message_end=3)
        assert result["attachment_count_observed"] == 60
        assert sum(len(row["attachments"]) for row in result["messages"]) == 20
        assert all(row["attachments_truncated"] for row in result["messages"][1:])
        alternate = read_record(library, item.uid, sequence="alternate")
        assert alternate["attachment_count_observed"] == 1
    monkeypatch.setattr(library_mcp, "open_library", lambda root: (tmp_path, Library(path)))
    compact = library_mcp.call_tool(
        tmp_path, READ_TOOL, {"uid": item.uid, "sequence": "alternate", "compact": True}
    )
    assert compact["aco"] == 1 and compact["tyc"] == 9
    assert compact["at"][0]["ref"] == "file-alternate"


@pytest.mark.parametrize("attachments", [None, {}, ["bad"]])
def test_malformed_attachment_container_is_rejected(attachments):
    with pytest.raises(ValueError, match="attachment metadata is malformed"):
        message_metadata({"attachments": attachments})


def test_inaccessible_pointer_reports_unavailable_store(tmp_path, monkeypatch):
    pointer = tmp_path / "library-all" / "current.json"
    real_read = Path.read_text

    def deny(path, *args, **kwargs):
        if path == pointer:
            raise PermissionError("synthetic permission failure")
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny)
    with pytest.raises(ValueError, match="store unavailable"):
        open_library(tmp_path)


def test_store_disappearing_during_pointer_lookup_is_not_unpublished(tmp_path, monkeypatch):
    store = tmp_path / "mount"
    store.mkdir()

    def disappear(root):
        root.rmdir()

    monkeypatch.setattr(library_commands, "current_generation", disappear)
    with pytest.raises(ValueError, match="store unavailable"):
        open_library(store)
    assert not store.exists()


def test_file_cannot_be_a_library_store(tmp_path):
    path = tmp_path / "file"
    path.write_text("fixture")
    with pytest.raises(ValueError, match="not a directory"):
        open_library(path)
