"""Credential masking at ingest: every shape, lookalikes that must survive, and idempotence.

Every fixture is synthetic and assembled from fragments so no real or scanner-detectable secret
sits in the repository.
"""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import time
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
    assert conversation.messages[1].typed_chars == len(conversation.messages[1].text)
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
    assert first["typed_chars"] == len(first["text"])
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


def test_clean_alternate_messages_are_left_byte_identical() -> None:
    row = {"role": "user", "text": "plain", "branch": "alternate"}
    conversation = Conversation(
        provider="chatgpt",
        source_id="synthetic-alt-3",
        title="t",
        messages=[Message(role="user", text="hi")],
        extra={"alternate_messages": [dict(row)]},
    )
    assert mask_conversation(conversation) == 0
    assert conversation.extra["alternate_messages"] == [row]


def test_json_quoted_authorization_header_is_masked() -> None:
    text = '{"Authorization":"Bearer abcdefghijklmnopqrstuvwxyz"}'
    assert mask_credentials(text) == '{"Authorization":"Bearer ' + MARKER + '"}'
    text = "{'authorization': 'Basic Zm9vOmJhcg=='}"
    assert mask_credentials(text) == "{'authorization': 'Basic " + MARKER + "'}"


def test_quoted_passphrase_with_spaces_is_masked() -> None:
    for text in (
        'password="correct horse battery staple123"',
        "passphrase: 'orange fish 7 window seat'",
        '{"client_secret": "two words 4 you now"}',
    ):
        masked = mask_credentials(text)
        assert MARKER in masked, text
        assert mask_credentials(masked) == masked
    assert mask_credentials('password="enter your password here"') == (
        'password="enter your password here"'
    )
    assert (
        mask_credentials('token = "<paste token here 123>"') == 'token = "<paste token here 123>"'
    )


def test_url_userinfo_password_is_masked_and_plain_urls_survive() -> None:
    pw = "Qx9Lm2Va7Pk4Nr8Tu3Wy"
    assert mask_credentials(f"DATABASE_URL=postgresql://app:{pw}@db.internal/appdb") == (
        "DATABASE_URL=postgresql://app:" + MARKER + "@db.internal/appdb"
    )
    assert mask_credentials(f"git clone https://bot:{pw}@host.example/r.git") == (
        "git clone https://bot:" + MARKER + "@host.example/r.git"
    )
    for text in (
        "https://user@host.example/path",
        "http://host.example:8080/path?x=1",
        "postgresql://app:${DB_PASSWORD}@db/appdb",
        "ssh://git@host.example:2222/repo.git",
        "see https://example.com/a:b@c for details",
    ):
        assert mask_credentials(text) == text


def test_url_userinfo_does_not_cross_query_or_fragment_and_keeps_percent_encoding_secret() -> None:
    for text in (
        "https://host.example?next=user:pass1234word@other.example",
        "https://host.example#frag=user:pass1234word@other.example",
    ):
        assert mask_credentials(text) == text
    assert mask_credentials("postgres://app:%40Qx9Lm2Va7Pk4@db/x") == (
        "postgres://app:" + MARKER + "@db/x"
    )
    assert (
        mask_credentials("postgres://app:%DB_PASSWORD%@db/x") == "postgres://app:%DB_PASSWORD%@db/x"
    )


def test_quoted_values_may_contain_the_other_quote_and_backticks_delimit() -> None:
    assert mask_credentials('password="don\'t share code 7391"') == 'password="' + MARKER + '"'
    assert mask_credentials("password='say \"hi\" 4 me now'") == "password='" + MARKER + "'"
    ticks = "`"
    for spacing in ("=", " = "):
        text = "api" + "_key" + spacing + ticks + "q8Zt3Lm0Xv2Pb7Rn5Wc1" + ticks
        assert mask_credentials(text) == "api_key" + spacing + ticks + MARKER + ticks


def test_alphabetic_passphrases_are_masked_but_templates_survive() -> None:
    assert mask_credentials('passphrase="correct horse battery staple"') == (
        'passphrase="' + MARKER + '"'
    )
    for text in (
        'password="enter your password here"',
        'secret="paste the secret value here"',
        'token = "your token goes here"',
        'password="changeme please"',
    ):
        assert mask_credentials(text) == text


def test_message_meta_strings_are_masked_in_messages_and_alternates() -> None:
    url = "https://bot:" + "Qx9Lm2Va7Pk4Nr8Tu3Wy" + "@host.example/r"
    message = Message(role="assistant", text="x", meta={"citations": [{"url": url}], "n": 3})
    conversation = Conversation(
        provider="copilotcli",
        source_id="synthetic-meta",
        title="t",
        messages=[message],
        extra={"alternate_messages": [{"role": "user", "text": "p", "meta": {"u": url}}]},
    )
    assert mask_conversation(conversation) == 2
    assert message.meta == {
        "citations": [{"url": "https://bot:" + MARKER + "@host.example/r"}],
        "n": 3,
    }
    assert conversation.extra["alternate_messages"][0]["meta"]["u"].count(MARKER) == 1
    assert mask_conversation(conversation) == 0


def test_typed_boundary_follows_the_masked_text() -> None:
    typed = "use api_key=" + "q8Zt3Lm0Xv2Pb7Rn5Wc1" + " now"
    attachment_text = "\n[attachment] notes"
    message = Message(role="user", text=typed + attachment_text, typed_chars=len(typed))
    conversation = Conversation(
        provider="gemini", source_id="synthetic-typed", title="t", messages=[message]
    )
    assert mask_conversation(conversation) == 1
    assert message.text == "use api_key=" + MARKER + " now" + attachment_text
    assert message.typed_chars == len("use api_key=" + MARKER + " now")
    assert message.text[message.typed_chars :] == attachment_text


def test_collection_extra_and_provenance_strings_are_masked() -> None:
    url = "https://bot:" + "Qx9Lm2Va7Pk4Nr8Tu3Wy" + "@host.example/r"
    conversation = Conversation(
        provider="claude",
        source_id="synthetic-extra",
        title="t",
        collection="c " + POSITIVE["github-ghp"],
        messages=[Message(role="user", text="hi")],
        provenance={"source_url": url},
        extra={"system_prompt": POSITIVE["jules-aq"], "nested": {"k": [url]}, "n": 1},
    )
    assert mask_conversation(conversation) == 4
    dump = repr(conversation.to_dict())
    assert B[:8] not in dump and "Qx9Lm2Va7" not in dump and "a1B2c3D4e5" not in dump
    assert mask_conversation(conversation) == 0


@pytest.mark.parametrize("filler", ["-", ".", "a-", "a.", "-a"])
def test_long_separator_runs_do_not_blow_up(filler: str) -> None:
    started = time.monotonic()
    text = "key " + filler * 150_000 + " token"
    assert mask_credentials(text) == text
    assert time.monotonic() - started < 5


def test_prose_lookalikes_survive() -> None:
    for text in (
        "Authorization: Token expired",
        "Authorization: Bearer of bad news",
        'the token: "was invalid for this request"',
        "secret = 'meeting notes from last tuesday'",
        "the file starts with -----BEGIN " + "PRIVATE KEY----- then base64 data follows",
        "plain text with no trigger words at all",
    ):
        assert mask_credentials(text) == text


def test_quoted_value_with_escaped_quote_is_masked_to_the_real_closing_quote() -> None:
    text = r'{"password":"Qx9Lm2Va7Pk4\"Nr8Tu3Wy5Az6"}'
    masked = mask_credentials(text)
    assert masked == '{"password":"' + MARKER + '"}'
    assert "Nr8Tu3Wy5Az6" not in masked
    assert mask_credentials(masked) == masked


def test_credential_named_keys_mask_their_values_in_metadata_extra_and_provenance() -> None:
    value = "q8Zt3Lm0Xv2Pb7Rn5Wc1"
    message = Message(role="user", text="x", meta={"api_key": value, "note": "keep me", "n": 1})
    conversation = Conversation(
        provider="claude",
        source_id="synthetic-keys",
        title="t",
        messages=[message],
        provenance={"client_secret": value},
        extra={"auth": {"password": value, "user": "alice"}, "alternate_messages": []},
    )
    assert mask_conversation(conversation) == 3
    assert message.meta == {"api_key": MARKER, "note": "keep me", "n": 1}
    assert conversation.provenance == {"client_secret": MARKER}
    assert conversation.extra["auth"] == {"password": MARKER, "user": "alice"}
    assert mask_conversation(conversation) == 0
    short = Message(role="user", text="x", meta={"token": "short1", "keyword": value})
    clean = Conversation(provider="claude", source_id="s", title="t", messages=[short])
    assert mask_conversation(clean) == 0
