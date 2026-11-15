from __future__ import annotations

import gzip
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app_cache_support import write_entry  # noqa: E402

from conversation_corpus_engine import app_cache_chromium as chromium  # noqa: E402
from conversation_corpus_engine.app_cache_chromium import (  # noqa: E402
    BodyUnavailable,
    CacheEntry,
    Skipped,
    decode_body,
    iter_entries,
    read_body,
    strip_query,
)


def test_entry_roundtrip_and_query_is_dropped(tmp_path: Path) -> None:
    write_entry(
        tmp_path, "00000000000000a1_0", "https://h.test/a/b?Signature=s3cret&x=1", b'{"k":1}'
    )
    (entry,) = list(iter_entries(tmp_path))
    assert isinstance(entry, CacheEntry)
    assert entry.url == "https://h.test/a/b"
    assert "s3cret" not in entry.url
    assert entry.status == 200 and entry.content_type == "application/json"
    assert read_body(entry) == b'{"k":1}'


def test_gzip_body_decodes(tmp_path: Path) -> None:
    write_entry(
        tmp_path,
        "00000000000000a2_0",
        "https://h.test/z",
        gzip.compress(b"hello"),
        {"Content-Encoding": "gzip"},
    )
    (entry,) = list(iter_entries(tmp_path))
    assert entry.encodings == ("gzip",)
    assert read_body(entry) == b"hello"


def test_brotli_is_reported_not_guessed_without_a_decoder(tmp_path: Path) -> None:
    write_entry(
        tmp_path, "00000000000000a3_0", "https://h.test/b", b"xx", {"Content-Encoding": "br"}
    )
    (entry,) = list(iter_entries(tmp_path))
    with pytest.raises(BodyUnavailable, match="unsupported:br"):
        read_body(entry, None)
    assert read_body(entry, lambda data: b"decoded:" + data) == b"decoded:xx"


def test_crc_mismatch_is_refused(tmp_path: Path) -> None:
    write_entry(tmp_path, "00000000000000a4_0", "https://h.test/c", b"abc", corrupt_body_crc=True)
    (entry,) = list(iter_entries(tmp_path))
    with pytest.raises(BodyUnavailable, match="body-crc-mismatch"):
        read_body(entry)


def test_bad_files_are_skipped_with_a_reason(tmp_path: Path) -> None:
    (tmp_path / "00000000000000a5_0").write_bytes(b"x" * 200)
    (tmp_path / "index").write_bytes(b"ignored")
    path = write_entry(tmp_path, "00000000000000a6_0", "https://h.test/d", b"ok")
    path.write_bytes(path.read_bytes()[:-30])
    skipped = [item for item in iter_entries(tmp_path) if isinstance(item, Skipped)]
    assert sorted(s.reason for s in skipped) == ["bad-final-magic", "bad-initial-magic"]


def test_decode_failure_names_the_layer() -> None:
    with pytest.raises(BodyUnavailable, match="decode-failed:gzip"):
        decode_body(b"not gzip", ("gzip",), None)


def test_strip_query_drops_userinfo_and_fragment() -> None:
    assert strip_query("https://u:p@h.test:8443/p?q=1#f") == "https://h.test:8443/p"


def test_invalid_port_does_not_abort_a_scan(tmp_path: Path) -> None:
    assert strip_query("https://h.test:bad/p") == ""
    write_entry(tmp_path, "00000000000000b1_0", "https://h.test:bad/p", b"ok")
    (entry,) = list(iter_entries(tmp_path))
    assert isinstance(entry, CacheEntry)


def test_brotli_decoder_failures_become_body_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    class Module:
        @staticmethod
        def decompress(data: bytes) -> bytes:
            raise ValueError("corrupt")

    monkeypatch.setattr(chromium.importlib, "import_module", lambda name: Module)
    decoder = chromium.brotli_decoder()
    assert decoder is not None
    with pytest.raises(BodyUnavailable, match="brotli-decode-failed"):
        decoder(b"x")


def test_brotli_command_timeout_becomes_body_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(name: str) -> None:
        raise ImportError(name)

    def slow(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired("brotli", 1)

    monkeypatch.setattr(chromium.importlib, "import_module", refuse)
    monkeypatch.setattr(chromium.shutil, "which", lambda name: "/bin/brotli")
    monkeypatch.setattr(chromium.subprocess, "run", slow)
    decoder = chromium.brotli_decoder()
    assert decoder is not None
    with pytest.raises(BodyUnavailable, match="brotli-decode-failed"):
        decoder(b"x")
