"""Invented data only. These tests never access a user's corpus or credentials."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager

import pytest

from conversation_corpus_engine import archive_sealed as sealed
from conversation_corpus_engine.archive_commands import verify_archive
from conversation_corpus_engine.archive_ingest import ArchivePaths
from conversation_corpus_engine.archive_sealed_commands import run
from conversation_corpus_engine.cli import main


@pytest.fixture
def vault(tmp_path):
    source = tmp_path / "private"
    source.mkdir()
    (source / "secret-title.txt").write_bytes(b"SYNTHETIC PRIVATE SENTENCE\r\n\x00" * 100)
    (source / "empty").write_bytes(b"")
    owner = sealed.create_identity(tmp_path / "owner.key", "test-app", "owner")
    device = sealed.create_identity(tmp_path / "device.key", "test-app", "phone")
    trust = sealed.create_trust(tmp_path / "trust.json", owner)
    return source, tmp_path / "cipher", owner, device, trust


def publish(vault, **kwargs):
    source, repo, owner, device, trust = vault
    return sealed.publish(
        source,
        ["secret-title.txt", "empty"],
        repo,
        owner,
        trust,
        [owner.recipient(), device.recipient()],
        **kwargs,
    )


def test_exact_restore_multiple_devices_and_no_plaintext_metadata(vault, tmp_path):
    source, repo, owner, device, trust = vault
    publish(vault)
    for n, identity in enumerate((owner, device)):
        target = tmp_path / f"restored{n}"
        sealed.restore(repo, target, identity, trust)
        assert {p.name: p.read_bytes() for p in target.iterdir()} == {
            p.name: p.read_bytes() for p in source.iterdir()
        }
    forbidden = [
        b"secret-title",
        b"SYNTHETIC PRIVATE",
        b"test-app",
        b"phone",
        hashlib.sha256((source / "secret-title.txt").read_bytes()).hexdigest().encode(),
    ]
    for path in repo.rglob("*"):
        if path.is_file():
            assert all(
                word not in path.read_bytes() + str(path.relative_to(repo)).encode()
                for word in forbidden
            )


def test_same_labels_do_not_authorize_and_wrong_trust_rejected(vault, tmp_path):
    _, repo, owner, _, trust = vault
    publish(vault)
    impostor = sealed.create_identity(tmp_path / "impostor", "test-app", "owner")
    with pytest.raises(sealed.SealedError):
        sealed.verify(repo, impostor, trust)
    other = sealed.create_trust(tmp_path / "other-trust", impostor)
    with pytest.raises(sealed.SealedError):
        sealed.verify(repo, owner, other)


def test_incremental_noop_changes_and_rotation(vault, tmp_path):
    source, repo, owner, device, trust = vault
    publish(vault)
    before = {p: p.read_bytes() for p in repo.rglob("*") if p.is_file()}
    assert publish(vault)["changed"] is False
    assert before == {p: p.read_bytes() for p in repo.rglob("*") if p.is_file()}
    (source / "secret-title.txt").write_text("SYNTHETIC CHANGED")
    publish(vault)
    assert sealed.verify(repo, owner, trust)["files"] == 2
    sealed.publish(source, ["empty"], repo, owner, trust, [owner.recipient()], rotate=True)
    with pytest.raises(sealed.SealedError):
        sealed.verify(repo, device, trust)
    target = tmp_path / "after"
    sealed.restore(repo, target, owner, trust)
    assert [p.name for p in target.iterdir()] == ["empty"]


def test_missing_corrupt_repair_and_atomic_restore(vault, tmp_path):
    _, repo, owner, _, trust = vault
    publish(vault)
    mirror = tmp_path / "mirror"
    shutil.copytree(repo, mirror)
    obj = next((repo / "objects").glob("*/*"))
    obj.unlink()
    target = tmp_path / "restored"
    with pytest.raises(sealed.SealedError):
        sealed.restore(repo, target, owner, trust)
    assert not target.exists()
    assert sealed.repair(repo, mirror, owner, trust)["repaired"] == 1
    obj.write_bytes(b"corrupt")
    with pytest.raises(sealed.SealedError):
        sealed.verify(repo, owner, trust)
    sealed.repair(repo, mirror, owner, trust)
    sealed.restore(repo, target, owner, trust)
    assert target.exists()


def test_tampered_head_and_rollback(vault):
    source, repo, owner, _, trust = vault
    publish(vault)
    old = (repo / "HEAD.cce").read_bytes()
    (source / "empty").write_bytes(b"new version")
    publish(vault)
    (repo / "HEAD.cce").write_bytes(old)
    with pytest.raises(sealed.SealedError, match="rollback"):
        sealed.verify(repo, owner, trust)
    head = json.loads(old)
    head["signature"] = "00" * 64
    (repo / "HEAD.cce").write_text(json.dumps(head))
    with pytest.raises(sealed.SealedError):
        sealed.verify(repo, owner, trust)


def test_private_paths_symlinks_overlap_and_budget(vault, tmp_path):
    source, repo, owner, _, trust = vault
    (source / "link").symlink_to(source / "empty")
    for selection in (["../owner.key"], ["link"], ["/etc/passwd"]):
        with pytest.raises(sealed.SealedError):
            sealed.publish(source, selection, repo, owner, trust, [owner.recipient()])
    with pytest.raises(sealed.SealedError):
        publish(vault, budget=1)
    assert not (repo / "HEAD.cce").exists()
    with pytest.raises(sealed.SealedError):
        sealed.publish(source, ["empty"], source / "cipher", owner, trust, [owner.recipient()])
    public = tmp_path / "git"
    subprocess.run(["git", "init", str(public)], check=True, capture_output=True)
    with pytest.raises(sealed.SealedError):
        sealed.create_identity(public / "key", "a", "b")
    publish(vault)
    with pytest.raises(sealed.SealedError):
        sealed.restore(repo, public / "plaintext", owner, trust)


def test_normalized_message_atoms_and_explicit_authorship(vault, tmp_path):
    source, repo, owner, _, trust = vault
    message = {
        "id": "m1",
        "role": "user",
        "channel": "typed",
        "text": "promptATTACHMENT",
        "typed_chars": 6,
        "ts": "2026-10-18T10:00:00Z",
        "meta": {"author": "unknown"},
    }
    alternate = {
        **message,
        "id": "m2",
        "text": "earlier prompt",
        "role": "assistant",
        "meta": {"model": "specific-model"},
    }
    row = {
        "schema": "archive-conversation.v2",
        "uid": "claude-secret",
        "provider": "claude",
        "model": "synthetic-model",
        "messages": [message],
        "created": "2026-10-18",
        "provenance": {"source_file": "private-export.json", "source_sha256": "source-hash"},
        "extra": {"alternate_messages": [alternate]},
    }
    name = "provider-claude/collection-claude-conversations.jsonl"
    (source / "provider-claude").mkdir()
    (source / name).write_text(json.dumps(row) + "\n")
    explicit = {
        "schema": "cce-knowledge-atom.v1",
        "id": "note1",
        "text": "my prose",
        "authorship": {"author": "operator", "kind": "original", "status": "asserted"},
        "source": {"app": "ios-notes"},
        "timestamps": {"created": None},
        "derived_from": [],
    }
    (source / "notes.atoms.jsonl").write_text(json.dumps(explicit) + "\n")
    sealed.publish(source, [name, "notes.atoms.jsonl"], repo, owner, trust, [owner.recipient()])
    atom = sealed.read_unit(repo, "claude-secret#current#m1", owner, trust)
    assert atom["message"] == message
    assert atom["authorship"]["prompt_owner"] == "unverified"
    assert atom["authorship"]["typed_chars"] == 6
    assert atom["authorship"]["model"] == ""
    assert atom["source"]["provenance"] == row["provenance"]
    assert (
        sealed.read_unit(repo, "claude-secret#alternate#m2", owner, trust)["message"] == alternate
    )
    assert sealed.read_unit(repo, "note1", owner, trust) == explicit
    assert (
        sealed.read_unit(repo, "claude-secret#alternate#m2", owner, trust)["authorship"]["model"]
        == "specific-model"
    )
    restored = tmp_path / "atom-restored"
    sealed.restore(repo, restored, owner, trust)
    assert (restored / name).read_bytes() == (source / name).read_bytes()


def test_existing_ignored_private_store_is_supported(vault, tmp_path):
    _, repo, owner, _, trust = vault
    workspace = tmp_path / "workspace"
    subprocess.run(["git", "init", str(workspace)], check=True, capture_output=True)
    (workspace / ".gitignore").write_text("private/\n")
    source = workspace / "private"
    source.mkdir()
    (source / "note").write_text("synthetic note")
    sealed.publish(source, ["note"], repo, owner, trust, [owner.recipient()])


def test_recipient_change_needs_rotation_and_unknown_files_refused(vault):
    _, repo, owner, _, trust = vault
    publish(vault)
    with pytest.raises(sealed.SealedError, match="rotation"):
        sealed.publish(vault[0], ["empty"], repo, owner, trust, [owner.recipient()])
    (repo / "private.txt").write_text("should never be published")
    with pytest.raises(sealed.SealedError, match="ciphertext"):
        sealed.verify(repo, owner, trust)


def test_restore_existing_target_and_symlink_ciphertext_refused(vault, tmp_path):
    _, repo, owner, _, trust = vault
    publish(vault)
    with pytest.raises(sealed.SealedError):
        sealed.restore(repo, tmp_path, owner, trust)
    obj = next((repo / "objects").glob("*/*"))
    saved = tmp_path / "saved"
    obj.rename(saved)
    obj.symlink_to(saved)
    with pytest.raises(sealed.SealedError, match="symlink"):
        sealed.verify(repo, owner, trust)


def test_cli_no_plaintext_on_success_or_failure(vault, tmp_path, capsys):

    _, repo, _, _, trust = vault
    publish(vault)
    assert (
        run(
            [
                "verify",
                "--repo",
                str(repo),
                "--key",
                str(tmp_path / "owner.key"),
                "--trust",
                str(trust),
            ]
        )
        == 0
    )
    impostor = tmp_path / "impostor"
    sealed.create_identity(impostor, "test-app", "owner")
    assert (
        run(
            [
                "restore",
                "--repo",
                str(repo),
                "--key",
                str(impostor),
                "--trust",
                str(trust),
                "--out",
                str(tmp_path / "secret-title"),
            ]
        )
        == 2
    )
    assert (
        run(
            [
                "verify",
                "--repo",
                str(tmp_path / "secret-title"),
                "--key",
                str(impostor),
                "--trust",
                str(trust),
            ]
        )
        == 2
    )
    output = capsys.readouterr()
    assert "secret-title" not in output.out + output.err
    assert "test-app" not in output.out + output.err
    assert "Traceback" not in output.err


def test_git_history_audit_rejects_deleted_plaintext(vault):
    _, repo, owner, _, trust = vault
    publish(vault)

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            env={
                **__import__("os").environ,
                "GIT_AUTHOR_NAME": "Synthetic",
                "GIT_AUTHOR_EMAIL": "test@example.invalid",
                "GIT_COMMITTER_NAME": "Synthetic",
                "GIT_COMMITTER_EMAIL": "test@example.invalid",
            },
        )

    git("init")
    git("add", "HEAD.cce", "objects")
    git("commit", "-m", "chore: update encrypted archive")
    assert sealed.audit_history(repo, owner, trust)["commits"] == 1
    (repo / "private-title.txt").write_text("synthetic history leak")
    git("add", "private-title.txt")
    git("commit", "-m", "chore: update encrypted archive")
    (repo / "private-title.txt").unlink()
    git("add", "-u")
    git("commit", "-m", "chore: update encrypted archive")
    with pytest.raises(sealed.SealedError, match="history"):
        sealed.audit_history(repo, owner, trust)


def test_chunked_reconstruction_and_independent_trust_checkpoint(vault, tmp_path):
    source, repo, owner, device, trust = vault
    data = bytes(range(256)) * 5000
    (source / "secret-title.txt").write_bytes(data)
    publish(vault)
    device_trust = tmp_path / "device-trust"
    shutil.copyfile(trust, device_trust)
    (source / "empty").write_text("next snapshot")
    publish(vault)
    sealed.restore(repo, tmp_path / "restored-chunked", device, device_trust)
    assert (tmp_path / "restored-chunked/secret-title.txt").read_bytes() == data
    assert json.loads(device_trust.read_bytes()) == json.loads(trust.read_bytes())


def test_malformed_json_cli_error_does_not_echo_content(vault, capsys):
    source, repo, _, _, trust = vault
    (source / "selection").write_text("SYNTHETIC SECRET BROKEN JSON")
    assert (
        run(
            [
                "publish",
                "--repo",
                str(repo),
                "--key",
                str(trust.parent / "owner.key"),
                "--trust",
                str(trust),
                "--source",
                str(source),
                "--selection",
                str(source / "selection"),
                "--recipient",
                str(source / "selection"),
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "[sealed] operation-failed\n"


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_repair_head_uses_exact_private_checkpoint(vault, tmp_path, damage):
    _, repo, owner, _, trust = vault
    publish(vault)
    mirror = tmp_path / "mirror"
    shutil.copytree(repo, mirror)
    if damage == "missing":
        (repo / "HEAD.cce").unlink()
    else:
        (repo / "HEAD.cce").write_bytes(b"SYNTHETIC CORRUPT HEAD")
    assert sealed.repair(repo, mirror, owner, trust)["repaired_head"] is True
    assert sealed.verify(repo, owner, trust)["files"] == 2


def test_recovery_mirror_cannot_roll_back_checkpoint(vault, tmp_path):
    source, repo, owner, _, trust = vault
    publish(vault)
    mirror = tmp_path / "old"
    shutil.copytree(repo, mirror)
    (source / "empty").write_text("changed")
    publish(vault)
    (repo / "HEAD.cce").unlink()
    with pytest.raises(sealed.SealedError):
        sealed.repair(repo, mirror, owner, trust)
    assert not (repo / "HEAD.cce").exists()


def test_opaque_named_plaintext_cannot_survive_publication_or_verification(vault):
    _, repo, owner, _, trust = vault
    hidden = repo / "objects" / "ab" / ("ab" + "0" * 62)
    hidden.parent.mkdir(parents=True)
    hidden.write_bytes(b"SYNTHETIC PRIVATE ORPHAN")
    with pytest.raises(sealed.SealedError):
        publish(vault)
    assert not (repo / "HEAD.cce").exists()
    hidden.unlink()
    publish(vault)
    hidden.write_bytes(b"SYNTHETIC PRIVATE ORPHAN")
    with pytest.raises(sealed.SealedError):
        sealed.verify(repo, owner, trust)


def test_history_audit_rejects_annotated_tag_payload(vault):
    _, repo, owner, _, trust = vault
    publish(vault)

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "Synthetic",
                "GIT_AUTHOR_EMAIL": "test@example.invalid",
                "GIT_COMMITTER_NAME": "Synthetic",
                "GIT_COMMITTER_EMAIL": "test@example.invalid",
            },
        )

    git("init")
    git("add", "HEAD.cce", "objects")
    git("commit", "-m", "chore: update encrypted archive")
    git("tag", "-a", "snapshot", "-m", "SYNTHETIC PRIVATE TAG")
    with pytest.raises(sealed.SealedError):
        sealed.audit_history(repo, owner, trust)


def test_failed_checkpoint_acceptance_does_not_install_restore(vault, tmp_path, monkeypatch):
    _, repo, owner, _, trust = vault
    publish(vault)

    def fail(*args):
        raise sealed.SealedError("checkpoint-advanced")

    monkeypatch.setattr(sealed, "_accept", fail)
    target = tmp_path / "failed-restore"
    with pytest.raises(sealed.SealedError):
        sealed.restore(repo, target, owner, trust)
    assert not target.exists()


def test_repair_rechecks_head_and_checkpoint_under_lock(vault, tmp_path, monkeypatch):
    source, repo, owner, device, trust = vault
    publish(vault)
    mirror = tmp_path / "mirror"
    shutil.copytree(repo, mirror)
    (repo / "HEAD.cce").unlink()
    original_lock = sealed._lock

    @contextmanager
    def interleave(path):
        monkeypatch.setattr(sealed, "_lock", original_lock)
        shutil.copyfile(mirror / "HEAD.cce", repo / "HEAD.cce")
        (source / "empty").write_bytes(b"new snapshot")
        publish(vault)
        with original_lock(path):
            yield

    monkeypatch.setattr(sealed, "_lock", interleave)
    with pytest.raises(sealed.SealedError):
        sealed.repair(repo, mirror, owner, trust)
    assert json.loads((repo / "HEAD.cce").read_bytes())["generation"] == 2
    assert sealed.verify(repo, device, trust)["generation"] == 2


def test_lock_directory_cannot_hide_plaintext(vault):
    _, repo, owner, _, trust = vault
    publish(vault)
    lock = repo / ".cce-lock"
    lock.mkdir()
    (lock / "note").write_text("synthetic private note")
    with pytest.raises(sealed.SealedError):
        sealed.verify(repo, owner, trust)


def test_newly_enrolled_device_verifies_old_epoch_ciphertext(vault, tmp_path):
    source, repo, owner, _, trust = vault
    publish(vault)
    new_device = sealed.create_identity(tmp_path / "new-device", "test-app", "new")
    sealed.publish(
        source,
        ["empty"],
        repo,
        owner,
        trust,
        [owner.recipient(), new_device.recipient()],
        rotate=True,
    )
    assert sealed.verify(repo, new_device, trust)["files"] == 1
    with pytest.raises(sealed.SealedError, match="publisher"):
        sealed.publish(source, ["empty"], repo, new_device, trust, [new_device.recipient()])


def test_full_cli_roundtrip_preserves_existing_archive_interfaces(
    archive_env, tmp_path, monkeypatch, capsys
):
    def cce(*args):
        monkeypatch.setattr(sys, "argv", ["cce", *map(str, args)])
        with pytest.raises(SystemExit) as result:
            main()
        output = capsys.readouterr()
        assert result.value.code == 0, output.err
        return json.loads(output.out)

    env = archive_env
    cce(
        "archive",
        "build",
        "--project-root",
        env.project,
        "--corpus-store-root",
        env.store,
        "--export-root",
        env.export,
    )
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps({"app_id": "fixture-app", "instance_id": "fixture-device"}))
    key, trust, recipient = tmp_path / "key", tmp_path / "trust", tmp_path / "recipient"
    cce("sealed", "identity", "--labels", labels, "--key", key, "--recipient", recipient)
    cce("sealed", "init", "--key", key, "--trust", trust)
    selection = tmp_path / "selection.json"
    selection.write_text(
        json.dumps(
            [str(p.relative_to(env.paths.root)) for p in env.paths.root.rglob("*") if p.is_file()]
        )
    )
    cipher = tmp_path / "cipher"
    cce(
        "sealed",
        "publish",
        "--source",
        env.paths.root,
        "--selection",
        selection,
        "--repo",
        cipher,
        "--key",
        key,
        "--trust",
        trust,
        "--recipient",
        recipient,
    )
    assert cce("sealed", "verify", "--repo", cipher, "--key", key, "--trust", trust)["units"] > 0
    cce(
        "sealed",
        "units",
        "--repo",
        cipher,
        "--key",
        key,
        "--trust",
        trust,
        "--out",
        tmp_path / "unit-index",
    )
    unit_id = json.loads((tmp_path / "unit-index").read_bytes())[0]
    (tmp_path / "unit-id").write_text(unit_id)
    cce(
        "sealed",
        "unit",
        "--repo",
        cipher,
        "--key",
        key,
        "--trust",
        trust,
        "--id-file",
        tmp_path / "unit-id",
        "--out",
        tmp_path / "unit-out",
    )
    assert json.loads((tmp_path / "unit-out").read_bytes())["id"] == unit_id
    restored = tmp_path / "restored-archive"
    cce("sealed", "restore", "--repo", cipher, "--out", restored, "--key", key, "--trust", trust)
    assert verify_archive(ArchivePaths(restored)) == []
    for path in env.paths.root.rglob("*"):
        if path.is_file():
            assert path.read_bytes() == (restored / path.relative_to(env.paths.root)).read_bytes()
