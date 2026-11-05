"""Recipient fingerprint command, using invented keys only."""

import hashlib
import json
import sys

import pytest

from conversation_corpus_engine import archive_sealed as sealed
from conversation_corpus_engine.archive_sealed_commands import run as sealed_run
from conversation_corpus_engine.archive_sealed_crypto import (
    Identity,
    recipient_fingerprint,
)
from conversation_corpus_engine.cli import main

# --- S5: recipient fingerprints --------------------------------------------------------------


def _recipient_file(tmp_path, identity, name="recipient.json"):
    path = tmp_path / name
    path.write_bytes(sealed.canonical(identity.recipient()))
    return path


def _fingerprint(path, capsys):
    code = sealed_run(["fingerprint", "--recipient", str(path)])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_fingerprint_is_stable_independent_and_needs_no_unlock(tmp_path, capsys):
    owner = sealed.create_legacy_identity(tmp_path / "owner.key", "fp-app", "owner")
    path = _recipient_file(tmp_path, owner)
    before = sorted(p.name for p in tmp_path.iterdir())
    first = _fingerprint(path, capsys)
    second = _fingerprint(path, capsys)
    assert first == second
    code, out, err = first
    assert (code, err) == (0, "")
    result = json.loads(out)
    expected = hashlib.sha256(
        b"cce-sealed.v1:recipient-fingerprint:"
        + json.dumps(owner.recipient(), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert result == {"algorithm": "sha256", "fingerprint": expected}
    assert recipient_fingerprint(owner.recipient()) == expected
    assert sorted(p.name for p in tmp_path.iterdir()) == before  # read-only
    assert owner.signing_key.hex() not in out and owner.encryption_key.hex() not in out


def test_fingerprint_distinguishes_keys_and_labels(tmp_path):
    a = sealed.create_legacy_identity(tmp_path / "a.key", "fp-app", "same")
    b = sealed.create_legacy_identity(tmp_path / "b.key", "fp-app", "same")
    relabeled = Identity("fp-app", "other", a.encryption_key, a.signing_key)
    prints = {recipient_fingerprint(x.recipient()) for x in (a, b, relabeled)}
    assert len(prints) == 3
    assert all(len(p) == 64 and p == p.lower() for p in prints)


@pytest.mark.parametrize("protected", [False, True])
def test_fingerprint_refuses_credential_files_without_echoing_secrets(tmp_path, capsys, protected):
    if protected:
        sealed.create_identity(
            tmp_path / "owner.key", "fp-app", "owner", passphrase=b"synthetic-passphrase"
        )
    else:
        sealed.create_legacy_identity(tmp_path / "owner.key", "fp-app", "owner")
    raw = (tmp_path / "owner.key").read_text()
    code, out, err = _fingerprint(tmp_path / "owner.key", capsys)
    assert code == 2
    assert out == ""
    assert err == "[sealed] invalid-recipient\n"
    assert not any(token in out + err for token in json.loads(raw).values())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: {**r, "encryption_public": "zz" * 32},
        lambda r: {**r, "encryption_public": r["encryption_public"][:-2]},
        lambda r: {**r, "encryption_public": r["encryption_public"].upper()},
        lambda r: {k: v for k, v in r.items() if k != "instance_id"},
        lambda r: {**r, "extra": "field"},
        lambda r: {**r, "app_id": 7},
    ],
)
def test_fingerprint_rejects_malformed_recipient_records(tmp_path, capsys, mutate):
    owner = sealed.create_legacy_identity(tmp_path / "owner.key", "fp-app", "owner")
    path = tmp_path / "bad.json"
    path.write_bytes(sealed.canonical(mutate(owner.recipient())))
    code, out, err = _fingerprint(path, capsys)
    assert (code, out, err) == (2, "", "[sealed] invalid-recipient\n")


def test_fingerprint_is_reachable_from_the_cce_entrypoint(tmp_path, monkeypatch, capsys):
    owner = sealed.create_legacy_identity(tmp_path / "owner.key", "fp-app", "owner")
    path = _recipient_file(tmp_path, owner)
    monkeypatch.setattr(sys, "argv", ["cce", "sealed", "fingerprint", "--recipient", str(path)])
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["fingerprint"] == recipient_fingerprint(owner.recipient())
