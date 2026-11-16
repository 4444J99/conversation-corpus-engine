"""Credential file protection and passphrase policy, using invented keys and passphrases."""

import json
import os
import stat
from contextlib import contextmanager

import pytest

from conversation_corpus_engine import archive_sealed as sealed
from conversation_corpus_engine.archive_sealed_commands import run
from conversation_corpus_engine.archive_sealed_credentials import protect_identity

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX modes only")

PHRASE = b"synthetic-passphrase"


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


@contextmanager
def umask(mask):
    previous = os.umask(mask)
    try:
        yield
    finally:
        os.umask(previous)


def private_file(path, data, file_mode=0o600):
    path.write_bytes(data)
    path.chmod(file_mode)
    return path


def labels(tmp_path):
    return private_file(
        tmp_path / "labels.json", json.dumps({"app_id": "cred-app", "instance_id": "dev"}).encode()
    )


# --- creation modes --------------------------------------------------------------------------


@pytest.mark.parametrize("mask", [0o022, 0o000])
def test_credentials_trust_and_recipient_are_created_0600(tmp_path, mask):
    with umask(mask):
        protected = sealed.create_identity(
            tmp_path / "protected.key", "cred-app", "one", passphrase=PHRASE
        )
        sealed.create_legacy_identity(tmp_path / "legacy.key", "cred-app", "two")
        trust = sealed.create_trust(tmp_path / "trust.json", protected)
        sealed._write(
            tmp_path / "recipient.json", sealed.canonical(protected.recipient()), exclusive=True
        )
    for name in ("protected.key", "legacy.key", "trust.json", "recipient.json"):
        assert mode(tmp_path / name) == 0o600, name
    assert mode(trust) == 0o600


def test_checkpoint_rewrites_keep_the_trust_file_0600(tmp_path):
    source = tmp_path / "private"
    source.mkdir()
    (source / "note").write_bytes(b"synthetic")
    owner = sealed.create_identity(tmp_path / "owner.key", "cred-app", "o", passphrase=PHRASE)
    trust = sealed.create_trust(tmp_path / "trust.json", owner)
    repo = tmp_path / "cipher"
    sealed.publish(source, ["note"], repo, owner, trust, [owner.recipient()])
    assert mode(trust) == 0o600
    sealed.verify(repo, owner, trust)
    assert mode(trust) == 0o600


def test_cli_identity_creates_key_and_recipient_0600(tmp_path):
    phrase = private_file(tmp_path / "phrase", PHRASE)
    key, recipient = tmp_path / "key", tmp_path / "recipient"
    args = ["identity", "--labels", str(labels(tmp_path)), "--key", str(key)]
    assert run([*args, "--recipient", str(recipient), "--passphrase-file", str(phrase)]) == 0
    assert mode(key) == mode(recipient) == 0o600


# --- mode enforcement on read ---------------------------------------------------------------


@pytest.mark.parametrize("bad", [0o640, 0o604, 0o644, 0o660, 0o666, 0o707])
def test_protected_credentials_with_group_or_other_access_are_refused(tmp_path, bad):
    path = tmp_path / "owner.key"
    owner = sealed.create_identity(path, "cred-app", "owner", passphrase=PHRASE)
    path.chmod(bad)
    with pytest.raises(sealed.SealedError, match="credential-permissions"):
        sealed.load_identity(path, passphrase=PHRASE)
    for good in (0o600, 0o400):
        path.chmod(good)
        assert sealed.load_identity(path, passphrase=PHRASE) == owner


@pytest.mark.parametrize("bad", [0o640, 0o604, 0o644])
def test_legacy_credentials_with_group_or_other_access_are_refused(tmp_path, bad):
    path = tmp_path / "legacy.key"
    legacy = sealed.create_legacy_identity(path, "cred-app", "legacy")
    path.chmod(bad)
    with pytest.raises(sealed.SealedError, match="credential-permissions"):
        sealed.load_legacy_identity(path)
    path.chmod(0o600)
    assert sealed.load_legacy_identity(path) == legacy


@pytest.mark.parametrize("bad", [0o640, 0o604, 0o644, 0o666])
def test_cli_refuses_a_passphrase_file_with_group_or_other_access(tmp_path, capsys, bad):
    phrase = private_file(tmp_path / "phrase", PHRASE, bad)
    key = tmp_path / "key"
    args = ["identity", "--labels", str(labels(tmp_path)), "--key", str(key)]
    args += ["--recipient", str(tmp_path / "recipient"), "--passphrase-file", str(phrase)]
    assert run(args) == 2
    captured = capsys.readouterr()
    assert captured.err == "[sealed] credential-permissions\n"
    assert captured.out == ""
    assert not key.exists()
    phrase.chmod(0o600)
    assert run(args) == 0


# --- parent directories ---------------------------------------------------------------------


@pytest.mark.parametrize("mask", [0o022, 0o000])
@pytest.mark.parametrize("kind", ["protected", "legacy", "trust", "recipient"])
def test_new_parent_directories_are_0700_and_existing_ones_untouched(tmp_path, mask, kind):
    seed = sealed.create_legacy_identity(tmp_path / "seed.key", "cred-app", "seed")
    existing = tmp_path / "existing"
    existing.mkdir()
    existing.chmod(0o755)
    path = existing / "new-one" / "new-two" / "credential"
    with umask(mask):
        if kind == "protected":
            sealed.create_identity(path, "cred-app", "x", passphrase=PHRASE)
        elif kind == "legacy":
            sealed.create_legacy_identity(path, "cred-app", "x")
        elif kind == "trust":
            sealed.create_trust(path, seed)
        else:
            sealed._write(path, sealed.canonical(seed.recipient()), exclusive=True)
    assert mode(existing) == 0o755
    assert mode(existing / "new-one") == 0o700
    assert mode(existing / "new-one" / "new-two") == 0o700
    assert mode(path) == 0o600


# --- minimum passphrase length for NEW identities only ----------------------------------------


@pytest.mark.parametrize("short", [b"", b"x", b"a" * 11])
def test_new_identity_requires_at_least_12_passphrase_bytes(tmp_path, short):
    path = tmp_path / "fresh" / "owner.key"
    with pytest.raises(sealed.SealedError, match="passphrase-too-short"):
        sealed.create_identity(path, "cred-app", "owner", passphrase=short)
    assert not path.exists()
    assert not path.parent.exists()  # nothing is created for a refused identity
    owner = sealed.create_identity(path, "cred-app", "owner", passphrase=b"a" * 12)
    assert sealed.load_identity(path, passphrase=b"a" * 12) == owner


def test_new_identity_still_refuses_a_missing_passphrase(tmp_path):
    with pytest.raises(sealed.SealedError, match="credential-unlock-failed"):
        sealed.create_identity(tmp_path / "owner.key", "cred-app", "owner", passphrase=None)
    assert not (tmp_path / "owner.key").exists()


def test_cli_identity_refuses_a_short_passphrase_and_creates_nothing(tmp_path, capsys):
    phrase = private_file(tmp_path / "phrase", b"tiny-pw")
    key, recipient = tmp_path / "key", tmp_path / "recipient"
    args = ["identity", "--labels", str(labels(tmp_path)), "--key", str(key)]
    args += ["--recipient", str(recipient), "--passphrase-file", str(phrase)]
    assert run(args) == 2
    captured = capsys.readouterr()
    assert captured.err == "[sealed] passphrase-too-short\n"
    assert captured.out == ""
    assert not key.exists()
    assert not recipient.exists()
    assert b"tiny-pw" not in (captured.out + captured.err).encode()


@pytest.mark.parametrize("short", [b"x", b"abc", b"eleven-char"])
def test_existing_identities_unlock_with_any_passphrase(tmp_path, capsys, short):
    legacy = sealed.create_legacy_identity(tmp_path / "seed.key", "cred-app", "old")
    key = private_file(tmp_path / "old.key", protect_identity(legacy, short))
    assert sealed.load_identity(key, passphrase=short) == legacy
    phrase = private_file(tmp_path / "phrase", short)
    trust = tmp_path / "trust"
    args = ["init", "--key", str(key), "--trust", str(trust), "--passphrase-file", str(phrase)]
    assert run(args) == 0
    assert trust.exists()
    capsys.readouterr()
    with pytest.raises(sealed.SealedError, match="credential-unlock-failed"):
        sealed.load_identity(key, passphrase=short + b"-wrong")
