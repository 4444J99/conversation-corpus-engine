"""Stage D regressions for PR 180 second cold review (synthetic fragments only)."""

from __future__ import annotations

import pytest

from conversation_corpus_engine.credential_redaction import (
    MARKER,
    mask_conversation,
    mask_credentials,
)

RAND = "aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3zA5bC7d"  # 40 mixed chars, no separators
DISCORD = "MTIzNDU2Nzg5MDEy" + "MzQ1Njc4" + "." + "GhIjKl" + "." + "aBcDeFgHiJkLmNoPqRsTuVwXyZ12"
DOTTED = "correct.horse." + "battery.staple9"


@pytest.mark.parametrize(
    "text, secret",
    [
        ("password: /" + RAND, RAND),  # allow-secret
        ("password=./" + RAND, RAND),  # allow-secret
        ("secret_key = ~/" + RAND, RAND),  # allow-secret
        ("aws secret: /" + RAND[:20] + "/" + RAND[20:], RAND[:20]),  # allow-secret
        ("token: " + DISCORD, "GhIjKl"),  # allow-secret
        ("passphrase: " + DOTTED, "staple9"),  # allow-secret
        ("password: CORRECT_HORSE_9_BATTERY_STAPLE", "CORRECT_HORSE_9"),  # allow-secret
        ("api_key_id: " + RAND, RAND),  # allow-secret
        ("token_file=" + RAND, RAND),  # allow-secret
        ("secret_name: " + RAND, RAND),  # allow-secret
    ],
)
def test_real_values_masked(text: str, secret: str) -> None:  # allow-secret
    out = mask_credentials(text)
    assert secret not in out
    assert MARKER in out


@pytest.mark.parametrize(
    "text",
    [
        "token_path: /Users/someone/.config/service/token0123.json",  # allow-secret
        "password_file=./secrets/db_password_2024.txt",  # allow-secret
        "token_hash: " + "a1b2c3d4" * 8,  # allow-secret
        "api_key_id: 123e4567-e89b-12d3-a456-426614174000",  # allow-secret
        "token = settings.OPENAI_API_KEY2",  # allow-secret
        "secret_file=~/.ssh/id_ed25519_backup2",  # allow-secret
    ],
)
def test_paths_ids_and_code_references_stay(text: str) -> None:
    assert mask_credentials(text) == text


def test_list_under_credential_key_is_masked() -> None:
    class Conv:
        messages: list = []
        extra = {"api_keys": ["x"], "tokens": [RAND, "ok"], "password": [[RAND]]}
        provenance: dict = {}

    conv = Conv()
    mask_conversation(conv)
    assert RAND not in repr(conv.extra)
    assert "ok" in conv.extra["tokens"]
