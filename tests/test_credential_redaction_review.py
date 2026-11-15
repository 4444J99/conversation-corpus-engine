"""Review-round regressions for credential masking (PR 180 threads and structured-field gaps).

Every credential is synthetic and assembled from fragments, so nothing here is a real secret and
nothing trips a scanner.
"""

from __future__ import annotations

import time

import pytest

from conversation_corpus_engine.archive_model import (
    Conversation,
    Message,
    finalize_conversation,
)
from conversation_corpus_engine.credential_redaction import (
    MARKER,
    mask_conversation,
    mask_credentials,
)

V = "Hx7qT2mZ9pLw4" + "Kd8Vn3Rb5"  # 22-char mixed value
SHORT = "Qx9Lm2Va" + "7Pk4"  # 12 chars, letters and digits
ALPHA = "abcdefghij" + "klmnopqrstuvwxyz"  # 26 letters


def fullwidth(text: str) -> str:
    return "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in text)


# ---- text-level shapes -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("Authorization: Bearer " + ALPHA, ALPHA),
        ("Authorization: Bearer " + SHORT, SHORT),
        ("Authorization: Bearer " + "qwertyuiop", "qwertyuiop"),
        ("Authorization: Basic " + "dXNlcjpwYXNzd29yZA", "dXNlcjpwYXNzd29yZA"),
        ('{"Authorization":"Bearer ' + ALPHA + '"}', ALPHA),
        ("api key = " + V, V),
        ("API key: " + V, V),
        ('password="correct horse battery staple' + '123"', "horse battery"),
        ('passphrase="correct horse battery' + ' staple"', "horse battery"),
        ("api_key=`" + V + "`", V),  # allow-secret
        ("DATABASE_URL=postgresql://app:" + V + "@db.internal/appdb", V),
        ("postgres://app:p%40" + "ss" + "w0rdXyz1@db/x", "p%40ssw0rdXyz1"),
        ("postgres://app:%40" + V + "@db/x", V),
    ],
)
def test_text_shapes_are_masked(text: str, needle: str) -> None:
    masked = mask_credentials(text)
    assert MARKER in masked
    assert needle not in masked
    assert mask_credentials(masked) == masked


def test_urls_without_userinfo_keep_query_and_fragment() -> None:
    for text in (
        "https://host.example?next=user:" + V + "@other.example",
        "https://host.example/path#frag=user:" + V + "@other",
        "https://host.example/a?b=c:d@e",
    ):
        assert mask_credentials(text) == text


def test_ordinary_prose_and_metadata_names_survive() -> None:
    for text in (
        "the Bearer of bad news arrived",
        "Authorization: Token expired",
        "Authorization: Bearer token",
        "Authorization: Bearer <token>",
        "Authorization: Bearer YOUR_API_KEY",
        '"token_type": "Bearer"',
        "max_tokens = 4096",
        "sha256 " + "a" * 64,
        "set the password to something safe",
    ):
        assert mask_credentials(text) == text, text


def test_oversized_unterminated_pem_leaves_no_remainder() -> None:
    body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC" * 1500
    tail = "TAILMARKER" + "9" * 20
    text = "see\n-----BEGIN " + "PRIVATE KEY-----\n" + body + tail
    masked = mask_credentials(text)
    assert masked == "see\n" + MARKER
    assert tail not in masked


def test_encrypted_pem_headers_before_the_body_do_not_hide_it() -> None:
    text = (
        "-----BEGIN "
        + "RSA PRIVATE KEY-----\nProc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,0123456789ABCDEF\n\n"
        + "MIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX6Ppy\nQWERTYUIOPASDFGH"
    )
    masked = mask_credentials("keep " + text)
    assert masked == "keep " + MARKER


def test_pem_scan_stays_linear_on_hostile_input() -> None:
    hostile = ("-----BEGIN " + "PRIVATE KEY-----\nA: b\n") * 4000
    started = time.perf_counter()
    mask_credentials(hostile)
    assert time.perf_counter() - started < 5.0


# ---- unicode and encoding evasions ------------------------------------------------------


def test_zero_width_characters_inside_a_name_do_not_hide_the_value() -> None:
    text = "pass\u200bword=" + V + " tail"
    masked = mask_credentials(text)
    assert V not in masked
    assert masked == "pass\u200bword=" + MARKER + " tail"


def test_fullwidth_lookalike_prefixes_are_masked() -> None:
    key = fullwidth("sk-ant-" + "api03-" + "Ab3dEf6gHi9jKl2mNo5pQr8s")
    masked = mask_credentials("use " + key + " now")
    assert masked == "use " + MARKER + " now"
    named = fullwidth("api_key") + "=" + V
    assert V not in mask_credentials(named)


def test_cyrillic_lookalike_credential_names_are_masked() -> None:
    text = "раssword=" + V  # Cyrillic er and a
    masked = mask_credentials(text)
    assert V not in masked
    assert masked.endswith(MARKER)
    assert "аpi_key = " + V not in mask_credentials("аpi_key = " + V)


def test_percent_encoded_names_and_delimiters_are_masked() -> None:
    masked = mask_credentials("?api_key%3D" + V + "&x=1")
    assert V not in masked
    assert V not in mask_credentials("?api%5Fkey=" + V)


def test_masking_replaces_only_the_original_span() -> None:
    text = "café ½ api_key=" + V + " naïve"
    assert mask_credentials(text) == "café ½ api_key=" + MARKER + " naïve"


# ---- structured fields ------------------------------------------------------------------


def _conversation(**kwargs: object) -> Conversation:
    return Conversation(
        provider="chatgpt",
        source_id=str(kwargs.pop("source_id", "synthetic-1")),
        title="t",
        messages=list(kwargs.pop("messages", [Message(role="user", text="hi")])),  # type: ignore[call-overload]
        **kwargs,  # type: ignore[arg-type]
    )


def test_alternate_messages_are_masked() -> None:
    conversation = _conversation(
        extra={"alternate_messages": [{"role": "user", "text": "api_key=" + V, "meta": {}}]}
    )
    assert mask_conversation(conversation) >= 1
    assert V not in conversation.extra["alternate_messages"][0]["text"]


def test_message_meta_dictionary_keys_mask_their_values() -> None:
    message = Message(
        role="user",
        text="hi",
        meta={
            "api_key": V,
            "Authorization": "Bearer " + SHORT,
            "citations": ["https://u:" + V + "@h/x"],
        },
    )
    conversation = _conversation(messages=[message])
    mask_conversation(conversation)
    dumped = repr(message.meta)
    assert V not in dumped
    assert SHORT not in dumped
    assert message.meta["Authorization"].startswith("Bearer ")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("password", "hunter2" + "xyz"),
        ("Password", "plainwordsecret"),
        ("client_secret", "plainwordsecret"),
        ("accessToken", "plainwordsecret"),
        ("Authorization", "Bearer " + SHORT),
        ("Authorization", "plainwordsecret"),
        ("proxy-authorization", "Basic " + "dXNlcjpwYXNz"),
        ("рassword", "plainwordsecret"),
    ],
)
def test_top_level_extra_is_key_aware(key: str, value: str) -> None:
    conversation = _conversation(extra={key: value})
    assert mask_conversation(conversation) == 1
    assert value.rsplit(maxsplit=1)[-1] not in repr(conversation.extra)
    assert MARKER in conversation.extra[key]


def test_top_level_extra_leaves_ordinary_values() -> None:
    extra = {"token_type": "Bearer", "max_tokens": "4096", "password": "password", "note": "hello"}
    conversation = _conversation(extra=dict(extra))
    assert mask_conversation(conversation) == 0
    assert conversation.extra == extra


def test_provenance_is_key_aware() -> None:
    conversation = _conversation(provenance={"secret": "plainwordsecret"})
    mask_conversation(conversation)
    assert conversation.provenance["secret"] == MARKER


def test_fallback_id_is_derived_from_masked_text() -> None:
    def build(variant: str) -> Conversation:
        return Conversation(
            provider="chatgpt",
            source_id="",
            title="t",
            messages=[Message(role="user", text="deploy with api_key=" + variant)],
        )

    first, second = build(V), build("Zr4" + V[3:])
    finalize_conversation(first)
    finalize_conversation(second)
    assert first.source_id == second.source_id
    assert first.uid == second.uid
