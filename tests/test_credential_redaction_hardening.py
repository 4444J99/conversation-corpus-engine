"""Deferred P3 hardening from the PR 180 review (synthetic fragments only)."""

from __future__ import annotations

import pytest

from conversation_corpus_engine.credential_redaction import (
    MARKER,
    mask_conversation,
    mask_credentials,
)

# Dash-segmented, every segment under 16 characters: no unbroken run for the legacy shape rule.
SEGMENTED = "Ab3dEf6gHi" + "-" + "9jKl2mNo5p" + "-" + "Qr8sT1uVwX" + "-" + "y4Zc7Bn0"
SLACK_TAIL = "Ab3dEf6gHi9jKl2mNo5pQr8s"  # 24 mixed characters


def _slack_url(tail: str = SLACK_TAIL) -> str:
    return "https://hooks." + "slack.com/services/" + "T0123ABCD" + "/" + "B0456EFGH" + "/" + tail


@pytest.mark.parametrize("prefix", ["sk-", "sk-proj-"])
def test_dash_segmented_openai_key_is_masked(prefix: str) -> None:
    secret = prefix + SEGMENTED  # allow-secret
    out = mask_credentials(f"my key is {secret} ok")
    assert out == f"my key is {MARKER} ok"


@pytest.mark.parametrize(
    "text",
    [
        "model sk-learn-pipeline-2-stage-processing runs",
        "sk-Learn-Pipeline-Stage-Processing-Run-Only",  # mixed case but no digit
        "sk-learn-pipeline-2-stage-processing-with-many-more-words",  # digit, no uppercase
        "sk-Ab3dEf6g-short",  # mixed case and digit but under 32 characters
    ],
)
def test_ordinary_sk_slugs_survive(text: str) -> None:
    assert mask_credentials(text) == text


def test_slack_webhook_url_final_segment_is_masked() -> None:
    url = _slack_url()
    out = mask_credentials(f"post to {url} please")
    assert SLACK_TAIL not in out
    assert out == f"post to {url.replace(SLACK_TAIL, MARKER)} please"
    assert "T0123ABCD/B0456EFGH" in out


def test_slack_webhook_url_under_a_webhook_key_is_masked() -> None:
    class Conv:
        def __init__(self) -> None:
            self.messages: list[object] = []
            self.extra: dict[str, object] = {"webhook_url": _slack_url()}
            self.provenance: dict[str, object] = {}

    conv = Conv()
    assert mask_conversation(conv) == 1
    assert SLACK_TAIL not in repr(conv.extra)
    assert MARKER in str(conv.extra["webhook_url"])


@pytest.mark.parametrize(
    "tail", ["XXXXXXXXXXXXXXXXXXXXXXXX", "x" * 24, "<your-webhook-token-here>", "{token}"]
)
def test_slack_webhook_placeholders_survive(tail: str) -> None:
    text = _slack_url(tail)
    assert mask_credentials(text) == text


def test_new_rules_are_idempotent() -> None:
    text = f"a sk-{SEGMENTED} b {_slack_url()}"
    once = mask_credentials(text)
    assert mask_credentials(once) == once
    assert SEGMENTED not in once
    assert SLACK_TAIL not in once
