"""Crash-safe ciphertext writes. Invented data only."""

import errno
import os

import pytest

from conversation_corpus_engine import archive_sealed as sealed


class _HalfWriter:
    """Wrap a real file object: persist half of the first write, then fail like ENOSPC."""

    def __init__(self, handle):
        self._handle = handle

    def write(self, data):
        self._handle.write(data[: len(data) // 2])
        self._handle.flush()
        raise OSError(errno.ENOSPC, "No space left on device")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._handle.close()
        return False

    def __getattr__(self, name):
        return getattr(self._handle, name)


def _fail_nth_write(monkeypatch, nth):
    real_fdopen = os.fdopen
    opened = []

    def fdopen(fd, mode="r", *args, **kwargs):
        handle = real_fdopen(fd, mode, *args, **kwargs)
        opened.append(fd)
        return _HalfWriter(handle) if len(opened) == nth else handle

    monkeypatch.setattr(sealed.os, "fdopen", fdopen)


@pytest.fixture
def vault(tmp_path):
    source = tmp_path / "private"
    source.mkdir()
    # Three 512 KiB chunks of distinct bytes, so several ciphertext objects are written.
    (source / "big").write_bytes(b"A" * sealed.CHUNK + b"B" * sealed.CHUNK + b"C" * sealed.CHUNK)
    owner = sealed.create_legacy_identity(tmp_path / "owner.key", "durable-app", "owner")
    trust = sealed.create_trust(tmp_path / "trust.json", owner)
    return source, tmp_path / "cipher", owner, trust


def _args(vault):
    source, repo, owner, trust = vault
    return source, ["big"], repo, owner, trust, [owner.recipient()]


def test_failed_exclusive_write_leaves_no_truncated_object_or_temp(tmp_path, monkeypatch):
    target = tmp_path / "dir" / "object"
    _fail_nth_write(monkeypatch, 1)
    with pytest.raises(OSError, match="No space"):
        sealed._write(target, b"0123456789" * 100, exclusive=True)
    assert not target.exists()
    assert list(target.parent.iterdir()) == []


def test_exclusive_write_is_synced_before_it_becomes_visible(tmp_path, monkeypatch):
    target = tmp_path / "dir" / "object"

    def failing_fsync(fd):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(sealed.os, "fsync", failing_fsync)
    with pytest.raises(OSError, match="Input/output"):
        sealed._write(target, b"synthetic ciphertext", exclusive=True)
    assert not target.exists()
    assert list(target.parent.iterdir()) == []


def test_exclusive_write_still_refuses_to_replace_an_existing_file(tmp_path):
    target = tmp_path / "object"
    sealed._write(target, b"first", exclusive=True)
    with pytest.raises(FileExistsError):
        sealed._write(target, b"second", exclusive=True)
    assert target.read_bytes() == b"first"
    assert [p.name for p in tmp_path.iterdir()] == ["object"]


def test_failed_replacing_write_keeps_previous_content(tmp_path, monkeypatch):
    target = tmp_path / "head"
    sealed._write(target, b"previous")
    _fail_nth_write(monkeypatch, 1)
    with pytest.raises(OSError, match="No space"):
        sealed._write(target, b"replacement")
    assert target.read_bytes() == b"previous"
    assert [p.name for p in tmp_path.iterdir()] == ["head"]


def test_mid_publication_write_failure_does_not_poison_later_publishes(vault, monkeypatch):
    _, repo, owner, trust = vault
    _fail_nth_write(monkeypatch, 2)
    with pytest.raises(OSError, match="No space"):
        sealed.publish(*_args(vault))
    monkeypatch.undo()
    assert not (repo / "HEAD.cce").exists()
    # No partial object may remain: a retry, verify and restore all still work.
    assert sealed.publish(*_args(vault))["changed"] is True
    assert sealed.verify(repo, owner, trust)["files"] == 1
    sealed.restore(repo, vault[0].parent / "restored", owner, trust)
    assert (vault[0].parent / "restored" / "big").read_bytes() == (vault[0] / "big").read_bytes()
