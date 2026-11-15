"""Mask pasted credentials before any text reaches a digest, an index or a shard.

People paste API keys, tokens and private keys into chats. Every reader feeds `Conversation`
objects through `assign_message_fields` and the library through `Library.put`; both call
`mask_conversation` first, so the secret never reaches a content digest, a full-text index, a
block store or an archive shard. The marker is fixed, so masking is idempotent: masked text
re-masks to itself and a second ingest creates no new revision.

Specific shapes (provider prefixes, JWTs, PEM blocks, Bearer tokens) are matched on their own.
The generic rule is anchored on a credential-like *name* (api key, secret, token, password ...),
never on shape alone, because a corpus is full of sha256 digests, git SHAs and UUIDs.
"""

from __future__ import annotations

import math
import re
from collections import Counter

MARKER = "[REDACTED-CREDENTIAL]"

_B64URL = r"[A-Za-z0-9_\-]"
_LEFT = r"(?<![A-Za-z0-9_\-])"
_RIGHT = r"(?![A-Za-z0-9_\-])"

# Whole-match shapes: the entire match is replaced by the marker.
_WHOLE: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "pem-private-key",
        re.compile(
            r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"
            r"(?:[\s\S]*?-----END (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"
            r"|[A-Za-z0-9+/=\s:,.\-]{0,16384})"
        ),
    ),
    (
        "jwt",
        re.compile(_LEFT + r"eyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    ),
    ("github-pat", re.compile(_LEFT + r"github_pat_[A-Za-z0-9_]{22,255}")),
    ("github-token", re.compile(_LEFT + r"gh[pousr]_[A-Za-z0-9]{36,255}")),
    (
        "aws-access-key-id",
        re.compile(_LEFT + r"(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA|AIPA)[A-Z0-9]{16}" + _RIGHT),
    ),
    ("google-api-key", re.compile(_LEFT + r"AIza[0-9A-Za-z_\-]{35}" + _RIGHT)),
    ("google-oauth-token", re.compile(_LEFT + r"ya29\.[0-9A-Za-z_\-]{20,}")),
    # Google's newer API-key and OAuth-style secrets: ``AQ.`` then a long base64url body.
    ("google-aq-key", re.compile(_LEFT + r"AQ\.[A-Za-z0-9_\-]{30,}")),
    ("anthropic-key", re.compile(_LEFT + r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    (
        "openai-key",
        # Needs a digit in the body so words such as ``sk-learn-pipeline-...`` stay readable.
        re.compile(
            _LEFT + r"sk-(?:proj-|svcacct-|admin-)?(?=[A-Za-z0-9_\-]*\d)[A-Za-z0-9_\-]{20,}"
        ),
    ),
    ("stripe-key", re.compile(_LEFT + r"(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("stripe-webhook", re.compile(_LEFT + r"whsec_[A-Za-z0-9]{16,}")),
    ("slack-token", re.compile(_LEFT + r"xox[abeprs]-[A-Za-z0-9\-]{10,}")),
    ("slack-app-token", re.compile(_LEFT + r"xapp-\d-[A-Za-z0-9\-]{10,}")),
)

# Value-only shapes: group 1 is kept, group 2 is replaced.
_BEARER = re.compile(
    r"(\bBearer\s+)((?=[A-Za-z0-9._~+/\-]*\d)[A-Za-z0-9._~+/\-]{20,}=*)", re.IGNORECASE
)
_NAME = (
    r"[A-Za-z0-9_.\-]*(?:api[_\-]?key|apikey|secret|token|passwd|password|passphrase"
    r"|access[_\-]?key|private[_\-]?key|client[_\-]?secret|auth[_\-]?key|credential)"
    r"[A-Za-z0-9_.\-]*"
)
_GENERIC = re.compile(
    r"(?P<head>(?<![A-Za-z0-9_])" + _NAME + r"[\"']?\s*[:=]\s*[\"']?)"
    r"(?P<value>[^\s\"'`,;<>(){}\[\]]{16,})",
    re.IGNORECASE,
)
_PLACEHOLDER = re.compile(
    r"^(?:\$|%|<|\{|\[REDACTED|x{4,}|\*{4,}|\.{3,}|your[_\-]|example|changeme|placeholder)",
    re.IGNORECASE,
)


def _entropy(value: str) -> float:
    counts = Counter(value)
    return -sum(n / len(value) * math.log2(n / len(value)) for n in counts.values())


def _generic_value_is_secret(value: str) -> bool:
    if _PLACEHOLDER.match(value) or MARKER in value:
        return False
    if not (re.search(r"\d", value) and re.search(r"[A-Za-z]", value)):
        return False
    return _entropy(value) >= 3.0


def _generic(match: re.Match[str]) -> str:
    value = match.group("value")
    return match.group("head") + MARKER if _generic_value_is_secret(value) else match.group(0)


def mask_credentials(text: str) -> str:
    """Return `text` with every recognized credential replaced by `[REDACTED-CREDENTIAL]`."""
    if not text:
        return text
    for _name, pattern in _WHOLE:
        text = pattern.sub(MARKER, text)
    text = _BEARER.sub(lambda m: m.group(1) + MARKER, text)
    return _GENERIC.sub(_generic, text)


def mask_conversation(conversation: object) -> int:
    """Mask a `Conversation` in place; return how many fields changed.

    `typed_chars` is the number of characters the person typed, so it is fixed from the original
    text before the text is masked.
    """
    changed = 0

    def masked(value: str) -> str | None:
        result = mask_credentials(value)
        return result if result != value else None

    title = getattr(conversation, "title", "")
    if isinstance(title, str) and (result := masked(title)) is not None:
        conversation.title = result  # type: ignore[attr-defined]
        changed += 1
    for message in getattr(conversation, "messages", []):
        if getattr(message, "typed_chars", None) is None:
            message.typed_chars = len(message.text)
        if (result := masked(message.text)) is not None:
            message.text = result
            changed += 1
        for attachment in message.attachments:
            if (result := masked(attachment.name)) is not None:
                attachment.name = result
                changed += 1
            if (result := masked(attachment.ref)) is not None:
                attachment.ref = result
                changed += 1
    return changed
