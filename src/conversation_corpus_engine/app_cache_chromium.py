"""Read-only reader for Chromium "simple cache" entries (version 5).

Desktop apps built on Chromium (the ChatGPT/Codex app among them) keep every HTTP response they
fetched as one file per entry, named ``<16 hex>_0``. The responses of an app the person is signed
in to hold that person's own data. This module recovers those bytes from the files alone: it never
opens a network connection, never reads cookies, tokens or any credential store, and opens every
cache file for reading only.

Entry layout (the same layout titan's ``chatgpt-app-cache.py`` documents and checks):

  header      24 B  u64 initial magic, u32 version, u32 key length, u32 key hash, u32 pad
  key               e.g. ``1/0/_dk_<site> <site> <url>``; the resource is the LAST ``http(s)://`` token
  body              stream 1: the response body exactly as stored (still content-encoded)
  EOF1        24 B  u64 final magic, u32 flags, u32 crc32(body), u32 stream size, u32 pad
  stream 0          the pickled response info; the HTTP header block follows a ``HTTP/x NNN`` line
  key sha256  32 B  present when EOF0 flags & 2
  EOF0        24 B  same record; its stream size is the length of stream 0

A body is returned only after its CRC32 matches EOF1. ``gzip``/``deflate`` decode with the standard
library; ``br`` decodes through the ``brotli`` module or the ``brotli`` command when one exists, and
is otherwise reported as ``unsupported:br`` - never guessed.
"""

from __future__ import annotations

import gzip
import hashlib
import importlib
import re
import shutil
import struct
import subprocess
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

INITIAL_MAGIC = 0xFCFB6D1BA7725C30
FINAL_MAGIC = 0xF4FA6F45970D41D8
SUPPORTED_VERSION = 5
RECORD = struct.Struct("<QIIII")
FLAG_HAS_CRC32 = 1
FLAG_HAS_KEY_SHA256 = 2
MAX_KEY_BYTES = 1 << 20
MAX_STREAM0_BYTES = 4 << 20
MAX_BODY_BYTES = 256 << 20
MAX_DECODED_BYTES = 1 << 30
ENTRY_NAME = re.compile(r"^[0-9a-f]{16}_0$")
URL_TOKEN = re.compile(r"https?://\S+")
STATUS_LINE = re.compile(rb"HTTP/\d(?:\.\d)? (\d{3})")


class EntryFault(Exception):
    """One cache entry cannot be trusted; it is skipped with this reason."""


class BodyUnavailable(Exception):
    """A body exists but cannot be returned (corrupt, or an encoding that cannot be decoded here)."""


@dataclass(frozen=True)
class CacheEntry:
    path: Path
    key: str
    url: str
    status: int | None
    content_type: str
    encodings: tuple[str, ...]
    served: str
    body_start: int
    body_len: int
    crc_expected: int | None = None
    problems: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Skipped:
    path: Path
    reason: str


def strip_query(url: str) -> str:
    """Drop userinfo, query and fragment: cached URLs can carry signed, credential-like parameters."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
    except ValueError:  # an invalid port is only detected when it is read
        return ""
    return f"{parts.scheme}://{host}{port}{parts.path}"


def resource_url(key: str) -> str:
    matches = URL_TOKEN.findall(key.replace("\0", " "))
    return matches[-1] if matches else ""


def _text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def parse_response_headers(stream0: bytes) -> tuple[int | None, dict[str, str]]:
    """Status and first value of each header (lower-cased names); Content-Encoding lines combine."""
    for match in STATUS_LINE.finditer(stream0):
        start = match.start()
        end = stream0.find(b"\0\0", start)
        if end < 0 or start < 4:
            continue
        (prefix,) = struct.unpack("<I", stream0[start - 4 : start])
        if prefix != end + 2 - start:
            continue
        headers: dict[str, str] = {}
        for line in stream0[start:end].split(b"\0")[1:]:
            name, _, value = line.partition(b":")
            field_name, value_text = _text(name).strip().lower(), _text(value).strip()
            if field_name == "content-encoding" and field_name in headers:
                headers[field_name] = f"{headers[field_name]}, {value_text}"
            else:
                headers.setdefault(field_name, value_text)
        return int(match.group(1)), headers
    return None, {}


def parse_entry(path: Path) -> CacheEntry:
    """Structural parse. Raises EntryFault, and nothing else, for a file that is not a good entry."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size < RECORD.size * 3:
                raise EntryFault("truncated")
            magic, version, key_len, _hash, _pad = RECORD.unpack(handle.read(RECORD.size))
            if magic != INITIAL_MAGIC:
                raise EntryFault("bad-initial-magic")
            if version != SUPPORTED_VERSION:
                raise EntryFault(f"unsupported-version:{version}")
            if key_len > MAX_KEY_BYTES or RECORD.size + key_len + 2 * RECORD.size > size:
                raise EntryFault("truncated-key")
            key = handle.read(key_len)
            handle.seek(size - RECORD.size)
            eof0 = RECORD.unpack(handle.read(RECORD.size))
            if eof0[0] != FINAL_MAGIC:
                raise EntryFault("bad-final-magic")
            flags0, crc0, stream0_len = eof0[1], eof0[2], eof0[3]
            sha_len = 32 if flags0 & FLAG_HAS_KEY_SHA256 else 0
            stream0_start = size - RECORD.size - sha_len - stream0_len
            eof1_at = stream0_start - RECORD.size
            body_start = RECORD.size + key_len
            if stream0_len > MAX_STREAM0_BYTES or eof1_at < body_start:
                raise EntryFault("bad-stream0-size")
            handle.seek(eof1_at)
            eof1 = RECORD.unpack(handle.read(RECORD.size))
            if eof1[0] != FINAL_MAGIC:
                raise EntryFault("bad-eof1-magic")
            body_len = eof1_at - body_start
            if eof1[3] not in (0, body_len):
                raise EntryFault("stream-size-mismatch")
            handle.seek(stream0_start)
            stream0 = handle.read(stream0_len)
            if len(stream0) != stream0_len:
                raise EntryFault("truncated-stream0")
            if flags0 & FLAG_HAS_CRC32 and zlib.crc32(stream0) != crc0:
                raise EntryFault("stream0-crc-mismatch")
            if sha_len and hashlib.sha256(key).digest() != handle.read(sha_len):
                raise EntryFault("key-sha256-mismatch")
    except OSError as error:
        raise EntryFault(f"unreadable:{error.__class__.__name__}") from error
    except struct.error as error:
        raise EntryFault("truncated-record") from error
    status, headers = parse_response_headers(stream0)
    encodings = tuple(
        token
        for token in (t.strip().lower() for t in headers.get("content-encoding", "").split(","))
        if token and token != "identity"
    )
    return CacheEntry(
        path=path,
        key=_text(key),
        url=strip_query(resource_url(_text(key))),
        status=status,
        content_type=headers.get("content-type", "").split(";")[0].strip().lower(),
        encodings=encodings,
        served=headers.get("date", ""),
        body_start=body_start,
        body_len=body_len,
        crc_expected=eof1[2] if eof1[1] & FLAG_HAS_CRC32 else None,
        problems=() if headers else ("no-response-headers",),
    )


def iter_entries(root: Path) -> Iterator[CacheEntry | Skipped]:
    """Every ``<16 hex>_0`` file directly under ``root``, parsed; a bad file yields Skipped."""
    if not root.is_dir():
        return
    for path in sorted(root.iterdir()):
        if not ENTRY_NAME.match(path.name) or not path.is_file() or path.is_symlink():
            continue
        try:
            yield parse_entry(path)
        except EntryFault as fault:
            yield Skipped(path, str(fault))


def brotli_decoder() -> Callable[[bytes], bytes] | None:
    """A brotli decoder when this machine has one (module first, then the command), else None."""
    try:
        module = importlib.import_module("brotli")
    except ImportError:
        module = None
    if module is not None:

        def decode(data: bytes) -> bytes:
            try:
                return bytes(module.decompress(data))
            except Exception as error:  # the module's own error type is not importable portably
                raise BodyUnavailable("brotli-decode-failed") from error

        return decode
    command = shutil.which("brotli")
    if not command:
        return None

    def run(data: bytes) -> bytes:
        try:
            done = subprocess.run(
                [command, "-d", "-c"], input=data, capture_output=True, check=False, timeout=120
            )
        except (subprocess.TimeoutExpired, OSError) as error:
            raise BodyUnavailable("brotli-decode-failed") from error
        if done.returncode != 0:
            raise BodyUnavailable("brotli-decode-failed")
        return done.stdout

    return run


def decode_body(
    data: bytes, encodings: tuple[str, ...], brotli: Callable[[bytes], bytes] | None
) -> bytes:
    """Undo Content-Encoding in reverse order; an undecodable layer is reported, never skipped."""
    for encoding in reversed(encodings):
        try:
            if encoding in {"gzip", "x-gzip"}:
                data = gzip.decompress(data)
            elif encoding == "deflate":
                try:
                    data = zlib.decompress(data)
                except zlib.error:
                    data = zlib.decompress(data, -zlib.MAX_WBITS)
            elif encoding == "br" and brotli is not None:
                data = brotli(data)
            else:
                raise BodyUnavailable(f"unsupported:{encoding}")
        except (OSError, EOFError, zlib.error) as error:
            raise BodyUnavailable(f"decode-failed:{encoding}") from error
        if len(data) > MAX_DECODED_BYTES:
            raise BodyUnavailable("decoded-too-large")
    return data


def read_body(
    entry: CacheEntry, brotli: Callable[[bytes], bytes] | None = None, *, decode: bool = True
) -> bytes:
    """The verified body (CRC32 against EOF1), decoded unless ``decode`` is false."""
    if entry.body_len > MAX_BODY_BYTES:
        raise BodyUnavailable("body-too-large")
    if entry.crc_expected is None:
        raise BodyUnavailable("unverified-no-crc")
    try:
        with entry.path.open("rb") as handle:
            handle.seek(entry.body_start)
            data = handle.read(entry.body_len)
    except OSError as error:
        raise BodyUnavailable(f"unreadable:{error.__class__.__name__}") from error
    if len(data) != entry.body_len or zlib.crc32(data) != entry.crc_expected:
        raise BodyUnavailable("body-crc-mismatch")
    return decode_body(data, entry.encodings, brotli) if decode else data
