"""Stage C regressions for PR 180 cold-review findings P2-1 to P2-3 (synthetic fragments only)."""

from __future__ import annotations

import pytest

from conversation_corpus_engine.credential_redaction import (
    MARKER,
    mask_conversation,
    mask_credentials,
)

BODY = "Hx7qT2mZ9pLw4" + "Kd8Vn3Rb5Zc1Yu6Ts0QaWe"  # 35 mixed chars
HEX32 = "a1b2c3d4" * 4


# ---- P2-1: dictionary keys ---------------------------------------------------------------


class _Conv:
    def __init__(self) -> None:
        self.messages: list[object] = []
        self.extra: dict[str, object] = {}
        self.provenance: dict[str, object] = {}


def test_secret_used_as_a_dict_key_is_masked_in_extra_and_provenance() -> None:
    secret = "sk-ant-" + BODY  # allow-secret
    conv = _Conv()
    conv.extra = {secret: "x", "nested": {secret: "y"}}  # allow-secret
    conv.provenance = {secret: 1}  # allow-secret
    mask_conversation(conv)
    assert secret not in repr(conv.extra) + repr(conv.provenance)
    assert MARKER in next(iter(conv.provenance))


def test_colliding_masked_keys_do_not_lose_values() -> None:
    conv = _Conv()
    conv.provenance = {"sk-ant-" + BODY: 1, "sk-ant-" + BODY[::-1]: 2}
    mask_conversation(conv)
    assert sorted(conv.provenance.values()) == [1, 2]


# ---- P2-2: provider shapes and names -----------------------------------------------------


@pytest.mark.parametrize(
    "secret",
    [
        "np" + "m_" + "Ab3dEf6hIj9lMn2pQr5tUv8xYz1BcD4fGh7J",
        "glp" + "at-" + "Ab3dEf6hIj9lMn2pQr5t",
        "hf" + "_" + "AbCdEfGhIjKlMnOpQrStUvWxYzAbCdEfGh",
        "S" + "G." + "Ab3dEf6hIj9lMn2pQr5tUv" + "." + "Ab3dEf6hIj9lMn2pQr5tUv8xYz1BcD4fGh7",
        "S" + "K" + HEX32,
    ],
)
def test_more_provider_shapes_are_masked(secret: str) -> None:  # allow-secret
    out = mask_credentials(f"here it is {secret} thanks")
    assert secret not in out
    assert MARKER in out


def test_azure_account_key_is_masked() -> None:
    key = "Ab3dEf6hIj9lMn2pQr5tUv8xYz1BcD4fGh7JkL0mNo2PqRs5TuVwXyZ" + "0123456789=="
    out = mask_credentials("DefaultEndpointsProtocol=https;AccountKey=" + key + ";EndpointSuffix=x")
    assert key not in out


@pytest.mark.parametrize("name", ["pwd", "pass", "db_pass", "DB_PWD"])
def test_pwd_and_pass_names_are_credential_names(name: str) -> None:
    value = "Qx9Lm2Va7Pk4Zt8Rw3Yn6"
    assert value not in mask_credentials(f"{name}={value}")


def test_lowercase_and_double_space_pem_begin_lines_are_masked() -> None:
    body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC" * 2
    for begin, end in (
        ("-----begin private key-----", "-----end private key-----"),
        ("-----BEGIN  RSA  PRIVATE KEY-----", "-----END  RSA  PRIVATE KEY-----"),
    ):
        out = mask_credentials(f"{begin}\n{body}\n{end}\nafter")
        assert body not in out
        assert out.endswith("after")


# ---- P2-3: false positives under credential-like names -----------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "token_hash: " + "9f2c7e1ab4d84c5e8a6b3d0f1e2a7c94",
        "api_key_hash=" + "9f2c7e1ab4d84c5e8a6b3d0f1e2a7c94",
        "api_key_id: 123e4567-e89b-12d3-a456-426614174000",
        "secret_name: projects/my-project/secrets/db-password-v2",
        "TOKEN_FILE=/var/run/secrets/kubernetes.io/token2",
        "token: process.env.STRIPE_SECRET_V2",  # allow-secret
        "api_key=settings.providers.openai_v2",  # allow-secret
        "model sk-learn-pipeline-2-stage-processing runs",
    ],
)
def test_identifiers_hashes_and_paths_survive(text: str) -> None:
    assert mask_credentials(text) == text


def test_real_secrets_next_to_those_names_still_mask() -> None:
    assert BODY not in mask_credentials("api_key: " + BODY)
    assert BODY not in mask_credentials("sk-" + "proj-" + BODY)
