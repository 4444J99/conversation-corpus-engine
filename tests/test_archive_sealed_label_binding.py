"""Label binding isolated from the AEAD key check, using invented keys only."""

import json

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey

from conversation_corpus_engine import archive_sealed as sealed
from conversation_corpus_engine.archive_sealed_crypto import FORMAT, Identity, decrypt


@pytest.fixture
def vault(tmp_path):
    source = tmp_path / "private"
    source.mkdir()
    (source / "note").write_bytes(b"synthetic label note")
    owner = sealed.create_legacy_identity(tmp_path / "owner.key", "label-app", "owner")
    device = sealed.create_legacy_identity(tmp_path / "device.key", "label-app", "phone")
    trust = sealed.create_trust(tmp_path / "trust.json", owner)
    sealed.publish(
        source,
        ["note"],
        tmp_path / "cipher",
        owner,
        trust,
        [owner.recipient(), device.recipient()],
    )
    return tmp_path / "cipher", owner, device, trust


def _envelope_decrypts_for_key(repo, identity, trust):
    """True when the wrapped-key AEAD itself accepts `identity`'s X25519 key (labels ignored)."""
    head = json.loads((repo / "HEAD.cce").read_bytes())
    context = f"{FORMAT}:{head['vault']}:{head['generation']}"
    private = X25519PrivateKey.from_private_bytes(identity.encryption_key)
    for envelope in head["envelopes"]:
        data = bytes.fromhex(envelope)
        shared = private.exchange(X25519PublicKey.from_public_bytes(data[:32]))
        try:
            payload = json.loads(decrypt(shared, data[32:], context))
        except ValueError:
            continue
        return payload["recipient"]
    return None


@pytest.mark.parametrize(
    "app_id,instance_id", [("other-app", None), (None, "other-instance"), ("other-app", "x")]
)
@pytest.mark.parametrize("who", ["owner", "device"])
def test_same_key_with_different_label_is_rejected_by_label_binding(
    vault, who, app_id, instance_id
):
    repo, owner, device, trust = vault
    real = owner if who == "owner" else device
    twin = Identity(
        app_id or real.app_id,
        instance_id or real.instance_id,
        real.encryption_key,
        real.signing_key,
    )
    assert twin.recipient()["encryption_public"] == real.recipient()["encryption_public"]
    assert twin.recipient() != real.recipient()
    # Isolation: the AEAD accepts this very key; only the label comparison can refuse it.
    assert _envelope_decrypts_for_key(repo, twin, trust) == real.recipient()
    for operation in (
        lambda: sealed.verify(repo, twin, trust),
        lambda: sealed.list_units(repo, twin, trust),
    ):
        with pytest.raises(sealed.SealedError, match="not-authorized"):
            operation()
    assert sealed.verify(repo, real, trust)["files"] == 1
