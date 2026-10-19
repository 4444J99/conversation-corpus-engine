"""Versioned cryptographic building blocks; no archive or Git side effects.

The envelope is deliberately a local key-possession boundary, not runtime attestation.
cryptography is an opt-in dependency; the existing archive commands do not import this module.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass, field
from typing import Any

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

FORMAT = "cce-sealed.v1"
PAD = 4096


class SealedError(ValueError):
    """Only fixed, non-sensitive error codes cross the CLI boundary."""


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def derive(secret: bytes, purpose: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=FORMAT.encode() + b":" + purpose
    ).derive(secret)


def address(secret: bytes, data: bytes) -> str:
    return hmac.new(derive(secret, b"address"), data, hashlib.sha256).hexdigest()


def encrypt(secret: bytes, data: bytes, context: str) -> bytes:
    nonce = os.urandom(12)
    body = len(data).to_bytes(8, "big") + data
    body += os.urandom((-len(body)) % PAD)
    return nonce + ChaCha20Poly1305(derive(secret, b"aead")).encrypt(nonce, body, context.encode())


def decrypt(secret: bytes, data: bytes, context: str) -> bytes:
    try:
        body = ChaCha20Poly1305(derive(secret, b"aead")).decrypt(
            data[:12], data[12:], context.encode()
        )
        size = int.from_bytes(body[:8], "big")
        if len(body) < 8 or size > len(body) - 8:
            raise SealedError("invalid-object")
        return body[8 : 8 + size]
    except (InvalidTag, ValueError) as exc:
        raise SealedError("authentication-failed") from exc


@dataclass(frozen=True)
class Identity:
    app_id: str
    instance_id: str
    encryption_key: bytes = field(repr=False)
    signing_key: bytes = field(repr=False)

    def recipient(self) -> dict[str, str]:
        return {
            "app_id": self.app_id,
            "instance_id": self.instance_id,
            "encryption_public": X25519PrivateKey.from_private_bytes(self.encryption_key)
            .public_key()
            .public_bytes_raw()
            .hex(),
        }

    def signing_public(self) -> str:
        return (
            Ed25519PrivateKey.from_private_bytes(self.signing_key)
            .public_key()
            .public_bytes_raw()
            .hex()
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "format": FORMAT,
            "app_id": self.app_id,
            "instance_id": self.instance_id,
            "encryption_key": self.encryption_key.hex(),
            "signing_key": self.signing_key.hex(),
        }


def wrap(secret: bytes, recipient: dict[str, str], context: str) -> str:
    ephemeral = X25519PrivateKey.generate()
    public = X25519PublicKey.from_public_bytes(bytes.fromhex(recipient["encryption_public"]))
    shared = ephemeral.exchange(public)
    payload = canonical({"secret": secret.hex(), "recipient": recipient})
    return (ephemeral.public_key().public_bytes_raw() + encrypt(shared, payload, context)).hex()


def unwrap(envelopes: list[str], identity: Identity, context: str) -> bytes:
    for envelope in envelopes:
        try:
            data = bytes.fromhex(envelope)
            shared = X25519PrivateKey.from_private_bytes(identity.encryption_key).exchange(
                X25519PublicKey.from_public_bytes(data[:32])
            )
            payload = json.loads(decrypt(shared, data[32:], context))
            if payload["recipient"] == identity.recipient():
                secret = bytes.fromhex(payload["secret"])
                if len(secret) == 32:
                    return secret
        except (ValueError, KeyError, TypeError):
            continue
    raise SealedError("not-authorized")


def sign(head: dict[str, Any], identity: Identity) -> dict[str, Any]:
    return {
        **head,
        "signature": Ed25519PrivateKey.from_private_bytes(identity.signing_key)
        .sign(canonical(head))
        .hex(),
    }


def authenticate(head: dict[str, Any], owner_public: str) -> None:
    try:
        unsigned = {k: v for k, v in head.items() if k != "signature"}
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(owner_public)).verify(
            bytes.fromhex(head["signature"]), canonical(unsigned)
        )
    except (InvalidSignature, ValueError, KeyError, TypeError) as exc:
        raise SealedError("invalid-signature") from exc


def object_signature_payload(data: bytes, oid: str, vault: str) -> bytes:
    return canonical(
        {
            "format": FORMAT,
            "purpose": "object",
            "vault": vault,
            "address": oid,
            "cipher_sha256": digest(data),
        }
    )


def sign_object(data: bytes, oid: str, vault: str, identity: Identity) -> bytes:
    signature = Ed25519PrivateKey.from_private_bytes(identity.signing_key).sign(
        object_signature_payload(data, oid, vault)
    )
    return signature + data


def authenticate_object(data: bytes, oid: str, vault: str, owner_public: str) -> None:
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(owner_public)).verify(
            data[:64], object_signature_payload(data[64:], oid, vault)
        )
    except (InvalidSignature, ValueError) as exc:
        raise SealedError("unauthenticated-retained-object") from exc
