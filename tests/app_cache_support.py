"""Build synthetic Chromium simple-cache entries (version 5) for tests. No real data."""

from __future__ import annotations

import hashlib
import struct
import zlib
from pathlib import Path

from conversation_corpus_engine.app_cache_chromium import (
    FINAL_MAGIC,
    FLAG_HAS_CRC32,
    FLAG_HAS_KEY_SHA256,
    INITIAL_MAGIC,
    RECORD,
)


def response_info(headers: dict[str, str], status: int = 200) -> bytes:
    block = b"\0".join(
        [f"HTTP/1.1 {status} OK".encode()] + [f"{k}: {v}".encode() for k, v in headers.items()]
    )
    block += b"\0\0"
    return b"\x00\x00\x00\x00" + struct.pack("<I", len(block)) + block


def write_entry(
    root: Path,
    name: str,
    url: str,
    body: bytes,
    headers: dict[str, str] | None = None,
    *,
    status: int = 200,
    corrupt_body_crc: bool = False,
) -> Path:
    key = f"1/0/_dk_https://example.test https://example.test {url}".encode()
    stream0 = response_info(headers or {"Content-Type": "application/json"}, status)
    crc = zlib.crc32(body) ^ (1 if corrupt_body_crc else 0)
    out = RECORD.pack(INITIAL_MAGIC, 5, len(key), 0, 0) + key + body
    out += RECORD.pack(FINAL_MAGIC, FLAG_HAS_CRC32, crc, 0, 0)
    out += stream0 + hashlib.sha256(key).digest()
    out += RECORD.pack(
        FINAL_MAGIC, FLAG_HAS_CRC32 | FLAG_HAS_KEY_SHA256, zlib.crc32(stream0), len(stream0), 0
    )
    path = root / name
    path.write_bytes(out)
    return path
