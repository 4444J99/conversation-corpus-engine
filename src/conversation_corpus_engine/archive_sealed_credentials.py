"""Password-protected software credentials, distinct from the publication wire format.

Scrypt parameters are fixed to bound unlock work; no attacker-supplied KDF parameters.
This protects files at rest, not a compromised running process or weak passphrases.
"""

from __future__ import annotations

import json
import os

from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .archive_sealed_crypto import FORMAT, Identity, SealedError, canonical, decrypt, encrypt

KEY_FORMAT = "cce-protected-identity.v1"


def _key(passphrase: bytes | None, salt: bytes) -> bytes:
    if not isinstance(passphrase, bytes) or not passphrase or len(passphrase) > 65536:
        raise SealedError("credential-unlock-failed")
    return Scrypt(salt=salt, length=32, n=32768, r=8, p=1).derive(passphrase)


def protect_identity(identity: Identity, passphrase: bytes) -> bytes:
    salt = os.urandom(16)
    ciphertext = encrypt(_key(passphrase, salt), canonical(identity.to_dict()), KEY_FORMAT)
    return canonical({"format": KEY_FORMAT, "salt": salt.hex(), "ciphertext": ciphertext.hex()})


def unlock_identity(raw: bytes, passphrase: bytes | None) -> Identity:
    try:
        data = json.loads(raw)
        if data["format"] != KEY_FORMAT:
            raise ValueError
        salt = bytes.fromhex(data["salt"])
        if len(salt) != 16:
            raise ValueError
        payload = json.loads(
            decrypt(_key(passphrase, salt), bytes.fromhex(data["ciphertext"]), KEY_FORMAT)
        )
        if payload["format"] != FORMAT:
            raise ValueError
        encryption = bytes.fromhex(payload["encryption_key"])
        signing = bytes.fromhex(payload["signing_key"])
        if len(encryption) != 32 or len(signing) != 32:
            raise ValueError
        return Identity(payload["app_id"], payload["instance_id"], encryption, signing)
    except (ValueError, KeyError, TypeError) as exc:
        raise SealedError("credential-unlock-failed") from exc
