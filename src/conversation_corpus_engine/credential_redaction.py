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
import sys
import unicodedata
from collections import Counter

MARKER = "[REDACTED-CREDENTIAL]"

_B64URL = r"[A-Za-z0-9_\-]"
_LEFT = r"(?<![A-Za-z0-9_\-])"
_RIGHT = r"(?![A-Za-z0-9_\-])"

# Whole-match shapes: the entire match is replaced by the marker.
_WHOLE: tuple[tuple[str, re.Pattern[str]], ...] = (
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
        # Needs a digit and a 16-character unbroken run so ``sk-learn-pipeline-2-stage-...`` stays readable.
        re.compile(
            _LEFT
            + r"sk-(?:proj-|svcacct-|admin-)?(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z0-9_]{16})"
            r"[A-Za-z0-9_\-]{20,}"
        ),
    ),
    ("stripe-key", re.compile(_LEFT + r"(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("stripe-webhook", re.compile(_LEFT + r"whsec_[A-Za-z0-9]{16,}")),
    ("slack-token", re.compile(_LEFT + r"xox[abeprs]-[A-Za-z0-9\-]{10,}")),
    ("slack-app-token", re.compile(_LEFT + r"xapp-\d-[A-Za-z0-9\-]{10,}")),
    ("npm-token", re.compile(_LEFT + r"npm_[A-Za-z0-9]{36}" + _RIGHT)),
    ("gitlab-pat", re.compile(_LEFT + r"glpat-[A-Za-z0-9_\-]{20,}")),
    ("huggingface-token", re.compile(_LEFT + r"hf_[A-Za-z0-9]{30,}")),
    (
        "sendgrid-key",
        re.compile(_LEFT + r"SG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}"),
    ),
    ("twilio-key", re.compile(_LEFT + r"SK[0-9a-f]{32}" + _RIGHT)),
    ("azure-account-key", re.compile(r"(?<=AccountKey=)[A-Za-z0-9+/]{40,}={0,2}")),
)

_PEM_BEGIN = re.compile(
    r"-----BEGIN\s+(?:[A-Z0-9]+\s+)*PRIVATE\s+KEY(?:\s+BLOCK)?-----", re.IGNORECASE
)
_PEM_END = re.compile(r"-----END\s+(?:[A-Z0-9]+\s+)*PRIVATE\s+KEY(?:\s+BLOCK)?-----", re.IGNORECASE)
# Key-like body after the BEGIN line. Encrypted blocks put ``Name: value`` header lines first.
_PEM_BODY = re.compile(r"(?:\s*[A-Za-z][A-Za-z0-9\-]*:[^\n]*\n)*\s*[A-Za-z0-9+/]{16}")


def _pem_spans(text: str) -> list[tuple[int, int]]:
    """Spans of private-key blocks, found in one linear pass.

    A block runs to its END line; an unterminated block that has key-like body runs to the end of
    the text, so no remainder survives. A bare header with no body (prose quoting the header) is
    left alone. Once no END exists after a position none exists later, so the scan never repeats.
    """
    spans: list[tuple[int, int]] = []
    pos = 0
    end_exhausted = False
    while (begin := _PEM_BEGIN.search(text, pos)) is not None:
        end = None if end_exhausted else _PEM_END.search(text, begin.end())
        if end is None:
            end_exhausted = True
            if _PEM_BODY.match(text, begin.end()) is None:
                pos = begin.end()
                continue
            spans.append((begin.start(), len(text)))
            break
        spans.append((begin.start(), end.end()))
        pos = end.end()
    return spans


# Value-only shapes: the named group is replaced, the rest is kept.
_BEARER = re.compile(
    r"(\bBearer\s+)(?P<value>(?=[A-Za-z0-9._~+/\-]*\d)[A-Za-z0-9._~+/\-]{20,}=*)", re.IGNORECASE
)
# Credentials in an Authorization header may be letters only (RFC 6750 b64token), so the header name
# is the anchor. Bearer and Basic values are masked whatever their shape unless they are a template
# word or placeholder; a Token value must be long or carry a non-letter, so "Authorization: Token
# expired" stays untouched.
_AUTH_HEADER = re.compile(
    r"(\bAuthorization[\"']?\s*:\s*[\"']?(?P<scheme>Bearer|Basic|Token)\s+)"
    r"(?P<value>[A-Za-z0-9._~+/_\-]+=*)",
    re.IGNORECASE,
)
# Passwords inside URI userinfo, e.g. a pasted database connection string. The value is greedy up to
# the last '@' of the authority, so a password that itself holds '@' is masked whole.
_URL_PASSWORD = re.compile(
    r"(?P<head>\b[A-Za-z][A-Za-z0-9+.\-]{0,31}://[^\s/:@?#\"'<>]+:)(?P<value>[^\s/?#\"'<>]+)(?P<tail>@)"
)
# Only template markers count as placeholders in a URL; "%40" is a percent-encoded character.
_URL_PLACEHOLDER = re.compile(r"^(?:\$|<|\{|%[A-Za-z_][A-Za-z0-9_]*%|\[REDACTED)")
_NAME = (
    r"[A-Za-z0-9_.\-]{0,40}(?:api[\s_\-]?key|secret[\s_\-]key|secret|token|passwd|password|passphrase"
    r"|pwd|(?<![A-Za-z0-9])pass|access[\s_\-]?key|private[\s_\-]?key|auth[\s_\-]?key|credential)"
    r"[A-Za-z0-9_.\-]{0,40}"
)
# A name that ends in one of these words labels metadata about a credential, not the credential.
_META_SUFFIX = re.compile(
    r"[_.\-](?:hash|id|name|file|path|dir|url|uri|type|count|length|len|version|ttl|expiry|expires"
    r"|expiration|endpoint)$",
    re.IGNORECASE,
)
_PATH_VALUE = re.compile(r"^(?:/|\./|\.\./|~/)")
_IDENT_VALUE = re.compile(
    r"^(?:[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+|[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)$"
)
_GENERIC = re.compile(
    r"(?P<head>(?<![A-Za-z0-9_])" + _NAME + r"[\"'`]?\s*[:=]\s*[\"'`]?)"
    r"(?P<value>[^\s\"'`,;<>(){}\[\]]{16,})",
    re.IGNORECASE,
)
# A quoted value may hold spaces and the other quote characters (a passphrase), so it runs to its
# own closing delimiter.
_GENERIC_QUOTED = re.compile(
    r"(?P<head>(?<![A-Za-z0-9_])" + _NAME + r"[\"'`]?\s*[:=]\s*(?P<quote>[\"'`]))"
    r"(?P<value>(?:\\.|(?!(?P=quote))[^\n\\]){8,})(?P=quote)",
    re.IGNORECASE,
)
# Words that mark a quoted value as a template rather than a passphrase.
_TEMPLATE_WORDS = frozenset(
    {
        "is",
        "was",
        "are",
        "were",
        "for",
        "this",
        "that",
        "and",
        "not",
        "with",
        "from",
        "has",
        "have",
        "had",
        "will",
        "expired",
        "invalid",
        "missing",
        "your",
        "enter",
        "paste",
        "here",
        "example",
        "placeholder",
        "password",
        "secret",
        "token",
        "key",
        "value",
        "insert",
        "replace",
        "type",
        "my",
        "the",
        "goes",
        "put",
        "add",
    }
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
    if _PATH_VALUE.match(value) or _IDENT_VALUE.match(value):
        return False
    if not (re.search(r"\d", value) and re.search(r"[A-Za-z]", value)):
        return False
    return _entropy(value) >= 3.0


def _head_names_metadata(head: str) -> bool:
    """Whether a matched ``name: `` head names metadata (``token_hash``, ``secret_name``)."""
    name = re.sub(r"[\s\"'`:=]+$", "", head)
    return _META_SUFFIX.search(name) is not None


def _quoted_value_is_secret(value: str) -> bool:
    if _generic_value_is_secret(value):
        return True
    if _PLACEHOLDER.match(value) or MARKER in value:
        return False
    words = value.split()
    return (
        len(words) >= 3
        and all(re.fullmatch(r"[A-Za-z]{3,}", word) for word in words)
        and not any(word.lower() in _TEMPLATE_WORDS for word in words)
        and _entropy(value) >= 3.0
    )


def _auth_value_is_secret(scheme: str, value: str) -> bool:
    bare = value.rstrip("=")
    if _PLACEHOLDER.match(bare) or MARKER in value or bare.lower() in _TEMPLATE_WORDS:
        return False
    if scheme.lower() in ("bearer", "basic"):
        return len(bare) >= 4
    long_enough = len(value) >= 20
    mixed = len(value) >= 8 and re.search(r"[\d._~+/=\-]", value) is not None
    return long_enough or mixed


# Cheap necessary condition for every rule above: most text carries none of these and returns early.
_TRIGGER = re.compile(
    r"AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA|AIPA|gh[pousr]_|github_pat_|AIza|ya29\.|AQ\.|sk-|sk_|rk_"
    r"|whsec_|xox|xapp-|npm_|glpat-|hf_|SG\.|sk[0-9a-f]{32}|eyJ|-----BEGIN|bearer|authorization|://|api|key|secret|token|pass|pwd|credential",
    re.IGNORECASE,
)


# Characters that look like ASCII letters but are not: Cyrillic and Greek homoglyphs. NFKC already
# folds full-width and compatibility forms; this table covers the confusables NFKC leaves alone.
_LOOKALIKES = str.maketrans(
    {
        "\u0430": "a",
        "\u0435": "e",
        "\u043e": "o",
        "\u0440": "p",
        "\u0441": "c",
        "\u0445": "x",
        "\u0443": "y",
        "\u0456": "i",
        "\u0455": "s",
        "\u0458": "j",
        "\u0501": "d",
        "\u051b": "q",
        "\u051d": "w",
        "\u0410": "A",
        "\u0412": "B",
        "\u0415": "E",
        "\u041a": "K",
        "\u041c": "M",
        "\u041d": "H",
        "\u041e": "O",
        "\u0420": "P",
        "\u0421": "C",
        "\u0422": "T",
        "\u0425": "X",
        "\u03bf": "o",
        "\u03bd": "v",
        "\u03c1": "p",
        "\u03b9": "i",
        "\u03ba": "k",
        "\u03c5": "u",
        "\u0391": "A",
        "\u0392": "B",
        "\u0395": "E",
        "\u039a": "K",
        "\u039c": "M",
        "\u039d": "N",
        "\u039f": "O",
        "\u03a1": "P",
        "\u03a4": "T",
        "\u03a7": "X",
    }
)
# Percent-escapes that stand for a name or delimiter character. Whitespace and '@' escapes stay
# encoded: they are data inside a value, not structure.
_PCT = re.compile(r"%([0-9A-Fa-f]{2})")
_PCT_DECODED = frozenset("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz=:_-.")
_PCT_DECODED_OR_PAD = _PCT_DECODED | {" ", "\t"}


_FORMAT_CHARS = re.compile(
    "["
    + "".join(chr(c) for c in range(sys.maxunicode + 1) if unicodedata.category(chr(c)) == "Cf")
    + "]"
)


def _needs_view(text: str) -> bool:
    """Whether detection needs a rewritten view: skips the per-character pass for ordinary text."""
    if _FORMAT_CHARS.search(text) is not None:
        return True
    if not text.isascii() and unicodedata.normalize("NFKC", text).translate(_LOOKALIKES) != text:
        return True
    return any(chr(int(m.group(1), 16)) in _PCT_DECODED_OR_PAD for m in _PCT.finditer(text))


def _fold(text: str) -> str:
    """Detection form of a short string (a dictionary key): normalised, lookalikes folded."""
    text = unicodedata.normalize("NFKC", text).translate(_LOOKALIKES)
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def _view(text: str) -> tuple[str, list[tuple[int, int]] | None]:
    """The detection view of `text` and, per view character, the original span it came from.

    Detection runs on NFKC-normalised text with invisible format characters dropped, lookalike
    letters folded and name/delimiter percent-escapes decoded; masking is applied to the original
    spans, so everything outside a credential is preserved byte for byte. Plain ASCII text without
    a percent sign is its own view (no map).
    """
    if not _needs_view(text):
        return text, None
    chars: list[str] = []
    spans: list[tuple[int, int]] = []
    i = 0
    size = len(text)
    while i < size:
        if text[i] == "%" and (match := _PCT.match(text, i)) is not None:
            decoded = chr(int(match.group(1), 16))
            # A space or tab right after a decoded ':' or '=' is the separator padding of "name: value".
            after_delimiter = bool(chars) and chars[-1] in ":=" and decoded in " \t"
            if decoded in _PCT_DECODED or after_delimiter:
                chars.append(decoded)
                spans.append((i, i + 3))
                i += 3
                continue
        for ch in unicodedata.normalize("NFKC", text[i]).translate(_LOOKALIKES):
            if unicodedata.category(ch) != "Cf":
                chars.append(ch)
                spans.append((i, i + 1))
        i += 1
    return "".join(chars), spans


def _credential_spans(text: str) -> list[tuple[int, int]]:
    """Every credential span of the detection view, as half-open (start, end) offsets."""
    spans: list[tuple[int, int]] = []
    if "-----begin" in text.lower():
        spans.extend(_pem_spans(text))
    for _name, pattern in _WHOLE:
        spans.extend(match.span() for match in pattern.finditer(text))
    for match in _AUTH_HEADER.finditer(text):
        if _auth_value_is_secret(match.group("scheme"), match.group("value")):
            spans.append(match.span("value"))
    spans.extend(match.span("value") for match in _BEARER.finditer(text))
    for match in _URL_PASSWORD.finditer(text):
        value = match.group("value")
        if not _URL_PLACEHOLDER.match(value) and MARKER not in value:
            spans.append(match.span("value"))
    for match in _GENERIC_QUOTED.finditer(text):
        if _quoted_value_is_secret(match.group("value")) and not _head_names_metadata(
            match.group("head")
        ):
            spans.append(match.span("value"))
    for match in _GENERIC.finditer(text):
        if _generic_value_is_secret(match.group("value")) and not _head_names_metadata(
            match.group("head")
        ):
            spans.append(match.span("value"))
    return spans


def mask_credentials(text: str) -> str:
    """Return `text` with every recognized credential replaced by `[REDACTED-CREDENTIAL]`."""
    if not text:
        return text
    view, origin = _view(text)
    if not _TRIGGER.search(view):
        return text
    spans = [span for span in _credential_spans(view) if span[0] < span[1]]
    if not spans:
        return text
    if origin is not None:
        spans = [(origin[start][0], origin[end - 1][1]) for start, end in spans]
    spans.sort()
    merged: list[list[int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    out: list[str] = []
    pos = 0
    for start, end in merged:
        out.append(text[pos:start])
        out.append(MARKER)
        pos = end
    out.append(text[pos:])
    return "".join(out)


def _typed_after_masking(text: str, typed: int | None) -> int:
    """The typed boundary of `text` once masked: the masked length of what was typed.

    `typed_chars` marks where the person's own typing ends (an attachment's text may follow), so it
    is recomputed from the masked typed prefix rather than kept as a stale offset.
    """
    if typed is None or typed >= len(text):
        return len(mask_credentials(text))
    return len(mask_credentials(text[: max(typed, 0)]))


def mask_conversation(conversation: object) -> int:
    """Mask a `Conversation` in place; return how many fields changed."""
    changed = 0

    def masked(value: str) -> str | None:
        result = mask_credentials(value)
        return result if result != value else None

    for name in ("title", "collection", "model"):
        value = getattr(conversation, name, "")
        if isinstance(value, str) and (result := masked(value)) is not None:
            setattr(conversation, name, result)
            changed += 1
    for message in getattr(conversation, "messages", []):
        if (result := masked(message.text)) is not None:
            message.typed_chars = _typed_after_masking(message.text, message.typed_chars)
            message.text = result
            changed += 1
        changed += _mask_json(message.meta)
        for attachment in message.attachments:
            if (result := masked(attachment.name)) is not None:
                attachment.name = result
                changed += 1
            if (result := masked(attachment.ref)) is not None:
                attachment.ref = result
                changed += 1
    extra = getattr(conversation, "extra", None)
    if isinstance(extra, dict):
        for key in list(extra):
            if key == "alternate_messages":
                for row in extra[key] if isinstance(extra[key], list) else []:
                    changed += _mask_message_dict(row)
            else:
                holder = {key: extra[key]}
                changed += _mask_json(holder)
                extra[key] = next(iter(holder.values()))
        changed += _mask_keys(extra)
    changed += _mask_json(getattr(conversation, "provenance", None))
    return changed


def _mask_message_dict(row: object) -> int:
    """Mask one serialized message (an off-branch alternate), keeping its typed boundary valid."""
    if not isinstance(row, dict):
        return 0
    changed = 0
    text = row.get("text")
    if isinstance(text, str):
        masked = mask_credentials(text)
        if masked != text:
            typed = row.get("typed_chars")
            row["typed_chars"] = _typed_after_masking(
                text, typed if isinstance(typed, int) and not isinstance(typed, bool) else None
            )
            row["text"] = masked
            changed += 1
    changed += _mask_json(row.get("meta"))
    for attachment in row.get("attachments") or []:
        if not isinstance(attachment, dict):
            continue
        for key in ("name", "ref"):
            value = attachment.get(key)
            if isinstance(value, str) and (masked := mask_credentials(value)) != value:
                attachment[key] = masked
                changed += 1
    return changed


# A dictionary key whose last word names a credential: the structure itself says the value is one.
_KEY_ENDS_IN_CREDENTIAL = re.compile(
    r"(?:api[\s_\-]?key|secret[\s_\-]?key|secret|token|passwd|password|passphrase"
    r"|access[\s_\-]?key|private[\s_\-]?key|auth[\s_\-]?key|credentials?|authorization)$",
    re.IGNORECASE,
)
_AUTHORIZATION_KEY = re.compile(r"(?:^|[\s_.\-])authorization$", re.IGNORECASE)
_NON_SECRET_WORDS = frozenset(
    {"bearer", "basic", "none", "null", "true", "false", "undefined", "redacted", "hidden", "n/a"}
)
_SCHEME_VALUE = re.compile(r"(?:Bearer|Basic|Token|Digest)\s", re.IGNORECASE)


def _named_value_is_secret(item: str) -> bool:
    """A string under a credential-named key: a single token of eight or more characters qualifies."""
    bare = item.strip()
    if MARKER in bare or _PLACEHOLDER.match(bare) or len(bare) < 8:
        return False
    if bare.lower() in _TEMPLATE_WORDS or bare.lower() in _NON_SECRET_WORDS:
        return False
    if re.search(r"\s", bare):
        return _quoted_value_is_secret(bare)
    return True


def _mask_keyed_string(key: object, item: str) -> str:
    """Mask one string by its dictionary key and its own content."""
    if isinstance(key, str) and len(key) <= 80:
        folded = _fold(key)
        if _KEY_ENDS_IN_CREDENTIAL.search(folded) is not None:
            if _AUTHORIZATION_KEY.search(folded) and _SCHEME_VALUE.match(item):
                headed = mask_credentials("Authorization: " + item)
                if headed.startswith("Authorization: "):
                    return headed[len("Authorization: ") :]
            if _named_value_is_secret(item):
                return MARKER
    return mask_credentials(item)


def _mask_keys(container: dict) -> int:
    """Rename dictionary keys that hold a credential; keep order, never drop a colliding value."""
    renames = {
        key: masked
        for key in container
        if isinstance(key, str) and (masked := mask_credentials(key)) != key
    }
    if not renames:
        return 0
    rebuilt: dict = {}
    for key, item in container.items():
        new = renames.get(key, key)
        base, n = new, 1
        while new in rebuilt:
            n += 1
            new = f"{base}#{n}"
        rebuilt[new] = item
    container.clear()
    container.update(rebuilt)
    return len(renames)


def _mask_json(value: object) -> int:
    """Mask every string inside a JSON-like container in place; return how many changed.

    A dictionary key that names a credential (``api_key``, ``password``, ``Authorization`` ...)
    marks its string value as one, because the value alone carries no trigger.
    """
    changed = 0
    if isinstance(value, dict):
        for key, item in list(value.items()):
            if isinstance(item, str):
                masked = _mask_keyed_string(key, item)
                if masked != item:
                    value[key] = masked
                    changed += 1
            else:
                changed += _mask_json(item)
        changed += _mask_keys(value)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, str):
                if (masked := mask_credentials(item)) != item:
                    value[index] = masked
                    changed += 1
            else:
                changed += _mask_json(item)
    return changed
