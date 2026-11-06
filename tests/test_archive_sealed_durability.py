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


# --- an interrupted publication, retried: nothing it left behind may stand in the way ----------


def _tree_files(repo):
    return sorted(
        path.relative_to(repo).as_posix()
        for path in repo.rglob("*")
        if path.is_file() and ".git" not in path.parts
    )


def _restored_matches(vault, tmp_path):
    source, repo, owner, trust = vault
    sealed.restore(repo, tmp_path / "restored", owner, trust)
    assert (tmp_path / "restored" / "big").read_bytes() == (source / "big").read_bytes()


def test_an_interrupted_second_publish_is_retried_on_the_same_secret(vault, tmp_path, monkeypatch):
    source, repo, owner, trust = vault
    assert sealed.publish(*_args(vault))["generation"] == 1
    # New content for the second generation, so it writes new objects under the same secret.
    (source / "big").write_bytes(b"D" * sealed.CHUNK + b"E" * sealed.CHUNK + b"F" * sealed.CHUNK)
    _fail_nth_write(monkeypatch, 2)
    with pytest.raises(OSError, match="No space"):
        sealed.publish(*_args(vault))
    monkeypatch.undo()
    assert sealed.verify(repo, owner, trust)["generation"] == 1, "the first generation still stands"
    result = sealed.publish(*_args(vault))
    assert result["changed"] is True and result["generation"] == 2
    assert sealed.verify(repo, owner, trust)["generation"] == 2
    assert sealed.list_units(repo, owner, trust) == []
    _restored_matches(vault, tmp_path)
    assert sealed.publish(*_args(vault))["changed"] is False


@pytest.mark.parametrize("second", [False, True], ids=["first-publish", "second-publish"])
def test_a_failed_head_write_after_every_object_is_retried(vault, tmp_path, monkeypatch, second):
    source, repo, owner, trust = vault
    if second:
        sealed.publish(*_args(vault))
        (source / "big").write_bytes(b"G" * sealed.CHUNK + b"H" * sealed.CHUNK + b"I" * 5)
    before = _tree_files(repo)
    # Three objects (one per 512 KiB chunk, each distinct), then the head is the next write.
    _fail_nth_write(monkeypatch, 3 + 1)
    with pytest.raises(OSError, match="No space"):
        sealed.publish(*_args(vault))
    monkeypatch.undo()
    assert not (repo / ".cce-lock").exists(), "the publisher lock is released"
    assert ("HEAD.cce" in before) == (repo / "HEAD.cce").exists()
    result = sealed.publish(*_args(vault))
    assert result["changed"] is True and result["generation"] == (2 if second else 1)
    assert sealed.verify(repo, owner, trust)["generation"] == result["generation"]
    _restored_matches(vault, tmp_path)


def test_an_interrupt_mid_publication_releases_the_lock_and_can_be_retried(
    vault, tmp_path, monkeypatch
):
    _, repo, owner, trust = vault
    real = os.fdopen
    opened = []

    def interrupted(fd, mode="r", *args, **kwargs):
        opened.append(fd)
        if len(opened) == 2:
            os.close(fd)
            raise KeyboardInterrupt
        return real(fd, mode, *args, **kwargs)

    monkeypatch.setattr(sealed.os, "fdopen", interrupted)
    with pytest.raises(KeyboardInterrupt):
        sealed.publish(*_args(vault))
    monkeypatch.undo()
    assert not (repo / ".cce-lock").exists()
    assert not any(name.rpartition("/")[2].startswith(".cce-") for name in _tree_files(repo))
    assert sealed.publish(*_args(vault))["changed"] is True
    assert sealed.verify(repo, owner, trust)["files"] == 1
    _restored_matches(vault, tmp_path)


def test_a_publication_that_failed_midway_leaves_no_objects_behind(vault, monkeypatch):
    """Objects the failed attempt wrote are unreferenced ciphertext; left in place they are counted
    against the storage budget of every retry (and, with a fresh secret, can never be reused)."""
    _, repo, owner, trust = vault
    _fail_nth_write(monkeypatch, 3)
    with pytest.raises(OSError, match="No space"):
        sealed.publish(*_args(vault))
    monkeypatch.undo()
    assert _tree_files(repo) == []


def test_a_retry_fits_the_budget_the_plan_said_it_would(vault, tmp_path, monkeypatch):
    source, repo, owner, trust = vault
    planned = sealed.plan_capacity(*_args(vault), budget=10**9)
    assert planned["fits"] is True
    budget = planned["planned_total_bytes"]
    _fail_nth_write(monkeypatch, 3)
    with pytest.raises(OSError, match="No space"):
        sealed.publish(*_args(vault), budget=budget)
    monkeypatch.undo()
    assert sealed.publish(*_args(vault), budget=budget)["changed"] is True
    assert sealed.verify(repo, owner, trust)["files"] == 1
    _restored_matches(vault, tmp_path)


def test_a_failed_retry_after_a_good_publication_keeps_that_publication_and_its_objects(
    vault, monkeypatch
):
    source, repo, owner, trust = vault
    sealed.publish(*_args(vault))
    kept = _tree_files(repo)
    (source / "big").write_bytes(b"J" * sealed.CHUNK + b"K" * sealed.CHUNK + b"L" * sealed.CHUNK)
    _fail_nth_write(monkeypatch, 2)
    with pytest.raises(OSError, match="No space"):
        sealed.publish(*_args(vault))
    monkeypatch.undo()
    assert set(kept) <= set(_tree_files(repo)), "nothing the first generation needs is removed"
    assert sealed.verify(repo, owner, trust)["generation"] == 1


def test_an_interrupt_after_the_head_landed_keeps_the_objects_it_names(vault, monkeypatch):
    """The cleanup of a failed publication must never remove objects a head already references:
    an interrupt delivered just after the head's atomic replace is not a failed publication."""
    _, repo, owner, trust = vault
    real = sealed._write

    def interrupted_after_head(path, data, *, exclusive=False):
        real(path, data, exclusive=exclusive)
        if path.name == "HEAD.cce":
            raise KeyboardInterrupt

    monkeypatch.setattr(sealed, "_write", interrupted_after_head)
    with pytest.raises(KeyboardInterrupt):
        sealed.publish(*_args(vault))
    monkeypatch.undo()
    assert (repo / "HEAD.cce").exists() and _tree_files(repo)
    assert (
        sealed.verify(
            repo,
            owner,
            trust,
        )["files"]
        == 1
    )
