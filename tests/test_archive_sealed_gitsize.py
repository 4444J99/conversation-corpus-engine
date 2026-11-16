"""The capacity plan must fail closed when Git size cannot be measured. Invented data only."""

import os
import subprocess

import pytest

from conversation_corpus_engine import archive_sealed as sealed

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Synthetic",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Synthetic",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


def _git(repo, *args):
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env={**os.environ, **GIT_ENV},
    )


@pytest.fixture
def vault(tmp_path):
    source = tmp_path / "private"
    source.mkdir()
    (source / "note").write_bytes(b"synthetic git size note")
    owner = sealed.create_legacy_identity(tmp_path / "owner.key", "size-app", "owner")
    trust = sealed.create_trust(tmp_path / "trust.json", owner)
    return source, tmp_path / "cipher", owner, trust


def _committed_vault(vault):
    source, repo, owner, trust = vault
    args = source, ["note"], repo, owner, trust, [owner.recipient()]
    sealed.publish(*args)
    _git(repo, "init", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "chore: update encrypted archive")
    return args


def _fake_count_objects(monkeypatch, result):
    real_run = subprocess.run

    def run(command, *args, **kwargs):
        if "count-objects" in command:
            return result(command)
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(sealed.subprocess, "run", run)


def test_unmeasurable_git_size_fails_closed_instead_of_reporting_zero(vault, monkeypatch):
    args = _committed_vault(vault)
    assert sealed.plan_capacity(*args)["git_history_bytes"] > 0
    _fake_count_objects(
        monkeypatch, lambda c: subprocess.CompletedProcess(c, 128, "", "fatal: synthetic")
    )
    with pytest.raises(sealed.SealedError, match="git-size-unmeasurable"):
        sealed.plan_capacity(*args)


def test_unparseable_git_size_output_fails_closed(vault, monkeypatch):
    args = _committed_vault(vault)
    _fake_count_objects(
        monkeypatch, lambda c: subprocess.CompletedProcess(c, 0, "no colon here\n", "")
    )
    with pytest.raises(sealed.SealedError, match="git-size-unmeasurable"):
        sealed.plan_capacity(*args)


def test_missing_git_binary_for_size_fails_closed(vault, monkeypatch):
    args = _committed_vault(vault)

    def missing(command):
        raise FileNotFoundError("git")

    _fake_count_objects(monkeypatch, missing)
    with pytest.raises(sealed.SealedError, match="git-size-unmeasurable"):
        sealed.plan_capacity(*args)


def test_storage_status_and_publish_fail_closed_when_size_is_unmeasurable(vault, monkeypatch):
    source, repo, owner, trust = vault
    args = _committed_vault(vault)
    (source / "note").write_bytes(b"changed synthetic bytes")
    _fake_count_objects(
        monkeypatch, lambda c: subprocess.CompletedProcess(c, 128, "", "fatal: synthetic")
    )
    before = (repo / "HEAD.cce").read_bytes()
    with pytest.raises(sealed.SealedError, match="git-size-unmeasurable"):
        sealed.publish(*args)
    assert (repo / "HEAD.cce").read_bytes() == before
    with pytest.raises(sealed.SealedError, match="git-size-unmeasurable"):
        sealed.storage_status(repo, owner, trust)


def test_plain_directory_and_missing_directory_still_measure_zero(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert sealed._git_bytes(plain) == 0
    assert sealed._git_bytes(tmp_path / "missing") == 0
