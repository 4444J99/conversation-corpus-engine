import json

from conversation_corpus_engine.archive_sealed import create_identity
from conversation_corpus_engine.archive_sealed_crypto import authenticate
from conversation_corpus_engine.knovv_cli import main


def test_device_enrollment_writes_only_protected_credentials_and_owner_signed_public_grants(
    tmp_path, capsys
):
    unlock = tmp_path / "owner-passphrase"
    unlock.write_bytes(b"synthetic owner passphrase")
    unlock.chmod(0o600)
    device_unlock = tmp_path / "device-passphrase"
    device_unlock.write_bytes(b"synthetic device passphrase")
    device_unlock.chmod(0o600)
    owner = create_identity(
        tmp_path / "owner.key",
        "knovv",
        "publisher",
        passphrase=unlock.read_bytes(),  # allow-secret: runtime-only
    )
    target = tmp_path / "enrollment"
    main(
        [
            "enroll",
            "--owner-key",
            str(tmp_path / "owner.key"),
            "--owner-passphrase-file",
            str(unlock),
            "--device-key",
            str(tmp_path / "device.key"),
            "--device-passphrase-file",
            str(device_unlock),
            "--instance-id",
            "fixture-phone",
            "--output",
            str(target),
        ]
    )
    reply = json.loads(capsys.readouterr().out)
    credential = json.loads((tmp_path / "device.key").read_bytes())
    assert credential["format"] == "cce-protected-identity.v1"
    assert "encryption_key" not in credential and "signing_key" not in credential
    grant = json.loads((target / "grant.json").read_bytes())
    authenticate(grant, owner.signing_public())
    assert grant["revoked"] is False
    assert (target / "recipient.json").is_file()
    assert "passphrase" not in json.dumps(reply)
    assert not (tmp_path / "device.key").stat().st_mode & 0o077
    main(
        [
            "grant",
            "--owner-key",
            str(tmp_path / "owner.key"),
            "--owner-passphrase-file",
            str(unlock),
            "--previous-grant",
            str(target / "grant.json"),
            "--state",
            "revoked",
            "--output",
            str(tmp_path / "revoked.json"),
        ]
    )
    revoked = json.loads((tmp_path / "revoked.json").read_bytes())
    authenticate(revoked, owner.signing_public())
    assert revoked["generation"] == 2 and revoked["revoked"] is True
