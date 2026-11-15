"""Credential masking at ingest: every shape, lookalikes that must survive, and idempotence.

Every fixture is synthetic and assembled from fragments so no real or scanner-detectable secret
sits in the repository.
"""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import uuid
import zlib
from pathlib import Path

import pytest

from conversation_corpus_engine.archive_model import (
    Attachment,
    Conversation,
    Message,
    assign_message_fields,
    finalize_conversation,
)
from conversation_corpus_engine.credential_redaction import (
    MARKER,
    mask_conversation,
    mask_credentials,
)
from conversation_corpus_engine.library_store import Library

B = "Ab8RNx0q4Z" + "k2Lm9PvT3" + "yUw7Hc5Jd1" + "Fe6Gs_-Qa8" + "Xo2Vb4N"  # 46 chars of base64url

POSITIVE = {
    "jules-aq": 'JULES_API_KEY="AQ.' + B + '"',
    "aq-bare": "key is AQ." + B + " ok",
    "aws-akia": "id AKIA" + "IOSFODNN7" + "EXAMPLE",
    "aws-asia": "id ASIA" + "ZX4K9Q2M7" + "TT5R8WL",
    "github-ghp": "token ghp_" + "a1B2c3D4e5" * 3 + "F6g7H8",
    "github-pat": "github_pat_" + "11ABCDEFG0" + "abcdefghij_KLMNOPQRST" + "uvwxyz0123",
    "google-aiza": "AIza" + "Sy" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q",
    "google-ya29": "ya29." + "a0AfB_byC1d2E3f4G5h6I7j8K9",
    "openai": "sk-" + "proj-" + "Ab3dEf6gHi9jKl2mNo5pQr8sT",
    "openai-legacy": "sk-" + "Ab3dEf6gHi9jKl2mNo5pQr8sT1uVw",
    "anthropic": "sk-ant-" + "api03-" + "Ab3dEf6gHi9jKl2mNo5pQr8s",
    "stripe-sk": "sk_" + "live_" + "4eC39HqLyjWDarjtT1zdp7dc",
    "stripe-rk": "rk_" + "test_" + "4eC39HqLyjWDarjtT1zdp7dc",
    "stripe-whsec": "whsec_" + "4eC39HqLyjWDarjtT1zdp7dc",
    "slack-xoxb": "xox" + "b-" + "123456789012-1234567890123-AbCdEfGhIjKlMnOp",
    "slack-xapp": "xapp" + "-1-A0123456789-1234567890-abcdef0123456789",
    "jwt": "eyJ"
    + "hbGciOiJIUzI1NiJ9."
    + "eyJ"
    + "zdWIiOiIxMjM0NTY3ODkwIn0."
    + "SflKxwRJSMeKKF2QT4fw",
    "pem": "-----BEGIN "
    + "RSA PRIVATE KEY-----\nMIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX6Ppy\n-----END RSA PRIVATE KEY-----",
    "pem-openssh-open": "-----BEGIN "
    + "OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmU=\n",
    "bearer": "Authorization: Bearer " + "abc123DEF456ghi789JKL012mno",
    "generic-api-key": "api_key = " + "q8Zt3Lm0Xv2Pb7Rn5Wc1",
    "generic-secret-colon": 'client_secret: "' + "Zx9Qw3Er5Ty7Ui1Op4As6Df" + '"',
    "generic-password": "DB_PASSWORD=" + "Hx7qT2mZ9pLw4Kd8Vn3",
}


@pytest.mark.parametrize("label", sorted(POSITIVE))
def test_every_shape_is_masked(label: str) -> None:
    text = f"before {POSITIVE[label]} after"
    masked = mask_credentials(text)
    assert MARKER in masked, label
    assert masked.startswith("before ")
    # The distinctive body of the fixture must be gone.
    body = POSITIVE[label].split("=")[-1].strip("\"' ")
    assert body[8:24] not in masked or len(body) < 24 or label.startswith("pem"), label
    assert mask_credentials(masked) == masked, f"not idempotent: {label}"


def test_jules_shape_keeps_the_name_and_masks_only_the_value() -> None:
    masked = mask_credentials(POSITIVE["jules-aq"])
    assert masked == 'JULES_API_KEY="' + MARKER + '"'
    assert B[:12] not in masked


def test_pem_block_is_masked_across_lines_and_unterminated_block_is_cut() -> None:
    assert mask_credentials("x\n" + POSITIVE["pem"] + "\ny") == "x\n" + MARKER + "\ny"
    masked = mask_credentials("a " + POSITIVE["pem-openssh-open"])
    assert masked == "a " + MARKER
    assert "b3BlbnNz" not in masked


def test_bearer_keeps_scheme_word() -> None:
    assert mask_credentials(POSITIVE["bearer"]) == "Authorization: Bearer " + MARKER


NEGATIVE = [
    "sha256: " + hashlib.sha256(b"x").hexdigest(),
    "commit " + "9fceb02d0ae598e95dc970b74767f19372d61af8",
    "uuid " + str(uuid.UUID(int=0x1234567890ABCDEF1234567890ABCDEF)),
    "checksum=" + hashlib.sha256(b"y").hexdigest(),
    "pip install scikit-learn sk-learn-pipeline-example-description",
    "the task-force-planning-group-mid-2026 met",
    "risk-assessment-framework-for-vendors-2026",
    "pk_" + "live_" + "4eC39HqLyjWDarjtT1zdp7dc",
    "AKIA" + "IOSFODNN7" + "EXAMPLE" + "TOOLONG",
    "Bearer of bad news and the long road to the capital city",
    "api_key=<your-api-key-goes-here-123>",  # allow-secret
    "export TOKEN=${GITHUB_TOKEN_FROM_ENVIRONMENT_1}",
    "password=short1",  # allow-secret
    "secret = xxxxxxxxxxxxxxxxxxxx",  # allow-secret
    "token: aaaaaaaaaaaaaaaaaaaa",  # allow-secret
    "eyJ" + "hbGciOiJIUzI1NiJ9 is a header prefix without dots",
    "ghp_ is the GitHub prefix",
    "The AIza prefix is documented; AIzaShort",
    "ya29 tokens are short lived",
    "-----BEGIN CERTIFICATE-----\nMIIBsjCCARugAwIBAgI=\n-----END CERTIFICATE-----",
    "-----BEGIN PUBLIC KEY-----\nMFwwDQYJKoZIhvcNAQEBBQADSwAwSAJBAKj3\n-----END PUBLIC KEY-----",
    "keyboard layout: qwerty-asdfgh-zxcvbn",
    "monkey_business=1234567890abcdef1234",
]


@pytest.mark.parametrize("text", NEGATIVE)
def test_lookalikes_survive_untouched(text: str) -> None:
    assert mask_credentials(text) == text


def test_marker_is_a_fixed_point_and_empty_is_safe() -> None:
    assert mask_credentials("") == ""
    assert mask_credentials(MARKER) == MARKER
    assert mask_credentials("api_key=" + MARKER) == "api_key=" + MARKER


def test_several_secrets_in_one_text() -> None:
    text = f"{POSITIVE['github-ghp']} and {POSITIVE['aws-akia']}\nAPI_KEY={B}"
    masked = mask_credentials(text)
    assert masked.count(MARKER) == 3
    assert mask_credentials(masked) == masked


def _conversation(text: str) -> Conversation:
    return Conversation(
        provider="gemini",
        source_id="synthetic-1",
        title="paste " + POSITIVE["github-ghp"],
        messages=[
            Message(role="user", text="plain words"),
            Message(
                role="user",
                text=text,
                attachments=[Attachment(name="n", ref="https://h/?k=" + POSITIVE["google-aiza"])],
            ),
        ],
    )


def test_mask_conversation_masks_text_title_attachments_and_keeps_typed_chars() -> None:
    text = POSITIVE["jules-aq"]
    conversation = _conversation(text)
    changed = mask_conversation(conversation)
    assert changed == 3
    assert conversation.messages[0].text == "plain words"
    assert conversation.messages[1].text == 'JULES_API_KEY="' + MARKER + '"'
    assert conversation.messages[1].typed_chars == len(text)
    assert MARKER in conversation.title
    assert MARKER in conversation.messages[1].attachments[0].ref
    assert mask_conversation(conversation) == 0


def test_assign_message_fields_masks_before_minting_ids_and_is_idempotent() -> None:
    conversation = _conversation(POSITIVE["jules-aq"])
    assign_message_fields(conversation)
    first = conversation.to_dict()
    assert B[:12] not in repr(first)
    assign_message_fields(conversation)
    assert conversation.to_dict() == first


def test_library_put_never_stores_the_secret_and_second_put_is_unchanged() -> None:
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "library.sqlite"
        with Library.create(path) as library:
            conversation = _conversation(POSITIVE["jules-aq"])
            first = library.put(conversation)
            assert first["changed"]
            second = library.put(_conversation(POSITIVE["jules-aq"]))
            assert not second["changed"]
            assert library.verify() == []
        needle = B[:16].encode()
        assert needle not in path.read_bytes()
        connection = sqlite3.connect(path)
        try:
            for (payload,) in connection.execute("SELECT payload FROM objects"):
                assert needle not in zlib.decompress(payload)
            for row in connection.execute("SELECT * FROM record_fts_content"):
                assert needle not in repr(row).encode()
        finally:
            connection.close()


def test_authorization_header_masks_short_and_letters_only_tokens() -> None:
    for token in ("mF_9.B5f-4.1JqM", "abcdefghijklmnopqrstuvwxyz", "Zm9vOmJhcg=="):
        for scheme in ("Bearer", "Basic"):
            text = f"Authorization: {scheme} {token}"
            assert mask_credentials(text) == f"Authorization: {scheme} {MARKER}"
    assert mask_credentials("Authorization: none") == "Authorization: none"


def test_unterminated_pem_block_is_consumed_to_the_end_however_long() -> None:
    body = ("MIIEvQIBADANBgkqhkiG9w0BAQEFAASC" * 2) + "\n"
    long_text = "head\n-----BEGIN " + "PRIVATE KEY-----\n" + body * 700 + "tail-after-key"
    assert len(long_text) > 40_000
    assert mask_credentials(long_text) == "head\n" + MARKER


def test_labels_with_spaces_are_recognized() -> None:
    value = "q8Zt3Lm0Xv2Pb7Rn5Wc1"
    for label in ("API key", "api key", "Secret Key", "private key", "access key"):
        assert mask_credentials(f"{label} = {value}") == f"{label} = {MARKER}"
        assert mask_credentials(f"{label}: {value}") == f"{label}: {MARKER}"
    assert mask_credentials("the api key rotates monthly") == "the api key rotates monthly"


def test_alternate_messages_are_masked_with_their_attachments_and_typed_chars() -> None:
    text = POSITIVE["github-ghp"]
    conversation = Conversation(
        provider="chatgpt",
        source_id="synthetic-alt",
        title="t",
        messages=[Message(role="user", text="hi")],
        extra={
            "alternate_messages": [
                {
                    "role": "user",
                    "text": text,
                    "attachments": [{"name": "n", "ref": "u?k=" + POSITIVE["google-aiza"]}],
                    "branch": "alternate",
                },
                {"role": "user", "text": "plain"},
            ]
        },
    )
    assert mask_conversation(conversation) == 2
    first, second = conversation.extra["alternate_messages"]
    assert first["text"] == "token " + MARKER
    assert first["typed_chars"] == len(text)
    assert MARKER in first["attachments"][0]["ref"]
    assert second["text"] == "plain"
    assert mask_conversation(conversation) == 0
    assert B[:8] not in repr(conversation.to_dict())


def test_library_put_masks_alternate_messages_in_object_and_fts() -> None:
    secret_text = POSITIVE["jules-aq"]
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "library.sqlite"
        with Library.create(path) as library:
            conversation = Conversation(
                provider="chatgpt",
                source_id="synthetic-alt-2",
                title="t",
                messages=[Message(role="user", text="hi")],
                extra={"alternate_messages": [{"role": "user", "text": secret_text}]},
            )
            library.put(conversation)
            assert library.verify() == []
        needle = B[:16].encode()
        assert needle not in path.read_bytes()
        connection = sqlite3.connect(path)
        try:
            for (payload,) in connection.execute("SELECT payload FROM objects"):
                assert needle not in zlib.decompress(payload)
            for row in connection.execute("SELECT * FROM record_fts_content"):
                assert needle not in repr(row).encode()
        finally:
            connection.close()


def test_derived_source_id_ignores_the_credential_value() -> None:
    def build(secret: str) -> Conversation:  # allow-secret
        return Conversation(
            provider="gemini",
            source_id="",
            title="t",
            created="2026-01-01T00:00:00Z",
            messages=[Message(role="user", text="deploy with key " + secret)],
        )

    first = build("AIza" + "Sy" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q")
    second = build("AIza" + "Sy" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J")
    finalize_conversation(first)
    finalize_conversation(second)
    assert first.source_id == second.source_id
    assert first.uid == second.uid
