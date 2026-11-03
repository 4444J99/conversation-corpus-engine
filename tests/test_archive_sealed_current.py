"""Current bounded archive and protected custody, using synthetic exports only."""

import io
import json
import os
import shutil
import subprocess
import sys

import pytest

from conversation_corpus_engine import archive_sealed as sealed
from conversation_corpus_engine.archive_ingest import ingest, iter_store
from conversation_corpus_engine.archive_sealed_commands import run


def _commit_snapshot(repo):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Synthetic",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Synthetic",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    for args in [
        ("init", "-b", "main"),
        ("add", "."),
        ("commit", "-m", "chore: update encrypted archive"),
    ]:
        subprocess.run(["git", "-C", str(repo), *args], env=env, check=True, capture_output=True)


def test_bounded_ingest_shards_have_atoms_and_clone_restores_exact_bytes(archive_env, tmp_path):
    ingest(export_root=archive_env.export, paths=archive_env.paths, authorization=archive_env.auth)
    source = archive_env.paths.root
    selection = sorted(p.relative_to(source).as_posix() for p in source.rglob("shard-*.jsonl"))
    assert selection
    owner = sealed.create_identity(
        tmp_path / "owner", "synthetic", "owner", passphrase=b"synthetic"
    )
    trust = sealed.create_trust(tmp_path / "trust", owner)
    repo = tmp_path / "cipher"
    result = sealed.publish(source, selection, repo, owner, trust, [owner.recipient()])
    expected = sum(
        len(c.messages) + len(c.extra.get("alternate_messages", []))
        for c in iter_store(archive_env.paths)
    )
    assert result["units"] == expected > 0
    _commit_snapshot(repo)
    clone = tmp_path / "independent"
    subprocess.run(
        ["git", "clone", "--no-local", str(repo), str(clone)], check=True, capture_output=True
    )
    checkpoint = tmp_path / "independent-trust"
    shutil.copyfile(trust, checkpoint)
    assert sealed.verify(clone, owner, checkpoint)["units"] == expected
    target = tmp_path / "restored"
    sealed.restore(clone, target, owner, checkpoint)
    assert {n: (target / n).read_bytes() for n in selection} == {
        n: (source / n).read_bytes() for n in selection
    }


def test_protected_identity_requires_unlock_and_contains_no_private_key(tmp_path):
    path = tmp_path / "owner.key"
    secret = b"synthetic passphrase only"
    owner = sealed.create_identity(path, "synthetic", "owner", passphrase=secret)
    raw = path.read_bytes()
    assert owner.encryption_key.hex().encode() not in raw
    assert owner.signing_key.hex().encode() not in raw
    assert secret not in raw
    assert sealed.load_identity(path, passphrase=secret) == owner
    for wrong in (None, b"wrong passphrase"):
        with pytest.raises(sealed.SealedError, match="credential-unlock-failed"):
            sealed.load_identity(path, passphrase=wrong)
    altered = json.loads(raw)
    ciphertext = bytearray.fromhex(altered["ciphertext"])
    ciphertext[-1] ^= 1
    altered["ciphertext"] = ciphertext.hex()
    path.write_text(json.dumps(altered))
    with pytest.raises(sealed.SealedError, match="credential-unlock-failed"):
        sealed.load_identity(path, passphrase=secret)


def test_capacity_plan_is_deterministic_read_only_and_includes_history(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "note").write_bytes(b"synthetic bytes")
    owner = sealed.create_identity(tmp_path / "owner", "synthetic", "owner", passphrase=b"unlock")
    trust = sealed.create_trust(tmp_path / "trust", owner)
    repo = tmp_path / "cipher"
    args = source, ["note"], repo, owner, trust, [owner.recipient()]
    before = trust.read_bytes()
    plan = sealed.plan_capacity(*args)
    assert plan == sealed.plan_capacity(*args)
    assert not repo.exists()
    assert trust.read_bytes() == before
    assert plan["selected_plaintext_bytes"] == 15
    assert plan["current_ciphertext_bytes"] == plan["git_history_bytes"] == 0
    assert plan["pending_ciphertext_bytes"] > 15
    assert plan["fits"]
    assert not sealed.plan_capacity(*args, budget=1)["fits"]
    with pytest.raises(sealed.SealedError, match="storage-budget-exceeded"):
        sealed.publish(*args, budget=1)
    assert not (repo / "HEAD.cce").exists()
    sealed.publish(*args)
    after = sealed.plan_capacity(*args)
    assert after["pending_ciphertext_bytes"] == 0
    assert after["current_ciphertext_bytes"] > 0
    assert after["planned_tree_bytes"] == after["current_ciphertext_bytes"]
    _commit_snapshot(repo)
    with_history = sealed.plan_capacity(*args)
    assert with_history["git_history_bytes"] > 0
    assert with_history["planned_total_bytes"] == (
        with_history["current_ciphertext_bytes"] + with_history["git_history_bytes"]
    )
    rotated = sealed.plan_capacity(*args, rotate=True)
    assert rotated["pending_ciphertext_bytes"] > 0
    assert rotated == sealed.plan_capacity(*args, rotate=True)
    (source / "note").write_bytes(b"updated synthetic")
    rejected = sealed.plan_capacity(*args, budget=1)
    assert not rejected["fits"]
    saved_head, saved_trust = (repo / "HEAD.cce").read_bytes(), trust.read_bytes()
    with pytest.raises(sealed.SealedError, match="storage-budget-exceeded"):
        sealed.publish(*args, budget=1)
    assert (repo / "HEAD.cce").read_bytes() == saved_head
    assert trust.read_bytes() == saved_trust


def test_reverting_or_reselecting_content_reuses_authenticated_retained_objects(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    note = source / "note"
    note.write_bytes(b"original synthetic bytes")
    owner = sealed.create_identity(tmp_path / "owner", "synthetic", "owner", passphrase=b"unlock")
    trust = sealed.create_trust(tmp_path / "trust", owner)
    repo = tmp_path / "cipher"
    args = source, ["note"], repo, owner, trust, [owner.recipient()]
    sealed.publish(*args)
    original_objects = {path.name: path.read_bytes() for path in (repo / "objects").glob("*/*")}
    note.write_bytes(b"changed synthetic bytes")
    sealed.publish(*args)
    note.write_bytes(b"original synthetic bytes")
    assert sealed.plan_capacity(*args)["pending_ciphertext_bytes"] == 0
    assert sealed.publish(*args)["changed"]
    for oid, ciphertext in original_objects.items():
        assert (repo / "objects" / oid[:2] / oid).read_bytes() == ciphertext
    sealed.publish(source, [], repo, owner, trust, [owner.recipient()])
    assert sealed.plan_capacity(*args)["pending_ciphertext_bytes"] == 0
    assert sealed.publish(*args)["changed"]
    sealed.restore(repo, tmp_path / "restored", owner, trust)
    assert (tmp_path / "restored" / "note").read_bytes() == note.read_bytes()


def test_capacity_forecast_covers_actual_first_git_commit(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "note").write_bytes(b"synthetic bytes")
    owner = sealed.create_identity(tmp_path / "owner", "synthetic", "owner", passphrase=b"unlock")
    trust = sealed.create_trust(tmp_path / "trust", owner)
    repo = tmp_path / "cipher"
    args = source, ["note"], repo, owner, trust, [owner.recipient()]
    forecast = sealed.plan_capacity(*args)
    sealed.publish(*args, budget=forecast["planned_total_bytes"])
    _commit_snapshot(repo)
    observed = sealed.storage_status(repo, owner, trust)
    assert (
        observed["ciphertext_tree_bytes"] + observed["git_object_store_bytes"]
        <= forecast["planned_total_bytes"]
    )


def test_protected_cli_file_and_stdin_unlock_never_echo_secrets(tmp_path, capsys, monkeypatch):

    labels = tmp_path / "labels"
    labels.write_text(json.dumps({"app_id": "synthetic", "instance_id": "owner"}))
    phrase = tmp_path / "phrase"
    phrase.write_bytes(b"synthetic-secret-unlock")
    phrase.chmod(0o600)
    key, recipient, trust = (tmp_path / n for n in ("key", "recipient", "trust"))
    assert (
        run(
            [
                "identity",
                "--labels",
                str(labels),
                "--key",
                str(key),
                "--recipient",
                str(recipient),
                "--passphrase-file",
                str(phrase),
            ]
        )
        == 0
    )
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(phrase.read_bytes())))
    assert run(["init", "--key", str(key), "--trust", str(trust), "--passphrase-stdin"]) == 0
    phrase.write_bytes(b"incorrect-secret")
    assert (
        run(
            [
                "init",
                "--key",
                str(key),
                "--trust",
                str(tmp_path / "wrong"),
                "--passphrase-file",
                str(phrase),
            ]
        )
        == 2
    )
    output = capsys.readouterr()
    assert "credential-unlock-failed" in output.err
    assert "secret" not in output.out + output.err
    assert key.read_bytes().find(b"encryption_key") == -1
    legacy = tmp_path / "legacy"
    sealed.create_legacy_identity(legacy, "synthetic", "legacy")
    with pytest.raises(sealed.SealedError, match="credential-unlock-failed"):
        sealed.load_identity(legacy, passphrase=b"any")
